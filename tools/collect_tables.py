# -*- coding: utf-8 -*-
"""只读：把 mod 里「像表条目」的行按形状归类，给扩测挑样本用。

    python tools/collect_tables.py
    python tools/collect_tables.py --root "E:\\Vortex Mods\\cyberpunk2077" --min 5

输出按出现次数排序的「字段签名」，每种签名给代表文件/行号和最多 3 条样例。
不做任何判断，只负责把形状摆出来。
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOTS = [
    r"E:\SteamLibrary\steamapps\common\Cyberpunk 2077\bin\x64\plugins\cyber_engine_tweaks\mods",
    r"E:\Vortex Mods\cyberpunk2077",
]

# 一条记录: { a = 'x', b = 'y' }  —— 只抓单行、且至少 2 个键值对
ENTRY_RE = re.compile(r"\{[^{}\n]*=[^{}\n]*\}")
PAIR_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*=\s*")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", default=None)
    ap.add_argument("--min", type=int, default=4, help="签名至少出现几次才列出")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--only", default="", help="只看包含这个字段名的签名")
    args = ap.parse_args()
    roots = args.root or ROOTS

    sigs = defaultdict(lambda: {"count": 0, "where": None, "samples": []})
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if d.lower() not in (".git", "__pycache__", "node_modules")]
            for f in fn:
                if not f.lower().endswith(".lua"):
                    continue
                p = os.path.join(dp, f)
                rel = os.path.relpath(p, root)
                try:
                    lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
                except Exception:
                    continue
                for i, line in enumerate(lines, 1):
                    s = line.strip()
                    if "=" not in s or s.startswith("--"):
                        continue
                    m = ENTRY_RE.search(s)
                    if not m:
                        continue
                    body = m.group(0)
                    keys = tuple(sorted(set(k.lower() for k in PAIR_RE.findall(body))))
                    if len(keys) < 2:
                        continue
                    if args.only and args.only.lower() not in keys:
                        continue
                    e = sigs[keys]
                    e["count"] += 1
                    if e["where"] is None:
                        e["where"] = "%s:%d" % (rel, i)
                    if len(e["samples"]) < 3:
                        e["samples"].append(s[:150])

    rows = [(c["count"], k, c) for k, c in sigs.items() if c["count"] >= args.min]
    rows.sort(reverse=True, key=lambda r: r[0])
    print("signatures >= %d: %d\n" % (args.min, len(rows)))
    for count, keys, c in rows[: args.top]:
        print("[%4d] %s" % (count, ",".join(keys)))
        print("       at %s" % c["where"])
        for s in c["samples"]:
            print("       %s" % s)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
