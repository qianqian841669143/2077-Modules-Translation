# -*- coding: utf-8 -*-
"""本地模型「这串是文案还是标识符」的判别能力实测。

    python tools/probe_local_label.py
    python tools/probe_local_label.py --api-base http://127.0.0.1:8080/v1 --model <id>
    python tools/probe_local_label.py --rounds context

只读：不写任何文件，只发 HTTP 请求给本地模型。用的正是工具自己的
`mt_client.chat()`（同一套关思考参数），所以结果能代表「接进工具后」的表现。

两轮对比：
  context —— 给源码/JSON 片段，让其按位置判断（工具真实场景）
  bare    —— 只给字符串本身（验证「上下文是不是必需」）
"""
import argparse
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src", "localization"))

import mt_client  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SYSTEM = """你是《赛博朋克 2077》模组的翻译质检员。
给你一段 Lua / JSON 源码片段，以及其中一个字符串。
判断这个字符串在游戏里的角色，只能回答一个词：

TEXT —— 玩家能看到的文字：界面标签、设置项名称、说明、短信、任务名、物品名。
KEY  —— 程序用的标识符：设置系统的变量名、查表键、资源路径、按键名、内部 ID、模块缩写。
ENUM —— 固定档位的枚举值，比如 OpticalCamo、assassinate、NUSA。

判断依据是它在片段里的**位置**，不是它像不像英文单词。
不要解释，只输出 TEXT、KEY、ENUM 三者之一。"""

SYSTEM_FEWSHOT = SYSTEM + """

例 1：
片段：{ group = '/graphics/advanced', name = 'Anisotropy' }
目标：Anisotropy
答案：KEY

例 2：
片段：{ name = 'Enable Mod', description = 'Turns the mod on.' }
目标：Enable Mod
答案：TEXT

例 3：
片段：shortName = 'CET',
目标：CET
答案：KEY

例 4：
片段：locations[0].name = 'Neon Sky Rooftop Bar'
目标：Neon Sky Rooftop Bar
答案：TEXT

例 5：
片段：GetText('UI-Labels-Resume')
目标：UI-Labels-Resume
答案：KEY"""

# (期望, 目标串, 片段, 备注)
CASES = [
    # ---- 该翻：玩家能看到的文字
    ("TEXT", "Enable Mod", "{ name = 'Enable Mod', description = 'Turns the mod on.' }", "MCM 设置项名"),
    ("TEXT", "Auto Drive", 'shortName = "Auto Drive",', "v2.51.2 那类真文案"),
    ("TEXT", "Base Variant", "settings[0].label = 'Base Variant'", "设置项标签"),
    ("TEXT", "Shard reader (open, hum, close)", "groups[17].label = 'Shard reader (open, hum, close)'", "音效组显示名"),
    ("TEXT", "Neon Sky Rooftop Bar", "locations[0].name = 'Neon Sky Rooftop Bar'", "地点显示名"),
    ("TEXT", "Pool Table", "favorites[0].name = 'Pool Table'", "收藏项显示名"),
    ("TEXT", "Bass boost", "name = 'Bass boost'", "预设名"),
    ("TEXT", "Draw Distance", '@runtimeProperty("ModSettings.displayName", "Draw Distance")', "MCM 显示名"),
    ("TEXT", "Not enough eddies", "showToast('Not enough eddies', 3)", "提示文本"),
    ("TEXT", "Rooftop Bars", "locations[0].category = 'Rooftop Bars'", "分类显示名"),
    # ---- 不能翻：程序用的标识符
    ("KEY", "Anisotropy", "{ group = '/graphics/advanced', name = 'Anisotropy' }", "Ultra+ 崩溃元凶"),
    ("KEY", "Vignette", "{ group = '/graphics/basic', name = 'Vignette' }", "同上"),
    ("KEY", "Resolution", "{ group = '/video/display', name = 'Resolution' }", "同上"),
    ("KEY", "Gamma", "{ group = '/video/display', name = 'Gamma' }", "同上"),
    ("KEY", "Sieja", "{ group = '/accessibility', name = 'Sieja' }", "同上"),
    ("KEY", "Overheads", "{ group = '/accessibility/subtitles', name = 'Overheads' }", "同上"),
    ("KEY", "Cinematic", "{ group = '/accessibility/subtitles', name = 'Cinematic' }", "同上"),
    ("KEY", "CET", "shortName = 'CET',", "v2.51.1 缩写徽标"),
    ("KEY", "RCF", "shortName = 'RCF',", "同上"),
    ("KEY", "IK_5", '{ "key": "IK_5", "type": "OpticalCamo" }', "按键绑定"),
    ("KEY", "breezycomfy_rug_pink", "{ name = 'breezycomfy_rug_pink', path = 'breezy_base\\\\x.ent' }", "道具内部 ID"),
    ("KEY", "UI-Labels-Resume", "GetText('UI-Labels-Resume')", "查表键"),
    ("KEY", "Workspot_Position_And_Rotation", "{ name = 'Workspot_Position_And_Rotation' }", "记录名"),
    ("KEY", "Tutorials", "{ group = '/gameplay/misc', name = 'Tutorials' }", "设置系统变量名"),
    # ---- 不能翻：枚举
    ("ENUM", "OpticalCamo", '{ "key": "IK_5", "type": "OpticalCamo" }', "义体槽类型"),
    ("ENUM", "NUSA", "Items.Cap_02_old_02.faction = 'NUSA'", "阵营枚举"),
    ("ENUM", "assassinate", '{ "contract_type": "assassinate" }', "合同类型"),
]

LABEL_RE = re.compile(r"\b(TEXT|KEY|ENUM)\b")
FOLD = {"KEY": "TEXT"}  # 评分口径：KEY 被答成 TEXT 才算致命（会被翻坏）


def ask(api_base, model, target, context, system=None):
    msg = "片段：\n%s\n\n目标字符串：%s" % (context, target)
    out = mt_client.chat(
        api_base, model,
        [{"role": "system", "content": system or SYSTEM},
         {"role": "user", "content": msg}],
        temperature=0.0, timeout=60, max_tokens=16,
    )
    m = LABEL_RE.search(out.upper())
    return (m.group(1) if m else "?"), out.strip()[:60]


def run_round(api_base, model, name, with_context):
    system = SYSTEM_FEWSHOT if name == "fewshot" else SYSTEM
    print("\n" + "=" * 72)
    print("round: %s (%s)" % (name, "带源码片段" if with_context else "只给字符串"))
    print("-" * 72)
    ok = bad = err = 0
    fatal = []       # 该判 KEY/ENUM 却答 TEXT —— 会被翻坏
    missed = []      # 该判 TEXT 却答 KEY/ENUM —— 会漏翻
    t0 = time.time()
    for expect, target, ctx, note in CASES:
        try:
            got, raw = ask(api_base, model, target, ctx if with_context else "(无上下文)", system)
        except Exception as ex:
            got, raw = "ERR", str(ex)[:60]
        if got == expect:
            ok += 1
        elif got == "?":
            err += 1
            print("  ??  %-34r 期望 %-4s 实际无法解析: %s" % (target[:34], expect, raw))
        else:
            bad += 1
            mark = "!!" if (expect in ("KEY", "ENUM") and got == "TEXT") else "  "
            print("  %s  %-34r 期望 %-4s 实际 %-4s | %s" % (mark, target[:34], expect, got, note))
            if expect in ("KEY", "ENUM") and got == "TEXT":
                fatal.append(target)
            elif expect == "TEXT":
                missed.append(target)
    n = len(CASES)
    print("-" * 72)
    print("  正确 %d/%d (%.0f%%) | 错 %d | 无法解析 %d | 用时 %.1fs"
          % (ok, n, 100.0 * ok / n, bad, err, time.time() - t0))
    print("  致命误判（KEY/ENUM→TEXT，会被翻坏）: %d %s" % (len(fatal), fatal or ""))
    print("  漏判（TEXT→KEY/ENUM，会漏翻）: %d %s" % (len(missed), missed or ""))
    return ok, n, fatal, missed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-base", default="http://127.0.0.1:8080/v1")
    ap.add_argument("--model", default=r"D:\Models\Qwen3.5-9B\Qwen3.5-9B-Q4_K_M.gguf")
    ap.add_argument("--rounds", default="context,bare")
    args = ap.parse_args()

    print("api_base: %s\nmodel   : %s\ncases   : %d" % (args.api_base, args.model, len(CASES)))
    for name in [r.strip() for r in args.rounds.split(",") if r.strip()]:
        run_round(args.api_base, args.model, name, with_context=(name != "bare"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
