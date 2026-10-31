# -*- coding: utf-8 -*-
"""
i18n.py —— 界面本地化(文本即键)

约定:
  * 源码里写中文原文, 用 tr("中文原文") 包起来;
  * locales/*.json 里给出各语言的 {中文原文: 译文} 映射;
  * 找不到译文时原样返回中文(所以漏翻只会退回中文, 不会报错);
  * 用户可在 <程序目录>/userdata/locales/ 放自己的 json 追加语言。
"""
import glob
import json
import os
import sys

FALLBACK = "zh-CN"

_state = {"lang": FALLBACK, "catalog": {}, "langs": {}, "loaded": False}


def bundled_dir():
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "locales")


def user_dir():
    import appconfig
    return os.path.join(appconfig.data_dir(), "locales")


def load_locales(force=False):
    """扫描内置 + 用户目录下的 *.json（进程内缓存，避免启动时重复读盘）"""
    if _state.get("loaded") and not force:
        return _state["langs"]
    langs = {}
    for d in (bundled_dir(), user_dir()):
        if not os.path.isdir(d):
            continue
        for path in sorted(glob.glob(os.path.join(d, "*.json"))):
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    continue
                code = data.get("code") or os.path.splitext(os.path.basename(path))[0]
                strings = data.get("strings") or {}
                if not isinstance(strings, dict):
                    strings = {}
                langs[code] = {
                    "code": code,
                    "name": data.get("name") or code,
                    "strings": {str(k): str(v) for k, v in strings.items()},
                }
            except Exception:
                continue
    _state["langs"] = langs
    _state["loaded"] = True
    return langs


def available():
    """[(code, name)] —— 中文永远排第一"""
    load_locales()
    items = sorted(_state["langs"].items(),
                   key=lambda kv: (kv[0] != FALLBACK, kv[1]["name"].lower()))
    return [(code, info["name"]) for code, info in items]


def set_language(code):
    load_locales()
    if code not in _state["langs"]:
        code = FALLBACK
    _state["lang"] = code
    _state["catalog"] = _state["langs"].get(code, {}).get("strings", {})
    return code


def current():
    return _state["lang"]


def catalog():
    """当前语言的完整映射(给前端用)"""
    return dict(_state["catalog"])


def tr(text):
    """把中文原文翻成当前语言; 没有译文则原样返回"""
    if not _state["catalog"]:
        return text
    return _state["catalog"].get(text, text)


def detect_system():
    """按 Windows 界面语言猜一个默认值"""
    try:
        if sys.platform.startswith("win"):
            import ctypes
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            primary = lang_id & 0x3FF
            if primary == 0x04:          # 中文
                return "zh-CN"
            if primary == 0x09:          # 英文
                return "en-US"
    except Exception:
        pass
    return FALLBACK
