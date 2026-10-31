# -*- coding: utf-8 -*-
"""vortex_map.py —— Vortex 三层映射：下载包 / 安装缓存 / 游戏部署

为什么要做这层映射
==================
这个工具实际改写的是「安装缓存目录」(staging)。Vortex 用硬链接把它和游戏目录
连起来：同一个 inode 有两条路径。所以

    staging 文件  ==  游戏里的文件

改 staging 就是改游戏。但这也意味着模组更新/卸载时 staging 目录会被整个替换，
写在里面的译文就跟着没了。要判断「哪些目录还会被游戏读到」「哪些已经作废」，
就必须知道 Vortex 自己的部署状态。

三个数据源
==========
1. 下载包名（只读、不变、可作版本锚）
   Nexus 经典:  Title-12345-1-2-3-1700000000.zip
   Nexus 新版:  Title 12345 1.2.3 2026-10-24T18-07Z AbCdEfGhi.zip
2. 安装缓存目录名（Vortex 直接用「压缩包名去扩展名」建目录，同样带 modId + 版本）
3. <staging>/vortex.deployment.msgpack —— Vortex 每次部署都会重写这份清单，
   files[].source 就是「这个游戏文件属于哪个 staging 目录」。
   它不出现的目录 = 没生效的陈旧副本。

本模块只读不写，也不做任何翻译。解析失败一律退回「未知」，不抛异常给界面。
"""
import os
import re
import struct
import time

# 下载包扩展名（Nexus 上能下到的就这几类）
ARCHIVE_EXTS = (".zip", ".rar", ".7z", ".7zz", ".tar", ".gz", ".tgz", ".bz2", ".xz")

# 集合目录不是模组，不参与翻译
COLLECTION_PREFIX = "vortex_collection_"

# A) 经典格式：Title-12345-1-2-3-1700000000（版本段也可能带字母，如 v1-0-0）
RE_A = re.compile(r"^(?P<title>.+?)-(?P<mid>\d{3,7})-(?P<ver>[A-Za-z0-9][\w.\-]*?)-"
                  r"(?P<ts>\d{9,11})$")
# B) 新版格式：Title 12345 1.2.3 2026-10-24T18-07Z AbCdEfGhi
RE_B = re.compile(
    r"^(?P<title>.+?)\s(?P<mid>\d{3,7})\s+(?P<ver>\S+?)\s+"
    r"(?P<ts>\d{4}-\d{2}-\d{2}T[\d\-]+Z)\s+[A-Za-z0-9_]+$")
# C) 兜底：Title-12345-something（没有时间戳，常见于手动改名的包）
RE_C = re.compile(r"^(?P<title>.+?)-(?P<mid>\d{3,7})-(?P<ver>[^\-]+(?:-[^\-]+)*)$")
# D) 没有时间戳也没有短横：Title 12345 1.2.3（旧版 Vortex / 手动解压的目录）
RE_D = re.compile(r"^(?P<title>.+?)\s(?P<mid>\d{3,7})\s+(?P<ver>v?\d[\w.]*)$")

_STATE_ORDER = ("active", "update", "drift", "pending", "stale", "orphan",
                "collection", "unparsed")


# ============================================================ msgpack
#
# Vortex 的部署清单是 msgpack。这里带一个只够用的解码器：不依赖第三方库，
# 打包成单文件 exe 时也不会多一个依赖。


class _Unpacker(object):
    __slots__ = ("b", "i", "n")

    def __init__(self, buf):
        self.b = buf
        self.i = 0
        self.n = len(buf)

    def _take(self, k):
        v = self.b[self.i:self.i + k]
        if len(v) != k:
            raise ValueError("msgpack 数据不完整")
        self.i += k
        return v

    def _uint(self, k):
        return int.from_bytes(self._take(k), "big")

    def _str(self, k):
        return self._take(k).decode("utf-8", "replace")

    def _arr(self, k):
        return [self.unpack() for _ in range(k)]

    def _map(self, k):
        out = {}
        for _ in range(k):
            key = self.unpack()
            out[key] = self.unpack()
        return out

    def unpack(self):
        if self.i >= self.n:
            raise ValueError("msgpack 已到末尾")
        c = self.b[self.i]
        self.i += 1
        if c <= 0x7F:
            return c
        if c >= 0xE0:
            return c - 0x100
        if 0x80 <= c <= 0x8F:
            return self._map(c & 0x0F)
        if 0x90 <= c <= 0x9F:
            return self._arr(c & 0x0F)
        if 0xA0 <= c <= 0xBF:
            return self._str(c & 0x1F)
        if c == 0xC0:
            return None
        if c == 0xC2:
            return False
        if c == 0xC3:
            return True
        if c == 0xC4:
            return self._take(self._uint(1))
        if c == 0xC5:
            return self._take(self._uint(2))
        if c == 0xC6:
            return self._take(self._uint(4))
        if c == 0xCA:
            return struct.unpack(">f", self._take(4))[0]
        if c == 0xCB:
            return struct.unpack(">d", self._take(8))[0]
        if c == 0xCC:
            return self._uint(1)
        if c == 0xCD:
            return self._uint(2)
        if c == 0xCE:
            return self._uint(4)
        if c == 0xCF:
            return self._uint(8)
        if c == 0xD0:
            return int.from_bytes(self._take(1), "big", signed=True)
        if c == 0xD1:
            return int.from_bytes(self._take(2), "big", signed=True)
        if c == 0xD2:
            return int.from_bytes(self._take(4), "big", signed=True)
        if c == 0xD3:
            return int.from_bytes(self._take(8), "big", signed=True)
        if c == 0xD9:
            return self._str(self._uint(1))
        if c == 0xDA:
            return self._str(self._uint(2))
        if c == 0xDB:
            return self._str(self._uint(4))
        if c == 0xDC:
            return self._arr(self._uint(2))
        if c == 0xDD:
            return self._arr(self._uint(4))
        if c == 0xDE:
            return self._map(self._uint(2))
        if c == 0xDF:
            return self._map(self._uint(4))
        raise ValueError("msgpack 未知类型 0x%02X" % c)


def unpackb(buf):
    return _Unpacker(buf).unpack()


def deployment_file(staging_dir):
    return os.path.join(staging_dir or "", "vortex.deployment.msgpack")


def deployment_info(staging_dir):
    """读 <staging>/vortex.deployment.msgpack，返回生效的 staging 目录集合。

    只做这一件事，所以很快（几百 KB 解码，毫秒级），可以每次加载模组时调用。
    """
    path = deployment_file(staging_dir)
    info = {"ok": False, "path": path, "sources": {}, "files": 0,
            "deployment_time": 0, "staging_path": "", "target_path": "",
            "method": "", "error": ""}
    if not staging_dir or not os.path.isfile(path):
        info["error"] = "没有找到 vortex.deployment.msgpack"
        return info
    try:
        with open(path, "rb") as f:
            data = unpackb(f.read())
    except Exception as ex:
        info["error"] = "部署清单解析失败: %s" % ex
        return info
    if not isinstance(data, dict):
        info["error"] = "部署清单格式不认识"
        return info
    files = data.get("files") or []
    sources = {}
    for f in files:
        if not isinstance(f, dict):
            continue
        src = f.get("source") or ""
        if not isinstance(src, str):
            continue
        top = src.replace("\\", "/").split("/")[0].strip()
        if not top:
            continue
        sources[top] = sources.get(top, 0) + 1
    info.update({
        "ok": True,
        "sources": sources,
        "files": len(files),
        "deployment_time": int(data.get("deploymentTime") or 0),
        "staging_path": data.get("stagingPath") or "",
        "target_path": data.get("targetPath") or "",
        "method": data.get("deploymentMethod") or "",
        "error": "",
    })
    return info


# ============================================================ 名字解析


def strip_ext(name):
    low = name.lower()
    for ext in ARCHIVE_EXTS:
        if low.endswith(ext):
            return name[: -len(ext)]
    return name


def parse_name(name):
    """从下载包名/目录名里拆出 (标题, modId, 版本)。拆不出返回 (name, None, '')。"""
    base = strip_ext(os.path.basename(name or "")).strip()
    if not base:
        return "", None, ""
    for rx in (RE_B, RE_A, RE_D, RE_C):
        m = rx.match(base)
        if not m:
            continue
        try:
            mid = int(m.group("mid"))
        except Exception:
            continue
        title = (m.group("title") or "").strip(" -_")
        ver = (m.group("ver") or "").strip()
        if not title:
            title = base
        return title, mid, ver
    return base, None, ""


def norm_version(ver):
    """版本号归一化：2-8-1 / 2.8.1 / v2.8.1 / 1.0.0.hf1 都变成可比较的字符串。

    不能直接字符串比大小，也不能拿原始串比相等——同一个版本 Vortex 会用
    「2-8-1」和「2.8.1」两种写法。这里只做「是否同一版本」的判断，
    不做「谁更新」的排序（排序交给文件修改时间）。
    """
    s = str(ver or "").strip().lower()
    if s.startswith("v"):
        s = s[1:]
    s = s.replace("_", ".").replace("-", ".")
    s = re.sub(r"(?<=\d)(?=[a-z])", ".", s)      # 6hf1 -> 6.hf1
    s = re.sub(r"[^0-9a-z.]+", ".", s)
    s = re.sub(r"\.{2,}", ".", s).strip(".")
    # 末尾的 .0 在版本号里没有意义：1.0 == 1.0.0
    while s.endswith(".0"):
        s = s[:-2]
    return s


def version_key(ver):
    """把版本号变成可比较的数字元组。只为判断「下载包比装的新」，
    拿不到数字就返回 ()，调用方据此放弃比较。"""
    parts = re.findall(r"\d+", norm_version(ver))
    if not parts:
        return ()
    return tuple(int(p) for p in parts)


def _newer(a, b):
    """a 是否比 b 新；任一侧无数字则返回 False（不猜）。"""
    ka, kb = version_key(a), version_key(b)
    if not ka or not kb:
        return False
    return ka > kb


def _mtime(path):
    try:
        return int(os.path.getmtime(path))
    except Exception:
        return 0


def fmt_time(ms):
    """Vortex 的 deploymentTime 是毫秒时间戳 -> 'YYYY-mm-dd HH:MM'"""
    try:
        ms = int(ms)
    except Exception:
        return ""
    if ms <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000.0))
    except Exception:
        return ""


_fmt_ts = fmt_time


# ============================================================ 目录探测


def _log_candidates():
    """从 Vortex 日志尾巴里捞路径。Vortex 每次启动都会打
    {"stagingPath":"...","vortexPath":"...","gamePath":"..."}，很可靠。"""
    out = {"staging": [], "downloads": [], "game": []}
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    vdir = os.path.join(appdata, "Vortex")
    if not os.path.isdir(vdir):
        return out
    try:
        names = [n for n in os.listdir(vdir)
                 if n.lower().startswith("vortex") and n.lower().endswith(".log")]
    except Exception:
        return out
    rx = re.compile(r'"(stagingPath|installationPath|gamePath|archivePath)"\s*:\s*"'
                    r'([A-Za-z]:\\\\[^"]{2,300})"')
    for name in names:
        path = os.path.join(vdir, name)
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                if size > 600000:
                    f.seek(-600000, os.SEEK_END)
                tail = f.read().decode("utf-8", "replace")
        except Exception:
            continue
        for key, raw in rx.findall(tail):
            val = raw.replace("\\\\", "\\")
            if key in ("stagingPath", "installationPath"):
                out["staging"].append(val)
            elif key == "gamePath":
                out["game"].append(val)
            else:
                out["downloads"].append(os.path.dirname(val))
    return out


def _download_name_hint(path):
    """"…\\VortexDownloads\\cyberpunk2077" 这类目录名本身就是线索"""
    low = (path or "").lower()
    return ("download" in low) or low.rstrip("\\/").endswith("downloads")


def detect(game_dir=""):
    """猜下载包目录 / 安装缓存目录。猜不到就返回空串，交给用户手填。

    判据很硬：只有目录里真的有 vortex.deployment.msgpack，才算安装缓存目录。
    """
    hints = _log_candidates()
    staging, downloads = "", ""

    seen = []
    for cand in hints["staging"]:
        cand = os.path.normpath(cand)
        if cand in seen:
            continue
        seen.append(cand)
        if os.path.isfile(deployment_file(cand)):
            staging = cand
            break

    # 日志里没有就按常见布局猜几个（覆盖非默认安装位置）
    if not staging:
        roots = ["Vortex Mods", "Vortex\\Mods", "Vortex", "Games\\Vortex Mods"]
        for drive in "CDEFGH":
            for rel in roots:
                cand = "%s:\\%s\\cyberpunk2077" % (drive, rel)
                if os.path.isfile(deployment_file(cand)):
                    staging = cand
                    break
            if staging:
                break

    # 下载目录：优先取日志里 archivePath 的父目录（且不像 staging）
    for cand in hints["downloads"]:
        cand = os.path.normpath(cand)
        if not os.path.isdir(cand) or cand == staging:
            continue
        if _download_name_hint(cand):
            downloads = cand
            break
    if not downloads:
        for cand in hints["downloads"]:
            cand = os.path.normpath(cand)
            if os.path.isdir(cand) and cand != staging:
                downloads = cand
                break
    if not downloads and staging:
        # 常见命名：安装缓存的同级目录 / 上一级的 Downloads
        parent = os.path.dirname(staging)
        for name in ("VortexDownloads", "downloads", "Downloads"):
            cand = os.path.join(parent, name)
            if os.path.isdir(cand):
                downloads = cand
                break
        if not downloads:
            drive = os.path.splitdrive(staging)[0]
            for name in ("VortexDownloads", "Downloads"):
                cand = os.path.join(drive + os.sep, name)
                if os.path.isdir(cand) and os.path.isdir(os.path.join(cand, "cyberpunk2077")):
                    downloads = os.path.join(cand, "cyberpunk2077")
                    break

    game = ""
    for cand in hints["game"]:
        cand = os.path.normpath(cand)
        if os.path.isdir(cand):
            game = cand
            break
    if not game and game_dir:
        game = game_dir

    return {"ok": bool(staging or downloads), "staging": staging,
            "downloads": downloads, "game": game}


# ============================================================ 扫描


def _list_archives(downloads_dir):
    """列下载包。Vortex 一般平铺，但允许一层子目录。"""
    out = []
    if not downloads_dir or not os.path.isdir(downloads_dir):
        return out
    try:
        entries = list(os.scandir(downloads_dir))
    except Exception:
        return out
    for e in entries:
        try:
            if e.is_file() and e.name.lower().endswith(ARCHIVE_EXTS):
                out.append((e.name, e.path))
            elif e.is_dir():
                for sub in os.scandir(e.path):
                    if sub.is_file() and sub.name.lower().endswith(ARCHIVE_EXTS):
                        out.append((sub.name, sub.path))
        except Exception:
            continue
    out.sort(key=lambda kv: kv[0].lower())
    return out


def _list_staging(staging_dir):
    dirs = []
    if not staging_dir or not os.path.isdir(staging_dir):
        return dirs
    try:
        for e in os.scandir(staging_dir):
            if e.is_dir():
                dirs.append((e.name, e.path))
    except Exception:
        return dirs
    dirs.sort(key=lambda kv: kv[0].lower())
    return dirs


STATE_LABEL = {
    "active": "生效",
    "update": "有更新待装",
    "drift": "版本不一致",
    "pending": "新版未部署",
    "stale": "陈旧未生效",
    "orphan": "无下载包",
    "collection": "合集(不翻译)",
    "unparsed": "名字无法解析",
    "not_installed": "已下载未安装",
}


def scan(downloads_dir="", staging_dir="", game_dir=""):
    """三层扫描。返回给界面直接渲染的结构，行内字段都可 JSON 序列化。"""
    downloads_dir = (downloads_dir or "").strip().strip('"')
    staging_dir = (staging_dir or "").strip().strip('"')
    game_dir = (game_dir or "").strip().strip('"')

    dep = deployment_info(staging_dir)
    active_map = dep.get("sources") or {}

    archives = _list_archives(downloads_dir)
    dirs = _list_staging(staging_dir)

    # 下载包按 modId 归组：一个 mod 可能有多个包（多版本 / 多文件）
    by_mid = {}
    arch_rows = []
    for name, path in archives:
        title, mid, ver = parse_name(name)
        row = {"name": name, "path": path, "title": title, "mod_id": mid,
               "version": ver or "", "mtime": _mtime(path),
               "size": 0}
        try:
            row["size"] = os.path.getsize(path)
        except Exception:
            pass
        arch_rows.append(row)
        if mid is not None:
            by_mid.setdefault(mid, []).append(row)

    rows = []
    for name, path in dirs:
        title, mid, ver = parse_name(name)
        files = active_map.get(name, 0)
        active = files > 0
        row = {"dir": name, "path": path, "title": title, "mod_id": mid,
               "version": ver or "", "active": active, "files": files,
               "archive": "", "archive_version": "", "archive_mtime": 0,
               "dir_mtime": _mtime(path), "state": "", "drift": False,
               "newer": False}

        if name.startswith(COLLECTION_PREFIX):
            row["state"] = "collection"
            rows.append(row)
            continue
        if mid is None:
            row["state"] = "unparsed"
            row["archive"] = ""
            rows.append(row)
            continue

        cands = by_mid.get(mid) or []
        best = None
        if cands:
            # 优先挑版本号一致的包；否则挑最新的包
            want = norm_version(ver)
            same = [c for c in cands if norm_version(c["version"]) == want]
            best = max(same or cands, key=lambda c: c["mtime"])
            row["archive"] = best["name"]
            row["archive_version"] = best["version"]
            row["archive_mtime"] = best["mtime"]
            row["drift"] = bool(want) and norm_version(best["version"]) != want

        if active:
            if row["drift"] and _newer(best["version"], ver):
                row["newer"] = True
                row["state"] = "update"
            elif row["drift"]:
                row["state"] = "drift"
            else:
                row["state"] = "active"
        elif best is not None:
            row["newer"] = _newer(best["version"], ver)
            row["state"] = "stale"          # 下面第二遍可能升级成 pending
        else:
            row["state"] = "orphan"
        rows.append(row)

    # 第二遍：某个 modId 一个生效目录都没有，而下载包里已经是更新版本，
    # 说明「新版下了还没部署」——和单纯的历史残留要分开报。
    live_ids = {r["mod_id"] for r in rows
                if r["mod_id"] is not None
                and r["state"] in ("active", "update", "drift")}
    for r in rows:
        if (r["state"] == "stale" and r.get("newer")
                and r["mod_id"] not in live_ids):
            r["state"] = "pending"

    # 下载包有、缓存里却没有目录的（下了没装）
    have = {r["mod_id"] for r in rows if r["mod_id"] is not None}
    for mid, cands in by_mid.items():
        if mid in have:
            continue
        best = max(cands, key=lambda c: c["mtime"])
        rows.append({"dir": "", "path": "", "title": best["title"],
                     "mod_id": mid, "version": "", "active": False, "files": 0,
                     "archive": best["name"],
                     "archive_version": best["version"],
                     "archive_mtime": best["mtime"], "dir_mtime": 0,
                     "state": "not_installed", "drift": False, "newer": True})

    stats = {k: 0 for k in _STATE_ORDER}
    stats["not_installed"] = 0
    for r in rows:
        stats[r["state"]] = stats.get(r["state"], 0) + 1
    deployed_ids = {r["mod_id"] for r in rows if r["mod_id"] is not None and r["active"]}
    all_ids = {r["mod_id"] for r in rows if r["mod_id"] is not None}
    stats.update({
        "rows": len(rows),
        "stagingDirs": len(dirs),
        "archives": len(archives),
        "deployed": len(active_map),
        "deployedFiles": dep.get("files", 0),
        "modIds": len(all_ids),
        "activeIds": len(deployed_ids),
        "archivesByExt": _count_ext([n for n, _p in archives]),
    })

    warnings = []
    if not staging_dir or not os.path.isdir(staging_dir):
        warnings.append("安装缓存目录不存在，无法判断哪些模组生效")
    elif not dep.get("ok"):
        warnings.append(dep.get("error") or "读不到部署清单，无法判断生效状态")
    if not downloads_dir or not os.path.isdir(downloads_dir):
        warnings.append("下载包目录不存在，只能按安装缓存目录列清单")

    return {
        "ok": True,
        "downloads": {"path": downloads_dir, "count": len(archives)},
        "staging": {"path": staging_dir, "count": len(dirs)},
        "game": game_dir,
        "deployment": {
            "ok": dep.get("ok", False),
            "path": dep.get("path", ""),
            "files": dep.get("files", 0),
            "sources": len(active_map),
            "method": dep.get("method", ""),
            "deploymentTime": dep.get("deployment_time", 0),
            "deployedAt": _fmt_ts(dep.get("deployment_time", 0)),
            "stagingPath": dep.get("staging_path", ""),
            "targetPath": dep.get("target_path", ""),
            "error": dep.get("error", ""),
        },
        "rows": rows,
        "archiveRows": arch_rows,
        "stats": stats,
        "labels": dict(STATE_LABEL),
        "warnings": warnings,
    }


def _count_ext(names):
    out = {}
    for n in names:
        ext = os.path.splitext(n)[1].lower().lstrip(".")
        out[ext] = out.get(ext, 0) + 1
    return out


def summary_line(res):
    """给状态栏/提示用的一句话"""
    st = (res or {}).get("stats") or {}
    return ("生效 %d · 有更新 %d · 新版未部署 %d · 陈旧 %d · 无包 %d · 合集 %d"
            " / 共 %d 个目录"
            % (st.get("active", 0), st.get("update", 0), st.get("pending", 0),
               st.get("stale", 0), st.get("orphan", 0),
               st.get("collection", 0), st.get("stagingDirs", 0)))
