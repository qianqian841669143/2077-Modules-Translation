# -*- coding: utf-8 -*-
"""mod_batch.py —— 用「单模组功能」批量处理一批模组目录。

和旧的 localization/batch.py 是两套东西：

* 旧的那条只认 `.archive` 里的 CR2W 本地化，只写输出目录，永远看不到
  `.reds` / `.lua` / 语言包 json —— 本次实测（286 个 staging 目录）它能看到
  74 个 mod / 2,459 行，而单模组的三层提取是 101 个 mod / 16,126 行。
* 这条走的是单模组那套 `_do_load` → `_run_translate` → `_do_apply`：
  先把整个模组目录复制到输出根，再在**副本**上提取、翻译、写回。
  原目录（staging / 游戏目录）全程只读，一个字节都不碰。

队列串行执行，单个失败不影响其他；随时可取消。
"""
from __future__ import annotations

import os
import re
import shutil
import time

# 贴图 / 音频 / 视频 / 字体：不参与翻译，复制时默认跳过。
# 这些文件在 mod 里往往占九成以上体积，复制它们纯属浪费。
SKIP_ASSET_EXT = {
    ".xbm", ".dds", ".png", ".jpg", ".jpeg", ".tga", ".bmp", ".hdr", ".exr",
    ".wav", ".opus", ".ogg", ".mp3", ".wem", ".bnk", ".bank", ".bk2",
    ".usm", ".mp4", ".webm", ".avi", ".bk2", ".ttf", ".otf", ".fnt",
}

# Windows 目录名非法字符
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def safe_name(text, fallback="mod"):
    """把 mod 名压成能当目录名的字符串（保留中文，去掉非法字符）。"""
    t = _BAD.sub("_", str(text or "")).strip(" .")
    return (t[:100] or fallback)


class Cancelled(Exception):
    """用户取消。向上抛，由 run() 统一收口。"""


# ---------------------------------------------------------------- 候选


def list_dirs(staging):
    """列出一个根目录下的一级子目录，作为候选 mod。"""
    if not staging or not os.path.isdir(staging):
        return []
    out = []
    for name in sorted(os.listdir(staging), key=lambda s: s.lower()):
        p = os.path.join(staging, name)
        if os.path.isdir(p):
            out.append({"key": safe_name(name), "name": name, "root": p})
    return out


def _tree_size(root):
    files = 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
                files += 1
            except OSError:
                pass
    return files, total


def probe_text(root, target_code, measure=False):
    """预扫一个目录：能出多少待翻行 / 已有多少中文行，不写任何东西。

    这一步和 `_do_load` 用的是同一套提取器（`locale_pack` +
    `extract_root_ex`），所以预扫出来的行数与真正跑起来时一致。
    只扫明文层；归档层要 WolvenKit 解包，放在执行阶段按开关决定。
    """
    import locale_pack
    import mod_text_engine as E

    loc = 0
    src = 0
    already = 0
    err = ""
    consumed = set()

    try:
        files = locale_pack.detect(root, parse=True, target=target_code)
    except Exception:
        files = []
    try:
        srcs = locale_pack.pick_sources(files, target_code) if files else []
        if srcs:
            lrows, _lmeta = locale_pack.extract(root, srcs, target_code)
            loc = len(lrows or [])
            for r in (lrows or []):
                for k in ("file", "target"):
                    if r.get(k):
                        consumed.add(r[k])
    except Exception as ex:
        err = str(ex)

    try:
        tgt_files = locale_pack.detect(root, parse=False, target=target_code)
    except Exception:
        tgt_files = []
    exclude = set(consumed)
    for f in tgt_files:
        if f.get("locale") == target_code:
            exclude.add(f["rel"])
    try:
        data, meta = E.extract_root_ex(
            root, include_lua=True, loose=True, include_locale_lua=False,
            target_locale=target_code, exclude_rels=exclude, trace=False,
            keep_target=True)
        src = sum(len(v) for v in (data or {}).values())
        already = sum(len(v) for v in ((meta or {}).get("already") or {}).values())
    except Exception as ex:
        err = (err + " | " if err else "") + str(ex)

    out = {"localeRows": loc, "sourceRows": src, "alreadyRows": already,
           "todo": loc + src, "files": 0, "bytes": 0, "error": err}
    if measure and out["todo"]:
        try:
            out["files"], out["bytes"] = _tree_size(root)
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- 复制


def copy_mod(src, dst, with_archive=False, skip_assets=True, cancel=None):
    """把整个模组目录复制到 dst，返回复制统计。

    with_archive=False 时跳过 `.archive`：归档层没开就不需要它，
    而它往往是几十上百 MB 的大头。skip_assets 再跳过贴图/音频。
    跳过的文件不会破坏副本的可用性——副本是覆盖包，缺的文件仍由原 mod 提供。
    """
    src = os.path.abspath(str(src or ""))
    dst = os.path.abspath(str(dst or ""))
    if not os.path.isdir(src):
        raise IOError("源目录不存在: %s" % src)
    stat = {"files": 0, "bytes": 0, "skipped": 0, "own": 0}
    seen = 0
    # 上一轮部署进这个模组的、我们自己新建的文件（语言的包、中文归档）。
    # 只在「路径命中**且**内容没变」时跳过：用户解压还原之后 hash 变了，
    # 它就得作为普通模组内容被读回来，不能因为路径还在清单里就永久跳过。
    own = {}
    try:
        import mod_deploy
        own = {k: v[0] for k, v in mod_deploy.deployed_files(src).items() if v[1]}
    except Exception:
        own = {}
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d.lower() != ".git"]
        rel = os.path.relpath(dirpath, src)
        out_dir = dst if rel == "." else os.path.join(dst, rel)
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError:
            continue
        for fn in filenames:
            seen += 1
            if cancel is not None and not (seen % 200) and cancel.is_set():
                raise Cancelled()
            ext = os.path.splitext(fn.lower())[1]
            # .bak 是上一轮写回时留的备份，不是模组内容。Cyberpunk 按 `*.reds`
            # / `init.lua` 扫描，这些文件不生效，但会把副本撑大 20 MB 上下，
            # 部署时还会被一起拷进游戏目录。
            if ext == ".bak":
                stat["skipped"] += 1
                continue
            if skip_assets and ext in SKIP_ASSET_EXT:
                stat["skipped"] += 1
                continue
            if ext == ".archive" and not with_archive:
                stat["skipped"] += 1
                continue
            sp = os.path.join(dirpath, fn)
            dp = os.path.join(out_dir, fn)
            key = (os.path.join(rel, fn) if rel != "." else fn).replace("/", "\\").lower()
            want = own.get(key)
            if want:
                try:
                    import mod_deploy
                    if mod_deploy.file_sha1(sp) == want:
                        stat["own"] += 1
                        continue
                except Exception:
                    pass
            try:
                shutil.copy2(sp, dp)
            except (OSError, shutil.Error):
                stat["skipped"] += 1
                continue
            stat["files"] += 1
            try:
                stat["bytes"] += os.path.getsize(dp)
            except OSError:
                pass
    return stat


def drop_backups(root):
    """删掉副本里的 `*.bak`，返回删掉的个数。

    副本里的 `.bak` 全是本工具写回时留的（复制阶段已经跳过源目录的 `.bak`），
    所以这里可以无条件清：它不是模组内容，留着只会被一起部署进游戏目录。
    撤销记录同理，批量里已经关掉了（见 `run_one` 里的 `_do_apply`）。
    """
    n = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not fn.lower().endswith(".bak"):
                continue
            try:
                os.remove(os.path.join(dirpath, fn))
                n += 1
            except OSError:
                pass
    return n


# ---------------------------------------------------------------- 执行


class Ctx(object):
    """跑批次的依赖集合。翻译能力由调用方（web_app）注入。"""

    def __init__(self, spawn_worker, out_root, with_archive=False,
                 skip_assets=True, base="", model="", target="简体中文",
                 on_event=None, cancel=None, deploy=False, deploy_dry=True):
        self.spawn_worker = spawn_worker
        self.out_root = out_root
        self.with_archive = with_archive
        self.skip_assets = skip_assets
        self.base = base
        self.model = model
        self.target = target
        self.on_event = on_event or (lambda e: None)
        self.cancel = cancel
        self.worker = None
        self.deploy = deploy            # 跑完把副本按文件覆盖回原模组目录
        self.deploy_dry = deploy_dry    # 只算不写

    def stopped(self):
        return self.cancel is not None and self.cancel.is_set()

    def stage(self, key, name, label, seconds=None):
        """记一条阶段（复制 / 加载 / 机翻 / 写回 / 部署）。

        batch.log 原来只有 start/done 两行，窗口被 C 层带走时只能看出「崩在第
        30 个模组」，看不出崩在复制、加载还是写回。把阶段也落一行，下一轮看
        日志就能指到具体那一步——这类崩溃在 Python 层没有 traceback，日志是
        唯一的现场。
        """
        try:
            self.on_event({"type": "mb-stage", "key": key, "name": name,
                           "label": label, "seconds": seconds})
        except Exception:
            pass


def _skip_reason(ctx, cand):
    """S6：已经部署好、源文件也没动的模组，整块跳过。

    只有「跑完覆盖回原模组」真开着才有意义：部署关掉时产物就在副本里，跳过等于
    这个模组什么都不产出。判据全在部署清单里（`mod_deploy.up_to_date`），不读
    模组内容——上一轮还有漏译的不会跳，用户解压还原过的也不会跳。
    """
    if not (ctx.deploy and not ctx.deploy_dry):
        return ""
    try:
        import mod_deploy
        return mod_deploy.up_to_date(cand.get("root") or "")
    except Exception:
        return ""


def prepare(ctx, cand):
    """复制 + 加载。返回值交给 `finish` 用。

    单独拆出来是为了 S4：上一个模组在等模型的时候，这个函数已经在复制并解析
    下一个模组了（加载 11.8 分钟 / 机翻 7.7 分钟，本来就是两拨不同的资源）。
    这里不碰 `ctx.worker`、不发事件，整条事件流仍由 `run()` 单线程发出。
    """
    dst = os.path.join(ctx.out_root, safe_name(cand.get("name")))
    # 输出目录自 v2.43.0 起由用户指定，下面这句 rmtree 就可能落到模组本体上
    # （目录填成 staging / 游戏目录）。界面层已经按「不与 staging、游戏目录重叠」
    # 校验过，这里再按路径关系挡一次：目标必须在输出目录内部，且不能与源目录
    # 互为祖先/后代——out_root 指到 staging 时，dst 恰好就是那个模组本身。
    out_n = os.path.normcase(os.path.abspath(ctx.out_root))
    dst_n = os.path.normcase(os.path.abspath(dst))
    src_n = os.path.normcase(os.path.abspath(cand.get("root") or ""))
    bad = (not out_n) or (not dst_n.startswith(out_n + os.sep))
    if not bad and src_n:
        bad = (dst_n == src_n or dst_n.startswith(src_n + os.sep)
               or src_n.startswith(dst_n + os.sep)
               # 输出目录是整个安装缓存的祖先（甚至就是缓存根）：dst 会落在
               # 缓存里，清空输出 / 重跑都会删到别的模组
               or src_n == out_n or src_n.startswith(out_n + os.sep))
    if bad:
        raise RuntimeError("输出目录校验失败: %s 会覆盖源目录 %s"
                           % (dst, cand.get("root") or ""))

    prep = {"dst": dst, "cp": {}, "w": None, "load": None,
            "error": "", "skip": "", "seconds": 0.0}
    t0 = time.time()
    skip = _skip_reason(ctx, cand)
    if skip:
        prep["skip"] = skip
        prep["seconds"] = round(time.time() - t0, 2)
        return prep
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)

    cp = copy_mod(cand["root"], dst, with_archive=ctx.with_archive,
                  skip_assets=ctx.skip_assets, cancel=ctx.cancel)
    prep["cp"] = cp
    t_copy = time.time()
    ctx.stage(cand.get("key"), cand.get("name"), "复制",
              round(t_copy - t0, 2))
    if ctx.stopped():
        raise Cancelled()
    if not cp["files"]:
        prep["error"] = "复制后没有任何文件"
        return prep

    w = ctx.spawn_worker()
    prep["w"] = w
    load = w._do_load(dst, {"resource": bool(ctx.with_archive),
                            "lua": True, "loose": True})
    prep["load"] = load
    ctx.stage(cand.get("key"), cand.get("name"), "加载",
              round(time.time() - t_copy, 2))
    if not load or not load.get("ok"):
        prep["error"] = (load or {}).get("error") or "加载失败"
    elif ctx.stopped():
        raise Cancelled()
    prep["seconds"] = round(time.time() - t0, 2)
    return prep


def run_one(ctx, cand, r):
    """跑一个 mod：复制 → 提取 → 机翻 → 写回副本。"""
    finish(ctx, cand, prepare(ctx, cand), r)


def finish(ctx, cand, prep, r):
    """把 `prepare` 的结果翻完、写回、按需部署。"""
    dst = prep.get("dst") or ""
    r["dst"] = dst
    if prep.get("skip"):
        # 什么都不做就算完成：产物已经在原模组里，且和现在这份源文件对得上
        r["ok"] = True
        r["skippedDone"] = prep["skip"]
        return
    cp = prep.get("cp") or {}
    t_work = time.time()
    r.update(copied=cp.get("files", 0), bytes=cp.get("bytes", 0),
             skipped=cp.get("skipped", 0), own=cp.get("own", 0))
    if prep.get("error"):
        r["error"] = prep["error"]
        return

    w = prep.get("w")
    load = prep.get("load") or {}
    rows = load.get("rows") or []
    r["rows"] = len(rows)
    r["tm"] = int(load.get("tmHits") or 0)
    r["already"] = sum(1 for x in rows if x.get("already"))
    todo = [j for j, x in enumerate(rows)
            if not str(x.get("trans") or "").strip()]
    r["todo"] = len(todo)
    if not rows:
        r["empty"] = True

    if ctx.stopped():
        raise Cancelled()
    if todo:
        w._run_translate(todo, ctx.base, ctx.model, ctx.target)
    t_tr = time.time()
    ctx.stage(cand.get("key"), cand.get("name"), "机翻",
              round(t_tr - t_work, 2))
    if ctx.stopped():
        raise Cancelled()

    # already 行本身已经是中文，写回去等于把原文再抄一遍，跳过。
    # 译文与原文相同的行同理：模型没翻出来。放过它们会覆盖目标语言文件
    # 里已经译好的文本（MCM 的 zh-cn.json 被冲成英文就是这个原因）。
    unchanged = [x for x in rows
                 if str(x.get("trans") or "").strip() and not x.get("already")
                 and str(x.get("trans")).strip() == str(x.get("orig") or "").strip()]
    r["untouched"] = len(unchanged)
    # 永远没翻出来的行：机翻后译文仍为空（模型没答）+ 译文与原文相同
    # （模型照抄）。这两种行都会被下面从 items 里剔掉，不写进任何文件，
    # 以前连计数都没有——25 个归档包漏了约 10 条，报告上却显示 failed=0。
    empty_trans = [x for x in rows
                   if not str(x.get("trans") or "").strip()
                   and not x.get("already")]
    r["untranslated"] = len(empty_trans) + len(unchanged)
    r["untranslatedSample"] = [str(x.get("orig") or "")[:60]
                               for x in (empty_trans + unchanged)[:5]]
    items = [x for x in rows
             if str(x.get("trans") or "").strip() and not x.get("already")
             and str(x.get("trans")).strip() != str(x.get("orig") or "").strip()]
    r["machine"] = sum(1 for x in items
                       if (x.get("transSrc") or "") == "machine")
    if items:
        # 批量写的是输出根里的副本：这份副本接下来会被整个部署进游戏目录，
        # 所以不要 `.bak` 也不要撤销记录（两种情况都是 127 份冗余，实测
        # 19 MB / 112 MB）。写错了重跑一次批量即可，撤销在这里没有落脚点。
        res = w._do_apply(items, keep_backup=False, make_undo=False)
        r["baks"] = drop_backups(dst)
        r["applied"] = int(res.get("ok") or 0)
        r["settled"] = int(res.get("settled") or 0)
        r["failed"] = int(res.get("fail") or 0)
        r["errors"] = [str(x) for x in (res.get("errors") or [])][:20]
        # 失败原因必须能落到 journal 里。只有总数的话，881 条失败查不下去：
        # 不知道是「没匹配到原文」还是「代码骨架被拒」还是「目标文件没有该 key」。
        reasons = {}
        for f in (res.get("failed") or []):
            why = str(f.get("reason") or "?").strip() or "?"
            reasons[why] = reasons.get(why, 0) + 1
        r["failReasons"] = dict(sorted(reasons.items(),
                                       key=lambda kv: -kv[1])[:8])
        r["undoId"] = res.get("undoId") or ""
        if res.get("archive"):
            r["archive"] = res.get("archive")
            r["xl"] = res.get("xl") or ""
    t_ap = time.time()
    ctx.stage(cand.get("key"), cand.get("name"), "写回",
              round(t_ap - t_tr, 2))
    if ctx.deploy and items:
        r["deploy"] = _deploy_one(ctx, cand, dst, items, r)
        ctx.stage(cand.get("key"), cand.get("name"), "部署",
                  round(time.time() - t_ap, 2))
    r["ok"] = True


def _deploy_one(ctx, cand, dst, items, r=None):
    """把副本按文件覆盖回原模组目录，返回报告（不抛异常）。

    顺便把这一轮的「行数 / 未译 / 失败」记进部署清单：下一次跑的时候，
    这三个数就是「这个模组能不能整块跳过」的判据（S6）。
    """
    rep = {"ok": False, "dry": bool(ctx.deploy_dry), "overwritten": 0,
           "added": 0, "same": 0, "notes": [], "error": ""}
    root = cand.get("root") or ""
    if not root or not os.path.isdir(root):
        rep["error"] = "原模组目录不存在"
        return rep
    stats = {}
    if r:
        stats = {"rows": int(r.get("rows") or 0),
                 "applied": int(r.get("applied") or 0),
                 "untranslated": int(r.get("untranslated") or 0),
                 "failed": int(r.get("failed") or 0)}
    try:
        import mod_deploy
        got = mod_deploy.deploy_mod(
            dst, root, undo_root=root, dry_run=ctx.deploy_dry,
            pairs_list=[(x.get("orig"), x.get("trans")) for x in (items or [])],
            done=stats)
        rep["ok"] = bool(got.get("ok"))
        rep["overwritten"] = int(got.get("overwritten") or 0)
        rep["added"] = int(got.get("added") or 0)
        rep["same"] = int(got.get("same") or 0)
        rep["notes"] = list(got.get("notes") or [])[:20]
        rep["error"] = got.get("error") or ""
    except Exception as ex:      # noqa: BLE001 - 部署失败不该让整队停
        rep["error"] = str(ex)
    return rep


def run(ctx, cands):
    """按顺序跑完队列，逐个上报。单个失败不中断。

    S4：复制 + 加载下一个模组和翻当前模组**同时进行**。实测一轮 19.5 分钟里
    加载 11.8 分钟、机翻 7.7 分钟，而这两段用的是完全不同的资源（WolvenKit
    子进程与磁盘 vs 模型），串着做等于把短的那一段白等掉。这里留一个预处理
    线程（同一时刻只有一个，别把磁盘打满），`prepare()` 不发事件，整条事件流
    仍是单线程按模组顺序发出。
    """
    from concurrent.futures import ThreadPoolExecutor

    results = []
    total = len(cands)
    ctx.on_event({"type": "mb-start", "total": total, "outRoot": ctx.out_root})
    pool = ThreadPoolExecutor(max_workers=1)

    def submit(i):
        return pool.submit(prepare, ctx, cands[i])

    pending = None
    try:
        if total:
            pending = (0, submit(0))
        for i, cand in enumerate(cands):
            if ctx.stopped():
                break
            name = cand.get("name") or cand.get("key") or ""
            t0 = time.time()
            ctx.on_event({"type": "mb-item", "phase": "start", "index": i,
                          "total": total, "key": cand.get("key"), "name": name})
            r = {"key": cand.get("key"), "name": name, "root": cand.get("root", ""),
                 "ok": False, "error": "", "dst": "", "copied": 0, "bytes": 0,
                 "skipped": 0, "own": 0, "rows": 0, "todo": 0, "already": 0, "tm": 0,
                 "machine": 0, "applied": 0, "settled": 0, "failed": 0, "errors": [],
                 "untouched": 0, "untranslated": 0, "untranslatedSample": [],
                 "failReasons": {}, "deploy": None, "skippedDone": "",
                 "prepSeconds": 0.0, "workSeconds": 0.0,
                 "undoId": "", "empty": False, "archive": "", "xl": "", "baks": 0}
            try:
                if pending is not None and pending[0] == i:
                    prep = pending[1].result()
                else:                    # 上一轮的预处理被取消/没排上
                    prep = prepare(ctx, cand)
                pending = None
                # 拿到这一步的结果就立刻把下一个排上：它会在下面这套翻译、
                # 写回、部署跑完之前就把复制和解析做完。
                if i + 1 < total and not ctx.stopped():
                    pending = (i + 1, submit(i + 1))
                r["prepSeconds"] = prep.get("seconds") or 0.0
                t_work = time.time()
                finish(ctx, cand, prep, r)
                r["workSeconds"] = round(time.time() - t_work, 2)
            except Cancelled:
                r["error"] = "cancelled"
            except Exception as ex:      # noqa: BLE001 - 单个 mod 炸了不该拖垮整队
                import traceback
                traceback.print_exc()
                r["error"] = str(ex)
            r["seconds"] = round(time.time() - t0, 1)
            results.append(r)
            ctx.on_event(dict(r, type="mb-item", phase="done", index=i,
                              total=total, done=len(results)))
    finally:
        # 取消时可能还有一次复制在飞：让它自己看到 cancel 退出，别把线程丢在后台
        pool.shutdown(wait=True)
    ctx.on_event({"type": "mb-done", "total": total, "done": len(results),
                  "cancelled": ctx.stopped(), "results": results})
    return results


# --------------------------------------------------------------- 结果导出
# 一行一个 mod，列固定：导出的是面板上那张表此刻显示的内容（本轮跑完的
# 那些），所以列名写死中文——这是给人看的 Excel，不是界面文案。
EXPORT_COLUMNS = ("模组", "状态", "总行", "待译", "已有中文", "记忆命中",
                  "机翻", "写入", "失败", "未译", "复制文件", "秒",
                  "部署", "覆盖", "新增", "备注")


def export_status(r):
    """一个 mod 的结果 → 面板上「状态」那格的话。"""
    if r.get("error") == "cancelled":
        return "已取消"
    if r.get("error"):
        return "失败"
    if r.get("skippedDone"):
        return "已是最新"
    if not r.get("applied"):
        return "无译文可写"
    return "完成"


def export_rows(results):
    """本轮跑完的 mod → CSV 数据行。

    取消掉的 row 只跑到一半（复制或翻译被停掉），算「没跑完」，不导出；
    还没轮到的压根不在 results 里。备注列收的是该看一眼的信号：整块跳过、
    部署失败或带说明、写回失败的前几条、未译样例。
    """
    out = []
    for r in results or []:
        if not isinstance(r, dict) or r.get("error") == "cancelled":
            continue
        dep = r.get("deploy") if isinstance(r.get("deploy"), dict) else {}
        notes = []
        if r.get("skippedDone"):
            notes.append("跳过：%s" % r["skippedDone"])
        if dep.get("error"):
            notes.append("部署失败：%s" % dep["error"])
        for n in (dep.get("notes") or [])[:5]:
            notes.append("%s: %s" % (n.get("path"), n.get("why")))
        for e in (r.get("errors") or [])[:3]:
            notes.append(str(e))
        if r.get("untranslatedSample"):
            notes.append("未译样例：%s" % " / ".join(
                str(x) for x in r["untranslatedSample"][:3]))
        if not dep:
            how = ""
        elif dep.get("error"):
            how = "失败"
        elif dep.get("dry"):
            how = "预演"
        else:
            how = "已部署"
        out.append([r.get("name") or r.get("key") or "",
                    export_status(r),
                    int(r.get("rows") or 0),
                    int(r.get("todo") or 0),
                    int(r.get("already") or 0),
                    int(r.get("tm") or 0),
                    int(r.get("machine") or 0),
                    int(r.get("applied") or 0),
                    int(r.get("failed") or 0),
                    int(r.get("untranslated") or 0),
                    int(r.get("copied") or 0),
                    r.get("seconds") or 0,
                    how,
                    int(dep.get("overwritten") or 0),
                    int(dep.get("added") or 0),
                    " | ".join(notes)])
    return out
