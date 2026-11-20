# -*- coding: utf-8 -*-
"""mod_pack.py —— 生成汉化覆盖包，合并进 zzz_CP2077_mod_fix.zip。

原模组**一行不动**：需要覆盖的文件先复制到临时目录，只改副本，
再连同账本、CET 运行时 mod 一起合并进同一个 zip。

    原模组（staging）      只读
        │
        ├─ 复制副本 → 临时目录 → 改副本（apply_many_ex）
        │
        └─ 合并进 zzz_CP2077_mod_fix.zip
              ├─ bin/x64/plugins/.../mods/zzz_CP2077_mod_fix/ledger/*.lua
              ├─ bin/x64/plugins/.../mods/zzz_mod_text_override/init.lua
              ├─ r6/scripts/<模组>/Localization/*.reds
              ├─ r6/tweaks/<模组>/*.yaml
              └─ archive/pc/mod/<模组>_zh-cn.archive + .xl

分流规则（2026-11-20 定稿，方案 A）：
    含设置注册的文件  → 整份不进包，只走账本（改了可能整模组报废）
    语言包 / tweakdb  → 进包
    其余源码           → 进包
    redscript（.reds）→ 只有「整目录都是 Codeware 本地化包」才进包，
                          逻辑 / UI 脚本一条都不投（见 is_localization_package）
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import zipfile

STATE_NAME = "zh_state.json"
STATE_VERSION = 1

# 文件里出现这些标记，说明它参与了 MCM / CET 的设置注册。
# 这份文件整份不进包 —— 2026-11 发生过「设置项被翻 → 整个模组不可用」。
_SETTING_MARKS = (
    "@runtimeProperty",
    "ModSettings",
    "registerHotkey",
    "registerForEvent",
)

# 这些扩展名永远不进包（二进制 / 资源 / 界面资源）
_NEVER_PACK_EXT = (
    ".archive", ".xl", ".dds", ".png", ".jpg", ".jpeg", ".tga",
    ".wav", ".ogg", ".mp3", ".bk2", ".ink", ".inkatlas", ".inkwidget",
    ".bin", ".dll", ".exe", ".pyd", ".pak", ".dat",
)

# 语言包 / 本地化：这些文件里的文案必须靠覆盖文件生效，账本拦不到
_LOCAL_HINTS = ("localization/", "localisation/", "/locale", "languages/")

# Codeware 的本地化包只有这两个基类。只有它们能当覆盖文件单独投递：
# 内容就是一张「key → 原文」的表，不 import 本模组的其它编译单元，
# 所以模组被禁用 / 升级后，我们的副本孤零零留着也不会让谁缺声明。
_L10N_MARKS = ("ModLocalizationPackage", "ModLocalizationProvider")


def is_localization_text(text):
    """redscript 源码是不是 Codeware 的本地化包（纯文本表）。

    2026-11-20 的教训：以前把所有 .reds 都当覆盖文件投，等于只投了
    某个模块的一部分编译单元。模组一被禁用（或升级换了文件名），
    游戏里就只剩我们的副本，`module X` 的声明文件不在 → 整条依赖链
    UNRESOLVED_IMPORT → redscript 报 214 条错 → RedRequire 把游戏拦在
    启动前（「您的模组脚本未编译」）。所以默认一条逻辑脚本都不投。
    """
    if not text:
        return False
    return any(m in text for m in _L10N_MARKS)


def is_localization_package(path):
    """is_localization_text 的磁盘版；读不到就当不是（宁可少投）。"""
    try:
        if os.path.getsize(path) > 2 << 20:
            return False
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return is_localization_text(f.read())
    except OSError:
        return False


def sha1_file(path):
    """文件的 sha1；读不到返回空串。"""
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def sha1_text(s):
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 状态库
def state_path(settings):
    """zh_state.json 的路径，跟 memory.json 放一起（userdata 下）。"""
    base = ""
    try:
        import tm
        base = os.path.dirname(tm.Memory().path)
    except Exception:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "dist", "userdata")
    return os.path.abspath(os.path.join(base, STATE_NAME))


def load_state(path):
    """读状态库；损坏时返回空骨架（不抛，免得一个坏 json 卡住整条流程）。"""
    if not path or not os.path.isfile(path):
        return {"version": STATE_VERSION, "mods": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return {"version": STATE_VERSION, "mods": {}}
    if not isinstance(st, dict) or not isinstance(st.get("mods"), dict):
        return {"version": STATE_VERSION, "mods": {}}
    st.setdefault("version", STATE_VERSION)
    return st


def save_state(path, st):
    """原子写：先写临时文件再替换。"""
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def mod_record(st, mod_dir):
    """取某模组的记录（没有就建一条空壳）。"""
    name = os.path.basename(mod_dir.rstrip("\\/"))
    rec = st.setdefault("mods", {}).setdefault(name, {})
    rec.setdefault("files", {})
    return rec


# ---------------------------------------------------------------- 分流
def is_settings_file(path):
    """整份读一遍，判断它是否参与设置注册。

    只做子串查找：比解析语法便宜得多，而且这些标记一旦出现，
    这份文件就已经在设置链上了，宁可错杀不可漏杀。
    """
    try:
        if os.path.getsize(path) > 4 << 20:
            return False
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    return any(m in text for m in _SETTING_MARKS)


def route(rel, typ, has_settings=False, reds_lang_ok=False):
    """决定一条行走哪条通道。

    返回 "ledger" / "pack" / "skip"。

    reds_lang_ok：这个 .reds 所在的目录是否「整目录都是 Codeware
    本地化包」（由 classify 预先算好）。逻辑 / UI 脚本永远是 False。
    """
    low = (rel or "").lower().replace("\\", "/")
    ext = os.path.splitext(low)[1]

    if ext in _NEVER_PACK_EXT:
        return "archive" if ext in (".archive", ".xl") else "skip"

    # 含设置注册的文件：整份留在原地，只能靠账本在显示层替换
    if has_settings:
        return "ledger"

    # redscript：只有本地化包能进包。其余 .reds 的文案走账本，
    # 文件本身一个字节都不覆盖 —— 见 is_localization_text 的说明。
    if ext == ".reds":
        return "pack" if reds_lang_ok else "ledger"

    # 语言包 / 本地化 / tweakdb：必须靠覆盖文件
    if any(h in low for h in _LOCAL_HINTS):
        return "pack"
    if ext in (".yaml", ".yml"):
        return "pack"

    # 其余源码：文案走包
    if typ:
        return "pack"
    return "skip"


def classify(mod_root, rows):
    """把一组行按通道分好。

    返回 {"ledger": [...], "pack": [...], "archive": [...], "skip": n,
          "settings_files": [...]}
    每条是原样的行对象。
    """
    out = {"ledger": [], "pack": [], "archive": [], "skip": 0,
           "settings_files": []}
    cache = {}

    # 先把 .reds 按目录归堆：整目录都是 Codeware 本地化包的才允许投递。
    # 单个文件判不够 —— 语言包目录里还有 LocalizationProvider 之类没被翻的
    # 兄弟文件，模组启用时由模组自己提供，我们只替换有译文的那些。
    reds_dirs = {}
    for r in rows or []:
        rel = str(r.get("file") or "")
        if rel.lower().endswith(".reds"):
            d = os.path.dirname(rel).replace("\\", "/")
            reds_dirs.setdefault(d, []).append(rel)
    lang_dirs = {d for d, rels in reds_dirs.items()
                 if rels and all(is_localization_package(os.path.join(mod_root, x))
                                 for x in rels)}

    for r in rows or []:
        rel = str(r.get("file") or "")
        if not rel:
            out["skip"] += 1
            continue
        # 归档行必须走归档通道：它们的 file 是**归档内部**的资源路径
        # （base\yv\localization\xxx.json），模组目录里没有这个文件。
        # 2026-11-20 之前这里只看路径串，命中 _LOCAL_HINTS 就判成 pack，
        # 于是 build_pack 去磁盘上找 → 「源文件不存在」→ 一整批归档行全丢
        # （309 个模组那轮丢了 87 个模组 4,387 条）。route() 也永远返回不到
        # "archive"（行不会带 .archive 扩展名），cls["archive"] 一直是空的。
        if str(r.get("kind") or "") == "resource":
            out["archive"].append(r)
            continue
        full = os.path.join(mod_root, rel)
        if rel not in cache:
            cache[rel] = is_settings_file(full)
            if cache[rel]:
                out["settings_files"].append(rel)
        rel_dir = os.path.dirname(rel).replace("\\", "/")
        ch = route(rel, r.get("type") or "", cache[rel], rel_dir in lang_dirs)
        if ch in ("ledger", "pack", "archive"):
            out[ch].append(r)
        else:
            out["skip"] += 1
    return out


# ---------------------------------------------------------------- 打包
def _group_by_file(rows):
    """{相对路径: [(行号, 原文, 译文, ctx)]}

    ctx 是写回时的稳定标识：数据表 JSON 是 JSON Pointer，语言包是条目名。
    2026-11-21 之前这里把 ctx 丢了，build_pack 只能按行号写回，于是数据表
    JSON 落到 apply_many_ex 被整份拒绝（mod_text_engine.py:3774）——
    BountyContracts 3 个 json 数据表 7843 条就是这么全丢的。按行号写 json
    本来也不成立：`"键": "值"` 在 reds 词法下是两个独立字面量。
    """
    files = {}
    for r in rows or []:
        rel = str(r.get("file") or "")
        orig = str(r.get("orig") or "")
        trans = str(r.get("trans") or "")
        if not rel or not orig or not trans or trans == orig:
            continue
        files.setdefault(rel, []).append(
            (r.get("line"), orig, trans, str(r.get("ctx") or "")))
    return files


def build_pack(mod_root, rows, extra_files=None, log=None):
    """把 rows 里该进包的文件做成一份待合并清单。

    extra_files: 归档层产物 {zip内路径: 磁盘路径}，由调用方（WolvenKit 组包）给出。

    返回 dict：
        ok / fail / errors / entries
        files: {zip内相对路径: 临时目录里的实际路径}
        temp:  临时目录（调用方用完要删）
    """
    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    import mod_text_engine as MTE

    temp = tempfile.mkdtemp(prefix="zhpack-")
    files = {}
    ok = fail = 0
    errors = []

    for rel, pairs in _group_by_file(rows).items():
        src = os.path.join(mod_root, rel)
        if not os.path.isfile(src):
            errors.append("%s: 源文件不存在" % rel)
            fail += len(pairs)
            continue
        # 兜底闸门：逻辑 / UI 脚本绝不出现在包里（route 那边已经拦过一道）。
        # 漏一个进去就可能在游戏里留下「半个模块」，代价是游戏启动不了。
        if rel.lower().endswith(".reds") and not is_localization_package(src):
            errors.append("%s: 跳过（redscript 逻辑文件不投递）" % rel)
            _log("跳过 %s：不是本地化包，文案只进账本" % rel)
            fail += len(pairs)
            continue
        dst = os.path.join(temp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.copy2(src, dst)
        except Exception as ex:
            errors.append("%s: 复制失败 %s" % (rel, ex))
            fail += len(pairs)
            continue
        is_json = rel.lower().endswith(".json")
        if is_json and not all(str(p[3] or "") for p in pairs):
            # 数据表没有 JSON Pointer 就没法按值定位，按行号写只会在
            # 「代码骨架不一致」那道守卫上整批被拒，白跑一趟。
            errors.append("%s: 数据表缺少 JSON Pointer，已拒绝写回" % rel)
            fail += len(pairs)
            continue
        try:
            if is_json:
                # 数据表 JSON：按 JSON Pointer 改值再整份序列化。apply_many_ex
                # 对 .json 是硬拒绝（mod_text_engine.py:3774），走它必然 0 写入。
                n, fails = MTE.apply_json_ex(
                    dst, [(p[3], p[1], p[2]) for p in pairs])
            else:
                n, fails = MTE.apply_many_ex(
                    dst, [(p[0], p[1], p[2]) for p in pairs])
        except Exception as ex:
            errors.append("%s: 写回异常 %s" % (rel, ex))
            fail += len(pairs)
            continue
        ok += int(n or 0)
        fail += len(fails or [])
        for f in (fails or [])[:3]:
            errors.append("%s:%s %s" % (rel, f[1], f[4]))
        files[rel] = dst
        _log("打包 %s: 写入 %d 条%s"
             % (rel, n, ("，失败 %d" % len(fails)) if fails else ""))

    for name, path in (extra_files or {}).items():
        if os.path.isfile(path):
            files[name] = path

    return {"ok": ok, "fail": fail, "errors": errors[:20],
            "entries": ok, "files": files, "temp": temp}


def merge_into_zip(zip_path, new_files, drop_names=None, log=None):
    """把 new_files 合并进现有 zip：旧条目保留，同名替换，drop_names 删除。

    new_files: {zip内路径: 磁盘路径}
    drop_names: 要删掉的 zip 内路径集合
    """
    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    drop = set(drop_names or ())
    new_files = dict(new_files or {})

    os.makedirs(os.path.dirname(zip_path) or ".", exist_ok=True)
    tmp = zip_path + ".new"

    old_infos = []
    if os.path.isfile(zip_path):
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                old_infos = z.infolist()
        except Exception as ex:
            _log("旧 zip 读不动（将重建）：%s" % ex)
            old_infos = []

    replaced = 0
    kept = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        if old_infos:
            with zipfile.ZipFile(zip_path, "r") as old:
                for info in old_infos:
                    name = info.filename
                    if name in new_files or name in drop:
                        continue
                    try:
                        out.writestr(info, old.read(name))
                        kept += 1
                    except Exception as ex:
                        _log("保留 %s 失败：%s" % (name, ex))
        for name, path in new_files.items():
            try:
                out.write(path, name)
                replaced += 1
            except Exception as ex:
                _log("写入 %s 失败：%s" % (name, ex))

    try:
        os.replace(tmp, zip_path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

    _log("合并进 %s：新增/替换 %d，保留 %d，删除 %d"
         % (os.path.basename(zip_path), replaced, kept, len(drop)))
    return {"replaced": replaced, "kept": kept, "dropped": len(drop)}


def pack_zip_name(zip_path, rel):
    """把模组内的相对路径映射成 zip 内的路径。

    目前就是原样（zip 根目录直接是 r6/… 结构，Vortex 解压后直接识别）。
    """
    return rel.replace("\\", "/")


def cleanup(temp):
    """删临时目录（调用方在合并完成后调用）。"""
    if temp and os.path.isdir(temp):
        shutil.rmtree(temp, ignore_errors=True)


def update_state(st, mod_dir, pack_ok, rels, zip_path):
    """把本次打包结果写进状态库。

    记的是**原模组**文件的 sha1（不是临时副本的）：mod 更新后拿它比对，
    才算得出「这份文件作者改过没有」。
    """
    rec = mod_record(st, mod_dir)
    rec["packed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    rec["zip"] = zip_path
    rec["entries"] = int(pack_ok or 0)
    rec["files"] = {
        rel: {"sha1": sha1_file(os.path.join(mod_dir, rel))}
        for rel in (rels or {})
    }
    return rec
