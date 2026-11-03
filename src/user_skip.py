# -*- coding: utf-8 -*-
"""user_skip.py —— 把「这条是代码, 别再报」这个判断持久化下来。

位置判据（`mod_text_engine.name_position_starts`）只能挡住工具认得出来的名字。
真正翻不了、又不是名字的串（纯符号 `")"`、某个模组特有的协议串、混在句子里的
一段标识符）只能靠人判断。以前的做法是每次都弹一个「应用失败」，用户点完还是
同一条 —— 判断没有落到任何地方。

现在落到 `userdata/user_skip.json`：

    {"<模组目录名小写>": {"<相对路径>": ["<原文 sha1>", ...]}}

键用 (相对路径, 原文 sha1) 而不是行号: 行号会随编辑漂移, 原文不会。
抽取时按这个集合直接过滤, 表格里不再出现, 候选表里也不会再算进「待翻」。

用目录名而不是绝对路径: 模组换个盘符/换个下载目录也还认得出来;
代价是目录改名要重新标记一次, 这种情况很少见, 比「重装一次全丢」好。
"""
import hashlib
import json
import os

import appconfig


def _path():
    return os.path.join(appconfig.config_dir(), "user_skip.json")


def _skip_path():
    """用户勾过「跳过」的行。

    和上面那份「标记为代码」分开存：代码标记是**隐藏**（加载时直接过滤掉），
    跳过只是**默认勾上**（行还在表里、可以取消）。两种判断混在一份文件里，
    取消一次跳过就得猜它当初是哪一类，所以各存各的。
    """
    return os.path.join(appconfig.config_dir(), "user_skips.json")


def sha1_of(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def _mod_key(root):
    """模组标识: 目录名小写。空 root 返回空串（不落任何标记）。"""
    name = os.path.basename(os.path.normpath(str(root or "")))
    return name.strip().lower()


def _load_at(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_at(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _load_all():
    return _load_at(_path())


def _save_all(data):
    return _save_at(_path(), data)


def _load_skips():
    return _load_at(_skip_path())


def _save_skips(data):
    return _save_at(_skip_path(), data)


def marks(root):
    """返回该模组已被标记为代码的 ``{(相对路径小写, sha1)}`` 集合。"""
    mod = _mod_key(root)
    if not mod:
        return set()
    files = _load_all().get(mod) or {}
    out = set()
    for rel, shas in files.items():
        for s in (shas or ()):
            out.add((str(rel).replace("\\", "/").lower(), str(s)))
    return out


def is_marked(root, rel, orig):
    mod = _mod_key(root)
    if not mod:
        return False
    files = _load_all().get(mod) or {}
    rel_key = str(rel).replace("\\", "/").lower()
    return sha1_of(orig) in (files.get(rel_key) or [])


def add(root, rel, orig):
    """标记一条为「代码」。返回 (是否写入成功, 当前该文件标记数)。"""
    mod = _mod_key(root)
    if not mod:
        return False, 0
    rel_key = str(rel).replace("\\", "/").lower()
    data = _load_all()
    files = data.setdefault(mod, {})
    shas = files.setdefault(rel_key, [])
    digest = sha1_of(orig)
    if digest not in shas:
        shas.append(digest)
    ok = _save_all(data)
    return ok, len(shas)


def remove(root, rel, orig):
    mod = _mod_key(root)
    if not mod:
        return False
    rel_key = str(rel).replace("\\", "/").lower()
    data = _load_all()
    files = data.get(mod) or {}
    shas = files.get(rel_key) or []
    digest = sha1_of(orig)
    if digest in shas:
        shas.remove(digest)
        if not shas:
            files.pop(rel_key, None)
        if not files:
            data.pop(mod, None)
        return _save_all(data)
    return True


def count(root):
    return len(marks(root))


# ---------------------------------------------------------------- 勾过「跳过」的行
def skip_marks(root):
    """返回该模组里用户勾过「跳过」的 ``{(相对路径小写, sha1)}`` 集合。"""
    mod = _mod_key(root)
    if not mod:
        return set()
    files = _load_skips().get(mod) or {}
    out = set()
    for rel, shas in files.items():
        for s in (shas or ()):
            out.add((str(rel).replace("\\", "/").lower(), str(s)))
    return out


def set_skip(root, rel, orig, value=True):
    """勾上 / 取消一条「跳过」。返回 (是否写入成功, 该文件记下的条数)。

    用 (相对路径, 原文 sha1) 记，行号漂移不影响；模组目录改名要重记一次
    （和「标记为代码」同一套键，理由见文件头）。
    """
    mod = _mod_key(root)
    if not mod:
        return False, 0
    rel_key = str(rel).replace("\\", "/").lower()
    data = _load_skips()
    files = data.get(mod) or {}
    shas = files.get(rel_key) or []
    digest = sha1_of(orig)
    if value:
        if digest not in shas:
            shas.append(digest)
    elif digest in shas:
        shas.remove(digest)
    if shas:
        files[rel_key] = shas
        data[mod] = files
    else:
        files.pop(rel_key, None)
        if files:
            data[mod] = files
        else:
            data.pop(mod, None)
    ok = _save_skips(data)
    return ok, len(shas)
