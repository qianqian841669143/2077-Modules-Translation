# -*- coding: utf-8 -*-
"""l10n_test.py —— 资源模式引擎的端到端自测（不依赖 UI）。

用法：
    python tools\\l10n_test.py env
    python tools\\l10n_test.py scan  "<mod.archive.xl>"
    python tools\\l10n_test.py e2e   "<mod.archive.xl>" [--limit 5] [--install]
    python tools\\l10n_test.py xl    "<file.xl>"

e2e 做的事：
    解包 → 抽条目 → 取前 N 条加上【L10N】前缀 → 组包 → 离线校验
产物落在 .l10n\\out\\，加 --install 才会拷进游戏。

只要 e2e 全部检查为 OK，就说明引擎链路（解包/序列化/改/反序列化/打包/.xl）
是通的；游戏里能不能显示，靠 M0 已经证明的机制。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

MARK = "【L10N】"


def _utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _ok(msg):
    print("  [OK]   " + msg)


def _bad(msg):
    print("  [FAIL] " + msg)


def _info(msg):
    print("  " + msg)


def cmd_env(args):
    from localization import archive, wkit

    cli = wkit.find_cli(args.cli)
    game = archive.find_game(args.game)
    problems, info = wkit.preflight(cli, game)

    print("WolvenKit :", info["cli"] or "(未找到)")
    print("版本      :", info["cliVersion"] or "-")
    print("游戏      :", info["game"] or "(未找到)")
    print("ArchiveXL :", info["archiveXL"] or "(未装)")
    if game:
        gi = archive.GameInfo(game)
        xls = gi.installed_xls()
        print("已装 .xl  :", len(xls))
        print("语言包    :", gi.text_archive() or "(缺失)")
        print("日志目录  :", gi.archivexl_log_dir or "(无)")

    if problems:
        print("\n问题：")
        for p in problems:
            _bad(p)
        return 1
    _ok("环境正常")
    return 0


def _make_session(args):
    from localization import pipeline, wkit

    cli = wkit.find_cli(args.cli)
    if not cli:
        raise SystemExit("找不到 WolvenKit.CLI.exe，用 --cli 指定")
    workdir = Path(args.work) if args.work else (ROOT / ".l10n" / "work")

    def log(msg):
        _info(msg)

    sess = pipeline.open_session(args.path, cli, workdir, lang=args.lang, log=log)
    return cli, sess


def cmd_scan(args):
    cli, sess = _make_session(args)
    print("目标:", sess.target)
    meta = sess.scan()
    print()
    print("资源文件 :", meta["files"])
    print("可翻条目 :", meta["total"])
    print("按类型   :", meta["types"])
    print("按语言   :", meta.get("languages") or {})
    print("extend   :", meta["extend"] or "(无)")
    if meta["warnings"]:
        print("警告：")
        for w in meta["warnings"]:
            _info("- " + w)

    for r in sess.rows[:8]:
        _info("%-46s %-12s %s" % (
            (r["disp"] or "")[:46], r["entryId"][:12], r["orig"][:40]))
    if len(sess.rows) > 8:
        _info("... 共 %d 条" % len(sess.rows))
    return 0 if sess.rows else 1


def cmd_xl(args):
    from localization import xl as xlmod

    cfg = xlmod.load(args.path)
    print("extend:", cfg.extend or "(无)")
    print("资源数:", cfg.resource_count())
    for node in xlmod.NODES:
        table = cfg.nodes.get(node) or {}
        for lang in sorted(table):
            for p in table[lang]:
                print("  %-10s %-8s %s" % (node, lang, p))
    text = xlmod.render(cfg)
    again = xlmod.parse(text)
    same = (again.extend == cfg.extend
            and again.nodes == cfg.nodes)
    print("\n往返一致:", "是" if same else "否")
    return 0 if same else 1


def cmd_e2e(args):
    from localization import verifier

    t0 = time.time()
    cli, sess = _make_session(args)
    print("目标:", sess.target)
    print()

    meta = sess.scan()
    if not sess.rows:
        _bad("没有抽到可翻译条目")
        return 1

    picks = sess.rows[: max(1, args.limit)]
    for r in picks:
        r["trans"] = MARK + r["orig"]
    n = sess.apply_rows(picks)
    print("回写条目 :", n, "/", len(picks))

    outdir = Path(args.out) if args.out else (ROOT / ".l10n" / "out")
    result = sess.build(outdir=outdir, log=lambda m: _info(m))
    print()
    print("产物:")
    for f in result.files():
        print("  %-52s %8d 字节" % (f.name, f.stat().st_size))

    print("\n--- 离线校验 ---")
    report = verifier.verify_build(result, sess.last_spec, cli, sess.workdir)
    print(report.text())
    for w in result.warnings:
        print("[WARN] " + w)

    if args.install:
        from localization import builder as buildmod
        from localization import archive as archmod
        game = archmod.find_game(args.game)
        if not game:
            _bad("找不到游戏目录，无法安装")
            return 1
        print("\n--- 安装 ---")
        buildmod.install(result, game, log=lambda m: _info(m))
        _info("启动游戏后运行：python tools\\l10n_test.py log")

    print("\n耗时 %.1f 秒" % (time.time() - t0))
    return 0 if report.ok else 1


def cmd_log(args):
    from localization import archive as archmod
    from localization import verifier

    game = archmod.find_game(args.game)
    if not game:
        _bad("找不到游戏目录，用 --game 指定")
        return 1
    info = verifier.scan_log(game, since=args.since, max_wait=args.wait)
    print("日志   :", info["path"] or "(无)")
    print("语言   :", ", ".join(info["languages"]) or "-")
    print("合并数 :", len(info["merged"]))
    for m in info["merged"]:
        if "subtitle" in m.lower() or "l10n" in m.lower() or "zh-cn" in m.lower():
            _info(m)
    if info["failed"]:
        print("失败   :")
        for f in info["failed"]:
            _bad(f)
    print("判定   :", info["verdict"])
    return 0 if info["verdict"] == "ok" else 1


def cmd_api(args):
    """走 web_app.Api 这一层，验证界面接线（不开窗口）。"""
    import web_app

    api = web_app.Api()
    api.settings = dict(api.settings)
    if args.game:
        api.settings["game_dir"] = args.game
    if args.cli:
        api.settings["wkit_path"] = args.cli
    api.settings["use_tm"] = False

    env = api.l10n_env()
    print("环境:", "OK" if env.get("ok") else "不可用")
    print("  CLI :", env.get("cli") or "-", env.get("cliVersion") or "")
    print("  游戏:", env.get("game") or "-")
    print("  输出:", env.get("outputDir") or "-")
    for p in env.get("problems") or []:
        _bad(p)
    if not env.get("ok"):
        return 1

    if not args.path:
        return 0

    print("\n打开归档:", args.path)
    r = api._do_load_resource(args.path, {})
    if not r.get("ok"):
        _bad(r.get("error", ""))
        return 1
    rows = r["rows"]
    print("  mode :", r.get("mode"))
    print("  行数 :", len(rows))
    print("  跳过 :", r.get("skippedTarget"))
    print("  语言 :", (r.get("meta") or {}).get("languages"))

    picks = rows[: max(1, args.limit)]
    items = []
    for row in picks:
        items.append({"kind": "resource", "file": row["file"],
                      "entryId": row["entryId"], "orig": row["orig"],
                      "trans": MARK + row["orig"]})
    print("\n提交 %d 条 …" % len(items))
    res = api._apply_resource(items)
    print("  成功 :", res.get("ok"), " 失败 :", res.get("fail"))
    print("  归档 :", res.get("archive"))
    print("  xl   :", res.get("xl"))
    rep = res.get("report") or {}
    print("  校验 :", "通过" if rep.get("ok") else "未通过")
    for p in rep.get("problems") or []:
        _bad(p)
    for w in (res.get("warnings") or [])[:5]:
        _info("[WARN] " + w)
    return 0 if rep.get("ok") else 1


def main(argv=None):
    _utf8_stdout()
    ap = argparse.ArgumentParser(description="资源模式引擎自测")
    ap.add_argument("cmd", choices=["env", "scan", "e2e", "xl", "log", "api"])
    ap.add_argument("path", nargs="?", help=".archive.xl / .archive / 目录 / .zip")
    ap.add_argument("--cli", default=None)
    ap.add_argument("--game", default=None)
    ap.add_argument("--work", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--lang", default="zh-cn")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--since", type=float, default=None)
    ap.add_argument("--wait", type=float, default=0.0)
    args = ap.parse_args(argv)

    if args.cmd in ("scan", "e2e") and not args.path:
        ap.error("%s 需要一个路径参数" % args.cmd)
    if args.cmd == "xl" and not args.path:
        ap.error("xl 需要一个 .xl 路径")

    return {
        "env": cmd_env,
        "scan": cmd_scan,
        "e2e": cmd_e2e,
        "xl": cmd_xl,
        "log": cmd_log,
        "api": cmd_api,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
