# -*- coding: utf-8 -*-
"""
locale_pack.py —— 语言包模式

针对"模组已经自带多语言文件"的情况:
  * 找出 locales/*.json、en_US.lua、English.reds 这类语言包
  * 从源语言文件里抽出 键->原文
  * 翻译后**生成一个新的目标语言文件**(而不是改源文件)

json 走"解析→改值→写回"；lua / reds 走"复制源文件→按行替换"。
"""
import json
import os
import re
import shutil

import mod_text_engine as E

LOCALE_DIRS = {"locales", "locale", "localization", "localisations", "i18n",
               "lang", "langs", "languages"}

# 文件名/目录名 -> 规范语言标签
LOCALE_ALIASES = {
    "en-us": "en-US", "en_us": "en-US", "english": "en-US", "en": "en-US", "eng": "en-US",
    "zh-cn": "zh-CN", "zh_cn": "zh-CN", "chinesesimplified": "zh-CN", "schinese": "zh-CN",
    "chs": "zh-CN", "cn": "zh-CN", "zh-hans": "zh-CN",
    "zh-tw": "zh-TW", "zh_tw": "zh-TW", "chinesetraditional": "zh-TW", "tchinese": "zh-TW",
    "cht": "zh-TW",
    "ja-jp": "ja-JP", "japanese": "ja-JP", "ja": "ja-JP", "jp-jp": "ja-JP", "jp": "ja-JP",
    "ko-kr": "ko-KR", "korean": "ko-KR", "ko": "ko-KR", "kr-kr": "ko-KR", "kr": "ko-KR",
    "ru-ru": "ru-RU", "russian": "ru-RU", "ru": "ru-RU",
    "de-de": "de-DE", "german": "de-DE", "de": "de-DE",
    "fr-fr": "fr-FR", "french": "fr-FR", "fr": "fr-FR",
    "es-es": "es-ES", "spanish": "es-ES", "es": "es-ES", "es-mx": "es-MX",
    "pl-pl": "pl-PL", "polish": "pl-PL", "pl": "pl-PL",
    "pt-br": "pt-BR", "portuguese": "pt-BR", "pt": "pt-BR",
    "it-it": "it-IT", "italian": "it-IT", "it": "it-IT",
    "cz-cz": "cs-CZ", "czech": "cs-CZ", "cs": "cs-CZ",
    "tr-tr": "tr-TR", "turkish": "tr-TR", "tr": "tr-TR",
    "hu-hu": "hu-HU", "hungarian": "hu-HU",
    "uk-ua": "uk-UA", "ukrainian": "uk-UA", "ua-ua": "uk-UA", "ua": "uk-UA",
    "ar": "ar-SA", "ar-ar": "ar-SA", "arabic": "ar-SA",
    "th-th": "th-TH", "thai": "th-TH",
}

# 文件名里带语言后缀的写法: BoardEnglish.reds / RiteFullChineseSimplified.reds
_LOCALE_SUFFIX_RE = re.compile(
    r"^(.*?)(" + "|".join(
        sorted((re.escape(k) for k in LOCALE_ALIASES if len(k) >= 5),
               key=len, reverse=True)
    ) + r")$",
    re.I,
)


def _locale_prefix(stem):
    """返回语言名之前的文件名前缀, 例如 BoardEnglish.reds -> "Board"。"""
    m = _LOCALE_SUFFIX_RE.match(stem)
    return m.group(1) if m and m.group(1) else ""


LOCALE_LABELS = {
    "en-US": "English", "zh-CN": "简体中文", "zh-TW": "繁體中文", "ja-JP": "日本語",
    "ko-KR": "한국어", "ru-RU": "Русский", "de-DE": "Deutsch", "fr-FR": "Français",
    "es-ES": "Español", "pl-PL": "Polski", "pt-BR": "Português", "it-IT": "Italiano",
    "cs-CZ": "Čeština", "tr-TR": "Türkçe", "hu-HU": "Magyar", "uk-UA": "Українська",
    "ar-SA": "العربية", "es-MX": "Español (México)", "th-TH": "ไทย",
}

# 目标语言 -> 生成文件用的名字
TARGET_NAMES = {
    "zh-CN": {"json": "zh-cn", "lua": "zh_CN", "reds": "ChineseSimplified"},
    "zh-TW": {"json": "zh-tw", "lua": "zh_TW", "reds": "ChineseTraditional"},
    "en-US": {"json": "en-us", "lua": "en_US", "reds": "English"},
    "ja-JP": {"json": "ja-jp", "lua": "ja_JP", "reds": "Japanese"},
    "ko-KR": {"json": "ko-kr", "lua": "ko_KR", "reds": "Korean"},
    "ru-RU": {"json": "ru-ru", "lua": "ru_RU", "reds": "Russian"},
    "de-DE": {"json": "de-de", "lua": "de_DE", "reds": "German"},
    "fr-FR": {"json": "fr-fr", "lua": "fr_FR", "reds": "French"},
    "es-ES": {"json": "es-es", "lua": "es_ES", "reds": "Spanish"},
    "pl-PL": {"json": "pl-pl", "lua": "pl_PL", "reds": "Polish"},
    "pt-BR": {"json": "pt-br", "lua": "pt_BR", "reds": "PortugueseBrazil"},
    "it-IT": {"json": "it-it", "lua": "it_IT", "reds": "Italian"},
    "cs-CZ": {"json": "cs-cz", "lua": "cs_CZ", "reds": "Czech"},
    "tr-TR": {"json": "tr-tr", "lua": "tr_TR", "reds": "Turkish"},
    "hu-HU": {"json": "hu-hu", "lua": "hu_HU", "reds": "Hungarian"},
    "uk-UA": {"json": "uk-ua", "lua": "uk_UA", "reds": "Ukrainian"},
    "ar-SA": {"json": "ar-sa", "lua": "ar_SA", "reds": "Arabic"},
}


# 设置里的"目标语言"显示名 -> 语言标签
TARGET_LANG_CODES = {
    "简体中文": "zh-CN", "繁體中文": "zh-TW", "English": "en-US",
    "日本語": "ja-JP", "한국어": "ko-KR", "Русский": "ru-RU",
    "Español": "es-ES", "Deutsch": "de-DE", "Français": "fr-FR",
}


def target_code(target_lang):
    return TARGET_LANG_CODES.get(target_lang, "zh-CN")


def pick_sources(files, target):
    """返回所有源语言文件: 优先英文包, 其次未知/非目标语言。

    当前目标语言文件永远不作为源文件。若模组里只有目标语言文件,
    返回空列表, 让上层退回内容模式或提示没有可提取的原文。
    """
    cand = [f for f in files
            if f["locale"] == "en-US" and f["locale"] != target]
    if not cand:
        cand = [f for f in files
                if (not f["locale"] or f["locale"] != target)]
    return cand


def pick_source(files, target):
    src = pick_sources(files, target)
    return src[0] if src else None


def canonical_locale(stem):
    key = re.sub(r"[^A-Za-z0-9\-_]", "", stem).lower()
    if key in LOCALE_ALIASES:
        return LOCALE_ALIASES[key]
    m = _LOCALE_SUFFIX_RE.match(stem)
    if m and m.group(1):
        return LOCALE_ALIASES.get(m.group(2).lower())
    # zh-cn / en-us / pt-br 这类
    m = re.match(r"^([a-z]{2})[-_]?([a-z]{2})$", key)
    if m:
        tag = "%s-%s" % (m.group(1), m.group(2).upper())
        if tag in LOCALE_LABELS:
            return tag
    return None


# 变体尾巴（en-us-sms 里的 sms）：允许连字符，最长 20。
_VARIANT_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,19}$")


def split_locale_stem(stem):
    """把 "en-us-sms" 拆成 ("en-US", "sms")；拆不开返回 (None, "")。

    模组常用「语言-用途」命名，CET 的 languages/en-us-sms.json 就是短信库。
    两段式认得出，带用途尾巴的旧写法认不出，整份文件就被当无关文件跳过了。
    这里先整串试，再从左往右剥尾巴试，取最长匹配，剩下的原样留作变体。
    """
    loc = canonical_locale(stem)
    if loc:
        return loc, ""
    parts = re.split(r"[-_.]", stem)
    for cut in range(len(parts) - 1, 0, -1):
        head = "-".join(parts[:cut])
        tail = "-".join(parts[cut:]).lower()
        if not _VARIANT_RE.match(tail):
            continue
        loc = canonical_locale(head)
        if loc:
            return loc, tail
    return None, ""


# ---------------------------------------------------------------- 抽取
def _flatten(obj, prefix=""):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = "%s.%s" % (prefix, k) if prefix else str(k)
            out.extend(_flatten(v, key))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_flatten(v, "%s[%d]" % (prefix, i)))
    elif isinstance(obj, str):
        out.append((prefix, obj))
    return out


def _set_path(obj, path, value):
    """按 a.b[0].c 的路径写回"""
    tokens = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    cur = obj
    for i, tok in enumerate(tokens):
        last = i == len(tokens) - 1
        if tok.startswith("["):
            idx = int(tok[1:-1])
            if last:
                cur[idx] = value
            else:
                cur = cur[idx]
        else:
            if last:
                cur[tok] = value
            else:
                cur = cur[tok]
    return obj


def _json_pairs(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return []
    return [(k, v) for k, v in _flatten(data) if k]


def _text_pairs_ex(path):
    """lua / reds: 用引擎的字面量扫描, 返回 (行号, 原文, key)。

    Codeware 的 this.Text("key", "value") 只提取第二个参数 value；
    第一个参数 key 会跳过并作为上下文显示。
    """
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except Exception:
        return []
    lang = "lua" if path.lower().endswith(".lua") else "reds"
    try:
        toks, lits = E.scan_source(text, lang)
    except Exception:
        return []
    values, key_starts = E.codeware_text_calls(toks, lits)
    out = []
    for lit in lits:
        if lit.quote not in ("\"", "'"):
            continue
        if lit.start in key_starts:
            continue
        if lit.start in values:
            val = E.visible_text(lit.inner)
            if val.strip():
                out.append((lit.line, val, values[lit.start]))
            continue
        if not E.is_translatable(lit.inner):
            continue
        out.append((lit.line, lit.inner, ""))
    return out


def _text_pairs(path):
    """兼容旧调用: 只返回 (行号, 原文)。"""
    return [(line, val) for line, val, _key in _text_pairs_ex(path)]


def detect(root, parse=True, target="zh-CN"):
    """扫描模组里的语言包文件

    parse=True 时只统计"源语言"文件的条数(其它语言填 0), 避免把几百个
    语言包全部解析一遍。
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in
                       ("redmod_backup", ".git", "node_modules", "__pycache__")]
        in_locale_dir = os.path.basename(dirpath).lower() in LOCALE_DIRS
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".json", ".lua", ".reds"):
                continue
            stem = os.path.splitext(fn)[0]
            loc, variant = split_locale_stem(stem)
            if not loc and not (in_locale_dir and ext == ".json"):
                continue
            full = os.path.join(dirpath, fn)
            found.append({
                "rel": os.path.relpath(full, root).replace("\\", "/"),
                "abs": full,
                "dir": os.path.relpath(dirpath, root).replace("\\", "/"),
                "locale": loc or "",
                "variant": variant,
                "format": "json" if ext == ".json" else ext[1:],
                "count": 0,
            })
    found.sort(key=lambda x: (x["locale"], x["rel"]))
    if parse:
        srcs = {f["rel"] for f in pick_sources(found, target)}
        for f in found:
            if f["rel"] not in srcs:
                continue
            try:
                pairs = (_json_pairs(f["abs"]) if f["format"] == "json"
                         else _text_pairs(f["abs"]))
            except Exception:
                pairs = []
            f["count"] = len(pairs)
    return found


def target_rel(entry, target_locale):
    """算出目标语言文件的相对路径

    源文件的「用途变体」后缀要带过去：en-us-sms.json 的目标是 zh-cn-sms.json，
    不能退化成 zh-cn.json，否则短信库会被写进设置界面那份文件里。
    """
    names = TARGET_NAMES.get(target_locale) or {}
    folder = entry["dir"] if entry["dir"] != "." else ""
    src = entry["rel"]
    ext = os.path.splitext(src)[1]
    variant = entry.get("variant") or ""
    stem = names.get(entry["format"]) or target_locale
    src_stem = os.path.splitext(os.path.basename(src))[0]
    prefix = _locale_prefix(src_stem)
    if prefix:
        stem = prefix + stem
    if entry["format"] == "json":
        # locales/zh-cn.json  (统一小写)
        stem = (names.get("json") or target_locale).lower()
        if prefix:
            stem = prefix.lower() + stem
    if variant:
        stem = stem + "-" + variant
    fn = stem + ext
    return (folder + "/" + fn).lstrip("/") if folder else fn


def _existing_target(root, tgt):
    """读已经存在的目标语言文件, 用于回填已有译文"""
    path = os.path.join(root, tgt.replace("/", os.sep))
    if not os.path.exists(path):
        return None
    try:
        if tgt.lower().endswith(".json"):
            return {k: v for k, v in _json_pairs(path)}
        return [v for _ln, v in _text_pairs(path)]
    except Exception:
        return None


def extract(root, entries, target_locale):
    """把选中的语言包抽成表格行; 目标文件已存在时顺带把已有译文带出来。

    已经是目标语言的文本会被跳过(中文/日文/韩文/俄文/阿拉伯文可按字符识别),
    避免把目标语言文件或源文件里已经翻好的内容再次当成原文。
    """
    rows = []
    meta = {"files": 0, "types": {}, "locale": True, "skipped_target": 0}
    for entry in entries:
        rel = entry["rel"]
        tgt = target_rel(entry, target_locale)
        fmt = entry["format"]
        existing = _existing_target(root, tgt)
        if fmt == "json":
            pairs = _json_pairs(os.path.join(root, rel))
            for key, val in pairs:
                if not E.is_translatable(val):
                    continue
                if E.is_target_language_text(val, target_locale):
                    meta["skipped_target"] += 1
                    continue
                cur = ""
                if isinstance(existing, dict):
                    v = existing.get(key) or ""
                    if v and v != val:
                        cur = v
                rows.append({"file": rel, "disp": rel, "line": 0, "type": "语言包",
                             "ctx": key, "orig": val, "trans": cur,
                             "target": tgt, "kind": "json", "key": key})
        else:
            pairs = _text_pairs_ex(os.path.join(root, rel))
            for idx, (line, val, key) in enumerate(pairs):
                if E.is_target_language_text(val, target_locale):
                    meta["skipped_target"] += 1
                    continue
                cur = ""
                if isinstance(existing, list) and idx < len(existing):
                    v = existing[idx] or ""
                    if v and v != val:
                        cur = v
                rows.append({"file": rel, "disp": rel, "line": line, "type": "语言包",
                             "ctx": key, "orig": val, "trans": cur,
                             "target": tgt, "kind": "text"})
        meta["files"] += 1
        meta["types"]["语言包"] = meta["types"].get("语言包", 0) + 1
    return rows, meta


# ---------------------------------------------------------------- 写回

# ---------------------------------------------------------------- Codeware provider
CODEWARE_PACKAGE_RE = re.compile(
    r"\bclass\s+([A-Za-z_]\w*)\s+extends\s+ModLocalizationPackage\b")
CODEWARE_PROVIDER_RE = re.compile(
    r"\bclass\s+([A-Za-z_]\w*)\s+extends\s+ModLocalizationProvider\b")
CODEWARE_MODULE_RE = re.compile(
    r"^\s*module\s+([A-Za-z_][\w.]*)\s*$", re.M)

PROVIDER_CASE_RE = re.compile(
    r'\bcase\s+n"([^"]+)"\s*:\s*return\s+new\s+([A-Za-z_]\w*)\s*\(\s*\)\s*;',
    re.M)
IMPORT_LINE_RE = re.compile(
    r"^\s*import\s+([A-Za-z_][\w.]*(?:\.\*)?)\s*;?\s*$", re.M)


def _read_reds(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return f.read()


def _write_reds(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _module_name(text):
    m = CODEWARE_MODULE_RE.search(text or "")
    return m.group(1) if m else ""


def _parse_imports(text):
    """解析 REDscript import，返回 [{'module': ..., 'symbol': None|类名}]。"""
    out = []
    for m in IMPORT_LINE_RE.finditer(text or ""):
        raw = m.group(1).strip().rstrip(";").strip()
        if not raw:
            continue
        if raw.endswith(".*"):
            out.append({"module": raw[:-2], "symbol": None})
        elif "." in raw:
            mod, sym = raw.rsplit(".", 1)
            out.append({"module": mod, "symbol": sym})
        else:
            out.append({"module": "", "symbol": raw})
    return out


def _scan_codeware(root):
    """一次扫描所有 .reds，收集语言包类和 Provider 元数据。"""
    packages, providers = [], []
    if not root or not os.path.isdir(root):
        return packages, providers
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in
                       ("redmod_backup", ".git", "node_modules", "__pycache__")]
        for fn in filenames:
            if not fn.lower().endswith(".reds"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                text = _read_reds(path)
            except Exception:
                continue
            rel = os.path.relpath(path, root).replace("\\", "/")
            mod = _module_name(text)
            classes = CODEWARE_PACKAGE_RE.findall(text)
            for cls in classes:
                packages.append({"rel": rel, "abs": path, "module": mod,
                                 "class": cls, "text": text})
            if CODEWARE_PROVIDER_RE.search(text):
                cases = [{"locale": a, "class": b}
                         for a, b in PROVIDER_CASE_RE.findall(text)]
                providers.append({
                    "rel": rel, "abs": path, "module": mod,
                    "classes": CODEWARE_PROVIDER_RE.findall(text),
                    "imports": _parse_imports(text), "cases": cases,
                    "text": text,
                })
    packages.sort(key=lambda x: (x["rel"], x["class"]))
    providers.sort(key=lambda x: (x["rel"], x["text"].find("ModLocalizationProvider")))
    return packages, providers


def _reds_entry(root, pkg):
    rel = pkg["rel"]
    stem = os.path.splitext(os.path.basename(rel))[0]
    return {
        "rel": rel,
        "abs": pkg["abs"],
        "dir": os.path.dirname(rel).replace("\\", "/"),
        "locale": canonical_locale(stem) or "",
        "format": "reds",
    }


def _uniq_packages(items):
    out, seen = [], set()
    for p in items:
        key = (p.get("rel"), p.get("class"))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _resolve_package(pkg_index, provider, class_name):
    """按同模块 → 显式 import → import * 的顺序解析语言包类。"""
    same = _uniq_packages(pkg_index.get((provider.get("module", ""), class_name), []))
    if len(same) == 1:
        return same[0], "same-module"
    if len(same) > 1:
        return None, "同模块存在多个同名语言包类"
    explicit, wildcard = [], []
    for imp in provider.get("imports") or ():
        key = (imp.get("module", ""), class_name)
        if imp.get("symbol") == class_name:
            explicit.extend(pkg_index.get(key, []))
        elif imp.get("symbol") is None:
            wildcard.extend(pkg_index.get(key, []))
    explicit = _uniq_packages(explicit)
    if len(explicit) == 1:
        return explicit[0], "explicit-import"
    if len(explicit) > 1:
        return None, "多个显式 import 中存在同名语言包类"
    wildcard = _uniq_packages(wildcard)
    if len(wildcard) == 1:
        return wildcard[0], "wildcard-import"
    if len(wildcard) > 1:
        return None, "多个 import * 中存在同名语言包类"
    return None, "找不到 Provider 返回的语言包类"


def _choose_source_case(cases, target_locale):
    """优先 en-US；否则选择第一个非目标语言分支。"""
    normalized = []
    for case in cases or ():
        loc = canonical_locale(case.get("locale", "")) or (case.get("locale") or "")
        normalized.append((loc, case))
    for loc, case in normalized:
        if loc == "en-US":
            return loc, case
    for loc, case in normalized:
        if loc and loc != target_locale:
            return loc, case
    return "", None


def discover_provider_pairs(root, target_locale):
    """按 Codeware Provider 自动发现「源语言包 → Provider → 目标语言包」。

    返回 ``(pairs, issues)``。配对依据是 Provider 的 case 语言分支所返回的
    类名，再按 module / import 解析到实际语言包文件，不按文件名猜测。
    """
    packages, providers = _scan_codeware(root)
    if not packages or not providers:
        return [], []

    pkg_index = {}
    for pkg in packages:
        pkg_index.setdefault((pkg.get("module", ""), pkg.get("class", "")), []).append(pkg)

    pairs, issues = [], []
    by_source = {}
    for prov in providers:
        loc, case = _choose_source_case(prov.get("cases") or (), target_locale)
        if not case:
            issues.append({"provider": prov["rel"], "reason": "没有可用的非目标语言分支"})
            continue
        pkg, how = _resolve_package(pkg_index, prov, case["class"])
        if not pkg:
            issues.append({
                "provider": prov["rel"],
                "class": case["class"],
                "locale": loc,
                "reason": how,
            })
            continue
        entry = _reds_entry(root, pkg)
        tgt_rel = target_rel(entry, target_locale)
        tgt_class = _reds_class_for_target(tgt_rel, target_locale)
        pair = {
            "id": "%s:%s" % (pkg.get("module") or pkg["rel"], pkg["class"]),
            "provider": prov["rel"],
            "providers": [prov["rel"]],
            "provider_module": prov.get("module", ""),
            "provider_class": (prov.get("classes") or [""])[0],
            "source": pkg["rel"],
            "source_module": pkg.get("module", ""),
            "source_class": pkg["class"],
            "source_locale": loc,
            "target": tgt_rel,
            "target_class": tgt_class,
            "target_locale": target_locale,
            "match": how,
            "source_entry": entry,
        }
        key = pkg["rel"]
        old = by_source.get(key)
        if old:
            if prov["rel"] not in old["providers"]:
                old["providers"].append(prov["rel"])
            continue
        by_source[key] = pair
        pairs.append(pair)

    pairs.sort(key=lambda x: (x["provider"], x["source"]))
    issues.sort(key=lambda x: (x.get("provider", ""), x.get("class", "")))
    return pairs, issues


def _reds_class_for_target(target_rel, target_locale=None):
    """Return the language class name used by a copied Codeware package."""
    stem = os.path.splitext(os.path.basename(target_rel.replace("\\", "/")))[0]
    prefix = _locale_prefix(stem)
    suffix = stem[len(prefix):] if prefix else stem
    cand = ""
    if target_locale and target_locale in TARGET_NAMES:
        cand = TARGET_NAMES[target_locale].get("reds") or ""
    if not cand:
        cand = suffix
    cand = re.sub(r"[^A-Za-z0-9_]", "_", cand or "Localized")
    if not cand or cand[0].isdigit():
        cand = "_" + cand
    return cand


def _provider_matches(root, src_module, target_dir):
    """Find ModLocalizationProvider files that can see the source package."""
    out = []
    if not src_module:
        return out
    src_rel_dir = src_module.replace(".", "/").lower()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in
                       ("redmod_backup", ".git", "node_modules", "__pycache__")]
        for fn in filenames:
            if not fn.lower().endswith(".reds"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                txt = _read_reds(p)
            except Exception:
                continue
            if not CODEWARE_PROVIDER_RE.search(txt):
                continue
            mod = _module_name(txt)
            has_import = bool(re.search(
                r"^\s*import\s+" + re.escape(src_module) + r"\.\*\s*;?\s*$",
                txt, re.M))
            same_dir = os.path.normcase(os.path.abspath(dirpath)) == \
                os.path.normcase(os.path.abspath(target_dir))
            score = 0
            if mod == src_module:
                score += 100
            if has_import:
                score += 50
            if not mod and same_dir:
                score += 10
            if score:
                out.append((score, p, txt))
    out.sort(key=lambda x: (-x[0], x[1]))
    return out


def _add_provider_import(text, src_module):
    if not src_module:
        return text
    mod = _module_name(text)
    if mod == src_module:
        return text
    pat = re.compile(r"^\s*import\s+" + re.escape(src_module) + r"\.\*\s*;?\s*$",
                     re.M)
    if pat.search(text):
        return text
    lines = text.splitlines(keepends=True)
    nl = "\r\n" if "\r\n" in text else "\n"
    insert_at = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("module "):
            insert_at = i + 1
            break
    for i, line in enumerate(lines):
        if line.strip().startswith("import "):
            insert_at = i + 1
    lines.insert(insert_at, "import %s.*%s" % (src_module, nl))
    return "".join(lines)


def _add_provider_case(text, locale, class_name):
    """Add/repair a language case in a ModLocalizationProvider switch."""
    tag = (locale or "").lower()
    if not tag:
        return text, False
    case_pat = re.compile(
        r'^(\s*)case\s+n"' + re.escape(tag) +
        r'"\s*:\s*return\s+new\s+([A-Za-z_]\w*)\s*\(\s*\)\s*;\s*$',
        re.M)
    m = case_pat.search(text)
    if m:
        if m.group(2) == class_name:
            return text, False
        repl = '%scase n"%s": return new %s();' % (m.group(1), tag, class_name)
        return text[:m.start()] + repl + text[m.end():], True
    # A multiline/format-variant case may already exist. Do not create a
    # duplicate case just because its whitespace differs from our pattern.
    if re.search(r'\bcase\s+n"' + re.escape(tag) + r'"\s*:', text):
        return text, False
    lines = text.splitlines(keepends=True)
    default_idx = None
    default_indent = ""
    case_indent = ""
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith('case n"en-us"'):
            case_indent = line[:len(line) - len(line.lstrip())]
        if s.startswith("default:"):
            default_idx = i
            default_indent = line[:len(line) - len(line.lstrip())]
            break
    if default_idx is None:
        return text, False
    indent = case_indent or default_indent
    nl = "\r\n" if "\r\n" in text else "\n"
    new_line = '%scase n"%s": return new %s();%s' % (
        indent, tag, class_name, nl)
    lines.insert(default_idx, new_line)
    return "".join(lines), True


def prepare_codeware_package(root, src_rel, tgt_rel, target_locale=None,
                             provider_rel=None, provider_rels=None,
                             before_write=None):
    """Make a copied Codeware package loadable in the target language.

    - rename the copied package class from ``English`` to e.g.
      ``ChineseSimplified`` so REDscript no longer reports SYM_REDEFINITION;
    - add ``case n"zh-cn": return new ChineseSimplified();`` to the matching
      ModLocalizationProvider so the translated package is actually loaded.

    ``provider_rel`` / ``provider_rels`` lock the operation to the Provider(s)
    discovered during extraction. Without them the old narrow matcher is used.
    ``before_write(path)`` is called immediately before each modified file is
    written, allowing the caller to build an undo snapshot.
    Safe to call repeatedly.
    """
    report = {"applied": False, "class": "", "providers": [],
              "target_changed": False, "provider_changed": False, "error": ""}
    if not src_rel.lower().endswith(".reds") or not tgt_rel.lower().endswith(".reds"):
        return report
    src = os.path.join(root, src_rel.replace("/", os.sep))
    dst = os.path.join(root, tgt_rel.replace("/", os.sep))
    if not os.path.exists(src) or not os.path.exists(dst):
        return report
    try:
        src_text = _read_reds(src)
        dst_text = _read_reds(dst)
    except Exception:
        return report
    if not CODEWARE_PACKAGE_RE.search(src_text) or "this.Text(" not in src_text:
        return report
    m = CODEWARE_PACKAGE_RE.search(dst_text)
    if not m:
        return report
    report["applied"] = True
    locale = target_locale
    if not locale:
        try:
            locale = canonical_locale(os.path.splitext(os.path.basename(tgt_rel))[0])
        except Exception:
            locale = None
    class_name = _reds_class_for_target(tgt_rel, locale)
    report["class"] = class_name
    src_module = _module_name(src_text)
    target_dir = os.path.dirname(dst)

    # 先检查显式 Provider，避免发现无效后再改目标文件导致半完成状态。
    exact_rels = list(provider_rels or ())
    if provider_rel:
        exact_rels.append(provider_rel)
    exact_rels = list(dict.fromkeys(x for x in exact_rels if x))
    candidates = []
    if exact_rels:
        for rel in exact_rels:
            prov_path = os.path.join(root, rel.replace("/", os.sep))
            if not os.path.isfile(prov_path):
                report["error"] = "指定的 Provider 不存在: %s" % rel
                return report
            try:
                prov_text = _read_reds(prov_path)
            except Exception as ex:
                report["error"] = "读取 Provider 失败: %s (%s)" % (rel, ex)
                return report
            if not CODEWARE_PROVIDER_RE.search(prov_text):
                report["error"] = "指定文件不是 ModLocalizationProvider: %s" % rel
                return report
            candidates.append((999, prov_path, prov_text))
    else:
        candidates = _provider_matches(root, src_module, target_dir)

    def write_text(path, text):
        if before_write:
            before_write(path)
        _write_reds(path, text)

    if m.group(1) != class_name:
        dst_text = dst_text[:m.start(1)] + class_name + dst_text[m.end(1):]
        write_text(dst, dst_text)
        report["target_changed"] = True

    for _score, prov_path, prov_text in candidates:
        new_text = _add_provider_import(prov_text, src_module)
        new_text, changed_case = _add_provider_case(
            new_text, locale, class_name)
        if new_text != prov_text:
            if before_write:
                before_write(prov_path + ".bak")
            try:
                E.backup_file(prov_path)
            except Exception:
                pass
            write_text(prov_path, new_text)
            report["provider_changed"] = True
        has_target_case = bool(re.search(
            r'\bcase\s+n"' + re.escape((locale or "").lower()) + r'"\s*:',
            new_text, re.I))
        if exact_rels and not has_target_case:
            report["error"] = "Provider 中没有可插入语言的 switch/default 分支: %s" % (
                os.path.relpath(prov_path, root).replace("\\", "/"))
        if changed_case or new_text != prov_text:
            report["providers"].append(
                os.path.relpath(prov_path, root).replace("\\", "/"))
    return report


def ensure_target(root, entry_rel, target_rel_path):
    """目标文件不存在时, 从源文件复制一份"""
    src = os.path.join(root, entry_rel.replace("/", os.sep))
    dst = os.path.join(root, target_rel_path.replace("/", os.sep))
    if os.path.exists(dst):
        return dst
    os.makedirs(os.path.dirname(dst) or root, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def write_json_target_ex(root, src_rel, tgt_rel, mapping):
    """把译文写进目标 json, 返回 (成功 key 集合, 失败 key 集合, 错误信息)。"""
    src = os.path.join(root, src_rel.replace("/", os.sep))
    dst = os.path.join(root, tgt_rel.replace("/", os.sep))
    keys = list(mapping.keys())
    try:
        with open(src, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return set(), set(keys), "json 解析失败: " + src_rel
    written = set()
    for key, val in mapping.items():
        # 1) 扁平结构: 键本身就带点 (MCM 那种 "settings.section.access")
        if isinstance(data, dict) and key in data:
            data[key] = val
            written.add(key)
            continue
        # 2) 嵌套结构: 按路径写回
        try:
            _set_path(data, key, val)
            written.add(key)
        except Exception:
            continue
    try:
        os.makedirs(os.path.dirname(dst) or root, exist_ok=True)
        with open(dst, "w", encoding="utf-8", newline="") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as ex:
        return set(), set(keys), str(ex)
    return written, set(keys) - written, ""


def write_json_target(root, src_rel, tgt_rel, mapping):
    """兼容旧调用: 返回 (成功条数, 错误信息)。"""
    written, _failed, err = write_json_target_ex(root, src_rel, tgt_rel, mapping)
    return len(written), err
