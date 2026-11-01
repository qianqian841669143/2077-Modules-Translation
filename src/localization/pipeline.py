# -*- coding: utf-8 -*-
"""pipeline.py —— 资源模式的门面，UI 只需要认识这一个类。

    s = Session(target, cli, workdir)
    meta = s.scan()               # 抽条目，拿到 rows
    s.set(row, "译文")            # 或 s.set_many({...})
    result = s.build()            # 出 archive + .xl

设计要点：**只提交改动过的条目**。

ArchiveXL 的合并是逐条目的（Extension.cpp: MergeTextEntry 里覆写或追加），
不是整表替换，所以我们的归档里不需要那份 9 MB 的 onscreens.json 全量副本，
只需要改过的那几条。产物从 9 MB 降到几 KB，批量处理时才可承受。

注意：字幕走的是另一套（注册表 + 文本文件成对），见 builder.SubtitlePair。
"""
from __future__ import annotations

import re
from pathlib import Path

from . import archive as archmod
from . import builder as buildmod
from . import cr2w, wkit, xl as xlmod

DEFAULT_LANG = "zh-cn"

# 我们能处理的资源种类
SUPPORTED = ("onscreen", "subtitle_entries")


class PipelineError(RuntimeError):
    pass


def _norm(text):
    return str(text or "").replace("/", "\\")


_LANG_RE = re.compile(r"^[a-z]{2}-[a-z]{2}$")


def strip_lang(rel):
    """把路径里的语言标记抹成 `*`，用于跨语言配对同一份资源。

        base\\localization\\en-us\\onscreens\\nearby-loot.json
        base\\localization\\zh-cn\\onscreens\\nearby-loot.json
     —— 两者统一成 base\\localization\\*\\onscreens\\nearby-loot.json
    """
    parts = [p for p in _norm(rel).split("\\") if p]
    if not parts:
        return ""
    stem = parts[-1]
    low = stem.lower()
    if low.endswith(".json"):
        low = low[:-5]
    if _LANG_RE.match(low):
        parts[-1] = "*"
    for i, p in enumerate(parts[:-1]):
        if _LANG_RE.match(p.lower()):
            parts[i] = "*"
    return "\\".join(parts).lower()


def pair_existing(rows, target_lang, target_locale="", src_lang="en-us"):
    """把目标语言文件里**已经有的译文**配到源语言行上。

    模组自带 zh-cn 时（本站这类 mod 很常见），以前这几行会被整批丢掉，
    界面于是把它们当成「还没翻」，一键机翻再把作者写好的中文覆盖掉——
    翻得还不如原作者的版本。这里改成：目标语言里已有且确实是中文的，
    直接填进译文列并标 already，机翻只补真正缺的条目。

    只动 src_lang 的行：多语言 mod 里 de-de / fr-fr 这些行同样能被配对（路径
    抹掉语言后一模一样），但它们不是原文，拿去当待翻行会把译文写成中文。

    返回新的行列表，不修改传入的 row（sess.rows 还要用于回写定位）。
    """
    tgt_lang = target_lang or DEFAULT_LANG
    src_lang = src_lang or "en-us"
    have = {}
    for r in rows:
        if (r.get("lang") or "") != tgt_lang:
            continue
        text = str(r.get("orig") or "").strip()
        if not text:
            continue
        key = (strip_lang(r.get("file")), str(r.get("entryId") or ""))
        have.setdefault(key, text)
    if not have:
        return list(rows)

    from mod_text_engine import is_target_language_text as _is_tgt

    out = []
    for r in rows:
        if (r.get("lang") or "") != src_lang:
            out.append(r)
            continue
        orig = str(r.get("orig") or "").strip()
        key = (strip_lang(r.get("file")), str(r.get("entryId") or ""))
        cur = have.get(key)
        # 三种情况都当「没翻」：查不到、与原文一字不差（作者照抄的占位英文）、
        # 不是目标语言的文字（作者把 en-us 复制了一份当 zh-cn）。
        if not cur or cur == orig or not _is_tgt(cur, target_locale or "zh-CN"):
            out.append(r)
            continue
        r = dict(r)
        r["trans"] = cur
        r["already"] = 1
        out.append(r)
    return out


def _lang_of_rel(rel):
    """从资源路径判断语言。

    两种常见形态都要认：
        equipment_ex\\localization\\zh-cn.json          ← 语言在文件名上
        mod\\x\\localization\\zh-cn\\onscreens.json     ← 语言在目录上
    """
    parts = [p for p in _norm(rel).split("\\") if p]
    if not parts:
        return ""
    stem = parts[-1].lower()
    if stem.endswith(".json"):
        stem = stem[:-5]
    if _LANG_RE.match(stem):
        return stem
    for p in parts[:-1]:
        low = p.lower()
        if _LANG_RE.match(low):
            return low
    return ""


class Session(object):
    """一次「打开 mod → 抽条目 → 回写」的会话。

    target   archive.Target
    cli      WolvenKit.CLI.exe 路径
    workdir  工作目录（解包与中间产物都在这里）
    lang     目标语言，默认 zh-cn
    """

    def __init__(self, target, cli, workdir, lang=DEFAULT_LANG, log=None,
                 source_lang="en-us"):
        self.target = target
        self.cli = cli
        self.workdir = Path(workdir)
        self.lang = lang or DEFAULT_LANG
        self.source_lang = (source_lang or "en-us").strip() or "en-us"
        self.log = log

        self.docs = {}          # rel -> doc（原始，未改）
        self.kinds = {}         # rel -> cr2w kind
        self.refs = {}          # rel -> [EntryRef]
        self.rows = []
        self.warnings = []
        self._sub_groups = {}   # rel -> subtitleGroup（从原 mod 的注册表里学到）
        self._byid = {}         # rel -> {entryId: entry}，回写时按主键定位
        self._touched = {}      # rel -> {entryId}，真正被写过的条目
        # 内容真的变了的条目。用户点「应用」时常常整批译文都跟归档里已有的
        # 一模一样（同一份表重复应用），这时不该重建整包：字节和时间戳都变，
        # 玩家看到的东西一个都没变，Vortex 还得重新部署一遍。
        self._dirty = {}        # rel -> {entryId}，内容与原文不同的条目
        self._scanned = False

    # -------------------------------------------------------------- 扫描

    def _say(self, msg):
        if self.log:
            self.log(msg)

    def scan(self):
        """解包并抽出全部可翻译条目。可重复调用，第二次直接返回缓存。"""
        if self._scanned:
            return self.meta

        raw = self.workdir / "raw"
        if raw.exists():
            import shutil
            shutil.rmtree(raw)
        raw.mkdir(parents=True, exist_ok=True)

        self._say("解包 %s" % self.target.name)
        files = archmod.unpack(self.cli, self.target, raw, log=None)
        # 只读源语言 + 目标语言：其余十几种语言序列化一次要 2.8 秒（进程启动
        # 占大头），而它们从来不参与配对，纯属白等。实测 Nearby Auto Loot
        # 19 个语言文件 → 2 个，加载 58.8 s → 约 8 s。
        langs = (self.source_lang, self.lang)
        cands = archmod.candidate_json(files, langs=langs)
        self._say("本地化候选 %d 个（语言 %s）" % (len(cands), "/".join(langs)))

        # 每个候选只序列化一次，doc 留在内存里复用
        docs = {}
        for rel in cands:
            doc = self._load_resource(raw / _norm(rel), rel)
            if doc is not None:
                docs[rel] = doc

        # 第一轮：字幕注册表里登记了 subtitleGroup，先学会，避免后面瞎猜
        for rel, doc in docs.items():
            if cr2w.kind_of(doc) == cr2w.KIND_SUB_MAP:
                self._learn_sub_groups(doc)

        # 第二轮：收集可翻条目
        for rel, doc in docs.items():
            self._collect_resource(rel, doc)

        self._scanned = True
        return self.meta

    def _learn_sub_groups(self, doc):
        for e in cr2w.entries(doc):
            tgt = cr2w.sub_map_target(e)
            if tgt:
                self._sub_groups[_norm(tgt).lower()] = cr2w.sub_map_group(e)

    def _load_resource(self, path, rel):
        """序列化成 JSON 并读回来；失败只记警告，不中断整次扫描。"""
        try:
            ser = wkit.serialize(self.cli, path, self.workdir / "json" / _slug(rel))
        except wkit.WkitError as ex:
            self.warnings.append("序列化失败 %s: %s" % (rel, ex))
            return None
        except Exception as ex:  # noqa: BLE001 - 单个资源坏了不该拖垮整包
            self.warnings.append("序列化异常 %s: %s" % (rel, ex))
            return None
        try:
            return cr2w.load_doc(ser)
        except cr2w.Cr2wError as ex:
            self.warnings.append("解析失败 %s: %s" % (rel, ex))
            return None

    def _collect_resource(self, rel, doc):
        kind = cr2w.kind_of(doc)
        if kind == cr2w.KIND_UNKNOWN:
            self.warnings.append("未知资源类型，跳过 %s (%s)"
                                 % (rel, cr2w.type_name(doc) or "无 $type"))
            return
        if kind == cr2w.KIND_SUB_MAP:
            return  # 注册表没有文案，只是指针

        refs = cr2w.collect(doc, rel, group=self._group_for(rel))
        if not refs:
            return

        # 每行带上「这条来自哪个语言的文件」。多语言 mod（如 EquipmentEx 有 13 种）
        # 全都会被扫出来，不标记语言的话 P2 没法区分原文和目标文。
        res_lang = _lang_of_rel(rel)
        self.docs[rel] = doc
        self.kinds[rel] = kind
        self.refs[rel] = refs
        for r in refs:
            row = r.as_row()
            row["lang"] = res_lang
            self.rows.append(row)
        self._say("%s [%s]: %d 条" % (rel, res_lang or "?", len(refs)))

    def _group_for(self, rel):
        """字幕分组：优先用原 mod 注册表里登记的值，其次按路径猜。"""
        grp = self._sub_groups.get(_norm(rel).lower())
        if grp:
            return grp
        low = _norm(rel).lower()
        m = re.search(r"\\subtitles\\([^\\]+)\\quest\\", low)
        if m:
            return "quest"
        for cand in ("quest", "open_world", "phone", "radio"):
            if "\\%s\\" % cand in low:
                return cand
        return "quest"

    @property
    def meta(self):
        counts = {}
        langs = {}
        for r in self.rows:
            counts[r["type"]] = counts.get(r["type"], 0) + 1
            lg = r.get("lang") or "?"
            langs[lg] = langs.get(lg, 0) + 1
        files = len({r["file"] for r in self.rows})
        return {
            "mode": "resource",
            "target": self.target.name,
            "source": self.target.archive_path.name if self.target.archive_path else "",
            "extend": self.target.extend_name(),
            "files": files,
            "total": len(self.rows),
            "types": counts,
            "languages": langs,
            "lang": self.lang,
            "warnings": list(self.warnings),
        }

    def rows_for(self, lang=None):
        """按语言挑行；lang 为空时返回全部。给 P2 的筛选按钮用。"""
        if not lang:
            return list(self.rows)
        return [r for r in self.rows if (r.get("lang") or "") == lang]

    # -------------------------------------------------------------- 写译文

    def _index(self, rel):
        """{entryId: entry} 缓存。

        必须缓存：官方 onscreens 有 6 万多条，若每条译文都重建索引，
        一次批量回写就是 O(n²)，几十秒卡死。文档结构在扫描后不再变化，
        所以索引可以安全复用。
        """
        cached = self._byid.get(rel)
        if cached is not None:
            return cached
        doc = self.docs.get(rel)
        if doc is None:
            return {}
        idx = {}
        for e in cr2w.entries(doc):
            _idk, eid = cr2w.entry_id(e)
            if eid:
                idx[eid] = e
        self._byid[rel] = idx
        return idx

    def set_text(self, rel, entry_id, text):
        """按 (资源, entryId) 写一条译文；找不到返回 False。"""
        entry = self._index(rel).get(str(entry_id))
        if entry is None:
            return False
        if cr2w.get_text(entry) != text:
            self._dirty.setdefault(rel, set()).add(str(entry_id))
        cr2w.set_text(entry, text)
        self._touched.setdefault(rel, set()).add(str(entry_id))
        return True

    def set_many(self, translations):
        """translations: {(rel, entryId): text}，返回成功条数。"""
        n = 0
        for (rel, eid), text in translations.items():
            if self.set_text(rel, eid, text):
                n += 1
        return n

    def apply_rows(self, rows, collect=None):
        """直接从 UI 行列表回写（行里有 file / entryId / trans）。

        collect 传入 list 时把真正写成功的 (file, entryId) 追加进去，
        调用方据此决定哪些行值得记进翻译记忆——只看条目总数分不出是哪几条。
        """
        n = 0
        for r in rows or []:
            if r.get("kind") and r.get("kind") != "resource":
                continue
            text = r.get("trans")
            if text is None or not str(text).strip():
                continue
            if self.set_text(r.get("file", ""), r.get("entryId", ""), text):
                n += 1
                if collect is not None:
                    collect.append((str(r.get("file") or ""),
                                    str(r.get("entryId") or "")))
        return n

    def changed(self):
        """哪些资源有改动：{rel: [改过的 entryId]}。

        以 set_text() 实际写过、**且内容与原文不同**的条目为准，而不是去看行
        对象的 trans 字段——调用方（UI）传来的行是另一批对象，靠行状态会漏判。
        写过但内容没变的不算改动（见 _dirty 的说明）。
        若一条都没写过，再退回扫描 rows 的 trans 作为兜底。
        """
        if self._touched:
            return {rel: sorted(ids) for rel, ids in self._dirty.items() if ids}
        out = {}
        for r in self.rows:
            if not str(r.get("trans") or "").strip():
                continue
            out.setdefault(r["file"], []).append(r["entryId"])
        return out

    # -------------------------------------------------------------- 出包

    def build(self, name=None, outdir=None, extend=True, log=None):
        """把当前 docs 里改动过的条目打成新 mod。"""
        todo = self.changed()
        if not todo:
            raise PipelineError("没有可提交的译文")

        spec = buildmod.make_spec(
            name or buildmod.default_output_name(self.target.name, self.lang),
            extend=(self.target.xl_path.name
                    if (extend and self.target.xl_path) else ""),
        )

        sub_index = 0
        for idx, (rel, eids) in enumerate(todo.items()):
            kind = self.kinds.get(rel)
            doc = self.docs.get(rel)
            if doc is None:
                continue
            keep = set(str(e) for e in eids)
            partial = _partial_doc(doc, keep)
            if not cr2w.entries(partial):
                self.warnings.append("资源 %s 没有匹配的条目，已跳过" % rel)
                continue

            if kind == cr2w.KIND_ONSCREEN:
                # 必须写到自己的命名空间。若沿用原路径（如
                # quickhack_fixes\localization\en-us.json），我们的归档会和
                # 原 mod 的同名资源互相遮蔽——中文文件顶掉英文文件，
                # 而且我们只带了改动条目，原 mod 的其他文本会整段消失。
                out_rel = _out_rel(self.target.name, self.lang, rel, idx)
                spec.onscreens.append(buildmod.Resource(out_rel, partial))
            elif kind == cr2w.KIND_SUB_ENTRIES:
                sub_index += 1
                stem = re.sub(r"[^0-9A-Za-z_.-]+", "_",
                              Path(_norm(rel)).stem)[:40] or ("sub%d" % sub_index)
                ent_rel = "localization\\%s\\subtitles\\%s_%s.json" % (
                    self.lang, _safe(self.target.name), stem)
                map_rel = "localization\\%s\\subtitles\\%s_map.json" % (
                    self.lang, _safe(self.target.name))
                group = self._group_for(rel)
                map_doc = _blank_sub_map(partial, ent_rel, group)
                spec.subtitles.append(
                    buildmod.SubtitlePair(ent_rel, partial, map_rel, map_doc))
            else:
                self.warnings.append("不支持的类型 %s，已跳过" % rel)

        if spec.is_empty():
            raise PipelineError("没有可打包的资源")

        out = Path(outdir) if outdir else (self.workdir / "out")
        result = buildmod.build(spec, self.cli, self.workdir / "build",
                                out, log=log or self.log)
        result.warnings.extend(self.warnings)
        self.last_spec = spec
        self.last_result = result
        return result


class GroupResult(object):
    """多个会话组包的结果。字段与 `builder.BuildResult` 同名，上层不用改。"""

    def __init__(self, parts, problems=None):
        self.parts = list(parts or [])          # [(Session, BuildResult)]
        self.problems = list(problems or [])

    @property
    def archive(self):
        for _s, r in self.parts:
            if getattr(r, "archive", None):
                return r.archive
        return ""

    @property
    def xl_path(self):
        for _s, r in self.parts:
            if getattr(r, "xl_path", None):
                return r.xl_path
        return ""

    @property
    def resources(self):
        out = []
        for _s, r in self.parts:
            out.extend(getattr(r, "resources", []) or [])
        return out

    @property
    def warnings(self):
        out = list(self.problems)
        for _s, r in self.parts:
            out.extend(getattr(r, "warnings", []) or [])
        return out

    @property
    def skipped(self):
        """转换失败被跳过的资源；调用方要据此把对应的行从「已写入」里扣掉。"""
        out = []
        for _s, r in self.parts:
            out.extend(getattr(r, "skipped", []) or [])
        return out


class SessionGroup(object):
    """一个模组的多个归档会话，对外装作一个 Session。

    一个模组经常不止一个归档（AMM：`AMM_Characters` / `AMM_Patches` /
    `AMM_Shared`，本地化表只在其中一个里）。上层只认「一个会话、一次组包、
    一条撤销记录」，所以这里把 N 个会话包成同样接口：`rows` 拼接、
    `apply_rows` 按行上的 `archive` 分流、`build` 各出各的包（产物名按归档名
    区分，不会互相覆盖）、`changed` 取并集。
    """

    def __init__(self, sessions, lang=DEFAULT_LANG):
        self.sessions = list(sessions or [])
        self.lang = lang or DEFAULT_LANG
        self.last_spec = None
        self.last_result = None

    # -- 与 Session 对齐的只读接口 -------------------------------------

    @property
    def target(self):
        return self.sessions[0].target if self.sessions else None

    @property
    def cli(self):
        return self.sessions[0].cli if self.sessions else None

    @property
    def workdir(self):
        return self.sessions[0].workdir if self.sessions else None

    @property
    def warnings(self):
        out = []
        for s in self.sessions:
            out.extend(s.warnings or [])
        return out

    @property
    def rows(self):
        out = []
        for s in self.sessions:
            out.extend(s.rows)
        return out

    @property
    def meta(self):
        langs, types = {}, {}
        files = 0
        names = []
        for s in self.sessions:
            m = s.meta
            for k, v in (m.get("languages") or {}).items():
                langs[k] = langs.get(k, 0) + int(v or 0)
            for k, v in (m.get("types") or {}).items():
                types[k] = types.get(k, 0) + int(v or 0)
            files += int(m.get("files") or 0)
            if m.get("source"):
                names.append(m["source"])
        return {
            "mode": "resource",
            "target": self.target.name if self.target else "",
            "source": "、".join(names),
            "archives": len(self.sessions),
            "extend": self.target.extend_name() if self.target else "",
            "files": files,
            "total": sum(len(s.rows) for s in self.sessions),
            "types": types,
            "languages": langs,
            "lang": self.lang,
            "warnings": self.warnings,
        }

    def scan(self):
        for s in self.sessions:
            s.scan()
        return self.meta

    # -- 写译文与出包 --------------------------------------------------

    def apply_rows(self, rows, collect=None):
        """按行上的 `archive` 分流到对应会话。

        不能一股脑塞给每个会话：两个归档里可能有同名的资源路径
        （都带一份 `localization_en-us.json`），那样一条译文会被写两遍。
        """
        by_arch, loose = {}, []
        for r in rows or []:
            key = _norm_arch(r.get("archive"))
            if key:
                by_arch.setdefault(key, []).append(r)
            else:
                loose.append(r)
        n = 0
        for s in self.sessions:
            sub = list(by_arch.get(_norm_arch(getattr(s.target, "archive_path", ""))) or [])
            sub.extend(loose)
            if sub:
                n += s.apply_rows(sub, collect=collect)
        return n

    def changed(self):
        out = {}
        for s in self.sessions:
            for rel, ids in (s.changed() or {}).items():
                out.setdefault(rel, set()).update(ids)
        return {rel: sorted(ids) for rel, ids in out.items() if ids}

    def build(self, name=None, outdir=None, extend=True, log=None):
        """各会话各自组包；一个失败不影响其它，失败原因放进 GroupResult。"""
        todo = [s for s in self.sessions if s.changed()]
        if not todo:
            raise PipelineError("没有可提交的译文")
        parts, problems = [], []
        for s in todo:
            try:
                parts.append((s, s.build(name=None, outdir=outdir,
                                         extend=extend, log=log)))
            except Exception as ex:      # noqa: BLE001 - 一个归档炸了不拖垮整包
                problems.append("%s: %s" % (
                    getattr(s.target, "name", "?"), ex))
        if not parts:
            raise PipelineError("；".join(problems) or "没有可提交的译文")
        self.last_spec = parts[0][0].last_spec
        self.last_result = GroupResult(parts, problems)
        return self.last_result


# ------------------------------------------------------------------ 工具


def _norm_arch(path):
    """归档路径归一化，用来把「行」和「会话」对起来。"""
    if not path:
        return ""
    try:
        import os
        return os.path.normcase(os.path.abspath(str(path)))
    except Exception:
        return str(path).lower()


def _safe(text):
    t = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(text or "")).strip("_")
    return (t[:32] or "mod").lower()


def _slug(rel):
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", rel)[-80:]


def _out_rel(mod_name, lang, src_rel, idx):
    """我们自己的归档内路径。

    刻意不复用原 mod 的路径：同名资源会在资源库里互相遮蔽，导致
    原文丢失。放在 l10n\\<mod>\\<lang>\\ 下，与任何 mod 都不冲突。

    _lang_of() 能从中间那段语言码认出 zh-cn，所以 .xl 里会自动归到正确语言。
    """
    stem = re.sub(r"[^0-9A-Za-z_.-]+", "_", Path(_norm(src_rel)).stem)[:40]
    return "l10n\\%s\\%s\\onscreens\\%02d_%s.json" % (
        _safe(mod_name), lang, idx, stem or "onscreens")


def _partial_doc(doc, keep_ids):
    """复制文档骨架，只保留指定 entryId 的条目。

    ArchiveXL 逐条目合并，所以只发改动项即可；这样归档不会因为
    一份 9 MB 的 onscreens 而膨胀。
    """
    import copy

    out = {
        "Header": copy.deepcopy(doc.get("Header", {})),
        "Data": copy.deepcopy({k: v for k, v in doc.get("Data", {}).items()
                               if k != "RootChunk"}),
    }
    root_chunk = doc["Data"]["RootChunk"]
    new_root_data = copy.deepcopy(root_chunk["root"]["Data"])
    kept = []
    for e in cr2w.entries(doc):
        idk, eid = cr2w.entry_id(e)
        if eid in keep_ids:
            kept.append(copy.deepcopy(e))
    new_root_data["entries"] = kept

    new_root = copy.deepcopy(root_chunk["root"])
    new_root["Data"] = new_root_data
    new_chunk = copy.deepcopy(root_chunk)
    new_chunk["root"] = new_root
    out["Data"]["RootChunk"] = new_chunk
    out["Data"].setdefault("EmbeddedFiles", [])
    return out


def _blank_sub_map(entries_doc, entries_rel, group):
    """造一份只含一条的注册表，指向我们的字幕文本文件。"""
    import copy

    shell = copy.deepcopy(entries_doc)  # 只是借 Header/Data 外壳
    data = shell["Data"]["RootChunk"]["root"]["Data"]
    data["$type"] = "localizationPersistenceSubtitleMap"
    data["entries"] = [cr2w.blank_sub_map_entry(entries_rel, group=group)]
    return shell


def open_session(path, cli, workdir, lang=DEFAULT_LANG, log=None):
    """一步到位：解析路径 → 建 Session。"""
    target = archmod.resolve_target(path, workdir=workdir)
    return Session(target, cli, workdir, lang=lang, log=log)


def default_workdir(base=None, target_name="work"):
    """给 UI 用的默认工作目录。"""
    root = Path(base) if base else Path.cwd()
    return root / ".l10n" / (target_name or "work")
