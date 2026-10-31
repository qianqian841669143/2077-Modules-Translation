# -*- coding: utf-8 -*-
"""baseline.py —— 官方译文基线（Translation Memory 的第一层）

为什么要有这一层
================
模组的界面词、物品名、技能名里，有相当一部分和游戏本体一模一样。这些句子
游戏已经翻了，而且译法是官方定的。机翻再翻一遍，只会翻出第二套术语。

实测（302 个模组的真实提取结果）：

    Appearance Menu Mod      156 条 → 48 条命中官方译文（30.8%）
    Mod Configuration Menu   281 条 →  4 条（1.4%）
    Anti-Tracking Breach      67 条 →  0 条（原创文案为主）

所以基线的定位是「又快又准的短界面词」，不是「替掉机翻」。命中一条就省一条
的模型调用，而且质量一定高于机翻。

三层优先级
==========
    手改(manual)  >  官方基线(official)  >  机翻记忆(machine)

用户自己动过手的永远最高——包括他在表格里改过的、也写回模组的那批。
官方基线排在机翻记忆之前：记忆里的东西是机翻攒出来的，official 是官方定的，
同一句有官方译法时没有理由用机翻。

为什么只对简体中文启用
======================
官方语料是 en-us → zh-cn 对齐出来的。目标语言是日语/英语时拿中文去填是错的；
繁体虽然同源，但直出简体再要求用户自己转，属于制造简繁混排，暂时也不启用。
两种情况都返回 available() == False 并给出原因，界面照实显示。
"""
from __future__ import annotations

import re

# 来源标记。tm.Memory 用同一套字符串，两边必须一致。
ORIGIN_MANUAL = "manual"
ORIGIN_OFFICIAL = "official"
# 官方命中但证据偏弱（单词条）：照样填，但要标出来让用户能一眼复核。
# 实测 AMM 的 34 条官方命中里，Mark→马克（官方里是人名）、Pan→潘（姓氏）
# 这种单词漂移是真实存在的；而 Crosshair→准星 这种单词又是对的。
# 分不出对错，就分级 + 可筛选，而不是一刀切丢掉。
ORIGIN_OFFICIAL_WEAK = "official-weak"
ORIGIN_MEMORY = "memory"
ORIGIN_MACHINE = "machine"

ORIGIN_RANK = {
    ORIGIN_MANUAL: 4,
    ORIGIN_OFFICIAL: 3,
    ORIGIN_OFFICIAL_WEAK: 2,
    ORIGIN_MEMORY: 2,
    ORIGIN_MACHINE: 1,
    "": 0,
}

ORIGIN_LABEL = {
    ORIGIN_MANUAL: "手改",
    ORIGIN_OFFICIAL: "官方",
    ORIGIN_OFFICIAL_WEAK: "官方(待核)",
    ORIGIN_MEMORY: "记忆",
    ORIGIN_MACHINE: "机翻",
    "": "未知",
}

# 官方语料只覆盖这一个目标语言
SUPPORTED_TARGETS = {"简体中文"}

# norm_text 由 official_tm 提供，但这里不能硬依赖它：
# 打包时漏了 sqlite3 的话，连「没启用官方基线」的填充都会跟着崩。
# 惰性解析一次，解析不到就退化成去首尾空白（此时也不会有官方命中）。
_NORM_FN = None
_NORM_READY = False


def _normalize(text):
    global _NORM_FN, _NORM_READY
    if not _NORM_READY:
        try:
            from localization.official_tm import norm_text as _fn
            _NORM_FN = _fn
        except Exception:
            _NORM_FN = None
        _NORM_READY = True
    if _NORM_FN is not None:
        return _NORM_FN(text)
    return str(text or "").strip()

# 纯数字/型号/符号没必要走基线；命中了也没什么意思
_JUNK_RE = re.compile(r"^[\s\d\W_]+$", re.UNICODE)


def rank(origin):
    return ORIGIN_RANK.get(origin or "", 0)


def memory_origin(kind):
    """把行上的 transSrc 映射成写进记忆的 origin。

    只有用户真动过手（或老前端压根没带来源）才算 manual。机翻结果必须记
    machine，否则每应用一次，机翻译文就升到最高优先级，官方语料以后再也
    盖不动它——这是最难发现的一种记忆污染。
    """
    k = str(kind or "")
    if k.startswith("official"):
        return k
    if k in (ORIGIN_MACHINE, ORIGIN_MEMORY):
        return ORIGIN_MACHINE
    return ORIGIN_MANUAL


def can_use_official(target_lang):
    """官方基线能不能用在当前目标语言上。返回 (bool, 原因)。"""
    lang = (target_lang or "").strip()
    if not lang:
        return False, "还没有设置目标语言"
    if lang not in SUPPORTED_TARGETS:
        return False, "官方语料只有英文→简体中文，目标语言是「%s」时不套用" % lang
    return True, ""


def load_official(enabled, target_lang):
    """按开关和目标语言取官方库；不可用时返回 (None, 原因)。"""
    if not enabled:
        return None, "已关闭「加载时套用官方语料」"
    ok, why = can_use_official(target_lang)
    if not ok:
        return None, why
    try:
        from localization import official_tm as Lotm
    except Exception as ex:
        return None, "官方语料模块加载失败：%s" % ex
    try:
        tm = Lotm.OfficialTM()
        if not tm.exists():
            return None, "还没有构建官方语料"
        return tm, ""
    except Exception as ex:
        return None, "官方语料库读取失败：%s" % ex


def fill_rows(rows, official=None, memory=None, memory_lang="简体中文",
              key="orig", field="trans", origin_field="transSrc",
              only_empty=True, allow_weak=True):
    """按「手改 > 官方 > 机翻记忆」把译文填进行列表。

    official   OfficialTM 实例，或 None（不参与）
    memory     tm.Memory 实例，或 None
    allow_weak 单词条命中是否也自动填（默认是，但标成 official-weak）
    返回统计 dict，同时给每行写 origin_field。

    字段名用 transSrc 而不是 source：行里已经有 source 表示语言包来源，
    混用会把语言包模式的行数据改坏。
    """
    stats = {"total": len(rows or []), "official": 0, "officialWeak": 0,
             "memory": 0, "kept": 0, "left": 0, "manual": 0}
    if not rows:
        return stats

    def _has(r):
        return bool(str(r.get(field) or "").strip())

    # 1) 逐行过一遍记忆：手改的条目优先级高于官方基线，先把它们定下来
    pending = []
    for r in rows:
        if only_empty and _has(r):
            stats["kept"] += 1
            cur = r.get(origin_field) or ""
            if cur == ORIGIN_MANUAL:
                stats["manual"] += 1
            continue
        src = str(r.get(key) or "")
        entry = None
        if memory is not None and src:
            try:
                entry = memory.get_entry(src, memory_lang)
            except Exception:
                entry = None
        if entry:
            o = entry.get("origin") or ""
            if o == ORIGIN_MANUAL:
                r[field] = entry["text"]
                r[origin_field] = ORIGIN_MANUAL
                stats["manual"] += 1
                continue
        pending.append((r, src, entry))

    # 2) 批量查官方语料（只查还空着的）。要 detailed：靠词数与译法数分级
    hits = {}
    if official is not None and pending:
        texts = [src for _r, src, _e in pending if src]
        try:
            hits = official.lookup_many(texts, detailed=True) or {}
        except Exception:
            hits = {}

    # 3) 落值：官方优先，其次记忆里的机翻
    for r, src, entry in pending:
        info = hits.get(_normalize(src)) if src else None
        if info:
            dst = info.get("dst") or ""
            # 分级：多词短语且官方内部只有一种译法 = 稳；单词 = 待核。
            # 实测单词条会撞上同形的专名/人名（Mark→马克、Pan→潘），
            # 而 Crosshair→准星 又是对的——分不出对错，就标出来别装看不见。
            weak = not (len(src.split()) >= 2 and int(info.get("distinct") or 1) == 1)
            if weak and not allow_weak:
                info = None
            elif dst and not _JUNK_RE.match(dst):
                r[field] = dst
                r[origin_field] = ORIGIN_OFFICIAL_WEAK if weak else ORIGIN_OFFICIAL
                stats["officialWeak" if weak else "official"] += 1
                continue
        if entry:
            r[field] = entry["text"]
            r[origin_field] = entry.get("origin") or ORIGIN_MEMORY
            stats["memory"] += 1
            continue
        stats["left"] += 1
    return stats


def summary(stats, available=True, reason=""):
    """给状态栏的一句话。"""
    if not stats:
        return ""
    bits = []
    total = max(1, stats.get("total") or 1)
    if stats.get("official"):
        bits.append("官方基线 %d 条（%.1f%%）"
                    % (stats["official"], 100.0 * stats["official"] / total))
    if stats.get("officialWeak"):
        bits.append("官方待核 %d 条" % stats["officialWeak"])
    if stats.get("manual"):
        bits.append("沿用你改过的 %d 条" % stats["manual"])
    if stats.get("memory"):
        bits.append("记忆 %d 条" % stats["memory"])
    if stats.get("left"):
        bits.append("待翻 %d 条" % stats["left"])
    if not bits:
        return ""
    line = " · ".join(bits)
    if not available and reason:
        line += "（官方基线未启用：%s）" % reason
    return line
