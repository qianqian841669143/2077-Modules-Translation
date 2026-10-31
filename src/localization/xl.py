# -*- coding: utf-8 -*-
"""xl.py —— ArchiveXL 的 .xl 配置（只处理 localization 段）。

实测格式（docs/M0验证.md）：

    localization:
      extend: TargetMod.archive.xl          # 可选：挂到别的 .xl 上
      onscreens:
        zh-cn:
          - path\\to\\file.json
      subtitles:
        zh-cn:
          - path\\to\\subtitles_map.json

两种语言写法都要吃：列表（官方文档 & 真实 mod 主流）与标量（文档另一处出现过）：

      onscreens:
        es-es: localization\\es-es\\RaitoLoc.json     ← 标量也行

三条硬规则：
  1. 路径**相对归档根目录**，不是相对 mod 目录。
  2. 必须用反斜杠；用正斜杠 ArchiveXL 找不到资源。
  3. 缩进用空格，层级写错整段失效。
"""
from __future__ import annotations

import re
from pathlib import Path

# ArchiveXL 认的 localization 子节点
NODES = ("onscreens", "subtitles", "lipmaps", "vomaps")

# 语言键：内部用 zh-cn 这种形式，与 ArchiveXL 的文件夹命名一致
LANG_ZH_CN = "zh-cn"
LANG_ZH_TW = "zh-tw"
LANG_EN_US = "en-us"

_TOP_RE = re.compile(r"^([A-Za-z_][\w]*)\s*:\s*(.*)$")
_ITEM_RE = re.compile(r"^-\s*(.+?)\s*$")


class XlError(RuntimeError):
    pass


class L10nConfig(object):
    """localization 段的模型。

    extend  目标 mod 的 .xl 文件名（不含路径），空表示不挂接
    nodes   {node_name: {lang: [rel_path, ...]}}
    """

    def __init__(self):
        self.extend = ""
        self.nodes = {n: {} for n in NODES}
        # 非 localization 的顶层段，原样保留，保证回写不丢配置
        self.other = []

    # -- 便捷访问 ---------------------------------------------------------

    def add(self, node, lang, rel_path):
        if node not in self.nodes:
            raise XlError("未知的 localization 节点：%s" % node)
        paths = self.nodes[node].setdefault(lang, [])
        rel = norm_rel(rel_path)
        if rel not in paths:
            paths.append(rel)
        return rel

    def get(self, node, lang):
        return list(self.nodes.get(node, {}).get(lang, []))

    def langs(self, node):
        return sorted(self.nodes.get(node, {}))

    def is_empty(self):
        return not any(self.nodes[n] for n in NODES)

    def resource_count(self):
        return sum(len(v) for n in NODES for v in self.nodes[n].values())

    def all_paths(self):
        seen = []
        for n in NODES:
            for lang in self.nodes[n]:
                for p in self.nodes[n][lang]:
                    if p not in seen:
                        seen.append(p)
        return seen

    def __repr__(self):
        return "<L10nConfig extend=%r %s>" % (
            self.extend, {n: self.nodes[n] for n in NODES if self.nodes[n]})


def norm_rel(path):
    """统一成归档内相对路径：反斜杠、去掉开头的斜杠与盘符。"""
    p = str(path or "").strip().strip('"').strip("'")
    p = p.replace("/", "\\")
    while p.startswith("\\"):
        p = p[1:]
    if re.match(r"^[A-Za-z]:", p):
        raise XlError("路径必须是相对归档根的相对路径，不能是绝对路径：%s" % path)
    if not p:
        raise XlError("资源路径为空")
    return p


def render(cfg) -> str:
    """生成 .xl 文本。缩进固定 2 空格，列表项固定 `- ` 前缀。"""
    lines = []
    for raw in cfg.other:
        lines.append(raw)
    lines.append("localization:")
    if cfg.extend:
        lines.append("  extend: %s" % cfg.extend)
    for node in NODES:
        table = cfg.nodes.get(node) or {}
        if not table:
            continue
        lines.append("  %s:" % node)
        for lang in sorted(table):
            paths = table[lang]
            if not paths:
                continue
            lines.append("    %s:" % lang)
            for p in paths:
                lines.append("      - %s" % p)
    return "\n".join(lines) + "\n"


def parse(text) -> L10nConfig:
    """解析 .xl 文本。

    只认 localization 段；其余顶层段落原样收进 other。
    容错优先：看不懂的行跳过而不报错——用户的 .xl 是别人写的，不能因为
    多了一个我们没实现的字段就整体失败。
    """
    cfg = L10nConfig()
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    in_l10n = False
    node = None       # 当前子节点名
    lang = None       # 当前语言
    node_indent = -1
    lang_indent = -1

    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        body = raw.strip()

        if indent == 0:
            in_l10n = False
            node = lang = None
            m = _TOP_RE.match(body)
            if m and m.group(1) == "localization":
                in_l10n = True
            else:
                cfg.other.append(raw)
            continue

        if not in_l10n:
            continue

        # localization 段内部
        m = _ITEM_RE.match(body)
        if m and lang is not None:
            try:
                cfg.add(node, lang, m.group(1))
            except XlError:
                pass
            continue

        if ":" not in body:
            continue
        key, _, rest = body.partition(":")
        key = key.strip()
        rest = rest.strip()

        if indent <= 2:
            # localization 的直接子节点
            lang = None
            if key == "extend":
                cfg.extend = rest.strip('"').strip("'")
                node = None
            elif key in NODES:
                node = key
                node_indent = indent
                cfg.nodes.setdefault(node, {})
            else:
                node = None
            continue

        # 语言层
        if node is None:
            continue
        lang = key
        lang_indent = indent
        if rest:
            # 标量写法：lang: path
            try:
                cfg.add(node, lang, rest)
            except XlError:
                pass
            lang = None
    return cfg


def load(path) -> L10nConfig:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8-sig")  # 容忍 BOM
    except OSError as ex:
        raise XlError("读不到 .xl：%s (%s)" % (p.name, ex))
    return parse(text)


def save(path, cfg) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = render(cfg)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return p


def looks_like_xl(path) -> bool:
    name = Path(path).name.lower()
    return name.endswith(".xl") or name.endswith(".archive.xl")


def default_name(mod_name) -> str:
    """标准命名：<mod 名>.archive.xl。ArchiveXL 要求 .xl 与归档同名才自动加载。"""
    base = str(mod_name or "").strip()
    if base.lower().endswith(".archive.xl"):
        return base
    for suffix in (".archive", ".xl"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
    return "%s.archive.xl" % base


def main(argv=None):
    """诊断用法：python -m localization.xl <file.xl>"""
    import argparse

    ap = argparse.ArgumentParser(description="解析 / 重新输出 .xl 的 localization 段")
    ap.add_argument("path")
    ap.add_argument("--rewrite", action="store_true", help="把解析结果重新打印为标准格式")
    args = ap.parse_args(argv)

    cfg = load(args.path)
    print("extend:", cfg.extend or "(无)")
    for node in NODES:
        table = cfg.nodes.get(node) or {}
        if not table:
            continue
        print("%s:" % node)
        for lang in sorted(table):
            for p in table[lang]:
                print("   %-8s %s" % (lang, p))
    if cfg.other:
        print("其他顶层段:", len(cfg.other), "行（保留）")
    if args.rewrite:
        print("\n--- 规范化输出 ---")
        print(render(cfg), end="")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
