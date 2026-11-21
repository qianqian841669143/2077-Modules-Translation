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

# 汉化包自己也会被 Vortex 解压进 staging。它带着我们的运行时目录，被当成模组
# 扫进来就是「翻译器在翻自己的产物」：索引里多出一个「CP2077 Localization
# Pack」，条目还会混进 _global 分片。mod_batch 早就在复制前挡过一道
# （`_is_own_output`），但重建词典和模组清单这两条路没挡 —— 2026-11-21
# 建索引时实测多出这一条。新版包认 red4ext 插件目录，旧版认那三个 CET 入口。
_OWN_PACK_MARKS = (
    os.path.join("red4ext", "plugins", "CP2077UITranslator"),
    os.path.join("bin", "x64", "plugins", "cyber_engine_tweaks", "mods",
                 "zzz_CP2077_Localization"),
    os.path.join("bin", "x64", "plugins", "cyber_engine_tweaks", "mods",
                 "zzz_mod_text_override"),
    os.path.join("bin", "x64", "plugins", "cyber_engine_tweaks", "mods",
                 "zzz_mcm_label_override"),
)


def is_own_pack(root):
    """这个目录是不是我们自己产出的汉化包（不是用户装的模组）。"""
    return any(os.path.isdir(os.path.join(root, m)) for m in _OWN_PACK_MARKS)



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

    # 用户标记「这条是代码，别翻」的集合。键是 (相对路径小写, 原文 sha1)，
    # 和 web_app 里 load_mod 用的是同一份数据。新模组尤其需要：提取规则挡不住
    # 的协议串、纯符号、混在句子里的标识符，只能靠人标一次就永远不再翻。
    skip_keys = set()
    try:
        import user_skip                                    # noqa: PLC0415
        skip_keys = set(user_skip.marks(root)) | set(user_skip.skip_marks(root))
    except Exception:
        pass

    def _is_skipped(rel, text):
        if not skip_keys:
            return False
        try:
            import user_skip                                # noqa: PLC0415
            return (rel.replace("\\", "/").lower(), user_skip.sha1_of(text)) in skip_keys
        except Exception:
            return False

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
            if _is_skipped(rel, src):
                meta["user_skipped"] = meta.get("user_skipped", 0) + 1
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

    # Codeware 的 Provider 语言包（r6/scripts/<Mod>/translations/**/English.reds）。
    #
    # `extract_root_ex` 只认「有 UI 位置证据」的调用，而
    # `this.Text("key", "value")` 的价值全在第二个参数上，它一条都不收；
    # 批量翻译那条路（locale_pack）认得，词典重建这条路不认。结果是
    # 「memory 里有译文、词典里没有条目、界面上还是英文」—— 2026-11-21 在
    # DigitalVixen 的热键面板上撞见：collect 拿到 13 条，locale_pack 有 5,552 条。
    try:
        import locale_pack as _lp
        _lang = target_locale or "zh-CN"
        _files = _lp.detect(root, parse=True, target=_lang)
        _srcs = _lp.pick_sources(_files, _lang)
        if _srcs:
            _rows, _ = _lp.extract(root, _srcs, _lang)
            _owners = {}
            for _r in (_rows or []):
                _s = _r.get("orig") or ""
                if not _s or NOISE.hard_family(_s):
                    continue
                _rel = str(_r.get("file") or "")
                if _rel not in _owners:
                    _full = os.path.join(root, _rel.replace("/", os.sep))
                    _owners[_rel] = (owner_key_of_file(_full)
                                     if _rel.lower().endswith(".reds") else "")
                if _is_skipped(_rel, _s):
                    continue
                fragments.setdefault(_owners[_rel], {}).setdefault(
                    _s, _rel or "locale")
    except Exception:            # noqa: BLE001 - 这一层失败不该让整个模组没词
        pass

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
        # 归一化变体：memory 的 key 是**源码原文**（`"a\nb"` 里那个是反斜杠+n
        # 两个字符），而提取器现在把转义还原成了真换行、_clean 再变成空格。
        # 不补这一条，249 条带换行的长文本会从「已翻译」整片掉回「待翻译」。
        t = _clean(ENGINE.unescape_text(s))
        if t != s:
            out.setdefault(t, d)
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


# 运行时漏网串里的两类噪声，读取 miss.tsv 时过滤掉（不影响正常提取）：
#
# 1. 模组自己的更新日志。AMM 的 "What's New" 窗口把整份 changelog 画在 ImGui 上，
#    于是 "Added Favorite button for Decor Presets" 这种成百上千条全被收集进来
#    —— 那是模组说明，不是游戏界面，翻它没有意义。
# 2. 实时读数。`Textures: 4.4 GB` / `135 FPS` 每帧都在变，同一个前缀收出几十个
#    数字变体。
_CHANGELOG_RE = re.compile(
    r'^(?:Added|Fixed|Improved|Removed|Updated|Changed|Reverted|Reworked|Restored)\b',
    re.I)
_READING_RE = re.compile(
    r'^(?:Textures|VRAM|FPS|Memory|Disk)\s*[:=]|\b\d+(?:\.\d+)?\s*(?:FPS|GB|MB)\b', re.I)


def _pick_text(translated, memory, src, s):
    """取译文：先按原文形态查，再按清理后的形态查一次。

    提取端现在会把源码里的 `\\n` 还原成真换行、`_clean` 再变成空格，所以
    memory 里按旧形态（字面反斜杠 n）存的那批译文用 src 查不到。load_memory
    已经补了归一化变体（key 是空格形态），这里必须拿 s 再查一次才接得上 ——
    否则 240 多条带换行的长文本会整片掉回「待翻译」。
    """
    v = translated.get(src) or memory.get(src)
    if v is None and s != src:
        v = translated.get(s) or memory.get(s)
    return v


def _add_upper_variant(rows, s, d):
    """补一条全大写形态的 key。

    游戏 UI 会按样式把标签转成全大写：GangRetaliation 的
    `this.Text("GR-MenuIntel", "Intel gathered")` 在面板上显示成
    `INTEL GATHERED`，`"Vacant"` 显示成 `VACANT`，`"Boss"` 显示成 `BOSS`。
    词典里只存源码的原大小写、DLL 又是逐字节比较，那批标签就一直是英文。

    只给短标签补（<=24 字符、最多 3 个词、纯 ASCII、原文确实含小写）。
    UI 的大写样式只用在标题/按钮这类短文本上，长句不会被大写；放宽到
    32 字符会让词典凭空涨一半（实测 +11,683 条），多出来的全是长句变体。
    """
    if len(s) > 24 or s.count(" ") > 2:
        return
    if not all(ord(c) < 128 for c in s):
        return
    if not any(c.islower() for c in s):
        return
    u = s.upper()
    if u != s:
        rows.setdefault(u, d)


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
            dst = _pick_text(translated, memory, src, s)
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
            _add_upper_variant(rows, s, d)
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
              translated=None, runtime_miss=None):
    """扫 mods_root 下每个模组目录，合并成分片词典写到 out_dir。

    这是「批量翻译」的落盘步骤：全量重扫重翻后整包重打。
    单模组那一步不调这里，只动自己那一片。

    translated: {原文: 译文}，优先级高于 memory，放本次新翻的条目。
    runtime_miss: 运行时补漏的原文列表，见函数末尾那段注释。

    返回值多一个 per_mod：{目录名: {"buckets": {分片键: 提取条数},
    "missing": 还没译文的条数}}。新架构下没有账本，这份统计就是「这个模组
    进了哪些分片、还差几行」的唯一记录，模组列表靠它显示条数。
    """
    translated = translated or {}
    names = sorted(d for d in os.listdir(mods_root)
                   if os.path.isdir(os.path.join(mods_root, d))
                   and not is_own_pack(os.path.join(mods_root, d)))
    merged = {}
    per_mod = {}
    stats = {"mods": 0, "mods_with_text": 0}
    for i, name in enumerate(names):
        try:
            frags, _meta = collect(os.path.join(mods_root, name),
                                   target_locale=target_locale)
        except Exception:
            stats["mods"] += 1
            per_mod[name] = {"buckets": {}, "missing": 0}
            continue
        stats["mods"] += 1
        # 提取到多少条、其中几条现在还没有译文：模组列表拿这两个数画进度。
        # 不必等 write_dicts 写完再反推，这里按同一套判据（有译文才落盘）数就行。
        #
        # 差额还要**按分片**分开数（missingBy）：面板展开时会列「<分片>.tsv」，
        # 只说「这个模组一共还差 5 条」的话，用户看到两片都写「已翻完」，
        # 和外面的「待翻译」自相矛盾 —— 以前就是这样，todo 被写死成 0。
        buckets = {k: len(v) for k, v in (frags or {}).items()}
        miss = 0
        miss_by = {}
        samples = []
        for k, items in (frags or {}).items():
            for src in items:
                s = _clean(src)
                if not s or has_cjk(s):
                    continue
                if not _pick_text(translated, memory, src, s):
                    miss += 1
                    miss_by[k or GLOBAL_KEY] = miss_by.get(k or GLOBAL_KEY, 0) + 1
                    if len(samples) < 5:
                        samples.append(s)
        per_mod[name] = {"buckets": buckets, "missing": miss,
                         "missingBy": miss_by, "missingSample": samples}
        if not frags:
            continue
        stats["mods_with_text"] += 1
        for key, items in frags.items():
            bucket = merged.setdefault(key, {})
            for src, ctx in items.items():
                bucket.setdefault(src, ctx)
        if progress is not None and (i % 10) == 0:
            progress(i, len(names))

    # 运行时补漏：CET 那层入口（src/pack/cet_zh/init.lua）会把「流经 ImGui 但
    # 词典里没有」的串写进 miss.tsv。静态提取看不见它们 —— 三元表达式
    # （`ImGui.Button(Duty.onDuty and 'Go off duty' or 'Go on duty')`）、
    # 别的 mod 自己封装的按钮（`controls.DynamicButton("All LUTs")`）都收不到 ——
    # 但界面上一字不差地显示过，是货真价实的界面文案。
    #
    # 并进全局分片后，它们才进得了「还差几条」、才有机会被翻译；翻完下一轮
    # 落盘就生效。不并的话 miss.tsv 就是个没人读的垃圾文件，白收集。
    for _raw in (runtime_miss or ()):
        s = _clean(str(_raw or ""))
        if not s or has_cjk(s):
            continue
        # 实时读数不进待译：`VRAM: 6.4 / 21.3 (22GB)` / `Textures: 4.4 GB`
        # 每帧都在变，同一个前缀会收出成百个数字变体，翻出来也用不上。
        # 数字多的串一律跳过；正常文案里四个数字很罕见。
        if sum(ch.isdigit() for ch in s) >= 4:
            continue
        # 模组的 changelog（AMM 的 What's New 整份画在 ImGui 上）和实时读数，
        # 都是「玩家看得到但不该翻」的：前者是模组说明，后者每帧都变。
        if _CHANGELOG_RE.match(s) or _READING_RE.search(s):
            continue
        merged.setdefault("", {}).setdefault(s, "runtime")

    stats.update(write_dicts(out_dir, merged, memory, translated))
    stats["buckets"] = len(merged)
    stats["per_mod"] = per_mod
    return stats


def load_tsv(path):
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if "\t" in line:
                s, d = line.split("\t", 1)
                if s and d:
                    out[s] = d
    return out


def merge_into(out_dir, fragments, memory, translated=None):
    """单模组增量：只重写这个模组涉及的分片，其它分片一个字节都不动。

    这是「单模组翻译」的落盘方式。区别于 build_all（全量重扫重建），
    这里读出现有 tsv、把新译文合并进去、再写回，所以不会把别的模组的
    条目冲掉 —— 尤其是 _global.tsv，那是所有模组共用的一片。
    """
    translated = translated or {}
    os.makedirs(out_dir, exist_ok=True)
    stats = {"files": 0, "added": 0, "missing": 0}
    for key, items in fragments.items():
        name = "%s.tsv" % (key if key else GLOBAL_KEY)
        path = os.path.join(out_dir, name)
        existing = load_tsv(path)
        before = len(existing)
        for src, _ctx in items.items():
            s = _clean(src)
            if not s or has_cjk(s):
                continue
            dst = _pick_text(translated, memory, src, s)
            if not dst:
                stats["missing"] += 1
                continue
            d = _clean(dst)
            existing[s] = d
            t = s.strip()
            if t and t != s:
                existing.setdefault(t, d)
        if len(existing) != before:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                for s, d in sorted(existing.items()):
                    f.write(s + "\t" + d + "\n")
            stats["files"] += 1
            stats["added"] += len(existing) - before
    return stats


def write_mod_full(out_dir, fragments, memory, translated=None):
    """单模组「全量更新」：这个模组自己的分片整片重建，_global 只做合并。

    和 merge_into 的区别：那个是只加不减 —— 模组升级后英文变了，旧译文还挂在
    词典里，界面就会显示「新英文配旧中文」。这里按**当前提取结果**重建，
    提取不到的条目直接从分片里消失。

    _global 是所有模组共用的一片，没办到知道哪条属于哪个模组，只能合并。
    """
    translated = translated or {}
    os.makedirs(out_dir, exist_ok=True)
    stats = {"files": 0, "added": 0, "removed": 0, "missing": 0,
             "missingBy": {}, "missingSample": []}

    def _miss(key, src):
        """一条「提取到了但没有译文」：总数、按分片、以及前几条原文。

        面板展开时按分片显示「还差几条」，再列出几条原文，用户才能判断是
        「模型翻不出来的拼接碎片」还是「真漏了」。只给一个总数，两种分不清。
        """
        k = key if key else GLOBAL_KEY
        stats["missingBy"][k] = stats["missingBy"].get(k, 0) + 1
        if len(stats["missingSample"]) < 5:
            stats["missingSample"].append(src)

    for key, items in fragments.items():
        name = "%s.tsv" % (key if key else GLOBAL_KEY)
        path = os.path.join(out_dir, name)
        existing = load_tsv(path)
        before = len(existing)

        if key:
            # 模组自己的分片：整片重建
            rows = {}
            for src in items:
                s = _clean(src)
                if not s or has_cjk(s):
                    continue
                dst = _pick_text(translated, memory, src, s)
                if not dst:
                    stats["missing"] += 1
                    _miss(key, s)
                    continue
                d = _clean(dst)
                rows[s] = d
                t = s.strip()
                if t and t != s:
                    rows.setdefault(t, d)
                _add_upper_variant(rows, s, d)
            if not rows and not existing:
                continue
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                for s, d in sorted(rows.items()):
                    f.write(s + "\t" + d + "\n")
            stats["files"] += 1
            stats["removed"] += max(0, before - len(rows))
            stats["added"] += max(0, len(rows) - before)
        else:
            # 共用分片：合并，只增不减
            changed = 0
            for src in items:
                s = _clean(src)
                if not s or has_cjk(s):
                    continue
                dst = _pick_text(translated, memory, src, s)
                if not dst:
                    stats["missing"] += 1
                    _miss(key, s)
                    continue
                d = _clean(dst)
                if existing.get(s) != d:
                    existing[s] = d
                    changed += 1
                t = s.strip()
                if t and t != s and t not in existing:
                    existing[t] = d
                    changed += 1
                before_up = len(existing)
                _add_upper_variant(existing, s, d)
                if len(existing) != before_up:
                    changed += 1
            if changed:
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    for s, d in sorted(existing.items()):
                        f.write(s + "\t" + d + "\n")
                stats["files"] += 1
                stats["added"] += max(0, len(existing) - before)
    return stats


def merge_tsv_dir(src_dir, out_file, skip=()):
    """把 dict 目录下所有 tsv 合成一个文件，给 CET 那边的入口用。

    CET 的 ImGui 文本没有 module 归属（`ImGui.Text("Enable mod")` 里没有类名），
    分片对它没意义，所以要的是一张总表。但分片里有 `_global` 没有的条目，
    所以全部并进来，先到先得（`_global` 先读，和插件侧的合并顺序一致）。

    skip：不参与合并的分片名（小写比较）。`_archive.tsv` 必须排除 —— 那是
    模组归档里的对话/物品描述，只会出现在游戏原生 UI，ImGui 上一辈子不会显示，
    并进来只是让 Lua 表白大一倍（实测多 634 KB）。

    返回写进去的条数。
    """
    skip = {str(s).lower() for s in (skip or ())}
    names = [n for n in sorted(os.listdir(src_dir))
             if n.lower().endswith(".tsv") and n.lower() not in skip]
    names.sort(key=lambda n: (n != "_global.tsv", n))
    seen = {}
    for name in names:
        try:
            with open(os.path.join(src_dir, name), encoding="utf-8",
                      errors="replace") as f:
                for line in f:
                    line = line.rstrip("\n").rstrip("\r")
                    if not line or line[0] == "#":
                        continue
                    src, _, dst = line.partition("\t")
                    if src and dst and src != dst and src not in seen:
                        seen[src] = dst
        except OSError:
            continue
    parent = os.path.dirname(out_file)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out_file, "w", encoding="utf-8", newline="\n") as f:
        for src, dst in seen.items():
            f.write(src + "\t" + dst + "\n")
    return len(seen)


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
