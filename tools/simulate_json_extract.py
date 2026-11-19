# -*- coding: utf-8 -*-
"""只读模拟：JSON 提取规则在全量 mod 上会收什么。

**不改引擎、不写 mod 文件**。它实现的是准备写进 `mod_text_engine.py`
的那套判据，用来在动源码之前先看结果：

    文件级  整份 json 里没有任何「强文案字段」-> 整份排除
    记录级  强字段自己就能收；name/title/label/category/section/group
            只在该条记录里有强字段时才收
    值级    路径/颜色/枚举/查表键/纯中文 一律不收

用法::

    python tools/simulate_json_extract.py
    python tools/simulate_json_extract.py --root "E:\\Vortex Mods\\cyberpunk2077"
    python tools/simulate_json_extract.py --verbose        # 逐文件列字段
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict

if hasattr(sys, "path"):
    _HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, _HERE)
    sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from scan_json_text import classify_name, classify_value, RUNTIME_TOP_KEYS, RUNTIME_PATH_RE
import mod_text_engine as E

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------- 判据
# 字段判据直接从引擎引，不再抄一份：抄一份的下场是「模拟器说 17 条、
# 引擎出 19 条」，改了一边忘了另一边。这里唯一自己实现的是「拒绝原因」的
# 细分（引擎只返回结果，不解释为什么）。
STRONG_KEYS = E.JSON_STRONG_KEYS
CONDITIONAL_KEYS = E.JSON_COND_KEYS
DENY_KEYS = E.JSON_DENY_KEYS
IS_SKIP_NAME = E.is_json_skipped

LOC_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:[-.][A-Za-z0-9_]+){2,}$")
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
PATH_RE = re.compile(r"[\\/]{1,2}[\w.\-]+[\\/]|^[\w.\-]+\.(ent|app|mesh|xl|archive|json|png|dds)$", re.I)
COLOR_RE = re.compile(r"^(#?[0-9a-f]{6,8}|rgba?\([^)]*\))$", re.I)
ENUM_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def is_loc_key(s):
    """CP2077 本地化查表键形态（UI-Labels-Resume）。"""
    t = (s or "").strip()
    if not t or len(t) < 10 or " " in t:
        return False
    if not LOC_KEY_RE.match(t):
        return False
    segs = re.split(r"[-.]", t)
    return len(segs) >= 3 and sum(1 for x in segs if x[:1].isupper()) >= 2


def value_ok(s):
    """值层面还有没有资格进待翻表。返回 (ok, 原因)。"""
    t = (s or "").strip()
    if len(t) < 2:
        return False, "太短"
    if t in ("true", "false", "null"):
        return False, "结构值"
    if CJK_RE.search(t):
        return False, "已是中文"
    if PATH_RE.search(t) and " " not in t:
        return False, "路径"
    if COLOR_RE.match(t):
        return False, "颜色"
    if is_loc_key(t):
        return False, "查表键"
    if ENUM_RE.match(t) and len(t) < 40:
        return False, "枚举"
    # 全大写 / 下划线的内部标识符
    if re.match(r"^[A-Z0-9_]{3,}$", t):
        return False, "标识符"
    return True, ""


def walk_records(obj, ptr, out):
    """收集所有 dict 记录 (指针, 键集合)。"""
    if isinstance(obj, dict):
        keys = frozenset(str(k) for k in obj.keys())
        out.append((ptr, obj, keys))
        for k, v in obj.items():
            walk_records(v, "%s/%s" % (ptr, k), out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            walk_records(v, "%s/%d" % (ptr, i), out)


def extract_rules(obj):
    """返回 (条目列表, 是否激活, 字段计数, 拒绝原因计数)。

    条目 = (pointer, 字段名, 值, 档)  档 = "strong" / "cond"
    """
    recs = []
    walk_records(obj, "", recs)
    # ---- 文件级：整份里有没有强文案字段
    active = any(keys & STRONG_KEYS for _p, _o, keys in recs)
    items, stats, rejects = [], defaultdict(int), defaultdict(int)
    if not active:
        return items, False, stats, rejects
    for ptr, obj_, keys in recs:
        strong_here = keys & STRONG_KEYS
        for k, v in obj_.items():
            k = str(k)
            kl = k.lower()
            if not isinstance(v, str):
                continue
            if kl in DENY_KEYS:
                rejects["deny"] += 1
                continue
            if kl in STRONG_KEYS:
                tier = "strong"
            elif kl in CONDITIONAL_KEYS and strong_here:
                tier = "cond"
            else:
                continue
            ok, why = value_ok(v)
            if not ok:
                rejects[why] += 1
                continue
            p = "%s/%s" % (ptr, k) if ptr else "/" + k
            items.append((p, k, v, tier))
            stats[k] += 1
    return items, active, stats, rejects


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", default=None)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--show", default="", help="只详列文件名含这个子串的")
    args = ap.parse_args(argv)

    roots = args.root or [
        r"E:\SteamLibrary\steamapps\common\Cyberpunk 2077\bin\x64\plugins\cyber_engine_tweaks\mods",
        r"E:\Vortex Mods\cyberpunk2077",
    ]
    for root in roots:
        if not os.path.isdir(root):
            print("[skip] %s" % root)
            continue
        per_file, skipped = [], []
        total = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in (".git", "node_modules", "__pycache__")]
            for fn in filenames:
                if not fn.lower().endswith(".json"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace("\\", "/")
                if classify_name(rel) != "data":
                    skipped.append((rel, "locale/runtime"))
                    continue
                if IS_SKIP_NAME(fn):
                    skipped.append((rel, "运行时状态/工具元数据"))
                    continue
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        raw = f.read()
                    obj = json.loads(raw)
                except Exception as ex:
                    skipped.append((rel, "解析失败: %s" % type(ex).__name__))
                    continue
                items, active, stats, rejects = extract_rules(obj)
                if not active:
                    skipped.append((rel, "无强文案字段"))
                    continue
                if not items:
                    skipped.append((rel, "有强字段但值全被挡(%s)" % (
                        ",".join("%s=%d" % kv for kv in sorted(rejects.items())) or "空")))
                    continue
                per_file.append((len(items), rel, items, stats, rejects))
                total += len(items)
        per_file.sort(reverse=True)
        print("=" * 100)
        print("root: %s" % root)
        print("会收 %d 条 / %d 个文件 | 排除 %d 个文件" % (total, len(per_file), len(skipped)))
        print("=" * 100)
        print("%-52s %6s  %s" % ("文件", "条数", "字段"))
        for n, rel, items, stats, rejects in per_file[: args.top]:
            print("%-52s %6d  %s" % (rel[-52:], n,
                                     ", ".join("%s=%d" % kv
                                               for kv in sorted(stats.items(), key=lambda x: -x[1]))))
        print("\n被排除的文件:")
        for rel, why in skipped:
            print("  %-58s %s" % (rel[-58:], why))
        if args.verbose or args.show:
            print("\n" + "-" * 100)
            for n, rel, items, stats, rejects in per_file:
                if args.show and args.show.lower() not in rel.lower():
                    continue
                print("\n[%s] %d 条  拒绝=%s" % (rel, n, dict(rejects)))
                for p, k, v, tier in items[:6]:
                    print("    %-6s %-10s %s = %r" % (tier, k, p, v[:80]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
