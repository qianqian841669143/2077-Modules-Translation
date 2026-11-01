# -*- coding: utf-8 -*-
"""archive.py —— 定位游戏、定位目标 mod、从归档里取资源。

资源模式的入口是「一个已装 mod」，可能是三种形态：

    archive\\pc\\mod\\Foo.archive.xl     ← 最常见，配合同名 .archive
    archive\\pc\\mod\\Foo.archive        ← 没有 .xl，纯归档
    Foo.zip                             ← 别人发来的整包，里面塞着上面两种

resolve_target() 把三种形态统一成 Target，上层不需要知道来源。
"""
from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path

from . import xl as xlmod

GAME_DIR_NAMES = ("Cyberpunk 2077",)

# 常见安装位置，命中即停
_GAME_CANDIDATES = (
    r"C:\Program Files (x86)\Steam\steamapps\common\Cyberpunk 2077",
    r"C:\Program Files\Steam\steamapps\common\Cyberpunk 2077",
    r"D:\SteamLibrary\steamapps\common\Cyberpunk 2077",
    r"E:\SteamLibrary\steamapps\common\Cyberpunk 2077",
    r"F:\SteamLibrary\steamapps\common\Cyberpunk 2077",
    r"D:\Steam\steamapps\common\Cyberpunk 2077",
    r"C:\Program Files\GOG Games\Cyberpunk 2077",
    r"D:\GOG Games\Cyberpunk 2077",
    r"E:\GOG Games\Cyberpunk 2077",
    r"C:\Games\Cyberpunk 2077",
)


class ArchiveError(RuntimeError):
    pass


def _valid_game(path) -> bool:
    p = Path(path)
    return (p / "bin" / "x64" / "Cyberpunk2077.exe").is_file()


def _steam_libraries():
    """从 Steam 的 libraryfolders.vdf 里挖出所有库目录。"""
    out = []
    for base in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam",
                 r"D:\Steam", r"E:\Steam"):
        vdf = Path(base) / "steamapps" / "libraryfolders.vdf"
        if not vdf.is_file():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            raw = m.group(1).replace("\\\\", "\\")
            out.append(raw)
    return out


def find_game(explicit=None):
    """定位游戏根目录，找不到返回空字符串（不抛异常）。"""
    if explicit:
        if _valid_game(explicit):
            return str(Path(explicit))
        # 用户可能指到了 bin\x64 或 archive 这种子目录
        for up in (Path(explicit), *Path(explicit).parents):
            if _valid_game(up):
                return str(up)

    for env in ("CYBERPUNK_DIR", "CP2077_DIR", "CP2077_PATH"):
        val = os.environ.get(env)
        if val and _valid_game(val):
            return str(Path(val))

    for cand in _GAME_CANDIDATES:
        if _valid_game(cand):
            return cand

    for lib in _steam_libraries():
        cand = Path(lib) / "steamapps" / "common" / "Cyberpunk 2077"
        if _valid_game(cand):
            return str(cand)

    return ""


class GameInfo(object):
    """游戏目录里我们关心的几个位置。"""

    def __init__(self, root):
        self.root = Path(root)
        self.mod_dir = self.root / "archive" / "pc" / "mod"
        self.content_dir = self.root / "archive" / "pc" / "content"
        self.plugins_dir = self.root / "red4ext" / "plugins"

    def __bool__(self):
        return _valid_game(self.root)

    @property
    def archivexl_dir(self):
        if not self.plugins_dir.is_dir():
            return None
        hits = sorted(self.plugins_dir.glob("ArchiveXL*"))
        return hits[0] if hits else None

    @property
    def archivexl_log_dir(self):
        """ArchiveXL 的日志写在插件目录里，不是 red4ext\\logs。"""
        d = self.archivexl_dir
        return d if d and d.is_dir() else None

    def text_archives(self):
        """所有语言的文本包，基础游戏 + 往日之影。

        P5 发现：以前只扫 archive/pc/content，漏掉了 archive/pc/ep1 下的
        lang_*_text.archive —— 整个往日之影的 22749 条字幕 id 因此不在官方语料里。
        """
        hej = self.root / "archive" / "pc"
        found = []
        for sub in ("content", "ep1"):
            d = hej / sub
            if d.is_dir():
                found.extend(d.glob("lang_*_text.archive"))
        return sorted(found)

    def text_archive(self, prefer="zh-cn"):
        """优先取指定语言的语言包，没有则退回最大的一个。"""
        packs = self.text_archives()
        if not packs:
            return None
        for p in packs:
            if prefer.lower() in p.name.lower():
                return p
        return max(packs, key=lambda p: p.stat().st_size)

    def find_archivexl_log(self, since=None):
        """返回最新的 ArchiveXL 日志路径（可只取某时间点之后的）。"""
        d = self.archivexl_log_dir
        if not d:
            return None
        logs = sorted(d.glob("ArchiveXL-*.log"), key=lambda p: p.stat().st_mtime)
        if since is not None:
            logs = [p for p in logs if p.stat().st_mtime >= since]
        return logs[-1] if logs else None

    def installed_xls(self):
        """mod 目录下所有 .xl（去重：*.archive.xl 同时会被 *.xl 匹配到）。"""
        if not self.mod_dir.is_dir():
            return []
        seen = {}
        for p in list(self.mod_dir.glob("*.xl")) + list(self.mod_dir.glob("*.archive.xl")):
            seen[p.name.lower()] = p
        return [seen[k] for k in sorted(seen)]


class Target(object):
    """一个待翻译的 mod。

    name         显示名（去掉扩展名）
    archive_path 归档文件，可能为空（mod 只提供 .xl 的罕见情况）
    xl_path      配套 .xl，可能为空
    root         工作根目录（zip 解压后的目录）
    origin       "game" / "dir" / "zip"
    xl_cfg       .xl 解析结果（可能为空）
    """

    def __init__(self, name, archive_path=None, xl_path=None, root=None, origin="dir"):
        self.name = name
        self.archive_path = Path(archive_path) if archive_path else None
        self.xl_path = Path(xl_path) if xl_path else None
        self.root = Path(root) if root else None
        self.origin = origin
        self._xl_cfg = None

    @property
    def xl_cfg(self):
        if self._xl_cfg is None and self.xl_path and self.xl_path.is_file():
            try:
                self._xl_cfg = xlmod.load(self.xl_path)
            except xlmod.XlError:
                self._xl_cfg = xlmod.L10nConfig()
        return self._xl_cfg

    def extend_name(self):
        """.xl 里声明的挂接目标；没有就返回空串。"""
        cfg = self.xl_cfg
        return cfg.extend if cfg else ""

    def declared(self, node, lang):
        """.xl 里已经声明的资源路径（这些是原 mod 自己的语言文件）。"""
        cfg = self.xl_cfg
        return cfg.get(node, lang) if cfg else []

    def __repr__(self):
        return "<Target %s archive=%s xl=%s origin=%s>" % (
            self.name,
            self.archive_path.name if self.archive_path else "-",
            self.xl_path.name if self.xl_path else "-",
            self.origin)


def _name_from(path):
    name = Path(path).name
    for suffix in (".archive.xl", ".archive", ".xl"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return Path(path).stem


def resolve_target(path, workdir=None):
    """把「用户给的路径」统一成 Target。

    支持：.archive.xl / .archive / 目录 / .zip
    """
    p = Path(path)
    if not p.exists():
        raise ArchiveError("路径不存在：%s" % p)

    if p.is_file() and p.suffix.lower() == ".zip":
        root = Path(workdir or (p.parent / (p.stem + "_unpacked")))
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(p) as zf:
                zf.extractall(root)
        except zipfile.BadZipFile as ex:
            raise ArchiveError("压缩包损坏：%s (%s)" % (p.name, ex))
        t = resolve_target(root, workdir=root)
        t.origin = "zip"
        if not t.root:
            t.root = root
        return t

    if p.is_file() and xlmod.looks_like_xl(p):
        name = _name_from(p)
        arch = _sibling_archive(p, name)
        return Target(name, arch, p, p.parent, "dir")

    if p.is_file() and p.suffix.lower() == ".archive":
        name = p.stem
        xl_path = p.with_name(p.name + ".xl")
        return Target(name, p, xl_path if xl_path.is_file() else None, p.parent, "dir")

    if p.is_dir():
        return _resolve_dir(p)

    raise ArchiveError("不认识的文件类型：%s" % p.name)


def _norm_key(text):
    """把名字归一化成只含小写字母数字，用于模糊匹配文件名。"""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _sibling_archive(xl_path, name):
    """Foo.archive.xl 对应 Foo.archive。

    真实 mod 里两者经常不同名（ExtraCyberware.archive.xl ↔ extra_cyberware.archive），
    所以除了精确查找，再按「忽略大小写与下划线」做一次归一化匹配。
    还找不到就返回 None，由上层提示用户直接选 .archive。
    """
    cands = [
        xl_path.with_name(name + ".archive"),
        xl_path.with_name(xl_path.name[: -3]
                          if xl_path.name.lower().endswith(".xl") else xl_path.name),
    ]
    for c in cands:
        if c.is_file() and c.suffix.lower() == ".archive":
            return c

    # 归一化匹配：同目录下唯一一个名字对得上的归档
    want = _norm_key(name)
    if not want:
        return None
    try:
        pool = [p for p in xl_path.parent.glob("*.archive") if p.is_file()]
    except OSError:
        return None
    exact = [p for p in pool if _norm_key(p.stem) == want]
    if len(exact) == 1:
        return exact[0]
    # 退一步：前缀/包含匹配，且必须唯一，避免抓错文件
    loose = [p for p in pool
             if want and (want in _norm_key(p.stem) or _norm_key(p.stem) in want)]
    if len(loose) == 1:
        return loose[0]
    return None


# ------------------------------------------------------------ 本工具的产物
#
# 产物叫 `<原归档名>_zh-cn.archive` + `.archive.xl`（builder.default_output_name），
# 和原归档并排躺在 mod 目录里。下一轮扫描时它就在 rglob 的结果里，而
# `_resolve_dir` 原来按「.xl 的 localization 段非空」挑候选——我们的包恰好非空，
# 于是挑中的是自己写的中文包，把中文当原文再翻一遍。
#
# 只排除「名字像产物」不够：真模组也可能叫 `Foo_zh-cn.archive`（别人做的汉化）。
# 所以要求两个信号同时命中：名字带语言码后缀 + 资源路径是我们的写法。

_LANG_CODE_RE = re.compile(
    r"_(?:zh[_-]cn|zh[_-]tw|en[_-]us|de[_-]de|es[_-]es|es[_-]mx|fr[_-]fr|"
    r"it[_-]it|ja[_-]jp|ko[_-]kr|pl[_-]pl|pt[_-]br|ru[_-]ru|ar[_-]sa|"
    r"tr[_-]tr|cz[_-]cz|hu[_-]hu|nl[_-]nl|chs|cht)$", re.I)

# pipeline._out_rel 写 onscreens、build 写字幕，两条都在这里
_OWN_SUB_RE = re.compile(r"^localization\\[a-z]{2}[-_][a-z]{2}\\subtitles\\", re.I)


def _strip_archive_ext(name):
    low = name.lower()
    for suffix in (".archive.xl", ".archive", ".xl"):
        if low.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def is_own_product(path, cfg=None):
    """是不是本工具生成的翻译包（`<原名>_zh-cn.archive` 或它的 `.xl`）。"""
    p = Path(path)
    if not _LANG_CODE_RE.search(_strip_archive_ext(p.name)):
        return False
    if p.name.lower().endswith(".archive"):
        return True
    if cfg is None:
        try:
            cfg = xlmod.load(p)
        except xlmod.XlError:
            return True          # 解析不了还叫这个名字，当产物处理更安全
    paths = cfg.all_paths()
    if not paths:
        return True
    for rel in paths:
        low = str(rel).lower()
        if low.startswith("l10n\\") or _OWN_SUB_RE.match(low):
            continue
        return False
    return True


def _pick_xl(cands):
    """从候选里挑最像「这个 mod 自己」的那份 .xl。

    判据依次是：localization 段非空 > 声明了 extend > 配套归档更大。
    原来是「按 rglob 顺序取第一个非空的」，目录里多一份 .xl 就变成随机命中。
    """
    best = None
    for p, cfg in cands:
        nonempty = 1 if (cfg is not None and not cfg.is_empty()) else 0
        extend = 1 if (cfg is not None and cfg.extend) else 0
        size = 0
        arch = _sibling_archive(p, _name_from(p))
        if arch is not None:
            try:
                size = arch.stat().st_size
            except OSError:
                size = 0
        key = (nonempty, extend, size)
        if best is None or key > best[0]:
            best = (key, p, cfg)
    return best[1], best[2]


def _resolve_dir(root):
    root = Path(root)
    xls = [p for p in root.rglob("*.xl") if xlmod.looks_like_xl(p)]
    archives = [p for p in root.rglob("*.archive")]
    if not xls and not archives:
        raise ArchiveError("目录里没有 .archive 或 .archive.xl：%s" % root)

    loaded = []
    for p in xls:
        try:
            cfg = xlmod.load(p)
        except xlmod.XlError:
            cfg = None
        loaded.append((p, cfg))
    own = set(p for p, cfg in loaded if is_own_product(p, cfg))
    own_arch = set(a for a in archives if is_own_product(a))
    real = [(p, cfg) for p, cfg in loaded if p not in own]
    real_arch = [a for a in archives if a not in own_arch]
    if not real and not real_arch:
        names = sorted(p.name for p in (own | own_arch))[:3]
        raise ArchiveError(
            "目录里只有本工具生成的翻译包（%s），请选择原 mod 的归档。"
            % "、".join(names))

    if real:
        chosen, cfg = _pick_xl(real)
        name = _name_from(chosen)
        arch = _sibling_archive(chosen, name)
        if arch is not None and arch in own_arch:
            arch = None          # 指向的是我们自己写的包，当它不存在
        if arch is None:
            same = [a for a in real_arch if a.stem.lower() == name.lower()]
            arch = same[0] if same else (real_arch[0] if len(real_arch) == 1 else None)
        return Target(name, arch, chosen, root, "dir")

    return Target(_name_from(real_arch[0]), real_arch[0], None, root, "dir")


def resolve_all(root):
    """目录里**所有**该翻的归档目标（已排除本工具的产物）。

    一个模组经常不止一个归档：AMM 有 `AMM_Characters` / `AMM_Patches` /
    `AMM_Shared`，本地化表只在其中一个里。老做法用 `_resolve_dir()` 挑一个，
    挑到没文本的那个就等于「这个模组没有归档层」，而且挑中谁取决于目录
    遍历顺序——同一份 mod 两次扫描能给出不同结果。

    排序：`_pick_xl()` 选中的那个放最前，其余按名字。
    """
    root = Path(root)
    xls = [p for p in root.rglob("*.xl") if xlmod.looks_like_xl(p)]
    archives = [p for p in root.rglob("*.archive")]
    if not xls and not archives:
        raise ArchiveError("目录里没有 .archive 或 .archive.xl：%s" % root)

    loaded = []
    for p in xls:
        try:
            cfg = xlmod.load(p)
        except xlmod.XlError:
            cfg = None
        loaded.append((p, cfg))
    own = set(p for p, cfg in loaded if is_own_product(p, cfg))
    own_arch = set(a for a in archives if is_own_product(a))
    real = [(p, cfg) for p, cfg in loaded if p not in own]
    real_arch = [a for a in archives if a not in own_arch]
    if not real and not real_arch:
        names = sorted(p.name for p in (own | own_arch))[:3]
        raise ArchiveError(
            "目录里只有本工具生成的翻译包（%s），请选择原 mod 的归档。"
            % "、".join(names))

    best = None
    if real:
        try:
            best = _pick_xl(real)[0]
        except Exception:
            best = None
    ordered = sorted(real, key=lambda pc: pc[0].name.lower())
    if best is not None:
        ordered.sort(key=lambda pc: 0 if pc[0] == best else 1)

    out, claimed = [], set()
    for p, _cfg in ordered:
        name = _name_from(p)
        arch = _sibling_archive(p, name)
        if arch is not None and arch in own_arch:
            arch = None
        if arch is None:
            same = [a for a in real_arch
                    if a.stem.lower() == name.lower() and a not in claimed]
            if same:
                arch = same[0]
            else:
                free = [a for a in real_arch if a not in claimed]
                if len(free) == 1:
                    arch = free[0]
        if arch is not None:
            claimed.add(arch)
        out.append(Target(name, arch, p, root, "dir"))
    for a in real_arch:
        if a in claimed:
            continue
        out.append(Target(_name_from(a), a, None, root, "dir"))
    return out


def unpack(cli, target, workdir, want=None, log=None):
    """解出目标归档的资源，返回 {rel: 绝对路径}。

    want 目前只作为提示保留：WolvenKit 的 -w 是通配符而非正则，拿路径列表
    拼不出可靠的选择器，所以这里一律全量解包。大 mod（几百 MB）会慢几秒，
    换来的是「绝不漏文件」。定向解包留作后续优化。
    """
    from . import wkit

    if not target.archive_path or not target.archive_path.is_file():
        raise ArchiveError("目标没有归档文件，无法解包：%s" % target.name)

    out = Path(workdir)
    out.mkdir(parents=True, exist_ok=True)
    wkit.unbundle(cli, target.archive_path, out, log=log)
    return map_files(out)


def map_files(root):
    root = Path(root)
    return {
        str(p.relative_to(root)).replace("/", "\\"): p
        for p in root.rglob("*")
        if p.is_file()
    }


def is_cr2w_file(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"CR2W"
    except OSError:
        return False


_LANG_SEG_RE = re.compile(r"^[a-z]{2}[-_][a-z]{2}$", re.I)


def lang_of_rel(rel):
    """从归档内路径猜语言码。

        equipment_ex\\localization\\en-us.json          → en-us
        base\\localization\\en-us\\onscreens\\x.json   → en-us
        kiki_buttercup_dress_localization.json         → ""（认不出）

    认不出时返回空串：调用方必须把这类文件**全部保留**，因为它们可能装的就是
    源语言文本（作者命名毫无规律，实测有 `translation_strings.json`）。
    """
    for part in re.split(r"[\\/]", str(rel or "")):
        if not part:
            continue
        stem = part.rsplit(".", 1)[0]
        if _LANG_SEG_RE.match(stem):
            return stem.lower().replace("_", "-")
    return ""


def candidate_json(files, langs=None):
    """列出归档里值得当本地化资源检查的文件。

    为什么不用路径分类：实测 mod 的文件命名毫无规律——
        quickhack_fixes\\localization\\en-us.json
        adidas_superstar_shoes\\feet\\...\\ops\\translation_strings.json
        kiki_buttercup_dress\\paperwork\\kiki_buttercup_dress_localization.json
        base\\yv\\axl\\02_s15_local.json
    按目录/文件名猜一定会漏。

    可靠的做法：游戏里以 .json 结尾的 CR2W 资源基本只有本地化，
    所以「.json 且 CR2W」就是候选，真正的类型交给 cr2w.kind_of() 判断。

    langs 给定时按路径里的语言码再筛一道：我们只读源语言和目标语言（前者是
    原文，后者用来复用作者已有的译文），其余十几种语言序列化一次要 2.8 秒
    （进程启动占大头），纯粹白等。语言码认不出的文件一律保留。
    """
    out = []
    want = tuple(x.lower().replace("_", "-") for x in (langs or ()))
    for rel, path in files.items():
        if not rel.lower().endswith(".json"):
            continue
        if want:
            lg = lang_of_rel(rel)
            if lg and lg not in want:
                continue
        if is_cr2w_file(path):
            out.append(rel)
    return sorted(out)


def main(argv=None):
    """诊断用法：python -m localization.archive [路径] [--game 路径]"""
    import argparse

    ap = argparse.ArgumentParser(description="定位游戏 / 解析目标 mod")
    ap.add_argument("path", nargs="?", help=".archive / .xl / 目录 / .zip")
    ap.add_argument("--game", default=None)
    args = ap.parse_args(argv)

    game = find_game(args.game)
    if game:
        gi = GameInfo(game)
        print("游戏    :", game)
        print("mod 目录:", gi.mod_dir, "(存在)" if gi.mod_dir.is_dir() else "(缺失)")
        print("ArchiveXL:", gi.archivexl_dir or "(未装)")
        print("语言包  :", gi.text_archive() or "(缺失)")
        print("日志    :", gi.find_archivexl_log() or "(无)")
    else:
        print("游戏    : 未找到")

    if args.path:
        t = resolve_target(args.path)
        print("\n目标    :", t.name)
        print("归档    :", t.archive_path)
        print("配置    :", t.xl_path)
        if t.xl_cfg:
            print("extend  :", t.xl_cfg.extend or "(无)")
            for node in xlmod.NODES:
                tables = t.xl_cfg.nodes.get(node) or {}
                for lang in sorted(tables):
                    for p in tables[lang]:
                        print("   %-10s %-8s %s" % (node, lang, p))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
