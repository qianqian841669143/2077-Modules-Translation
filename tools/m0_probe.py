# -*- coding: utf-8 -*-
"""M0 冒烟探针 —— 验证 ArchiveXL 能否注入 onscreens 与 subtitles。

这是整个改造方案里唯一没有实证支撑的假设。先跑通它，再谈引擎。

用法:
    python tools\\m0_probe.py --game "E:\\SteamLibrary\\steamapps\\common\\Cyberpunk 2077" \
                              --wkit "C:\\Tools\\WolvenKit\\WolvenKit.CLI.exe"

    python tools\\m0_probe.py --clean      # 移除测试文件

做的事:
    1. 检查前置条件（游戏目录 / ArchiveXL / WolvenKit / 语言包）
    2. unbundle 官方 zh-cn 文本包
    3. 取 onscreens.json，改一条主菜单文本，serialize -> 改 -> deserialize -> pack
    4. 取一条字幕资源，同样处理
    5. 生成 m0test.archive + m0test.archive.xl，装进 archive\\pc\\mod
    6. 输出验收清单
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_GAME = r"E:\SteamLibrary\steamapps\common\Cyberpunk 2077"
MARK = "【M0】"

# 主菜单/暂停菜单上一定能看到的条目（secondaryKey 有主键段连号 23264-23273，属同一组 UI 标签）
TARGET_KEYS = (
    "UI-Labels-Resume",
    "UI-Labels-Settings",
    "UI-Labels-Credits",
    "UI-Labels-CloseGame",
    "UI-ScriptExports-NewGame0",
)


def log(msg: str = "") -> None:
    print(msg, flush=True)


def run(cmd: list[str], cwd: Path | None = None) -> str:
    log(">> " + " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd))
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.stdout:
        for line in proc.stdout.splitlines():
            if line.strip():
                log("   " + line.rstrip())
    if proc.returncode != 0:
        raise RuntimeError(f"命令失败 (exit {proc.returncode}): {cmd[0]}")
    return proc.stdout


def is_cr2w(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"CR2W"
    except OSError:
        return False


def find_wkit(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    for env in ("WOLVENKIT_CLI", "WOLVENKIT"):
        if os.environ.get(env):
            candidates.append(Path(os.environ[env]))
    for name in ("WolvenKit.CLI.exe", "wolvenkit.cli.exe", "WolvenKit.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for base in (
        r"C:\Program Files\WolvenKit",
        r"C:\Program Files (x86)\WolvenKit",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\WolvenKit"),
        os.path.expandvars(r"%USERPROFILE%\WolvenKit"),
    ):
        p = Path(base)
        if p.exists():
            candidates.extend(p.rglob("WolvenKit.CLI.exe"))
            candidates.extend(p.rglob("WolvenKit.exe"))
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        "找不到 WolvenKit。请用 --wkit 指定 WolvenKit.CLI.exe 的完整路径。"
    )


def preflight(game: Path) -> list[str]:
    problems: list[str] = []
    if not (game / "bin" / "x64" / "Cyberpunk2077.exe").exists():
        problems.append(f"游戏目录不对：{game}")
    if not (game / "archive" / "pc" / "mod").exists():
        problems.append("缺少 archive\\pc\\mod 目录")
    axl = list((game / "red4ext" / "plugins").glob("ArchiveXL*")) if (game / "red4ext" / "plugins").exists() else []
    if not axl:
        problems.append("未检测到 ArchiveXL（red4ext\\plugins\\ArchiveXL）——没有它 .xl 不会被加载")
    if not list((game / "archive" / "pc" / "content").glob("lang_*_text.archive")):
        problems.append("未找到 lang_*_text.archive")
    return problems


def unbundle(wkit: Path, archive: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    try:
        run([wkit, "unbundle", str(archive), "-o", str(out), "-v", "Minimal"])
    except RuntimeError:
        log("   unbundle 失败，改用 extract 重试……")
        run([wkit, "extract", str(archive), "-o", str(out)])


def serialize(wkit: Path, src: Path, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    run([wkit, "convert", "serialize", str(src), "-o", str(out)])
    hits = sorted(out.rglob("*.json.json"))
    if not hits:
        hits = [p for p in out.rglob("*.json") if p.is_file()]
    if not hits:
        raise RuntimeError(f"serialize 未产出 JSON：{src}")
    return hits[0]


def deserialize(wkit: Path, src: Path, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    run([wkit, "convert", "deserialize", str(src), "-o", str(out)])
    hits = [p for p in out.rglob("*") if p.is_file() and is_cr2w(p)]
    if not hits:
        raise RuntimeError(f"deserialize 未产出 CR2W：{src}")
    return hits[0]


def entries_of(doc: dict) -> list[dict]:
    return doc["Data"]["RootChunk"]["root"]["Data"]["entries"]


def pick_onscreen_targets(entries: list[dict]) -> list[dict]:
    """按 secondaryKey 精确挑主菜单条目；挑不到就退化成模糊匹配。"""
    by_key = {}
    for e in entries:
        if isinstance(e, dict):
            by_key.setdefault(str(e.get("secondaryKey", "")), e)
    picked = [by_key[k] for k in TARGET_KEYS if k in by_key]
    if picked:
        return picked
    log("   !! 没匹配到已知主菜单 key，退化为模糊匹配")
    preferred = ("新游戏", "载入", "继续", "设置", "退出")
    for want in preferred:
        for e in entries:
            if isinstance(e, dict) and want in str(e.get("femaleVariant", "")):
                return [e]
    for e in entries:
        if isinstance(e, dict) and str(e.get("femaleVariant", "")).strip():
            return [e]
    raise RuntimeError("onscreens 里没找到可替换的条目")


def pack(wkit: Path, stage: Path, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    run([wkit, "pack", str(stage), "-o", str(out)])
    hits = sorted(out.rglob("*.archive"))
    if not hits:
        raise RuntimeError("pack 未产出 .archive")
    for h in hits:
        if h.stem.lower() == stage.name.lower():
            return h
    return hits[0]


def pick_text_archive(game: Path, prefer: str = "zh-cn") -> Path:
    """优先取简体中文文本包；没有就退回最大的那个。"""
    packs = sorted((game / "archive" / "pc" / "content").glob("lang_*_text.archive"))
    for p in packs:
        if prefer in p.name.lower():
            return p
    return max(packs, key=lambda p: p.stat().st_size)


def build_onscreens(wkit: Path, game: Path, work: Path) -> tuple[Path, list[str]]:
    log("\n=== [1/2] onscreens 注入 ===")
    raw = work / "raw"
    if raw.exists() and any(raw.rglob("onscreens.json")):
        log(f"复用已有解包结果: {raw}")
    else:
        src_archive = pick_text_archive(game)
        log(f"源包: {src_archive.name}")
        unbundle(wkit, src_archive, raw)

    onscreen_files = [p for p in raw.rglob("onscreens.json") if is_cr2w(p)]
    if not onscreen_files:
        raise RuntimeError("解包结果里没有 onscreens.json")
    src = onscreen_files[0]
    log(f"onscreens: {src.relative_to(raw)}")

    ser = serialize(wkit, src, work / "ser_onscreens")
    doc = json.loads(ser.read_text(encoding="utf-8"))
    targets = pick_onscreen_targets(entries_of(doc))
    changed: list[str] = []
    for entry in targets:
        old = str(entry.get("femaleVariant", ""))
        entry["femaleVariant"] = MARK + old
        changed.append(old)
        log(f'标记: {entry.get("secondaryKey")!r:34} "{old}" -> "{entry["femaleVariant"]}"')

    edited = work / "edit_onscreens" / "onscreens.json.json"
    edited.parent.mkdir(parents=True, exist_ok=True)
    edited.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    cr2w = deserialize(wkit, edited, work / "de_onscreens")
    stage = work / "stage" / "m0test" / "localization" / "zh-cn"
    stage.mkdir(parents=True, exist_ok=True)
    target = stage / "onscreens.json"
    shutil.copy2(cr2w, target)
    log(f"CR2W 就位: {target}")
    return target, changed


SUB_MAP_REL = "localization\\zh-cn\\subtitles\\m0sub_map.json"
SUB_TXT_REL = "localization\\zh-cn\\subtitles\\m0sub_text.json"


def build_subtitles(wkit: Path, work: Path) -> str | None:
    """字幕注入 = 造一份 PersistenceSubtitleMap，指向我们自己的 Entries 文件。

    实测结论（2026-10-30）：
      - subtitles 节点确实存在，且语言键生效；
      - 但资源类型必须是 localizationPersistenceSubtitleMap（字幕文件注册表），
        直接塞 PersistenceSubtitleEntries 会报 Resource failed to load；
      - ArchiveXL 把 map 条目 PushBack 进语言字幕表，由游戏据此再去加载 Entries 文件。
    """
    log("\n=== [2/2] subtitles 注入 ===")
    raw = work / "raw"
    maps = [p for p in raw.rglob("subtitles.json") if "subtitles" in p.parts and is_cr2w(p)]
    ents = [
        p for p in raw.rglob("*.json")
        if "subtitles" in p.parts and is_cr2w(p) and p.name != "subtitles.json"
    ]
    if not maps or not ents:
        log("!! 解包结果里没有字幕资源（map 或 entries），跳过")
        return None
    map_src = maps[0]
    ent_src = min(ents, key=lambda p: p.stat().st_size)
    log(f"字幕注册表: {map_src.relative_to(raw)}")
    log(f"字幕文本样本: {ent_src.relative_to(raw)}  ({ent_src.stat().st_size} bytes)")

    # --- 1. Entries 文件：挑一条文案加标记
    ser_ent = serialize(wkit, ent_src, work / "ser_subs")
    doc_ent = json.loads(ser_ent.read_text(encoding="utf-8"))
    e_list = entries_of(doc_ent)
    if not e_list:
        log("!! 字幕样本里没有条目，跳过")
        return None
    target_entry = None
    for e in e_list:
        if isinstance(e, dict) and str(e.get("femaleVariant", "")).strip():
            target_entry = e
            break
    if target_entry is None:
        log("!! 字幕样本里没有可替换文案，跳过")
        return None
    old = str(target_entry["femaleVariant"])
    target_entry["femaleVariant"] = MARK + old
    sid = str(target_entry.get("stringId", ""))
    log(f'字幕标记: stringId={sid}  "{old}" -> "{target_entry["femaleVariant"]}"')

    p_ent = work / "edit_subs" / "m0sub_text.json.json"
    p_ent.parent.mkdir(parents=True, exist_ok=True)
    p_ent.write_text(json.dumps(doc_ent, ensure_ascii=False, indent=2), encoding="utf-8")
    cr2w_ent = deserialize(wkit, p_ent, work / "de_subs")

    # --- 2. Map 文件：只留一条，指向我们的 Entries
    ser_map = serialize(wkit, map_src, work / "ser_submap")
    doc_map = json.loads(ser_map.read_text(encoding="utf-8"))
    m_list = entries_of(doc_map)
    if not m_list:
        log("!! 字幕注册表是空的，跳过")
        return None
    template = json.loads(json.dumps(m_list[0]))  # 深拷贝，保留 $type/storage 元数据
    # 组名沿用样本里出现过的；字体组不对会导致 UI 不显示
    group = None
    for m in m_list:
        g = m.get("subtitleGroup")
        if isinstance(g, dict) and g.get("$value") == "quest":
            group = "quest"
            break
    if group is None:
        g = template.get("subtitleGroup")
        group = g.get("$value") if isinstance(g, dict) else "quest"
    # subtitleFile 是资源引用对象：路径在 DepotPath.$value，不是 subtitleFile.$value。
    # 写错层级会让 WolvenKit 报 "JSON value could not be converted to RedFileDto"。
    ref = template.get("subtitleFile") or {}
    if "DepotPath" not in ref or not isinstance(ref.get("DepotPath"), dict):
        st = json.dumps(ref, ensure_ascii=False)
        raise RuntimeError(f"subtitleFile 结构与预期不符，无法安全改写: {st[:200]}")
    ref["DepotPath"]["$value"] = SUB_TXT_REL
    ref.setdefault("Flags", "Soft")
    template["subtitleGroup"]["$value"] = group
    doc_map["Data"]["RootChunk"]["root"]["Data"]["entries"] = [template]
    log(f"字幕注册表: {SUB_MAP_REL} -> {SUB_TXT_REL} (group={group})")

    p_map = work / "edit_subs" / "m0sub_map.json.json"
    p_map.write_text(json.dumps(doc_map, ensure_ascii=False, indent=2), encoding="utf-8")
    cr2w_map = deserialize(wkit, p_map, work / "de_submap")

    stage = work / "stage" / "m0test" / "localization" / "zh-cn" / "subtitles"
    stage.mkdir(parents=True, exist_ok=True)
    for src, name in ((cr2w_ent, "m0sub_text.json"), (cr2w_map, "m0sub_map.json")):
        shutil.copy2(src, stage / name)
        log(f"CR2W 就位: {stage / name}")
    return SUB_MAP_REL


def write_xl(out_dir: Path, subs_rel: str | None) -> Path:
    # 路径相对归档根目录：pack 打包的是目录内容，不含目录名本身。
    # .xl 必须放在打包目录之外，否则会被一起塞进 archive（虽被跳过，但不干净）。
    lines = ["localization:", "  onscreens:", "    zh-cn:", "      - localization\\zh-cn\\onscreens.json"]
    if subs_rel:
        lines += ["  subtitles:", "    zh-cn:", f"      - {subs_rel}"]
    out_dir.mkdir(parents=True, exist_ok=True)
    xl = out_dir / "m0test.archive.xl"
    xl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return xl


def install(game: Path, archive: Path, xl: Path) -> None:
    dst = game / "archive" / "pc" / "mod"
    for src in (archive, xl):
        target = dst / src.name
        if target.exists():
            backup = target.with_suffix(target.suffix + ".bak-m0")
            if not backup.exists():
                shutil.copy2(target, backup)
        shutil.copy2(src, target)
        log(f"已安装: {target}")


def clean(game: Path) -> None:
    dst = game / "archive" / "pc" / "mod"
    for name in ("m0test.archive", "m0test.archive.xl"):
        p = dst / name
        if p.exists():
            p.unlink()
            log(f"已删除: {p}")
    log("清理完成，请重新启动游戏确认恢复原状。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", default=DEFAULT_GAME)
    ap.add_argument("--wkit", default=None)
    ap.add_argument("--work", default=None)
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()

    game = Path(args.game)
    if args.clean:
        clean(game)
        return 0

    work = Path(args.work) if args.work else Path(__file__).resolve().parent.parent / ".m0"
    work.mkdir(parents=True, exist_ok=True)

    problems = preflight(game)
    if problems:
        log("前置检查未通过:")
        for p in problems:
            log("  - " + p)
        return 2

    wkit = find_wkit(args.wkit)
    log(f"WolvenKit: {wkit}")
    log(f"工作目录 : {work}")

    # 打包目录里只允许出现本次产物：必须在生成之前清
    stage_root = work / "stage" / "m0test"
    if stage_root.exists():
        shutil.rmtree(stage_root)
    for d in ("packed", "out"):
        p = work / d
        if p.exists():
            shutil.rmtree(p)

    _, marked = build_onscreens(wkit, game, work)
    sub_rel = build_subtitles(wkit, work)

    out_dir = work / "out"
    xl = write_xl(out_dir, sub_rel)

    archive = pack(wkit, stage_root, work / "packed")
    final = out_dir / "m0test.archive"
    shutil.copy2(archive, final)
    log(f"\n产物: {final}")

    install(game, final, xl)

    log("\n" + "=" * 60)
    log("验收清单")
    log("=" * 60)
    log("1) 确认游戏显示语言 = 简体中文（设置 -> 语言）")
    log("2) 启动游戏，主菜单应出现以下标记（任意一条命中即算通过）：")
    for t in marked:
        log(f'   "{MARK}{t}"')
    axl_log = game / "red4ext" / "plugins" / "ArchiveXL"
    log("3) ArchiveXL 日志（不在 red4ext\\logs，在插件目录里）：")
    log(f"   {axl_log}")
    log('   搜 "Localization"：应能看到')
    log('     Initializing texts for "zh-cn" language...')
    log(r'     Merging entries from "localization\zh-cn\onscreens.json"...')
    log(r'   字幕部分应看到 Merging entries from "localization\zh-cn\subtitles\m0sub_map.json"...')
    log('   并以 "All subtitles merged." 收尾（不再出现 failed to load）')
    log("4) 字幕视觉验证需要进到 mq000 秘密房间那一段；日志命中即说明链路成立")
    log("\n清理: python tools\\m0_probe.py --clean")
    log("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
