# -*- coding: utf-8 -*-
"""只读统计：mod 里数据类 .json 的字段名 + 值形状 + 兄弟字段。

回答的问题是「字段名单该定成什么」，而不是「翻得对不对」：

1. 同一个字段名，在不同结构里含义不同 —— ``name`` 在 ``{group=..., name=...}``
   里是设置键，在 ``{name=..., description=...}`` 里是显示名。
   所以统计必须带「兄弟字段」，只看字段名会一刀切错。
2. 值形状（sentence/phrase/label/enum/path）决定要不要兜底收。
3. 输出按「(字段名, 兄弟字段集合)」聚合，正是写规则时要用的判据。

用法::

    python tools/collect_json_fields.py
    python tools/collect_json_fields.py --json out.json
    python tools/collect_json_fields.py --root "E:\\Vortex Mods\\cyberpunk2077"

**只读**，不写任何文件（--json 除外）。
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 和 scan_json_text.py 共用同一套路径判据，避免两处标准漂移。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan_json_text import classify_name, classify_value, RUNTIME_TOP_KEYS, RUNTIME_PATH_RE

LANG_DIRS = {"languages", "language", "locale", "locales", "localization", "i18n", "lang"}

# 值是「看起来像文案」的四档
TEXT_BUCKETS = ("sentence", "phrase", "label")


def walk(obj, ptr, parent_key, sib, out):
    """收集 (指针, 字段名, 值, 兄弟字段集合)。"""
    if isinstance(obj, dict):
        keys = [str(k) for k in obj.keys()]
        for k, v in obj.items():
            k = str(k)
            # 兄弟字段 = 同一条记录里除自己以外的键（列表/字典都算）
            bros = frozenset(x for x in keys if x != k)
            p = ptr + "." + k if ptr else k
            if isinstance(v, str):
                out.append((p, k, v, bros, parent_key))
            else:
                walk(v, p, k, bros, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = "%s[%d]" % (ptr, i)
            if isinstance(v, str):
                out.append((p, parent_key, v, sib, parent_key))
            else:
                walk(v, p, parent_key, sib, out)


def scan(root):
    # stats[字段名][兄弟签名] = {"n": 条数, "buckets": {档: 数}, "samples": [...]}
    stats = defaultdict(lambda: defaultdict(
        lambda: {"n": 0, "buckets": defaultdict(int), "samples": []}))
    files = 0
    skipped = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in (".git", "node_modules", "__pycache__")]
        for fn in filenames:
            if not fn.lower().endswith(".json"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            if classify_name(rel) != "data":
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    obj = json.loads(f.read())
            except Exception:
                skipped += 1
                continue
            recs = []
            walk(obj, "", "", frozenset(), recs)
            tops = set(p.split(".")[0].split("[")[0] for p, _k, _v, _b, _pk in recs)
            if tops & RUNTIME_TOP_KEYS or RUNTIME_PATH_RE.search(rel):
                continue
            files += 1
            for _p, key, val, bros, _pk in recs:
                bucket = classify_value(val)
                if bucket is None:
                    continue
                sig = ",".join(sorted(bros))
                cell = stats[key][sig]
                cell["n"] += 1
                cell["buckets"][bucket] += 1
                if len(cell["samples"]) < 2 and bucket in TEXT_BUCKETS:
                    cell["samples"].append("%s :: %s = %r"
                                           % (os.path.basename(rel), _p, val[:70]))
    return stats, files, skipped


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--top", type=int, default=60, help="每档展示多少个字段名")
    ap.add_argument("--min", type=int, default=1, help="少于这么多条的字段不展示")
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
        stats, files, skipped = scan(root)
        report[root] = {"files": files, "skipped": skipped}
        # 字段名总览：按「是否以文案形状为主」分两堆
        rows = []
        for key, sigs in stats.items():
            n = sum(c["n"] for c in sigs.values())
            text_n = sum(c["buckets"].get(b, 0) for c in sigs.values() for b in TEXT_BUCKETS)
            rows.append((n, text_n, key, sigs))
        rows.sort(reverse=True)
        print("=" * 100)
        print("root: %s | 数据类 json %d 个 (解析失败 %d)" % (root, files, skipped))
        print("=" * 100)
        print("%-24s %6s %6s  %s" % ("字段名", "总数", "文案形", "主要兄弟字段(按条目数)"))
        for n, text_n, key, sigs in rows[: args.top]:
            if n < args.min:
                continue
            topsigs = sorted(sigs.items(), key=lambda kv: -kv[1]["n"])[:2]
            desc = " | ".join("%s=%d" % (s[:44] or "<无>", c["n"])
                              for s, c in topsigs)
            print("%-24s %6d %6d  %s" % (key[:24], n, text_n, desc))
        # 详情：文案形状占主导的字段，逐 (字段, 兄弟签名) 列出样例
        print("\n" + "-" * 100)
        print("文案形状占比 >= 50%% 的 (字段, 兄弟字段) 组合")
        print("-" * 100)
        detail = []
        for key, sigs in stats.items():
            for sig, c in sigs.items():
                tot = c["n"]
                tex = sum(c["buckets"].get(b, 0) for b in TEXT_BUCKETS)
                if tot >= 2 and tex * 2 >= tot:
                    detail.append((tex, tot, key, sig, c))
        detail.sort(reverse=True)
        for tex, tot, key, sig, c in detail[: args.top]:
            print("\n[%s] 兄弟=%s  %d/%d 条像文案  档=%s"
                  % (key, sig[:70] or "<无>", tex, tot,
                     ", ".join("%s=%d" % kv for kv in sorted(c["buckets"].items()))))
            for s in c["samples"]:
                print("    %r" % s)
        report[root]["stats"] = {
            k: {s: {"n": c["n"], "buckets": dict(c["buckets"]), "samples": c["samples"]}
                for s, c in sigs.items()}
            for k, sigs in stats.items()}
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print("\nreport -> %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
