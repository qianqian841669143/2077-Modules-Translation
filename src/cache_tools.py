# -*- coding: utf-8 -*-
"""cache_tools.py —— 设置页「缓存清理」的统计与删除。

只碰 userdata 下这些「删了会重新生成 / 纯占地方」的目录：

    cache        归档目录扫描结果（archive_toc.json）
    work         WolvenKit 解包、组包的中间产物
    batch/out    批量翻译的输出副本（下次跑批量会重新生成）
    undo         「撤销上次应用」用的文件快照（删了就不能撤销）
    corpus       官方语料导出（重建官方语料时重新生成）
    logs/reports 日志与报告

**不碰**：memory.json（翻译记忆）、official_tm.sqlite（官方语料）、settings.json、
glossary.json、locales\\ —— 那些不是缓存，各有各的入口（清空翻译记忆、构建官方语料…）。

删除只清目录**里面**的东西，目录本身留着：批量输出目录靠 `.mb-out-root` 印章证明
「这是我们建的」，把印章删了 `batch_clear` 下次就拒绝认领这个目录。
"""
import os
import shutil

# key / 相对 userdata 的目录 / 中文标签与说明（前端用 T() 查英文译文）
SCOPES = (
    {"key": "cache", "dirs": ("cache",), "label": "提取缓存",
     "note": "归档目录的扫描结果，重建很快"},
    {"key": "work", "dirs": ("work",), "label": "中间文件",
     "note": "WolvenKit 解包、组包的中间产物"},
    {"key": "batch", "dirs": ("batch/out",), "label": "批量输出副本",
     "note": "下次批量会重新生成，续跑记录与上一轮报告一并删除",
     "files": ("batch/run.jsonl", "batch/last-run.json")},
    {"key": "undo", "dirs": ("undo",), "label": "撤销记录",
     "note": "删了就不能再「撤销上次应用」"},
    {"key": "corpus", "dirs": ("corpus",), "label": "官方语料导出",
     "note": "重建官方语料时重新生成，需要游戏目录"},
    {"key": "logs", "dirs": ("logs", "reports"), "label": "日志与报告",
     "note": "排错用，删掉不影响功能"},
)

# 目录里要保住的东西（批量输出目录的印章）
KEEP = (".mb-out-root",)


def _abs(base, rel):
    """拼出绝对路径，并确认它没跑出 base 之外。"""
    root = os.path.abspath(str(base))
    path = os.path.abspath(os.path.join(root, *str(rel).split("/")))
    low_root = os.path.normcase(root)
    low_path = os.path.normcase(path)
    if low_path != low_root and not low_path.startswith(low_root + os.sep):
        raise ValueError("路径越界: %s" % (rel,))
    return path


def _du(path):
    """(字节数, 文件数)。读不到的条目跳过，不因为一个坏文件整项失败。

    KEEP 里的文件（`.mb-out-root` 印章）不算——它不是产物，清完就该显示 0。
    """
    total = files = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            if name in KEEP:
                continue
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
            files += 1
    return total, files


def _size_of(path):
    if os.path.isdir(path) and not os.path.islink(path):
        return _du(path)
    try:
        return os.path.getsize(path), 1
    except OSError:
        return 0, 0


def _remove(path):
    """删文件 / 目录 / 目录链接。Windows 上目录符号链接只能 rmdir。"""
    if os.path.islink(path):
        try:
            os.remove(path)
        except OSError:
            os.rmdir(path)
        return
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def scan(base):
    """各类缓存占多少空间。返回 {items: [...], total: 字节}。"""
    items = []
    for scope in SCOPES:
        size = files = 0
        present = []
        for rel in scope["dirs"]:
            path = _abs(base, rel)
            if os.path.isdir(path):
                present.append(rel)
                one_size, one_files = _du(path)
                size += one_size
                files += one_files
        for rel in scope.get("files") or ():
            path = _abs(base, rel)
            if os.path.isfile(path):
                present.append(rel)
                one_size, one_files = _size_of(path)
                size += one_size
                files += one_files
        items.append({"key": scope["key"], "label": scope["label"],
                      "note": scope["note"], "bytes": size, "files": files,
                      "exists": bool(present), "dirs": present})
    return {"items": items, "total": sum(it["bytes"] for it in items)}


def clear(keys, base):
    """清掉选中项的内容（目录本身与 KEEP 里的印章留着）。

    返回 {ok, keys, freed, files, errors}。不认识的 key 直接忽略——
    界面传来的东西不能当成「要删哪些路径」的唯一依据。
    """
    want = set(str(k) for k in (keys or []))
    picked = [s for s in SCOPES if s["key"] in want]
    freed = files = 0
    removed = []
    errors = []
    for scope in picked:
        # 单列的附属文件（batch\run.jsonl 续跑记录：输出没了它就是废纸，
        # 留着会让下一轮把已完成的模组整块跳过，一个产物都不产出）
        for rel in scope.get("files") or ():
            path = _abs(base, rel)
            if not os.path.isfile(path):
                continue
            one_size, one_files = _size_of(path)
            try:
                os.remove(path)
            except OSError as ex:
                errors.append("%s: %s" % (rel, ex))
                continue
            freed += one_size
            files += one_files
        for rel in scope["dirs"]:
            path = _abs(base, rel)
            if not os.path.isdir(path):
                continue
            try:
                names = sorted(os.listdir(path))
            except OSError as ex:
                errors.append("%s: %s" % (rel, ex))
                continue
            for name in names:
                if name in KEEP:
                    continue
                target = os.path.join(path, name)
                one_size, one_files = _size_of(target)
                try:
                    _remove(target)
                except OSError as ex:
                    errors.append("%s: %s" % (name, ex))
                    continue
                freed += one_size
                files += one_files
        removed.append(scope["key"])
    return {"ok": not errors, "keys": removed, "freed": freed, "files": files,
            "errors": errors[:5]}
