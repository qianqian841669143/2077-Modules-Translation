# -*- coding: utf-8 -*-
"""
mod_text_engine.py —— 《赛博朋克 2077》模组设置文本提取/回填引擎

支持三类来源:
  1) REDscript / XML   : @"runtimeProperty" 注解 + 字符串字面量
  2) CET Lua           : Mod Settings / Native Settings UI / ImGui 的设置项文本
  3) 纯字符串长句      : 宽松模式下兜底收录(可选)

设计要点:
  * 用一个轻量扫描器同时产出「字符串字面量」和「token 流」,
    因此能识别多行函数调用、跨行参数、注释、r""/n""/t"" 前缀。
  * 回填时按 (行号, 原文) 定位到具体字面量, 保留原引号类型,
    并做转义处理, 不会把双引号或反斜杠写坏。
"""
import bisect
import os
import re

try:
    from . import text_noise as NOISE
except ImportError:                     # 单文件入口(pyinstaller / 直接运行)
    import text_noise as NOISE

# ---------------------------------------------------------------- 类型常量
TYPE_NAME = "显示名称"
TYPE_DESC = "描述"
TYPE_CAT = "分类"
TYPE_MOD = "模组名"
TYPE_OPT = "选项文本"
TYPE_UI = "界面文本"
TYPE_LONG = "长句文本"          # 宽松兜底收进来的长句(可能含日志/调试文本)

SETTING_TYPES = {TYPE_NAME, TYPE_DESC, TYPE_CAT, TYPE_MOD, TYPE_OPT}
UI_TYPES = {TYPE_UI, TYPE_LONG}

SRC_EXTS = {".reds", ".xml", ".lua"}
MAX_BYTES = 8 * 1024 * 1024        # 单个源文件上限, 超过则跳过(避免卡死)
SKIP_DIRS = {"redmod_backup", ".git", "__pycache__", "node_modules",
             "_internal", "dist", "build"}

# ---------------------------------------------------------------- 文本判定
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LOCALE_RE = re.compile(r"^[a-z]{2}([_-][A-Za-z]{2,4})?$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
URL_RE = re.compile(r"^(https?|ftp)://", re.I)
HEX_RE = re.compile(r"^#?[0-9A-Fa-f]{6,8}$")
# 语言包文件: en_US.lua / zh-cn.lua / it-it.lua / English.reds / ChineseSimplified.reds
LOCALE_FILE_RE = re.compile(r"(^|/)[a-z]{2}[-_][a-z]{2,4}\.(lua|reds|json)$", re.I)
# GarageCoreLocalization_de.reds / strings_ko.lua: 「本地化字样 + 语言后缀」的文件名。
# 这类文件是同一个键在别的语言里的副本, 当源文件扫会产出整片外文行。
LOCALE_SUFFIX_RE = re.compile(
    r"[_\-\.](?:en|de|ko|kr|ja|jp|pl|fr|es|it|ru|pt|br|cz|cs|hu|tr|uk|zh|cn|"
    r"tw|nl|th|vi|id|ro|el|sv|no|da|fi|hi|ar|he)$", re.I)
LOCALE_STEM_HINT = re.compile(
    r"(?:localization|localisation|locales?|langs?|i18n|l10n|strings?|"
    r"translations?|language|packages?)$", re.I)
LOCALE_NAMES = {
    "english", "chinesesimplified", "chinesetraditional", "chinese", "japanese",
    "korean", "french", "german", "spanish", "spanishlatam", "spanishspain",
    "italian", "polish", "portuguese", "portuguesebrazil", "brazilianportuguese",
    "russian", "turkish", "czech", "hungarian", "ukrainian", "arabic", "dutch",
    "thai", "vietnamese", "indonesian", "romanian", "greek", "swedish",
    "norwegian", "danish", "finnish", "hindi", "bengali", "persian", "hebrew",
    "bulgarian", "croatian", "serbian", "slovak", "slovenian", "estonian",
    "latvian", "lithuanian", "catalan", "filipino", "malay", "tamil", "telugu",
    "tchinese", "schinese", "japaneseja", "englishus",
}


def is_locale_file(path):
    """判断是否为语言包文件 (en_US.lua / English.reds / zh-cn.json ...)"""
    if LOCALE_FILE_RE.search(path.replace("\\", "/")):
        return True
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    if re.sub(r"[^a-z]", "", stem) in LOCALE_NAMES:
        return True
    m = LOCALE_SUFFIX_RE.search(stem)
    if m and LOCALE_STEM_HINT.search(stem[:m.start()]):
        return True
    return False
SQL_RE = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|WITH|PRAGMA)\s", re.I)
CODEY_RE = re.compile(r"(function\s*\(|\blocal\s+\w+\s*=|\breturn\s+|^\s*\{|\}\s*$|"
                      r"\bself\.|\bend\b\s*$)", re.I)


MARKUP_RE = re.compile(r"</?[A-Za-z][^<>]*>")


def _looks_like_path(t):
    """资源路径 / ResRef / TweakDB 记录 / 文件名"""
    # <Rich ...>...</> 是游戏富文本标记，不是路径；即使其中没有空格也不能误判。
    if MARKUP_RE.search(t):
        return False
    if ("\\" in t or "/" in t):
        # 中文里 `/` 常作「或」用(启用/禁用、实验性/有趣), 且中文不以空格分词,
        # 下面那条「无空格才算路径」会把整句中文判成路径, 直接丢掉。
        if CJK_RE.search(t):
            return False
        # 带空格的一般是句子("He said \\ ok"), 无空格才当作路径
        if not re.search(r"\s", t):
            return True
        if re.match(r"^(base|r6|mod|archive|engine)\\", t, re.I):
            return True
    if re.match(r"^[\w.\-]+\.(lua|reds|json|png|dds|dll|exe|txt|xml|ink|inkatlas|"
                r"wav|ogg|mp3|wem|bin|archive|xl|tweak|script)$", t, re.I):
        return True
    return False


# CP2077 本地化 key 的形态：UI-Labels-Resume / UI-Mod-Settings-foo-label。
# 这类串是 onscreens.json 的查表键，不是文案；翻掉它等于把引用打断。
_LOC_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:[-.][A-Za-z0-9_]+){2,}$")


def is_loc_key(s):
    """判断一个字符串是不是本地化查表键（而不是给玩家看的文本）。

    只收「无空格的连字符串」：真正的文案几乎总带空格或标点，
    而 key 是 Seg-Seg-Seg 形式。宁可漏判也不误伤——漏判的代价是
    多一条待翻行，误伤的代价是整块界面文本凭空消失。
    """
    t = (s or "").strip()
    if not t or len(t) < 10:
        return False
    if any(c in t for c in " \t,;:!?()[]{}'\""):
        return False
    if not _LOC_KEY_RE.match(t):
        return False
    segs = re.split(r"[-.]", t)
    if len(segs) < 3:
        return False
    # 至少两段以大写字母开头，避免把 kebab-case 的普通词组当成 key
    caps = sum(1 for x in segs if x[:1].isupper())
    return caps >= 2


def is_translatable(s):
    """严格判定: 值得翻译的界面/设置文本"""
    if not s:
        return False
    t = s.strip()
    if not t:
        return False
    if _looks_like_path(t) or URL_RE.match(t) or HEX_RE.match(t):
        return False
    if LOCALE_RE.match(t):
        return False
    if is_loc_key(t):
        return False
    if CJK_RE.search(t):
        return True
    # 资源转储 / TweakDB 记录名 / 桥接签名 / SQL / 调试 dump: 哪个档位都不是文本
    if NOISE.hard_family(t):
        return False
    if len(t) < 2:
        return False
    if IDENT_RE.match(t):           # 纯标识符 / 点分名
        return False
    if t.startswith("#") or t.startswith("$"):
        return False
    words = re.findall(r"[A-Za-z]{2,}", t)
    return len(words) >= 2


# 当前目标语言 -> 该语言文本常用的 Unicode 字符范围。
# 用于提取时跳过已经翻好的目标语言文本, 避免把中文/日文等再次收进原文列。
# 英语/西班牙语/德语/法语等拉丁字母语言与英文源码无法只靠字符区分,
# 它们仍通过「跳过目标语言文件」来处理。
_TARGET_SCRIPT_RE = {
    "zh-CN": re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"),
    "zh-TW": re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"),
    "ja-JP": re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]"),
    "ko-KR": re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]"),
    "ru-RU": re.compile(r"[\u0400-\u04ff]"),
    "ar-SA": re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]"),
}


def is_target_language_text(s, target_locale):
    """判断一段文字是否已经是当前目标语言。

    只对能通过字符范围可靠辨认的语言生效(中文/日文/韩文/俄文/阿拉伯文),
    避免把英文源码当成目标语言误跳过。
    """
    if not s or not target_locale:
        return False
    rx = _TARGET_SCRIPT_RE.get(target_locale)
    return bool(rx and rx.search(s))


# ---------------------------------------------------------------- 本地化替换表
# StrReplaceAll(description, "旧文本", "新文本") 常被模组作者用来放一份
# 多语言替换表，例如:
#   // en
#   description = StrReplaceAll(description, "Old English", "New English");
#   // zh-cn
#   description = StrReplaceAll(description, "旧中文", "新中文");
# 这类行不能把每种语言都当作普通界面文本提取，否则一次会产出几十条重复文本，
# 用户翻译后会同时改坏多种语言。下面只保留当前目标语言对应的「新文本」；
# 目标语言不存在时保留英文条目作为参考。
LOCALE_COMMENT_RE = re.compile(
    r"^\s*//\s*([A-Za-z]{2,3}(?:[-_][A-Za-z]{2,4})?)\s*$")
_TARGET_LOCALE_ALIASES = {
    "zh-cn": {"zh-cn", "zh-hans", "zh", "chs", "cn", "schinese",
              "chinesesimplified", "simplifiedchinese"},
    "zh-tw": {"zh-tw", "zh-hant", "zh", "cht", "tw", "tchinese",
              "chinesetraditional", "traditionalchinese"},
    "en-us": {"en-us", "en-gb", "en", "english", "englishus"},
    "ja-jp": {"ja-jp", "ja", "japanese"},
    "ko-kr": {"ko-kr", "ko", "korean"},
    "ru-ru": {"ru-ru", "ru", "russian"},
    "es-es": {"es-es", "es", "spanish", "spanishspain"},
    "de-de": {"de-de", "de", "german"},
    "fr-fr": {"fr-fr", "fr", "french"},
}


def _locale_key(tag):
    return re.sub(r"[^a-z0-9-]", "", (tag or "").strip().lower().replace("_", "-"))


def _locale_matches(tag, target_locale):
    """语言标记匹配；兼容 en / en-US、zh-cn / zh-CN 等写法。"""
    a = _locale_key(tag)
    b = _locale_key(target_locale)
    if not a or not b:
        return False
    if a == b:
        return True
    return a in _TARGET_LOCALE_ALIASES.get(b, set())


def is_target_locale_replacement(ctx, target_locale):
    """该行是否是从目标语言 StrReplaceAll 表中选出的条目。"""
    m = re.match(r"^StrReplaceAll\[([^\]]+)\]$", ctx or "")
    return bool(m and _locale_matches(m.group(1), target_locale))


def _preceding_locale_tag(text, pos):
    """读取调用前 1-2 行内的 // en / // zh-cn 语言标记。"""
    line_start = text.rfind("\n", 0, pos) + 1
    before = text[:line_start].splitlines()
    blanks = 0
    for line in reversed(before[-3:]):
        if not line.strip():
            blanks += 1
            if blanks > 1:
                break
            continue
        m = LOCALE_COMMENT_RE.match(line)
        return m.group(1) if m else ""
    return ""


def _locale_replace_entries(text, toks, spans, lits):
    """识别带 // 语言标记的 StrReplaceAll 多语言替换表。

    返回 [{locale, old_start, new_start}]；只有至少出现两种语言且调用次数
    达到 2 次才启用，避免把普通的一次性 StrReplaceAll 误判成语言表。
    """
    by_start = {l.start: l for l in lits}
    entries = []
    for name, oi, close in spans:
        if name.split(".")[-1].lower() != "strreplaceall":
            continue
        args = _split_args(toks, oi, close)
        if len(args) < 3:
            continue
        a, b = args[1]
        c, d = args[2]
        if a != b or c != d or toks[a].kind != "str" or toks[c].kind != "str":
            continue
        old_lit = by_start.get(toks[a].start)
        new_lit = by_start.get(toks[c].start)
        if old_lit is None or new_lit is None:
            continue
        loc = _preceding_locale_tag(text, toks[oi].start)
        if loc:
            entries.append({"locale": loc, "old_start": old_lit.start,
                            "new_start": new_lit.start})
    if len(entries) < 2 or len({e["locale"].lower() for e in entries}) < 2:
        return []
    return entries


def is_label_text(s):
    """宽松判定: 已知 UI 控件参数位置上的标签, 允许单个单词("Apply"/"Close")"""
    t = (s or "").strip()
    if not t:
        return False
    if is_translatable(t):
        return True
    if t.startswith("##") or t.startswith("#") or t.startswith("$"):
        return False
    if CJK_RE.search(t):
        return True
    # 首字母大写的英文单词, 例如 Apply / Save / Close / Health
    if re.match(r"^[A-Z][a-z]{2,}( [A-Za-z0-9'.,!?:;()\-]+)*$", t) and len(t) <= 60:
        return True
    # 全大写缩写: HUD / MCM / OFF
    if t.isupper() and t.isalpha() and 2 <= len(t) <= 8:
        return True
    return False


# ---------------------------------------------------------------- 扫描器
class Lit(object):
    __slots__ = ("start", "end", "quote", "inner", "line", "prefix")

    def __init__(self, start, end, quote, inner, line, prefix=""):
        self.start = start
        self.end = end
        self.quote = quote
        self.inner = inner
        self.line = line
        self.prefix = prefix

    def __repr__(self):
        return "Lit(%d:%d %s%r)" % (self.line, self.start, self.prefix, self.inner)


class Tok(object):
    __slots__ = ("kind", "val", "start", "end", "line")

    def __init__(self, kind, val, start, end, line):
        self.kind = kind
        self.val = val
        self.start = start
        self.end = end
        self.line = line


_REDS_PREFIX = set("rnts")
IDENT_TOK_RE = re.compile(r"[A-Za-z_]\w*")
NUM_TOK_RE = re.compile(r"\d+\.?\d*")
LONG_OPEN_RE = re.compile(r"\[(=*)\[")


def scan_source(text, lang):
    """返回 (tokens, literals)。lang: 'lua' | 'reds' | 'xml'"""
    toks, lits = [], []
    i, n, line = 0, len(text), 1
    lua = (lang == "lua")
    xml = (lang == "xml")
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r":
            i += 1
            continue
        # ---- 注释
        if lua and text.startswith("--", i):
            j = text.find("\n", i)
            if j < 0:
                break
            line += text.count("\n", i, j)
            i = j
            continue
        if (not lua) and (not xml) and text.startswith("//", i):
            j = text.find("\n", i)
            if j < 0:
                break
            i = j
            continue
        if (not lua) and (not xml) and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                break
            line += text.count("\n", i, j)
            i = j + 2
            continue
        # ---- Lua 长字符串 [[ ]] / [=[ ]=]  : 跳过(不适合逐行回填)
        if lua and c == "[":
            m = LONG_OPEN_RE.match(text, i)
            if m:
                close = "]" + m.group(1) + "]"
                j = text.find(close, m.end())
                if j < 0:
                    break
                line += text.count("\n", i, j)
                i = j + len(close)
                continue
        # ---- 字符串
        if c in "\"'":
            j = i + 1
            while j < n:
                ch = text[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == c:
                    break
                j += 1
            if j >= n:
                break
            inner = text[i + 1:j]
            prefix = ""
            if not lua:
                k = i
                while k > 0 and (text[k - 1].isalnum() or text[k - 1] == "_"):
                    k -= 1
                run = text[k:i]
                if run in ("r", "n", "t", "s"):
                    prefix = run
            lit = Lit(i, j + 1, c, inner, line, prefix)
            lits.append(lit)
            toks.append(Tok("str", inner, i, j + 1, line))
            line += inner.count("\n")
            i = j + 1
            continue
        if xml:
            if c == "<":
                # 标签整体跳过, 只保留属性里的字符串(由上面的分支处理)
                j = text.find(">", i)
                i = (j + 1) if j >= 0 else n
                continue
            # 文本节点
            j = text.find("<", i)
            if j < 0:
                j = n
            seg = text[i:j]
            if seg.strip():
                lits.append(Lit(i, j, "", seg.strip(), line))
            line += seg.count("\n")
            i = j
            continue
        # ---- 标识符 / 数字 / 标点
        if c.isalpha() or c == "_":
            m = IDENT_TOK_RE.match(text, i)
            toks.append(Tok("ident", m.group(0), i, m.end(), line))
            i = m.end()
            continue
        if c.isdigit():
            m = NUM_TOK_RE.match(text, i)
            toks.append(Tok("num", m.group(0), i, m.end(), line))
            i = m.end()
            continue
        for p in ("...", "..", "==", "~=", "<=", ">=", "::"):
            if text.startswith(p, i):
                toks.append(Tok("op", p, i, i + len(p), line))
                i += len(p)
                break
        else:
            toks.append(Tok("op", c, i, i + 1, line))
            i += 1
    return toks, lits


# ---------------------------------------------------------------- 规则表
SETTINGS_KEYS = {
    "mod": TYPE_MOD, "modname": TYPE_MOD, "mod_name": TYPE_MOD,
    "category": TYPE_CAT, "subcategory": TYPE_CAT, "section": TYPE_CAT,
    "group": TYPE_CAT, "tab": TYPE_CAT, "panel": TYPE_CAT,
    "displayname": TYPE_NAME, "display_name": TYPE_NAME, "title": TYPE_NAME,
    "name": TYPE_NAME, "header": TYPE_NAME,
    "description": TYPE_DESC, "desc": TYPE_DESC, "tooltip": TYPE_DESC,
    "hint": TYPE_DESC, "help": TYPE_DESC, "detail": TYPE_DESC,
    "subtitle": TYPE_DESC, "tooltip_text": TYPE_DESC,
    "label": TYPE_OPT, "labels": TYPE_OPT, "caption": TYPE_OPT,
    "text": TYPE_UI, "buttontext": TYPE_UI, "button_text": TYPE_UI,
    "placeholder": TYPE_UI,
    # 下面这些同时用于「赋值目标名」证据 (text_evidence)：
    # 出现在名字里就说明这个字面量是给玩家看的。
    "msg": TYPE_UI, "message": TYPE_UI, "body": TYPE_UI, "notice": TYPE_UI,
    "notification": TYPE_UI, "summary": TYPE_DESC, "prompt": TYPE_NAME,
    "fullname": TYPE_NAME, "shortname": TYPE_NAME,
    "localizedname": TYPE_NAME, "displaytext": TYPE_UI,
}

LIST_KEYS = {"labels", "options", "items", "choices", "entries", "values", "list"}

# 函数调用规则: 函数名(小写) -> [(参数序号(0起), 类型)]
CALL_RULES = {
    # ------- Native Settings UI (ui.*)  : (path, label, description, ...)
    "addtab": [(1, TYPE_CAT)],
    "addcategory": [(1, TYPE_CAT)],
    "addsubcategory": [(1, TYPE_CAT)],
    "addgroup": [(1, TYPE_CAT)],
    "addswitch": [(1, TYPE_NAME), (2, TYPE_DESC)],
    "addtoggle": [(1, TYPE_NAME), (2, TYPE_DESC)],
    "addbutton": [(1, TYPE_NAME), (2, TYPE_DESC)],
    "addrangeint": [(1, TYPE_NAME), (2, TYPE_DESC)],
    "addrangefloat": [(1, TYPE_NAME), (2, TYPE_DESC)],
    "addsliderint": [(1, TYPE_NAME), (2, TYPE_DESC)],
    "addsliderfloat": [(1, TYPE_NAME), (2, TYPE_DESC)],
    "addselectorstring": [(1, TYPE_NAME), (2, TYPE_DESC), (3, TYPE_OPT)],
    "addselectornumber": [(1, TYPE_NAME), (2, TYPE_DESC), (3, TYPE_OPT)],
    "addkeybinding": [(1, TYPE_NAME), (2, TYPE_DESC)],
    "addinputstring": [(1, TYPE_NAME), (2, TYPE_DESC)],
    "addinputint": [(1, TYPE_NAME), (2, TYPE_DESC)],
    "addinputfloat": [(1, TYPE_NAME), (2, TYPE_DESC)],
    # ------- ImGui
    "button": [(0, TYPE_OPT)],
    "smallbutton": [(0, TYPE_OPT)],
    "checkbox": [(0, TYPE_OPT)],
    "radiobutton": [(0, TYPE_OPT)],
    "selectable": [(0, TYPE_OPT)],
    "menuitem": [(0, TYPE_OPT)],
    "collapsingheader": [(0, TYPE_OPT)],
    "treenode": [(0, TYPE_OPT)],
    "treenodeex": [(0, TYPE_OPT)],
    "combo": [(0, TYPE_OPT)],
    "begincombo": [(0, TYPE_OPT)],
    "inputtext": [(0, TYPE_OPT)],
    "inputtextmultiline": [(0, TYPE_OPT)],
    "sliderfloat": [(0, TYPE_OPT)],
    "sliderint": [(0, TYPE_OPT)],
    "dragfloat": [(0, TYPE_OPT)],
    "dragint": [(0, TYPE_OPT)],
    "coloredit3": [(0, TYPE_OPT)],
    "coloredit4": [(0, TYPE_OPT)],
    "colorpicker3": [(0, TYPE_OPT)],
    "colorpicker4": [(0, TYPE_OPT)],
    "begintabitem": [(0, TYPE_OPT)],
    "beginmenu": [(0, TYPE_OPT)],
    "begin": [(0, TYPE_UI)],
    "text": [(0, TYPE_UI)],
    "textunformatted": [(0, TYPE_UI)],
    "textwrapped": [(0, TYPE_UI)],
    "textdisabled": [(0, TYPE_UI)],
    "textcolored": [(1, TYPE_UI)],
    "bulletttext": [(0, TYPE_UI)],
    "bullettext": [(0, TYPE_UI)],
    "labeltext": [(0, TYPE_UI)],
    "settooltip": [(0, TYPE_UI)],
    "progressbar": [(0, TYPE_OPT)],
    # ------- 常见通知 / Toast 包装函数
    "notify": [(0, TYPE_UI)],
    "notification": [(0, TYPE_UI)],
    "showtoast": [(0, TYPE_UI)],
    "toast": [(0, TYPE_UI)],
    "alert": [(0, TYPE_UI)],
}

# 局部包装函数的参数名 -> 文本类型。
# 有些模组不直接调用 ns.addSwitch, 而是自己封装 switch(path, label, desc, key)。
WRAPPER_PARAM_TYPES = {
    "label": TYPE_NAME, "caption": TYPE_NAME, "title": TYPE_NAME,
    "displayname": TYPE_NAME, "display_name": TYPE_NAME, "name": TYPE_NAME,
    "desc": TYPE_DESC, "description": TYPE_DESC, "tooltip": TYPE_DESC,
    "hint": TYPE_DESC, "help": TYPE_DESC,
    "text": TYPE_UI, "message": TYPE_UI, "msg": TYPE_UI,
}

# 这些变量里累积的字符串通常会作为界面提示显示出来。
UI_TEXT_VARS = {
    "message", "msg", "text", "label", "caption", "title", "desc",
    "description", "notification", "notify", "name",
}
UI_BUFFER_VARS = {
    "found", "parts", "lines", "messages", "msgs", "texts", "notifications",
}
UI_NOTIFY_CALLS = {"notify", "notification", "showtoast", "toast", "alert"}
TEXT_TABLE_NAME_RE = re.compile(
    r"label|text|option|choice|caption|rumou?r|dialog|message|notify|subtitle",
    re.I,
)

# 宽松模式下不采集其参数的函数(纯技术用途)
DEAD_CALLS = {
    "require", "dofile", "loadfile", "load", "print", "printf", "io", "os",
    "registerforevent", "observe", "override", "getmod", "getmodbyname",
    "getlocalizedtext", "t", "gettext", "log", "logdebug", "loginfo",
    "logwarn", "logerror", "spdlog", "json", "encode", "decode", "dump",
    "spawn", "execcmd", "getsystem", "getsettingssystem", "readjson",
    "writejson", "getfile", "openfile", "readfile", "writefile", "loadstring",
    "assert", "tostring", "tonumber", "pcall", "setmetatable",
}

# 这些函数的字符串参数是**记录名/枚举名**, 不是给玩家看的文本。
# 例: StringToName("MBC-Operation-Misfire-Stage" + ...) 只是查表用的 key。
KEY_CALLS = {
    "stringtoname", "stringtocname", "stringtoenum", "toname", "tocname",
    "enumfromstring",
}
DEAD_CALLS |= KEY_CALLS


def _callee_name(toks, i):
    """从 ident 开始解析 a.b.c / a:b 形式的被调名; 返回 (name, open_paren_index)"""
    parts = []
    j = i
    if toks[j].kind != "ident":
        return None, None
    parts.append(toks[j].val)
    j += 1
    while j + 1 < len(toks) and toks[j].kind == "op" and toks[j].val in (".", ":") \
            and toks[j + 1].kind == "ident":
        parts.append(toks[j + 1].val)
        j += 2
    if j < len(toks) and toks[j].kind == "op" and toks[j].val == "(":
        return ".".join(parts), j
    return None, None


def _split_args(toks, open_idx, close_idx):
    """按顶层逗号切分参数, 返回 [(start_idx, end_idx)] (含端点, 闭区间)"""
    args, depth, start = [], 0, open_idx + 1
    for k in range(open_idx + 1, close_idx):
        tk = toks[k]
        if tk.kind == "op":
            if tk.val in ("(", "{", "["):
                depth += 1
            elif tk.val in (")", "}", "]"):
                depth -= 1
            elif tk.val == "," and depth == 0:
                args.append((start, k - 1))
                start = k + 1
    if start <= close_idx - 1:
        args.append((start, close_idx - 1))
    return args


def _arg_strings(toks, span):
    """参数若为纯字符串字面量、{字符串,...} 表, 或 string.format(...), 返回 token 下标列表。"""
    a, b = span
    if a > b:
        return []
    if b == a and toks[a].kind == "str":
        return [a]
    if toks[a].kind == "op" and toks[a].val == "{" and \
            toks[b].kind == "op" and toks[b].val == "}":
        out = []
        for k in range(a + 1, b):
            if toks[k].kind == "str":
                out.append(k)
        return out
    # Util.Notify(string.format("...", ...)) 这类包装：只展开 string.format 的第一个参数
    name, oi = _callee_name(toks, a)
    if name and name.split(".")[-1].lower() == "format":
        depth, close = 0, None
        for k in range(oi, b + 1):
            if toks[k].kind == "op":
                if toks[k].val in ("(", "{", "["):
                    depth += 1
                elif toks[k].val in (")", "}", "]"):
                    depth -= 1
                    if depth == 0:
                        close = k
                        break
        if close is not None:
            args = _split_args(toks, oi, close)
            if args:
                return _arg_strings(toks, args[0])
    return []


# ---------------------------------------------------------------- 调用区间
def call_spans(toks):
    """返回 [(被调名, 左括号token下标, 右括号token下标)]"""
    spans = []
    for i, tk in enumerate(toks):
        if tk.kind != "ident":
            continue
        name, oi = _callee_name(toks, i)
        if name is None:
            continue
        depth, close = 0, None
        for k in range(oi, len(toks)):
            if toks[k].kind == "op":
                if toks[k].val in ("(", "{", "["):
                    depth += 1
                elif toks[k].val in (")", "}", "]"):
                    depth -= 1
                    if depth == 0:
                        close = k
                        break
        if close is not None:
            spans.append((name, oi, close))
    return spans


def codeware_text_calls(toks, lits):
    """识别 Codeware 的 this.Text("key", "value")。

    返回 (values, key_starts):
      values     : {value_lit.start: key} —— 只有第二个参数才是译文
      key_starts : 所有作为第一个参数出现的 key 字面量, 提取时应跳过
    """
    by_start = {l.start: l for l in lits}
    values, key_starts = {}, set()
    for name, oi, close in call_spans(toks):
        if name.split(".")[-1].lower() != "text":
            continue
        args = _split_args(toks, oi, close)
        if len(args) < 2:
            continue
        a, b = args[0]
        c, d = args[1]
        key_lit = by_start.get(toks[a].start) if a == b and toks[a].kind == "str" else None
        if key_lit is not None:
            key_starts.add(key_lit.start)
        if c == d and toks[c].kind == "str":
            val_lit = by_start.get(toks[c].start)
            if val_lit is not None and val_lit.quote in ("\"", "'"):
                values[val_lit.start] = key_lit.inner if key_lit is not None else ""
    return values, key_starts


def callee_map(toks, spans):
    """返回列表: 每个 token 所处的最近一层函数调用名"""
    open_map, close_map = {}, {}
    for name, oi, ci in spans:
        open_map.setdefault(oi, name)
        close_map.setdefault(ci, oi)
    m = [None] * len(toks)
    stack = []
    for i in range(len(toks)):
        if i in open_map:
            stack.append(open_map[i])
        if stack:
            m[i] = stack[-1]
        if i in close_map and stack:
            stack.pop()
    return m


def lit_callee(m, toks, starts, pos):
    k = bisect.bisect_right(starts, pos) - 1
    if 0 <= k < len(m):
        return m[k]
    return None


def visible_text(inner):
    """ImGui 标签 "文字##内部ID" : 只把 ## 之前的部分交给译者"""
    i = inner.find("##")
    if i > 0:
        return inner[:i].rstrip()
    return inner


def _ctx(text, pos, take=28):
    seg = text[max(0, pos - take - 14):pos]
    seg = re.sub(r"\s+", " ", seg).strip()
    return seg[-take:]


# ---------------------------------------------------------------- Lua 提取
def _enclosing_call(span, spans):
    """返回包含给定调用的最内层父调用, 没有则返回 None。"""
    _name, oi, close = span
    best = None
    for other in spans:
        _, oi2, close2 = other
        if oi2 < oi and close2 > close:
            if best is None or (close2 - oi2) < (best[2] - best[1]):
                best = other
    return best


def _assigned_var(text, pos):
    """返回 pos 前最近一个赋值目标变量名, 例如 message = string.format(...)。"""
    head = text[max(0, pos - 320):pos]
    m = re.search(r"([A-Za-z_]\w*)\s*=\s*[^=;\n]*$", head)
    if m:
        return m.group(1).lower()
    # 三元表达式可能含 ==, 再按同一行最近一次赋值判断
    m = re.search(r"(?:^|\n)\s*(?:local\s+)?([A-Za-z_]\w*)\s*=\s*[^\n]*$", head)
    return m.group(1).lower() if m else ""


def _assign_target(text, pos):
    """同 _assigned_var, 但保留原始大小写 (用于驼峰命名拆分)。"""
    head = text[max(0, pos - 320):pos]
    m = re.search(r"([A-Za-z_]\w*)\s*=\s*[^=;\n]*$", head)
    if m:
        return m.group(1)
    m = re.search(r"(?:^|\n)\s*(?:local\s+)?([A-Za-z_]\w*)\s*=\s*[^\n]*$", head)
    return m.group(1) if m else ""


# 名字里出现这些词，说明这个字符串多半是给玩家看的文本。
DISPLAY_SEGMENTS = {
    "name", "displayname", "fullname", "shortname", "localizedname",
    "title", "subtitle", "header", "label", "caption", "text", "body",
    "textbody", "message", "msg", "notice", "notification", "prompt",
    "summary", "detail", "desc", "description", "tooltip", "hint", "help",
}
# 名字里出现这些词，说明它是内部状态 / 日志 / 路径，不是界面文本。
INTERNAL_SEGMENTS = {
    "issue", "error", "err", "reason", "debug", "trace", "log", "state",
    "status", "path", "file", "key", "fact", "flag", "mode", "result",
    "output", "input", "count", "index", "id", "uid", "guid", "hash",
    "url", "dir", "event", "hook", "channel",
}
_LOG_CALL_RE = re.compile(
    r"^(log|ftlog|print|printf|dbg|debug|trace|warn|error|err|"
    r"assert|fail|throw|spdlog|breakpoint)", re.I)
# 模组常自己封装日志: MBCInfo / ModWarn / DebugTrace 都是日志, 不是界面文本。
# 但 ShowInfoDialog / CreateErrorDialog 这类是界面, 靠前缀动词排掉。
_LOG_SEGMENTS = {"log", "logs", "logger", "dbg", "debug", "trace", "warn",
                 "warning", "error", "err", "info", "verbose", "critical"}
_UI_CALL_PREFIX = {"show", "display", "open", "create", "add", "push", "make",
                   "notify", "prompt", "ask", "set", "get", "build", "render",
                   "draw", "send"}


def _looks_like_log_call(callee):
    last = (callee or "").split(".")[-1]
    if not last:
        return False
    if _LOG_CALL_RE.match(last):
        return True
    segs = _name_segments(last)
    if not segs or segs[0] in _UI_CALL_PREFIX:
        return False
    return any(s in _LOG_SEGMENTS for s in segs)


def _name_segments(name):
    """把 m_lastMessage / LastMessage / MBCInfo / last_message 拆成小写词片段。"""
    if not name:
        return []
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)   # MBCInfo -> MBC_Info
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)         # lastMessage -> last_Message
    return [p for p in re.split(r"[^A-Za-z0-9]+", s.lower()) if p]


def _diag_fragment(t):
    """诊断拼接片段: "Board: enabled=" / " player=unavailable" / "current=unresolved"。

    这类字符串是 StringBuffer 里拼给日志看的, 当成句子翻出来只会污染表格。
    只用于「没有位置证据」的兜底档, 有证据的界面文本不受影响。
    """
    return bool(re.search(r"(^|[\s:(\[])[A-Za-z_][\w.]*=", t))


def text_evidence(text, pos, callee=""):
    """判断一个字面量「有证据是界面文本」的程度。

    返回 (等级, 依据):
      display  —— 赋值目标名里出现 name/title/text/desc 等词
      internal —— 私有成员 (m_/s_) 或名字里出现 issue/error/log 等词,
                  或出现在 Log/Print 类调用里
      plain    —— 没有位置证据, 只能靠「像不像句子」判断
    """
    var = _assign_target(text, pos)
    segs = _name_segments(var)
    for seg in segs:
        if seg in DISPLAY_SEGMENTS:
            return "display", seg
    if segs and (segs[0] in ("m", "s", "p") or
                 any(seg in INTERNAL_SEGMENTS for seg in segs)):
        return "internal", var
    if _looks_like_log_call(callee):
        return "internal", callee
    return "plain", ""


def _wrapper_rules(text):
    """从 local function switch(path, label, desc, key) 这类声明推导参数文本位置。"""
    out = {}
    pat = re.compile(
        r"(?:^|\n)\s*(?:local\s+)?function\s+"
        r"([A-Za-z_]\w*(?:[.:][A-Za-z_]\w*)*)\s*\(([^)]*)\)"
    )
    for m in pat.finditer(text):
        name = re.split(r"[.:]", m.group(1))[-1].lower()
        if not name or name in CALL_RULES:
            continue
        rules = []
        for idx, p in enumerate(m.group(2).split(",")):
            param = p.strip().split("=")[0].strip().lower()
            typ = WRAPPER_PARAM_TYPES.get(param)
            if typ:
                rules.append((idx, typ))
        if rules:
            out.setdefault(name, rules)
    return out


def _brace_end(text, open_pos):
    """从 '{' 的位置出发, 返回与之配对的 '}' 之后的位置。

    Lua 语言段表 (["en-us"] = { ... }) 的边界要靠它算: 只有知道段在哪里结束,
    段外的普通代码才不会被误判成「还在最后一段里」。
    """
    depth, i, n = 0, open_pos, len(text)
    while i < n:
        c = text[i]
        if c in ("\"", "'"):
            q = c
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == q:
                    break
                i += 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _extract_lua(text, loose=True, trace=False, noise=None, target_locale=""):
    toks, lits = scan_source(text, "lua")
    by_start = {l.start: l for l in lits}
    taken = {}          # start -> (类型, 上下文)
    spans = call_spans(toks)
    cmap = callee_map(toks, spans)
    starts = [t.start for t in toks]
    wrappers = _wrapper_rules(text)
    # 模组自带的「英文 → 中文」数据表: 左边是查表 key, 翻它会把表打坏
    table_keys = NOISE.bilingual_keys(text)
    # 同一个文件里按语言键塞了多套语言时 (["en-us"] = {...}, ["de-de"] = {...})，
    # 只有源语言段是「原文」，目标语言段留给 is_target_language_text 当已译处理。
    # 拉丁字母写成的德/法/西语段靠字符区分不出来，以前会被整段当成待翻文本 ——
    # QuickhackPanel 一个文件就因此多出 48 条外语长句。
    # 按语言键切段 (["en-us"] = {...} / ["zh-cn"] = {...})：只有源语言段是原文，
    # 目标语言段留给 is_target_language_text 区分「已译」与「这段里漏翻的英文」
    # (如 QuickhackPanel 的 tab)。其余语言段整段丢弃 —— 拉丁字母写成的德/法/西
    # 语靠字符区分不出来，以前会被整段当成待翻文本，一个文件就多出几十条外语。
    sections = []
    for m in NOISE._LUA_LANG_KEY_RE.finditer(text):
        code = (m.group(1) or "").lower()
        region = (m.group(2) or "").lower()
        lang = code + "-" + region if region else code
        open_pos = text.rfind("{", m.start(), m.end())
        sections.append((m.start(), _brace_end(text, open_pos) if open_pos >= 0 else m.end(), lang))
    keep_starts = None
    if sections:
        _tgt_full = (target_locale or "").lower().replace("_", "-")
        _tgt_main = _tgt_full.split("-")[0]

        def wants(lang):
            if lang.split("-")[0] == "en":
                return True
            if _tgt_full and lang == _tgt_full:
                return True
            return bool(_tgt_main and lang == _tgt_main)

        keep_starts = {start for start, _end, lang in sections if wants(lang)}
    dropped = noise if noise is not None else {}

    def drop(family):
        dropped[family] = dropped.get(family, 0) + 1

    def kept_section(pos):
        if keep_starts is None:
            return True
        for start, end, _lang in sections:
            if start <= pos < end:
                return start in keep_starts
        return True

    def take(lit, typ, ctx=""):
        if lit is None or lit.quote not in ("\"", "'"):
            return
        if lit.start in taken:
            return
        if not kept_section(lit.start):
            drop(NOISE.FAMILY_LANG)
            return
        taken[lit.start] = (typ, ctx)

    # ---- 1) 已知 UI 函数调用 (Native Settings UI / ImGui)
    for name, oi, close in spans:
        last = name.split(".")[-1].lower()
        rules = CALL_RULES.get(last)
        if not rules:
            continue
        args = _split_args(toks, oi, close)
        for argidx, typ in rules:
            if argidx >= len(args):
                continue
            for ti in _arg_strings(toks, args[argidx]):
                lit = by_start.get(toks[ti].start)
                if lit is None:
                    continue
                txt = lit.inner
                if txt.startswith("##") or txt.startswith("###"):
                    continue
                ok = (is_translatable(txt) if typ in (TYPE_DESC, TYPE_MOD)
                      else is_label_text(txt))
                if ok:
                    take(lit, typ, name)

    # ---- 1b) 本文件里自定义的包装函数: switch(path, label, desc, key)
    for name, oi, close in spans:
        last = name.split(".")[-1].lower()
        rules = wrappers.get(last)
        if not rules:
            continue
        args = _split_args(toks, oi, close)
        for argidx, typ in rules:
            if argidx >= len(args):
                continue
            for ti in _arg_strings(toks, args[argidx]):
                lit = by_start.get(toks[ti].start)
                if lit is None:
                    continue
                txt = lit.inner
                if txt.startswith("##") or txt.startswith("###"):
                    continue
                ok = (is_translatable(txt) if typ in (TYPE_DESC, TYPE_MOD)
                      else is_label_text(txt))
                if ok:
                    take(lit, typ, name)

    # ---- 2) 表字段:  label = "..." / Part.Name = "..." / ["displayName"] = "..."
    for lit in lits:
        if lit.start in taken or lit.quote not in ("\"", "'"):
            continue
        head = text[max(0, lit.start - 200):lit.start]
        m = re.search(r"""([A-Za-z_]\w*|\[\s*"[^"]*"\s*\]|\[\s*'[^']*'\s*\])\s*=\s*$""", head)
        if not m:
            continue
        key = m.group(1).strip().strip("[]").strip("\"'").lower()
        typ = SETTINGS_KEYS.get(key)
        if not typ:
            continue
        ok = (is_translatable(lit.inner) if typ in (TYPE_DESC, TYPE_MOD)
              else is_label_text(lit.inner))
        if ok:
            take(lit, typ, key + " =")

    # ---- 3) 多行选项表:  labels = { "A", "B", ... }
    for lit in lits:
        if lit.start in taken:
            continue
        head = text[max(0, lit.start - 1500):lit.start]
        m = re.search(r"""([A-Za-z_]\w*)\s*=\s*\{[^{}]*$""", head)
        if not m:
            continue
        tname = m.group(1).lower()
        if tname not in LIST_KEYS and not TEXT_TABLE_NAME_RE.search(tname):
            continue
        tail = text[lit.end:lit.end + 2].lstrip()[:1]
        if tail not in (",", "}"):
            continue
        if is_label_text(lit.inner):
            take(lit, TYPE_OPT, m.group(1) + " = {...}")

    # ---- 3b) 通知缓冲区 / message 变量 / string.format 提示文本
    for name, oi, close in spans:
        last = name.split(".")[-1].lower()
        args = _split_args(toks, oi, close)
        if last == "insert" and len(args) >= 2:
            a, b = args[0]
            if a == b and toks[a].kind == "ident" and \
                    toks[a].val.lower() in UI_BUFFER_VARS:
                for ti in _arg_strings(toks, args[1]):
                    lit = by_start.get(toks[ti].start)
                    if lit is not None and is_label_text(lit.inner):
                        take(lit, TYPE_UI, name)
        if last == "format" and args:
            parent = _enclosing_call((name, oi, close), spans)
            parent_last = parent[0].split(".")[-1].lower() if parent else ""
            assigned = _assigned_var(text, toks[oi].start)
            if parent_last in UI_NOTIFY_CALLS or assigned in UI_TEXT_VARS:
                for ti in _arg_strings(toks, args[0]):
                    lit = by_start.get(toks[ti].start)
                    if lit is not None and is_label_text(lit.inner):
                        take(lit, TYPE_UI, name)

    # message = message .. " 片段" 这类拼接提示
    for lit in lits:
        if lit.start in taken or lit.quote not in ("\"", "'"):
            continue
        if _assigned_var(text, lit.start) in UI_TEXT_VARS and is_label_text(lit.inner):
            take(lit, TYPE_UI, _ctx(text, lit.start))

    # ---- 4) 宽松兜底: 长句字面量
    if loose:
        for lit in lits:
            if lit.start in taken or lit.quote not in ("\"", "'"):
                continue
            txt = lit.inner
            if txt.startswith("##") or txt.startswith("###"):
                continue
            if not is_translatable(txt):
                continue
            words = re.findall(r"[A-Za-z]{2,}", txt)
            if not (len(words) >= 4 or (CJK_RE.search(txt) and len(txt) >= 6)):
                continue
            if CODEY_RE.search(txt) or SQL_RE.match(txt):
                continue
            # 兜底只收「句子感」的文本: 首字母大写或含中文
            # 去掉开头引号/标点后再判断, 例如 "'I already gave you everything!'"
            sentence = txt.lstrip(" \t\r\n\"'")
            if not (sentence[:1].isupper() or CJK_RE.search(txt)):
                continue
            callee = lit_callee(cmap, toks, starts, lit.start) or ""
            last = callee.split(".")[-1].lower()
            if last in DEAD_CALLS or callee.split(".")[0].lower() in DEAD_CALLS:
                continue
            if last.startswith("log") or last.startswith("print"):
                continue
            # 日志 / 私有状态里的句子默认不收, 避免把调试输出送进机翻。
            if not trace and text_evidence(text, lit.start, callee)[0] == "internal":
                continue
            if _diag_fragment(txt):
                continue
            # 双语表 key / 多语言副本 / 外语文本: 只在「长句兜底」这一档过滤
            if table_keys and txt in table_keys:
                drop(NOISE.FAMILY_TABLE)
                continue
            if not trace:
                fam = NOISE.soft_family(txt)
                if fam:
                    drop(fam)
                    continue
            take(lit, TYPE_LONG, callee)

    return [(lit.line, taken[lit.start][0], taken[lit.start][1],
             visible_text(lit.inner))
            for lit in lits if lit.start in taken]


# ---------------------------------------------------------------- REDscript / XML
def _extract_reds(text, ext, target_locale="", stats=None, loose=True,
                  trace=False, noise=None):
    toks, lits = scan_source(text, "xml" if ext == ".xml" else "reds")
    spans = call_spans(toks)
    cmap = callee_map(toks, spans)
    starts = [t.start for t in toks]
    codeware_values, codeware_keys = codeware_text_calls(toks, lits)
    out, seen = [], set()
    # 同一文件里带多套语言(EN_US / RU_RU …，或 English/German 两个语言包类)时，
    # 只有源语言那一段是「原文」；把俄语段当原文翻一遍纯属误报。
    sections = NOISE.lang_sections(text) if ext != ".xml" else []
    keep_starts = NOISE.keep_sections(sections)
    table_keys = NOISE.bilingual_keys(text)

    def kept_section(pos):
        if keep_starts is None:
            return True
        sec = NOISE.section_of(sections, pos)
        return True if sec is None else (sec in keep_starts)

    dropped = noise if noise is not None else {}

    def drop(family):
        dropped[family] = dropped.get(family, 0) + 1

    # 多语言 StrReplaceAll 表：只取目标语言对应的第三个参数(替换后的文本)，
    # 其余语言参数全部跳过，避免同一句文本被重复提取十几遍。
    locale_entries = _locale_replace_entries(text, toks, spans, lits)
    chosen_locale = {}
    if locale_entries:
        picked = []
        if target_locale:
            picked = [e for e in locale_entries if _locale_matches(e["locale"], target_locale)]
        if not picked and not target_locale:
            picked = [e for e in locale_entries
                      if _locale_key(e["locale"]) in ("en", "en-us", "en-gb")]
        chosen_locale = {e["new_start"]: e["locale"] for e in picked}
        skip_locale_starts = set()
        for e in locale_entries:
            skip_locale_starts.add(e["old_start"])
            skip_locale_starts.add(e["new_start"])
        if stats is not None:
            stats["locale_pairs"] = stats.get("locale_pairs", 0) + len(locale_entries)
            stats["locale_pairs_used"] = stats.get("locale_pairs_used", 0) + len(picked)
    else:
        skip_locale_starts = set()
    # ---- 标识符守卫
    #
    # 同一个字符串可能既是「给玩家看的文本」，又是引擎标识符：
    #   @runtimeProperty("ModSettings.mod", "Auto Drive Enhanced")
    #   ModSettings.GetCategories(n"Auto Drive Enhanced")
    # 只翻前者、不翻后者，两处就脱钩了——查找永远落空，设置项联动静默失效，
    # 而界面上看起来一切正常（这正是最坏的一类错误）。
    # 工具无法判断哪边是主语义，所以保守地整条不翻，并把这笔账记进报告，
    # 让用户自己决定要不要手动放行。
    ident_texts = set()
    for lit in lits:
        if lit.prefix and lit.prefix in _REDS_PREFIX:
            ident_texts.add(lit.inner)
            continue
        if (lit_callee(cmap, toks, starts, lit.start) or ""
                ).split(".")[-1].lower() in KEY_CALLS:
            ident_texts.add(lit.inner)

    rp_re = re.compile(r"@runtimeProperty\(\s*\"([^\"]*)\"\s*,\s*\"((?:[^\"\\]|\\.)*)\"\s*\)")
    rp_types = {}
    if ext != ".xml":
        for m in rp_re.finditer(text):
            key = m.group(1).split(".")[-1]
            typ = SETTINGS_KEYS.get(key.lower())
            if not typ:
                continue
            line = text.count("\n", 0, m.start(2)) + 1
            rp_types[(line, m.group(2))] = (typ, "@runtimeProperty(" + m.group(1) + ")")
    for lit in lits:
        if lit.prefix and lit.prefix in _REDS_PREFIX:
            # r"..." ResRef / n"..." CName / t"..." TweakDBID：都是引擎标识符，
            # 而且几乎总是作为参数直接传给别的函数（Add(rows, t"Items.X", ...)）。
            # 玩家在界面上看到的是 TweakDB 查表后的显示名，不是这些串本身，
            # 所以它们不进表格。但也不能悄悄消失——记进「记录名」，扫描报告里
            # 能看到「已过滤 N 条（记录名 X）」，用户才知道这里确实生效了。
            drop(NOISE.FAMILY_KEY)
            continue
        if lit.prefix:                       # 未知前缀：同样不当文本
            continue
        if lit.quote not in ("\"", ""):
            continue
        if lit.start in chosen_locale:
            if not lit.inner.strip() or not is_translatable(lit.inner):
                continue
            typ = TYPE_UI
            ctx = "StrReplaceAll[%s]" % chosen_locale[lit.start]
            if lit.start not in seen:
                seen.add(lit.start)
                out.append((lit.line, typ, ctx, lit.inner))
            continue
        if lit.start in skip_locale_starts:  # 同一替换表的其它语言
            continue
        if table_keys and lit.inner in table_keys:
            drop(NOISE.FAMILY_TABLE)          # 双语表的查表 key, 翻它会把表打坏
            continue
        if lit.inner in ident_texts:
            # 这个串在别处被当成 CName / TweakDBID / ResRef / StringToName 用了。
            # 翻掉文本位就会让标识符位与它失配，所以整条放弃。
            drop(NOISE.FAMILY_IDENT)
            continue
        if is_loc_key(lit.inner):
            # 本地化查表键：它的译文在 mod 归档的 onscreens 里，属于资源层。
            # 在源码层翻它只会造出一堆假的「待翻行」。
            drop(NOISE.FAMILY_LOCKEY)
            continue
        if lit.start in codeware_keys:        # this.Text 的第一个参数是 key, 不翻译
            continue
        if (lit_callee(cmap, toks, starts, lit.start) or ""
                ).split(".")[-1].lower() in KEY_CALLS:
            continue                          # StringToName("...") 之类的记录名/枚举名
        if not kept_section(lit.start):       # 同一文件里的其它语言段
            drop(NOISE.FAMILY_LANG)
            continue
        key = (lit.line, lit.inner)
        if lit.start in codeware_values:      # this.Text("key", "value") -> 只收 value
            if not lit.inner.strip():
                continue
            typ = TYPE_UI
            ck = codeware_values[lit.start]
            ctx = 'this.Text("%s")' % ck if ck else "this.Text(...)"
        elif key in rp_types:
            typ, ctx = rp_types[key]
            if not is_translatable(lit.inner):
                continue
        else:
            # 没有 @runtimeProperty / this.Text / StrReplaceAll 这类机构性证据时，
            # 按「赋值目标名 + 调用者」判断这个字面量到底是不是界面文本。
            # 以前这里无条件当界面文本收下，逻辑重的模组会混进几千条内部状态字符串。
            if not is_translatable(lit.inner):
                continue
            call = lit_callee(cmap, toks, starts, lit.start) or ""
            tier, why = text_evidence(text, lit.start, call)
            if tier == "display":
                typ = SETTINGS_KEYS.get(why, TYPE_UI)
            elif tier == "internal":
                if not trace:
                    continue
                typ = TYPE_LONG
            else:
                if not loose:
                    continue
                if _diag_fragment(lit.inner):
                    continue
                if not trace:
                    fam = NOISE.soft_family(lit.inner)
                    if fam:
                        drop(fam)
                        continue
                typ = TYPE_LONG
            ctx = call or _ctx(text, lit.start)
        if lit.start in seen:
            continue
        seen.add(lit.start)
        out.append((lit.line, typ, ctx, lit.inner))
    return out


# ---------------------------------------------------------------- 对外接口
def lang_of(path):
    """返回 scan_source 需要的语言名: lua / xml / reds。

    注意不能返回带点的扩展名, 否则 Lua 注释与长字符串不会被正确跳过,
    会导致 apply_many 在含注释/字符串的大文件里匹配不到部分译文。
    """
    ext = os.path.splitext(path)[1].lower()
    return "lua" if ext == ".lua" else ("xml" if ext == ".xml" else "reds")


def extract_from_file(path, target_locale="", loose=True, trace=False):
    """返回 [(行号, 类型, 上下文, 原文), ...]"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            text = f.read()
    except Exception:
        return []
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".lua":
            return _extract_lua(text, loose=loose, trace=trace)
        return _extract_reds(text, ext, target_locale=target_locale,
                             loose=loose, trace=trace)
    except Exception:
        return []


def iter_source_files(root, include_lua=True, include_locale_lua=False):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext == ".lua":
                if not include_lua:
                    continue
                if not include_locale_lua and is_locale_file(fn):
                    continue
                yield os.path.join(dirpath, fn)
            elif ext in (".reds", ".xml"):
                if not include_locale_lua and is_locale_file(fn):
                    continue
                yield os.path.join(dirpath, fn)


def _count_locale_json(root):
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath).lower() in ("locales", "locale", "localization", "i18n", "lang"):
            n += sum(1 for f in filenames if f.lower().endswith(".json"))
    return n


def extract_root_ex(root, include_lua=True, loose=True, include_locale_lua=False,
                    target_locale="", exclude_rels=None, trace=False,
                    keep_target=False):
    """返回 (data, meta)。

    target_locale: 当前目标语言代码。设置后, 已经是该目标语言的文本会被跳过,
                   不会再次进入「原文」列。
    exclude_rels:  要整体跳过的相对路径集合(用于排除当前目标语言文件,
                   以及已经由语言包层处理过的文件)。
    loose:         是否收集「没有位置证据但像句子」的长句(长句兜底)。
    trace:         是否连内部状态 / 日志字符串一起收集(默认关闭)。
    keep_target:   被跳过的目标语言文本是否另存到 meta["already"]。
                   翻完再提取时这些行本来会整片消失, 用户既看不到也改不了;
                   打开后它们以「已译」身份回到表格里。
    """
    data = {}
    meta = {"files": 0, "lua_files": 0, "reds_files": 0, "types": {},
            "locale_json": _count_locale_json(root),
            "locale_lua": _count_locale_lua(root), "skipped_target": 0,
            "locale_pairs": 0, "locale_pairs_used": 0, "noise": {}}
    excluded = {str(x).replace("\\", "/") for x in (exclude_rels or ())}
    for path in iter_source_files(root, include_lua=include_lua,
                                  include_locale_lua=include_locale_lua):
        rel = os.path.relpath(path, root).replace("\\", "/")
        if rel in excluded:
            continue
        try:
            if os.path.getsize(path) > MAX_BYTES:
                meta["skipped_big"] = meta.get("skipped_big", 0) + 1
                continue
        except OSError:
            continue
        items = []
        noise = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
                text = f.read()
            ext = os.path.splitext(path)[1].lower()
            if ext == ".lua":
                items = _extract_lua(text, loose=loose, trace=trace, noise=noise,
                                    target_locale=target_locale)
                meta["lua_files"] += 1
            else:
                reds_stats = {}
                items = _extract_reds(text, ext, target_locale=target_locale,
                                      stats=reds_stats, loose=loose,
                                      trace=trace, noise=noise)
                meta["reds_files"] += 1
                for k in ("locale_pairs", "locale_pairs_used"):
                    meta[k] += reds_stats.get(k, 0)
        except Exception:
            continue
        for fam in NOISE.ALL_FAMILIES:
            n = int(noise.get(fam, 0) or 0)
            if n:
                meta["noise"][fam] = meta["noise"].get(fam, 0) + n
        if target_locale:
            # StrReplaceAll 多语言表中主动选中的目标语言条目需要保留，
            # 即使它本身已经是中文；否则用户无法校对/修改模组已有译文。
            kept, already = [], []
            for it in items:
                if (is_target_locale_replacement(it[2], target_locale)
                        or not is_target_language_text(it[3], target_locale)):
                    kept.append(it)
                else:
                    already.append(it)
            meta["skipped_target"] += len(already)
            if keep_target and already:
                meta.setdefault("already", {})[rel] = already
            items = kept
        if items:
            data[rel] = items
            meta["files"] += 1
            for it in items:
                meta["types"][it[1]] = meta["types"].get(it[1], 0) + 1
    return data, meta


def _count_locale_lua(root):
    """统计被跳过的语言包文件数 (.lua / .reds)"""
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        n += sum(1 for f in filenames
                 if f.lower().endswith((".lua", ".reds")) and is_locale_file(f))
    return n


def extract_root(root, include_lua=True, loose=True, include_locale_lua=False,
                 target_locale="", exclude_rels=None, trace=False):
    return extract_root_ex(root, include_lua=include_lua, loose=loose,
                           include_locale_lua=include_locale_lua,
                           target_locale=target_locale,
                           exclude_rels=exclude_rels, trace=trace)[0]


# ---------------------------------------------------------------- 回填
_ESCAPE_KEEP = set('ntr"\'\\abfv0')


def escape_for(text, quote):
    """把译文转义成目标语言字符串内容"""
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if nxt and nxt in _ESCAPE_KEEP:
                out.append(ch + nxt)
                i += 2
                continue
            out.append("\\\\")
            i += 1
            continue
        if ch == quote:
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def apply_translation(path, line_no, original, trans):
    """把文件中 (行号, 原文) 对应的字符串替换为译文; 返回是否成功"""
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
    except Exception:
        return False
    lang = lang_of(path)
    try:
        _, lits = scan_source(text, lang)
    except Exception:
        return False
    cand = [l for l in lits
            if l.quote in ("\"", "'")
            and (l.inner == original
                 or l.inner.startswith(original + "##"))]
    if not cand:
        return False
    pick = None
    for l in cand:
        if l.line == line_no:
            pick = l
            break
    if pick is None:
        pick = cand[0]
    suffix = pick.inner[len(original):]          # 保留 "##id" 这类隐藏后缀
    new_text = (text[:pick.start + 1] + escape_for(trans, pick.quote)
                + escape_for(suffix, pick.quote) + text[pick.end - 1:])
    # 语法自检: 重新扫描后字面量数量与引号必须一致
    try:
        _, lits2 = scan_source(new_text, lang)
        if len(lits2) < len(lits):
            return False
    except Exception:
        return False
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
    except Exception:
        return False
    return True


def apply_many_ex(path, pairs):
    """一次读改写: pairs = [(行号, 原文, 译文)]。

    返回 (成功条数, 失败明细)。失败明细为 [(pairs索引, 行号, 原文, 译文, 原因)]。
    比逐条 apply_translation 快得多(文件只读写一次), 用于回填大批量译文。
    """
    pairs = list(pairs)
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
    except Exception:
        return 0, [(i, p[0], p[1], p[2], "读取文件失败")
                   for i, p in enumerate(pairs)]
    lang = lang_of(path)
    try:
        _, lits = scan_source(text, lang)
    except Exception:
        return 0, [(i, p[0], p[1], p[2], "文件解析失败")
                   for i, p in enumerate(pairs)]
    by_key, by_text, by_line = {}, {}, {}
    for lit in lits:
        if lit.quote in ("\"", "'"):
            vis = visible_text(lit.inner)
            by_key.setdefault((lit.line, vis), []).append(lit)
            by_text.setdefault(vis, []).append(lit)
            by_line.setdefault(lit.line, []).append(lit)
    repls, failures, used = [], [], set()

    def pick(cands):
        while cands:
            lit = cands.pop(0)
            if lit.start not in used:
                used.add(lit.start)
                return lit
        return None

    for idx, (line, orig, trans) in enumerate(pairs):
        try:
            line_no = int(line)
        except Exception:
            line_no = -1
        lit = pick(by_key.get((line_no, orig), []))
        if lit is None:
            # 退回 1: 按原文匹配(目标文件排版与源文件不同)
            lit = pick(by_text.get(orig, []))
        if lit is None:
            # 退回 2: 按行号匹配(按顺序找该行尚未使用的字符串)
            lit = pick(by_line.get(line_no, []))
        if lit is None:
            failures.append((idx, line, orig, trans, "未找到对应原文"))
            continue
        repls.append((lit, orig, trans))
    if not repls:
        return 0, failures
    for lit, orig, trans in sorted(repls, key=lambda x: -x[0].start):
        suffix = lit.inner[len(orig):] if lit.inner.startswith(orig) else ""
        new = escape_for(trans + suffix, lit.quote)
        text = text[:lit.start + 1] + new + text[lit.end - 1:]
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    except Exception:
        all_fail = [(i, p[0], p[1], p[2], "写入文件失败")
                    for i, p in enumerate(pairs)]
        return 0, all_fail
    return len(repls), failures


def apply_many(path, pairs):
    """兼容旧调用: 返回成功条数。"""
    n, _failed = apply_many_ex(path, pairs)
    return n


def backup_file(path):
    import shutil
    bak = path + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    return bak
