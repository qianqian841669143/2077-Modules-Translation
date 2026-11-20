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

# rcf（Redscript 配置框架）的注册：DVRCF.Register(game, "id", "显示名", …)。
# 这个 id 就是 rcf bridge 的 `listMods` 里的 `mod.id`，跟显示名是两个字符串——
# Monowire Rework 注册成 "MonowireE3" / "Monowire Rework"，两张表都得建键。
_RCF_RE = re.compile(
    r'DVRCF\.Register\s*\(\s*[\w.]+\s*,\s*"((?:[^"\\]|\\.)*)"\s*,'
    r'\s*"((?:[^"\\]|\\.)*)"')

# nativeSettings 的顶层页签：native_settings bridge 的 listMods 用
# `id = tostring(tabPath)`、`name = tab.label`，所以键是 "/ENC" 这种带前导斜杠的串。
# 只认 addTab —— addSubcategory 是页签**里**的分类，不是模组名。
_TAB_RE = re.compile(
    r'nativeSettings\.addTab\s*\(\s*"((?:[^"\\]|\\.)*)"\s*,'
    r'\s*"((?:[^"\\]|\\.)*)"')

# 扫出来的「名字」里混着框架自己的提示语，别写进 names.lua
_NAME_JUNK = re.compile(r"(installed!|^nomod$|^nil$|^unknown$)", re.I)

_SCAN_SKIP = {".git", "__pycache__", "node_modules", ".svn"}

# ms 的 ModSettings.mod 有两种写法：作者直接写可读名（"Apex Grapple"），
# 或者写一个 id（"CNC-nttn-Framework-Title"）。id 逐词直译出来是
# 「CNC-nttn 框架标题」这种谁也看不懂的东西——这种值要换回它所属模组的
# 正式名（staging 目录名剥掉版本尾巴，得到 "Cosmopolitan Framework"）。
_ID_SHAPE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+$")

# Vortex staging 目录名的尾巴，从最具体剥到最兜底：
#   <名> 36235 1.3.2 2026-11-02T21-10Z n1B61wfRS
#   <名> 2026-11-02T21-10Z n1B61wfRS
#   <名> 1.4-19632-1-4-1776700679
_MOD_TAIL_RULES = (
    re.compile(r"^(.*?)\s+\d+\s+\S+\s+\d{4}-\d{2}-\d{2}T[0-9T:\-]+Z\s+[A-Za-z0-9]+$"),
    re.compile(r"^(.*?)\s+\d{4}-\d{2}-\d{2}T[0-9T:\-]+Z\s+[A-Za-z0-9]+$"),
    re.compile(r"^(.*?)\s+\d+(?:\.\d+)*-\d+(?:-\d+)+$"),
)
_TAIL_VER = re.compile(r"^(.*?)\s+[Vv]?\d+(?:\.\d+)+$")
# "Always_Best_Quality For 2.5" 里的 2.5 是名字的一部分（适用游戏 2.5），别剥
_TAIL_STOP = re.compile(r"(?:^|\s)(?:for|with|and|of|to|in|by|vs|@)$", re.I)

# staging 根上直接就是这些目录时，说明传进来的是单个模组目录而不是 staging
_GAME_DIRS = {"r6", "bin", "archive", "red4ext", "mods", "v2077",
              "cyberpunk 2077", "tools"}


def mod_title(dirname):
    """把 Vortex staging 目录名剥成作者写的模组名；剥不出来就返回原名。

    目录名是 `Cosmopolitan Framework 35340 1.0 2026-10-31T04-06Z NEnvE3Wgg`
    这种「名字 + 版本 + 时间戳 + 随机 ID」，名字是要拿去翻的那部分。
    """
    name = re.sub(r"\s+", " ", str(dirname or "")).strip()
    if not name:
        return ""
    for rx in _MOD_TAIL_RULES:
        m = rx.match(name)
        if m and m.group(1).strip():
            name = m.group(1).strip()
            break
    m = _TAIL_VER.match(name)
    if m and m.group(1).strip() and not _TAIL_STOP.search(m.group(1).strip()):
        name = m.group(1).strip()
    return name


def _is_mod_root(root):
    """root 本身是一个模组目录（下面直接是 r6/bin/…），还是整个 staging？"""
    try:
        names = {n.lower() for n in os.listdir(root)}
    except OSError:
        return False
    return bool(names & _GAME_DIRS)

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


def scan_name_pairs(root):
    """扫出「模组显示名」候选，返回 [(键, 英文名)]，按出现顺序去重。

    三个 bridge 给的 id 形状各不相同，所以两种键都要建：
        ms      @runtimeProperty("ModSettings.mod", "X")        → 键 "X"
        rcf     DVRCF.Register(game, "MonowireE3", "Monowire…") → 键 "MonowireE3" + 英文名
        native  nativeSettings.addTab("/ENC", "ENC")            → 键 "/ENC" + 英文名

    ms 的 X 有时是 id 不是名字（"CNC-nttn-Framework-Title"）。这种值不按字面
    翻，改用它所在 staging 目录的正式名（"Cosmopolitan Framework"）——否则
    界面上会出现「CNC-nttn 框架标题」这种谁也看不懂的直译。

    运行时先按 id 查、再按英文名查（init.lua 的 lookupName），所以
    names.lua 里键和英文名各写一份，任一形状对上就能换。
    """
    out = []
    seen = set()

    def add(key, name):
        key = re.sub(r"\s+", " ", str(key or "")).strip()
        name = re.sub(r"\s+", " ", _unesc(str(name or ""))).strip()
        if not name or _CJK_RE.search(name) or _NAME_JUNK.search(name):
            return
        for k in ((key, name) if key and key != name else (name,)):
            if k not in seen:
                seen.add(k)
                out.append((k, name))

    if not root or not os.path.isdir(root):
        return out

    mod_root_mode = _is_mod_root(root)

    def dir_title(dirpath):
        """当前 .reds 所属模组目录的正式名；判断不出来就返回空串。"""
        if mod_root_mode:
            cand = os.path.basename(os.path.normpath(root))
        else:
            rel = os.path.relpath(dirpath, root)
            if rel == ".":
                cand = os.path.basename(os.path.normpath(root))
            else:
                cand = rel.split(os.sep)[0]
        title = mod_title(cand)
        if not title or title.lower() in _GAME_DIRS:
            return ""
        return title

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SCAN_SKIP]
        for fn in filenames:
            low = fn.lower()
            if not low.endswith((".reds", ".lua")):
                continue
            try:
                with open(os.path.join(dirpath, fn), "r",
                          encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            if "ModSettings.mod" in text:
                for m in _MOD_RE.finditer(_strip_comments(text)):
                    key = m.group(1)
                    name = key
                    if _ID_SHAPE.match(key):
                        name = dir_title(dirpath) or key
                    add(key, name)
            if "DVRCF.Register" in text:
                for m in _RCF_RE.finditer(text):
                    add(m.group(1), m.group(2))
            if "nativeSettings.addTab" in text:
                for m in _TAB_RE.finditer(text):
                    add(m.group(1), m.group(2))
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
    "专有品牌/系列名（Cosmopolitan、Kiroshi、Monowire 等）保留原文不译，"
    "只翻它后面的通用词；"
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
        "-- 模组显示名表（mod.id 或英文名 -> 中文名）。工具只补缺的，人工改过的不会被覆盖。",
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


_NAME_CHUNK = 25          # 一批最多翻多少个模组名
_NAME_SINGLE_MAX = 40     # 批里漏掉的名字，最多再逐个补多少个


def _translate_chunk(names, base, model, target, timeout, log):
    """翻一小批名字。返回 {英文名: 中文名}；模型回的数组长度对不上就整批作废。"""
    import json as _json
    import mt_client as MT

    sys_prompt = _NAME_SYS.replace("简体中文", target, 1)
    try:
        raw = MT.chat(
            base, model,
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": _json.dumps(names, ensure_ascii=False)}],
            temperature=0.2, timeout=timeout,
            max_tokens=max(256, 80 * len(names)))
    except Exception as ex:                       # noqa: BLE001
        if log:
            log("names: 翻 %d 个模组名失败 %s" % (len(names), ex))
        return {}
    got = MT.parse_json_array(raw, len(names))
    if got is None:
        try:
            got = MT._parse_lines(raw, len(names))   # noqa: SLF001 - 同包内部兜底
        except Exception:                          # noqa: BLE001
            got = None
    if not got:
        return {}
    out = {}
    for i, name in zip(names, got):
        lab = _clean_name(name)
        if lab:
            out[i] = lab
    return out


def _translate_batch(chunk, base, model, target, timeout, log):
    """翻一批；整批空手而归就劈半再来，劈到单个为止。

    模型对「一次 25 个名字」最常见的失败是数组长度对不上，整批作废。
    劈半比逐个补省得多：8 个名字一次失败，劈到单个是 8 次调用，劈半是 4 次。
    """
    got = _translate_chunk(chunk, base, model, target, timeout, log)
    if got or len(chunk) <= 1:
        return got
    mid = len(chunk) // 2
    out = _translate_batch(chunk[:mid], base, model, target, timeout, log)
    out.update(_translate_batch(chunk[mid:], base, model, target, timeout, log))
    return out


def translate_names(names, base, model, target="简体中文", timeout=180, log=None):
    """只翻模组名，分批 + 补漏。返回 {英文名: 中文名}，没翻出来的不放进结果。

    以前是一把全丢给模型、要一个等长 JSON 数组：数组少一个多一个，长度校验
    就整批作废，names.lua 里于是只有一部分名字，而且失败是静默的（批量那条
    路调 write 时没传 log）。现在每批 25 个，批结果都进日志，漏掉的再逐个补。
    """
    todo = [str(x).strip() for x in (names or []) if str(x or "").strip()]
    todo = list(dict.fromkeys(todo))
    if not todo or not base:
        return {}
    out = {}
    for i in range(0, len(todo), _NAME_CHUNK):
        chunk = todo[i:i + _NAME_CHUNK]
        got = _translate_batch(chunk, base, model, target, timeout, log)
        out.update(got)
        if log:
            log("names: 第 %d 批 %d 个，回来 %d 个"
                % (i // _NAME_CHUNK + 1, len(chunk), len(got)))
    missing = [n for n in todo if n not in out][:_NAME_SINGLE_MAX]
    for n in missing:
        out.update(_translate_chunk([n], base, model, target, timeout, log))
    if log and missing:
        log("names: 逐个补 %d 个，补回 %d 个"
            % (len(missing), sum(1 for n in missing if n in out)))
    return out


def ensure_names(out_dir, pairs, base="", model="", target="简体中文",
                 timeout=180, log=None):
    """补 names.lua。pairs: [(键, 英文名)]，键和英文名各写一份。

    键是运行时拿得到的 mod.id：ms 的注册名 / rcf 的 id / nativeSettings 的
    页签路径。英文名也建键——runtime 按 id 查不到时会拿 `mod.name` 再查
    （init.lua 的 lookupName）。表里已经有中文的键，一个都不覆盖。
    """
    path = os.path.join(out_dir, "names.lua")
    have = parse_names(path)

    by_eng = {}
    for key, eng in pairs or []:
        eng = str(eng or "").strip()
        if not eng or _CJK_RE.search(eng):
            continue                     # 名字本来就是中文，不需要映射
        keys = by_eng.setdefault(eng, [])
        key = str(key or "").strip()
        if key and key != eng and key not in keys:
            keys.append(key)

    def needs(eng, keys):
        if _clean_name(have.get(eng)):
            return False                     # 英文名已经有中文
        if any(_clean_name(have.get(k)) for k in keys):
            # 键上有中文：只有「id 键 + 已经拿到正式名」才需要重翻改写
            return any(_ID_SHAPE.match(k) for k in keys)
        return True

    todo = [eng for eng, keys in by_eng.items() if needs(eng, keys)]
    stat = {"added": 0, "todo": len(todo), "have": len(have), "error": ""}
    if not todo:
        return stat
    if not base:
        stat["error"] = "没有配置模型 API，模组名未翻译"
        return stat
    got = translate_names(todo, base, model, target, timeout, log)
    if not got:
        stat["error"] = "模组名一个都没翻出来"
        return stat

    added = 0
    for eng, keys in by_eng.items():
        zh = got.get(eng) or _clean_name(have.get(eng))
        if not zh:
            continue
        for k in [eng] + keys:
            old = have.get(k)
            if old == zh:
                continue
            # id 键可以用正式名的译文改写；其它键保持「已有的中文不覆盖」
            if old and not (_ID_SHAPE.match(k) and eng != k):
                continue
            have[k] = zh
            added += 1
    write_names(path, have)
    stat["added"] = added
    if log:
        log("names.lua：%d 条模组名（这轮新增 %d）" % (len(have), added))
    return stat


def _read_entries(path):
    """读回已经写出去的模组账本，按文件里的顺序返回条目。

    只认本工具写出来的那种格式（`{ t = .., o = .., z = .., f = .. }`）；
    照这个格式手写进去的条目也会被读到，格式不同的行会被忽略。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return []
    return [{"t": _unesc(t), "o": _unesc(o), "z": _unesc(z), "f": _unesc(f)}
            for t, o, z, f in _LEDGER_ENTRY.findall(text)]


def _merge_entries(old, new):
    """按原文合并新旧条目：这轮没出现的旧条目留下，同原文的以新译文为准。

    以前是整份覆盖写。账本从 32,505 条缩到 27,740 条就是因为某一轮只提取到
    其中一部分原文，剩下的被整块抹掉了（CTO 那个合集从 671 条掉到 166 条）。
    合并之后重跑一个模组不会再丢掉别的轮次写进去的条目；顺带也缓解了
    folder_name 撞车（两个模组推成同一个文件夹名）时后写的挤掉先写的问题。
    """
    out, idx = [], {}
    for e in old or []:
        o = str(e.get("o") or "")
        if not o or o in idx:
            continue                    # 同一原文只留第一条：运行时也是按原文查
        idx[o] = len(out)
        out.append({"t": str(e.get("t") or ""), "o": o,
                    "z": str(e.get("z") or ""), "f": str(e.get("f") or "")})
    base, added, updated, matched = len(out), 0, 0, 0
    for e in new or []:
        o = str(e.get("o") or "")
        if not o:
            continue
        i = idx.get(o)
        if i is None:
            idx[o] = len(out)
            out.append({"t": str(e.get("t") or ""), "o": o,
                        "z": str(e.get("z") or ""), "f": str(e.get("f") or "")})
            added += 1
            continue
        matched += 1
        cur = out[i]
        if str(cur.get("z") or "") != str(e.get("z") or ""):
            cur["t"] = str(e.get("t") or cur.get("t") or "")
            cur["z"] = str(e.get("z") or "")
            updated += 1
        if e.get("f"):
            cur["f"] = str(e["f"])      # 文件/行号以这轮为准
    return out, added, updated, base - matched


def write(out_dir, mod_root, rows, staging_name="", target="简体中文",
          base="", model="", log=None):
    """写一份账本，并更新索引。返回记录（写进 results 里给界面看）。

    `mod_root` 是这次翻译用的模组根目录（批量里是输出目录下的副本，
    单模组里就是原目录），用来推断文件夹名和把文件路径转成相对路径。
    """
    folder = folder_name(mod_root) or safe_name(staging_name) or "mod"
    mods_dir = os.path.join(out_dir, "mods")
    os.makedirs(mods_dir, exist_ok=True)
    path = os.path.join(mods_dir, folder + ".lua")

    # 合并写，不是覆盖写：这一轮没提取到的旧条目必须留着（见 _merge_entries）
    fresh = _entries(rows, mod_root)
    entries, added, updated, kept = _merge_entries(_read_entries(path), fresh)
    if log and (added or updated or kept):
        log("账本 %s：本轮 %d 条，合并后 %d 条（新增 %d / 更新 %d / 保留旧 %d）"
            % (folder, len(fresh), len(entries), added, updated, kept))

    ids = _source_ids(mod_root)

    # 模组名的中文名并进 names.lua：人工整理过的不会被覆盖，缺的才翻。
    # 三种注册写法都要扫（ms 的 runtimeProperty / rcf 的 DVRCF.Register /
    # nativeSettings 的 addTab）——只认 ms 那一种时，MCM 里一半模组名永远是英文。
    pairs = scan_name_pairs(mod_root)
    if not pairs and ids:
        pairs = [(i, i) for i in ids]
    if pairs:
        try:
            ensure_names(out_dir, pairs, base=base, model=model,
                         target=target, log=log)
        except Exception as ex:          # noqa: BLE001 - 补名失败不影响账本
            if log:
                log("names: %s" % ex)

    lines = [
        "-- 由 2077 Mod Translator 生成（%s）。" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "-- 改译文请改 z 字段；重跑同一个模组时按原文对一遍，同原文以新一轮的译文为准，",
        "-- 这一轮没提取到的旧条目会原样保留（合并写，不是覆盖写）。",
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
           "entries": len(entries),
           # 已填多少条：模组清单靠这个算「待翻译 / 已汉化」。
           # 老索引没有这个字段，读的时候按 0 处理（会显示成待翻译，重跑一次提取就补齐）。
           "filled": sum(1 for e in entries if str(e.get("z") or "").strip()),
           "sourceIds": ids}
    _update_index(out_dir, rec)
    _rebuild_all(out_dir)
    return rec


_LEDGER_ENTRY = re.compile(
    r"\{ t = '((?:[^'\\]|\\.)*)', o = '((?:[^'\\]|\\.)*)', "
    r"z = '((?:[^'\\]|\\.)*)', f = '((?:[^'\\]|\\.)*)' \}")
_LEDGER_MOD = re.compile(r"^  mod = '((?:[^'\\]|\\.)*)'", re.M)
_LEDGER_IDS = re.compile(r"^  sourceIds = \{ (.*?) \}", re.M)


def file_stats(path):
    """按文件统计一个模组账本：每个文件共几条、还差几条没填。

    模组清单要显示「这个模组还有哪些文件要翻」，数据就在账本条目里
    （每条带 f = 文件）。只读，解析失败就返回空表，不影响清单。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    agg = {}
    for m in _LEDGER_ENTRY.finditer(text):
        _t, _o, z, f = m.groups()
        # f 是「相对路径:行号」，统计到文件就得把行号去掉
        key = re.sub(r":\d+$", "", _unesc(f)) or "-"
        d = agg.get(key)
        if d is None:
            d = agg[key] = {"file": key, "total": 0, "todo": 0}
        d["total"] += 1
        if not _unesc(z).strip():
            d["todo"] += 1
    out = list(agg.values())
    out.sort(key=lambda d: (-d["todo"], -d["total"], d["file"]))
    return out


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
