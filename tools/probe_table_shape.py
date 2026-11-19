# -*- coding: utf-8 -*-
"""验证：让本地模型「看整张表的形状」来判断某个字段的角色。

和 probe_local_label.py 的区别：那边一次只给一条记录（逐条裁决，实测 67~78%），
这里给一张表的 5~8 条同样形状的记录，问「表里 name 字段是显示名还是标识符」。

    python tools/probe_table_shape.py
    python tools/probe_table_shape.py --api-base http://127.0.0.1:8080/v1 --model <id>

只读，不发任何写请求。
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import mt_client  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SYSTEM = """你在看《赛博朋克 2077》模组里的一张 Lua 表。
给你这张表的若干条记录，以及要判断的字段名。判断该字段扮演什么角色：

DISPLAY —— 玩家能看到的显示名（会被画到设置界面/菜单上）
KEY     —— 程序用的标识符（设置系统的变量名、查表键、资源 ID、记录名）

下结论前先看**结构**，不要被值看起来像不像英文带跑：
- 同一条记录里还有 group / section / key / id / path 这类字段时，name 几乎一定是
  KEY，哪怕它的值看起来像界面文字（MasterVolume、SfxVolume 都是键）；
- 只有 name 旁边是 description / tooltip / label 这类描述字段，或值是
  LocalizableString(...) 这种显示名包装时，才算 DISPLAY。
只回答 DISPLAY 或 KEY。"""

MODS = r"E:\SteamLibrary\steamapps\common\Cyberpunk 2077\bin\x64\plugins\cyber_engine_tweaks\mods"

# (期望, 字段, 说明, 相对路径, 取样本的 needle)
TABLES = [
    ("KEY", "name", "Ultra+ GameMenu.settings（系统组路径）",
     r"UltraPlus\lib\GameMenu.lua", "group = "),
    ("KEY", "name", "Ultra+ GameMenu.settings 音频段（同表另一段）",
     r"UltraPlus\lib\GameMenu.lua", "'/audio/volume'"),
    ("DISPLAY", "label", "Ultra+ GameMenu.sections 的分组名",
     r"UltraPlus\lib\GameMenu.lua", "label = "),
    ("KEY", "name", "AppearanceCreatorMod TweakDB 记录名",
     r"E:\Vortex Mods\cyberpunk2077\Appearance Creator Mod 10795 2.2.0 2026-10-21T20-54Z sHlUHDDyW\bin\x64\plugins\cyber_engine_tweaks\mods\AppearanceCreatorMod\Game\Requirements.lua",
     "get = function"),
    ("DISPLAY", "label", "AppearanceCreatorMod 派系下拉",
     r"E:\Vortex Mods\cyberpunk2077\Appearance Creator Mod 10795 2.2.0 2026-10-21T20-54Z sHlUHDDyW\bin\x64\plugins\cyber_engine_tweaks\mods\AppearanceCreatorMod\Data\CharacterInfo.lua",
     "record = "),
    ("DISPLAY", "name", "AMM scan 分类（LocalizableString）",
     r"AppearanceMenuMod\Modules\scan.lua", "cname = "),
    ("DISPLAY", "name", "AMM scan 动作项",
     r"AppearanceMenuMod\Modules\scan.lua", "actions = function"),
    ("DISPLAY", "name", "AMM 相机姿态名",
     r"AppearanceMenuMod\Modules\tools.lua", "vec = Vector4"),
    ("DISPLAY", "name", "AMM 目标类型（type 是数字）",
     r"AppearanceMenuMod\Modules\scan.lua", "type = 1}"),
    ("DISPLAY", "label", "AMM 弹窗按钮",
     r"AppearanceMenuMod\init.lua", "action = '"),
    ("DISPLAY", "name", "AMM 表情选项（key 与 name 同值）",
     r"AppearanceMenuMod\init.lua", "cat_name = "),
    ("DISPLAY", "name", "NCPDFR 罪名（sev 是数字）",
     r"NCPDFR\modules\peds.lua", "sev = "),
    ("DISPLAY", "name", "NCPDFR 警衔（xp 是数字）",
     r"NCPDFR\config.lua", "xp = "),
    ("DISPLAY", "label", "NCPDFR 设置页签（id + label）",
     r"NCPDFR\modules\settings.lua", "id = '"),
    ("DISPLAY", "name", "BountyContracts 公寓坐标表",
     r"BountyContracts\init.lua", "r = 120"),
    ("DISPLAY", "title", "CET 内衣移除的设置项标题",
     r"CET_player_underwear_removal_extended\init.lua", "tooltips = "),
    ("KEY", "name", "AMM 道具模板（name + path + category）",
     r"AppearanceMenuMod\Collabs\Custom Props\breezycomfylivingproppack.lua", "path = "),
    ("KEY", "key", "ITP 滑块定义（key/group/min/max）",
     r"immersive_third_person\init.lua", "format = "),
]


def sample(rel, needle, n=6):
    path = os.path.join(MODS, rel)
    if not os.path.isfile(path):
        return ""
    out = []
    for line in open(path, encoding="utf-8", errors="replace").read().splitlines():
        s = line.strip()
        if needle in s and s not in out:
            out.append(s)
        if len(out) >= n:
            break
    return "\n".join(out)


# 旧版 4 条手写样本，保留作对照；main() 现在读 TABLES + sample()
_LEGACY_CASES = [
    ("KEY", "设置系统变量表（Ultra+ GameMenu.settings）", """{
  { group = '/graphics/advanced', name = 'ContactShadows' },
  { group = '/graphics/advanced', name = 'FacialTangentUpdates' },
  { group = '/graphics/advanced', name = 'ShadowMeshQuality' },
  { group = '/graphics/advanced', name = 'LocalShadowsQuality' },
  { group = '/graphics/advanced', name = 'MirrorQuality' },
  { group = '/graphics/advanced', name = 'LODPreset' },
}"""),
    ("DISPLAY", "MCM 设置项表（有 description 兄弟字段）", """{
  { name = 'Enable Mod', description = 'Turns the mod on.' },
  { name = 'Show Marker', description = 'Show the map marker.' },
  { name = 'Debug Log', description = 'Write extra lines to the log.' },
  { name = 'Max Distance', description = 'How far away targets spawn.' },
}"""),
    ("KEY", "道具模板表（有 path / category）", """{
  { name = 'breezycomfy_marble_table_pink', path = 'breezy_base\\\\a.ent', category = 'Other' },
  { name = 'breezycomfy_rug_pink', path = 'breezy_base\\\\b.ent', category = 'Other' },
  { name = 'breezycomfy_comfy_bed_2', path = 'breezy_base\\\\c.ent', category = 'Other' },
}"""),
    ("DISPLAY", "地点包（locations）", """{
  { name = 'Neon Sky Rooftop Bar', category = 'Rooftop Bars' },
  { name = 'Afterlife Back Room', category = 'Clubs' },
  { name = 'Judy\\'s Apartment', category = 'Homes' },
}"""),
]

LABEL_RE = re.compile(r"\b(DISPLAY|KEY)\b")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-base", default="http://127.0.0.1:8080/v1")
    ap.add_argument("--model", default=r"D:\Models\Qwen3.5-9B\Qwen3.5-9B-Q4_K_M.gguf")
    args = ap.parse_args()

    ok = 0
    fatal = []
    print("api_base: %s | tables: %d" % (args.api_base, len(TABLES)))
    for expect, field, note, rel, needle in TABLES:
        snippet = sample(rel, needle)
        if not snippet:
            print("  --   %-44s (样本为空，跳过)" % note)
            continue
        try:
            out = mt_client.chat(
                args.api_base, args.model,
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": "这张表里 `%s` 字段：\n%s" % (field, snippet)}],
                temperature=0.0, timeout=60, max_tokens=16,
            )
        except Exception as ex:
            print("  ERR  %s | %s" % (note, ex))
            continue
        m = LABEL_RE.search(out.upper())
        got = m.group(1) if m else "?"
        flag = "OK " if got == expect else "!! "
        if got == expect:
            ok += 1
        elif expect == "KEY":
            fatal.append(note)
        print("  %s%-44s 期望 %-7s 实际 %-7s" % (flag, note, expect, got))
    print("\n  %d/%d | 致命误判(KEY→DISPLAY，会翻坏): %d %s"
          % (ok, len(TABLES), len(fatal), fatal or ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
