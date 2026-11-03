# -*- coding: utf-8 -*-
r"""全盘扫 CET 模组 Lua 里的 string.format, 找「写进去必崩」的那种。

为什么需要它: 批量翻译会翻 .lua 里的字面量, 而 `string.format` 是**按位置**
传参的。中文语序常常想把「谁」放前面、「多少」放后面, 模型就顺手把
`"Applied %d change(s) for %s."` 写成 `"已为 %s 应用 %d 项更改。"` —— 占位符
个数一个不少, 字数校验看不出问题, 运行时却拿到 (数字, 名字) 去匹配 (%s, %d),
第 3 个参数要 %d 却给字符串, 直接
`bad argument #3 to 'format' (number expected, got string)`,
整个 MCM 设置面板的操作都报错 (实测 `ModConfigurationMenuAPI\modules\core.lua:1344`)。

写回前的顺序检查 (`mod_text_engine.swapped_spec_order()`) 已经挡住新的; 这个
脚本是**事后体检**: 扫已经部署进游戏的 .lua, 把这类调用全找出来。

只报「必崩」的:
  * 占位符个数 != 实参个数;
  * %d / %f / %x 这类要数字的位置, 实参能确定是字符串或表
    (`#x`、字面量、`tostring(…)`、`x.name` 这类按约定是字符串的字段、`{…}`)。
拿不准的一律不报, 免得满屏误报。

用法:
    python tools/scan_lua_format.py                # 用 settings.json 里的游戏目录
    python tools/scan_lua_format.py <目录>          # 指定要扫的根
报告写到 userdata\\reports\\lua-format-<时间>.txt。
"""
import io
import json
import os
import re
import sys
import time

NUM_SPEC = set("diouxXfFeEgG")
ALL_SPEC = set("diouxXfFeEgGqscaA%")

SPEC_RE = re.compile(r"%(?:\d+\$)?[-+ 0#]*\d*(?:\.\d+)?([a-zA-Z%])")
STR_FIELDS = ("name", "title", "label", "key", "id", "path", "provider",
              "description", "text", "message", "version", "code", "dir",
              "file", "unit", "suffix", "prefix", "type", "kind", "reason",
              "collectionname", "providername", "modname", "displayname",
              "url", "icon", "comment", "note", "hint", "tooltip")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def parse_literal(src, i):
    """从 src[i] 开始解析一个 Lua 字符串字面量, 返回 (字面量内容, 结束位置)。"""
    q = src[i]
    assert q in "\"'"
    i += 1
    out = []
    while i < len(src):
        c = src[i]
        if c == "\\":
            nxt = src[i + 1:i + 2]
            mapping = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\",
                       '"': '"', "'": "'", "a": "\a", "b": "\b",
                       "f": "\f", "v": "\v"}
            if nxt in mapping:
                out.append(mapping[nxt])
                i += 2
                continue
            out.append(c + nxt)
            i += 2
            continue
        if c == q:
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise ValueError("unterminated literal")


def find_calls(src, needle="string.format"):
    """产出 (位置, 第一个实参起始下标, 右括号下标)。跳过注释与字符串内部。"""
    i = 0
    n = len(src)
    while True:
        j = src.find(needle, i)
        if j < 0:
            return
        i = j + len(needle)
        line_start = src.rfind("\n", 0, j) + 1
        if "--" in src[line_start:j]:
            continue
        k = i
        while k < n and src[k] in " \t\r\n":
            k += 1
        if k >= n or src[k] != "(":
            continue
        depth = 0
        p = k
        while p < n:
            c = src[p]
            if c in "\"'":
                try:
                    _, p = parse_literal(src, p)
                except Exception:
                    break
                continue
            if c == "-" and src[p:p + 2] == "--":
                nl = src.find("\n", p)
                p = n if nl < 0 else nl + 1
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    yield j, k + 1, p
                    i = p + 1
                    break
            p += 1
        else:
            return


def split_args(src, start, end):
    """把 (start, end) 之间的实参按顶层逗号切开。"""
    out = []
    depth = 0
    p = start
    cur = start
    while p < end:
        c = src[p]
        if c in "\"'":
            try:
                _, p = parse_literal(src, p)
            except Exception:
                break
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(src[cur:p])
            cur = p + 1
        p += 1
    out.append(src[cur:end])
    return [a.strip() for a in out if a.strip()]


def arg_type(expr):
    e = expr.strip()
    if not e:
        return "?"
    if "..." in e or "unpack(" in e:
        return "?"
    if e.startswith("#"):
        return "num"
    if e.startswith("tostring(") or e.startswith("tostring ("):
        return "str"
    if re.fullmatch(r"-?\d+(?:\.\d+)?", e):
        return "num"
    if e[0] in "\"'":
        return "str"
    if e.startswith("tonumber(") or e.startswith("math."):
        return "num"
    tail = e.rsplit(".", 1)[-1] if "." in e else ""
    if tail.lower() in STR_FIELDS and re.fullmatch(r"[\w.\[\]\"']+", e):
        return "str"
    if e.startswith("{"):
        return "table"
    if ".." in e:
        return "str"
    return "?"


def scan_file(path):
    try:
        src = io.open(path, encoding="utf-8").read()
    except Exception:
        return []
    if "string.format" not in src:
        return []
    hits = []
    for pos, astart, aend in find_calls(src):
        body = src[astart:aend]
        if not body or body[0] not in "\"'":
            continue
        try:
            lit, litend = parse_literal(src, astart)
        except Exception:
            continue
        # 格式串是拼出来的: string.format("%." .. n .. "f", v) —— 静态看不出来
        j = litend
        while j < len(src) and src[j] in " \t":
            j += 1
        if src[j:j + 2] == "..":
            continue
        args = split_args(src, litend, aend)
        specs = [m.group(1) for m in SPEC_RE.finditer(lit) if m.group(1) != "%"]
        if any(c not in ALL_SPEC for c in specs):
            continue
        line = src.count("\n", 0, pos) + 1
        if len(specs) != len(args):
            hits.append((path, line, lit, args,
                         "占位符 %d 个, 实参 %d 个" % (len(specs), len(args))))
            continue
        for idx, (sp, arg) in enumerate(zip(specs, args), 1):
            t = arg_type(arg)
            if sp in NUM_SPEC and t in ("str", "table"):
                hits.append((path, line, lit, args,
                             "第 %d 个实参要 %%%s (数字), 给的是%s: %s"
                             % (idx, sp, "表" if t == "table" else "字符串",
                                arg.strip()[:60])))
                break
    return hits


def default_root():
    """settings.json 里的游戏目录 + CET 模组路径。"""
    for rel in ("userdata", os.path.join("dist", "userdata")):
        p = os.path.join(ROOT, rel, "settings.json")
        if not os.path.isfile(p):
            continue
        try:
            game = json.load(io.open(p, encoding="utf-8-sig")).get("game_dir") or ""
        except Exception:
            continue
        if not game:
            continue
        root = os.path.join(game, "bin", "x64", "plugins",
                            "cyber_engine_tweaks", "mods")
        if os.path.isdir(root):
            return root
    return ""


def main(argv):
    root = argv[1] if len(argv) > 1 else default_root()
    if not root or not os.path.isdir(root):
        print("要扫的目录不存在: %r" % root)
        print("用法: python tools/scan_lua_format.py <CET 模组目录>")
        return 2
    hits = []
    files = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.lower().endswith(".lua"):
                files += 1
                hits.extend(scan_file(os.path.join(dirpath, fn)))
    print("扫了 %d 个 .lua, 查出 %d 处必崩的 string.format" % (files, len(hits)))
    for path, line, lit, args, why in hits:
        print("\n%s:%d" % (path, line))
        print("  格式串: " + lit.replace("\n", "\\n")[:160])
        print("  实参  : " + " | ".join(a[:50] for a in args))
        print("  原因  : " + why)
    out_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "userdata", "reports")
    if not os.path.isdir(os.path.join(ROOT, "userdata")):
        out_dir = os.path.join(ROOT, "dist", "userdata", "reports")
    try:
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, time.strftime("lua-format-%Y%m%d-%H%M%S.txt"))
        lines = ["根目录: " + root, "扫了 %d 个 .lua, 查出 %d 处" % (files, len(hits))]
        for path, line, lit, args, why in hits:
            lines += ["", "%s:%d" % (path, line),
                      "  格式串: " + lit.replace("\n", "\\n")[:200],
                      "  实参  : " + " | ".join(a[:60] for a in args),
                      "  原因  : " + why]
        io.open(dst, "w", encoding="utf-8").write("\n".join(lines))
        print("\n报告: " + dst)
    except Exception as exc:
        print("\n报告没写成: %s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
