# -*- coding: utf-8 -*-
"""按 .bak 逐行还原 Settings.reds 被顶掉的 @runtimeProperty 键名。

背景
----
翻译工具第二次对同一文件应用时，兜底匹配（只按行号）会挑中同一行的第一个
字面量 —— 也就是 @runtimeProperty 的键名 —— 把它换成值的译文：

    原档：  @runtimeProperty("ModSettings.mod", "Living Streets")
    损坏：  @runtimeProperty("鲜活街道", "鲜活街道")

MCM 只认 ModSettings.* 这套固定键名，键名一坏，分类注册中途落空，设置页
只剩前两个分类。

本脚本不重新翻译，只做结构还原：两文件行数一致（605 行），逐行对齐，
把损坏行第一个参数换回 .bak 里的键名，第二个参数（中文译文）原样保留。

用法
----
    python restore_settings_keys.py            # dry-run，只报告
    python restore_settings_keys.py --apply    # 落盘（先另存 .broken 备份）
"""
import argparse
import os
import re
import sys

# Windows 控制台默认 GBK，中文路径/中文键名会乱码
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 目标文件（可用 --file 覆盖）
DEFAULT_FILE = (
    r"E:\Vortex Mods\cyberpunk2077"
    r"\NightCityLivingStreets 1.0.0 37465 1 2026-11-06T09-27Z bWY8Whjtu"
    r"\r6\scripts\NightCityLivingStreets\Settings.reds"
)

VALID_KEY = re.compile(r'^ModSettings\.[A-Za-z.]+$')
# 行首的 @runtimeProperty("第一个参数"
# 键名被译文顶掉后可能带转义引号（`...离开。\"背叛者\"...`），所以不能只写
# `[^"]*`，否则匹配会在 `\"` 处断掉、把半个键名留下。
PROP_KEY = re.compile(r'^(\s*@runtimeProperty\(\s*)"((?:[^"\\]|\\.)*)"')


def load_lines(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def main():
    ap = argparse.ArgumentParser(description="还原 @runtimeProperty 键名")
    ap.add_argument("--file", default=DEFAULT_FILE, help="要修复的 .reds")
    ap.add_argument("--bak", default=None, help="对照用的原档（默认 <file>.bak）")
    ap.add_argument("--apply", action="store_true", help="真正写盘")
    args = ap.parse_args()

    cur_path = args.file
    bak_path = args.bak or (cur_path + ".bak")
    for p in (cur_path, bak_path):
        if not os.path.isfile(p):
            print(f"[中止] 找不到文件: {p}")
            return 2

    cur = load_lines(cur_path)
    bak = load_lines(bak_path)
    cur_lines = cur.splitlines(True)
    bak_lines = bak.splitlines(True)

    if len(cur_lines) != len(bak_lines):
        print(f"[中止] 行数不一致: 当前 {len(cur_lines)} 行, 原档 {len(bak_lines)} 行。")
        print("       行数不等说明不是单纯的字符串替换, 逐行还原会错位, 不写盘。")
        return 2

    fixed = 0
    samples = []
    out = []
    for i, (cl, bl) in enumerate(zip(cur_lines, bak_lines)):
        lineno = i + 1
        cm = PROP_KEY.match(cl)
        bm = PROP_KEY.match(bl)
        if not (cm and bm):
            out.append(cl)
            continue
        cur_key, bak_key = cm.group(2), bm.group(2)
        # 只处理「原档键名合法、当前键名不合法」的行；其它一律不动。
        if VALID_KEY.match(bak_key) and not VALID_KEY.match(cur_key):
            newline = cl[:cm.start(2)] + bak_key + cl[cm.end(2):]
            out.append(newline)
            fixed += 1
            if len(samples) < 8:
                samples.append((lineno, cur_key, bak_key,
                                newline.strip()[:100]))
        else:
            out.append(cl)

    print(f"文件: {cur_path}")
    print(f"原档: {bak_path}")
    print(f"总行数: {len(cur_lines)}（一致）")
    print(f"待还原的键名: {fixed} 条")

    if samples:
        print("\n样例（行号 / 当前键名 → 原档键名）:")
        for lineno, ck, bk, line in samples:
            shown = ck if len(ck) <= 28 else ck[:28] + "…"
            print(f"  L{lineno}: {shown!r} → {bk!r}")
            print(f"        结果: {line}")

    if fixed == 0:
        print("\n没有需要还原的行，文件看起来是好的。")
        return 0

    # 还原后自检：所有键名必须合法
    result = "".join(out)
    bad_after = [k for k in PROP_KEY.findall(result) if not VALID_KEY.match(k)]
    # findall 在多分组时返回元组，这里用 finditer 复查
    bad_after = [m.group(2) for m in PROP_KEY.finditer("".join(
        l for l in out)) if not VALID_KEY.match(m.group(2))]
    print(f"\n还原后仍不合法的键名: {len(bad_after)}")
    if bad_after:
        for k in bad_after[:10]:
            print(f"  {k[:60]!r}")
        print("[中止] 有残留，不写盘。")
        return 3

    if not args.apply:
        print("\n[dry-run] 未写盘。确认无误后加 --apply 执行。")
        print(f"  将生成: {cur_path}.restored")
        return 0

    # 落盘：先备份损坏文件，再写还原结果
    broken_copy = cur_path + ".broken"
    restored_tmp = cur_path + ".restored"
    with open(broken_copy, "w", encoding="utf-8", newline="") as f:
        f.write(cur)
    with open(restored_tmp, "w", encoding="utf-8", newline="") as f:
        f.write(result)
    with open(cur_path, "w", encoding="utf-8", newline="") as f:
        f.write(result)
    print("\n[完成]")
    print(f"  损坏副本: {broken_copy}")
    print(f"  还原结果: {cur_path}")
    print(f"  对照副本: {restored_tmp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
