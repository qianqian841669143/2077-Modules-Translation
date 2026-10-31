# -*- coding: utf-8 -*-
"""undo_store.py — 写回事务快照与撤销。"""
import json
import os
import shutil
import time
import uuid

from appconfig import config_dir

MAX_TRANSACTIONS = 30
_MANIFEST = "manifest.json"


def undo_root():
    return os.path.join(config_dir(), "undo")


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def _inside(root, path):
    try:
        return os.path.commonpath([_norm(root), _norm(path)]) == _norm(root)
    except Exception:
        return False


def _rel(root, path):
    return os.path.relpath(os.path.abspath(path), os.path.abspath(root)).replace("\\", "/")


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default

class ApplyTransaction(object):
    """一次写回操作的文件快照。"""

    def __init__(self, root, label=""):
        self.root = os.path.abspath(root or "")
        self.label = label or "应用到模组"
        self.created_ns = time.time_ns()
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        self.dir = os.path.join(undo_root(), self.id)
        self.files_dir = os.path.join(self.dir, "files")
        self.entries = []
        self._seen = set()
        self.committed = False
        os.makedirs(self.files_dir, exist_ok=True)

    def snapshot(self, path):
        """在写文件之前调用。重复调用同一路径只保存第一次状态。"""
        if not path:
            return False
        abspath = os.path.abspath(path)
        if not _inside(self.root, abspath):
            return False
        key = _norm(abspath)
        if key in self._seen:
            return True
        self._seen.add(key)
        rel = _rel(self.root, abspath)
        entry = {"path": rel, "existed": os.path.isfile(abspath)}
        if entry["existed"]:
            snap_name = "%04d.bin" % (len(self.entries) + 1)
            snap = os.path.join(self.files_dir, snap_name)
            shutil.copy2(abspath, snap)
            entry["snapshot"] = snap_name
            entry["size"] = os.path.getsize(abspath)
        self.entries.append(entry)
        return True

    def commit(self):
        """提交事务；没有任何快照时不产生撤销记录。"""
        if self.committed:
            return self.id
        if not self.entries:
            self.discard()
            return ""
        manifest = {
            "version": 1,
            "id": self.id,
            "root": self.root,
            "label": self.label,
            "created_ns": self.created_ns,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "files": self.entries,
        }
        _write_json(os.path.join(self.dir, _MANIFEST), manifest)
        self.committed = True
        _prune()
        return self.id

    def discard(self):
        try:
            shutil.rmtree(self.dir, ignore_errors=True)
        except Exception:
            pass


def _iter_manifests():
    base = undo_root()
    if not os.path.isdir(base):
        return
    for name in os.listdir(base):
        path = os.path.join(base, name, _MANIFEST)
        if os.path.isfile(path):
            data = _read_json(path)
            if isinstance(data, dict) and data.get("id"):
                yield path, data

def _matches_root(root, data):
    if not root or not data.get("root"):
        return False
    try:
        return _norm(root) == _norm(data["root"])
    except Exception:
        return False


def list_transactions(root=None, limit=20):
    out = []
    for _path, data in _iter_manifests():
        if root and not _matches_root(root, data):
            continue
        out.append({
            "id": data.get("id", ""),
            "root": data.get("root", ""),
            "label": data.get("label", ""),
            "created_at": data.get("created_at", ""),
            "files": len(data.get("files") or []),
        })
    out.sort(key=lambda x: x.get("id", ""), reverse=True)
    return out[:max(0, int(limit or 20))]


def earliest_snapshots(root):
    """每个文件「第一次被改之前」的快照文件路径（只对原本存在的文件）。

    返回 {相对路径: 快照绝对路径}。还原翻译在没有可用下载包时靠它兜底：
    事务记录会被裁剪（每个模组保留最近 MAX_TRANSACTIONS 次），但只要最早那
    条还在，原始内容就还在。
    """
    if not root:
        return {}
    items = []
    for path, d in _iter_manifests():
        if _matches_root(root, d):
            items.append((d.get("created_ns", 0), path, d))
    items.sort(key=lambda x: (x[0], x[2].get("id", "")))
    out = {}
    for _ns, path, d in items:
        txdir = os.path.dirname(path)
        for e in d.get("files") or []:
            rel = str(e.get("path") or "").replace("\\", "/").strip()
            if not rel or rel in out or not e.get("existed"):
                continue
            snap = os.path.join(txdir, "files", str(e.get("snapshot") or ""))
            if os.path.isfile(snap):
                out[rel] = snap
    return out


def latest(root):
    items = list_transactions(root, 1)
    return items[0] if items else None


def whitelist(root):
    """这个模组被本工具碰过的所有文件，以及「第一次碰之前它是否存在」。

    还原翻译要先知道「哪些文件值得动」：只碰这里列出来的路径，模组里其它
    文件一个不动。existed=False 表示这个文件是本工具新建的，还原时删掉。

    同一个文件被改过多次时，取最早那次记录的 existed —— 那才是「原始状态」。
    返回 (files, first_at, count)：files 是 {相对路径: existed}。
    """
    if not root:
        return {}, "", 0
    items = []
    for _p, d in _iter_manifests():
        if _matches_root(root, d):
            items.append((d.get("created_ns", 0), d))
    items.sort(key=lambda x: (x[0], x[1].get("id", "")))
    files = {}
    for _ns, d in items:
        for e in d.get("files") or []:
            rel = str(e.get("path") or "").replace("\\", "/").strip()
            if not rel or rel in files:
                continue
            files[rel] = bool(e.get("existed"))
    first_at = items[0][1].get("created_at", "") if items else ""
    return files, first_at, len(items)

def undo_last(root):
    """撤销指定模组目录最近一次写回。返回报告。"""
    if not root:
        return {"ok": False, "error": "没有可用于撤销的模组目录"}
    candidates = []
    for p, d in _iter_manifests():
        if _matches_root(root, d):
            candidates.append((d.get("created_ns", 0), p, d))
    if not candidates:
        return {"ok": False, "error": "没有可撤销的应用记录"}
    candidates.sort(key=lambda x: (x[0], x[2].get("id", "")), reverse=True)
    _created, path, data = candidates[0]

    restored, deleted, errors = [], [], []
    txdir = os.path.dirname(path)
    for entry in data.get("files") or []:
        rel = str(entry.get("path") or "").replace("\\", "/")
        target = os.path.abspath(os.path.join(root, rel.replace("/", os.sep)))
        if not rel or not _inside(root, target):
            errors.append({"path": rel, "reason": "路径越界"})
            continue
        try:
            if entry.get("existed"):
                snap = os.path.join(txdir, "files", str(entry.get("snapshot") or ""))
                if not os.path.isfile(snap):
                    errors.append({"path": rel, "reason": "撤销快照丢失"})
                    continue
                os.makedirs(os.path.dirname(target) or root, exist_ok=True)
                shutil.copy2(snap, target)
                restored.append(rel)
            else:
                if os.path.isfile(target):
                    os.remove(target)
                    deleted.append(rel)
        except Exception as ex:
            errors.append({"path": rel, "reason": str(ex)})

    if errors:
        return {
            "ok": False,
            "error": "撤销未完全成功",
            "restored": restored,
            "deleted": deleted,
            "errors": errors[:50],
        }
    # 全部恢复成功后才删除事务，失败时保留记录供重试。
    try:
        shutil.rmtree(txdir, ignore_errors=True)
    except Exception:
        pass
    return {
        "ok": True,
        "id": data.get("id", ""),
        "created_at": data.get("created_at", ""),
        "restored": restored,
        "deleted": deleted,
        "files": len(restored) + len(deleted),
    }


def _prune():
    """每个模组目录各自保留最近 MAX_TRANSACTIONS 次写回记录。"""
    groups = {}
    for path, data in _iter_manifests():
        key = _norm(data.get("root", ""))
        groups.setdefault(key, []).append((data.get("created_ns", 0), path))
    for items in groups.values():
        items.sort(reverse=True)
        for _ns, path in items[MAX_TRANSACTIONS:]:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)
