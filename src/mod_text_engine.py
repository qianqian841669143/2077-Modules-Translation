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
import functools
import os
import re

try:
    from . import text_noise as NOISE
except ImportError:                     # 单文件入口(pyinstaller / 直接运行)
    import text_noise as NOISE

# ---------------------------------------------------------------- 类型常量
# 提取规则版本：凡是改了「认哪些字面量」的规则就 +1。
# mod_deploy 用它判断「已部署且源未变」还能不能整块跳过 —— 换了规则不失效的话,
# 新规则认出来的行永远进不了表（v2.46.0 的成对表、v2.48.0 的注册器别名都吃这个亏）。
#   1: 初版
#   2: v2.46.0 成对文本表 / 事件名守卫 / 官方弱匹配
#   3: v2.48.0 注册器别名 / 菜单标题 return "…", opts
RULES_VERSION = 3

TYPE_NAME = "显示名称"
TYPE_DESC = "描述"
TYPE_CAT = "分类"
TYPE_MOD = "模组名"
TYPE_OPT = "选项文本"
TYPE_UI = "界面文本"
TYPE_LONG = "长句文本"          # 宽松兜底收进来的长句(可能含日志/调试文本)

SETTING_TYPES = {TYPE_NAME, TYPE_DESC, TYPE_CAT, TYPE_MOD, TYPE_OPT}
UI_TYPES = {TYPE_UI, TYPE_LONG}

SRC_EXTS = {".reds", ".xml", ".lua", ".yaml", ".yml"}
MAX_BYTES = 8 * 1024 * 1024        # 单个源文件上限, 超过则跳过(避免卡死)
SKIP_DIRS = {"redmod_backup", ".git", "__pycache__", "node_modules",
             "_internal", "dist", "build"}

# ---------------------------------------------------------------- 文本判定
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LOCALE_RE = re.compile(r"^[a-z]{2}([_-][A-Za-z]{2,4})?$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
URL_RE = re.compile(r"^(https?|ftp)://", re.I)
HEX_RE = re.compile(r"^#?[0-9A-Fa-f]{6,8}$")
# _extract_lua 里「逐字面量」的两个热路径正则: 一个 1 MB 的 Base.lua 有 2 万个
# 字面量, 每个都要试一次。必须预编译 —— 用字符串 pattern 调 re.search 会走
# re._cache(只有 512 项), 批量线程一多就被别的 pattern 冲掉, 变成每次重新编译,
# 6 秒的提取能拖成十几分钟并占死 GIL(v2.42.4 实测)。
ASSIGN_TAIL_RE = re.compile(
    r"""([A-Za-z_]\w*|\[\s*"[^"]*"\s*\]|\[\s*'[^']*'\s*\])\s*=\s*$""")
LIST_HEAD_RE = re.compile(r"""([A-Za-z_]\w*)\s*=\s*\{[^{}]*$""")
# 同上, 这两个在「每个字面量/每个调用」的路径上各调一次(_assigned_var / _assign_target)。
_ASSIGN_VAR_RE = re.compile(r"([A-Za-z_]\w*)\s*=\s*[^=;\n]*$")
_ASSIGN_LINE_RE = re.compile(
    r"(?:^|\n)\s*(?:local\s+)?([A-Za-z_]\w*)\s*=\s*[^\n]*$")
# 标识符拆词的三个(原来在 _name_segments 里每次重编译)。
_UPPER_RUN_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUND_RE = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")
_WS_RE = re.compile(r"\s")
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


# 「带空格的查表键」: `Gameplay-Items-Item Type-Prt_Fragment`。
# 游戏的 onscreens 键经常在中间夹一个空格（物品类型名、槽位名这类），
# `is_loc_key()` 要求整串无空格，于是漏判；`getText` / `@runtimeProperty` 那条
# 排除又只认调用位置，管不到裸 `return "…"`。
# 描准「紧贴的连字符」（A-B，不是 A - B 那种人写的标签）+ 至少两处 + 无句子标点。
_SPACED_TIGHT_DASH_RE = re.compile(r"[A-Za-z0-9]-[A-Za-z0-9]")
_SPACED_LOOSE_DASH_RE = re.compile(r"\s-\s")
_SPACED_KEY_CHARS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _'\-]*$")
_SPACED_KEY_PUNCT_RE = re.compile(r"[.!?;:,]")


def is_spaced_loc_key(s):
    """带空格的本地化查表键（`Gameplay-Items-Item Type-Prt_Fragment`）。

    只在「没有任何位置证据、靠像不像句子兜底」的那一档用。判据：
    有空格、至少两处紧贴的连字符、没有 `A - B` 这种散开的连字符（那是人写的
    标签）、没有句子标点、词数不多、纯 ASCII。
    """
    t = (s or "").strip()
    if not t or len(t) < 10 or len(t) > 120:
        return False
    if " " not in t or CJK_RE.search(t):
        return False
    if t.count("-") < 2:
        return False
    if not _SPACED_TIGHT_DASH_RE.search(t):
        return False
    if _SPACED_LOOSE_DASH_RE.search(t):     # "Part - Engine - Turbo": 人写的标签
        return False
    if _SPACED_KEY_PUNCT_RE.search(t):
        return False
    if len(t.split()) > 8:
        return False
    return bool(_SPACED_KEY_CHARS_RE.match(t))


# 两段式查表键：`GR-UiHomes` / `MBC-Wire-CrimeRcr18` / `SS.panelX`。大写缩写前缀 +
# 驼峰段、没有空格、长度短。`is_loc_key()` 要求三段以上，两段式的（模组自己造前缀，
# 如 GR-）正好漏掉；`@runtimeProperty` 与 `getText` 那两条又只认调用位置，
# 于是在 `this.T("GR-MenuBoss")` / `text("GR-UiHomes", "兜底")` 这种自造查表函数里
# 就被当成正文收进表了（Gang Retaliation 一个 mod 57 条）。
_SHORT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,6}(?:-[A-Za-z0-9]+)+$")
# 自造查表函数的常见名字，配合「键形状」一起用，所以 `text("某句真文案")` 不受影响。
_LOOKUP_EXTRA = {"t", "tr", "text", "translate", "loc", "locate", "l10n"}


def is_short_loc_key(s):
    """`GR-UiHomes` 这种两段式查表键（大写缩写前缀 + 驼峰段，无空格）。"""
    t = (s or "").strip()
    if not t or len(t) > 48 or " " in t or CJK_RE.search(t):
        return False
    if not _SHORT_KEY_RE.match(t):
        return False
    return any(seg[:1].isupper() for seg in t.split("-")[1:])


def is_lookup_call(callee):
    """这个被调名像不像「拿键去查文本」：`this.T(…)` / `text(键, 兜底)` / `getText(…)`。"""
    last = re.split(r"[.:]", str(callee or ""))[-1].strip().lower()
    if not last:
        return False
    return last in LOC_CALLS or last in _LOOKUP_EXTRA


# @runtimeProperty("ModSettings.displayName", "NearbyLoot-Enabled") —— 第二个参数
# 是 onscreens 的**查表键**，不是文案：Codeware 拿它去 GetLocalizedText，查不到
# 才回退显示键本身。真文案在 mod 归档的 localization 里。
# is_loc_key 要求「三段以上」，而 NearbyLoot-Enabled / NearbyLoot-Sound 只有两段，
# 于是被当成界面文本翻掉——写回 .reds 就把查表键改成了中文，引用当场断掉，
# 表面上还正常（回退显示的就是那句中文），是最容易被忽略的一类损坏。
_PROP_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:[-._][A-Za-z0-9_]+)+$")
# 段内还有大写拐点：NearbyLoot / GeneralCategory。普通连字符词组（Auto-Drive）没有。
_CAMEL_SEG_RE = re.compile(r"^[A-Za-z]+[A-Z]")


def is_prop_loc_key(s):
    """@runtimeProperty 第二个参数是不是查表键。

    比 is_loc_key 松（允许两段），但只在「已经确定这个位置就是 key 字段」
    时才用，所以不会误伤普通文案。
    """
    t = (s or "").strip()
    if not t or len(t) > 64 or re.search(r"\s", t):
        return False
    if not _PROP_KEY_RE.match(t):
        return False
    segs = [x for x in re.split(r"[-._]", t) if x]
    if any(_CAMEL_SEG_RE.match(x) for x in segs):
        return True
    return len(segs) >= 3


# 全大写写法, 但本身就是普通英文单词: 该翻成中文才看得懂。
# 其余的 2-4 个字母全大写串当代号 (ENC / CET / RCF / MCM / HUD / VDB), 不送翻译 ——
# 代号没有上下文, 模型只能猜它展开成什么:
#   CET (Cyber Engine Tweaks) 被猜成 Central Intelligence Agency -> 模组列表每行
#   前面挂上「中央情报局」; ENC (Enemies of NC) 被猜成 Neural Connection ->
#   设置面板的页签变成「神经连接」。猜错比不翻难看得多。
#
# 只卡 4 个字母以内: RESET / CLEAR / QUICK / SLIDE / MELEE / ERROR 这些全大写词
# 都是真文案, 长度 ≥5 的一律照翻。白名单里的词来自实测: 这些全大写词在模组里
# 当按钮标签用过 (SELL 13 次 / NONE 13 次 / CLEAR 16 次 …), 漏掉一个就等于
# 界面上留一个英文单词。
_PLAIN_CAPS_WORDS = {
    # 是/否、开关、范围
    "OK", "ON", "OFF", "YES", "NO", "NONE", "ALL", "ANY", "MAX", "MIN",
    "TOP", "LOW", "FULL", "HALF", "UP", "DOWN",
    # 常用按钮
    "ADD", "DEL", "SET", "GET", "PUT", "USE", "NEW", "OLD", "BUY", "SELL",
    "OPEN", "LOAD", "SAVE", "EXIT", "QUIT", "EDIT", "VIEW", "MENU", "ITEM",
    "SORT", "MAKE", "TAKE", "GIVE", "DROP", "MISC",
    # 游戏内动作与状态
    "RUN", "JUMP", "AIM", "FIRE", "HIT", "DIE", "PAY", "STOP", "WAIT",
    "ROLL", "FACE", "SWAP", "LEAN", "YAW", "ARMS", "LEGS", "BODY", "GUN",
    "DEAD", "GONE", "LOST", "WARN", "WEAK", "ZERO", "INFO", "DOCS", "LOGS",
    "GIGS", "RED", "GOLD", "COLD", "HIGH", "WIKI",
    # 杂项常见词
    "BUG", "LOG", "KEY", "MAP", "BAR", "CAR", "GAS", "BAG", "WAR", "DAY",
    "NOW", "END", "BIG", "RATE", "TIER", "RANK", "SEED", "PLOT", "DUMP",
    # 实测在全盘模组里出现过的: 身体部位表、复数标签、连接词
    "AND", "OR", "MODS", "TIME", "PASS", "TABS", "BASE", "MIND", "TEST",
    "FAIL", "HEAD", "NECK", "BACK", "EARS", "FEET", "TOES", "TV",
}


def is_acronym(s):
    """这个字符串是不是纯代号 (2-4 个字符的全大写字母, 无空格)。

    ``ENC`` / ``CET`` / ``RCF`` / ``MCM`` 算代号; ``RESET`` / ``IMPORT`` 这种
    5 个字母以上的全大写写法算普通词; ``OFF`` / ``SELL`` 在白名单里, 照常翻。
    """
    t = (s or "").strip()
    if not (2 <= len(t) <= 4):
        return False
    if not t.isupper() or not t.isalpha():
        return False
    return t not in _PLAIN_CAPS_WORDS


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
    if looks_like_code_token(t):
        return False
    words = re.findall(r"[A-Za-z]{2,}", t)
    return len(words) >= 2


# 当前目标语言 -> 该语言文本常用的 Unicode 字符范围。
# 用于提取时跳过已经翻好的目标语言文本, 避免把中文/日文等再次收进原文列。
# 英语/西班牙语/德语/法语等拉丁字母语言与英文源码无法只靠字符区分,
# 它们仍通过「跳过目标语言文件」来处理。
# 键一律小写, 查询前用 _locale_key() 归一化 —— 以前这里写 "zh-CN", 而
# is_target_language_text 是精确查表, 调用方传 "zh-cn" 就静默返回 False,
# 结果是整份中文文件重新变成「待翻候选」(同一份 SenSen Core: zh-cn 389 条,
# zh-CN 2 条)。
_TARGET_SCRIPT_RE = {
    "zh-cn": re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"),
    "zh-tw": re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"),
    "ja-jp": re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]"),
    "ko-kr": re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]"),
    "ru-ru": re.compile(r"[\u0400-\u04ff]"),
    "ar-sa": re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]"),
}


def is_target_language_text(s, target_locale):
    """判断一段文字是否已经是当前目标语言。

    只对能通过字符范围可靠辨认的语言生效(中文/日文/韩文/俄文/阿拉伯文),
    避免把英文源码当成目标语言误跳过。

    ``target_locale`` 大小写/连接符不敏感: ``zh-CN`` / ``zh-cn`` / ``zh_CN``
    等价。以前是精确查表, 传小写就静默返回 False, 中文行会被当成待翻候选。
    """
    if not s or not target_locale:
        return False
    key = _locale_key(target_locale)
    rx = _TARGET_SCRIPT_RE.get(key)
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
    # 全大写缩写: HUD / MCM / OFF。代号 (ENC / CET) 不收, 见 is_acronym。
    if t.isupper() and t.isalpha() and 2 <= len(t) <= 8:
        return not is_acronym(t)
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


def _skip_interp(text, j):
    r"""从 ``\(`` 之后开始, 跳到配对的 ``)`` 之后; 返回 -1 表示没配对。

    插值里可以再写 ``n"..."`` / ``ToString(")")``, 所以内部字符串要整段跳过,
    里面的括号不算数。
    """
    n = len(text)
    depth = 1
    while j < n:
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch in "\"'":
            k = j + 1
            while k < n:
                if text[k] == "\\":
                    k += 2
                    continue
                if text[k] == ch:
                    break
                k += 1
            if k >= n:
                return -1
            j = k + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return -1


# ---------------------------------------------------------------- YAML 扫描
# tweakdb 的 yaml 只有一种「可回填」的写法: `key: '...'` 或 `key: "..."`,
# 一行一个标量。块标量 (`key: |` / `key: >`) 跨行、偏移不可预测, 整段跳过;
# 裸标量 (`key: murphys_web_name`) 是查表键或数字, 也不当文案。
_YAML_KV_RE = re.compile(
    r"^[ \t]*(?:-[ \t]+)?([A-Za-z_][\w.\-]*)[ \t]*:[ \t]*(.*)$")
_YAML_RECORD_RE = re.compile(r"^([A-Za-z_][\w.\-]*)[ \t]*:[ \t]*$")


def _yaml_scalar(val, quote):
    """从 ``val``（以引号开头）里切出标量内容, 返回 (结束引号下标, 内容)。

    内容按「人看到的」给: 单引号里的 ``''`` 还原成 ``'``，双引号里的 ``\\"``
    还原成 ``"``。写回时再由 :func:`escape_for` 转回去 —— 这样界面上的原文/
    译文和游戏里显示的文字一致, 比对、记忆、撤销都不会被转义符干扰。
    """
    buf = []
    k, n = 1, len(val)
    while k < n:
        ch = val[k]
        if quote == "'":
            if ch == "'":
                if k + 1 < n and val[k + 1] == "'":
                    buf.append("'")
                    k += 2
                    continue
                return k, "".join(buf)
            buf.append(ch)
            k += 1
            continue
        if ch == "\\" and k + 1 < n:
            nxt = val[k + 1]
            buf.append(nxt if nxt in "\"\\" else "\\" + nxt)
            k += 2
            continue
        if ch == '"':
            return k, "".join(buf)
        buf.append(ch)
        k += 1
    return -1, ""


def _scan_yaml(text):
    """YAML: 每个行内引号标量算一个字面量, 记下它在文本里的绝对偏移。"""
    lits = []
    pos = 0
    for line_no, raw in enumerate(text.splitlines(True), 1):
        body = raw.rstrip("\r\n")
        if body.lstrip()[:1] != "#":
            m = _YAML_KV_RE.match(body)
            if m is not None:
                vstart = m.start(2)
                quote = body[vstart:vstart + 1]
                if quote in ("'", '"'):
                    end, inner = _yaml_scalar(body[vstart:], quote)
                    if end > 0:
                        start = pos + vstart
                        lits.append(Lit(start, start + end + 1, quote, inner,
                                        line_no, "yaml"))
        pos += len(raw)
    return [], lits


def scan_source(text, lang):
    """返回 (tokens, literals)。lang: 'lua' | 'reds' | 'xml' | 'yaml'"""
    if lang == "yaml":
        return _scan_yaml(text)
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
            prefix = ""
            if not lua:
                k = i
                while k > 0 and (text[k - 1].isalnum() or text[k - 1] == "_"):
                    k -= 1
                run = text[k:i]
                if run in ("r", "n", "t", "s"):
                    prefix = run
            # s"..." 是插值字符串: \( ... ) 里面是代码, 其中的 " 不是字符串结束。
            # 不认插值的话, 一行会被切成几块碎片, 抽出来的「原文」其实是半句
            # 代码 (例如 ")) \(this.m_page + 1)/\(pages)"), 写回时还会把
            # 插值的右括号一起吃掉, 整行语法崩掉。
            interp = (prefix == "s" and c == '"')
            j = i + 1
            while j < n:
                ch = text[j]
                if ch == "\\":
                    if interp and j + 1 < n and text[j + 1] == "(":
                        k2 = _skip_interp(text, j + 2)
                        if k2 > 0:
                            j = k2
                            continue
                    j += 2
                    continue
                if ch == c:
                    break
                j += 1
            if j >= n:
                break
            inner = text[i + 1:j]
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
        #
        # 只认 ASCII: str.isalpha()/isdigit() 对中日韩字符和全角数字也返回
        # True, 但 IDENT_TOK_RE / NUM_TOK_RE 是纯 ASCII 正则, match 会得到
        # None -> AttributeError, 整份文件被 extract_from_file / extract_root_ex
        # 的 except 吞掉, 表现成「这个模组没有文本」。中文标识符(被误改过的
        # reds)以前就是这么让整个文件从表里消失的。
        if c.isascii() and (c.isalpha() or c == "_"):
            m = IDENT_TOK_RE.match(text, i)
            if m is not None:
                toks.append(Tok("ident", m.group(0), i, m.end(), line))
                i = m.end()
                continue
        if c.isascii() and c.isdigit():
            m = NUM_TOK_RE.match(text, i)
            if m is not None:
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
    "fullname": TYPE_NAME, "localizedname": TYPE_NAME, "displaytext": TYPE_UI,
    # `shortname` 也照收: 它确实是缩写徽标 (MCM 的 provider 写着 shortName =
    # "CET" / "RCF" / "MS"), 但「是不是代号」由值本身判断 (is_acronym), 不靠
    # 字段名一刀切 —— 有些模组在里面写的是一句真文案, 按字段名排除就整句漏翻。
    # v2.51.2 之前这里不收, 那正是「遮住真文本」的那一刀。
    "shortname": TYPE_NAME,
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
    # ------- 模组自己的「动作表」(菜单项) 注册器
    # NCPD First Response: Settings.RegisterAction(id, label, fn, desc, opts)
    # 这类注册器的标签常常只有两三个词 ("Request backup" / "Police stations"),
    # 兜底规则要四个词才收, 于是它们永远进不了表、界面上一直是英文。
    "registeraction": [(1, TYPE_NAME), (3, TYPE_DESC)],
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

# 形参里出现这些词，说明这个函数是「带标签的回调执行器」而不是设置项包装：
# local function safeCall(label, callback, quiet) —— label 只进日志。
_CALLBACK_PARAMS = {"callback", "cb", "fn", "func", "handler", "closure"}

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
    if "=" not in head:
        return ""
    m = _ASSIGN_VAR_RE.search(head)
    if m:
        return m.group(1).lower()
    # 三元表达式可能含 ==, 再按同一行最近一次赋值判断
    m = _ASSIGN_LINE_RE.search(head)
    return m.group(1).lower() if m else ""


def _assign_target(text, pos):
    """同 _assigned_var, 但保留原始大小写 (用于驼峰命名拆分)。"""
    head = text[max(0, pos - 320):pos]
    if "=" not in head:
        return ""
    m = _ASSIGN_VAR_RE.search(head)
    if m:
        return m.group(1)
    m = _ASSIGN_LINE_RE.search(head)
    return m.group(1) if m else ""


# 名字里出现这些词，说明这个字符串多半是给玩家看的文本。
#
# `shortname` 不在里面，但 `shortName` 仍能命中 —— `_name_segments` 会把它拆成
# short + name，`name` 本身在表里。判断「是不是代号」交给值 (is_acronym)，
# 不做字段名层面的排除：那会把 `shortName = "Auto Drive"` 这类真文案一起遮住。
DISPLAY_SEGMENTS = {
    "name", "displayname", "fullname", "localizedname",
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


@functools.lru_cache(maxsize=8192)
def _name_segments(name):
    """把 m_lastMessage / LastMessage / MBCInfo / last_message 拆成小写词片段。

    结果缓存: 同一个 callee 名在一个文件里会重复出现几百次(TweakDB:GetFlat
    这种), 每次重跑三遍 re.sub 纯属浪费。v2.42.4 之前这里也是「字符串 pattern
    调 re.sub」, py-spy 抓到的卡死栈正好落在它的 _compile 上。
    """
    if not name:
        return ()
    s = _UPPER_RUN_RE.sub(r"\1_\2", name)      # MBCInfo -> MBC_Info
    s = _CAMEL_BOUND_RE.sub(r"\1_\2", s)       # lastMessage -> last_Message
    return tuple(p for p in _NON_ALNUM_RE.split(s.lower()) if p)


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
        params = [p.strip().split("=")[0].strip().lower()
                  for p in m.group(2).split(",")]
        # 「标签 + 回调」执行器：safeCall(label, callback) 是 pcall 包装，
        # label 只出现在出错时的调试信息里。把它当显示名，就会把
        # "fppCamera:ResetPitch" 这类事件名翻成中文写回代码。
        if any(p in _CALLBACK_PARAMS for p in params):
            continue
        rules = []
        for idx, param in enumerate(params):
            typ = WRAPPER_PARAM_TYPES.get(param)
            if typ:
                rules.append((idx, typ))
        if rules:
            out.setdefault(name, rules)
    return out


def _callee_aliases(text):
    """local A = Settings.RegisterAction —— 别名按被指向的名字套同一套规则。

    CET 模组几乎都这么写: 把注册器取个短名字, 然后整篇 A(id, label, fn, desc)。
    不认别名的话, 这些菜单项就只剩「长句兜底」一条路 (要四个词), 两三词的
    动作名永远进不了表 —— NCPD First Response 的菜单因此一半中文一半英文。

    只认**注册器**(名字里带 register)。别名不一定是「显示文本的函数」:
    MCM 的 local t = ctx.text 是查表函数, t("settings.section.general") 的实参
    是查表键, 按显示文本收进来就会把键翻掉 (P13 那一类静默损坏)。
    """
    out = {}
    pat = re.compile(
        r"(?:^|[\r\n;])\s*local\s+([A-Za-z_]\w*)\s*=\s*"
        r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*(?=[\r\n;]|$)"
    )
    for m in pat.finditer(text):
        name, target = m.group(1), m.group(2)
        last = target.split(".")[-1].lower()
        if "register" not in last:
            continue
        rules = CALL_RULES.get(last)
        if not rules or name.lower() in CALL_RULES:
            continue
        # 别名本身也可能就是同名局部函数, 交给 _wrapper_rules 决定
        out.setdefault(name.lower(), rules)
    return out


# ---------------------------------------------------------------- 位置证据 (P21)
#
# 有些字面量的「位置」本身就在说：这是名字，不是文案。它们形态上是完全正常
# 的单词，looks_like_code_fragment 一条都挡不住，但翻掉的后果是静默的 ——
# 方法名不认、插槽名查不到、能力清单解析不出来，而文件照样能编译通过。
#
# 实测（Immersive Third Person 2.0.0，全部是这条路径漏出去的）：
#   api.lua:650             call(sys, "Renew")           -> call(sys, "刷新")
#   api.lua:1044            host.cname("None")           -> host.cname("无")
#   init.lua:4551           slotWorldPosition(c,"Head")  -> slotWorldPosition(c,"头部")
#   tpp_camera_api.reds:850 CapabilityRoster() 的 "a.read,b.read,…" 整块被翻
#
# 前三个是「位置证据」，最后一个是「形状证据」（点分逗号清单），两套互补。

# 这些被调函数的指定参数位是名字。键 = 小写的最后一段被调名。
_NAME_ARG_CALLS = {
    # CET 远程调用：call(obj, "MethodName", ...) 的第二个参数是 Redscript 方法名。
    # 方法名不存在 -> host.call 返回 ok=false -> 功能走失败分支，界面上看不出来。
    "call": (1,),
    # CName / 枚举名
    "cname": (0,), "stringtoname": (0,), "stringtocname": (0,),
    "toname": (0,), "tocname": (0,), "stringtoenum": (0,), "enumfromstring": (0,),
    # 引擎插槽 / 骨骼名：传中文等于传一个不存在的插槽，被 pcall 兜住 -> 静默失效
    "getslottransform": (0,), "getslotworldtransform": (0,),
    "getslotposition": (0,), "getboneworldposition": (0,),
    "getbonelocalposition": (0,), "findbone": (0,), "attachslot": (0,),
    # 比较 / 切分：右边那个是名字或分隔符
    "equals": (1,), "strsplit": (1,),
}

# 被调名里带这些「词」时，它的字符串参数是插槽/骨骼/fact/记录名一类的名字。
# 用来接住「名字经由本地包装函数中转」的情况：ITP 的
# slotWorldPosition(slotComp, "Head") 光看参数位判不出来，但函数名在说。
#
# 必须按**词**匹配而不是子串：`artifact` 里含 `fact`、`Factory` 里含 `fact`，
# 按子串匹配会把 ShowArtifact("…") 的文案也挡掉。
_NAME_CALLEE_SEGS = {
    "slot", "slots", "bone", "bones", "skeleton", "attach", "attachment",
    "attachments", "persist", "persisted", "fact", "facts", "recordid",
    "recordname", "entityid", "rigidbody",
}


def name_callee_hint(last):
    """被调名在暗示「我这一串参数是名字」。last 保留原始大小写。"""
    low = (last or "").lower()
    if not low:
        return False
    if "tweakdb" in low:            # makeTweakDBID / GetTweakDBID 拆不出词
        return True
    return any(seg in _NAME_CALLEE_SEGS for seg in _name_segments(last))


# 查表键的取用函数：它们的实参是 key，译文在资源层归档里。
# 源码层翻掉这个 key 等于把引用打断，而界面回退显示的就是那句中文，看着正常。
LOC_CALLS = {"gettext", "gettextorkey", "getlocalizedtextbykey",
             "getlocalkey", "getlocalkeyorfallback", "getlocalizedtext"}


def _is_name_literal(s):
    """「名字」形状的字面量：无空格、纯 ASCII 的单词或点分名。

    中文直接排除：名字翻了确实坏了，但已经是中文的串说明坏在别处（或者就是
    历史译文），这里不该再拦 —— 否则用户在界面上连改都改不了。
    """
    t = (s or "").strip()
    if not t or CJK_RE.search(t):
        return False
    if _WS_RE.search(t):
        return False
    if t[:1] in ("/", "#", "$"):
        return False
    return bool(IDENT_RE.match(t))


def looks_like_protocol_list(t):
    '''点分逗号清单: "meta.read,perspective.read,events.push,"。'''
    return NOISE.is_protocol_list(t)


# 无空格、却带代码符号的字面量: 调用链 / 事件签名 / 枚举路径。
# 例如 safeCall("fppCamera:ResetPitch", fn) 的标签、
# Logger("handle:GameSessionEvent") 的签名、
# "TakeOverControlSystem:EnablePlayerTPPRepresenation(false)"。
# 它们靠 is_translatable 的「至少两个英文词」是拦不住的 —— camelCase 一个
# 标识符就能拆出两段 —— 所以按形状单独挡（P21 位置证据的补充）。
_CODE_TOKEN_RE = re.compile(r"[:()=<>]")
# 调色板里的色值: `Lemon = "FFFACD"`，形状上是「大写字母」，认成标签就会把
# 颜色表整片翻掉（Garage Manager 的 ColorSwatches 实测命中 14 条）。
_HEX_COLOR_RE = re.compile(r"^#?(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")


def looks_like_code_token(s):
    t = (s or "").strip()
    return bool(t) and not _WS_RE.search(t) and bool(_CODE_TOKEN_RE.search(t))


def _entry_value_ok(s, is_desc=False):
    """成对文本表里，一个值像不像界面文本。

    无空格的单个 token 只接受「像界面词」的（`Off` / `Height`），
    名字形状的一律不收：`glove` / `night_out` / `seat_front_left` /
    `NPCStatPreset.AllDOTImmunity_inline1` / `handle:GameSessionEvent`。
    """
    t = (s or "").strip()
    if not t or looks_like_code_token(t):
        return False
    if _HEX_COLOR_RE.match(t):
        return False
    if not _WS_RE.search(t):
        if ("." in t or "_" in t or t.islower()
                or any(ch.isdigit() for ch in t)):
            return False
    return is_translatable(t) if is_desc else is_label_text(t)


def _pair_table_entries(toks):
    """按形状找「成对文本表」，返回 [(字面量 start, 条目名, 是否第二项)]。

    `NAME = { A = { "标签", "描述" }, B = { "一行" }, … }` 这种表：整张表的
    **直接条目**都是 1~2 个裸字符串、且至少 3 条。单个 `{ "a", "b" }` 多半是
    坐标/键值对，不收；条目里出现 `id = …` 之类的字段也不算（那是有结构的数据）。

    只从 token 流判断，不猜语义：值像不像界面词交给 `_entry_value_ok`。
    """
    n = len(toks)
    out = []

    def op(k, v):
        return k < n and toks[k].kind == "op" and toks[k].val == v

    def entry(k):
        """k 指向条目名，返回 (名字, [字面量 start], 下一个 token, 是否带 {} 值)。

        两种形状都算条目：`key = "一行"` 与 `key = { "标签" [, "描述"] }`。
        ITP 的 TEXT 表就是混着写的（`keyNote = "…"` 夹在一堆 `{}` 里）。
        """
        if k >= n or toks[k].kind != "ident":
            return None, None, k, False
        name = toks[k].val
        if not op(k + 1, "="):
            return None, None, k, False
        if k + 2 < n and toks[k + 2].kind == "str":
            return name, [toks[k + 2].start], k + 3, False
        if not op(k + 2, "{"):
            return None, None, k, False
        j = k + 3
        starts = []
        while j < n and toks[j].kind == "str":
            starts.append(toks[j].start)
            j += 1
            if op(j, ","):
                j += 1
                if op(j, "}"):
                    break
                if len(starts) >= 2:
                    return None, None, k, False
                continue
            break
        if not starts or len(starts) > 2 or not op(j, "}"):
            return None, None, k, False
        return name, starts, j + 1, True

    i = 0
    while i < n - 2:
        if toks[i].kind == "ident" and op(i + 1, "=") and op(i + 2, "{"):
            j = i + 3
            entries = []
            braced = 0
            while j < n and not op(j, "}"):
                name, starts, nxt, is_braced = entry(j)
                if name is None:
                    entries = []
                    break
                entries.append((name, starts))
                braced += 1 if is_braced else 0
                j = nxt
                if op(j, ","):
                    j += 1
            # 记录表（`desc = { district = "WESTBROOK", … }` /
            # `GameUI.Event = { Camera = "Camera", … }`）也是「一条一名一串」，
            # 但它们是数据不是界面文案：要求带 {} 的条目过半，只把
            # `key = { "标签", "描述" }` 这种当成文本表。
            if (j < n and op(j, "}") and len(entries) >= 3
                    and braced * 2 >= len(entries)):
                for name, starts in entries:
                    for idx, st in enumerate(starts):
                        out.append((st, name, idx == 1))
                i = j
        i += 1
    return out


def name_position_starts(toks, spans, by_start):
    """返回「处于名字位置」的字面量起始偏移集合。

    只认**直接**作为实参的那一个字面量：中间夹了变量或表达式就不管了。
    宁可漏判（多一条待翻行）也不误伤（功能静默失效）。
    """
    out = set()
    for name, oi, close in spans:
        raw_last = name.split(".")[-1]
        last = raw_last.lower()
        if not last:
            continue
        idxs = _NAME_ARG_CALLS.get(last)
        hinted = name_callee_hint(raw_last)
        if not idxs and not hinted:
            continue
        args = _split_args(toks, oi, close)
        for ai, (a, b) in enumerate(args):
            if a != b or toks[a].kind != "str":
                continue
            if idxs and ai not in idxs:
                continue
            lit = by_start.get(toks[a].start)
            if lit is None or not _is_name_literal(lit.inner):
                continue
            out.add(lit.start)
    return out


# getLabel = function() return "…" end —— 直接返回字面量的显示名取用函数。
# 只认「函数体就是一句 return 字面量」；返回变量（return self.label）的不算，
# 那种显示名在别处赋值，走的是「表字段」那条路。
_GETTER_RE = re.compile(
    r"\bget(label|actionlabel|name|title|description|desc|tooltip)\s*=\s*"
    r"function\s*\([^)]*\)\s*return\s*$", re.I)


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
    # 名字位置（call 的方法名 / 插槽名 / CName）与查表键位置的字面量, 一律不进表
    name_starts = name_position_starts(toks, spans, by_start)
    wrappers = _wrapper_rules(text)
    aliases = _callee_aliases(text)
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
        if lit.start in name_starts:
            # 位置证据: 这是方法名/插槽名/CName/查表键, 不是文案。
            # 写进去功能会静默失效, 而界面上一点异常都看不出来。
            drop(NOISE.FAMILY_POSITION)
            return
        if NOISE.is_data_row(lit.inner):
            # a|b|c|20 这种 id 表: 翻掉任一段, 语音池/fact 名就失配
            drop(NOISE.FAMILY_DATA)
            return
        if NOISE.is_protocol_list(lit.inner):
            drop(NOISE.FAMILY_POSITION)
            return
        # getText("UI-Foo-label") / getTextOrKey(...) 的实参是查表键: 译文在
        # 资源层归档里, 源码层翻掉它等于把引用打断, 而界面回退显示的就是那句
        # 中文 —— 表面上一切正常, 是最容易被忽略的一类损坏 (P13)。
        if (lit_callee(cmap, toks, starts, lit.start) or ""
                ).split(".")[-1].lower() in LOC_CALLS:
            drop(NOISE.FAMILY_LOCKEY)
            return
        # 自造查表函数 (`this.T("GR-MenuBoss")` / `text("GR-UiHomes", "兜底")`)：
        # 实参是键形状时同样不翻。只对「键形状」生效，`text("某句真文案")` 不受影响。
        if is_short_loc_key(lit.inner) and is_lookup_call(
                lit_callee(cmap, toks, starts, lit.start)):
            drop(NOISE.FAMILY_LOCKEY)
            return
        if not kept_section(lit.start):
            drop(NOISE.FAMILY_LANG)
            return
        taken[lit.start] = (typ, ctx)

    # ---- 1) 已知 UI 函数调用 (Native Settings UI / ImGui)
    for name, oi, close in spans:
        last = name.split(".")[-1].lower()
        rules = CALL_RULES.get(last) or aliases.get(last)
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
        # 剪枝: 正则要求 `... =` 收尾, 窗口里没有 = 就不可能命中。
        if "=" not in head:
            continue
        m = ASSIGN_TAIL_RE.search(head)
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

    # ---- 2b) getLabel / getDescription 直接返回字面量
    #
    #   getLabel = function() return "[For Dev] Restricted Gig Settings" end,
    #
    # 这类显示名一条判据都沾不上：前面不是 `label =`（中间隔了 function）、
    # 也没有任何调用包装；长句兜底又要求首字母大写，而它首字符是 `[`，
    # 于是永远进不了表 —— 用户翻完整个模组，面板上那一项还是英文。
    # 返回值就是显示名本身，取出来即可（不涉及任何查表键）。
    for lit in lits:
        if lit.start in taken or lit.quote not in ("\"", "'"):
            continue
        head = text[max(0, lit.start - 200):lit.start]
        m = _GETTER_RE.search(head)
        if not m:
            continue
        typ = (TYPE_DESC if m.group(1).lower() in ("description", "desc", "tooltip")
               else TYPE_NAME)
        ok = (is_translatable(lit.inner) if typ is TYPE_DESC
              else is_label_text(lit.inner))
        if ok:
            take(lit, typ, "get" + m.group(1) + "=function() return")

    # ---- 3) 多行选项表:  labels = { "A", "B", ... }
    for lit in lits:
        if lit.start in taken:
            continue
        head = text[max(0, lit.start - 1500):lit.start]
        # 剪枝: 正则要求 `name = {`, 窗口里没有 { 或 = 就直接跳过。
        # 两万个字面量里绝大多数走这条路, 由 C 层 memchr 判掉, 不再进正则引擎。
        if "{" not in head or "=" not in head:
            continue
        m = LIST_HEAD_RE.search(head)
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

    # ---- 2c) 成对文本表:  key = { "标签", "描述" } / key = { "一行" }
    #
    # Immersive Third Person 的设置界面整张表都是这种形状 (60 多条)：左值是
    # 短标签、右值是描述。单看每一对，都不是「已知 UI 调用」、表名也不在
    # LIST_KEYS / TEXT_TABLE_NAME_RE 里，于是短标签永远进不了行；只有描述因为
    # 「像长句」被兜底收走 —— 界面上看到的就是「一对里只译了后半」。
    # 判据全在形状上（见 `_pair_table_entries`），值再按 `_entry_value_ok` 过一遍。
    for st, key, is_desc in _pair_table_entries(toks):
        lit = by_start.get(st)
        if lit is None or lit.start in taken:
            continue
        if table_keys and lit.inner in table_keys:
            drop(NOISE.FAMILY_TABLE)
            continue
        if not _entry_value_ok(lit.inner, is_desc):
            continue
        take(lit, TYPE_DESC if is_desc else TYPE_NAME, key + " ={...}")

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

    # ---- 3c) 菜单标题: return "标题", opts
    # CET 的菜单铺法: 函数返回 (标题, 选项表) 两元组, 标题由模组自己画在窗口顶栏。
    # 这类标题常常只有两三个词 ("Request backup" / "Police stations"), 兜底收不到,
    # 结果菜单项已经是中文, 顶栏还是英文。
    for m in re.finditer(
            r"\breturn\s+([\"'])((?:(?!\1)[^\r\n]){1,120})\1\s*,\s*opts\b", text):
        lit = by_start.get(m.start(1))
        if lit is None or not is_label_text(lit.inner):
            continue
        take(lit, TYPE_NAME, "return …opts")

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
            # 带空格的查表键 (`Gameplay-Items-Item Type-Prt_Fragment`) 不是文案：
            # 它没有位置证据，只能靠「像不像句子」走到这里，而 key 里的连字符让它
            # 看着像一句短语。翻掉它 = 把 onscreens 的引用改成中文，游戏里显示
            # 原始 key 或空白（Immersive Cyberware 的眼镜槽位名就是这个）。
            if is_spaced_loc_key(txt):
                drop(NOISE.FAMILY_LOCKEY)
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
            # 两段式查表键 (`GR-HudMore`) 在没有查表调用、或调用本身是查表函数时不收。
            # 这里要求「键形状」，所以 `this.Text("MBC-Key", "真文案")` 的 value 不受影响。
            if is_short_loc_key(txt) and (not callee or is_lookup_call(callee)):
                drop(NOISE.FAMILY_LOCKEY)
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
    by_start = {l.start: l for l in lits}
    name_starts = name_position_starts(toks, spans, by_start)
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
    rp_keys = set()          # 第二个参数是查表键的 (行, 值)
    if ext != ".xml":
        for m in rp_re.finditer(text):
            key = m.group(1).split(".")[-1]
            typ = SETTINGS_KEYS.get(key.lower())
            if not typ:
                continue
            line = text.count("\n", 0, m.start(2)) + 1
            rp_types[(line, m.group(2))] = (typ, "@runtimeProperty(" + m.group(1) + ")")
            # "ModSettings.mod", "Nearby Auto Loot" 这种带空格的是真名字，
            # 保持可翻；无空格的连字符串才是查表键。
            if is_prop_loc_key(m.group(2)):
                rp_keys.add((line, m.group(2)))
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
        if (lit.line, lit.inner) in rp_keys:
            # @runtimeProperty("ModSettings.displayName", "NearbyLoot-Enabled")：
            # 两段式查表键，is_loc_key 的「三段」门槛认不出来，这里靠位置证据补上。
            drop(NOISE.FAMILY_LOCKEY)
            continue
        if lit.start in codeware_keys:        # this.Text 的第一个参数是 key, 不翻译
            continue
        if lit.start in name_starts:
            # 位置证据 (P21): 方法名 / 插槽名 / CName 实参, 翻了功能静默失效
            drop(NOISE.FAMILY_POSITION)
            continue
        if NOISE.is_data_row(lit.inner):
            drop(NOISE.FAMILY_DATA)           # 竖线 id 表, 翻任一段就失配
            continue
        if NOISE.is_protocol_list(lit.inner):
            drop(NOISE.FAMILY_POSITION)       # 点分逗号清单 (能力/协议)
            continue
        _callee_last = (lit_callee(cmap, toks, starts, lit.start) or ""
                        ).split(".")[-1].lower()
        if _callee_last in LOC_CALLS:
            # getText("UI-Foo-label") 的实参是查表键, 译文在资源层归档里。
            # 以前这里靠「像不像句子」判, 于是 key 被当成文案翻掉 -> 查表断供。
            drop(NOISE.FAMILY_LOCKEY)
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
                # 带空格的查表键不是文案：`return "Gameplay-Items-Item Type-Prt_Fragment"`
                # 这种裸返回没有调用位置证据，只能落到这一档；它是 onscreens 的键，
                # 翻掉以后游戏里显示原始 key（Immersive Cyberware 的眼镜槽位名）。
                if is_spaced_loc_key(lit.inner):
                    drop(NOISE.FAMILY_LOCKEY)
                    continue
                # 两段式查表键同理：`this.T("GR-MenuBoss")` 的实参、或裸返回的键。
                if is_short_loc_key(lit.inner) and (not call or is_lookup_call(call)):
                    drop(NOISE.FAMILY_LOCKEY)
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


# ---------------------------------------------------------------- tweakdb YAML
# `r6/tweaks/**/*.yaml` 是 tweakdb 的明文记录。玩家真正读到的物品/快速破解
# 说明有两套写法:
#   1. `displayName: murphys_web_name` / `localizedDescription: murphys_web_desc`
#      —— 存的是 onscreens 的**查表键**, 译文在归档的 localization json 里;
#   2. `localizedDescription: 'Uploads a priming daemon …'`
#      —— 内联文案, 直接显示。游戏优先用这一条。
# 第 2 种以前完全没进表: 工具只认 .reds/.lua/.xml, yaml 连打开都不会。于是
# 「墨菲之网」的标题是中文（归档层那条翻了）、正文还是英文（内联这条没人管）。
YAML_TEXT_KEYS = {
    "localizedname": TYPE_NAME,
    "localizedtitle": TYPE_NAME,
    "localizeddescription": TYPE_DESC,
}

# 全小写 + 下划线/点 = 查表键 (`murphys_web_desc`)，不是文案。
_YAML_KEYLIKE_RE = re.compile(r"^[a-z0-9_.\-]+$")


def _yaml_is_key_like(value):
    return bool(_YAML_KEYLIKE_RE.match(value)) and ("_" in value or "." in value)


def _extract_yaml(text):
    """只收 localized* 字段里的行内文案, 类型按字段名给。"""
    out = []
    record = ""
    for line_no, raw in enumerate(text.splitlines(), 1):
        body = raw.rstrip("\r\n")
        if not body.strip() or body.lstrip().startswith("#"):
            continue
        m = _YAML_RECORD_RE.match(body)
        if m is not None:
            record = m.group(1)      # 顶格 `Name:` 才是记录名, 缩进的属于字段
            continue
        m = _YAML_KV_RE.match(body)
        if m is None:
            continue
        typ = YAML_TEXT_KEYS.get(m.group(1).lower())
        if not typ:
            continue
        val = m.group(2)
        quote = val[:1]
        if quote not in ("'", '"'):
            continue                 # 查表键 / 空值 / 数字 / 块标量
        end, inner = _yaml_scalar(val, quote)
        if end <= 0:
            continue
        text_ = inner.strip()
        if len(text_) < 3 or _yaml_is_key_like(text_):
            continue
        out.append((line_no, typ, record or _ctx(body, m.start(2)), inner))
    return out


# ---------------------------------------------------------------- 对外接口
def lang_of(path):
    """返回 scan_source 需要的语言名: lua / xml / reds / yaml。

    注意不能返回带点的扩展名, 否则 Lua 注释与长字符串不会被正确跳过,
    会导致 apply_many 在含注释/字符串的大文件里匹配不到部分译文。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".lua":
        return "lua"
    if ext == ".xml":
        return "xml"
    if ext in (".yaml", ".yml"):
        return "yaml"
    return "reds"


def extract_from_file(path, target_locale="", loose=True, trace=False, errors=None):
    """返回 [(行号, 类型, 上下文, 原文), ...]

    errors: 传入 list 时, 解析失败会追加 (path, 原因)。以前这里静默
    ``return []``, 一份文件解析不了就等于「这个模组没有文本」, 用户和
    日志都看不到任何痕迹。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            text = f.read()
    except Exception as ex:
        if errors is not None:
            errors.append((path, "读取失败: %s" % ex))
        return []
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".lua":
            return _extract_lua(text, loose=loose, trace=trace)
        if ext in (".yaml", ".yml"):
            return _extract_yaml(text)
        return _extract_reds(text, ext, target_locale=target_locale,
                             loose=loose, trace=trace)
    except Exception as ex:
        if errors is not None:
            errors.append((path, "%s: %s" % (type(ex).__name__, ex)))
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
            elif ext in (".reds", ".xml", ".yaml", ".yml"):
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
    meta = {"files": 0, "lua_files": 0, "reds_files": 0, "yaml_files": 0,
            "types": {},
            "locale_json": _count_locale_json(root),
            "locale_lua": _count_locale_lua(root), "skipped_target": 0,
            "locale_pairs": 0, "locale_pairs_used": 0, "noise": {},
            "parse_errors": 0, "parse_error_sample": []}
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
            elif ext in (".yaml", ".yml"):
                items = _extract_yaml(text)
                meta["yaml_files"] += 1
            else:
                reds_stats = {}
                items = _extract_reds(text, ext, target_locale=target_locale,
                                      stats=reds_stats, loose=loose,
                                      trace=trace, noise=noise)
                meta["reds_files"] += 1
                for k in ("locale_pairs", "locale_pairs_used"):
                    meta[k] += reds_stats.get(k, 0)
        except Exception as ex:
            # 解析不了 ≠ 没有文本: 计数记下来, 让加载结果里能报出来。
            meta["parse_errors"] += 1
            if len(meta["parse_error_sample"]) < 5:
                meta["parse_error_sample"].append(
                    "%s: %s: %s" % (rel, type(ex).__name__, ex))
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


def escape_for(text, quote, yaml=False):
    """把译文转义成目标语言字符串内容。

    yaml=True 走 YAML 标量的规则, 和 .reds/.lua 完全不同:
    单引号里**只有** ``'`` 需要写成 ``''``，反斜杠是字面量 —— 这些说明里的
    ``\\n\\n`` 是排版用的段落分隔, 按代码语言转义成 ``\\\\n`` 会在游戏里
    显示成可见的「\n\n」，整段说明挤成一行。
    双引号里 ``"`` 写成 ``\\"``，其余原样。
    """
    if yaml:
        # 真换行必须写成 `\n`: 单引号标量里塞一个真换行, 整份 yaml 的行结构就
        # 断了(而且这些说明本来就是用 `\n\n` 分段的)。
        flat = (text.replace("\r\n", "\\n").replace("\r", "\\n")
                    .replace("\n", "\\n"))
        if quote == "'":
            return flat.replace("'", "''")
        return flat.replace('"', '\\"')
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


# 代码碎片的特征: redscript 插值 \\( / 连续右括号 / 以 ) ] 开头。
#
# 原来的规则还包含「以 , 开头」和「以 ); 结尾」两支, 实测把它们全算上时
# 45 条拦截里 43 条是误伤:
#   - `s += ", 已计入" + IntToString(n)` 是 redscript 拼 UI 文本的惯用写法;
#   - `"… (the mod folder is not writable …); the previous settings file is kept"`
#     是屏幕上的说明文字, 只是句子里正好出现了「);」。
# 真正要防的插值断裂一定会命中 \\\( 或 \)\), 这两支保留就够了。
_CODEY_FRAGMENT_RE = re.compile(r"\\\(|\)\)|^\s*[\)\]]")


def looks_like_code_fragment(t):
    r"""判断一条「原文」其实是代码碎片, 而不是文案。

    ``s"..."`` 插值串被错误切分后会留下 ``)) \(this.m_page + 1)/\(pages)``
    这种半句代码: 它既不该翻, 写回去还会把插值的右括号一起吃掉, 让整行
    语法崩掉 (REDscript 编译失败 -> 整个脚本层不加载)。

    已经是中文的串直接放行: 代码碎片是 ASCII, 中文只可能是译好的文案。
    老规则不区分这一点, 于是历史中文行(「, 已计入」「) 重试后」)会被永久
    拒写, 用户连改措辞都做不到。
    """
    s = (t or "").strip()
    if not s:
        return False
    if CJK_RE.search(s):
        return False
    return bool(_CODEY_FRAGMENT_RE.search(s))


# 译文必须把原文的标记和占位符带回来。
#
# Codeware 的 .reds 文案大量写成
#   <Rich color="TooltipText.cyberwareDescriptionHighlightColor">+{int_0}%</>
# 模型几乎总会把尖括号那一段当成 HTML 标记整段删掉（实测 DigitalVixenCore
# 2 条），游戏里就只剩一行既没高亮也没配色的纯文本。把 `{value}` 换成 `%s`
# 更糟：`%s` 不是 Codeware 的插值写法，弹窗会原样显示「游戏默认值：%s」。
#
# 开头/结尾的标签位置没有歧义，可以直接补回去；夹在句中、或者花括号占位符
# 丢了，就没法安全插回——那种情况报出来按软失败拦下，宁可这一条留英文。
_MARKUP_TAG_RE = re.compile(r"<[^<>\n]{1,400}>")
_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][^{}\n]{0,40}\}")
_PCT_SPEC_RE = re.compile(r"%(?:\d+\$)?[-+0-9.]*[sdifqxX%]")


def _count(seq):
    out = {}
    for x in seq:
        out[x] = out.get(x, 0) + 1
    return out


def _plain_escapes(s):
    """把转义引号还原成引号。

    只用在「这段标签在不在译文里」这类包含判断上：原文是 .reds 里的字面量，
    带 `\\"`（`<Rich color=\\"TooltipText…\\">`），而模型给回来的译文一律是裸引号。
    直接拿带反斜杠的那份去 `in` 判断永远不命中，于是补标记时会把同一段标签
    再加一遍 —— 结果是 `<Rich …><Rich …>文字</>`：游戏里整段标记被当普通文字
    显示出来（SenSen 四个模组就是这么坏的）。
    """
    return s.replace('\\"', '"').replace("\\'", "'")


def _trim_excess_closes(text):
    """末尾多出来的关闭标记删掉。

    模型常把原文结尾的 ``</>`` 照抄过来、却把对应的开标签当 HTML 吞了：
    一句说明末尾就挂出 ``</></></></>`` 这么一串，游戏里原样显示成一排尖括号。
    关闭标记比开标签多时，从末尾逐个删到配平为止。
    """
    for _ in range(40):
        tags = _MARKUP_TAG_RE.findall(text)
        opens = sum(1 for t in tags if not t.startswith("</"))
        if len(tags) - opens <= opens:
            break
        last = None
        for m in _MARKUP_TAG_RE.finditer(text):
            if m.group(0).startswith("</"):
                last = m
        if last is None or text[last.end():].strip():
            break
        text = text[:last.start()] + text[last.end():]
    return text


def repair_markup(orig, trans):
    """把译文丢掉的包裹标记补回来，返回 ``(译文, 补不回来的占位符列表)``。

    能补的三种情况，位置都没有歧义：

    * 原文开头那一串标签（``<Rich color=...>``）——补在译文最前面；
    * 原文结尾那一串标签——补在译文最后面；
    * 原文里的关闭标记（``</>``）在译文里没了——补在译文末尾。Rich 文本的
      高亮本来可能只盖住半句，但补在末尾只是把高亮范围放大一点，不影响文案；
    * 模型把 ``{value}`` 写成 ``%s``——两边各多一个、彼此都没有时，按顺序换回来。

    其余丢失（句中标签、多出来的花括号）补不了，放进第二个返回值，由调用方
    按软失败拦下：宁可这一条留英文，也不要把占位符写没。
    """
    if not orig or not trans or orig == trans:
        return trans, []
    m_lead = re.match(r"(?:<[^<>\n]{1,400}>)+", orig)
    m_trail = re.search(r"(?:<[^<>\n]{1,400}>)+$", orig)
    lead = m_lead.group(0) if m_lead else ""
    trail = m_trail.group(0) if (m_trail and m_lead and
                                 m_trail.start() >= m_lead.end()) else ""
    if trail and trail == lead:
        trail = ""                      # 只有一个自闭合标签, 不是包裹
    fixed = trans
    plain = _plain_escapes(fixed)
    if lead and _plain_escapes(lead) not in plain:
        fixed = lead + fixed
        plain = _plain_escapes(fixed)
    if trail and _plain_escapes(trail) not in plain:
        fixed = fixed + trail
        plain = _plain_escapes(fixed)

    # 模型把 {value} 写成 %s：各多一个、彼此都没有，按顺序换回来。
    want = _count(_PLACEHOLDER_RE.findall(orig))
    have = _count(_PLACEHOLDER_RE.findall(fixed))
    want_pct = _count(_PCT_SPEC_RE.findall(orig))
    have_pct = _count(_PCT_SPEC_RE.findall(fixed))
    lost_ph = [k for k, n in want.items() if have.get(k, 0) < n]
    extra_pct = [k for k, n in have_pct.items() if want_pct.get(k, 0) < n]
    if len(lost_ph) == 1 and len(extra_pct) == 1:
        fixed = fixed.replace(extra_pct[0], lost_ph[0])
        want = _count(_PLACEHOLDER_RE.findall(orig))
        have = _count(_PLACEHOLDER_RE.findall(fixed))
        lost_ph = [k for k, n in want.items() if have.get(k, 0) < n]

    # 关闭标记数量对不上：少一个就等于整段标记没闭合，游戏会把 `<Rich …>` 当普通
    # 文字原样显示出来（SenSen 四个模组的说明文字整段变成 `<Rich color=…>3% …`）。
    # 多个缺口的真实位置分不清，统一补在末尾：结果是高亮范围偏大一点，文案照样能读。
    #
    # 但只能补到「开标签都有主」为止：模型把整段 `<Rich …>…</>` 成对删掉时，译文里
    # 开标签只剩 2 个，照着原文的 12 个关闭标记补，末尾就挂出 10 个 `</>`（Pax Grips
    # 的说明行，12 个关闭标记对 2 个开标签），游戏里显示成一排尖括号。宁可少一段高亮。
    closes = [t for t in _MARKUP_TAG_RE.findall(orig) if t.startswith("</")]
    tags_have = _MARKUP_TAG_RE.findall(fixed)
    opens_have = sum(1 for t in tags_have if not t.startswith("</"))
    closes_have = len(tags_have) - opens_have
    need = len(closes) - closes_have
    if need > 0 and closes and opens_have > closes_have:
        fixed = fixed + closes[0] * min(need, opens_have - closes_have)
    fixed = _trim_excess_closes(fixed)

    lost = list(lost_ph)
    for name, n in want_pct.items():
        if _count(_PCT_SPEC_RE.findall(fixed)).get(name, 0) < n:
            lost.append(name)
    # 句中标签（一句话里两段不同颜色的数值）位置不明，补不回去，但丢的只是一段
    # 高亮配色，不影响文案能读：这类不拦，照常写回。真正会坏的只有占位符——
    # 少了它运行时拿到的是字面的 {int_0} 或 %s。
    return fixed, lost


def _pct_specs(s):
    return [m.group(0) for m in _PCT_SPEC_RE.finditer(s or "")
            if m.group(0) != "%%"]


def swapped_spec_order(orig, trans):
    """译文里的 %s/%d 顺序和原文不一样时, 返回 ``(级别, 原因)``; 一致返回 ``("", "")``。

    ``string.format`` 是**按位置**取参数的: 原文 ``"Applied %d change(s) for %s."``
    传进去的是 (数字, 名字)。中文习惯把名字放前面, 模型就顺手写成
    ``"已为 %s 应用 %d 项更改。"`` —— 占位符一个不少, 原来的校验只数个数, 于是
    照样写回; 运行时第 3 个参数要 ``%d`` 却拿到字符串, 直接
    ``bad argument #3 to 'format' (number expected, got string)``, 整个 MCM
    面板的操作都报「MCM action failed」(实测 MCM core.lua:1344 崩在这里)。

    中文语序想调只能改句子, 不能让占位符换位。个数对不上的情况交给原有的
    「丢失」检查, 这里只管「个数对、顺序错」。级别给 soft: 万一原文那个位置
    本来就传的是数字 (``"%s items", #list``), 用户还能单条覆盖放行。
    """
    a = _pct_specs(orig)
    b = _pct_specs(trans)
    if len(a) < 2 or len(a) != len(b) or a == b:
        return "", ""
    if _count(a) != _count(b):
        return "", ""
    # 原因写成固定一句: 失败列表里本来就带原文和译文两列, 动态拼串反而没法进
    # locales（界面语言切英文时这句会裸露成中文）。
    return "soft", "占位符顺序变了, string.format 按位置取参数会崩"


# 模型偶尔会「跑飞」：同一段话连写几十遍。翻译记忆里实测两条 ——
#   GR-MsgPromoted       → "你已晋升为 %s 级 %d 级 %q 级 %s 级 …"（42 遍、127 个占位符）
#   g:ghxst_boss_warn_02 → "%q，%s，%d 个快速破解，… %d 个感动，%d 个震撼"（33 遍、86 个）
# 写回模组后游戏里是一长串重复的数字，`string.format` 还会因为参数不够直接抛错。
# 原文本来就重复的说法（"No, no, no" / "先生们，先生们"）不算，放行。
_ADJ_DUP_RE = re.compile(r"(.{4,}?)\1", re.S)
_PH_ANY_RE = re.compile(r"\{[A-Za-z_][^{}\n]{0,40}\}|%(?:\d+\$)?[-+0-9.]*[sdifqxX]")


def _adjacent_run(text):
    """本段文字里「紧邻重复」的那一段重复了几遍，返回 (单元, 遍数)。"""
    t = text or ""
    m = _ADJ_DUP_RE.search(t)
    if not m:
        return "", 0
    unit = m.group(1)
    run, i = 0, m.start()
    while t.startswith(unit, i):
        run += 1
        i += len(unit)
    return unit, run


def repeat_run_guard(orig, trans):
    """译文里同一段连写 3 遍以上 → ``("soft", 原因)``；否则 ``("", "")``。

    级别给 soft：真需要重复的说法（副标题里的 "No, no, no"）用户能单条覆盖，
    批量永远不写。

    原因写成固定一句，不拼遍数：这句要能进 `locales/en-US.json`，动态拼串
    在英文界面下会裸露成中文。
    """
    _unit, run = _adjacent_run(trans)
    if run < 3:
        return "", ""
    _unit2, run2 = _adjacent_run(orig)
    if run2 >= 3:
        return "", ""       # 原文自己也这么重复，是照着翻的
    return "soft", "译文里同一段连写了多遍（模型跑飞），先确认再写"


def placeholder_explosion(orig, trans):
    """译文的占位符比原文多出一大截 → ``("soft", 原因)``。

    ``{int_0}`` / ``%s`` 这类占位符是运行时按位置填的：原文 1 个、译文 24 个，
    游戏里除了多出一串空白还会 `format` 崩。

    只在**原文自己带占位符**时比较。Ghost Protocol 那类模组的原文是一串 id
    （``g:ghxst_heat_02``），真正的模板在别处，译文带占位符是正常的 —— 拿
    「原文 0 个」去比会把 14 条正常译法一起误伤。
    """
    want = len(_PH_ANY_RE.findall(orig or ""))
    have = len(_PH_ANY_RE.findall(trans or ""))
    if want < 1:
        return "", ""
    if have >= want + 3 and have >= 2 * want:
        return "soft", "译文里的占位符比原文多出好几个, 写回去 format 会崩"
    return "", ""


def _code_shape(toks):
    return [(t.kind, "" if t.kind == "str" else t.val) for t in toks]


def same_code_shape(old_text, new_text, lang):
    """替换前后「代码骨架」必须一致(字符串内容除外)。

    只改字符串内容时骨架不变; 替换后若多/少括号、引号错位, 骨架就会变,
    说明这次写回会把语法改坏, 必须拒绝而不是硬写。
    """
    try:
        t1, _ = scan_source(old_text, lang)
        t2, _ = scan_source(new_text, lang)
    except Exception:
        return False
    return _code_shape(t1) == _code_shape(t2)


def _shape_guard_applies(path):
    """只有真正的代码文件才做「骨架不变」校验。

    .txt / .ini 这类纯文本没有语法, 一段中文替换掉一段英文时 token 序列
    本来就会变, 不能拿它当依据。
    """
    return os.path.splitext(path)[1].lower() in (".reds", ".lua")


def guard_level(path, orig, trans):
    """写回前的内容级裁决, 返回 (级别, 原因)。级别为空表示放行。

    soft = 形态可疑, 可能误报 —— 允许用户单条覆盖(默认仍然拒绝);
    hard = 写进去必坏, 永远不给覆盖开关。

    以前两者塞在同一条失败通道里, 用户只看到「应用失败」, 既分不清能不能救,
    也没法告诉工具「这条我看过了, 别再报」。
    """
    if _shape_guard_applies(path) and looks_like_code_fragment(orig):
        return "soft", "疑似代码片段, 拒绝写回"
    level, why = swapped_spec_order(orig, trans)
    if level:
        return level, why
    # 模型跑飞：同一段连写几十遍 / 占位符凭空多出一大截。两条都只坏这一条，
    # 但批量里没人会逐条看，必须拦下。
    level, why = repeat_run_guard(orig, trans)
    if level:
        return level, why
    level, why = placeholder_explosion(orig, trans)
    if level:
        return level, why
    if NOISE.is_protocol_list(orig) and not NOISE.is_protocol_list(trans or ""):
        return "hard", "这是能力/协议清单, 翻掉后别处解析不出任何一项"
    if NOISE.is_data_row(orig):
        if NOISE.data_row_shape(orig) != NOISE.data_row_shape(trans or ""):
            return "hard", "竖线数据行的段数或属性变了, 写回会打断 id 表"
    return "", ""


def _edge_spaces(text, lit, orig, trans):
    """拼接上下文里的首尾空格要保住 (P14)。

    ``label="Restrict "..gig:GetQuestName()`` 写回成 ``label="限制"..`` 之后,
    面板上显示的是「限制Japantown…」—— 缺的那个空格模型不会给, 因为原文里
    它在引号内、译文里它被当成排版噪音吃掉了。只在字面量紧邻 ``..`` 时才补,
    普通字段（``description = "..."``）不动, 免得无端多出空格。
    """
    if not trans:
        return trans
    # 两侧都留 4 个字符再看：写成 `"Restrict " .. name` 时，引号前面是
    # 「空格 + ..」，只取 2 个字符会看到 ". " 而漏判。
    left = (text[max(0, lit.start - 4):lit.start] or "").strip().endswith("..")
    right = (text[lit.end:lit.end + 4] or "").strip().startswith("..")
    pre = post = ""
    if left and orig[:1].isspace() and not trans[:1].isspace():
        pre = " "
    if right and orig[-1:].isspace() and not trans[-1:].isspace():
        post = " "
    return pre + trans + post


def _render_replacements(text, repls):
    """把 [(lit, 原文, 译文, 索引)] 一次性套到 text 上(从后往前, 偏移不互相影响)。"""
    out = text
    for lit, orig, trans, _idx in sorted(repls, key=lambda x: -x[0].start):
        suffix = lit.inner[len(orig):] if lit.inner.startswith(orig) else ""
        body = escape_for(_edge_spaces(text, lit, orig, trans) + suffix,
                          lit.quote, yaml=(lit.prefix == "yaml"))
        out = out[:lit.start + 1] + body + out[lit.end - 1:]
    return out


def _shape_offenders(base, repls, lang, budget=40):
    """二分找出「把代码骨架改坏」的那几条, 返回 (坏行, 剩下的)。

    全批一起拒会把同一文件里几十条好译文一起扣下(一条坏 -> 整批 0 条);
    逐条重放又太慢(大文件每行一次全量分词)。二分只需要 O(log n) 次分词。
    """
    bad, rest = [], list(repls)
    while rest and len(bad) < budget:
        if same_code_shape(base, _render_replacements(base, rest), lang):
            break
        chunk = rest
        while len(chunk) > 1:
            half = chunk[:len(chunk) // 2]
            if not same_code_shape(base, _render_replacements(base, half), lang):
                chunk = half
            else:
                chunk = chunk[len(chunk) // 2:]
        bad.append(chunk[0])
        rest = [x for x in rest if x is not chunk[0]]
    return bad, rest


def apply_many_ex(path, pairs, allow_soft=False):
    """一次读改写: pairs = [(行号, 原文, 译文)]。

    返回 (成功条数, 失败明细)。失败明细为
    ``[(pairs索引, 行号, 原文, 译文, 原因, 级别)]``, 级别是 "soft" / "hard"。
    比逐条 apply_translation 快得多(文件只读写一次), 用于回填大批量译文。

    allow_soft: 允许写回 soft 级失败(代码碎片这类「可疑但可能误报」的行)。
                只有单条覆盖(apply_one)才会传 True; 批量永远不传。
    """
    pairs = list(pairs)
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
    except Exception:
        return 0, [(i, p[0], p[1], p[2], "读取文件失败", "hard")
                   for i, p in enumerate(pairs)]
    original_text = text
    lang = lang_of(path)
    guard = _shape_guard_applies(path)
    try:
        toks, lits = scan_source(text, lang)
    except Exception:
        return 0, [(i, p[0], p[1], p[2], "文件解析失败", "hard")
                   for i, p in enumerate(pairs)]
    # Codeware 语言包 `this.Text("key", "译文")` 的第一个参数是查表键。
    # 兜底匹配（尤其「按行号」那一层）以前会挑中它：目标文件这一行只剩
    # 「键 + 已经翻好的译文」时，英文原文一个都匹配不上，兜底就把译文写进了
    # key —— 文件语法照旧、行数照旧，界面上却什么都查不到（Pax Grips 的
    # 4 条说明有 3 条 key 变成中文，游戏里这几条直接消失）。
    try:
        _texts, key_starts = codeware_text_calls(toks, lits)
    except Exception:       # noqa: BLE001
        key_starts = set()
    by_key, by_text, by_line, key_lines = {}, {}, {}, set()
    for lit in lits:
        if lit.quote not in ("\"", "'"):
            continue
        if lit.start in key_starts:
            key_lines.add(lit.line)
            continue
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
        # 译文与原文一字不差 = 模型没翻出来(单个英文单词、专名最常见)。这种
        # 条目写回去只有坏处: 目标文件里本来有更好的译文时会被原文顶掉。
        # MCM 的 zh-cn.json 就是这样被冲回英文的 -- "访问" 覆盖成 "Access"。
        # 既不算成功也不算失败: 文件没变, 统计里不该出现。
        if str(trans) == str(orig):
            continue
        level, why = guard_level(path, orig, trans)
        if level == "hard" or (level == "soft" and not allow_soft):
            failures.append((idx, line, orig, trans, why, level))
            continue
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
            # 这一行只剩查表键：说明目标文件里这条已经翻过（或本就不是文案），
            # 再怎么兜底也只能改坏引用。宁可报失败，让用户看见。
            if line_no in key_lines:
                failures.append((idx, line, orig, trans,
                                 "这一行只剩查表键, 写进去会打断本地化引用", "hard"))
            else:
                failures.append((idx, line, orig, trans, "未找到对应原文", "hard"))
            continue
        repls.append((lit, orig, trans, idx))
    if not repls:
        return 0, failures
    text = _render_replacements(original_text, repls)
    if guard and not same_code_shape(original_text, text, lang):
        # 有行把语法改坏了。找出是哪一条, 而不是整批拒 —— 否则一条坏
        # 就干掉同文件里几十条好译文, 用户还看不出来是谁。
        bad_repls, keep = _shape_offenders(original_text, repls, lang)
        if not keep:
            return 0, [(i, p[0], p[1], p[2],
                        "写回后代码骨架不一致, 已拒绝", "hard")
                       for i, p in enumerate(pairs)]
        for _lit, orig, trans, idx in bad_repls:
            failures.append((idx, pairs[idx][0], orig, trans,
                             "写回后代码骨架不一致, 这条会改坏语法", "hard"))
        repls = keep
        text = _render_replacements(original_text, repls)
        if guard and not same_code_shape(original_text, text, lang):
            return 0, [(i, p[0], p[1], p[2],
                        "写回后代码骨架不一致, 已拒绝", "hard")
                       for i, p in enumerate(pairs)]
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    except Exception:
        all_fail = [(i, p[0], p[1], p[2], "写入文件失败", "hard")
                    for i, p in enumerate(pairs)]
        return 0, all_fail
    return len(repls), failures


def apply_one(path, line, orig, trans, allow_soft=False):
    """单条写回: 复用 apply_many_ex 的同一个入口。

    撤销记录、记忆记账、失败回滚都走同一条路, 否则单条与批量会出现两套行为。
    """
    return apply_many_ex(path, [(line, orig, trans)], allow_soft=allow_soft)


def apply_translation(path, line_no, original, trans, allow_soft=False):
    """把文件中 (行号, 原文) 对应的字符串替换为译文; 返回是否成功"""
    n, _failed = apply_many_ex(path, [(line_no, original, trans)],
                              allow_soft=allow_soft)
    return n == 1


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
