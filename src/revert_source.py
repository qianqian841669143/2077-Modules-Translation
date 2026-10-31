# -*- coding: utf-8 -*-
"""revert_source.py —— 用下载包把模组还原成「未翻译前」的样子

为什么用下载包而不是本地快照
============================
原版文件就躺在 Vortex 的下载目录里，那是权威副本，不占额外磁盘，也不会
因为撤销记录被裁剪（undo_store 每个模组只留最近 30 次）而失真。

三种格式都能读：
    .zip        → zipfile，精确提取单个成员
    .7z / .rar  → Windows 自带的 tar.exe（bsdtar / libarchive），同样能按名提取
不用引入 py7zr / rarfile，打包体积不变。

还原的范围
==========
只碰「事务记录里出现过的路径」——也就是本工具真正写过的东西。绝不拿包去
覆盖整个模组目录：用户自己改过的、别的工具改过的文件必须原样留着。

    existed=True   → 从包里取回同名文件覆盖
    existed=False  → 本工具新建的文件，删掉

包里找不到对应文件、或者这个模组是 FOMOD（安装选项决定落盘路径）时，不猜，
如实报给界面，由调用方决定退回本地快照还是就此收手。
"""
import os
import shutil
import subprocess
import tempfile
import zipfile

import vortex_map as VM

# 包内这些顶层目录属于「模组根」本身，不能当成多余的一层前缀剥掉
ROOT_SEGMENTS = {"archive", "r6", "bin", "red4ext", "mods", "fomod",
                 "tools", "source", "engine", "content"}


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _tar_exe():
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    for name in ("tar.exe", "bsdtar.exe"):
        p = os.path.join(root, "System32", name)
        if os.path.isfile(p):
            return p
    return ""


def _norm_rel(name):
    s = str(name or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s.strip("/")


def _is_zip(path):
    return str(path).lower().endswith(".zip")


def list_entries(pkg_path):
    """列出包内条目。返回 (names, error)。names 已归一化成 / 分隔的相对路径。"""
    pkg = str(pkg_path or "")
    if not pkg or not os.path.isfile(pkg):
        return [], "下载包不存在"
    if _is_zip(pkg):
        try:
            with zipfile.ZipFile(pkg) as zf:
                names = [i.filename for i in zf.infolist() if not i.is_dir()]
        except Exception as ex:
            return [], "读取 zip 失败: %s" % ex
        return [x for x in (_norm_rel(n) for n in names) if x], ""

    tar = _tar_exe()
    if not tar:
        return [], "系统缺少 tar.exe，读不了 %s" % os.path.splitext(pkg)[1]
    try:
        proc = subprocess.run(
            [tar, "-tf", pkg],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=300, creationflags=_no_window())
    except Exception as ex:
        return [], "调用 tar 失败: %s" % ex
    if proc.returncode != 0:
        msg = (proc.stderr or b"").decode("utf-8", "replace").strip()
        return [], "tar 读取失败: %s" % (msg[:200] or "exit %d" % proc.returncode)
    text = (proc.stdout or b"").decode("utf-8", "replace")
    names = []
    for line in text.splitlines():
        rel = _norm_rel(line)
        if rel:
            names.append(rel)
    return names, ""


def looks_like_fomod(names):
    """FOMOD：装哪些文件由安装向导的选项决定，包内路径不等于装完的路径。"""
    for n in names:
        low = n.lower()
        if low == "fomod" or low.startswith("fomod/"):
            return True
        if low.endswith("moduleconfig.xml"):
            return True
    return False


def _strip_one_level(names):
    """有些包把所有东西套在 `<ModName>/` 里。返回剥掉后的名字与剥掉的那层。"""
    if not names:
        return names, ""
    firsts = set()
    for n in names:
        if "/" not in n:
            return names, ""      # 顶层直接有文件，说明没有多余前缀
        firsts.add(n.split("/", 1)[0])
    if len(firsts) != 1:
        return names, ""
    head = firsts.pop()
    if head.lower() in ROOT_SEGMENTS:
        return names, ""
    return [n.split("/", 1)[1] for n in names], head


def build_index(pkg_path, wanted):
    """返回 (index, prefix, fomod, error)。

    index 的键是「模组相对路径」的小写形式，值是包内真正的成员名——两者可能
    不同：有的包把所有内容套在 `<ModName>/` 里，匹配要按剥掉后的路径，提取还得
    用带前缀的原名。
    """
    names, err = list_entries(pkg_path)
    if err:
        return {}, "", False, err
    if not names:
        return {}, "", False, "包是空的"
    fomod = looks_like_fomod(names)
    flat, head = _strip_one_level(names)

    def score(ns):
        return len({n.lower() for n in ns} & wanted)

    if score(flat) > score(names):
        pairs = [(n.split("/", 1)[1], n) for n in names if "/" in n]
        prefix = head
    else:
        pairs = [(n, n) for n in names]
        prefix = ""
    index = {}
    for logical, real in pairs:
        index.setdefault(logical.lower(), real)
    return index, prefix, fomod, ""


# ------------------------------------------------------------------ 选包


def _downloads(downloads_dir):
    out = []
    d = (downloads_dir or "").strip().strip('"')
    if not d or not os.path.isdir(d):
        return out
    try:
        entries = list(os.scandir(d))
    except Exception:
        return out
    for e in entries:
        try:
            if e.is_file() and e.name.lower().endswith(VM.ARCHIVE_EXTS):
                out.append((e.name, e.path))
            elif e.is_dir():
                for sub in os.scandir(e.path):
                    if sub.is_file() and sub.name.lower().endswith(VM.ARCHIVE_EXTS):
                        out.append((sub.name, sub.path))
        except Exception:
            continue
    out.sort(key=lambda kv: kv[0].lower())
    return out


def find_packages(root, downloads_dir):
    """按 staging 目录名里的 modId 找下载包。

    返回 (same_version, other_version, note)。same_version 是版本一致的那些包
    ——一个 mod 可能拆成主文件加补丁，同版本的包要一起用才是完整原始状态。
    """
    name = os.path.basename(os.path.abspath(str(root or "")))
    _title, mid, ver = VM.parse_name(name)
    if mid is None:
        return [], [], "目录名里没有 modId，找不到对应的下载包"
    same, other = [], []
    want = VM.norm_version(ver)
    for fname, path in _downloads(downloads_dir):
        _t, m, v = VM.parse_name(fname)
        if m != mid:
            continue
        row = {"name": fname, "path": path, "version": v}
        if VM.norm_version(v) == want:
            same.append(row)
        else:
            other.append(row)
    if not same and not other:
        return [], [], "下载目录里没有这个 mod 的包"
    return same, other, ""


# ------------------------------------------------------------------ 计划


def _inside(root, path):
    try:
        return os.path.normcase(os.path.commonpath([root, path])) == \
            os.path.normcase(root)
    except Exception:
        return False


# 计划里用这个假包名标记「这一项来自本地快照，不是下载包」
SNAPSHOT_TAG = "@snapshot"


def build_plan(root, whitelist, downloads_dir="", snapshots=None):
    """算出「从哪取、取什么、删什么」。

    whitelist: {相对路径: 第一次写之前是否存在}
    snapshots: {相对路径: 最早快照文件}，文件不存在于下载包时用它兜底
    返回的 dict 可以直接 JSON 序列化给前端做确认框。
    """
    root = os.path.abspath(str(root or ""))
    plan = {
        "ok": False, "root": root, "source": "", "packages": [],
        "restore": [], "remove": [], "skip": [],
        "warnings": [], "error": "", "fomod": False, "drift": False,
        "usedSnapshot": False, "firstAt": "", "records": 0,
    }
    if not root or not os.path.isdir(root):
        plan["error"] = "模组目录不存在"
        return plan
    if not whitelist:
        plan["error"] = "这个模组没有本工具的写回记录，没有可还原的内容"
        return plan

    wanted = {str(r).lower() for r in whitelist}
    same, other, note = find_packages(root, downloads_dir)
    if note:
        plan["warnings"].append(note)

    # 版本一致优先；一个都没有时才用版本不符的包，并明确告诉用户会把模组
    # 换成包里那一版——比留着中文译文强，但不能不说。
    chosen = same or other
    plan["drift"] = bool(other and not same)
    if plan["drift"]:
        plan["warnings"].append(
            "没有与安装版本一致的包，改用版本 %s 的包，模组文件会变成包里那一版"
            % "、".join(sorted({x["version"] or "?" for x in other})))
    elif same and other:
        plan["warnings"].append(
            "下载目录里还有 %d 个版本不符的包，没有使用" % len(other))

    index = {}
    for p in chosen:
        idx, prefix, fomod, err = build_index(p["path"], wanted)
        if err:
            plan["warnings"].append("%s: %s" % (p["name"], err))
            continue
        if fomod:
            plan["fomod"] = True
            plan["warnings"].append(
                "%s 是 FOMOD 安装包，包内路径不等于装完的路径，跳过" % p["name"])
            continue
        plan["packages"].append({"name": p["name"], "version": p["version"],
                                 "prefix": prefix})
        for low, real in idx.items():
            index.setdefault(low, (p["path"], real))

    if plan["packages"]:
        plan["source"] = "package"

    snap_by_low = {}
    for rel, snap in (snapshots or {}).items():
        snap_by_low[str(rel).replace("\\", "/").lower()] = snap

    removed_seen = set()
    for rel, existed in whitelist.items():
        rel = str(rel).replace("\\", "/")
        hit = index.get(rel.lower())
        if hit:
            pkg_path, member = hit
            plan["restore"].append({"rel": rel, "package": pkg_path,
                                    "member": member})
        elif not existed:
            if rel.lower() in removed_seen:
                continue
            removed_seen.add(rel.lower())
            plan["remove"].append(rel)
        else:
            # 包里没有：退回本地快照（第一次写之前存下来的原件）。
            snap = snap_by_low.get(rel.lower())
            if snap and os.path.isfile(snap):
                plan["restore"].append({"rel": rel, "package": SNAPSHOT_TAG,
                                        "member": snap})
                plan["usedSnapshot"] = True
            else:
                plan["skip"].append({"rel": rel,
                                     "reason": "包里和本地快照里都没有"})
    if plan["usedSnapshot"]:
        if not plan["source"]:
            plan["source"] = "snapshot"
        plan["warnings"].append("有文件是从本地快照还原的（下载包里没有）")
    plan["ok"] = bool(plan["restore"] or plan["remove"])
    if not plan["ok"]:
        plan["error"] = "没有可还原的文件（包内路径对不上，也没有可删除的新建文件）"
    return plan


# ------------------------------------------------------------------ 执行


def _extract_zip(pkg_path, members, dest):
    """从 zip 里精确取出几个成员到 dest，返回 {member: 解出来的绝对路径}。"""
    out = {}
    with zipfile.ZipFile(pkg_path) as zf:
        for m in members:
            try:
                zf.extract(m, dest)
            except Exception:
                continue
            p = os.path.join(dest, m.replace("/", os.sep))
            if os.path.isfile(p):
                out[m] = p
    return out


def _extract_tar(pkg_path, members, dest):
    tar = _tar_exe()
    if not tar:
        return {}
    out = {}
    for m in members:
        try:
            proc = subprocess.run(
                [tar, "-xf", pkg_path, "-C", dest, "--", m],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=300, creationflags=_no_window())
        except Exception:
            continue
        p = os.path.join(dest, m.replace("/", os.sep))
        if proc.returncode == 0 and os.path.isfile(p):
            out[m] = p
    return out


def _extract(pkg_path, members, dest):
    if not members:
        return {}
    if _is_zip(pkg_path):
        return _extract_zip(pkg_path, members, dest)
    return _extract_tar(pkg_path, members, dest)


def run_plan(plan):
    """执行计划。返回报告；每个文件单独尽力，坏一个不影响其它。"""
    root = plan.get("root") or ""
    restored, removed, errors = [], [], []
    tmp = tempfile.mkdtemp(prefix="mt-revert-")
    try:
        by_pkg = {}
        for item in plan.get("restore") or []:
            by_pkg.setdefault(item["package"], []).append(item)
        for pkg, items in by_pkg.items():
            if pkg == SNAPSHOT_TAG:
                # 本地快照本身就是文件路径，不用解包
                got = {it["member"]: it["member"] for it in items}
            else:
                got = _extract(pkg, [i["member"] for i in items], tmp)
            for it in items:
                src = got.get(it["member"])
                if not src:
                    errors.append({"path": it["rel"], "reason": "从包里解不出来"})
                    continue
                dst = os.path.abspath(os.path.join(root, it["rel"].replace("/", os.sep)))
                if not _inside(root, dst):
                    errors.append({"path": it["rel"], "reason": "路径越界"})
                    continue
                try:
                    os.makedirs(os.path.dirname(dst) or root, exist_ok=True)
                    shutil.copy2(src, dst)
                    restored.append(it["rel"])
                except Exception as ex:
                    errors.append({"path": it["rel"], "reason": str(ex)})

        for rel in plan.get("remove") or []:
            dst = os.path.abspath(os.path.join(root, rel.replace("/", os.sep)))
            if not _inside(root, dst):
                errors.append({"path": rel, "reason": "路径越界"})
                continue
            try:
                if os.path.isfile(dst):
                    os.remove(dst)
                    removed.append(rel)
                    # 产物清掉后留下的空目录（如新建的语言包目录）顺手收走
                    _prune_empty(os.path.dirname(dst), root)
            except Exception as ex:
                errors.append({"path": rel, "reason": str(ex)})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return {
        "ok": not errors,
        "restored": restored,
        "removed": removed,
        "errors": errors[:50],
        "files": len(restored) + len(removed),
    }


def _prune_empty(path, root):
    """删掉因删除文件而空掉、且仍在模组根之内的目录。"""
    cur = os.path.abspath(path)
    root = os.path.abspath(root)
    while _inside(root, cur) and os.path.normcase(cur) != os.path.normcase(root):
        try:
            if os.listdir(cur):
                return
            os.rmdir(cur)
        except Exception:
            return
        cur = os.path.dirname(cur)
