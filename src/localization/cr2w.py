# -*- coding: utf-8 -*-
"""cr2w.py —— 官方本地化资源的结构认识。

CP2077 的本地化文件后缀是 .json，实际是 CR2W 二进制。WolvenKit 把它展开成
*.json.json 文本后，结构固定为：

    Data → RootChunk → root → Data → entries

三种 root data 类型（都实测过，见 docs/M0验证.md）：

    localizationPersistenceOnScreenEntries   onscreens/onscreens.json
        条目：{primaryKey, secondaryKey, femaleVariant, maleVariant}
        合并语义：按 primaryKey **原地替换**

    localizationPersistenceSubtitleMap       subtitles/subtitles.json
        条目：{subtitleFile, subtitleGroup}  —— 这是「字幕文件注册表」
        subtitleFile 是资源引用对象，路径在 DepotPath.$value

    localizationPersistenceSubtitleEntries   subtitles/<group>/<file>.json
        条目：{stringId, femaleVariant, maleVariant}
        合并语义：ArchiveXL PushBack **追加**

本模块只负责「读懂 + 定位 + 改文本」，不碰打包（见 builder.py）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

KIND_ONSCREEN = "onscreen"
KIND_SUB_MAP = "subtitle_map"
KIND_SUB_ENTRIES = "subtitle_entries"
KIND_UNKNOWN = "unknown"

_TYPE_TO_KIND = {
    "localizationPersistenceOnScreenEntries": KIND_ONSCREEN,
    "localizationPersistenceSubtitleMap": KIND_SUB_MAP,
    "localizationPersistenceSubtitleEntries": KIND_SUB_ENTRIES,
}

# 官方 onscreen 条目的 $type；我们自己造条目时缺省用它
ONSCREEN_ENTRY_TYPE = "localizationPersistenceOnScreenEntry"
SUB_ENTRY_TYPE = "localizationPersistenceSubtitleEntry"
SUB_MAP_ENTRY_TYPE = "localizationPersistenceSubtitleMapEntry"

# CP2077 文本内嵌富文本标签：<Rich ...>...</> 之类。
# 翻译时必须原样保留，数量对不上就说明译文坏了。
_RICH_RE = re.compile(r"<[^<>\n]{1,400}>")


class Cr2wError(RuntimeError):
    pass


def is_cr2w(path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"CR2W"
    except OSError:
        return False


def load_doc(path) -> dict:
    """读 WolvenKit 展开出来的 *.json.json。"""
    p = Path(path)
    try:
        with open(p, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except OSError as ex:
        raise Cr2wError("读不到 JSON：%s (%s)" % (p.name, ex))
    except ValueError as ex:
        raise Cr2wError("JSON 解析失败：%s (%s)" % (p.name, ex))
    if not isinstance(doc, dict):
        raise Cr2wError("不是 WolvenKit JSON：%s" % p.name)
    return doc


def save_doc(doc, path) -> Path:
    """写回 *.json.json。

    用 ensure_ascii=False + indent=2：WolvenKit 能原样吃回去（M0 已验证），
    同时让中文在编辑器里可读。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return p


def root_data(doc) -> dict:
    """取出 Data.RootChunk.root.Data；结构不对时抛 Cr2wError。"""
    try:
        data = doc["Data"]["RootChunk"]["root"]["Data"]
    except (KeyError, TypeError):
        raise Cr2wError("CR2W 结构异常：缺少 Data.RootChunk.root.Data")
    if not isinstance(data, dict):
        raise Cr2wError("CR2W 结构异常：root.Data 不是对象")
    return data


def kind_of(doc) -> str:
    """按 root.Data 的 $type 判断资源种类。"""
    try:
        typ = root_data(doc).get("$type", "")
    except Cr2wError:
        return KIND_UNKNOWN
    return _TYPE_TO_KIND.get(str(typ), KIND_UNKNOWN)


def type_name(doc) -> str:
    try:
        return str(root_data(doc).get("$type", ""))
    except Cr2wError:
        return ""


def entries(doc) -> list:
    """返回条目列表（原地引用，改它等于改 doc）。"""
    data = root_data(doc)
    es = data.get("entries")
    if not isinstance(es, list):
        raise Cr2wError("CR2W 结构异常：entries 不是数组")
    return es


# ---------------------------------------------------------------- 条目身份


def entry_id(entry):
    """给条目一个稳定标识，用于跨版本对齐（mod 更新后仍能认出来）。

    onscreen  → ("pk", "<primaryKey>")，primaryKey 为 0 时退回 secondaryKey
    subtitle  → ("sid", "<stringId>")
    """
    if not isinstance(entry, dict):
        return ("", "")
    if "stringId" in entry:
        return ("sid", str(entry.get("stringId", "")).strip())
    pk = str(entry.get("primaryKey", "") or "").strip()
    if pk and pk != "0":
        return ("pk", pk)
    sk = str(entry.get("secondaryKey", "") or "").strip()
    return ("sk", sk) if sk else ("", "")


def entry_label(entry) -> str:
    """给人看的短标签：onscreen 用 secondaryKey，字幕用 stringId。"""
    if not isinstance(entry, dict):
        return ""
    sk = str(entry.get("secondaryKey", "") or "").strip()
    if sk:
        return sk
    return str(entry.get("stringId", "") or "").strip()


def entry_group(entry) -> str:
    """onscreens 的 secondaryKey 形如 `UI-Labels-Resume`，取前两段做分组。"""
    sk = str(entry.get("secondaryKey", "") or "")
    parts = sk.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else sk


# ---------------------------------------------------------------- 文本读写


def get_text(entry) -> str:
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("femaleVariant", "") or "")


def get_male(entry) -> str:
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("maleVariant", "") or "")


def set_text(entry, text, male=None) -> None:
    """写入译文。

    CP2077 用 femaleVariant 放默认文本；maleVariant 只在有性别变体时使用。
    若原条目 maleVariant 非空、且我们只给了一份译文，就同步覆盖它，
    避免出现「中文女声版本 / 英文男声版本」的混搭。
    """
    entry["femaleVariant"] = text
    if male is not None:
        entry["maleVariant"] = male
    elif get_male(entry):
        entry["maleVariant"] = text


def is_translatable(entry) -> bool:
    """有文案才算可翻译；纯符号 key（ArchiveXL 用 - / = 开头做指令）要排除。"""
    text = get_text(entry)
    if not text.strip():
        return False
    if text.strip() in ("-", "="):
        return False
    return True


def rich_tags(text) -> list:
    """抽出富文本标签序列，用于校验译文有没有改坏标记。"""
    return _RICH_RE.findall(str(text or ""))


def tags_match(orig, trans) -> bool:
    """译文标签必须与原文完全一致（顺序与内容）。"""
    return rich_tags(orig) == rich_tags(trans)


def tags_ok(orig, trans) -> bool:
    """宽松校验：标签种类与出现次数一致即可，允许随语序调整位置。

    批量翻译是无人值守的，真正的风险是模型把标签吞掉、截断或改名；
    整块 <Rich ...>...</Rich> 跟着从句搬到别处是合理的。所以这里只比数量，
    不锁顺序。要严格比较用 tags_match()。
    """
    a, b = rich_tags(orig), rich_tags(trans)
    if len(a) != len(b):
        return False
    from collections import Counter
    return Counter(a) == Counter(b)


# ---------------------------------------------------------------- 工厂函数


def blank_onscreen_entry(primary_key="0", secondary_key="", text="") -> dict:
    return {
        "$type": ONSCREEN_ENTRY_TYPE,
        "primaryKey": str(primary_key),
        "secondaryKey": secondary_key,
        "femaleVariant": text,
        "maleVariant": "",
    }


def blank_sub_entry(string_id, text="", male="") -> dict:
    return {
        "$type": SUB_ENTRY_TYPE,
        "stringId": str(string_id),
        "femaleVariant": text,
        "maleVariant": male,
    }


def blank_sub_map_entry(subtitle_rel, group="quest", flags="Soft") -> dict:
    """造一条字幕注册表条目。

    坑：subtitleFile 是资源引用对象，路径必须写进 DepotPath.$value。
    写成 subtitleFile.$value 会让 WolvenKit 报
    "JSON value could not be converted to RedFileDto"（M0 踩过）。
    """
    return {
        "$type": SUB_MAP_ENTRY_TYPE,
        "subtitleFile": {
            "DepotPath": {
                "$type": "ResourcePath",
                "$storage": "string",
                "$value": str(subtitle_rel),
            },
            "Flags": flags,
        },
        "subtitleGroup": {
            "$type": "CName",
            "$storage": "string",
            "$value": group,
        },
    }


def sub_map_target(entry) -> str:
    """读出注册表条目指向的字幕文件路径；结构不对返回空串。"""
    if not isinstance(entry, dict):
        return ""
    ref = entry.get("subtitleFile")
    if not isinstance(ref, dict):
        return ""
    depot = ref.get("DepotPath")
    if not isinstance(depot, dict):
        return ""
    return str(depot.get("$value", "") or "")


def sub_map_group(entry) -> str:
    if not isinstance(entry, dict):
        return ""
    g = entry.get("subtitleGroup")
    if not isinstance(g, dict):
        return ""
    return str(g.get("$value", "") or "")


def set_sub_map_target(entry, subtitle_rel) -> None:
    """安全改写注册表条目指向的字幕文件。"""
    ref = entry.get("subtitleFile")
    if not isinstance(ref, dict) or not isinstance(ref.get("DepotPath"), dict):
        raise Cr2wError("subtitleFile 结构异常，无法安全改写")
    ref["DepotPath"]["$value"] = str(subtitle_rel)


# ---------------------------------------------------------------- 语料抽取


class EntryRef(object):
    """抽取结果的一行：够生成 UI 表格，也够回写。

    rel     归档内资源路径（相对归档根，反斜杠）
    kind    KIND_ONSCREEN / KIND_SUB_ENTRIES
    idkind  "pk" / "sk" / "sid"
    eid     主键值
    label   secondaryKey 或 stringId
    group   UI 分组（onscreen 用 secondaryKey 前缀，字幕用 subtitleGroup）
    text    原文
    male    原文的 maleVariant
    """

    __slots__ = ("rel", "kind", "idkind", "eid", "label", "group", "text", "male", "entry")

    def __init__(self, rel, kind, idkind, eid, label, group, text, male, entry):
        self.rel = rel
        self.kind = kind
        self.idkind = idkind
        self.eid = eid
        self.label = label
        self.group = group
        self.text = text
        self.male = male
        self.entry = entry

    def as_row(self) -> dict:
        """转成 UI 用的一行；字段名与现有 web_app 的行结构保持一致的部分尽量复用。"""
        return {
            "kind": "resource",
            "file": self.rel,
            "disp": self.rel,
            "entryId": self.eid,
            "entryIdKind": self.idkind,
            "label": self.label,
            "ctx": self.group,
            "type": self.kind,
            "orig": self.text,
            "trans": "",
            "maleOrig": self.male,
        }


def collect(doc, rel, group="") -> list:
    """从一份 CR2W JSON 里抽出所有可翻译条目，返回 EntryRef 列表。

    字幕注册表（KIND_SUB_MAP）不产出行——它没有文案，只有指针。
    """
    k = kind_of(doc)
    if k == KIND_SUB_MAP:
        return []
    if k not in (KIND_ONSCREEN, KIND_SUB_ENTRIES):
        return []

    out = []
    for e in entries(doc):
        if not is_translatable(e):
            continue
        idk, eid = entry_id(e)
        if not eid:
            continue
        g = group or (entry_group(e) if k == KIND_ONSCREEN else "")
        out.append(EntryRef(
            rel=rel, kind=k, idkind=idk, eid=eid,
            label=entry_label(e), group=g,
            text=get_text(e), male=get_male(e), entry=e,
        ))
    return out


def index_by_id(doc) -> dict:
    """建 {entryId: entry} 索引，回写时按主键定位，避免行号漂移。"""
    idx = {}
    for e in entries(doc):
        idk, eid = entry_id(e)
        if eid:
            idx[(idk, eid)] = e
    return idx


def main(argv=None):
    """诊断用法：python -m localization.cr2w <file.json.json>"""
    import argparse

    ap = argparse.ArgumentParser(description="检查 WolvenKit JSON 的本地化结构")
    ap.add_argument("path")
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args(argv)

    doc = load_doc(args.path)
    k = kind_of(doc)
    es = entries(doc)
    print("类型 :", type_name(doc) or "(未识别)")
    print("种类 :", k)
    print("条目 :", len(es))
    if k == KIND_SUB_MAP:
        for e in es[: args.limit]:
            print("   ->", sub_map_target(e), "|", sub_map_group(e))
        return 0
    shown = 0
    for e in es:
        if not is_translatable(e):
            continue
        idk, eid = entry_id(e)
        print("   %-4s %-22s %-34s %s" % (idk, eid, entry_label(e)[:34], get_text(e)[:40]))
        shown += 1
        if shown >= args.limit:
            break
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
