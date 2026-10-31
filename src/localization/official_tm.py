# -*- coding: utf-8 -*-
"""official_tm.py —— 用官方双语语料建翻译记忆库。

思路：官方语言包按语言各一份，资源结构完全一致，所以可以按**主键**对齐：

    onscreens:  en-us 的 primaryKey  ↔  zh-cn 的 primaryKey   （约 6.2 万对）
    subtitles:  en-us 的 stringId    ↔  zh-cn 的 stringId

对齐得到的是**游戏官方自己的译文**，术语与语气天然一致——这正是机器翻译
最缺的东西。模组里凡是引用/覆盖官方文本的部分（绝大多数装备、UI、任务名）
都能直接命中。

存 SQLite：单文件、可增量、按 src 建索引，几十万条查询也是毫秒级。

两个性能要点：
  1. WolvenKit 的 `convert serialize` 可以指定**目录**，一次进程展开三千个
     字幕文件；逐个起进程会慢两个数量级。
  2. 对齐前按主键去重，同一个 key 只留一条，避免重复行把库撑大。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

from . import archive as archmod
from . import cr2w, wkit

SCHEMA_VERSION = "1"

# 语言包文件名 → 包内语言目录名
LANG_DIR = {
    "ar": "ar-ar", "cs": "cz-cz", "de": "de-de", "en": "en-us",
    "es-es": "es-es", "es-mx": "es-mx", "fr": "fr-fr", "hu": "hu-hu",
    "it": "it-it", "ja": "jp-jp", "ko": "kr-kr", "pl": "pl-pl",
    "pt": "pt-br", "ru": "ru-ru", "th": "th-th", "tr": "tr-tr",
    "ua": "ua-ua", "zh-cn": "zh-cn", "zh-tw": "zh-tw",
}

DEFAULT_SRC = "en-us"
DEFAULT_DST = "zh-cn"

_SPACE_RE = re.compile(r"[ \t\u00a0]+")
# 汉字：目标语言是中文时，译文里必须至少出现一个
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# 官方包里漏翻的占位符，形如 "[en_us]Back"
_PLACEHOLDER_RE = re.compile(r"^\[[a-z]{2}[-_][a-z]{2}\]", re.I)


def _is_usable_pair(src, dst):
    """判断一对语料值不值得入库。

    官方中文包并不干净：实测存在形如 `[en_us]Back` 的漏翻占位，
    也有整段照抄英文的条目。收进来只会让 TM 反向污染译文，
    所以宁缺毋滥。
    """
    if _PLACEHOLDER_RE.match(dst):
        return False
    low = dst.lower()
    if "[en_us]" in low or "[en-us]" in low:
        return False
    # 目标语言是中文，译文里一个汉字都没有 → 基本可以断定没翻
    if not _CJK_RE.search(dst):
        return False
    return True


def default_db_path():
    """与 appconfig.APP_DIR_NAME 保持一致；单独跑时也能用。"""
    try:
        import appconfig
        return os.path.join(appconfig.config_dir(), "official_tm.sqlite")
    except Exception:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "2077ModTranslator", "official_tm.sqlite")


def norm_text(text):
    """归一化后再比对。

    官方文本里有大量：首尾空白、连续空格、\\r\\n 混用、零宽空格。
    严格相等会把这么多能命中的对子全部漏掉。
    """
    s = str(text or "")
    if not s:
        return ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _SPACE_RE.sub(" ", s)
    return s.strip()


# ------------------------------------------------------------------ 抽取


def _lang_of_pack(path):
    """lang_zh-cn_text.archive → zh-cn；lang_en_text.archive → en-us。"""
    name = Path(path).name
    m = re.match(r"lang_(.+?)_(text|voice)\.archive$", name, re.I)
    if not m:
        return ""
    code = m.group(1).lower()
    return LANG_DIR.get(code, code)


class CorpusRow(object):
    __slots__ = ("kind", "key", "src", "dst", "src_lang", "dst_lang")

    def __init__(self, kind, key, src, dst, src_lang, dst_lang):
        self.kind = kind
        self.key = key
        self.src = src
        self.dst = dst
        self.src_lang = src_lang
        self.dst_lang = dst_lang


def extract_language(cli, game, lang_code, workdir, log=None):
    """解包并展开一个语言的本地化资源，返回 {"onscreen": {pk: text}, "subtitle": {sid: text}}。

    lang_code 是包内目录名（en-us / zh-cn）。

    一个语言可能有两个包：基础游戏和往日之影。两边的资源目录不同
    （base/localization 与 ep1/localization），必须合并，否则狗镇那一整块
    字幕（22749 条）会被漏掉 —— P5 实测踩过。
    """
    gi = archmod.GameInfo(game)
    packs = [p for p in gi.text_archives()
             if _lang_of_pack(p) == lang_code]
    if not packs:
        raise RuntimeError("找不到 %s 的语言包" % lang_code)

    out = {"onscreen": {}, "subtitle": {}}

    for pack in packs:
        # 每个包单独一个工作目录，避免 base/ep1 的 raw 互相覆盖。
        # 注意两个包的 stem 同名（lang_zh-cn_text），必须带上父目录才能区分，
        # 否则第二个包会被 --any(raw) 判断跳过（P5 踩过）。
        tag = "%s_%s" % (pack.parent.name, pack.stem)
        root = Path(workdir) / lang_code / tag
        raw = root / "raw"
        ser = root / "json"

        if log:
            log("解包 %s" % pack.name)
        if not raw.is_dir() or not any(raw.iterdir()):
            raw.mkdir(parents=True, exist_ok=True)
            wkit.unbundle(cli, pack, raw)

        # 语言目录可能落在 base/localization 或 ep1/localization
        for lang_dir in sorted(raw.glob("*/localization/" + lang_code)):
            for name in ("onscreens", "subtitles"):
                src_dir = lang_dir / name
                if not src_dir.is_dir():
                    continue
                rel = src_dir.relative_to(raw)
                out_dir = ser / rel
                out_dir.mkdir(parents=True, exist_ok=True)
                if log:
                    log("展开 %s" % rel.as_posix())
                # 一次进程展开整个目录，别逐文件调
                try:
                    wkit.run(cli, ["convert", "serialize", str(src_dir), "-o", str(out_dir),
                                   "-w", "*.json"], timeout=3600)
                except wkit.WkitError as ex:
                    if log:
                        log("  展开失败（跳过该目录）: %s" % ex)

        for p in ser.rglob("*.json.json"):
            try:
                doc = cr2w.load_doc(p)
            except cr2w.Cr2wError:
                continue
            kind = cr2w.kind_of(doc)
            if kind == cr2w.KIND_ONSCREEN:
                bucket = out["onscreen"]
            elif kind == cr2w.KIND_SUB_ENTRIES:
                bucket = out["subtitle"]
            else:
                continue
            for e in cr2w.entries(doc):
                if not cr2w.is_translatable(e):
                    continue
                _idk, eid = cr2w.entry_id(e)
                if not eid:
                    continue
                bucket[eid] = cr2w.get_text(e)

    return out


def align(src_data, dst_data, src_lang, dst_lang):
    """按主键对齐两个语言的条目，产出 (可用行, 丢弃数)。"""
    rows = []
    dropped = 0
    for kind in ("onscreen", "subtitle"):
        a = src_data.get(kind) or {}
        b = dst_data.get(kind) or {}
        for key, src in a.items():
            dst = b.get(key)
            if not dst:
                dropped += 1
                continue
            ns, nd = norm_text(src), norm_text(dst)
            if not ns or not nd or ns == nd:
                dropped += 1          # 空文本，或英中完全相同（纯数字/型号）
                continue
            if not _is_usable_pair(ns, nd):
                dropped += 1          # 官方漏翻的占位/英文照抄
                continue
            rows.append(CorpusRow(kind, str(key), ns, nd, src_lang, dst_lang))
    return rows, dropped


# ------------------------------------------------------------------ 建库


def _open_db(db_path, fresh=False):
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if fresh and p.exists():
        p.unlink()
    con = sqlite3.connect(str(p))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA temp_store=MEMORY")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS pairs (
            id       INTEGER PRIMARY KEY,
            kind     TEXT NOT NULL,
            key      TEXT,
            src      TEXT NOT NULL,
            dst      TEXT NOT NULL,
            src_lang TEXT,
            dst_lang TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_pairs_src ON pairs(src);
        CREATE INDEX IF NOT EXISTS ix_pairs_key ON pairs(key);
    """)
    return con


def build(game=None, cli=None, workdir=None, db_path=None,
          src_lang=DEFAULT_SRC, dst_lang=DEFAULT_DST, log=None, progress=None):
    """构建官方双语库，返回统计信息。"""
    def say(m):
        if log:
            log(m)

    def step(done, total, label):
        if progress:
            progress(done, total, label)

    game = game or archmod.find_game()
    if not game:
        raise RuntimeError("找不到游戏目录")
    cli = cli or wkit.find_cli()
    if not cli:
        raise RuntimeError("找不到 WolvenKit.CLI.exe")
    db_path = db_path or default_db_path()
    workdir = workdir or os.path.join(os.path.dirname(db_path), "corpus")

    t0 = time.time()
    step(1, 4, "展开 %s" % src_lang)
    say("=== 读取 %s ===" % src_lang)
    src_data = extract_language(cli, game, src_lang, workdir, log=log)
    say("  onscreens %d 条 / 字幕 %d 条"
        % (len(src_data["onscreen"]), len(src_data["subtitle"])))

    step(2, 4, "展开 %s" % dst_lang)
    say("=== 读取 %s ===" % dst_lang)
    dst_data = extract_language(cli, game, dst_lang, workdir, log=log)
    say("  onscreens %d 条 / 字幕 %d 条"
        % (len(dst_data["onscreen"]), len(dst_data["subtitle"])))

    step(3, 4, "对齐")
    rows, dropped = align(src_data, dst_data, src_lang, dst_lang)
    say("=== 对齐 %d 对（丢弃 %d 条：缺失/相同/官方漏翻）===" % (len(rows), dropped))

    step(3, 4, "写入数据库")
    con = _open_db(db_path, fresh=True)
    try:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        con.executemany(
            "INSERT INTO pairs (kind, key, src, dst, src_lang, dst_lang) VALUES (?,?,?,?,?,?)",
            [(r.kind, r.key, r.src, r.dst, r.src_lang, r.dst_lang) for r in rows])
        counts = dict(con.execute(
            "SELECT kind, COUNT(*) FROM pairs GROUP BY kind").fetchall())
        meta = {
            "schema": SCHEMA_VERSION,
            "built_at": now,
            "src_lang": src_lang,
            "dst_lang": dst_lang,
            "pairs": str(len(rows)),
            "dropped": str(dropped),
            "onscreen": str(counts.get("onscreen", 0)),
            "subtitle": str(counts.get("subtitle", 0)),
            "game": str(game),
        }
        con.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                        list(meta.items()))
        con.commit()
        con.execute("ANALYZE")
        con.commit()
    finally:
        con.close()

    step(4, 4, "完成")
    size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    meta["db"] = db_path
    meta["bytes"] = str(size)
    meta["seconds"] = "%.1f" % (time.time() - t0)
    say("=== 完成：%s（%.1f MB，用时 %s 秒）"
        % (db_path, size / 1048576.0, meta["seconds"]))
    return meta


# ------------------------------------------------------------------ 查询


class OfficialTM(object):
    """只读查询。库不存在时静默降级（exists() 为 False）。"""

    def __init__(self, db_path=None):
        self.db_path = db_path or default_db_path()
        self._con = None

    # -- 生命周期 ---------------------------------------------------------

    @property
    def con(self):
        if self._con is None:
            if not os.path.exists(self.db_path):
                return None
            self._con = sqlite3.connect("file:%s?mode=ro" % str(self.db_path).replace("\\", "/"),
                                        uri=True)
        return self._con

    def exists(self):
        return os.path.exists(self.db_path) and os.path.getsize(self.db_path) > 0

    def close(self):
        if self._con is not None:
            self._con.close()
            self._con = None

    # -- 元信息 -----------------------------------------------------------

    def stats(self):
        if not self.exists():
            return {"exists": False, "db": self.db_path}
        con = self.con
        out = {"exists": True, "db": self.db_path,
               "bytes": os.path.getsize(self.db_path)}
        try:
            for k, v in con.execute("SELECT key, value FROM meta"):
                out[k] = v
            total = con.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
            out["total"] = total
        except sqlite3.Error:
            out["exists"] = False
        return out

    # -- 查询 -------------------------------------------------------------

    def lookup(self, text):
        """单条查询，返回最常用的译文；没有命中返回空串。"""
        key = norm_text(text)
        if not key:
            return ""
        con = self.con
        if con is None:
            return ""
        try:
            row = con.execute(
                "SELECT dst, COUNT(*) c FROM pairs WHERE src = ? "
                "GROUP BY dst ORDER BY c DESC, LENGTH(dst) ASC LIMIT 1",
                (key,)).fetchone()
        except sqlite3.Error:
            return ""
        return row[0] if row else ""

    def lookup_many(self, texts, chunk=400, detailed=False):
        """批量查询，返回 {归一化原文: 译文}。

        分块用 IN 查询：几十万条一次性拼进去会撞 SQLite 的参数上限。

        detailed=True 时值改成 {"dst", "total", "distinct"}：
        基线层要靠 total（这条在官方语料里出现几次）和 distinct（有几种译法）
        判断这条命中算不算「够稳」。
        """
        con = self.con
        if con is None:
            return {}
        wanted = {}
        for t in texts:
            k = norm_text(t)
            if k:
                wanted[k] = 1
        keys = list(wanted)
        if not keys:
            return {}

        out = {}
        for i in range(0, len(keys), chunk):
            part = keys[i:i + chunk]
            ph = ",".join("?" * len(part))
            sql = ("SELECT src, dst, COUNT(*) c FROM pairs WHERE src IN (%s) "
                   "GROUP BY src, dst ORDER BY src, c DESC, LENGTH(dst) ASC" % ph)
            try:
                cur = con.execute(sql, part)
            except sqlite3.Error:
                continue
            agg = {}
            for src, dst, c in cur:
                cur_agg = agg.get(src)
                if cur_agg is None:
                    agg[src] = {"dst": dst, "total": c, "distinct": 1}
                else:
                    cur_agg["total"] += c
                    cur_agg["distinct"] += 1
            if detailed:
                out.update(agg)
            else:
                out.update({k: v["dst"] for k, v in agg.items()})
        return out

    def fill_rows(self, rows, key="orig", field="trans", only_empty=True):
        """把命中项写进行列表，返回填充条数。"""
        if not rows:
            return 0
        need = [r.get(key, "") for r in rows
                if not (only_empty and r.get(field))]
        hits = self.lookup_many(need)
        if not hits:
            return 0
        n = 0
        for r in rows:
            if only_empty and r.get(field):
                continue
            t = hits.get(norm_text(r.get(key, "")))
            if t:
                r[field] = t
                n += 1
        return n


# ------------------------------------------------------------------ 术语提示


# 这些词当术语没有意义，反而会污染提示词
_STOPWORDS = frozenset("""
the and you your with from this that have will not for are but all can one
his her its out get got was were been has had who what when where which how
""".split())

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\u2019-]*")


def _is_term_like(src):
    """判断一条官方文本像不像「术语」。

    语料里既有 "Street Cred" 这种真术语，也有 "failed" / "full" / "new"
    这种从整句里切出来的普通词——后者喂给机翻只会误导，必须挡掉。
    规则：多词短语一律收（足够具体）；单词必须首字母大写（游戏里做标签用）。
    """
    words = src.split()
    if not words:
        return False
    if len(words) > 1:
        return True
    w = words[0]
    return bool(w[:1].isupper())


def _dedupe_subterms(items):
    """去掉被更长术语完整包含的短语：有 "street cred" 就不要 "street"。"""
    out = []
    for src, dst in items:
        padded = " %s " % src
        covered = False
        for other, _d in items:
            if other != src and padded in (" %s " % other):
                covered = True
                break
        if not covered:
            out.append((src, dst))
    return out


class TermHints(object):
    """从官方语料里挖出「英文术语 → 官方中文」，用于机翻提示词。

    为什么需要它：整句命中率只有约 10%（模组文案大多是原创），但模组里
    大量出现官方名词（Inventory / Cyberware / Quickhack / Street Cred）。
    把这些词的官方译法塞进提示词，能让整批译文在术语上保持一致——
    这是机器翻译最容易做错、而用户最容易挑错的地方。
    """

    def __init__(self, db_path=None, min_len=3, max_len=28):
        self.db_path = db_path or default_db_path()
        self.min_len = min_len
        self.max_len = max_len
        self._table = None

    def load(self):
        """把短条目读进内存；重复做没有意义，所以缓存。返回术语数。

        筛选靠「译文分歧度」：同一个英文词如果在官方语料里对应了很多种不同
        中文，说明它高度依赖上下文（如 Full → 完整/满/满了），当术语会误导，
        直接排除；只有译文稳定、且出现够多的词才算术语。
        """
        if self._table is not None:
            return len(self._table)
        self._table = {}
        tm = OfficialTM(self.db_path)
        con = tm.con
        if con is None:
            return 0
        try:
            # 只从 onscreen 里挖术语：那是 UI/物品/技能名，属于「规范译名」；
            # 字幕是口语化对白，同一个词会有各种意译（Body → 体格/尸体），
            # 混进来只会把术语表搞脏。
            cur = con.execute(
                "SELECT src, dst, COUNT(*) c FROM pairs "
                "WHERE kind = 'onscreen' "
                "AND LENGTH(src) BETWEEN ? AND ? AND src NOT LIKE '%' || char(10) || '%' "
                "GROUP BY src, dst ORDER BY src, c DESC",
                (self.min_len, self.max_len))
            buckets = {}
            for src, dst, c in cur:
                buckets.setdefault(src, []).append((dst, c))
            for src, cands in buckets.items():
                key = src.lower().strip()
                if not key or key in _STOPWORDS:
                    continue
                if not _is_term_like(src):
                    continue          # 普通词，当术语会误导
                if len(_WORD_RE.findall(key)) > 3:
                    continue          # 太长的短句不当作术语
                total = sum(c for _d, c in cands)
                if total < 3 or len(cands) > 3:
                    continue          # 太少出现，或译文分歧太大（上下文相关词）
                best = cands[0][0]
                if len(best) == 1 and _CJK_RE.match(best) and len(key) >= 3:
                    continue          # 一个汉字对多个字母，基本是切坏的碎片
                self._table[key] = best
        except sqlite3.Error:
            self._table = {}
        finally:
            tm.close()
        return len(self._table)

    def hints(self, texts, limit=24, skip=None):
        """在一批文本里找出现过的官方术语，长词优先。

        skip 是已经在用户术语表里的词（小写），避免同一个词被说两遍。
        """
        if not texts:
            return []
        if self._table is None:
            self.load()
        if not self._table:
            return []
        skip = skip or set()

        found = {}
        for text in texts:
            low = str(text or "").lower()
            if not low:
                continue
            words = _WORD_RE.findall(low)
            n = len(words)
            for i in range(n):
                for span in (1, 2, 3):
                    if i + span > n:
                        break
                    cand = " ".join(words[i:i + span])
                    if cand in skip:
                        continue
                    dst = self._table.get(cand)
                    if dst and cand not in found:
                        found[cand] = dst
        # 长术语更具体，排前面；同样长度按原文顺序
        items = sorted(found.items(), key=lambda kv: (-len(kv[0]), kv[0]))
        return _dedupe_subterms(items)[:limit]

    def prompt_block(self, texts, limit=24, skip=None):
        """拼成可直接塞进 system prompt 的一段话；没命中返回空串。

        措辞上明确让位给用户自定义术语表：用户改过的译法永远优先。
        """
        h = self.hints(texts, limit=limit, skip=skip)
        if not h:
            return ""
        pairs = "；".join("%s=%s" % (s, d) for s, d in h)
        return ("以下术语建议沿用游戏官方译法：%s\n"
                "（若与上文自定义术语冲突，以自定义术语为准）" % pairs)


# ------------------------------------------------------------------ CLI


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="官方双语语料：构建 / 查询")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="构建数据库")
    b.add_argument("--game", default=None)
    b.add_argument("--cli", default=None)
    b.add_argument("--db", default=None)
    b.add_argument("--src", default=DEFAULT_SRC)
    b.add_argument("--dst", default=DEFAULT_DST)

    s = sub.add_parser("stats", help="查看库信息")
    s.add_argument("--db", default=None)

    q = sub.add_parser("lookup", help="查询一条或多条")
    q.add_argument("text", nargs="+")
    q.add_argument("--db", default=None)

    t = sub.add_parser("terms", help="测试术语提示（喂给机翻的词表）")
    t.add_argument("text", nargs="+")
    t.add_argument("--db", default=None)

    args = ap.parse_args(argv)
    if args.cmd == "build":
        meta = build(game=args.game, cli=args.cli, db_path=args.db,
                     src_lang=args.src, dst_lang=args.dst,
                     log=lambda m: print(m))
        for k in ("db", "pairs", "onscreen", "subtitle", "built_at", "seconds"):
            print("  %-9s %s" % (k, meta.get(k, "")))
        return 0
    if args.cmd == "stats":
        st = OfficialTM(args.db).stats()
        if not st.get("exists"):
            print("库不存在:", st.get("db"))
            return 1
        for k in ("db", "built_at", "src_lang", "dst_lang", "total",
                  "onscreen", "subtitle", "bytes"):
            print("  %-9s %s" % (k, st.get(k, "")))
        return 0
    tm = OfficialTM(args.db)
    if not tm.exists():
        print("库不存在，请先 build")
        return 1
    if args.cmd == "terms":
        th = TermHints(args.db)
        n = th.load()
        print("术语表大小:", n)
        block = th.prompt_block(args.text)
        print(block or "（没有命中术语）")
        return 0
    for t in args.text:
        hit = tm.lookup(t)
        print(("%s  →  %s" % (t, hit)) if hit else ("%s  →  （未命中）" % t))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
