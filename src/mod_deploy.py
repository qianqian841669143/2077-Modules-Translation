# -*- coding: utf-8 -*-
"""mod_deploy.py —— 把批量的输出副本按文件覆盖回原模组目录。

为什么是「按文件覆盖」而不是「删目录再拷」：输出副本天生是残缺的
（`copy_mod` 跳过 `.archive` / 贴图 / 音频 / 视频），照目录替换会把这些原文件
一起删掉，mod 直接坏。这里只写副本里真实存在、且内容与原目录不同的文件，
其余一个字节都不动。

清单 `userdata/batch/deployed.json` 记三件事，缺一不可：

    files[rel] = {"sha1": ..., "added": bool}
        部署后这份文件的指纹，`added` 表示原目录里本来没有它（我们新建的
        语言包）。`copy_mod` 靠它跳过自己上一轮的产物，判据是
        **路径命中原样 hash 相同**：用户解压还原过的文件 hash 一定变了，
        会被正常读回来，不会被永久跳过。
    pairs = [[原文, 译文], ...]
        「重译这个模组（丢弃本轮记忆）」按它对翻译记忆调 `forget()`。不清的话
        重跑会先命中 TM，把上次那份坏译文原样搬回来，看着像「改了没生效」。
    at / root
        部署时间与模组根目录，供清单与排错用。
"""
from __future__ import annotations

import json
import os
import shutil

try:
    from appconfig import config_dir
except Exception:                     # 命令行工具里单独 import 时不致命
    def config_dir():
        return os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "dist", "userdata"))

VAULT_NAME = "deployed.json"
TEXT_EXTS = (".reds", ".lua", ".json", ".txt", ".xl", ".csv", ".ini", ".toml")
MAX_PAIRS = 8000                      # 单个模组最多记这么多译文对


# ------------------------------------------------------------------ 小工具


def file_sha1(path):
    h = __import__("hashlib").sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def count_han(text):
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def _norm(root):
    return os.path.normcase(os.path.abspath(str(root or "")))


def _inside(root, path):
    r, p = _norm(root), _norm(path)
    return p == r or p.startswith(r + os.sep)


# ------------------------------------------------------------------ 清单


def vault_path():
    return os.path.join(config_dir(), "batch", VAULT_NAME)


def load():
    p = vault_path()
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("mods"), dict):
            return data
    except Exception:
        pass
    return {"version": 1, "mods": {}}


def save(data):
    p = vault_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def entry(mod_root):
    return (load().get("mods") or {}).get(_norm(mod_root)) or {}


def deployed_files(mod_root):
    """{相对路径(小写, 反斜杠): (sha1, added)}，给 copy_mod 跳过自己产物用。"""
    out = {}
    for rel, info in (entry(mod_root).get("files") or {}).items():
        if isinstance(info, dict) and info.get("sha1"):
            out[str(rel).replace("/", "\\").lower()] = (
                str(info["sha1"]), bool(info.get("added")))
    return out


def pairs(mod_root):
    """[(原文, 译文)]，供清翻译记忆用。"""
    out = []
    for item in entry(mod_root).get("pairs") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2 and str(item[0]).strip():
            out.append((str(item[0]), str(item[1])))
    return out


def record(mod_root, files=None, pairs_list=None, keep_pairs=True, done=None):
    """把一次部署的结果并进清单。

    `files` 是 {相对路径: {"sha1":..., "added":...}}，与已有条目合并
    （同一个文件重新部署时以最新一次为准）。

    `done` 记这一轮的成绩（行数 / 未译 / 失败），供「已部署且源未变就整块跳过」
    判断——上一轮还有没翻出来的行，就说明这个模组还没做完，不能跳。
    """
    data = load()
    mods = data.setdefault("mods", {})
    key = _norm(mod_root)
    cur = mods.get(key) or {}
    merged = dict(cur.get("files") or {})
    merged.update(files or {})
    new_pairs = list(cur.get("pairs") or [])
    if pairs_list and keep_pairs:
        seen = set((str(a), str(b)) for a, b in
                   ((p[0], p[1]) for p in new_pairs if len(p) == 2))
        for orig, trans in pairs_list:
            if not str(orig or "").strip():
                continue
            k = (str(orig), str(trans))
            if k in seen:
                continue
            seen.add(k)
            new_pairs.append([k[0], k[1]])
        if len(new_pairs) > MAX_PAIRS:
            new_pairs = new_pairs[-MAX_PAIRS:]
    stat = dict(cur.get("done") or {})
    if done:
        stat.update(done)
        stat["at"] = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
    mods[key] = {
        "root": os.path.abspath(str(mod_root or "")),
        "at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "files": merged,
        "pairs": new_pairs,
        "done": stat,
    }
    save(data)
    return mods[key]


def up_to_date(mod_root):
    """S6：这个模组是不是「已经部署好、源文件也没动、没有漏译」。

    返回非空字符串 = 可以整块跳过（内容就是报告里的原因）。判据全部来自清单，
    不重新解析模组：

      * 清单里记过 `done`，且上一轮 `untranslated` / `failed` 都是 0；
      * 清单里每个文件的 sha1 与磁盘现状一致 —— 这一条同时证明了两件事：
        我们写进去的译文还在，而且用户没有解压还原过（还原会把原件换回来，
        hash 立刻变）。
    """
    e = entry(mod_root)
    if not e:
        return ""
    done = e.get("done") or {}
    if not done:
        return ""
    if int(done.get("untranslated") or 0) or int(done.get("failed") or 0):
        return ""
    files = e.get("files") or {}
    if not files:
        return ""
    for rel, info in files.items():
        if not isinstance(info, dict) or not info.get("sha1"):
            return ""
        p = os.path.join(str(mod_root), str(rel).replace("\\", os.sep))
        if not os.path.isfile(p) or file_sha1(p) != str(info["sha1"]):
            return ""
    return "已部署且源文件未变"


def forget_in(mod_root, memory, lang):
    """把清单里记的译文对从翻译记忆里删掉（只删 machine/memory 来源）。

    返回删掉的条数。调用方负责 `memory.save()`。
    """
    n = 0
    for orig, _trans in pairs(mod_root):
        try:
            if memory.forget(orig, lang):
                n += 1
        except Exception:
            break
    return n


# ------------------------------------------------------------------ 部署


def deploy_mod(src_dir, dst_dir, undo_root=None, dry_run=False,
               label="批量部署", pairs_list=None, done=None, log=None):
    """把 `src_dir` 里有的文件覆盖到 `dst_dir`，返回报告。

    报告字段：
      overwritten  覆盖的文件数
      added        新增的文件数（原目录里没有）
      same         内容已经一样、跳过的文件数
      written      [相对路径]，真正写过的（dry_run 时是「将要写」）
      notes        [{path, why}]，照覆盖但值得记一笔的情况
      error        出错时的原因
    """
    rep = {"ok": True, "overwritten": 0, "added": 0, "same": 0,
           "written": [], "notes": [], "error": "", "files": {}}
    src = os.path.abspath(str(src_dir or ""))
    dst = os.path.abspath(str(dst_dir or ""))
    if not os.path.isdir(src):
        return dict(rep, ok=False, error="副本目录不存在: %s" % src)
    if not os.path.isdir(dst):
        return dict(rep, ok=False, error="原模组目录不存在: %s" % dst)
    if _inside(src, dst) or _inside(dst, src):
        return dict(rep, ok=False, error="副本与原目录互相包含，拒绝部署")

    tx = None
    if not dry_run and undo_root:
        try:
            import undo_store
            tx = undo_store.ApplyTransaction(undo_root, label)
        except Exception:
            tx = None

    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d.lower() != ".git"]
        rel_dir = os.path.relpath(dirpath, src)
        for fn in filenames:
            if fn.lower().endswith(".bak"):
                continue
            sp = os.path.join(dirpath, fn)
            rel = fn if rel_dir == "." else os.path.join(rel_dir, fn)
            dp = os.path.join(dst, rel)
            key = rel.replace("/", "\\")
            try:
                existed = os.path.isfile(dp)
                if existed:
                    if file_sha1(sp) == file_sha1(dp):
                        rep["same"] += 1
                        continue
                    why = _note_for(dp)
                    if why:
                        rep["notes"].append({"path": rel, "why": why})
                    if not dry_run:
                        if tx is not None:
                            tx.snapshot(dp)
                        os.makedirs(os.path.dirname(dp) or dst, exist_ok=True)
                        shutil.copy2(sp, dp)
                    rep["overwritten"] += 1
                else:
                    if not dry_run:
                        os.makedirs(os.path.dirname(dp) or dst, exist_ok=True)
                        shutil.copy2(sp, dp)
                    rep["added"] += 1
                rep["written"].append(key)
                # added 必须是「写之前原目录里没有」，写之后再看就永远为 False
                rep["files"][key] = {"sha1": file_sha1(sp), "added": not existed}
            except Exception as ex:      # noqa: BLE001 - 一个文件失败不代表整个模组坏
                rep["notes"].append({"path": rel, "why": "写入失败: %s" % ex})
                if log:
                    log("部署失败 %s: %s" % (rel, ex))

    if tx is not None:
        try:
            uid = tx.commit()
            if uid:
                rep["undoId"] = uid
        except Exception:
            tx.discard()

    if not dry_run and rep["files"]:
        record(dst, files=rep["files"], pairs_list=pairs_list, done=done)
    if log:
        log("部署 %s: 覆盖 %d / 新增 %d / 相同 %d" % (
            os.path.basename(dst.rstrip("\\/")), rep["overwritten"],
            rep["added"], rep["same"]))
    return rep


def _note_for(path):
    """照覆盖、但值得记一笔的情况。目前只认「原件已经是中文」。"""
    ext = os.path.splitext(path.lower())[1]
    if ext not in TEXT_EXTS:
        return ""
    try:
        if os.path.getsize(path) > (2 << 20):
            return ""
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return ""
    if count_han(text) >= 8:
        return "原件已含中文，已按覆盖策略覆盖"
    return ""
