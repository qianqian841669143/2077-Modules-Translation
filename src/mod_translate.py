#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CP2077 模组翻译工具 (REDscript .reds)
extract  扫描模组 -> 生成翻译表 xlsx
import   xlsx 译文回填 -> 重写 .reds
"""
import argparse, re, sys
from pathlib import Path
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

STR = r'([^"\\]|\\.)*'
SETTINGS_KEYS = {"mod": "模块名", "category": "分类",
                 "displayName": "显示名称", "description": "描述"}
HEADER = ["文件", "行号", "类型", "上下文", "原文", "译文"]

def iter_mod_files(root):
    root = Path(root)
    if root.is_file():
        yield root
        return
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".reds", ".xml"):
            yield p

def is_translatable(s):
    if len(s) < 2:
        return False
    if re.search(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', s):
        return True
    words = s.split()
    return len(words) >= 2 and all(w[0].isalpha() for w in words if w)

def extract(path):
    text = path.read_text(encoding="utf-8")
    entries = []
    for i, ln in enumerate(text.splitlines(), 1):
        m = re.search(r'@runtimeProperty\(\s*"([^"]+)"\s*,\s*"(' + STR + r')"', ln)
        if m:
            key = m.group(1).split(".")[-1]
            if key in SETTINGS_KEYS:
                entries.append((i, "设置-" + SETTINGS_KEYS[key], key, m.group(2)))
                continue
        for m in re.finditer(r'"(' + STR + r')"', ln):
            s = m.group(1)
            pre = ln[max(0, m.start() - 1):m.start() + 1]
            if ln[max(0, m.start()-2):m.start()] in ('n', 't'):
                continue
            if 'runtimeProperty' in ln:
                continue
            if is_translatable(s):
                ctx = re.sub(r'\s+', ' ', ln[:m.start()].rstrip())[-40:]
                entries.append((i, "文本", ctx, s))
    seen, out = set(), []
    for e in entries:
        if (e[2], e[3]) not in seen:
            seen.add((e[2], e[3]))
            out.append(e)
    return out

def export_xlsx(groups, out):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "翻译表"
    fill = PatternFill("solid", fgColor="1F4E79")
    for c, h in enumerate(HEADER, 1):
        cell = ws.cell(1, c, h)
        cell.fill, cell.font = fill, Font(color="FFFFFF", bold=True)
    row = 2
    total = 0
    for f, entries in groups.items():
        for (i, kind, ctx, src) in entries:
            ws.cell(row, 1, f); ws.cell(row, 2, i)
            ws.cell(row, 3, kind); ws.cell(row, 4, ctx)
            ws.cell(row, 5, src); ws.cell(row, 6, "")
            row += 1; total += 1
    for c, w in enumerate([40, 8, 16, 32, 60, 60], 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(c)].width = w
    for r in range(2, row):
        for c in (4, 5, 6):
            ws.cell(r, c).alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    wb.save(out)
    return total

def import_xlsx(xlsx):
    wb = openpyxl.load_workbook(xlsx, read_only=True)
    ws = wb.active
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    idx = {h: n for n, h in enumerate(header)}
    data = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r[idx["译文"]] not in (None, ""):
            data[(r[idx["文件"]], r[idx["行号"]], r[idx["原文"]])] = str(r[idx["译文"]])
    return data

def apply_translations(root, data):
    done, found = 0, 0
    for path in iter_mod_files(root):
        if path.suffix.lower() != ".reds":
            continue
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        touched = False
        for (i, kind, ctx, src) in extract(path):
            if (str(path), i, src) not in data:
                continue
            found += 1
            tgt = data[(str(path), i, src)]
            m = re.search(r'"' + re.escape(src) + r'"', lines[i - 1])
            if m:
                lines[i - 1] = lines[i - 1][:m.start()] + '"' + tgt + '"' + lines[i - 1][m.end():]
                touched = True
                done += 1
            else:
                print(f"  [!] line {i}: 未找到 {src!r}")
        if touched:
            path.write_text("".join(lines), encoding="utf-8")
    return done, found

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="CP2077 模组翻译工具 (.reds)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("extract", help="生成翻译表 xlsx")
    p1.add_argument("root")
    p1.add_argument("-o", "--output")
    p2 = sub.add_parser("import", help="译文回填 .reds")
    p2.add_argument("root")
    p2.add_argument("-t", "--table", required=True)
    a = ap.parse_args()
    if a.cmd == "extract":
        groups = {}
        for f in iter_mod_files(Path(a.root)):
            es = extract(f)
            if es:
                groups[str(f)] = es
                print(f"  {f.name}: {len(es)} 段")
        if not groups:
            print("未找到可翻译文本。")
            return
        out = Path(a.output) if a.output else Path(a.root) / "翻译表.xlsx"
        print(f"\n共 {export_xlsx(groups, out)} 段待翻译 -> {out}")
        print("在 Excel [译文] 列填写后执行: import ... -t 该xlsx")
    else:
        done, found = apply_translations(Path(a.root), import_xlsx(Path(a.table)))
        print(f"已回填 {done}/{found} 条（Excel 共 {len(import_xlsx(Path(a.table)))} 条译文）。")

if __name__ == "__main__":
    main()
