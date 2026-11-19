# -*- coding: utf-8 -*-
"""回归：对比两份引擎对同一批源文件的提取结果，逐条比。

改提取规则时最容易出的事不是「新规则没生效」，而是「顺手把旧的改坏了」。
这个脚本把两次提取的 (文件 -> [(行号, 类型, ctx, 原文)]) 全量落盘再逐条比，
新增/消失/改动分三类报出来。

用法::

    # 1. 用改动前的引擎跑一份基线（把 .bak 复制成独立目录）
    python tools/regress_extract.py --engine "%TEMP%\\dsh_baseline" --out "%TEMP%\\old.json"

    # 2. 用改动后的引擎跑一份
    python tools/regress_extract.py --engine src --out "%TEMP%\\new.json"

    # 3. 比
    python tools/regress_extract.py --diff "%TEMP%\\old.json" "%TEMP%\\new.json"

`--engine` 指向包含 mod_text_engine.py 与 text_noise.py 的目录。
"""
import argparse
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_ROOTS = [
    r"E:\SteamLibrary\steamapps\common\Cyberpunk 2077\bin\x64\plugins\cyber_engine_tweaks\mods",
    r"E:\Vortex Mods\cyberpunk2077",
]


def run(engine, roots):
    engine = os.path.abspath(engine)
    sys.path.insert(0, engine)
    import mod_text_engine as E          # noqa: E402
    got = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        data, meta = E.extract_root_ex(root, include_lua=True, loose=True,
                                       include_locale_lua=False, target_locale="")
        for rel, items in data.items():
            got[root + "::" + rel] = [list(x) for x in items]
    return got, meta


def diff(old_path, new_path, show=40):
    with open(old_path, "r", encoding="utf-8") as f:
        old = json.load(f)
    with open(new_path, "r", encoding="utf-8") as f:
        new = json.load(f)
    o, n = old["files"], new["files"]
    oset, nset = set(o), set(n)
    added_files = sorted(nset - oset)
    gone_files = sorted(oset - nset)
    changed = []
    for k in sorted(oset & nset):
        a, b = o[k], n[k]
        if a != b:
            sa = {tuple(x) for x in a}
            sb = {tuple(x) for x in b}
            changed.append((k, len(sb - sa), len(sa - sb)))
    # 非 json 的老文件如果变了，就是误伤
    harm = [(k, add, lose) for k, add, lose in changed
            if not k.lower().endswith(".json")]
    print("=" * 96)
    print("旧: %d 文件 / %d 条   新: %d 文件 / %d 条"
          % (len(o), sum(len(v) for v in o.values()),
             len(n), sum(len(v) for v in n.values())))
    print("新增文件 %d | 消失文件 %d | 内容变化 %d（其中非 json %d）"
          % (len(added_files), len(gone_files), len(changed), len(harm)))
    if harm:
        print("\n!! 非 json 文件被改动（误伤）：")
        for k, add, lose in harm[:show]:
            print("   +%-4d -%-4d  %s" % (add, lose, k[-80:]))
    if gone_files:
        print("\n消失的文件：")
        for k in gone_files[:show]:
            print("   -%-5d %s" % (len(o[k]), k[-80:]))
    if added_files:
        print("\n新增的文件：")
        for k in added_files[:show]:
            print("   +%-5d %s" % (len(n[k]), k[-80:]))
    return 1 if harm else 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=None, help="含 mod_text_engine.py 的目录")
    ap.add_argument("--out", default=None, help="把提取结果写到这个 json")
    ap.add_argument("--root", action="append", default=None)
    ap.add_argument("--diff", nargs=2, default=None, metavar=("OLD", "NEW"))
    ap.add_argument("--show", type=int, default=40)
    args = ap.parse_args(argv)

    if args.diff:
        return diff(args.diff[0], args.diff[1], show=args.show)

    if not (args.engine and args.out):
        ap.error("需要 --engine 与 --out，或 --diff OLD NEW")
    roots = args.root or DEFAULT_ROOTS
    got, meta = run(args.engine, roots)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"files": got, "meta": meta}, f, ensure_ascii=False)
    print("%d 文件 / %d 条 -> %s"
          % (len(got), sum(len(v) for v in got.values()), args.out))
    print("meta: %s" % {k: v for k, v in meta.items() if not isinstance(v, (dict, list))})
    return 0


if __name__ == "__main__":
    sys.exit(main())
