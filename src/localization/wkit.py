# -*- coding: utf-8 -*-
"""wkit.py —— WolvenKit CLI 的调用层。

设计约束：
  1. **只做进程调用，不链接任何库。** WolvenKit 是 GPL-3.0，独立进程调用属聚合，
     静态/动态链接则会传染许可证。
  2. CLI 是外部依赖，可能缺失、可能是 GUI 版（无 CLI）、可能版本不同。
     所有发现与错误都在这里收敛成 WkitError，上层不必判断。
  3. 用户机器上 CLI 常在 D:\\wkit-tools、%LOCALAPPDATA%\\Programs\\WolvenKit 之类
     非标准位置，逐个探测，不做假设。

9.1.0 实测命令（详见 docs/M0验证.md）：
    unbundle   <archive> -o <dir> [-v Minimal]
    convert serialize   <cr2w.json>   -o <dir>
    convert deserialize <x.json.json> -o <dir>
    pack <dir> -o <outdir>

两个必须记住的约束：
  * `-o` 指向的目录**必须已存在**，否则报 "Invalid output directory"。
  * `pack` 打包的是目录**内容**，输出名取自目录名（m0test\\ → m0test.archive）。
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# unbundle 大归档（官方中文包 ~2 GB 解压）可能跑几分钟
DEFAULT_TIMEOUT = 1800

_EXE_NAMES = ("WolvenKit.CLI.exe", "WolvenKit.CLI", "wolvenkit.cli.exe")

# 已知安装位置；按命中概率排序，找到即停
_SEARCH_GLOBS = (
    r"D:\wkit-tools\console-*\WolvenKit.CLI.exe",
    r"C:\wkit-tools\console-*\WolvenKit.CLI.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\WolvenKit\WolvenKit.CLI.exe"),
    os.path.expandvars(r"%USERPROFILE%\WolvenKit\WolvenKit.CLI.exe"),
    r"C:\Program Files\WolvenKit\WolvenKit.CLI.exe",
    r"C:\Program Files (x86)\WolvenKit\WolvenKit.CLI.exe",
)

# 进度条只输出 "  12%" 这类行，逐行打印会淹没真正有用的日志
_PROGRESS_RE = re.compile(r"^\s*\d{1,3}%\s*$")


class WkitError(RuntimeError):
    """WolvenKit 调用失败。args[0] 是给人看的信息，args[1] 可选原始输出。"""

    def __init__(self, message, output=""):
        super().__init__(message)
        self.output = output or ""


def _is_cli(path) -> bool:
    """GUI 版也叫 WolvenKit.exe，必须排除；只认名字里带 CLI 的。"""
    p = Path(path)
    if not p.is_file():
        return False
    name = p.name.lower()
    return name.endswith(".exe") and "cli" in name


def find_cli(explicit=None):
    """定位 WolvenKit.CLI.exe。

    顺序：显式传入 → 环境变量 → PATH → 已知安装位置（含版本号通配）。
    找不到返回空字符串，由调用方决定是提示用户还是降级。
    """
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        # 用户可能给了安装目录
        if p.is_dir():
            for name in _EXE_NAMES:
                hit = p / name
                if hit.is_file():
                    return str(hit)
            for hit in p.glob("**/WolvenKit.CLI.exe"):
                return str(hit)

    for env in ("WOLVENKIT_CLI", "WOLVENKIT_CONSOLE"):
        val = os.environ.get(env)
        if val:
            p = Path(val)
            if p.is_file():
                return str(p)
            hit = p / "WolvenKit.CLI.exe"
            if hit.is_file():
                return str(hit)

    for name in _EXE_NAMES:
        found = shutil.which(name)
        if found:
            return found

    for pattern in _SEARCH_GLOBS:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]  # 版本号递增，取最新的

    return ""


def _filter(output):
    """丢掉纯进度行，保留其余；同时把 CR/LF 归一。"""
    keep = []
    for line in output.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip() or _PROGRESS_RE.match(line):
            continue
        keep.append(line.rstrip())
    return keep


def run(cli, args, timeout=DEFAULT_TIMEOUT, log=None):
    """执行一条 CLI 命令，返回过滤后的输出行列表。

    失败时抛 WkitError，并把完整输出挂在 .output 上，方便上层展示。
    """
    if not cli:
        raise WkitError("未找到 WolvenKit.CLI.exe，请先在设置里指定路径。")
    cmd = [str(cli)] + [str(a) for a in args]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise WkitError("WolvenKit.CLI.exe 无法启动：%s" % cli)
    except subprocess.TimeoutExpired:
        raise WkitError("WolvenKit 超时（%d 秒）：%s" % (timeout, args[0] if args else ""))

    text = (proc.stdout or b"").decode("utf-8", "replace")
    lines = _filter(text)
    if log:
        for line in lines:
            log(line)
    if proc.returncode != 0:
        tail = "\n".join(lines[-8:])
        raise WkitError(
            "WolvenKit %s 失败（exit %d）\n%s"
            % (args[0] if args else "", proc.returncode, tail),
            text,
        )
    return lines


def version(cli):
    """返回版本号字符串，失败返回空串（用于设置页展示与能力判断）。"""
    try:
        lines = run(cli, ["--version"], timeout=60)
    except WkitError:
        return ""
    for line in lines:
        m = re.search(r"\d+\.\d+\.\d+", line)
        if m:
            return m.group(0)
    return ""


def _ensure_dir(path):
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def list_archive(cli, archive, timeout=180, log=None):
    """只读归档目录，返回归档内的相对路径列表（不解包）。

    加载模组时要判断「这个 .archive 里到底有没有 localization」。
    直接解包一个几百 MB 的归档只为确认没有，代价太大；列目录只读 TOC。
    """
    lines = run(cli, ["archive", "-l", str(archive)], timeout=timeout, log=log)
    out = []
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("[") or s.startswith("Usage"):
            continue
        if s.lower().startswith("description") or s.lower().startswith("usage"):
            continue
        out.append(s.replace("/", "\\").lower())
    return out


def localization_files(files):
    """从 `list_archive()` 的输出里挑出「可能是本地化表」的 json。

    只按路径段判断，不看文件名前缀：实测命名毫无规律（`en-us.json`、
    `kiki_buttercup_dress_localization.json`、`translation_strings.json`）。
    """
    out = []
    for f in files or []:
        low = str(f).lower()
        if not low.endswith(".json"):
            continue
        parts = low.split("\\")
        if ("localization" in parts or "l10n" in parts or "onscreens" in parts
                or "localization" in parts[-1]):
            out.append(f)
    return out


def has_localization(cli, archive, timeout=180):
    """预筛：归档里有没有可能是本地化表的 json。拿不准就返回 True。

    这里只是省一次解包，**不能当结论**：目录里有 localization 目录、里面却
    没有条目（AMM 的 `AMM_Patches.archive` 就是这样）也会返回 True。
    真正的判据是解包后扫出来的行数，为 0 时由调用方报 `no_rows`。
    """
    try:
        files = list_archive(cli, archive, timeout=timeout)
    except Exception:
        return True
    if not files:
        return True
    return bool(localization_files(files))


def unbundle(cli, archive, outdir, pattern=None, timeout=DEFAULT_TIMEOUT, log=None):
    """解包整个归档到 outdir，返回解出的文件相对路径集合。

    pattern 是 WolvenKit 的 `-w` 通配（如 `*onscreens.json`），可显著加速。
    """
    archive = Path(archive)
    if not archive.is_file():
        raise WkitError("归档不存在：%s" % archive)
    out = _ensure_dir(outdir)
    args = ["unbundle", str(archive), "-o", str(out), "-v", "Minimal"]
    if pattern:
        args += ["-w", pattern]
    run(cli, args, timeout=timeout, log=log)
    return _rel_files(out)


def serialize(cli, src, outdir, timeout=600, log=None):
    """CR2W → WolvenKit JSON（*.json.json）。返回产出文件路径。"""
    src = Path(src)
    out = _ensure_dir(outdir)
    before = set(out.rglob("*.json.json"))
    run(cli, ["convert", "serialize", str(src), "-o", str(out)], timeout=timeout, log=log)
    hits = [p for p in out.rglob("*.json.json") if p not in before]
    if not hits:
        hits = list(out.rglob("*.json.json"))
    if not hits:
        raise WkitError("serialize 未产出 JSON：%s" % src.name)
    return hits[0]


def deserialize(cli, src, outdir, timeout=600, log=None, expect_name=None):
    """WolvenKit JSON → CR2W。返回产出文件路径。

    expect_name 用于在目录里有多个产物时挑选目标（按文件名匹配）。
    """
    src = Path(src)
    out = _ensure_dir(outdir)
    before = set(out.rglob("*"))
    run(cli, ["convert", "deserialize", str(src), "-o", str(out)], timeout=timeout, log=log)
    hits = [p for p in out.rglob("*") if p.is_file() and p not in before]
    hits = [p for p in hits if _is_cr2w(p)]
    if not hits:
        raise WkitError("deserialize 未产出 CR2W：%s" % src.name)
    if expect_name:
        for p in hits:
            if p.name.lower() == expect_name.lower():
                return p
    return hits[0]


def pack(cli, folder, outdir, timeout=900, log=None):
    """把 folder 的**内容**打成 <folder.name>.archive，返回归档路径。"""
    folder = Path(folder)
    if not folder.is_dir():
        raise WkitError("待打包目录不存在：%s" % folder)
    out = _ensure_dir(outdir)
    run(cli, ["pack", str(folder), "-o", str(out)], timeout=timeout, log=log)
    hits = sorted(out.rglob("*.archive"))
    if not hits:
        raise WkitError("pack 未产出 .archive")
    for h in hits:
        if h.stem.lower() == folder.name.lower():
            return h
    # 目录名与产物名不一致时，取最新写入的那个
    return max(hits, key=lambda p: p.stat().st_mtime)


def _is_cr2w(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"CR2W"
    except OSError:
        return False


def _rel_files(root):
    root = Path(root)
    return {
        str(p.relative_to(root)).replace("/", "\\")
        for p in root.rglob("*")
        if p.is_file()
    }


def preflight(cli, game_dir):
    """启动前自检，返回 (问题列表, 环境信息字典)。

    不抛异常：设置页要能展示「哪一项没配好」。
    """
    problems = []
    info = {"cli": "", "cliVersion": "", "game": str(game_dir or ""), "archiveXL": ""}

    if not cli:
        problems.append("未找到 WolvenKit.CLI.exe")
    else:
        info["cli"] = str(cli)
        ver = version(cli)
        info["cliVersion"] = ver
        if not ver:
            problems.append("WolvenKit 无法执行（--version 无输出）")

    g = Path(game_dir) if game_dir else None
    if not g or not (g / "bin" / "x64" / "Cyberpunk2077.exe").is_file():
        problems.append("游戏目录不正确（找不到 bin\\x64\\Cyberpunk2077.exe）")
    else:
        mod_dir = g / "archive" / "pc" / "mod"
        if not mod_dir.is_dir():
            problems.append("缺少 archive\\pc\\mod 目录")
        plugins = g / "red4ext" / "plugins"
        hits = list(plugins.glob("ArchiveXL*")) if plugins.is_dir() else []
        if not hits:
            problems.append("未检测到 ArchiveXL —— 生成的 .xl 不会被加载")
        else:
            info["archiveXL"] = hits[0].name
        if not list((g / "archive" / "pc" / "content").glob("lang_*_text.archive")):
            problems.append("未找到官方语言包（lang_*_text.archive）")

    return problems, info


def main(argv=None):
    """直接运行本文件时给出诊断信息：python -m localization.wkit"""
    import argparse

    ap = argparse.ArgumentParser(description="WolvenKit CLI 环境诊断")
    ap.add_argument("--cli", default=None, help="WolvenKit.CLI.exe 路径")
    ap.add_argument("--game", default=None, help="游戏根目录")
    args = ap.parse_args(argv)

    cli = find_cli(args.cli)
    problems, info = preflight(cli, args.game)
    print("WolvenKit :", info["cli"] or "(未找到)")
    print("版本      :", info["cliVersion"] or "-")
    print("游戏目录  :", info["game"] or "-")
    print("ArchiveXL :", info["archiveXL"] or "-")
    if problems:
        print("\n问题：")
        for p in problems:
            print("  -", p)
        return 1
    print("\n环境正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
