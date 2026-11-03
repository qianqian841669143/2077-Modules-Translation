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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
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


def _error_tail(lines, keep=8):
    """从 CLI 输出里挑出真正有信息的那几行。

    .NET 崩溃时末尾几行永远是 `System.CommandLine.Invocation...` 的调用链
    （每次一字不差），真正的异常类型和消息写在前面。只取尾部等于把线索扔了，
    这里优先捞「异常/error」行，再补尾部。
    """
    picked = []
    for ln in lines or ():
        low = ln.lower()
        if ("exception" in low or "unhandled" in low
                or low.strip().startswith("error") or "错误" in ln):
            if ln not in picked:
                picked.append(ln)
        if len(picked) >= 3:
            break
    tail = list(lines or ())[-4:]
    for ln in tail:
        if ln not in picked:
            picked.append(ln)
    return "\n".join(picked[-keep:])


# 多线程同时 CreateProcess 时，一个线程刚建好的管道写端可能被另一个线程正在
# 启动的进程继承（Windows 的句柄继承是进程级、有窗口期的）。继承者只要活着，
# 管道就读不到 EOF —— 调用方会一直卡在 communicate() 里。所以「起进程」这一段
# 串行，输出也不走管道（见下）。
_SPAWN_LOCK = threading.Lock()


def _kill_tree(proc):
    """连子进程一起杀掉。超时往往就是孙进程占着句柄不放。"""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run(cli, args, timeout=DEFAULT_TIMEOUT, log=None):
    """执行一条 CLI 命令，返回过滤后的输出行列表。

    失败时抛 WkitError，并把完整输出挂在 .output 上，方便上层展示。

    **输出写临时文件，不走管道。** 走管道时，只要 CLI 启动的子进程继承了写端，
    主进程提前退出后管道仍不关，`communicate()` 会一直等 EOF —— 实测批量卡死
    半小时以上：WolvenKit 早就没在跑了，Python 却还堵在读取上（超时之后
    `subprocess.run` 还会再调一次 `communicate()`，那次没有超时，等于永久挂死）。
    写完等进程退出再读文件，孙进程占着句柄也没关系。
    """
    if not cli:
        raise WkitError("未找到 WolvenKit.CLI.exe，请先在设置里指定路径。")
    cmd = [str(cli)] + [str(a) for a in args]
    fd, out_path = tempfile.mkstemp(prefix="wkit-", suffix=".out")
    os.close(fd)
    try:
        try:
            with _SPAWN_LOCK:
                with open(out_path, "wb") as fh:
                    proc = subprocess.Popen(cmd, stdout=fh,
                                            stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise WkitError("WolvenKit.CLI.exe 无法启动：%s" % cli)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            raise WkitError("WolvenKit 超时（%d 秒）：%s"
                            % (timeout, args[0] if args else ""))
        with open(out_path, "rb") as f:
            text = f.read().decode("utf-8", "replace")
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass

    lines = _filter(text)
    if log:
        for line in lines:
            log(line)
    if proc.returncode != 0:
        tail = _error_tail(lines)
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

    判据是「有没有 .json」，不是路径里有没有 localization：目录名实测毫无规律
    （`base\\yv\\axl\\02_s15_local.json`、`kiki_buttercup_dress\\paperwork\\
    kiki_buttercup_dress_localization.json`、`phoenicia\\immersivecyberware\\
    translations\\translation_strings.json`），而这几个名字里只有第三个是
    「一眼能看出来」的。`archive.candidate_json()` 真正的判据也是「.json 且
    CR2W 头」——游戏里以 .json 结尾的 CR2W 资源基本只有本地化。

    v2.51.3 之前这里只认路径段里的 localization / l10n / onscreens，于是
    Immersive Cyberware 的本地化表（在 `...\\translations\\` 下）在解包前就被
    判成「没有本地化」，43 条物品名/描述根本进不了表（截图里那句
    `Holo Display: Ads` 就是其中一条）。省一次解包不值这个代价：归档里没有
    json 的大多是一堆 xbm/inkatlas/mesh，这条判据照样把它们挡在外面。
    """
    return [f for f in (files or []) if str(f).lower().endswith(".json")]


_TOC_MEM = {}
_TOC_LOCK = threading.Lock()
# 判据改过就换个版本号：TOC 缓存存的是布尔结论，不换版本号老归档会一直
# 命中「没有本地化」那条旧结论（v2.51.3 放宽 localization_files 时踩过）。
_TOC_KEY_VERSION = "v2"


def _toc_key(archive):
    """缓存键：版本 + 文件名 + 大小 + 开头 64 KB 的 sha1。

    不能带绝对路径也不能带 mtime：批量每次都把模组复制到输出目录，副本的路径
    和修改时间全变了 —— 那样等于永远不命中（实测第二轮还是 51 秒）。用「名字 +
    大小 + 头部指纹」既扛得住复制，也不会因为一个文件换了版本就复用旧结论。
    """
    try:
        st = Path(archive).stat()
        with open(archive, "rb") as f:
            head = f.read(65536)
    except OSError:
        return ""
    h = hashlib.sha1(head).hexdigest()[:16]
    return "%s|%s|%d|%s" % (_TOC_KEY_VERSION, Path(archive).name.lower(),
                            st.st_size, h)


def _toc_cache_path():
    try:
        from appconfig import config_dir
        return Path(config_dir()) / "cache" / "archive_toc.json"
    except Exception:
        return None


def _toc_cached(key):
    with _TOC_LOCK:
        if key in _TOC_MEM:
            return _TOC_MEM[key]
        p = _toc_cache_path()
        if p is None:
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        val = data.get(key)
        if isinstance(val, bool):
            _TOC_MEM[key] = val
            return val
    return None


def _toc_remember(key, val):
    with _TOC_LOCK:
        _TOC_MEM[key] = val
        p = _toc_cache_path()
        if p is None:
            return
        try:
            data = {}
            if p.is_file():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            data[key] = bool(val)
            if len(data) > 4000:      # 别让它无限长
                for k in list(data)[: len(data) - 4000]:
                    data.pop(k, None)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass


def has_localization(cli, archive, timeout=180, cache=True):
    """预筛：归档里有没有可能是本地化表的 json。拿不准就返回 True。

    这里只是省一次解包，**不能当结论**：目录里有 localization 目录、里面却
    没有条目（AMM 的 `AMM_Patches.archive` 就是这样）也会返回 True。
    真正的判据是解包后扫出来的行数，为 0 时由调用方报 `no_rows`。

    读一次目录是 2 秒的固定开销（WolvenKit 进程启动占大头），一个 72 归档的
    模组就要 147 秒。所以结果按「路径 + 大小 + mtime」缓存，第二遍直接命中。
    """
    key = _toc_key(archive) if cache else ""
    if key:
        hit = _toc_cached(key)
        if hit is not None:
            return hit
    try:
        files = list_archive(cli, archive, timeout=timeout)
    except Exception:
        return True
    if not files:
        return True          # 拿不准，不缓存，下次再试
    val = bool(localization_files(files))
    if key:
        _toc_remember(key, val)
    return val


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
    # 输入不存在时 WolvenKit 会直接 0xC0000005 崩掉（不是报错），日志上只剩一串
    # System.CommandLine 调用链。这种显而易见的错自己先拦住。
    if not src.is_file():
        raise WkitError("待转换的文件不存在：%s" % src)
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
    if not src.is_file():
        raise WkitError("待转换的 JSON 不存在：%s" % src)
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
