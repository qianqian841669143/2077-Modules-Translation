# -*- coding: utf-8 -*-
"""text_noise.py —— 把「源码里像文本、其实不该翻」的字符串挡在提取层之外。

提取器只能看字面量长什么样，看不到玩家到底会不会在屏幕上读到它。于是三类
东西会混进表格：

  资源转储  图集清单 "kb_a:64:64:0|kb_b:64:64:0|..."、路径 + 计数
  记录名    Items.xxx / Foo::Bar;StringString / StringToName 之外的 TweakDB 记录
  技术串    SQL 片段、纯格式化串、key=value 调试 dump
  ---- 以上在任何档位都不是给玩家看的，直接判死（hard）。
  外文表    [en_us][pl_pl] 前缀、韩/俄/阿拉伯文、波兰语等带特征变音符号的行。
  ---- 这类只在「长句兜底」这一档过滤；显式界面位置（label= / this.Text）
       上的同形字符串仍然保留，因为确实有模组把非英语标签写在控件上。

同一文件里出现多套语言（JVF_SubText_EN_US / _RU_RU …，或 English / German
两个 ModLocalizationPackage）时，只有源语言那一段值得翻，见 lang_sections()。

每个判定都返回一个中文家族名，汇总进扫描报告，用户能看到
「已过滤 812 条（资源转储 64 / 记录名 165 / 外文表 583）」，而不是无声消失。
"""

import re

FAMILY_ASSET = "资源转储"
FAMILY_KEY = "记录名"
FAMILY_IDENT = "标识符"
FAMILY_LOCKEY = "本地化键"
FAMILY_TECH = "技术串"
FAMILY_LANG = "外文表"
FAMILY_TABLE = "双语表"

HARD_FAMILIES = (FAMILY_ASSET, FAMILY_KEY, FAMILY_TECH)
ALL_FAMILIES = (HARD_FAMILIES + (FAMILY_IDENT, FAMILY_LOCKEY,
                                 FAMILY_LANG, FAMILY_TABLE))

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# ---------------- 硬噪音 ----------------
# 图集条目 name:w:h:flag
_ASSET_TRIPLET = re.compile(r"[A-Za-z0-9_\-]+:\d{1,5}:\d{1,5}:\d(?![\w:])")
_PIPE_KV = re.compile(r"^[A-Za-z0-9_\-.]+:\w")


def _pipe_list(s):
    """g:ghxst_intro_01|vr:8|g:ghxst_intro_02 —— 全是 key:value 的竖线清单"""
    if s.count("|") < 2 or re.search(r"\s", s):
        return False
    segs = s.split("|")
    return sum(1 for seg in segs if _PIPE_KV.match(seg)) >= 2


# "widgets/notifications/notification_assets (154)" 路径 + 计数
_PATH_COUNT = re.compile(r"^[\w.\-]+(?:[/\\][\w.\-]+)+ \(\d+\)$")
# Foo.Bar::Baz;StringString  REDscript/Lua 桥接函数签名
_TYPESIG = re.compile(r"^[A-Za-z_][\w.]*(?:::[A-Za-z_]\w*)+(?:;[A-Za-z0-9_]*)?$")
# TweakDB 记录 / 存档字段
_TWEAKDB = re.compile(
    r"^(?:Items|TweakDB|gamedata|Game[A-Z]\w*|Vehicle|Character|Attachments|"
    r"Suffix|Quality|Perk|Stat|Quest|Audio|LocKey|Engine)\.\w")
# SQL 片段。必须写全关键字，否则 "Select Category" 这种真界面文本会被误杀。
_SQL = re.compile(
    r"^\s*(?:SELECT\s+[\w\s,.*()]+?\s+FROM\b|INSERT\s+INTO\b|"
    r"UPDATE\s+\w+\s+SET\b|DELETE\s+FROM\b|CREATE\s+(?:TABLE|INDEX)\b|"
    r"ALTER\s+TABLE\b|DROP\s+TABLE\b|"
    r"ORDER\s+BY\s+[\w.,\s]+?(?:ASC|DESC)?)\s*$", re.I)
# 去掉格式占位符后一个字母都不剩: "%d / %s"、"/"、"€$"
_FMT_STRIP = re.compile(r"%\d*(?:\.\d+)?[sdifgeExXo%]|\{[^{}]*\}|\\[nrt\"'\\]|\$[A-Za-z_]\w*")
# REDscript 的 \( ... ) 插值: 里面是代码, 不是文案
_INTERP_OPEN = "\\("
# 任意文字系统的字母(含中日韩/谚文), 用来区分「纯符号」和「外文」
_ANY_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
# key=value 调试 dump: "dist=%.1f busy=%.1f", "error|reason=player missing|..."
_KV_RE = re.compile(r"\b[A-Za-z_]\w*=")


def _strip_interp(s):
    r"""抹掉 REDscript 的 ``\( ... )`` 插值(括号配平)。

    ``s"\(count) \(GetLocalizedTextByKey(n"X.galleryParts"))   ..."`` 这种
    插值字符串, 去掉代码后只剩空格和斜杠 —— 一个词都没有, 本来就不是文案。
    不抹掉的话 ``this.m_page`` 里的字母会让它伪装成正常文本。
    """
    if _INTERP_OPEN not in s:
        return s
    out = []
    i, n = 0, len(s)
    depth = 0
    while i < n:
        if depth == 0 and s.startswith(_INTERP_OPEN, i):
            depth = 1
            i += 2
            continue
        ch = s[i]
        if depth:
            if ch == "\\":
                i += 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _has_letters(s):
    return bool(_ANY_LETTER.search(_strip_interp(_FMT_STRIP.sub("", s))))


def _kv_dump(s):
    if "<" in s or len(s) > 160:
        return False
    return len(_KV_RE.findall(s)) >= 2


def hard_family(t):
    """返回硬噪音家族名；空串表示「不是硬噪音」。"""
    s = (t or "").strip()
    if not s or CJK_RE.search(s):
        return ""
    if _pipe_list(s) or _ASSET_TRIPLET.search(s):
        return FAMILY_ASSET
    if _PATH_COUNT.match(s):
        return FAMILY_ASSET
    if _TYPESIG.match(s) or _TWEAKDB.match(s):
        return FAMILY_KEY
    if _SQL.match(s):
        return FAMILY_TECH
    if not _has_letters(s):
        return FAMILY_TECH
    if _kv_dump(s):
        return FAMILY_TECH
    return ""


# ---------------- 软噪音(只在长句兜底档过滤) ----------------
# [en_us][pl_pl]Żałosne. —— 同一句的多语言副本
_LANGTAG = re.compile(r"^(?:\[[A-Za-z]{2}(?:[-_]?[A-Za-z]{0,4})?\]){2,}")
# 非拉丁文字(中日韩由 CJK_RE 先放行)
_NONLATIN = re.compile(r"[\u0370-\u03ff\u0400-\u04ff\u0590-\u05ff\u0600-\u06ff"
                       r"\u0900-\u097f\u0e00-\u0e7f\u3040-\u30ff\uac00-\ud7af]")
# 有辨识度的变音符号: 波兰/捷克/匈牙利/土耳其/德语。单个重音的人名
# (Álvarez / José) 不会被判掉，要求出现两个。
_DIST_LATIN = re.compile(r"[żźćńółęąśčřšžďťňůěőűığşäöüß¿¡]")


def soft_family(t):
    """返回软噪音家族名；空串表示「不算噪音」。"""
    s = (t or "").strip()
    if not s or CJK_RE.search(s):
        return ""
    if _LANGTAG.match(s):
        return FAMILY_LANG
    if _NONLATIN.search(s):
        return FAMILY_LANG
    if len(_DIST_LATIN.findall(s)) >= 2:
        return FAMILY_LANG
    return ""


def family_of(t):
    """(级别, 家族名)，级别为 "hard" / "soft" / ""。"""
    fam = hard_family(t)
    if fam:
        return "hard", fam
    fam = soft_family(t)
    if fam:
        return "soft", fam
    return "", ""


# ---------------- 一文件多语言 ----------------
LANG_NAMES = {
    "english": "en", "german": "de", "korean": "kr", "polish": "pl",
    "french": "fr", "spanish": "es", "italian": "it", "russian": "ru",
    "japanese": "jp", "portuguese": "pt", "turkish": "tr", "czech": "cz",
    "hungarian": "hu", "ukrainian": "ua", "chinese": "zh", "dutch": "nl",
    "thai": "th", "vietnamese": "vi", "indonesian": "id", "romanian": "ro",
    "greek": "el", "swedish": "sv", "norwegian": "no", "danish": "da",
    "finnish": "fi", "hebrew": "he", "arabic": "ar",
    "chinesesimplified": "cn", "chinesetraditional": "tw",
    "brazilianportuguese": "br", "spanishlatam": "mx",
}
LANG_CODES = {
    "en", "us", "gb", "de", "kr", "ko", "jp", "ja", "pl", "fr", "es", "mx",
    "ar", "it", "ru", "pt", "br", "cz", "cs", "hu", "tr", "ua", "uk", "zh",
    "cn", "tw", "nl", "th", "vi", "id", "ro", "gr", "el", "sv", "no", "da",
    "fi", "hi", "he", "sa",
}
# func JVF_SubText_EN_US( / func Text_PL( / function Text_de(
_FUNC_LANG_RE = re.compile(
    r"\bfunc(?:tion)?\s+[A-Za-z_]\w*?_([A-Za-z]{2})(?:_([A-Za-z]{2}))?\s*\(", re.I)
# public class German extends ModLocalizationPackage
_CLASS_LANG_RE = re.compile(
    r"\bclass\s+([A-Za-z_]\w*)\s+extends\s+ModLocalizationPackage\b", re.I)
# ["en-us"] = { ... } —— Lua 里按语言键组织的多语言表。
# QuickhackPanel 这类模组把 20 种语言塞在同一个 texts 表里, 只有 en 段是原文;
# 拉丁字母的德语/法语/西语段无法靠字符区分, 只能靠语言键认出来。
_LUA_LANG_KEY_RE = re.compile(
    r"""\[\s*["']([A-Za-z]{2})(?:[-_]([A-Za-z]{2,4}))?["']\s*\]\s*=\s*\{""")


def _norm_lang(code):
    c = (code or "").strip().lower()
    if c in LANG_NAMES:
        return LANG_NAMES[c]
    # 只认真正的语言码: 否则 func Foo_ui() / Foo_api() 会被当成「另一个语言」
    return c if c in LANG_CODES else ""


def lang_sections(text):
    """找出文件里的多语言分段 [(语言, 起始偏移), ...]。

    只有在**两种以上语言**同时出现时才返回；否则返回空列表，表示这个文件
    是普通单语言源码，交给原有规则处理。
    """
    hits = []
    for m in _FUNC_LANG_RE.finditer(text):
        tag = (m.group(1) or "").lower()
        if m.group(2):
            tag = tag + "_" + m.group(2).lower()
        lang = _norm_lang(tag.split("_")[0])
        if len(lang) != 2:
            continue
        hits.append((m.start(), lang))
    for m in _CLASS_LANG_RE.finditer(text):
        lang = LANG_NAMES.get(m.group(1).strip().lower(), "")
        if not lang:
            continue
        hits.append((m.start(), lang))
    for m in _LUA_LANG_KEY_RE.finditer(text):
        lang = _norm_lang(m.group(1))
        if lang:
            hits.append((m.start(), lang))
    if len({h[1] for h in hits}) < 2:
        return []
    hits.sort()
    return hits


def keep_sections(sections, prefer=("en",)):
    """返回应当保留的分段起点偏移集合。

    有多语言时只留源语言(默认英语)；没有英语段(纯外语包)就一段都不留，
    因为把一个完整外语包当"待翻译原文"只会又生成一批误报。
    """
    if not sections:
        return None
    langs = [lang for _pos, lang in sections]
    for want in prefer:
        if want in langs:
            return {pos for pos, lang in sections if lang == want}
    return set()


def section_of(sections, pos):
    """pos 落在哪个分段上；返回该分段的起始偏移(用于和 keep_sections 比对)。"""
    lo, hi, ans = 0, len(sections) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if sections[mid][0] <= pos:
            ans = sections[mid][0]
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


# ---------------- 双语数据表 ----------------
# ["Wall Wood"]="墙木"  —— 模组自带译文表: 左边是查表 key，翻它会把表打坏
_TABLE_PAIR_RE = re.compile(
    r"""\[\s*(?P<q>["'])(?P<k>(?:[^"'\\]|\\.)*?)(?P=q)\s*\]\s*=\s*"""
    r"""(?P<q2>["'])(?P<v>(?:[^"'\\]|\\.)*?)(?P=q2)""")
BILINGUAL_MIN_PAIRS = 30


def bilingual_keys(text):
    """识别「英→中」双语数据表，返回其 key 集合(应为空集合)。"""
    if not text or text.count("]=") + text.count("] =") < BILINGUAL_MIN_PAIRS:
        return set()
    keys, translated = set(), 0
    for m in _TABLE_PAIR_RE.finditer(text):
        keys.add(m.group("k"))
        if CJK_RE.search(m.group("v")):
            translated += 1
    if translated < BILINGUAL_MIN_PAIRS:
        return set()
    return keys
