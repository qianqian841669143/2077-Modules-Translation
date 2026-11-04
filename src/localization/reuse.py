# -*- coding: utf-8 -*-
"""归档产物复用：译文没变就别再让 WolvenKit 重打一遍。

批量里每个带 `.archive` 的模组每一轮都要走「解包 → 注入译文 → 重打包」，
即使归档里一条译文都没变：实测 45 个模组的一轮里，21 个带归档的模组吃掉
242 秒中的 **211 秒（87%）**，而 WolvenKit 一次 CLI 操作本身就是 2.2 秒的
固定开销（8 KB 的归档也一样）。

复用判据三条全中，少一条就老实重打：

* 源归档的 sha1 没变 —— 模组自己没更新；
* 这次要写进归档的译文对（``资源路径|entryId|译文``）与上次一致；
* 上次产出的 ``<mod>_zh-cn.archive``（以及配套的 ``.xl``）还在，且 sha1 与
  记录一致。

记录只存指纹，不存译文，落在 ``userdata/cache/archive_reuse.json``。
任何一项对不上都会退回原来的「重打包」路径，所以记录损坏/过期只会慢一点，
不会写出错包。
"""
import hashlib
import io
import json
import os

VERSION = 1


def store_path():
    try:
        import appconfig
        return os.path.join(appconfig.config_dir(), "cache", "archive_reuse.json")
    except Exception:       # noqa: BLE001
        return ""


def load(path=None):
    """读出 {key: {src, pairs, files}}，读不出来就当空的。"""
    p = path or store_path()
    if not p or not os.path.isfile(p):
        return {}
    try:
        with io.open(p, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:       # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    items = data.get("items")
    return items if isinstance(items, dict) else {}


def save(items, path=None):
    """整份写回。写失败吞掉——最坏情况是下次重打包，不影响正确性。"""
    p = path or store_path()
    if not p:
        return False
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, "items": items}, f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except Exception:       # noqa: BLE001
        return False


def file_sha1(path):
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def mod_key(root, archives):
    """一条记录的键：模组根目录 + 参与组包的归档（都规范化成小写绝对路径）。

    批量里 ``root`` 是输出副本目录，每轮都一样；换个输出目录就会重新打包一次，
    这是有意的保守做法。
    """
    parts = [os.path.normcase(os.path.abspath(str(root or "")))]
    for a in sorted(archives or ()):
        parts.append(os.path.normcase(os.path.abspath(str(a or ""))))
    return "|".join(parts)


def src_hash(archives):
    """源归档指纹：按路径排序后把每个文件的 sha1 串起来。"""
    h = hashlib.sha1()
    for a in sorted(archives or ()):
        h.update(os.path.normcase(os.path.abspath(str(a or ""))).encode("utf-8"))
        h.update(file_sha1(str(a or "")).encode("ascii"))
    return h.hexdigest()


def pairs_hash(rows):
    """这次要写进归档的译文对指纹。

    ``rows`` 是界面行（file / entryId / trans）。排序后再算，所以行的顺序
    变了不算改动；只改译文、只增删一行都会换指纹。
    """
    items = []
    for r in rows or ():
        if str(r.get("kind") or "") != "resource":
            continue
        text = r.get("trans")
        if text is None or not str(text).strip():
            continue
        items.append("%s\x1f%s\x1f%s" % (
            str(r.get("file") or ""), str(r.get("entryId") or ""), str(text)))
    h = hashlib.sha1()
    for one in sorted(items):
        h.update(one.encode("utf-8"))
    return h.hexdigest()


def files_hash(paths):
    """产物指纹：{绝对路径: sha1}，文件不存在记空串。"""
    out = {}
    for p in paths or ():
        out[os.path.abspath(str(p))] = file_sha1(str(p))
    return out


def reusable(rec, src, pairs, files):
    """这条记录还能不能直接用。"""
    if not isinstance(rec, dict):
        return False
    if rec.get("src") != src or rec.get("pairs") != pairs:
        return False
    have = rec.get("files") or {}
    if not have or have != files:
        return False
    # 归档本体必须在（.xl 允许本来就没有，两边都是空串）
    for path, sha in files.items():
        if path.lower().endswith(".archive") and not sha:
            return False
    return True


def remember(key, src, pairs, files, path=None):
    """记下这次成功打包的指纹。"""
    items = load(path)
    items[key] = {"src": src, "pairs": pairs, "files": files}
    return save(items, path)


def forget(key, path=None):
    items = load(path)
    if key in items:
        del items[key]
        return save(items, path)
    return False
