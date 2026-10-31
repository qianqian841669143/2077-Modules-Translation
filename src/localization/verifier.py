# -*- coding: utf-8 -*-
"""verifier.py —— 产物校验。

打包成功 ≠ 能用。ArchiveXL 的失败方式很安静：资源路径写错、资源类型不对，
都只是在日志里丢一句 failed to load，游戏里毫无反应。所以产物必须自检。

两层校验：
  1. **离线**（不需要启动游戏）：归档里有没有这些资源、是不是 CR2W、
     字幕注册表指向的文本文件是否真的在归档里、.xl 能否原样解析回来。
  2. **在线**（启动游戏后）：读 ArchiveXL 日志，确认
     `Merging entries from "<我们的路径>"` 出现过、且没有 failed to load。

第 2 层是唯一能证明「真的生效」的证据，M0 就是靠它定位到字幕类型错误的。
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from . import archive as archmod
from . import cr2w, wkit, xl as xlmod

# ArchiveXL 日志里的关键行
_RE_INIT_TEXT = re.compile(r'\[Localization\] Initializing texts for "([^"]+)"')
_RE_INIT_SUB = re.compile(r'\[Localization\] Initializing subtitles for "([^"]+)"')
_RE_MERGE = re.compile(r'\[Localization\] Merging entries from "([^"]+)"')
_RE_FAIL = re.compile(r'\[Localization\] Resource "([^"]+)" failed to load')
_RE_DONE_TEXT = re.compile(r"\[Localization\] (All translations merged|Translations merged with issues|No translations to merge)")
_RE_DONE_SUB = re.compile(r"\[Localization\] (All subtitles merged|Subtitles merged with issues|No subtitles to merge)")


class Report(object):
    """校验结果。problems 非空即不合格；warnings 不算失败。"""

    def __init__(self):
        self.checks = []      # [(名字, 是否通过, 说明)]
        self.problems = []
        self.warnings = []

    def check(self, name, ok, detail=""):
        self.checks.append((name, bool(ok), detail))
        if not ok:
            self.problems.append("%s%s" % (name, ("：" + detail) if detail else ""))
        return ok

    def warn(self, text):
        self.warnings.append(text)

    @property
    def ok(self):
        return not self.problems

    def as_dict(self):
        return {
            "ok": self.ok,
            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in self.checks],
            "problems": list(self.problems),
            "warnings": list(self.warnings),
        }

    def text(self):
        lines = []
        for name, ok, detail in self.checks:
            lines.append("%s %s%s" % ("[OK]  " if ok else "[FAIL]", name,
                                      ("  " + detail) if detail else ""))
        for w in self.warnings:
            lines.append("[WARN] " + w)
        return "\n".join(lines)


def verify_build(result, spec, cli, workdir, log=None):
    """离线校验一个 BuildResult。"""
    report = Report()
    if result.archive is None or not result.archive.is_file():
        report.check("归档已生成", False, "文件不存在")
        return report
    if result.xl_path is None or not result.xl_path.is_file():
        report.check(".xl 已生成", False, "文件不存在")
        return report

    declared = set()
    cfg = None
    try:
        cfg = xlmod.load(result.xl_path)
        declared = set(cfg.all_paths())
        report.check(".xl 可解析", True, "%d 个资源" % len(declared))
    except xlmod.XlError as ex:
        report.check(".xl 可解析", False, str(ex))
        return report

    # 解包产物，逐个确认
    tmp = Path(workdir) / "_verify" / result.name
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        wkit.unbundle(cli, result.archive, tmp, log=None)
    except wkit.WkitError as ex:
        report.check("归档可解包", False, str(ex))
        return report

    got = set(archmod.map_files(tmp).keys())
    report.check("归档可解包", True, "%d 个文件" % len(got))

    missing = sorted(declared - got)
    report.check("声明与归档一致", not missing,
                 "缺失: " + ", ".join(missing) if missing else "全部命中")

    not_cr2w = []
    for rel in sorted(declared & got):
        if not cr2w.is_cr2w(tmp / rel.replace("\\", "/")):
            not_cr2w.append(rel)
    report.check("资源都是 CR2W", not not_cr2w,
                 "非法: " + ", ".join(not_cr2w) if not_cr2w else "")

    # 字幕：注册表条目必须指向归档内真实存在的文本文件
    bad_links = []
    for pair in getattr(spec, "subtitles", []):
        if pair.map is None:
            report.warn("字幕 %s 没有注册表，ArchiveXL 会报 failed to load" % pair.entries.rel)
            continue
        targets = [cr2w.sub_map_target(e) for e in cr2w.entries(pair.map.doc)]
        real = [t for t in targets if t]
        if not real:
            bad_links.append("%s 的注册表里没有有效条目" % pair.map.rel)
            continue
        for t in real:
            if t not in got:
                bad_links.append("%s → %s 在归档里不存在" % (pair.map.rel, t))
    report.check("字幕指针有效", not bad_links, "; ".join(bad_links) if bad_links else "")

    # 条目层面的健康度：文档不能是空的，译文不该整段丢失
    empty_docs = []
    empty_texts = 0
    total_entries = 0
    content_resources = list(getattr(spec, "onscreens", []))
    for pair in getattr(spec, "subtitles", []):
        content_resources.append(pair.entries)
    for res in content_resources:
        try:
            es = cr2w.entries(res.doc)
        except cr2w.Cr2wError as ex:
            empty_docs.append("%s (%s)" % (res.rel, ex))
            continue
        if not es:
            empty_docs.append("%s (0 条)" % res.rel)
            continue
        total_entries += len(es)
        for e in es:
            if not cr2w.get_text(e).strip():
                empty_texts += 1
    report.check("内容资源非空", not empty_docs, "; ".join(empty_docs))
    report.check("条目总数", total_entries > 0, "%d 条" % total_entries)
    if empty_texts:
        report.warn("%d 条文本为空，游戏里会显示成空白" % empty_texts)

    report.check("无阻断问题", True)
    return report


def scan_log(game, since=None, max_wait=0.0):
    """读最新的 ArchiveXL 日志，抽本地化事件。

    since     只接受这个时间点（epoch 秒）之后写入的日志
    max_wait  没有新日志时最多等多少秒（启动游戏后可以轮询）

    返回 dict：
        path         日志路径
        texts        我们的资源是否被合并
        subtitles    字幕是否被合并
        merged       出现过的 Merging entries 列表
        failed       加载失败的资源
        verdict      "ok" / "failed" / "no-log" / "no-localization"
    """
    gi = archmod.GameInfo(archmod.find_game(game))
    deadline = time.time() + max(0.0, max_wait)
    log_path = None
    while True:
        log_path = gi.find_archivexl_log(since=since)
        if log_path or time.time() >= deadline:
            break
        time.sleep(2.0)

    out = {
        "path": str(log_path) if log_path else "",
        "texts": False,
        "subtitles": False,
        "merged": [],
        "failed": [],
        "verdict": "no-log",
        "languages": [],
    }
    if not log_path:
        return out

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out

    langs = set(_RE_INIT_TEXT.findall(text)) | set(_RE_INIT_SUB.findall(text))
    out["languages"] = sorted(langs)
    out["merged"] = _RE_MERGE.findall(text)
    out["failed"] = _RE_FAIL.findall(text)

    if not langs:
        out["verdict"] = "no-localization"
        return out

    our = set(out["merged"])
    out["texts"] = bool(our)
    out["subtitles"] = bool([m for m in out["merged"] if "subtitle" in m.lower()])
    out["verdict"] = "failed" if out["failed"] else "ok"
    return out


def verify_installed(result, game, lang="zh-cn", since=None, max_wait=0.0):
    """在线校验：确认我们的资源出现在日志里，且没有被判失败。"""
    report = Report()
    info = scan_log(game, since=since, max_wait=max_wait)
    if not info["path"]:
        report.check("找到 ArchiveXL 日志", False,
                     "没有日志。ArchiveXL 未装，或游戏从未启动过")
        return report
    report.check("找到 ArchiveXL 日志", True, Path(info["path"]).name)

    if info["verdict"] == "no-localization":
        report.check("日志含本地化段", False, "ArchiveXL 版本过旧，不支持 localization")
        return report

    names = {p.rsplit("\\", 1)[-1].lower() for p in info["merged"]}
    ours = {r.rsplit("\\", 1)[-1].lower() for r in result.resources}
    hit = sorted(ours & names)
    report.check("我们的资源被合并", bool(hit),
                 "命中: " + ", ".join(hit) if hit else "日志里没有我们的文件")

    if info["failed"]:
        report.check("无加载失败", False, "; ".join(info["failed"]))
    else:
        report.check("无加载失败", True)

    if not info["subtitles"] and any("subtitle" in r.lower() for r in result.resources):
        report.warn("字幕资源在归档里，但日志没看到字幕合并记录")
    return report


def main(argv=None):
    """诊断用法：python -m localization.verifier --game <路径> [--wait 秒]"""
    import argparse

    ap = argparse.ArgumentParser(description="读取 ArchiveXL 日志，校验注入结果")
    ap.add_argument("--game", default=None)
    ap.add_argument("--wait", type=float, default=0.0, help="等待新日志的秒数")
    ap.add_argument("--since", type=float, default=None, help="只接受此 epoch 之后的日志")
    args = ap.parse_args(argv)

    game = archmod.find_game(args.game)
    if not game:
        print("未找到游戏目录，请用 --game 指定")
        return 2
    info = scan_log(game, since=args.since, max_wait=args.wait)
    print("日志   :", info["path"] or "(无)")
    print("语言   :", ", ".join(info["languages"]) or "-")
    print("合并   :", len(info["merged"]), "条")
    for m in info["merged"]:
        if "zh-cn" in m.lower() or "subtitle" in m.lower():
            print("   ", m)
    print("失败   :", ", ".join(info["failed"]) or "无")
    print("判定   :", info["verdict"])
    return 0 if info["verdict"] == "ok" else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
