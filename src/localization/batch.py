# -*- coding: utf-8 -*-
"""batch.py —— 批量队列与版本迁移。

两件事：

1. **批量**：一次把几十个 mod 跑完（探测 → 扫描 → 填译 → 组包 → 校验）。
   单个 mod 失败不影响其他，可随时取消。产物只写输出目录，绝不自动装进游戏。

2. **迁移**：mod 更新后按 `entryId` 比对上次快照，未变的条目直接复用旧译文，
   只对新增/改动条目做机翻，并报告新增/改动/删除的数量。

为什么快照按 `entryId` 而不是按原文文本：原文改一个标点就是新条目，
按文本比对会把它当新句子重翻；而 `entryId` 是稳定的主键，能准确区分
「换了文本」和「换了排版」。翻译记忆仍然按文本走，两者互补。

快照只存 `entryId -> [原文, 译文]`，几十个 mod 也只有几 MB。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from . import archive as archmod
from . import cr2w
from . import pipeline as Lpipe
from . import verifier as Lverify
from . import wkit

TARGET_LANG = "zh-cn"

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class Cancelled(Exception):
    """用户取消。向上抛，由 run() 统一收口。"""


def safe_key(text, fallback="mod"):
    """把名字压成稳定的文件/键名。"""
    t = _SAFE.sub("_", str(text or "")).strip("_").lower()
    return t[:80] or fallback


# ---------------------------------------------------------------- 行处理


def filter_rows(rows, source_lang, skip_target=True):
    """按语言挑出要翻译的行，返回 (rows, 跳过数)。

    与界面里的筛选规则一致：先丢掉目标语言，再优先只留原语言；
    如果这个 mod 压根没有原语言，就把其余语言都留下，
    否则会把「唯一可用的原文」一起滤掉。
    """
    skipped = 0
    keep = []
    for r in rows:
        lg = r.get("lang") or ""
        if skip_target and lg == TARGET_LANG:
            skipped += 1
            continue
        keep.append(r)

    src = (source_lang or "").strip()
    if src:
        from_src = [r for r in keep if (r.get("lang") or "") == src]
        if from_src:
            skipped += len(keep) - len(from_src)
            return from_src, skipped
    return keep, skipped


def snapshot(rows):
    """{entryId: [原文, 译文]}。只留迁移要用的两列，控制体积。"""
    out = {}
    for r in rows:
        eid = str(r.get("entryId") or "")
        if not eid:
            continue
        out[eid] = [str(r.get("orig") or ""), str(r.get("trans") or "")]
    return out


def diff_rows(rows, prev):
    """比对当前行与上次快照，返回统计。"""
    prev = prev or {}
    stats = {"added": 0, "changed": 0, "same": 0, "removed": 0,
             "reused": 0, "first": not bool(prev)}
    seen = set()
    for r in rows:
        eid = str(r.get("entryId") or "")
        old = prev.get(eid) if eid else None
        if not eid or old is None:
            stats["added"] += 1
            continue
        seen.add(eid)
        old_src = old[0] if isinstance(old, list) and old else ""
        if old_src != str(r.get("orig") or ""):
            stats["changed"] += 1
        else:
            stats["same"] += 1
    stats["removed"] = sum(1 for k in prev if k not in seen)
    return stats


def apply_previous(rows, prev, stats):
    """按 entryId 复用上次译文；原文变了的条目不复用（要重翻）。"""
    prev = prev or {}
    n = 0
    for r in rows:
        if str(r.get("trans") or "").strip():
            continue
        old = prev.get(str(r.get("entryId") or ""))
        if not isinstance(old, list) or len(old) < 2:
            continue
        if (old[0] or "") != str(r.get("orig") or ""):
            continue
        dst = (old[1] or "").strip()
        if not dst:
            continue
        r["trans"] = old[1]
        n += 1
    stats["reused"] = n
    return n


# ---------------------------------------------------------------- 持久化


class _JsonStore(object):
    """一个容错的 JSON 小仓库：读不出来就当空，写失败不炸。"""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        self._load()

    def _load(self):
        try:
            if self.path.is_file():
                with self.path.open("r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    self.data = d
        except Exception:
            self.data = {}

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            pass


class Index(_JsonStore):
    """探测缓存。按 (mtime, size) 判失效，mod 没动就不重复解包。"""

    def get(self, key):
        return self.data.get(key)

    def put(self, key, entry):
        self.data[key] = entry

    def drop(self, key):
        self.data.pop(key, None)


class State(_JsonStore):
    """每个 mod 上次出包时的 entryId 快照，用于版本迁移。"""

    def put(self, key, name, rows):
        self.data[key] = {
            "name": name,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "entries": snapshot(rows),
        }

    def entries(self, key):
        item = self.data.get(key) or {}
        e = item.get("entries")
        return e if isinstance(e, dict) else {}

    def summary(self, key):
        item = self.data.get(key) or {}
        ents = item.get("entries") if isinstance(item.get("entries"), dict) else {}
        return {"name": item.get("name") or "", "saved_at": item.get("saved_at") or "",
                "count": len(ents)}

    def drop(self, key):
        self.data.pop(key, None)


# ---------------------------------------------------------------- 探测


def probe(target, cli, workdir, log=None):
    """轻量探测：只解 `*.json`，判断归档里有没有本地化资源。

    不做完整 Session.scan()，因为那个会解整包；批量扫几十个 mod 时
    贴图、音频的搬运时间占九成以上。
    """
    root = Path(workdir)
    raw = root / "raw"
    ser = root / "ser"
    for d in (raw, ser):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    files = wkit.unbundle(cli, target.archive_path, raw, pattern="*.json", log=log)
    if not files:
        return {"total": 0, "types": {}, "langs": {}, "files": 0}

    try:
        wkit.serialize(cli, raw, ser, timeout=1800, log=log)
    except wkit.WkitError:
        return {"total": 0, "types": {}, "langs": {}, "files": len(files)}

    total = 0
    nfiles = 0
    types = {}
    langs = {}

    # serialize 会把目录压平成 `000_x.json.json`，语言信息就丢了。
    # 用文件名反查回解包时的相对路径，才能判断这个 mod 的原文是哪个语言。
    def _stem(name):
        n = str(name or "").lower()
        while n.endswith(".json"):
            n = n[: -len(".json")]
        return n

    raw_rels = {}
    for f in files:
        raw_rels[_stem(Path(f).name)] = f

    def _orig_rel(ser_name):
        s = _stem(ser_name)
        hit = raw_rels.get(s)
        if hit:
            return hit
        m = re.match(r"^\d+_(.+)$", s)
        if m:
            return raw_rels.get(m.group(1), "")
        return ""

    for p in ser.rglob("*.json.json"):
        try:
            doc = cr2w.load_doc(p)
        except cr2w.Cr2wError:
            continue
        kind = cr2w.kind_of(doc)
        if kind not in (cr2w.KIND_ONSCREEN, cr2w.KIND_SUB_ENTRIES, cr2w.KIND_SUB_MAP):
            continue
        if kind == cr2w.KIND_SUB_MAP:
            nfiles += 1
            continue
        lg = Lpipe._lang_of_rel(_orig_rel(p.name[:-5]))
        got = 0
        for e in cr2w.entries(doc):
            if cr2w.is_translatable(e):
                total += 1
                got += 1
        if got:
            nfiles += 1
            types[kind] = types.get(kind, 0) + got
            langs[lg or "?"] = langs.get(lg or "?", 0) + got

    return {"total": total, "types": types, "langs": langs, "files": nfiles}


def list_candidates(game_dir, cli, workdir, index=None, progress=None, cancel=None,
                    xls=None):
    """列出游戏里装了哪些「含本地化资源」的 mod，带探测缓存。

    返回的每一项都能直接喂给 run()。自己生成的 `*_zh-cn` 产物会被跳过，
    否则第二次批量会把译好的成品再翻一遍。

    xls 只用于测试时限定范围，正常调用留空。
    """
    gi = archmod.GameInfo(game_dir)
    xls = list(xls) if xls is not None else gi.installed_xls()
    out = []
    total = len(xls)
    for i, xl in enumerate(xls):
        if cancel is not None and cancel.is_set():
            break
        if progress:
            progress(i, total, xl.name)

        name = xl.name
        for suffix in (".archive.xl", ".xl"):
            if name.lower().endswith(suffix):
                name = name[: -len(suffix)]
                break
        if name.lower().endswith("_%s" % TARGET_LANG):
            continue          # 我们自己出的包，不是待翻源

        item = {"key": safe_key(name), "name": name, "xl": str(xl), "archive": "",
                "total": 0, "types": {}, "langs": {}, "extend": "",
                "error": "", "cached": False}
        try:
            tgt = archmod.resolve_target(xl)
        except Exception as ex:
            item["error"] = str(ex)
            out.append(item)
            continue

        item["name"] = tgt.name or name
        item["extend"] = tgt.extend_name()
        if tgt.archive_path is None:
            item["error"] = "找不到配套的 .archive"
            out.append(item)
            continue

        item["archive"] = str(tgt.archive_path)
        key = safe_key(tgt.archive_path.name)
        item["key"] = key
        try:
            st = tgt.archive_path.stat()
            sig = [int(st.st_mtime), int(st.st_size)]
        except OSError:
            sig = [0, 0]

        cached = index.get(key) if index is not None else None
        if cached and cached.get("sig") == sig and "total" in cached:
            item.update(total=cached.get("total") or 0,
                        types=cached.get("types") or {},
                        langs=cached.get("langs") or {},
                        cached=True)
            out.append(item)
            continue

        try:
            info = probe(tgt, cli, Path(workdir) / "probe" / key)
        except Exception as ex:
            item["error"] = str(ex)
            out.append(item)
            continue

        item.update(total=info["total"], types=info["types"], langs=info["langs"])
        if index is not None:
            index.put(key, {"sig": sig, "total": info["total"],
                            "types": info["types"], "langs": info["langs"],
                            "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        out.append(item)

    if progress:
        progress(total, total, "")
    if index is not None:
        index.save()
    return out


# ---------------------------------------------------------------- 执行


class Ctx(object):
    """跑批次的依赖集合。翻译能力由调用方注入，batch.py 不碰模型配置。"""

    def __init__(self, cli, workdir, outdir, source_lang="en-us",
                 skip_target=True, memory=None, memory_lang="简体中文",
                 official=None, translate_texts=None, state=None,
                 on_event=None, on_progress=None, cancel=None, batch_size=8):
        self.cli = cli
        self.workdir = Path(workdir)
        self.outdir = Path(outdir)
        self.source_lang = source_lang
        self.skip_target = skip_target
        self.memory = memory
        self.memory_lang = memory_lang
        self.official = official
        self.translate_texts = translate_texts
        self.state = state or State(self.workdir / "state.json")
        self.on_event = on_event or (lambda e: None)
        self.on_progress = on_progress or (lambda key, done, total: None)
        self.cancel = cancel
        self.batch_size = batch_size

    def stopped(self):
        return self.cancel is not None and self.cancel.is_set()


def process_one(ctx, cand):
    """跑一个 mod：扫描 → 填译 → 机翻 → 组包 → 校验。"""
    tgt = archmod.resolve_target(cand["xl"])
    if tgt.archive_path is None:
        raise RuntimeError("找不到配套的 .archive")

    key = cand["key"] or safe_key(tgt.name)
    sess = Lpipe.Session(tgt, ctx.cli, ctx.workdir / "run" / key,
                         lang=TARGET_LANG, log=None)
    sess.scan()

    rows, skipped = filter_rows(sess.rows, ctx.source_lang, ctx.skip_target)
    if not rows:
        return {"key": key, "name": tgt.name, "ok": True, "empty": True,
                "rows": 0, "skipped": skipped, "filled": 0, "machine": 0,
                "tm": 0, "official": 0, "failed": 0, "tagFallback": 0,
                "dedup": 0, "failedBatches": 0, "failedRows": 0,
                "migration": {"added": 0, "changed": 0, "same": 0,
                              "removed": 0, "reused": 0, "first": False},
                "archive": "", "xl": "", "problems": [], "warnings": []}

    prev = ctx.state.entries(key)
    stats = diff_rows(rows, prev)
    apply_previous(rows, prev, stats)

    # 记忆与官方基线走同一套优先级：手改 > 官方 > 官方待核 > 机翻记忆。
    # 这里以前是「先用 memory.get() 填满，再让 OfficialTM.fill_rows() 补空行」，
    # 顺序反了：记忆一旦填上，官方译文就没机会覆盖它，等于让机翻压过官方。
    # OfficialTM.fill_rows 也不会分级、不写 transSrc，界面上看不出哪条是官方的。
    # 统一交给 baseline.fill_rows 处理，与单模组那三条路径保持一致。
    tm = 0
    official = 0
    if ctx.memory is not None or ctx.official is not None:
        try:
            import baseline as Lbase
            bst = Lbase.fill_rows(rows, official=ctx.official,
                                  memory=ctx.memory,
                                  memory_lang=ctx.memory_lang,
                                  key="orig", field="trans",
                                  origin_field="transSrc")
            tm = int(bst.get("memory", 0) or 0) + int(bst.get("manual", 0) or 0)
            official = (int(bst.get("official", 0) or 0)
                        + int(bst.get("officialWeak", 0) or 0))
        except Exception:
            tm = official = 0

    todo = [r for r in rows if not str(r.get("trans") or "").strip()]
    machine = 0
    failed = 0
    tag_bad = 0
    dedup_saved = 0
    failed_batches = 0
    failed_rows = 0
    consec = 0
    if todo and ctx.translate_texts is not None:
        # 同一句原文只发给模型一次，翻完再展开回所有重复行。和单模组那条路径
        # （web_app._run_translate）用同一套规则：key 取去空白后的文本用于归组，
        # 发给模型和写进记忆的仍是行上的原始文本。
        groups = {}
        rep_text = {}
        order = []
        for r in todo:
            raw = str(r.get("orig") or "")
            g = raw.strip()
            if g not in groups:
                groups[g] = []
                rep_text[g] = raw
                order.append(g)
            groups[g].append(r)
        dedup_saved = len(todo) - len(order)
        progress_done = 0
        for k in range(0, len(order), ctx.batch_size):
            if ctx.stopped():
                raise Cancelled()
            gkeys = order[k:k + ctx.batch_size]
            texts = [rep_text[g] for g in gkeys]
            try:
                outs = ctx.translate_texts(texts)
            except Cancelled:
                raise
            except Exception as ex:
                # 一批超时不该让整个 mod 作废：包已经解开、前面的批次也翻好了，
                # 一抛到底等于把这些全丢掉。记下来继续跑，最后统一报「N 批待重试」。
                failed_batches += 1
                failed_rows += sum(len(groups[g]) for g in gkeys)
                consec += 1
                progress_done += sum(len(groups[g]) for g in gkeys)
                ctx.on_event({"type": "queue-note", "key": key,
                              "message": str(ex)})
                # 连续失败且一条都没翻出来 = 服务挂了，再跑也是白等。
                if consec >= 5 and machine == 0:
                    raise
                continue
            consec = 0
            for g, out in zip(gkeys, outs or []):
                text = (out or "").strip()
                if not text:
                    failed += len(groups[g])
                    progress_done += len(groups[g])
                    continue
                # 提示词里已经要求原样保留 <Rich ...> 之类占位符，但模型偶尔仍会
                # 吃掉或改写标签。批量是无人值守的，坏标记进游戏会让 tooltip 直接
                # 露出源码，所以这里宁可让这条留原文（英文），也不发出去。
                # 按行校验而不是按组：同一组内的原始写法可能有空白差异。
                good = []
                for r in groups[g]:
                    if not cr2w.tags_ok(str(r.get("orig") or ""), text):
                        tag_bad += 1
                        continue
                    r["trans"] = text
                    machine += 1
                    good.append(r)
                progress_done += len(groups[g])
                if good and ctx.memory is not None:
                    for r in good:
                        ctx.memory.put(r["orig"], ctx.memory_lang, text)
            ctx.on_progress(key, min(progress_done, len(todo)), len(todo))
    elif todo:
        failed = len(todo)

    if ctx.memory is not None:
        ctx.memory.save()

    applied = sess.apply_rows(rows)
    result = sess.build(outdir=ctx.outdir)

    report = None
    try:
        report = Lverify.verify_build(result, sess.last_spec, sess.cli, sess.workdir)
    except Exception:
        report = None
    ok = bool(report.ok) if report is not None else True
    problems = list(report.problems)[:5] if report is not None else []

    ctx.state.put(key, tgt.name, rows)
    ctx.state.save()

    return {"key": key, "name": tgt.name, "ok": ok, "empty": False,
            "rows": len(rows), "skipped": skipped, "filled": applied,
            "machine": machine, "tm": tm, "official": official,
            "dedup": dedup_saved,
            "failedBatches": failed_batches, "failedRows": failed_rows,
            "failed": failed, "tagFallback": tag_bad,
            "migration": stats,
            "archive": str(result.archive or ""), "xl": str(result.xl_path or ""),
            "problems": problems, "warnings": list(result.warnings)[:5]}


def run(ctx, cands):
    """按顺序跑完队列，逐个上报。单个失败不中断。"""
    results = []
    total = len(cands)
    ctx.on_event({"type": "queue-start", "total": total})
    for i, cand in enumerate(cands):
        if ctx.stopped():
            ctx.on_event({"type": "queue-done", "cancelled": True,
                          "done": len(results), "total": total,
                          "results": results})
            return results
        name = cand.get("name") or cand.get("key") or ""
        ctx.on_event({"type": "queue-item", "phase": "start", "index": i,
                      "total": total, "key": cand.get("key"), "name": name})
        t0 = time.time()
        try:
            r = process_one(ctx, cand)
        except Cancelled:
            ctx.on_event({"type": "queue-done", "cancelled": True,
                          "done": len(results), "total": total,
                          "results": results})
            return results
        except Exception as ex:
            import traceback
            traceback.print_exc()
            r = {"key": cand.get("key"), "name": name, "ok": False,
                 "error": str(ex), "rows": 0, "filled": 0, "machine": 0,
                 "tm": 0, "official": 0, "failed": 0,
                 "dedup": 0, "failedBatches": 0, "failedRows": 0,
                 "migration": {"added": 0, "changed": 0, "same": 0,
                               "removed": 0, "reused": 0, "first": False},
                 "archive": "", "xl": "", "problems": [], "warnings": []}
        r["seconds"] = round(time.time() - t0, 1)
        results.append(r)
        ctx.on_event(dict(r, type="queue-item", phase="done", index=i,
                          total=total, done=len(results)))
    ctx.on_event({"type": "queue-done", "cancelled": False,
                  "done": len(results), "total": total, "results": results})
    return results
