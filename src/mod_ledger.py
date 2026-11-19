# -*- coding: utf-8 -*-
"""mod_ledger.py —— 把译文写成「按模组切分」的账本。

这个工具不再回写模组目录：翻译结果只落到这里，由游戏侧的运行时 mod
（zzz_CP2077_Localization）读账本、在显示层替换文本。

    <输出目录>/
      index.json                    # 索引：模组 / 文件 / sourceId / 条目数
      mods/<文件夹名>.lua           # 一个模组一份账本

账本里的 `o`（原文）/ `z`（译文）成对保存：运行时必须是「拿原文查表」，
只存译文在游戏里对不上任何东西。

文件名用「部署到游戏目录时的文件夹名」而不是 Vortex 下载目录名——
后者带版本号和时间戳（`Living Streets 1.0.0 37465 1 2026-11-06T09-27Z bWY8`），
在游戏里根本不存在这个名字。
"""
from __future__ import annotations

import json
import os
import re
import time

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')

# MCM 的 sourceId 唯一来源：@runtimeProperty("ModSettings.mod", "XXX")
_MOD_RE = re.compile(
    r'@runtimeProperty\(\s*"ModSettings\.mod"\s*,\s*"((?:[^"\\]|\\.)*)"\s*\)')

_SCAN_SKIP = {".git", "__pycache__", "node_modules", ".svn"}

# 从 mod 内部路径推断「部署到游戏目录时的文件夹名」。按优先级匹配：
#   ① r6\scripts\X\…            redscript 模组
#   ② bin\…\cyber_engine_tweaks\mods\X\…   CET 模组
#   ③ red4ext\plugins\X\…       red4ext 插件
#   ④ bin\x64\plugins\X\…       其它插件
_PATTERNS = (
    re.compile(r"^r6[\\/]scripts[\\/]([^\\/]+)", re.I),
    re.compile(r"^bin[\\/]x64[\\/]plugins[\\/]cyber_engine_tweaks[\\/]"
               r"mods[\\/]([^\\/]+)", re.I),
    re.compile(r"^red4ext[\\/]plugins[\\/]([^\\/]+)", re.I),
    re.compile(r"^bin[\\/]x64[\\/]plugins[\\/]([^\\/]+)", re.I),
)

# 账本默认落点的兜底（拿不到 appconfig 时用）。
DEFAULT_LEDGER = (r"E:\Vortex Mods\cyberpunk2077\CP2077 Localization Pack"
                  r"\bin\x64\plugins\cyber_engine_tweaks\mods"
                  r"\zzz_CP2077_Localization\ledger")

_TEXT_EXT = (".reds", ".lua")


def ledger_dir(settings=None):
    """账本输出目录：设置里能覆盖，默认在工具自己的数据目录下。

    2026-11-20 改：不再往 Vortex 的 staging 里写。staging 是 Vortex 的地盘，
    而真实流程是「工具先产 zip → 用户才装」——窗口期里写进去的账本，Vortex
    不知道有这些文件（不会部署），装包那一刻又会被 zip 内容覆盖；包一改名，
    目录整个换掉，账本就地失联。工作目录改到 ``userdata\\pack\\ledger``，
    打包时按固定路径映射进 zip，路径只由 zip 决定。
    """
    if isinstance(settings, dict):
        p = str(settings.get("ledger_dir") or "").strip()
        if p:
            return p
    try:
        import appconfig
        return os.path.join(appconfig.config_dir(), "pack", "ledger")
    except Exception:            # 单独跑 mod_ledger 的脚本/自测
        return DEFAULT_LEDGER


def safe_name(text, fallback="mod"):
    """把名字压成能当文件名的字符串（保留中文和空格）。"""
    t = _BAD.sub("_", str(text or "")).strip(" .")
    return t[:120] or fallback


def _strip_comments(text):
    """去掉 Redscript 的 // 行注释和 /* */ 块注释，字符串里的内容原样保留。

    Mod Settings 自己的 packed.reds 里有一整段被注释掉的示例
    （`// @runtimeProperty("ModSettings.mod", "Test mod")` 之类共 48 条），
    直接拿全文跑正则会把作者编的占位名当成真模组。
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            if j < 0:
                j = n
            out.append(" " * (j - i))
            i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in text[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def scan_ids(root):
    """扫一个模组目录（或整个 staging）里的 MCM sourceId，去重保序。"""
    out = []
    seen = set()
    if not root or not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SCAN_SKIP]
        for fn in filenames:
            if not fn.lower().endswith(".reds"):
                continue
            try:
                with open(os.path.join(dirpath, fn), "r",
                          encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            if "ModSettings.mod" not in text:
                continue
            text = _strip_comments(text)
            for m in _MOD_RE.finditer(text):
                v = re.sub(r"\s+", " ", _unesc(m.group(1))).strip()
                if v and v not in seen:
                    seen.add(v)
                    out.append(v)
    return out


def folder_name(root, limit=20000):
    """推断这个模组部署到游戏目录时的文件夹名。

    一个模组可能同时装到多处（`r6\\scripts\\X` + `bin\\…\\mods\\Y`），
    按「含文本文件最多的那个」为准；一个都推不出来就返回空串。
    """
    if not root or not os.path.isdir(root):
        return ""
    votes = {}
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in (".git", "__pycache__")]
        for fn in filenames:
            if not fn.lower().endswith(_TEXT_EXT):
                continue
            n += 1
            if n > limit:
                break
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            rel = rel.replace("\\", "/")
            for rx in _PATTERNS:
                m = rx.match(rel)
                if m:
                    votes[m.group(1)] = votes.get(m.group(1), 0) + 1
                    break
        if n > limit:
            break
    if not votes:
        return ""
    return max(votes.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _q(s):
    """Lua 单引号字符串。换行压成空格——单引号字符串不能跨行。"""
    t = str(s or "").replace("\r", " ").replace("\n", " ")
    return "'" + t.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _unesc(s):
    """Lua 单引号字符串的最小反转义（读回自己写出去的内容用）。"""
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            out.append(s[i + 1])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _rel(path, root):
    try:
        p = os.path.relpath(str(path or ""), root)
    except Exception:
        p = str(path or "")
    return p.replace("\\", "/")


def _entries(rows, root):
    """把 rows 转成账本条目，去掉没译文、译文=原文、重复的。"""
    out = []
    seen = set()
    for x in rows or []:
        orig = str(x.get("orig") or "").strip()
        trans = str(x.get("trans") or "").strip()
        if not orig or not trans or trans == orig:
            continue
        key = (orig, trans)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "t": str(x.get("type") or ""),
            "o": orig,
            "z": trans,
            "f": "%s:%s" % (_rel(x.get("file"), root), x.get("line") or ""),
        })
    return out


def _source_ids(mod_root):
    """账本里带上 MCM 的 sourceId，运行时 mod 用它把条目对到 provider。"""
    try:
        return scan_ids(mod_root)
    except Exception:
        return []


# ---------------------------------------------------------------- 模组名表
#
# 这里原来是一份独立的 labels.lua（zzz_mcm_label_override 读它）。现在合并
# 进账本：names.lua 只放 sourceId -> 中文显示名，人工整理的优先，工具只补缺的。

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_NAMES_ENTRY = re.compile(
    r"^\s*\['((?:[^'\\]|\\.)*)'\]\s*=\s*'((?:[^'\\]|\\.)*)'\s*,?\s*$")

_NAME_SYS = (
    "你在为 Cyberpunk 2077 的模组管理器（MCM）制作中文显示名。"
    "输入是一个 JSON 字符串数组，每个元素是一个模组的英文名称。"
    "要求：翻成简短自然的中文名，不超过 12 个汉字；"
    "开头是厂商/系列缩写前缀的（如 ST / CVL / GR / CNC-nttn）保留前缀不翻；"
    "不要加句号、引号、括号或任何解释。"
    "只输出一个 JSON 字符串数组，顺序和长度与输入完全一致。"
)


def parse_names(path):
    """读 names.lua，返回 {sourceId: 中文名}。"""
    out = {}
    if not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = _NAMES_ENTRY.match(line.rstrip("\r\n"))
                if m:
                    out[_unesc(m.group(1))] = _unesc(m.group(2))
    except OSError:
        return out
    return out


def write_names(path, mapping):
    """写 names.lua（按 id 排序），人工已有的名字原样保留。"""
    rows = sorted(mapping.items(), key=lambda kv: kv[0].lower())
    width = max([len("  ['%s'] = " % _q(k)) for k, _ in rows] + [20]) + 1
    lines = [
        "-- 模组显示名表（sourceId -> 中文名）。工具只补缺的，人工改过的不会被覆盖。",
        "-- 由 2077 Mod Translator 生成（%s）" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "return {",
    ]
    for k, v in rows:
        head = "  ['%s'] = " % _q(k)[1:-1]
        pad = " " * max(1, width - len(head) - 2)
        lines.append("%s'%s'," % (head + pad, _q(v)[1:-1]))
    lines += ["}", ""]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    os.replace(tmp, path)


def _clean_name(name):
    s = str(name or "").strip().strip("\"'").strip()
    s = re.sub(r"\s+", " ", s).rstrip("。.").strip()
    return s if s and _CJK_RE.search(s) else ""


def translate_names(ids, base, model, target="简体中文", timeout=180, log=None):
    """只翻模组名。返回 {id: 中文名}，没翻出来的不放进结果。"""
    import json as _json
    import mt_client as MT

    ids = [str(x).strip() for x in (ids or []) if str(x).strip()]
    if not ids or not base:
        return {}
    sys_prompt = _NAME_SYS.replace("简体中文", target, 1)
    try:
        raw = MT.chat(
            base, model,
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": _json.dumps(ids, ensure_ascii=False)}],
            temperature=0.2, timeout=timeout,
            max_tokens=max(256, 80 * len(ids)))
    except Exception as ex:                       # noqa: BLE001
        if log:
            log("names: 翻模组名失败 %s" % ex)
        return {}
    got = MT.parse_json_array(raw, len(ids))
    if got is None:
        try:
            got = MT._parse_lines(raw, len(ids))   # noqa: SLF001 - 同包内部兜底
        except Exception:                          # noqa: BLE001
            got = None
    if not got:
        return {}
    out = {}
    for i, name in zip(ids, got):
        lab = _clean_name(name)
        if lab:
            out[i] = lab
    return out


def ensure_names(out_dir, ids, base="", model="", target="简体中文",
                 timeout=180, log=None):
    """补 names.lua：已有中文的不动，缺的翻一次，id 本身是中文的跳过。"""
    path = os.path.join(out_dir, "names.lua")
    have = parse_names(path)
    todo = []
    for i in ids:
        if not i or _CJK_RE.search(i):
            continue                     # 已经是中文名，不需要映射
        cur = have.get(i)
        if cur is not None and _CJK_RE.search(cur):
            continue                     # 表里已有中文名，绝不覆盖
        todo.append(i)
    stat = {"added": 0, "todo": len(todo), "have": len(have), "error": ""}
    if not todo:
        return stat
    if not base:
        stat["error"] = "没有配置模型 API，模组名未翻译"
        return stat
    got = translate_names(todo, base, model, target, timeout, log)
    if not got:
        stat["added"] = 0
        return stat
    have.update(got)
    write_names(path, have)
    stat["added"] = len(got)
    return stat


def write(out_dir, mod_root, rows, staging_name="", target="简体中文",
          base="", model="", log=None):
    """写一份账本，并更新索引。返回记录（写进 results 里给界面看）。

    `mod_root` 是这次翻译用的模组根目录（批量里是输出目录下的副本，
    单模组里就是原目录），用来推断文件夹名和把文件路径转成相对路径。
    """
    folder = folder_name(mod_root) or safe_name(staging_name) or "mod"
    entries = _entries(rows, mod_root)
    mods_dir = os.path.join(out_dir, "mods")
    os.makedirs(mods_dir, exist_ok=True)
    path = os.path.join(mods_dir, folder + ".lua")

    ids = _source_ids(mod_root)

    # 模组名的中文名并进 names.lua：人工整理过的不会被覆盖，缺的才翻
    if ids:
        try:
            ensure_names(out_dir, ids, base=base, model=model,
                         target=target, log=log)
        except Exception as ex:          # noqa: BLE001 - 补名失败不影响账本
            if log:
                log("names: %s" % ex)

    lines = [
        "-- 由 2077 Mod Translator 生成（%s）。" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "-- 改译文请改 z 字段；重跑工具会按原文重新对一遍，译文不同的以这里为准。",
        "return {",
        "  mod = %s," % _q(staging_name or folder),
        "  folder = %s," % _q(folder),
        "  staging = %s," % _q(staging_name),
        "  target = %s," % _q(target),
        "  sourceIds = { %s }," % ", ".join(_q(s) for s in ids),
        "  entries = {",
    ]
    for e in entries:
        lines.append("    { t = %s, o = %s, z = %s, f = %s },"
                     % (_q(e["t"]), _q(e["o"]), _q(e["z"]), _q(e["f"])))
    lines += ["  },", "}", ""]

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    os.replace(tmp, path)

    rec = {"file": "mods/" + folder + ".lua", "folder": folder,
           "mod": staging_name or folder, "staging": staging_name,
           "entries": len(entries), "sourceIds": ids}
    _update_index(out_dir, rec)
    _rebuild_all(out_dir)
    return rec


_LEDGER_ENTRY = re.compile(
    r"\{ t = '((?:[^'\\]|\\.)*)', o = '((?:[^'\\]|\\.)*)', "
    r"z = '((?:[^'\\]|\\.)*)', f = '((?:[^'\\]|\\.)*)' \}")
_LEDGER_MOD = re.compile(r"^  mod = '((?:[^'\\]|\\.)*)'", re.M)
_LEDGER_IDS = re.compile(r"^  sourceIds = \{ (.*?) \}", re.M)


def _rebuild_all(out_dir):
    """把 mods/*.lua 合并成一份 all.lua，给运行时 mod 一次读完。

    CET 的 Lua 没有 JSON 解析器、也不能列目录，所以必须有一个汇总表：
    运行时只 loadfile 这一个文件，把所有模组的 o/z 装进查表结构。
    """
    mods_dir = os.path.join(out_dir, "mods")
    if not os.path.isdir(mods_dir):
        return ""
    blocks = []
    for fn in sorted(os.listdir(mods_dir)):
        if not fn.lower().endswith(".lua"):
            continue
        p = os.path.join(mods_dir, fn)
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        m = _LEDGER_MOD.search(text)
        if not m:
            continue
        folder = fn[:-4]
        ids = []
        mi = _LEDGER_IDS.search(text)
        if mi:
            ids = re.findall(r"'((?:[^'\\]|\\.)*)'", mi.group(1))
        entries = _LEDGER_ENTRY.findall(text)
        if not entries:
            continue
        lines = [
            "  [%s] = {" % _q(folder),
            "    mod = %s," % _q(_unesc(m.group(1))),
            "    sourceIds = { %s }," % ", ".join(_q(_unesc(s)) for s in ids),
            "    entries = {",
        ]
        for t, o, z, _f in entries:
            lines.append("      { t = %s, o = %s, z = %s },"
                         % (_q(_unesc(t)), _q(_unesc(o)), _q(_unesc(z))))
        lines += ["    },", "  },"]
        blocks.append("\n".join(lines))

    head = ["-- 由 2077 Mod Translator 生成（%s），运行时 mod 读这一个文件。"
            % time.strftime("%Y-%m-%d %H:%M:%S"),
            "-- 按模组切分；entries 里的 o 是原文、z 是译文，运行时按原文查表。",
            "return {"]
    tail = ["}", ""]
    p = os.path.join(out_dir, "all.lua")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(head + blocks + tail))
    os.replace(tmp, p)
    return p


def _update_index(out_dir, rec):
    p = os.path.join(out_dir, "index.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            idx = json.load(f)
        if not isinstance(idx, dict):
            idx = {}
    except Exception:
        idx = {}
    mods = [m for m in (idx.get("mods") or [])
            if isinstance(m, dict) and m.get("file") != rec["file"]]
    mods.append(rec)
    mods.sort(key=lambda m: str(m.get("folder") or "").lower())
    idx = {"version": 1, "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
           "mods": mods}
    os.makedirs(out_dir, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
