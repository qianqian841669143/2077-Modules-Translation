# -*- coding: utf-8 -*-
"""ui_dict.py —— 模组文本 → red4ext 分片词典 → 可直接安装的压缩包。

这一层只做一件事：把「模组里能提取到的文本」整理成插件认得的词典，
不再回写任何模组文件，也不再产出 CET 账本。

词典分片规则（必须和插件侧 ModKeyFromFullName 完全一致）：
    文件里有 module X   -> 键 = X
    没有 module 有 class Y -> 键 = Y
    两样都没有          -> 进 _global.tsv

插件运行时从调用链取 "DigitalVixenCore.DVCStatusWindow::Update"
的第一段（第一个 '.' 之前）或 "ME3Bar::Show" 的 "::" 之前部分，
得到的正是这里的键。

输出布局：
    <out>/dict/_global.tsv
    <out>/dict/<key>.tsv
每条一行：<英文原文>\t<中文译文>，UTF-8。
"""
from __future__ import annotations

import json
import os
import re

try:
    from . import mod_text_engine as ENGINE
    from . import text_noise as NOISE
except ImportError:                     # 单文件入口
    import mod_text_engine as ENGINE
    import text_noise as NOISE

# redscript 的归属声明。module 优先——它同时是运行时 fullName 的第一段。
_MODULE_RE = re.compile(r"^[ \t]*module[ \t]+([A-Za-z_][\w]*)", re.M)
_CLASS_RE = re.compile(
    r"^[ \t]*(?:(?:public|private|protected|abstract|final|native|static)\s+)*"
    r"class[ \t]+([A-Za-z_][\w]*)",
    re.M,
)

GLOBAL_KEY = "_global"


def owner_key_of_text(text: str) -> str:
    """从一份 .reds 源码里取出它所属的模组键；取不到返回空串。"""
    m = _MODULE_RE.search(text)
    if m:
        return m.group(1)
    m = _CLASS_RE.search(text)
    if m:
        return m.group(1)
    return ""


# module / class 声明都在文件最前面，没必要为了拿归属把 66 MB 的 .reds 再读一遍。
_OWNER_HEAD_BYTES = 16384


def owner_key_of_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            head = f.read(_OWNER_HEAD_BYTES)
    except OSError:
        return ""
    return owner_key_of_text(head)


# 模组自带的界面语言文件：bin\x64\plugins\cyber_engine_tweaks\mods\<Mod>\lang\en.txt
# 内容是 tab.title=Frame Generation SM86 这种 key=value，现有引擎完全不收。
_LANG_TXT_RE = re.compile(r"^(en|en-us|en_us|english)\.txt$", re.I)


def collect_lang_txt(root):
    """扫 <root>/**/lang/en*.txt，返回 {模块键: {原文: 位置}}。

    模块键取 lang 的父目录名（FrameGenSM86）；运行时调用链上的键如果正好
    是这个名字就能精确命中，对不上就落全局，不会更糟。
    """
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in ENGINE.SKIP_DIRS]
        if os.path.basename(dirpath).lower() != "lang":
            continue
        key = os.path.basename(os.path.dirname(dirpath))
        for fn in filenames:
            if not _LANG_TXT_RE.match(fn):
                continue
            try:
                with open(os.path.join(dirpath, fn), "r",
                          encoding="utf-8", errors="replace", newline="") as f:
                    text = f.read()
            except OSError:
                continue
            bucket = out.setdefault(key, {})
            for raw in text.splitlines():
                line = raw.strip()
                if not line or line[0] in "#;/" or "=" not in line:
                    continue
                _k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'").strip()
                if v:
                    bucket.setdefault(v, "lang/%s" % fn)
    return out


def collect(root, target_locale="", include_lua=True, loose=False):
    """扫一个模组根目录，返回 (fragments, meta)。

    target_locale 默认留空：模组正文里内联的中文（case 0: return "额叶皮质"）
    同样是有效证据，不该在提取阶段就被吞掉；中文条目最终由 write_dicts
    按「key 必须是英文」统一挡掉。

    loose 默认关闭：开启后会把没有位置证据的长句也收进来，实测多出的
    一万条里绝大多数是代码注释和调试输出（"-- camera motion"、
    "%s %d older entries not shown"），界面上一辈子不会出现。
    精确档只收能证明「这串是 UI 文案」的调用，命中率反而更高。

    fragments: {key: {原文: 上下文}}，同一个原文在同一个 key 下只留一条。
    """
    data, meta = ENGINE.extract_root_ex(
        root,
        include_lua=include_lua,
        loose=loose,
        target_locale=target_locale,
        keep_target=False,
    )

    fragments = {}

    for rel, items in data.items():
        if not items:
            continue
        full = os.path.join(root, rel.replace("/", os.sep))
        dirname = os.path.dirname(rel)
        key = ""
        if rel.lower().endswith(".reds"):
            key = owner_key_of_file(full)
        else:
            # lua/json/yaml/xml 没有 module 声明。以前这里拿目录名当键
            # （modules / Data / Text / UI），跟运行时调用链上的类名永远对不上，
            # 分出来的字典一次都命中不了 —— 统一落全局，别造垃圾分片。
            key = ""

        bucket = fragments.setdefault(key, {})
        for _line, typ, ctx, src in items:
            if not src:
                continue
            # 日志、资源转储、调试行、SQL、路径清单这类一律不进词典：
            # 它们不会出现在界面上，翻了也是白占体积。
            if NOISE.hard_family(src):
                continue
            if src not in bucket:
                bucket[src] = ctx or typ

    # 模组自带的 lang/en.txt（key=value）——帧生成这类 MOD 的整个界面文本都在里面，
    # 现有引擎按 .reds/.lua/.json 认文件，根本不看这种。
    for key, items in collect_lang_txt(root).items():
        bucket = fragments.setdefault(key, {})
        for src, ctx in items.items():
            if NOISE.hard_family(src):
                continue
            bucket.setdefault(src, ctx)

    return fragments, meta


def load_memory(path):
    """memory.json -> {原文: 译文}。只认两边都非空、且译文含汉字的。"""
    if not os.path.exists(path):
        return {}
    try:
        raw = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for v in (raw.get("entries") or {}).values():
        if not isinstance(v, dict):
            continue
        s = (v.get("s") or "").strip()
        d = (v.get("t") or "").strip()
        if not s or not d or s == d:
            continue
        if not has_cjk(d):
            continue
        out.setdefault(s, d)
    return out


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf" for c in text)


def _clean(text: str) -> str:
    """词典是 <key>\\t<value>\\n，源文里不能出现真 tab / 换行。"""
    for ch in ("\t", "\n", "\r", "\x1f", "\x00"):
        text = text.replace(ch, " ")
    while "  " in text:
        text = text.replace("  ", " ")
    return text


def write_dicts(out_dir, fragments, memory, translated=None):
    """把分片写成 tsv。返回统计。

    fragments: {key: {原文: ctx}}
    memory:    {原文: 译文}
    translated: {原文: 新译文}，优先级最高
    """
    translated = translated or {}
    os.makedirs(out_dir, exist_ok=True)

    stats = {"files": 0, "entries": 0, "missing": 0, "missing_samples": []}
    for key, items in sorted(fragments.items()):
        rows = {}
        for src, _ctx in items.items():
            s = _clean(src)
            if not s or has_cjk(s):
                continue
            dst = translated.get(src) or memory.get(src)
            if not dst:
                stats["missing"] += 1
                if len(stats["missing_samples"]) < 200:
                    stats["missing_samples"].append(s)
                continue
            d = _clean(dst)
            rows[s] = d
            # 游戏侧传进来的串常常带首尾空格（" See the message"）。
            # 原样那条照留，另外补一条去空格的，两种写法都能命中；
            # 已经存在同文条目就不覆盖，避免把空格版挤掉。
            t = s.strip()
            if t and t != s:
                rows.setdefault(t, d)
        if not rows:
            continue
        pair_list = sorted(rows.items())
        name = "%s.tsv" % (key if key else GLOBAL_KEY)
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for s, d in pair_list:
                f.write(s + "\t" + d + "\n")
        stats["files"] += 1
        stats["entries"] += len(pair_list)
    return stats


def build_all(mods_root, memory, out_dir, target_locale="", progress=None,
              translated=None):
    """扫 mods_root 下每个模组目录，合并成分片词典写到 out_dir。

    这是「批量翻译」的落盘步骤：全量重扫重翻后整包重打。
    单模组那一步不调这里，只动自己那一片。

    translated: {原文: 译文}，优先级高于 memory，放本次新翻的条目。
    """
    names = sorted(d for d in os.listdir(mods_root)
                   if os.path.isdir(os.path.join(mods_root, d)))
    merged = {}
    stats = {"mods": 0, "mods_with_text": 0}
    for i, name in enumerate(names):
        try:
            frags, _meta = collect(os.path.join(mods_root, name),
                                   target_locale=target_locale)
        except Exception:
            stats["mods"] += 1
            continue
        stats["mods"] += 1
        if not frags:
            continue
        stats["mods_with_text"] += 1
        for key, items in frags.items():
            bucket = merged.setdefault(key, {})
            for src, ctx in items.items():
                bucket.setdefault(src, ctx)
        if progress is not None and (i % 10) == 0:
            progress(i, len(names))
    stats.update(write_dicts(out_dir, merged, memory, translated))
    stats["buckets"] = len(merged)
    return stats


def pack(zip_path, package_dir):
    """把 package_dir 打成 Vortex 能直接装的 zip（路径一律 '/'）。"""
    import zipfile

    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, dirs, files in os.walk(package_dir):
            dirs.sort()
            files.sort()
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, package_dir).replace("\\", "/")
                z.write(full, rel)
    return zip_path
