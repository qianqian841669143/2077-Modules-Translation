# -*- coding: utf-8 -*-
"""只读扫描：把 mod 目录里的 .json 分类，并统计「看起来是给玩家看的文本」。

用法::

    python tools/scan_json_text.py
    python tools/scan_json_text.py --root "E:\\Vortex Mods\\cyberpunk2077"
    python tools/scan_json_text.py --root <dir> --json out.json

这个脚本**不写任何文件**（--json 除外，那是你自己指定的输出路径）。
它只回答三个问题：

1. 这些 json 里，哪些是语言包 / 运行时状态 / 配置，哪些是「数据表」；
2. 数据表里，哪些字段的值像文案（句子）、像标识符、像枚举标签；
3. 每个模组因此会新增多少条待翻文本。

判据都是形状（长度、空格、下划线、大小写），不做语义判断；语义拿不准的
交给 --json 报告里列出的候选，由人（或本地模型打标）再裁决。
"""
import argparse
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LANG_DIRS = {"languages", "language", "locale", "locales", "localization", "i18n", "lang"}

# 语言包文件名: en-us.json / zh-cn-sms.json / en_us_template/ 之类
LOCALE_RE = re.compile(r"(^|[/\\])[a-z]{2}[-_][a-z]{2,4}([-_][a-z0-9]+)*\.json$", re.I)
LANG_TAG_RE = re.compile(r"(?:^|[-_.\s])(zh[-_]cn|zh[-_]tw|en[-_]us|en[-_]gb|ja[-_]jp|jp[-_]jp|ko[-_]kr|kr[-_]kr)(?:[-_.\s]|$)", re.I)

# 运行时状态 / 用户配置：翻了会坏
RUNTIME_NAMES = {
    "config.json", "phonelog.json", "spots.json", "gangspots.json",
    "gangtypes.json", "scan_results.json", "settings.json", "state.json",
    "mod_user_settings.json", "presets.json", "user.json", "loot.json",
    "gpu-survey-result.json", "scene-diagnostic.json", "untracked_saves_cache.json",
}
RUNTIME_SUFFIX = ("_cache.json", "-cache.json")
RUNTIME_PREFIX = ("settings-",)

# 内容特征：运行时/存档/序列化数据的 top-level 键。命中就提示「别翻」。
RUNTIME_TOP_KEYS = {"campaigns", "$type", "Header", "Data", "favorites", "slots", "mods"}
RUNTIME_PATH_RE = re.compile(r"(snapshot|cache|userdata|/saves?/|_settings|settings[-_]|phonelog|gangspots)", re.I)
SERIALIZED_PATH_RE = re.compile(r"\.json\.json$", re.I)   # WolvenKit 解包残留

PATHLIKE_RE = re.compile(r"[\\/]{1,2}[\w.\-]+[\\/]|^[\w.\-]+\.(ent|app|mesh|xl|archive|json|png|dds)$", re.I)
ENUM_RE = re.compile(r"^[a-z][a-z0-9_]*$")
COLOR_RE = re.compile(r"^(#?[0-9a-f]{6,8}|rgba?\([^)]*\))$", re.I)


def classify_name(rel):
    """按路径/文件名归类: locale / runtime / data"""
    base = os.path.basename(rel).lower()
    parts = [p.lower() for p in rel.replace("\\", "/").split("/")[:-1]]
    if LOCALE_RE.search(rel):
        return "locale"
    if "en-us_template" in rel.lower() or "uiStrings.json".lower() == base:
        return "locale"
    if any(d in LANG_DIRS for d in parts) and (LANG_TAG_RE.search(base) or LOCALE_RE.search(rel)):
        return "locale"
    if base in RUNTIME_NAMES or base.endswith(RUNTIME_SUFFIX) or base.startswith(RUNTIME_PREFIX):
        return "runtime"
    return "data"


def classify_value(s):
    """true / false 之类的结构值先排除，再按形状分档。"""
    if not s or not s.strip():
        return None
    if s in ("", "true", "false", "null"):
        return None
    if len(s) < 2:
        return None
    if PATHLIKE_RE.search(s) and " " not in s:
        return "path"
    if COLOR_RE.match(s.strip()):
        return "color"
    if ENUM_RE.match(s) and len(s) < 40:
        return "enum"
    words = s.split()
    if len(words) >= 5 and len(s) >= 30:
        return "sentence"
    if " " in s and len(s) >= 8:
        return "phrase"
    if re.match(r"^[A-Z][\w'’\-]*(?:\s+[A-Z0-9][\w'’\-]*){0,3}$", s):
        return "label"
    return "other"


def walk_strings(obj, path, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            walk_strings(v, path + ("." + str(k) if path else str(k)), out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            walk_strings(v, path + "[%d]" % i, out)
    elif isinstance(obj, str):
        out.append((path, obj))


def scan(root):
    mods = {}
    totals = {"files": 0, "locale": 0, "runtime": 0, "data": 0, "unparsable": 0, "bytes": 0}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in (".git", "node_modules", "__pycache__")]
        for fn in filenames:
            if not fn.lower().endswith(".json"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            mod = rel.split(os.sep)[0]
            m = mods.setdefault(mod, {"files": [], "kinds": {}, "rows": {}, "fields": {}, "has_zh": False,
                                      "samples": {}, "bytes": 0, "hints": {}})
            totals["files"] += 1
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            m["bytes"] += size
            totals["bytes"] += size
            kind = classify_name(rel)
            m["kinds"][kind] = m["kinds"].get(kind, 0) + 1
            totals[kind if kind != "unparsable" else "unparsable"] = totals.get(kind, 0) + 1
            if LANG_TAG_RE.search(rel) and re.search(r"zh[-_]cn", rel, re.I):
                m["has_zh"] = True
            if kind != "data":
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    obj = json.loads(f.read())
            except Exception:
                totals["unparsable"] += 1
                m["kinds"]["unparsable"] = m["kinds"].get("unparsable", 0) + 1
                continue
            strings = []
            walk_strings(obj, "", strings)
            tops = set(p.split(".")[0].split("[")[0] for p, _s in strings)
            hint = ""
            if tops & RUNTIME_TOP_KEYS:
                hint = "运行时数据?"
            elif RUNTIME_PATH_RE.search(rel):
                hint = "运行时数据?"
            elif SERIALIZED_PATH_RE.search(fn):
                hint = "解包残留?"
            m["hints"][hint or "数据表"] = m["hints"].get(hint or "数据表", 0) + 1
            if hint:
                continue
            for p, s in strings:
                bucket = classify_value(s)
                if bucket in (None, "path", "color", "enum"):
                    continue
                key = re.sub(r"\[\d+\]", "[]", p)
                m["rows"][bucket] = m["rows"].get(bucket, 0) + 1
                m["fields"][key] = m["fields"].get(key, 0) + 1
                if len(m["samples"].get(bucket, [])) < 3:
                    m["samples"].setdefault(bucket, []).append((rel, p, s[:90]))
            m["files"].append(rel)
    return mods, totals


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", default=None,
                    help="要扫的根目录，可重复；默认扫游戏目录与 Vortex staging")
    ap.add_argument("--json", default=None, help="把完整报告写到这个路径")
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args(argv)

    roots = args.root or [
        r"E:\SteamLibrary\steamapps\common\Cyberpunk 2077\bin\x64\plugins\cyber_engine_tweaks\mods",
        r"E:\Vortex Mods\cyberpunk2077",
    ]
    report = {}
    for root in roots:
        if not os.path.isdir(root):
            print("[skip] %s" % root)
            continue
        mods, totals = scan(root)
        report[root] = {"totals": totals, "mods": mods}
        print("=" * 78)
        print("root: %s" % root)
        print("json %d 个 / %.2f MB | locale %d | runtime %d | data %d | 解析失败 %d"
              % (totals["files"], totals["bytes"] / 1048576.0, totals["locale"],
                 totals["runtime"], totals["data"], totals["unparsable"]))
        print("-" * 78)
        rows = []
        for mod, m in mods.items():
            text_rows = m["rows"].get("sentence", 0) + m["rows"].get("phrase", 0) + m["rows"].get("label", 0)
            if not m["files"]:
                continue
            rows.append((text_rows, mod, m))
        rows.sort(reverse=True)
        print("%-40s %5s %6s %6s %6s  %s" % ("mod", "files", "sent", "phr", "label", "hint / zh"))
        for text_rows, mod, m in rows[: args.top]:
            hint = ",".join("%s=%d" % (k, v) for k, v in sorted(m["hints"].items()) if k != "数据表")
            print("%-40s %5d %6d %6d %6d  %s%s"
                  % (mod[:40], len(m["files"]), m["rows"].get("sentence", 0),
                     m["rows"].get("phrase", 0), m["rows"].get("label", 0),
                     hint or "", "  [自带中文]" if m["has_zh"] else ""))
        for text_rows, mod, m in rows[: args.top]:
            if text_rows < 20:
                continue
            fields = sorted(m["fields"].items(), key=lambda kv: -kv[1])[:8]
            print("\n  [%s] 字段分布: %s" % (mod[:60], ", ".join("%s=%d" % (k, v) for k, v in fields)))
            for bucket in ("sentence", "phrase", "label"):
                for rel, p, s in m["samples"].get(bucket, [])[:2]:
                    print("    %-8s %s :: %s = %r" % (bucket, os.path.basename(rel), p, s))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print("\nreport -> %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
