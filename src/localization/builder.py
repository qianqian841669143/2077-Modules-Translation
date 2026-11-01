# -*- coding: utf-8 -*-
"""builder.py —— 把译文做成一个能被 ArchiveXL 加载的 mod。

产物永远是**新增**的一对文件，绝不改写原 mod：

    <name>.archive      我们自己的资源（CR2W）
    <name>.archive.xl   声明这些资源、并用 extend: 挂到原 mod 上

这条设计是刻意的：CMT 那类做法会改写原 mod 的 .xl 并删掉 en-us，
导致英文丢失、mod 一更新译文就废。extend 让两份配置共存。

流程（每种资源一份）：
    doc(dict) → *.json.json → convert deserialize → CR2W → 放进 stage → pack

坑记（都是 M0 实测）：
  * `-o` 目录必须已存在。
  * .xl 不能放在被 pack 的目录里，否则会被一起塞进归档（虽被跳过）。
  * pack 的输出名取自目录名。
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from . import cr2w, wkit, xl as xlmod


class BuildError(RuntimeError):
    pass


def _safe_token(text, fallback="res"):
    """把路径压成一个安全的文件名片段。"""
    t = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(text or "")).strip("_")
    return t[:60] or fallback


def _save_failure(root, res, json_path, ex):
    """把崩溃现场留下来：出错的 JSON + WolvenKit 的完整输出。

    CLI 原生崩溃时，屏幕/日志上那几行是 System.CommandLine 的调用链，看不出
    所以然。留一份 JSON 才能拿同一个文件再跑一次，或者报给 WolvenKit。
    """
    import time
    try:
        d = Path(root) / "failed" / ("%s_%s" % (
            time.strftime("%Y%m%d-%H%M%S"),
            _safe_token(res.rel.rsplit("\\", 1)[-1], "res")))
        d.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(json_path, d / Path(json_path).name)
        except OSError:
            pass
        out = getattr(ex, "output", "") or str(ex)
        with open(d / "wolvenkit.log", "w", encoding="utf-8") as f:
            f.write(str(out))
        return str(d)
    except Exception:
        return ""


class Resource(object):
    """一个要被写进归档的资源。

    rel  归档内相对路径（反斜杠）
    doc  WolvenKit JSON 文档（dict）
    """

    def __init__(self, rel, doc):
        self.rel = xlmod.norm_rel(rel)
        self.doc = doc

    def __repr__(self):
        return "<Resource %s>" % self.rel


class SubtitlePair(object):
    """字幕必须成对出现：注册表(map) + 文本(entries)。

    M0 结论：只给 entries 会报 `Resource "..." failed to load`。
    """

    def __init__(self, entries_rel, entries_doc, map_rel=None, map_doc=None):
        self.entries = Resource(entries_rel, entries_doc)
        self.map = Resource(map_rel, map_doc) if map_rel else None

    def ensure_map_points_here(self):
        """保证注册表条目指向的正是我们的 entries 文件。

        返回 (修正数, 错误列表)。这一步是防呆：注册表里的路径写错，
        游戏日志只会丢一句 failed to load，很难查。
        """
        if self.map is None:
            return 0, []
        fixed = 0
        errors = []
        for e in cr2w.entries(self.map.doc):
            cur = cr2w.sub_map_target(e)
            if not cur:
                errors.append("注册表条目缺少 subtitleFile.DepotPath")
                continue
            if xlmod.norm_rel(cur) != self.entries.rel:
                try:
                    cr2w.set_sub_map_target(e, self.entries.rel)
                    fixed += 1
                except cr2w.Cr2wError as ex:
                    errors.append(str(ex))
        return fixed, errors


class BuildSpec(object):
    def __init__(self, name, extend=""):
        self.name = name
        self.extend = extend or ""
        self.onscreens = []   # list[Resource]
        self.subtitles = []   # list[SubtitlePair]
        self.lipmaps = []
        self.vomaps = []

    def resources(self):
        out = list(self.onscreens) + list(self.lipmaps) + list(self.vomaps)
        for pair in self.subtitles:
            out.append(pair.entries)
            if pair.map is not None:
                out.append(pair.map)
        return out

    def is_empty(self):
        return not self.resources()

    def __repr__(self):
        return "<BuildSpec %s onscreen=%d subtitle=%d extend=%r>" % (
            self.name, len(self.onscreens), len(self.subtitles), self.extend)


class BuildResult(object):
    def __init__(self, name, archive, xl_path, resources, xl_text=""):
        self.name = name
        self.archive = Path(archive) if archive else None
        self.xl_path = Path(xl_path) if xl_path else None
        self.resources = list(resources)
        self.xl_text = xl_text
        self.warnings = []
        # 转换失败、被跳过的资源（相对归档根的路径）。调用方要按它把对应
        # 的行从「已写入」里扣掉，否则会报成「8 条写入成功」而归档里没有。
        self.skipped = []

    def files(self):
        return [p for p in (self.archive, self.xl_path) if p is not None]

    def __repr__(self):
        return "<BuildResult %s archive=%s xl=%s>" % (
            self.name,
            self.archive.name if self.archive else "-",
            self.xl_path.name if self.xl_path else "-")


def make_spec(name, extend="") -> BuildSpec:
    return BuildSpec(_clean_name(name), extend)


def _clean_name(name):
    n = str(name or "").strip()
    for suffix in (".archive.xl", ".archive", ".xl"):
        if n.lower().endswith(suffix):
            n = n[: -len(suffix)]
    n = re.sub(r"[\\/:*?\"<>|]+", "_", n).strip()
    return n or "translation"


def default_output_name(target_name, lang="zh-cn"):
    """默认产物名：原 mod 名 + 语言后缀，便于在 mod 目录里一眼认出。"""
    base = _clean_name(target_name)
    return "%s_%s" % (base, lang)


def build(spec, cli, workdir, outdir, log=None):
    """执行打包，返回 BuildResult。

    目录结构：
        <workdir>/<name>/json/         WolvenKit JSON（中间产物，可删）
        <workdir>/<name>/cr2w/         deserialize 产物
        <workdir>/<name>/stage/<name>/ 待打包树（内容 = 归档内容）
        <outdir>/<name>.archive        最终产物
        <outdir>/<name>.archive.xl
    """
    def say(msg):
        if log:
            log(msg)

    if spec.is_empty():
        raise BuildError("没有可打包的资源")

    resources = spec.resources()
    if not resources:
        raise BuildError("没有可打包的资源")

    name = _clean_name(spec.name)
    root = Path(workdir) / name
    json_dir = root / "json"
    cr2w_dir = root / "cr2w"
    stage_parent = root / "stage"
    stage = stage_parent / name        # 目录名 = 归档名，pack 直接用
    out = Path(outdir)

    for d in (json_dir, cr2w_dir, stage_parent, out):
        d.mkdir(parents=True, exist_ok=True)

    # 每次重建都清空，避免上一轮的残留文件混进归档
    for d in (json_dir, cr2w_dir, stage_parent):
        for child in list(d.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    stage.mkdir(parents=True, exist_ok=True)

    staged = []          # 真正转换成功的资源（归档内相对路径）
    failed = []          # [(rel, 说明, 现场目录)]

    for idx, res in enumerate(resources):
        say("序列化 %s" % res.rel)
        slot = json_dir / ("%03d_%s" % (idx, _safe_token(res.rel.rsplit("\\", 1)[-1], "res")))
        slot.mkdir(parents=True, exist_ok=True)
        target_name = res.rel.rsplit("\\", 1)[-1]
        json_path = slot / (target_name + ".json")
        cr2w.save_doc(res.doc, json_path)

        # WolvenKit 的 convert 偶尔会原生崩溃（exit 0xC0000005，栈落在
        # System.CommandLine 上，跟输入无关——同一份 JSON 再跑一次就过）。
        # 一次重试能把这类抖动吃掉；还不行就只跳过这一个资源，别把整包
        # 连同其它几十条译文一起丢进失败。
        try:
            cr2w_path = wkit.deserialize(cli, json_path, cr2w_dir / ("%03d" % idx),
                                         log=log, expect_name=target_name)
        except Exception:
            say("  转换失败，重试一次")
            try:
                cr2w_path = wkit.deserialize(
                    cli, json_path, cr2w_dir / ("%03d" % idx),
                    log=log, expect_name=target_name)
            except Exception as ex2:
                where = _save_failure(root, res, json_path, ex2)
                failed.append((res.rel, str(ex2), where))
                say("  仍然失败，跳过 %s（现场：%s）" % (res.rel, where or "-"))
                continue
        dst = stage / res.rel.replace("\\", os.sep)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cr2w_path, dst)
        if not cr2w.is_cr2w(dst):
            where = _save_failure(root, res, json_path,
                                  BuildError("产物不是 CR2W：%s" % res.rel))
            failed.append((res.rel, "产物不是 CR2W", where))
            say("  产物不是 CR2W，跳过 %s" % res.rel)
            continue
        staged.append(res.rel)
        say("  → %s (%d 字节)" % (res.rel, dst.stat().st_size))

    if not staged:
        detail = "；".join("%s: %s" % (rel, why) for rel, why, _w in failed[:3])
        raise BuildError("没有可打包的资源%s" % (("（" + detail + "）") if detail else ""))

    result = BuildResult(name, None, None, staged)
    result.skipped = [rel for rel, _why, _w in failed]
    for rel, why, where in failed:
        result.warnings.append("转换失败已跳过 %s（%s）%s" % (
            rel, why.splitlines()[0][:120], ("现场：" + where) if where else ""))

    say("打包 %d 个资源" % len(staged))
    # pack 的输出名取自目录名，而目录名就是我们要的名字，所以直接落到 out
    archive = wkit.pack(cli, stage, out, log=log)
    final_archive = out / (name + ".archive")
    if archive.resolve() != final_archive.resolve():
        shutil.copy2(archive, final_archive)
    result.archive = final_archive

    cfg = xlmod.L10nConfig()
    cfg.extend = spec.extend
    ok = set(staged)
    for res in spec.onscreens:
        if res.rel in ok:
            cfg.add("onscreens", _lang_of(res.rel), res.rel)
    for res in spec.lipmaps:
        if res.rel in ok:
            cfg.add("lipmaps", _lang_of(res.rel), res.rel)
    for res in spec.vomaps:
        if res.rel in ok:
            cfg.add("vomaps", _lang_of(res.rel), res.rel)
    for pair in spec.subtitles:
        # .xl 指向注册表，不指向文本 —— 这是 M0 最容易搞错的一点
        anchor = pair.map or pair.entries
        if anchor.rel in ok:
            cfg.add("subtitles", _lang_of(anchor.rel), anchor.rel)

    final_xl = out / xlmod.default_name(name)
    xlmod.save(final_xl, cfg)
    result.xl_path = final_xl
    result.xl_text = xlmod.render(cfg)

    declared = set(cfg.all_paths())
    missing = sorted(declared - ok)
    if missing:
        result.warnings.append(".xl 声明了但归档里没有：%s" % ", ".join(missing))
    unused = sorted(ok - declared)
    if unused:
        result.warnings.append("归档里有但 .xl 未声明：%s" % ", ".join(unused))

    say("产物: %s / %s" % (final_archive.name, final_xl.name))
    return result


def _lang_of(rel):
    """从路径里猜语言：localization\\zh-cn\\... → zh-cn。猜不到默认 zh-cn。"""
    parts = [p.lower() for p in str(rel).replace("/", "\\").split("\\")]
    for i, p in enumerate(parts[:-1]):
        if p in ("localization", "localisation", "locales", "locale") and i + 1 < len(parts):
            cand = parts[i + 1]
            if re.match(r"^[a-z]{2}-[a-z]{2}$", cand):
                return cand
    for p in parts:
        if re.match(r"^[a-z]{2}-[a-z]{2}$", p):
            return p
    return xlmod.LANG_ZH_CN


# ------------------------------------------------------------------ 安装


def install(result, game_dir, backup=True, log=None):
    """把产物拷进 archive\\pc\\mod。

    只写 archive\\pc\\mod 下的两个文件，不碰原 mod。
    已存在的同名文件先备份为 .bak（同名备份只留一份）。
    """
    game = Path(game_dir)
    mod_dir = game / "archive" / "pc" / "mod"
    if not mod_dir.is_dir():
        raise BuildError("游戏 mod 目录不存在：%s" % mod_dir)

    written = []
    for src in result.files():
        dst = mod_dir / src.name
        if dst.exists() and backup:
            bak = dst.with_name(dst.name + ".bak")
            if not bak.exists():
                shutil.copy2(dst, bak)
                if log:
                    log("备份: %s" % bak.name)
        shutil.copy2(src, dst)
        written.append(dst)
        if log:
            log("安装: %s" % dst.name)
    return written


def uninstall(result_or_name, game_dir, log=None):
    """移除装进游戏的产物（不动原 mod），返回删掉的文件列表。"""
    name = (result_or_name.name
            if isinstance(result_or_name, BuildResult) else _clean_name(result_or_name))
    mod_dir = Path(game_dir) / "archive" / "pc" / "mod"
    removed = []
    for fname in (name + ".archive", xlmod.default_name(name)):
        p = mod_dir / fname
        if p.exists():
            p.unlink()
            removed.append(p)
            if log:
                log("移除: %s" % p.name)
        bak = mod_dir / (fname + ".bak")
        if bak.exists():
            bak.unlink()
    return removed
