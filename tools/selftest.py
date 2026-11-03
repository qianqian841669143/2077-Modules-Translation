# -*- coding: utf-8 -*-
"""
回归自测: 检查提取引擎在已知模组上的表现。

    python tools/selftest.py
    python tools/selftest.py --vortex "E:\\Vortex Mods\\cyberpunk2077"

没有测试模组时只做「语法/基本行为」自检, 不会失败。
"""
import io
import itertools
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import mod_text_engine as E  # noqa: E402
import locale_pack  # noqa: E402
import web_app  # noqa: E402
import undo_store  # noqa: E402
import text_noise as NOISE  # noqa: E402

VORTEX = r"E:\Vortex Mods\cyberpunk2077"

CASES = [
    # (目录名关键字, 期望条数 或 None, 说明)
    # 67 是 v1.x 的数; staging 里现在是 2.0 版, 同一份代码抽出 56 条
    # (HEAD vs 工作区各跑一次都是 56, 不是回归)。
    ("Anti-Tracking Breach-27505", 56, "REDscript @runtimeProperty"),
    ("Mod Configuration Menu 31640", None, "MCM 框架: .reds 应为 0 条误报"),
]


def find_mod(keyword):
    if not os.path.isdir(VORTEX):
        return None
    for d in os.listdir(VORTEX):
        if keyword.lower() in d.lower():
            p = os.path.join(VORTEX, d)
            if os.path.isdir(p):
                return p
    return None


def _drop_tx(root):
    """删掉指向某个临时目录的撤销记录：自测不该往用户数据里留东西。

    撤销记录写的是 app_root()/userdata/undo，跟测试自己设的 APPDATA 无关，
    所以每次「应用」都会在真实数据目录里留一条，得显式收尾。
    """
    import json
    base = undo_store.undo_root()
    if not os.path.isdir(base):
        return
    want = os.path.normcase(os.path.abspath(root))
    for name in os.listdir(base):
        p = os.path.join(base, name, "manifest.json")
        if not os.path.isfile(p):
            continue
        try:
            data = json.load(io.open(p, encoding="utf-8"))
        except Exception:
            continue
        if os.path.normcase(os.path.abspath(data.get("root") or "")) == want:
            shutil.rmtree(os.path.join(base, name), ignore_errors=True)


def check_vortex_map():
    """活动映射：名字解析 / 版本归一 / 部署清单判定。

    回归背景: 安装缓存里 1/3 的目录是已被替换、没部署的旧副本。照着目录名
    翻译会让一大半工作量落在游戏永远读不到的文件上，所以要能从
    vortex.deployment.msgpack 认出「哪些目录真的生效」。
    """
    import vortex_map as vm

    # 1) 两种 Nexus 命名格式 + 手动改名
    cases = [
        ("Anti-Tracking Breach-27505-2-0-1773756783.zip", 27505, "2-0"),
        ("ArchiveXL 4198 1.28.6 2026-10-29T12-59Z VquAqYEdu.zip", 4198, "1.28.6"),
        ("EnemiesOfNightCity_zhcn-26368-v1-0-0-1766148025", 26368, "v1-0-0"),
        ("Enemies of Night City-8467-PL-Beta-1-8-8-Hotfix-1720272238",
         8467, "PL-Beta-1-8-8-Hotfix"),
    ]
    for name, mid, ver in cases:
        title, got_mid, got_ver = vm.parse_name(name)
        assert got_mid == mid, (name, got_mid)
        assert got_ver == ver, (name, got_ver)
        assert title, name

    # 2) 版本号归一：同一个版本可能有「2-8-1 / 2.8.1 / v2.8.1」三种写法
    same = [("2-8-1", "2.8.1"), ("v1.9.9", "1.9.9"), ("0-0-0-20", "0.0.0.20"),
            ("1.0", "1.0.0"), ("2.15.1", "2-15-1")]
    for a, b in same:
        assert vm.norm_version(a) == vm.norm_version(b), (a, b)
    diff = [("2.3", "2.3.1"), ("2-8-1", "3.0.2"), ("0.10.6hf1", "0.10.6")]
    for a, b in diff:
        assert vm.norm_version(a) != vm.norm_version(b), (a, b)

    # 3) 自己造一份最小部署清单，验证 source 归并
    def mp_str(s):
        b = s.encode("utf-8")
        if len(b) < 32:
            return bytes([0xA0 | len(b)]) + b
        return bytes([0xD9, len(b)]) + b

    def mp_arr(items):
        return bytes([0x90 | len(items)]) + b"".join(items)

    def mp_map(pairs):
        return bytes([0x80 | len(pairs)]) + b"".join(
            mp_str(k) + v for k, v in pairs)

    def entry(rel, src):
        return mp_map([("relPath", mp_str(rel)), ("source", mp_str(src)),
                       ("target", mp_str("")), ("time", mp_str("1"))])

    LIVE = "Live Mod 111 1.0 2026-10-29T12-59Z QqWwEeRrT"
    OLD = "Old Mod 333 0-9 2026-10-01T08-00Z AbCdEfGhj"
    COLL = "vortex_collection_abc"

    blob = mp_map([
        ("deploymentMethod", mp_str("hardlink_activator")),
        ("stagingPath", mp_str(r"X:\stage")),
        ("targetPath", mp_str(r"X:\game")),
        ("deploymentTime", mp_str("1793399640438")),
        ("files", mp_arr([entry("archive/pc/mod/a.archive", LIVE),
                          entry("bin/x64/a.lua", LIVE),
                          entry("r6/scripts/b.reds", "Other Mod 222 2.0")])),
    ])

    tmp = tempfile.mkdtemp()
    try:
        stage = os.path.join(tmp, "stage")
        down = os.path.join(tmp, "down")
        os.makedirs(stage)
        os.makedirs(down)
        with open(os.path.join(stage, "vortex.deployment.msgpack"), "wb") as f:
            f.write(blob)
        info = vm.deployment_info(stage)
        assert info["ok"], info
        assert info["files"] == 3, info["files"]
        assert info["sources"] == {LIVE: 2, "Other Mod 222 2.0": 1}, info["sources"]
        assert info["method"] == "hardlink_activator", info["method"]
        assert vm.fmt_time(info["deployment_time"]), "部署时间没转出来"

        # 4) 三个目录：生效 / 陈旧 / 合集；下载包里另有更新的版本
        for name in (LIVE, OLD, COLL):
            os.makedirs(os.path.join(stage, name))
        for name in ("Live Mod 111 1.2 2026-11-02T09-00Z ZzXxCcVvB.zip",
                     "Old Mod 333 1-0 2026-11-01T08-00Z AbCdEfGhk.zip"):
            with open(os.path.join(down, name), "wb") as f:
                f.write(b"x")

        res = vm.scan(down, stage, r"X:\game")
        assert res["ok"], res
        rows = {r["dir"]: r for r in res["rows"]}
        assert rows[LIVE]["state"] == "update", rows[LIVE]
        assert rows[LIVE]["active"] is True
        assert rows[LIVE]["files"] == 2
        assert rows[OLD]["state"] == "pending", rows[OLD]
        assert rows[COLL]["state"] == "collection"
        assert res["stats"]["stagingDirs"] == 3, res["stats"]
        assert res["stats"]["deployed"] == 2, res["stats"]
        assert "有更新 1" in vm.summary_line(res), vm.summary_line(res)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return True


def check_translate_report():
    """机翻没翻完时必须回一次「账」：已翻译 / 未翻译 / 失败原因。

    回归背景: 以前只推一个 error 事件，界面只看到一行红字，用户不知道
    到底还有多少没翻、该不该继续。
    """
    import json
    import time
    import types

    class FakeWindow:
        def __init__(self):
            self.events = []

        def evaluate_js(self, script):
            try:
                body = script.split("window.__evt(", 1)[1]
                self.events.append(json.loads(body[:body.rfind(")")]))
            except Exception:
                pass

    class FakeMemory:
        def put(self, *a):
            pass

        def save(self):
            pass

    class FakeGlossary:
        def relevant(self, texts):
            return []

        def prompt_block(self, terms):
            return ""

    def run(fake_batch, count=3):
        api = web_app.Api("")
        api.rows = [{"file": "a.reds", "line": i + 1, "type": "界面文本",
                     "ctx": "", "orig": "text %d" % i, "trans": ""}
                    for i in range(count)]
        api.settings["use_tm"] = False
        api.settings["use_official_tm"] = False
        api.settings["api_base"] = "http://127.0.0.1:1/v1"
        win = FakeWindow()
        fake_mt = types.ModuleType("mt_client")
        fake_mt.resolve_model = lambda *a, **k: "fake"
        fake_mt.translate_batch = fake_batch
        fake_mt.warmup = lambda *a, **k: (False, "fake")
        old = sys.modules.get("mt_client")
        sys.modules["mt_client"] = fake_mt
        try:
            api._window = win
            api._memory = FakeMemory()
            api._glossary = FakeGlossary()
            r = api.translate(list(range(count)), [])
            assert r["ok"], r
            for _ in range(200):
                if not api._busy:
                    break
                time.sleep(0.02)
            assert not api._busy, "翻译线程没有结束"
        finally:
            if old is None:
                sys.modules.pop("mt_client", None)
            else:
                sys.modules["mt_client"] = old
        rep = None
        for ev in win.events:
            if ev.get("type") == "trans-report":
                rep = ev
        assert rep is not None, win.events
        assert rep["total"] == count, rep
        assert rep["ok"] + rep["left"] == count, rep
        assert len(rep["failed"]) == rep["left"], rep
        return rep

    # 接口直接报错：一条都没翻出来，必须给出原因
    def boom(*a, **k):
        raise RuntimeError("HTTP 500 - internal server error")

    rep = run(boom)
    assert rep["left"] == 3 and rep["ok"] == 0, rep
    assert rep["reasons"], rep
    assert any("HTTP 500" in s or "500" in s for s in rep["reasons"]), rep
    # 单批失败不再把整轮判成「已中止」：跳过该批、如实报「N 批没翻成」。
    # 以前这里一抛就 return，长任务跑到一半就整轮作废，要用户重新点一遍。
    assert rep["stopped"] is False, rep
    assert any("1 批" in s for s in rep["reasons"]), rep

    # 中间某一批失败：后面的批次必须照常跑完
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("timed out")
        texts = a[2]
        return ["译%d" % i for i in range(len(texts))]

    rep3 = run(flaky, count=20)
    assert rep3["ok"] == 12 and rep3["left"] == 8, rep3
    assert rep3["stopped"] is False, rep3
    assert any("1 批" in s for s in rep3["reasons"]), rep3

    # 模型返回空译文：不能算成功，要能点「继续」补上
    rep2 = run(lambda *a, **k: ["译文一", "", ""])
    assert rep2["ok"] == 1 and rep2["left"] == 2, rep2
    assert rep2["reasons"], rep2
    assert rep2["failed"] == [1, 2], rep2
    return True


def _tiny_http(respond):
    """起一个一次性的本地 HTTP 桩, 返回 (server, 收到的请求列表)。"""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                body = {}
            seen.append({"path": self.path, "body": body})
            code, payload = respond(self.path, body)
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except Exception:
                pass

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def check_mt_budget():
    """P0-1：每个模型请求都必须带输出上限。

    回归背景（2026-10-31 服务端日志）：一批只有 8 条短界面文案的请求生成过
    3254 个 token、单条 49.9 秒；近一小时 58 个请求里两次撞上 180 秒超时，
    都是这样来的。payload 里没有 max_tokens 时模型可以一路写到上下文上限。
    """
    import json

    import mt_client

    def respond(path, body):
        try:
            texts = json.loads(body["messages"][-1]["content"])
            if not isinstance(texts, list):
                texts = [texts]
        except Exception:
            texts = ["x"]
        content = json.dumps(["译%d" % i for i in range(len(texts))],
                             ensure_ascii=False)
        return 200, {"choices": [{"message": {"content": content}}]}

    srv, seen = _tiny_http(respond)
    try:
        base = "http://127.0.0.1:%d/v1" % srv.server_address[1]
        out = mt_client.translate_batch(base, "m", ["Close", "Cancel"])
        assert out == ["译0", "译1"], out
        short = seen[-1]["body"].get("max_tokens")
        assert short, seen[-1]["body"]
        assert 512 <= short <= 8192, short

        # 输入变长, 上限要跟着涨, 但仍然有硬顶
        mt_client.translate_batch(base, "m", ["x" * 4000] * 8)
        big = seen[-1]["body"].get("max_tokens")
        assert big > short and big <= 8192, (big, short)

        # 逐条兜底那条路径也必须带, 否则一条长文本又能挂满超时
        mt_client.translate_one(base, "m", "Close")
        one = seen[-1]["body"].get("max_tokens")
        assert one and one <= 8192, one
    finally:
        srv.shutdown()
    return True


def check_mt_warmup():
    """P0-2：预热走 Ollama 原生 /api/chat 并带 keep_alive。

    首次载入 27B 要 35-80 秒，以前这段时间整段算在第一个批次的 180 秒超时里
    （实测空闲后的首个请求 1m30s / 45s）。OpenAI 兼容层不认 keep_alive，只有
    原生接口认；别的后端没有这个端点，失败必须只当没做、不能中断翻译。
    """
    import mt_client

    def respond(path, body):
        if path.endswith("/api/chat"):
            return 200, {"model": "m", "message": {"content": "x"}, "done": True}
        return 404, {"error": "not found"}

    srv, seen = _tiny_http(respond)
    try:
        base = "http://127.0.0.1:%d/v1" % srv.server_address[1]
        ok, info = mt_client.warmup(base, "m", keep_alive="2h")
        assert ok is True, (ok, info)
        hit = seen[-1]
        assert hit["path"] == "/api/chat", hit["path"]
        assert hit["body"].get("keep_alive") == "2h", hit["body"]
        assert (hit["body"].get("options") or {}).get("num_predict") == 1, hit["body"]
    finally:
        srv.shutdown()

    def nope(path, body):
        return 404, {"error": "not found"}

    srv2, _seen2 = _tiny_http(nope)
    try:
        base2 = "http://127.0.0.1:%d/v1" % srv2.server_address[1]
        ok2, _msg = mt_client.warmup(base2, "m", timeout=10)
        assert ok2 is False, ok2
    finally:
        srv2.shutdown()
    return True


def check_baseline_layer():
    """官方译文基线：三层优先级 + 目标语言守卫 + 机翻前命中就不发模型。

    回归背景:
      1. 源码模式（.reds/.lua）以前完全没套官方语料，只有资源模式和批量队列套；
      2. 机翻前只查翻译记忆，整句命中官方译法的那部分也照样发给模型；
      3. 目标语言改成日语/英语时，官方中文语料仍然会被填进译文。
    """
    import time
    import types

    import baseline as Lbase
    import tm as TMod
    from localization import official_tm as Lotm

    tmp = tempfile.mkdtemp()
    try:
        db = os.path.join(tmp, "official.sqlite")
        con = Lotm._open_db(db, fresh=True)
        con.executemany(
            "INSERT INTO pairs (kind,key,src,dst,src_lang,dst_lang)"
            " VALUES (?,?,?,?,?,?)",
            [("onscreen", "k1", "Off", "关", "en-us", "zh-cn"),
             ("onscreen", "k2", "Warning", "警告", "en-us", "zh-cn"),
             ("onscreen", "k3", "Street Cred", "街头声望", "en-us", "zh-cn")])
        con.commit()
        con.close()
        off = Lotm.OfficialTM(db)
        assert off.exists()

        mem = TMod.Memory(os.path.join(tmp, "memory.json"))
        mem.put("Off", "简体中文", "关闭", origin="machine")
        mem.put("Warning", "简体中文", "警示语", origin="manual")
        mem.put("Unknown", "简体中文", "未知", origin="machine")

        rows = [{"orig": "Off", "trans": ""},
                {"orig": "Warning", "trans": ""},
                {"orig": "Street Cred", "trans": ""},
                {"orig": "Unknown", "trans": ""},
                {"orig": "Nothing here", "trans": ""}]
        st = Lbase.fill_rows(rows, official=off, memory=mem)

        # 官方优先于机翻记忆（Off 是单词条 → official-weak；Street Cred 两词 → official）
        assert rows[0]["trans"] == "关", rows[0]
        assert rows[0]["transSrc"] == "official-weak", rows[0]
        # 手改优先于官方
        assert rows[1]["trans"] == "警示语", rows[1]
        assert rows[1]["transSrc"] == "manual", rows[1]
        # 只有官方（多词短语 → 稳）
        assert rows[2]["trans"] == "街头声望", rows[2]
        assert rows[2]["transSrc"] == "official", rows[2]
        # 只有记忆
        assert rows[3]["trans"] == "未知", rows[3]
        assert rows[3]["transSrc"] == "machine", rows[3]
        # 谁都没有
        assert not rows[4]["trans"], rows[4]
        assert st["official"] == 1 and st["officialWeak"] == 1, st
        assert st["manual"] == 1, st
        assert st["memory"] == 1 and st["left"] == 1, st

        # 关掉「单词条也算」后，Off 不再由官方填，落到记忆/待翻
        rows2 = [{"orig": "Off", "trans": ""}]
        st2 = Lbase.fill_rows(rows2, official=off, memory=mem, allow_weak=False)
        assert rows2[0]["transSrc"] != "official-weak", rows2
        assert st2["officialWeak"] == 0, st2

        # 机翻不能把「手改」顶掉
        mem.put("Warning", "简体中文", "机翻会改坏", origin="machine")
        assert mem.get_entry("Warning", "简体中文")["text"] == "警示语"

        # 目标语言守卫：官方语料只有 en→zh-cn
        assert Lbase.can_use_official("日本語")[0] is False
        assert Lbase.can_use_official("English")[0] is False
        assert Lbase.can_use_official("简体中文")[0] is True

        # ---- 集成：Api 层优先用官方，且命中的不发模型
        api = web_app.Api("")
        api.settings["target_lang"] = "简体中文"
        api.settings["use_official_tm"] = True
        api.settings["use_tm"] = True
        api.settings["api_base"] = "http://127.0.0.1:1/v1"
        api._memory = mem

        class FakeWindow:
            def __init__(self):
                self.events = []

            def evaluate_js(self, script):
                self.events.append(script)

        class FakeGlossary:
            def relevant(self, texts):
                return []

            def prompt_block(self, terms):
                return ""

        win = FakeWindow()
        api._window = win
        api._glossary = FakeGlossary()
        api.rows = [{"orig": "Off", "line": 1, "file": "a.reds", "trans": ""},
                    {"orig": "Street Cred", "line": 2, "file": "a.reds", "trans": ""}]

        old_load = Lbase.load_official
        Lbase.load_official = lambda enabled, lang: (off, "")
        calls = {"n": 0}

        def fake_batch(base, model, texts, target=None, glossary=""):
            calls["n"] += 1
            return ["机翻"] * len(texts)

        fake_mt = types.ModuleType("mt_client")
        fake_mt.resolve_model = lambda *a, **k: "fake"
        fake_mt.translate_batch = fake_batch
        fake_mt.warmup = lambda *a, **k: (False, "fake")
        old_mt = sys.modules.get("mt_client")
        sys.modules["mt_client"] = fake_mt
        try:
            bstat = api._baseline_fill(api.rows)
            assert bstat["official"] >= 1, bstat
            assert [r["trans"] for r in api.rows] == ["关", "街头声望"], api.rows

            api.rows = [{"orig": "Off", "line": 1, "file": "a.reds", "trans": ""},
                        {"orig": "Street Cred", "line": 2, "file": "a.reds", "trans": ""}]
            r = api.translate([0, 1], [])
            assert r["ok"], r
            for _ in range(200):
                if not api._busy:
                    break
                time.sleep(0.02)
            assert not api._busy, "翻译线程没有结束"
            assert calls["n"] == 0, "官方基线已命中，不该再发给模型"
            assert [x["trans"] for x in api.rows] == ["关", "街头声望"], api.rows
        finally:
            Lbase.load_official = old_load
            if old_mt is None:
                sys.modules.pop("mt_client", None)
            else:
                sys.modules["mt_client"] = old_mt
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return True


def check_false_positive(items):
    """r"..." 资源路径 / 语言码 不应被提取"""
    bad = []
    for f, v in items.items():
        for it in v:
            t = it[3]
            if "\\" in t and " " not in t:
                bad.append((f, it))
            elif t in ("zh-cn", "zh-tw", "en-us", "en-us".upper()):
                bad.append((f, it))
            elif "inkatlas" in t or "inkfontfamily" in t:
                bad.append((f, it))
    return bad


def check_apply_roundtrip():
    """建一个临时 lua 文件, 提取 -> 回填 -> 再提取, 验证引号/转义/##ID"""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "sample.lua")
        src = (
            "local s = { displayName = 'Hello World', description = \"A long enough sentence here.\" }\n"
            "ui.addSwitch('/tab','Enable Thing','This turns the thing on.',true)\n"
            "ImGui.Button(\"All Presets##rootFolder\")\n"
        )
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(src)
        rows = E.extract_from_file(p)
        assert len(rows) == 5, rows
        for ln, typ, ctx, orig in rows:
            assert E.apply_translation(p, ln, orig, "中文" + str(ln)), orig
        text = open(p, encoding="utf-8", newline="").read()
        assert "##rootFolder" in text, "ImGui 内部 ID 丢了"
        assert "'中文1'" in text, text          # 单引号保留
        assert '"中文1"' in text, text          # 双引号保留
        assert "'中文2'" in text, text
        assert '"中文3##rootFolder"' in text, text
        again = E.extract_from_file(p)
        assert len(again) == 5, again
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



def check_codeware_text():
    """this.Text("key", "value") 只能收 value, 单词值也要收。"""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "English.reds")
        src = (
            'module X\n'
            'import Codeware.Localization.*\n'
            'public class English extends ModLocalizationPackage {\n'
            '  protected func DefineTexts() -> Void {\n'
            '    this.Text("MBC-Nem-Maelstrom-0", "Static");\n'
            '    this.Text("MBC-Nem-Animals-2", "Mad Dog");\n'
            '    this.Text(\n'
            '      "MBC-Multi-Line",\n'
            '      "A multi line value"\n'
            '    );\n'
            '  }\n'
            '}\n'
        )
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(src)
        rows = E.extract_from_file(p)
        got = [r[3] for r in rows]
        assert got == ["Static", "Mad Dog", "A multi line value"], got
        assert all(not x.startswith("MBC-") for x in got), got
        assert E.apply_translation(p, rows[0][0], rows[0][3], "静态")
        text = open(p, encoding="utf-8", newline="").read()
        assert 'this.Text("MBC-Nem-Maelstrom-0", "静态");' in text, text
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_target_language_skip():
    """提取时跳过当前目标语言文本和目标语言文件。"""
    tmp = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmp, "sample.lua"), "w", encoding="utf-8", newline="") as f:
            f.write(
                'ImGui.Text("已经是中文了")\n'
                'ImGui.Text("English source text")\n'
            )
        os.makedirs(os.path.join(tmp, "locales"))
        with open(os.path.join(tmp, "locales", "en-us.lua"),
                  "w", encoding="utf-8", newline="") as f:
            f.write('ImGui.Text("English locale text")\n')
        with open(os.path.join(tmp, "locales", "zh-cn.lua"),
                  "w", encoding="utf-8", newline="") as f:
            f.write('ImGui.Text("Target File Text Here")\n')

        files = locale_pack.detect(tmp, parse=True, target="zh-CN")
        exclude = {x["rel"] for x in files if x["locale"] == "zh-CN"}
        data, meta = E.extract_root_ex(
            tmp, loose=True, include_locale_lua=True,
            target_locale="zh-CN", exclude_rels=exclude)
        values = [it[3] for items in data.values() for it in items]
        assert "已经是中文了" not in values, values
        assert "English source text" in values, values
        assert "English locale text" in values, values
        assert "Target File Text Here" not in values, values
        assert meta["skipped_target"] >= 1, meta

        # 语言包里只选非目标语言文件, 并且值本身是中文时也跳过。
        os.makedirs(os.path.join(tmp, "pack", "locales"))
        with open(os.path.join(tmp, "pack", "locales", "en-us.json"),
                  "w", encoding="utf-8", newline="") as f:
            f.write('{"hello":"Hello world","already":"已经是中文"}')
        with open(os.path.join(tmp, "pack", "locales", "zh-cn.json"),
                  "w", encoding="utf-8", newline="") as f:
            f.write('{"hello":"你好世界","already":"已经是中文"}')
        pack = os.path.join(tmp, "pack")
        found = locale_pack.detect(pack, parse=True, target="zh-CN")
        srcs = locale_pack.pick_sources(found, "zh-CN")
        assert srcs and all(f["locale"] != "zh-CN" for f in srcs), srcs
        rows, lmeta = locale_pack.extract(pack, srcs, "zh-CN")
        origs = [r["orig"] for r in rows]
        assert "Hello world" in origs, origs
        assert "已经是中文" not in origs, origs
        assert lmeta["skipped_target"] >= 1, lmeta
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_wrapper_and_notify_extraction():
    """包装函数 label/desc、通知函数、提示表与 message 拼接都应提取。"""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "wrapped.lua")
        src = (
            "local function switch(path, label, desc, key) end\n"
            "switch('/x', 'Aerial takedowns', 'Allow takedowns from above.', 'aerial')\n"
            "local LABELS_BY_INPUT = { Choice2 = 'Pickpocket', Choice3 = 'Mug Eddies' }\n"
            'local RUMOURS = {"\'A long rumour sentence here.\'"}\n'
            "Util.Notify('A witness called NCPD.')\n"
            'local message = string.format("Mugged %d eddies", 10)\n'
            'message = message .. ". They fight back!"\n'
            'table.insert(found, "no gang members nearby")\n'
        )
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(src)
        rows = E.extract_from_file(p)
        got = [(t, v) for _ln, t, _ctx, v in rows]
        vals = [v for _t, v in got]
        for want in ("Aerial takedowns", "Allow takedowns from above.",
                     "Pickpocket", "Mug Eddies",
                     "'A long rumour sentence here.'",
                     "A witness called NCPD.", "Mugged %d eddies",
                     ". They fight back!", "no gang members nearby"):
            assert want in vals, (want, vals)
        assert ("显示名称", "Aerial takedowns") in got, got
        assert ("描述", "Allow takedowns from above.") in got, got
        assert ("界面文本", "A witness called NCPD.") in got, got
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_reds_locale_replace_all():
    """StrReplaceAll 多语言表只提取目标语言条目，并能正确回填。"""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "perk-tooltip.reds")
        src = (
            'public class X {\n'
            '  public static func F(description: String) -> String {\n'
            '    // en\n'
            '    description = StrReplaceAll(description, "Unlocks <Rich>dual wield</>, but each weapon deals -{float_0}% damage.", "Unlocks the ability to <Rich>dual wield</>.");\n'
            '    // de\n'
            '    description = StrReplaceAll(description, "Schaltet <Rich>beidhändig</> frei, aber jede Waffe verursacht -{float_0}% Schaden.", "Schaltet die Fähigkeit frei, <Rich>beidhändig</> zu kämpfen.");\n'
            '    // zh-cn\n'
            '    description = StrReplaceAll(description, "解锁<Rich>双持</>，但每把武器伤害 -{float_0}%。", "解锁能力：<Rich>双持</>。");\n'
            '    // zh-tw\n'
            '    description = StrReplaceAll(description, "解鎖<Rich>雙持</>，但每把武器傷害 -{float_0}%。", "開放此能力可<Rich>雙持</>武器。");\n'
            '    return description;\n'
            '  }\n'
            '}\n'
        )
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(src)
        rows = E.extract_from_file(p, target_locale="zh-CN")
        assert len(rows) == 1, rows
        assert rows[0][2] == "StrReplaceAll[zh-cn]", rows
        assert rows[0][3] == "解锁能力：<Rich>双持</>。", rows
        assert E.apply_translation(p, rows[0][0], rows[0][3], "解锁双持能力。")
        text = open(p, encoding="utf-8", newline="").read()
        assert text.count("解锁双持能力。") == 1, text
        assert "Unlocks the ability to <Rich>dual wield</>." in text, text
        assert "Schaltet die Fähigkeit frei" in text, text
        assert "開放此能力可<Rich>雙持</>武器。" in text, text

        rows_en = E.extract_from_file(p, target_locale="en-US")
        assert len(rows_en) == 1 and rows_en[0][2] == "StrReplaceAll[en]", rows_en
        # 目标语言不在表中时不要乱取其它语言；没有目标时用英文作参考。
        assert E.extract_from_file(p, target_locale="ko-KR") == []
        assert E.extract_from_file(p)[0][2] == "StrReplaceAll[en]"
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_lua_apply_with_comments():
    """Lua 注释/长字符串后面的文本也必须能回填 (lang_of 不能返回 .lua)。"""
    assert E.lang_of("x.lua") == "lua"
    assert E.lang_of("x.reds") == "reds"
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "comments.lua")
        src = (
            '-- a comment with an unmatched " quote\n'
            "local t = [[ a long string with ' quote ]]\n"
            'ImGui.Text("First after comment")\n'
            '-- another comment with an unmatched \' quote\n'
            'ImGui.Text("Second after comment")\n'
        )
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(src)
        n, failed = E.apply_many_ex(p, [
            (3, "First after comment", "第一条"),
            (5, "Second after comment", "第二条"),
        ])
        assert n == 2 and not failed, (n, failed)
        text = open(p, encoding="utf-8", newline="").read()
        assert '"第一条"' in text and '"第二条"' in text, text
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_apply_failure_details():
    """部分回填失败时应返回精确到条目的失败明细。"""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "sample.lua")
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write('ImGui.Text("One valid line")\n')
        n, failed = E.apply_many_ex(p, [
            (1, "One valid line", "有效译文"),
            (1, "Missing source line", "失败译文"),
        ])
        assert n == 1, n
        assert len(failed) == 1, failed
        idx, _line, orig, _trans, reason, level = failed[0]
        assert idx == 1 and orig == "Missing source line", failed
        assert reason and level == "hard", failed
        text = open(p, encoding="utf-8", newline="").read()
        assert 'ImGui.Text("有效译文")' in text, text
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_clear_and_skip_features():
    """清空当前结果/译文、跳过翻译状态与 Excel 读写。"""
    tmp = tempfile.mkdtemp()
    try:
        api = web_app.Api("")
        # pywebview 会递归遍历 js_api 的公开属性；这些属性必须保持私有，
        # 否则会遍历 Window / WebView2 COM 对象并导致启动卡死。
        public = [n for n in dir(api) if not n.startswith("_")]
        assert "window" not in public, public
        assert "memory" not in public, public
        assert "glossary" not in public, public
        api.rows = [
            {"orig": "First text", "trans": "第一条"},
            {"orig": "Second text", "trans": ""},
        ]
        assert api.clear_translations()["ok"]
        assert all(r["trans"] == "" for r in api.rows)
        # 所有目标都跳过时，不应进入需要 API 地址的机翻流程。
        res = api.translate(None, [0, 1])
        assert not res["ok"], res
        res = api.translate([0], [0])
        assert not res["ok"], res
        api.clear_rows()
        assert api.rows == [] and api.meta == {}

        path = os.path.join(tmp, "skip.xlsx")
        rows = [
            {"file": "a.reds", "line": 1, "type": "界面文本", "ctx": "x",
             "orig": "Hello world", "trans": "你好世界", "skip": True},
            {"file": "a.reds", "line": 2, "type": "界面文本", "ctx": "y",
             "orig": "Another text", "trans": "", "skip": False},
        ]
        assert api.export_xlsx(path, rows)["ok"]
        imported = api.import_xlsx(path)["items"]
        assert imported[0]["skip"] is True, imported
        assert imported[1]["skip"] is False, imported

        root = os.path.dirname(HERE)
        js = open(os.path.join(root, "src", "web", "app.js"),
                  encoding="utf-8-sig").read()
        html = open(os.path.join(root, "src", "web", "index.html"),
                    encoding="utf-8-sig").read()
        css = open(os.path.join(root, "src", "web", "style.css"),
                   encoding="utf-8-sig").read()
        for marker in ('id="btn-clear-trans"', 'id="btn-clear-rows"',
                       'data-i18n="跳过"'):
            assert marker in html, marker
        for marker in ("skip-box", "toggle-skip", "clearCurrentRows",
                       "clearCurrentTranslations", "r.skip", "未跳过"):
            assert marker in js, marker
        assert ".trow.skipped" in css
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_translate_pipeline():
    """机翻工作线程必须能拿到 mt_client 并完成 rows/progress/done 事件。

    回归背景: 曾只在 Api.translate() 内 import mt_client，而真正干活的
    Api._run_translate() 是另一个方法，工作线程因此直接 NameError，
    前端只看到「机翻失败: name 'mt_client' is not defined」。
    """
    import time
    import types

    api = web_app.Api("")
    api.rows = [
        {"file": "a.reds", "line": 1, "type": "界面文本", "ctx": "x",
         "orig": "First text", "trans": ""},
        {"file": "a.reds", "line": 2, "type": "界面文本", "ctx": "y",
         "orig": "Second text", "trans": ""},
    ]
    api.settings["use_tm"] = False
    events = []

    class FakeWindow:
        def evaluate_js(self, script):
            events.append(script)

    class FakeMemory:
        def __init__(self):
            self.saved = []
        def put(self, src, target, trans):
            self.saved.append((src, target, trans))
        def save(self):
            pass

    class FakeGlossary:
        def relevant(self, texts):
            return []
        def prompt_block(self, terms):
            return ""

    fake_mt = types.ModuleType("mt_client")
    fake_mt.resolve_model = lambda *a, **k: "fake-model"
    fake_mt.translate_batch = lambda *a, **k: [
        "译文一", "译文二"][:len(k.get("texts", a[2] if len(a) > 2 else []))]

    old_mt = sys.modules.get("mt_client")
    sys.modules["mt_client"] = fake_mt
    try:
        api._window = FakeWindow()
        api._memory = FakeMemory()
        api._glossary = FakeGlossary()
        result = api.translate([0, 1], [])
        assert result["ok"], result
        for _ in range(100):
            if not api._busy:
                break
            time.sleep(0.02)
        assert not api._busy, "翻译线程没有结束"
    finally:
        if old_mt is None:
            sys.modules.pop("mt_client", None)
        else:
            sys.modules["mt_client"] = old_mt

    assert [r["trans"] for r in api.rows] == ["译文一", "译文二"], api.rows
    joined = "\n".join(events)
    assert '"type": "rows"' in joined, joined
    assert '"type": "progress"' in joined, joined
    assert '"type": "done"' in joined, joined
    assert '"type": "error"' not in joined, joined
    assert "mt_client" not in joined, joined
    return True


def check_translate_retry_and_echo():
    """批量里被跳过 / 原样返回的行要单条补翻，占位符密集的直接走单条 (P12/P17)。

    背景：`translate_batch` 保证返回长度一致，但模型会对短串返回空串、对满是
    `{…}` 的模板把英文原样吐回来。以前 `if not text: continue` 静默跳过，用户
    点「应用」时才发现文件只写回了一半，而报告里只有一句「空译文 7 条」。
    echo 更隐蔽：它落进了 `trans`，被算成「已填」，还因为「译文=原文」不进
    记忆库，下次加载又变空 —— 表现为「这条怎么永远翻不了」。
    """
    import time
    import types

    api = web_app.Api("")
    api.rows = [
        {"file": "a.reds", "line": 1, "type": "界面文本", "ctx": "x",
         "orig": "First text", "trans": ""},
        {"file": "a.reds", "line": 2, "type": "界面文本", "ctx": "y",
         "orig": "Second text", "trans": ""},
        {"file": "a.reds", "line": 3, "type": "界面文本", "ctx": "z",
         "orig": "Third text", "trans": ""},
        {"file": "a.reds", "line": 4, "type": "长句文本", "ctx": "w",
         "orig": "You have {a} items, {b} coins and {c} boxes", "trans": ""},
    ]
    api.settings["use_tm"] = False
    api.settings["use_official_tm"] = False
    events, batches = [], []

    class FakeWindow:
        def evaluate_js(self, script):
            events.append(script)

    class FakeMemory:
        def put(self, *a, **k):
            pass
        def save(self):
            pass

    class FakeGlossary:
        def relevant(self, texts):
            return []
        def prompt_block(self, terms):
            return ""

    fake_mt = types.ModuleType("mt_client")
    fake_mt.resolve_model = lambda *a, **k: "fake-model"

    def fake_batch(api_base, model, texts, **kw):
        batches.append(list(texts))
        out = []
        for t in texts:
            if t == "First text":
                out.append("")               # 被整条跳过
            elif t == "Second text":
                out.append(t)                # 原样返回
            else:
                out.append("译文三")
        return out

    def fake_one(api_base, model, text, **kw):
        return {"First text": "补翻一",
                "Second text": "补翻二",
                "You have {a} items, {b} coins and {c} boxes":
                    "你有 {a} 件物品、{b} 枚硬币和 {c} 个盒子"}.get(text, "")

    fake_mt.translate_batch = fake_batch
    fake_mt.translate_one = fake_one

    old_mt = sys.modules.get("mt_client")
    sys.modules["mt_client"] = fake_mt
    try:
        api._window = FakeWindow()
        api._memory = FakeMemory()
        api._glossary = FakeGlossary()
        result = api.translate([0, 1, 2, 3], [])
        assert result["ok"], result
        for _ in range(200):
            if not api._busy:
                break
            time.sleep(0.02)
        assert not api._busy, "翻译线程没有结束"
    finally:
        if old_mt is None:
            sys.modules.pop("mt_client", None)
        else:
            sys.modules["mt_client"] = old_mt

    got = [r["trans"] for r in api.rows]
    assert got[0] == "补翻一" and got[1] == "补翻二", got
    assert got[2] == "译文三", got
    assert got[3].startswith("你有 {a} 件物品"), got
    # 占位符密集的那条根本没进批量
    assert batches and all(len(b) == 3 for b in batches), batches
    assert all(not any("{" in t for t in b) for b in batches), batches

    joined = "\n".join(events)
    assert '"type": "trans-report"' in joined, joined
    assert "原样返回" in joined, joined
    assert "单条补翻成功" in joined, joined
    # echo 的那条不能再被算成「已填」
    assert '"left": 0' in joined, joined
    return True


def check_translate_dedup():
    """同一句原文只发给模型一次，重复行复用同一份译文。

    回归背景: 以前按行发，同一句出现 40 次就发 40 次——慢，而且温度不为 0 时
    会得到两种译法，同一个词在同一个 mod 里前后不一致。
    """
    import time
    import types

    api = web_app.Api("")
    api.rows = [
        {"file": "a.reds", "line": 1, "type": "界面文本", "ctx": "x",
         "orig": "Close", "trans": ""},
        {"file": "a.reds", "line": 2, "type": "界面文本", "ctx": "x",
         "orig": "Close", "trans": ""},
        {"file": "a.reds", "line": 3, "type": "界面文本", "ctx": "y",
         "orig": "Cancel", "trans": ""},
        {"file": "a.reds", "line": 4, "type": "界面文本", "ctx": "x",
         "orig": "  Close  ", "trans": ""},     # 只差空白，算同一句
    ]
    api.settings["use_tm"] = False
    api.settings["use_official_tm"] = False
    calls = []
    events = []

    class FakeWindow:
        def evaluate_js(self, script):
            events.append(script)

    class FakeMemory:
        def __init__(self):
            self.saved = []

        def put(self, src, target, trans):
            self.saved.append((src, target, trans))

        def save(self):
            pass

    class FakeGlossary:
        def relevant(self, texts):
            return []

        def prompt_block(self, terms):
            return ""

    def fake_batch(base, model, texts, **kw):
        calls.append(list(texts))
        table = {"Close": "关闭", "Cancel": "取消"}
        return [table.get(str(t).strip(), "") for t in texts]

    fake_mt = types.ModuleType("mt_client")
    fake_mt.resolve_model = lambda *a, **k: "fake-model"
    fake_mt.translate_batch = fake_batch

    old_mt = sys.modules.get("mt_client")
    sys.modules["mt_client"] = fake_mt
    try:
        api._window = FakeWindow()
        api._memory = FakeMemory()
        api._glossary = FakeGlossary()
        result = api.translate([0, 1, 2, 3], [])
        assert result["ok"], result
        for _ in range(100):
            if not api._busy:
                break
            time.sleep(0.02)
        assert not api._busy, "翻译线程没有结束"
    finally:
        if old_mt is None:
            sys.modules.pop("mt_client", None)
        else:
            sys.modules["mt_client"] = old_mt

    sent = [t for batch in calls for t in batch]
    assert sorted(sent) == sorted(["Close", "Cancel"]), calls
    assert [r["trans"] for r in api.rows] == ["关闭", "关闭", "取消", "关闭"], api.rows
    assert all(r.get("transSrc") == "machine" for r in api.rows), api.rows

    rep = [e for e in events if "trans-report" in e]
    assert rep, events
    assert '"dedup": 2' in rep[-1], rep[-1]
    return True


def check_codeware_provider_apply():
    """Codeware 语言包应用: 目标类名唯一, 并自动注册到 Provider。"""
    tmp = tempfile.mkdtemp()
    old_appdata = os.environ.get("APPDATA")
    os.environ["APPDATA"] = os.path.join(tmp, "appdata")
    try:
        base = os.path.join(tmp, "r6", "scripts", "Demo", "Localization")
        os.makedirs(base)
        src_rel = "r6/scripts/Demo/Localization/BoardEnglish.reds"
        tgt_rel = "r6/scripts/Demo/Localization/BoardChineseSimplified.reds"
        prov_rel = "r6/scripts/Demo/Localization/BoardProvider.reds"
        src = (
            'module Demo.Localization.Wire\n\n'
            'import Codeware.Localization.*\n\n'
            'public class English extends ModLocalizationPackage {\n'
            '  protected func DefineTexts() -> Void {\n'
            '    this.Text("DEMO-Gigs", "Gigs");\n'
            '  }\n'
            '}\n'
        )
        prov = (
            'module Demo.Localization.Wire\n\n'
            'import Codeware.Localization.*\n\n'
            'public class LocalizationProvider extends ModLocalizationProvider {\n'
            '  public func GetPackage(language: CName) -> ref<ModLocalizationPackage> {\n'
            '    switch language {\n'
            '      case n"en-us": return new English();\n'
            '      default: return null;\n'
            '    }\n'
            '  }\n\n'
            '  public func GetFallback() -> CName {\n'
            '    return n"en-us";\n'
            '  }\n'
            '}\n'
        )
        io.open(os.path.join(tmp, src_rel.replace("/", os.sep)), "w",
                encoding="utf-8", newline="").write(src)
        io.open(os.path.join(tmp, prov_rel.replace("/", os.sep)), "w",
                encoding="utf-8", newline="").write(prov)

        api = web_app.Api("")
        res = api._do_apply([{
            "root": tmp, "file": src_rel, "target": tgt_rel,
            "line": 7, "orig": "Gigs", "trans": "任务", "kind": "text",
        }])
        assert res["ok"] == 1 and res["fail"] == 0, res
        target = io.open(os.path.join(tmp, tgt_rel.replace("/", os.sep)),
                         encoding="utf-8").read()
        assert "public class ChineseSimplified extends ModLocalizationPackage" in target, target
        assert '"任务"' in target, target
        provider = io.open(os.path.join(tmp, prov_rel.replace("/", os.sep)),
                           encoding="utf-8").read()
        assert 'case n"zh-cn": return new ChineseSimplified();' in provider, provider
        assert provider.count('case n"zh-cn"') == 1, provider
        assert os.path.exists(os.path.join(
            tmp, prov_rel.replace("/", os.sep)) + ".bak"), "Provider 未备份"
        report = locale_pack.prepare_codeware_package(tmp, src_rel, tgt_rel)
        assert report["applied"], report
        provider2 = io.open(os.path.join(tmp, prov_rel.replace("/", os.sep)),
                            encoding="utf-8").read()
        assert provider2.count('case n"zh-cn"') == 1, provider2
        target2 = io.open(os.path.join(tmp, tgt_rel.replace("/", os.sep)),
                          encoding="utf-8").read()
        assert "public class ChineseSimplified" in target2, target2

        base2 = os.path.join(tmp, "r6", "scripts", "Demo2", "Localization")
        packdir = os.path.join(base2, "Packages")
        os.makedirs(packdir)
        src2_rel = "r6/scripts/Demo2/Localization/Packages/English.reds"
        tgt2_rel = "r6/scripts/Demo2/Localization/Packages/ChineseSimplified.reds"
        prov2_rel = "r6/scripts/Demo2/Localization/LocalizationProvider.reds"
        src2 = (
            'module Demo2.Localization.Packages\n\n'
            'import Codeware.Localization.*\n\n'
            'public class English extends ModLocalizationPackage {\n'
            '  protected func DefineTexts() -> Void {\n'
            '    this.Text("DEMO-Close", "Close");\n'
            '  }\n'
            '}\n'
        )
        prov2 = (
            'module Demo2.Localization\n'
            'import Demo2.Localization.Packages.*\n'
            'import Codeware.Localization.*\n\n'
            'public class LocalizationProvider extends ModLocalizationProvider {\n'
            '  public func GetPackage(language: CName) -> ref<ModLocalizationPackage> {\n'
            '    switch language {\n'
            '      case n"en-us": return new English();\n'
            '      default: return null;\n'
            '    }\n'
            '  }\n'
            '}\n'
        )
        io.open(os.path.join(tmp, src2_rel.replace("/", os.sep)), "w",
                encoding="utf-8", newline="").write(src2)
        io.open(os.path.join(tmp, prov2_rel.replace("/", os.sep)), "w",
                encoding="utf-8", newline="").write(prov2)
        res2 = api._do_apply([{
            "root": tmp, "file": src2_rel, "target": tgt2_rel,
            "line": 7, "orig": "Close", "trans": "关闭", "kind": "text",
        }])
        assert res2["ok"] == 1 and res2["fail"] == 0, res2
        provider2 = io.open(os.path.join(tmp, prov2_rel.replace("/", os.sep)),
                            encoding="utf-8").read()
        assert 'case n"zh-cn": return new ChineseSimplified();' in provider2, provider2
        assert 'import Demo2.Localization.Packages.*' in provider2, provider2
        return True
    finally:
        if old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = old_appdata
        _drop_tx(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def check_provider_pairs_and_undo():
    """Provider 自动配对、精确写回和撤销上次应用。"""
    tmp = tempfile.mkdtemp()
    old_appdata = os.environ.get("APPDATA")
    os.environ["APPDATA"] = os.path.join(tmp, "appdata")
    try:
        modroot = os.path.join(tmp, "mod")
        base = os.path.join(modroot, "r6", "scripts", "Demo", "Localization")
        os.makedirs(base)
        src_rel = "r6/scripts/Demo/Localization/BoardEnglish.reds"
        prov_rel = "r6/scripts/Demo/Localization/BoardProvider.reds"
        tgt_rel = "r6/scripts/Demo/Localization/BoardChineseSimplified.reds"
        src = (
            'module Demo.Localization.Wire\n\n'
            'import Codeware.Localization.*\n\n'
            'public class English extends ModLocalizationPackage {\n'
            '  protected func DefineTexts() -> Void {\n'
            '    this.Text("DEMO-Gigs", "Gigs");\n'
            '  }\n'
            '}\n'
        )
        prov = (
            'module Demo.Localization.Wire\n\n'
            'import Codeware.Localization.*\n\n'
            'public class LocalizationProvider extends ModLocalizationProvider {\n'
            '  public func GetPackage(language: CName) -> ref<ModLocalizationPackage> {\n'
            '    switch language {\n'
            '      case n"en-us": return new English();\n'
            '      default: return null;\n'
            '    }\n'
            '  }\n'
            '}\n'
        )
        for rel, text in ((src_rel, src), (prov_rel, prov)):
            path = os.path.join(modroot, rel.replace("/", os.sep))
            io.open(path, "w", encoding="utf-8", newline="").write(text)
        pairs, issues = locale_pack.discover_provider_pairs(modroot, "zh-CN")
        assert len(pairs) == 1 and not issues, (pairs, issues)
        pair = pairs[0]
        assert pair["source"] == src_rel and pair["provider"] == prov_rel, pair
        assert pair["target"] == tgt_rel and pair["match"] == "same-module", pair
        api = web_app.Api("")
        api.settings["use_tm"] = False
        res = api._do_load(modroot, {
            "localeFirst": True, "providerPair": True,
            "lua": False, "loose": False, "locale": False,
        })
        assert res.get("ok") and res.get("mode") == "provider", res
        assert res.get("pairCount") == 1, res
        rows = res.get("rows") or []
        assert rows and rows[0].get("provider") == prov_rel, rows
        assert rows[0].get("target") == tgt_rel, rows
        applied = api._do_apply([{
            "root": modroot, "file": src_rel, "target": tgt_rel,
            "line": rows[0]["line"], "orig": rows[0]["orig"],
            "trans": "任务", "kind": "text",
        }])
        assert applied["ok"] == 1 and applied.get("undoId"), applied
        target_path = os.path.join(modroot, tgt_rel.replace("/", os.sep))
        provider_path = os.path.join(modroot, prov_rel.replace("/", os.sep))
        assert os.path.isfile(target_path), "目标语言包未创建"
        provider_text = io.open(provider_path, encoding="utf-8").read()
        assert 'case n"zh-cn": return new ChineseSimplified();' in provider_text, provider_text
        undone = undo_store.undo_last(modroot)
        assert undone.get("ok"), undone
        assert not os.path.exists(target_path), "撤销后新建目标文件仍存在"
        assert not os.path.exists(target_path + ".bak"), "撤销后目标 .bak 仍存在"
        provider_text = io.open(provider_path, encoding="utf-8").read()
        assert 'case n"zh-cn"' not in provider_text, provider_text
        assert not os.path.exists(provider_path + ".bak"), "撤销后 Provider .bak 仍存在"

        # 嵌套 Packages + 上层 import * 也应能配对；多个同名 import 必须判为歧义。
        pdir = os.path.join(modroot, "r6", "scripts", "Nested", "Packages")
        os.makedirs(pdir)
        nested_src = "r6/scripts/Nested/Packages/English.reds"
        nested_prov = "r6/scripts/Nested/LocalizationProvider.reds"
        nested_text = (
            'module Nested.Packages\n'
            'import Codeware.Localization.*\n'
            'public class English extends ModLocalizationPackage {\n'
            '  protected func DefineTexts() -> Void { this.Text("N-Close", "Close"); }\n'
            '}\n'
        )
        nested_provider_text = (
            'module Nested.Root\n'
            'import Nested.Packages.*\n'
            'import Codeware.Localization.*\n'
            'public class LocalizationProvider extends ModLocalizationProvider {\n'
            '  public func GetPackage(language: CName) -> ref<ModLocalizationPackage> {\n'
            '    switch language { case n"en-us": return new English(); default: return null; }\n'
            '  }\n'
            '}\n'
        )
        for rel, text in ((nested_src, nested_text), (nested_prov, nested_provider_text)):
            io.open(os.path.join(modroot, rel.replace("/", os.sep)),
                    "w", encoding="utf-8", newline="").write(text)
        npairs, nissues = locale_pack.discover_provider_pairs(modroot, "zh-CN")
        nmatch = [p for p in npairs if p["source"] == nested_src]
        assert nmatch and nmatch[0]["match"] == "wildcard-import", (npairs, nissues)
        io.open(os.path.join(modroot, nested_prov.replace("/", os.sep)),
                "w", encoding="utf-8", newline="").write(
            nested_provider_text.replace(
                'import Nested.Packages.*\n',
                'import Nested.Packages.*\nimport Other.Packages.*\n'))
        other = os.path.join(modroot, "r6", "scripts", "Other", "Packages", "English.reds")
        os.makedirs(os.path.dirname(other))
        io.open(other, "w", encoding="utf-8", newline="").write(
            nested_text.replace("module Nested.Packages", "module Other.Packages"))
        apairs, aissues = locale_pack.discover_provider_pairs(modroot, "zh-CN")
        assert not [p for p in apairs if p["source"] == nested_src], apairs
        assert any("多个 import *" in (x.get("reason") or "") for x in aissues), aissues
        return True
    finally:
        if old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = old_appdata
        if "modroot" in locals():
            _drop_tx(modroot)
        shutil.rmtree(tmp, ignore_errors=True)


def check_context_copy_ui():
    """右键菜单：复制单元格、三类分组、以及裁切/撤销那几处约定。"""
    root = os.path.dirname(HERE)
    with open(os.path.join(root, "src", "web", "app.js"),
              "r", encoding="utf-8-sig") as f:
        js = f.read()
    assert 'data-act="copy-cell"' in js
    assert 'data-col="orig"' in js
    assert 'data-col="trans"' in js
    assert 'copyToClipboard' in js

    # 「复制原文」与「复制此单元格（列名）」都撤了：前者和后者重复，后者会让
    # 菜单项宽度随列名跳动。现在只有一项固定文案的「复制单元格内容」。
    assert 'data-act="copy"' not in js, "还留着单独的「复制原文」项"
    assert "复制此单元格" not in js, "复制项应改成固定文案"

    # 「跳过」是勾选框，不是可复制的文本；空值和表格外都要置灰。
    assert 'if (col === "skip") return { col, label, value: "", disabled: true };' in js
    assert "disabled: !value" in js
    assert "canCopy" in js

    # 第一类只看左键选中的行：组标题固定，没选中就整组置灰。
    assert 'data-act="locate"' in js
    assert "左键选中" in js
    assert "state.sel >= 0 && !!state.rows[state.sel]" in js

    # 「合并重复行」已从「更多」菜单迁到右键菜单：整表操作放右键更顺手，
    # 也少了一个要来回改文案、按模式隐藏的有状态按钮。
    assert 'data-act="merge"' in js, "右键菜单缺「合并重复行」"
    assert "toggleMerge()" in js, "右键菜单没有调用 toggleMerge"
    with open(os.path.join(root, "src", "web", "index.html"),
              "r", encoding="utf-8-sig") as f:
        html = f.read()
    assert 'id="btn-merge"' not in html, "「更多」菜单里还留着旧按钮"
    assert 'showApplyErrors' in js
    assert 'modal-apply-errors' in js
    assert '多语言替换表' in js

    # 两个「显示全部」曾撞名：只看文件 / 只看已翻译 的逆操作各叫各的，
    # 顶部那颗清范围按钮改成「清除筛选」。
    assert "显示所有文件" in js
    assert "显示所有行" in js
    assert 'data-i18n="清除筛选"' in html

    # 行身份带 entryId：资源模式同文件同原文的条目不能再被一起跳过/清空。
    assert 'String(x.entryId || "")' in js, "rowKey 没把 entryId 算进去"
    assert "rowKeysFor" in js

    # 编辑框失焦与换行前要结算，否则点别的行就把刚敲的译文丢了。
    assert "function flushEditor" in js
    assert "_transDirty" in js
    assert 'flushEditor();           // 先结算上一行，再换目标' in js or \
        "flushEditor()" in js

    # 菜单是浮层：Esc 与滚动都要收掉。
    assert 'if (e.key === "Escape") closeCtxMenu();' in js
    assert 'addEventListener("scroll", closeCtxMenu' in js
    return True


def check_revert_source():
    """还原翻译：从下载包取回原件、删掉本工具新建的产物。"""
    import zipfile
    import revert_source as R

    tmp = tempfile.mkdtemp(prefix="mt-revert-")
    try:
        dl = os.path.join(tmp, "downloads")
        os.makedirs(dl)
        mod = os.path.join(tmp, "Test Mod 12345 1.0 2026-10-24T18-07Z AbCdEfgHi")
        os.makedirs(os.path.join(mod, "archive", "pc", "mod"))
        os.makedirs(os.path.join(mod, "r6", "scripts"))

        pkg = os.path.join(dl, "Test Mod-12345-1-0-1700000000.zip")
        with zipfile.ZipFile(pkg, "w") as zf:
            zf.writestr("archive/pc/mod/x.archive", "ORIGINAL")
            zf.writestr("r6/scripts/a.reds", "ORIG")

        with open(os.path.join(mod, "archive", "pc", "mod", "x.archive"),
                  "w", encoding="utf-8") as f:
            f.write("CHANGED")
        with open(os.path.join(mod, "r6", "scripts", "a.reds"),
                  "w", encoding="utf-8") as f:
            f.write("CHANGED")
        prod = os.path.join(mod, "archive", "pc", "mod", "x_zh-cn.archive")
        with open(prod, "w", encoding="utf-8") as f:
            f.write("PRODUCT")

        wl = {"archive/pc/mod/x.archive": True,
              "r6/scripts/a.reds": True,
              "archive/pc/mod/x_zh-cn.archive": False}
        plan = R.build_plan(mod, wl, dl)
        assert plan["source"] == "package", plan
        assert len(plan["restore"]) == 2, plan["restore"]
        assert plan["remove"] == ["archive/pc/mod/x_zh-cn.archive"], plan["remove"]
        rep = R.run_plan(plan)
        assert rep["ok"], rep
        with open(os.path.join(mod, "archive", "pc", "mod", "x.archive"),
                  encoding="utf-8") as f:
            assert f.read() == "ORIGINAL"
        with open(os.path.join(mod, "r6", "scripts", "a.reds"),
                  encoding="utf-8") as f:
            assert f.read() == "ORIG"
        assert not os.path.exists(prod), "产物没删掉"

        # 包内多套一层顶层目录：匹配要按剥掉后的路径，提取还得用带前缀的原名
        os.remove(pkg)
        pkg2 = os.path.join(dl, "Test Mod-12345-1-0-1700000001.zip")
        with zipfile.ZipFile(pkg2, "w") as zf:
            zf.writestr("TestMod/archive/pc/mod/x.archive", "ORIGINAL2")
        with open(os.path.join(mod, "archive", "pc", "mod", "x.archive"),
                  "w", encoding="utf-8") as f:
            f.write("CHANGED2")
        plan2 = R.build_plan(mod, {"archive/pc/mod/x.archive": True}, dl)
        assert any(p["prefix"] == "TestMod" for p in plan2["packages"]), plan2["packages"]
        assert R.run_plan(plan2)["ok"], plan2
        with open(os.path.join(mod, "archive", "pc", "mod", "x.archive"),
                  encoding="utf-8") as f:
            assert f.read() == "ORIGINAL2"

        # FOMOD：装哪些文件由安装选项决定，包内路径对不上，不能用包
        os.remove(pkg2)
        pkg3 = os.path.join(dl, "Test Mod-12345-1-0-1700000002.zip")
        with zipfile.ZipFile(pkg3, "w") as zf:
            zf.writestr("fomod/ModuleConfig.xml", "<xml/>")
            zf.writestr("OptionA/archive/pc/mod/x.archive", "A")
        plan3 = R.build_plan(mod, {"archive/pc/mod/x.archive": True}, dl)
        assert plan3["fomod"] and not plan3["ok"], plan3

        # 没有版本一致的包时照用版本不符的包，但必须标 drift 并给警告
        os.remove(pkg3)
        pkg4 = os.path.join(dl, "Test Mod-12345-2-0-1700000003.zip")
        with zipfile.ZipFile(pkg4, "w") as zf:
            zf.writestr("archive/pc/mod/x.archive", "V2")
        plan4 = R.build_plan(mod, {"archive/pc/mod/x.archive": True}, dl)
        assert plan4["drift"] and plan4["source"] == "package", plan4
        assert plan4["warnings"], plan4

        # 白名单为空 = 这个模组没被本工具动过
        assert not R.build_plan(mod, {}, dl)["ok"]

        # 下载包缺失时退回本地快照：事务里存着「第一次写之前的原件」
        os.remove(pkg4)
        snap_mod = os.path.join(tmp, "Snapshot Mod 54321 1.0 2026-10-24T18-07Z XyZ")
        os.makedirs(os.path.join(snap_mod, "archive", "pc", "mod"))
        tfile = os.path.join(snap_mod, "archive", "pc", "mod", "y.archive")
        io.open(tfile, "w", encoding="utf-8").write("ORIGINAL")
        tx = undo_store.ApplyTransaction(snap_mod, "probe")
        tx.snapshot(tfile)
        io.open(tfile, "w", encoding="utf-8").write("CHANGED")
        tx.commit()
        files2, _f2, _n2 = undo_store.whitelist(snap_mod)
        snaps = undo_store.earliest_snapshots(snap_mod)
        plan5 = R.build_plan(snap_mod, files2, dl, snapshots=snaps)
        assert plan5["source"] == "snapshot" and plan5["usedSnapshot"], plan5
        assert R.run_plan(plan5)["ok"], plan5
        assert io.open(tfile, encoding="utf-8").read() == "ORIGINAL", "快照没还原回去"
        return True
    finally:
        if "snap_mod" in locals():
            _drop_tx(snap_mod)
        shutil.rmtree(tmp, ignore_errors=True)


def check_mixed_single_undo():
    """混合批次两层共用一个撤销事务：一次应用只留一条记录。"""
    root = os.path.dirname(HERE)
    with open(os.path.join(root, "src", "web_app.py"),
              "r", encoding="utf-8-sig") as f:
        src = f.read()
    assert "def _apply_resource(self, items, tx=None)" in src
    assert "def _do_apply_source(self, items, tx=None" in src
    assert "_apply_resource(res_items, tx=tx)" in src
    assert "_do_apply_source(src_items, tx=tx" in src
    # 事务必须在两层都跑完之后才提交，否则先提交的那层盖不住后写的文件
    seg = src[src.index("def _apply_mixed"):]
    seg = seg[:seg.index("def _do_apply_source")]
    assert seg.index("_do_apply_source(src_items, tx=tx") < seg.index("tx.commit()"), \
        "撤销记录的提交时机不对"
    # 外部传入的事务不能被下层丢弃
    assert "if own_tx and tx is not None:" in src

    # 还原接口存在，且用的是「事务记录白名单 + 下载包」
    assert "def revert_plan" in src and "def revert_mod" in src
    assert "undo_store.whitelist" in src
    with open(os.path.join(root, "src", "web", "app.js"),
              "r", encoding="utf-8-sig") as f:
        js = f.read()
    assert 'id="btn-revert"' in open(os.path.join(
        root, "src", "web", "index.html"), "r", encoding="utf-8-sig").read()
    assert "revert_plan" in js and "revert_mod" in js
    assert 'e.type === "reverted"' in js
    return True


def check_resource_outdir_and_undo():
    """资源模式产物写在 .archive 旁边，并且能撤销（记录 → 删除）。"""
    import tempfile
    import undo_store

    root = os.path.dirname(HERE)
    with open(os.path.join(root, "src", "web_app.py"),
              "r", encoding="utf-8-sig") as f:
        src = f.read()
    assert "def _resource_outdir" in src
    assert "def _archive_mod_root" in src
    assert "self.l10n_undo_root" in src
    assert "undo_store.ApplyTransaction(undo_root" in src
    # 产物路径要在 build 之前进快照，否则撤销只能删到生成后的文件，
    # 同名旧产物还原不回来。
    assert "tx.snapshot(os.path.join(outdir, out_name + suffix))" in src

    # _archive_mod_root 的推理：<mod>\archive\pc\mod\x.archive → <mod>
    fake = os.path.join(tempfile.gettempdir(), "mt-root-probe",
                        "archive", "pc", "mod", "x.archive")
    got = web_app.Api._archive_mod_root(fake)
    want = os.path.join(tempfile.gettempdir(), "mt-root-probe")
    assert os.path.normcase(got) == os.path.normcase(want), (got, want)
    # 没有 archive 层时退到文件所在目录
    flat = os.path.join(tempfile.gettempdir(), "mt-root-probe2", "x.archive")
    assert os.path.normcase(web_app.Api._archive_mod_root(flat)) == \
        os.path.normcase(os.path.dirname(flat))

    # 撤销真的会把这批新建文件删掉（新建 = 快照时不存在）。
    tmp = tempfile.mkdtemp(prefix="mt-undo-")
    try:
        tx = undo_store.ApplyTransaction(tmp, "probe")
        made = os.path.join(tmp, "archive", "pc", "mod", "m_zh-cn.archive")
        os.makedirs(os.path.dirname(made), exist_ok=True)
        tx.snapshot(made)                 # 写之前：不存在
        with open(made, "w", encoding="utf-8") as f:
            f.write("x")
        assert tx.commit(), "事务没提交成功"
        rep = undo_store.undo_last(tmp)
        assert rep.get("ok"), rep
        assert not os.path.exists(made), "撤销后产物仍然存在"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return True


def check_archive_open_dir():
    """资源行「打开文件所在目录」要定位到归档，而不是拿归档内部路径去拼。"""
    root = os.path.dirname(HERE)
    with open(os.path.join(root, "src", "web_app.py"),
              "r", encoding="utf-8-sig") as f:
        src = f.read()
    assert "def open_archive_dir" in src
    # 两条加载路径都要给资源行带上归档绝对路径
    assert src.count('r["archive"] = str(target.archive_path or "")') == 2
    with open(os.path.join(root, "src", "web", "app.js"),
              "r", encoding="utf-8-sig") as f:
        js = f.read()
    assert "open_archive_dir" in js
    assert "if (row && row.archive) pywebview.api.open_archive_dir(row.archive);" in js
    assert "if (r.archive) pywebview.api.open_archive_dir(r.archive);" in js
    # 资源行没有独立磁盘文件，它的「完整路径」就是归档本身
    assert 'if (row.layer === "resource") return row.archive || f;' in js
    return True


def check_locale_variant():
    """languages/ 目录 + 「语言-用途」变体 + 非标语言码都要认出来。

    CET 模组把设置界面放在 languages/en-us.json、短信正文放在
    languages/en-us-sms.json。以前 languages 不在目录白名单里，两段式的
    en-us-sms 也拆不出语言，整份短信库（几百条）被当无关文件跳过；
    jp-jp / kr-kr / ua-ua 这类写法同样认不出。目标路径还必须保住 -sms 尾巴，
    否则短信库会被写进设置界面那份文件里。
    """
    import locale_pack as LP
    tmp = tempfile.mkdtemp()
    try:
        d = os.path.join(tmp, "bin", "x64", "plugins", "cyber_engine_tweaks",
                         "mods", "Demo", "languages")
        os.makedirs(d)
        for name in ("en-us", "en-us-sms", "zh-cn", "zh-cn-sms", "jp-jp",
                     "kr-kr", "ua-ua", "th-th", "es-mx"):
            with open(os.path.join(d, name + ".json"), "w",
                      encoding="utf-8") as f:
                f.write('{"A": "Show Hints", "B": "Skip Gig Now"}')

        files = LP.detect(tmp, parse=False, target="zh-CN")
        got = {os.path.basename(f["rel"]): (f["locale"], f["variant"])
               for f in files}
        assert got.get("en-us.json") == ("en-US", ""), got
        assert got.get("en-us-sms.json") == ("en-US", "sms"), got
        assert got.get("zh-cn.json") == ("zh-CN", ""), got
        assert got.get("zh-cn-sms.json") == ("zh-CN", "sms"), got
        assert got.get("jp-jp.json", ("", ""))[0] == "ja-JP", got
        assert got.get("kr-kr.json", ("", ""))[0] == "ko-KR", got
        assert got.get("ua-ua.json", ("", ""))[0] == "uk-UA", got
        assert got.get("th-th.json", ("", ""))[0] == "th-TH", got
        assert got.get("es-mx.json", ("", ""))[0] == "es-MX", got

        srcs = LP.pick_sources(files, "zh-CN")
        assert len(srcs) == 2, [s["rel"] for s in srcs]
        tgts = {os.path.basename(LP.target_rel(s, "zh-CN")) for s in srcs}
        assert tgts == {"zh-cn.json", "zh-cn-sms.json"}, tgts

        rows, _meta = LP.extract(tmp, srcs, "zh-CN")
        assert len(rows) == 4, len(rows)
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_already_rows():
    """翻完再提取时，已经是中文的行不能整片消失。

    以前它们被 target_locale 过滤掉、只留在 skipped 计数里，用户再加载一次
    会发现行数凭空变少，也没法修「Dark Mods → 暗插件」这种别扭的旧译文。
    现在打开 keep_target 后它们以「已译」身份回来，译文就是当前值。
    """
    import mod_text_engine as E
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "init.lua")
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write('ImGui.Text("Hidden Gems")\n'
                    'ImGui.Button("Skip Gig", 150, 28)\n'
                    'ImGui.Text("秘宝")\n'
                    'ImGui.Button("跳过委托", 150, 28)\n')

        data, meta = E.extract_root_ex(tmp, target_locale="zh-CN")
        vals = [v for items in data.values() for (_l, _t, _c, v) in items]
        assert meta["skipped_target"] == 2, meta
        assert "秘宝" not in vals and "跳过委托" not in vals, vals
        assert not meta.get("already"), meta

        data2, meta2 = E.extract_root_ex(tmp, target_locale="zh-CN",
                                         keep_target=True)
        vals2 = [v for items in data2.values() for (_l, _t, _c, v) in items]
        assert "秘宝" not in vals2 and "跳过委托" not in vals2, vals2
        assert meta2["skipped_target"] == 2, meta2
        got = {v for items in (meta2.get("already") or {}).values()
               for (_l, _t, _c, v) in items}
        assert got == {"秘宝", "跳过委托"}, got
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_search_scoped():
    """搜索框要能查到译文，不只是原文。

    翻完一整片再回头核对某句时，手里只有中文——只搜 orig 等于搜不到。
    顺带守住英文界面：placeholder 的键必须有英文串，否则英文界面回落成中文。
    """
    import json
    root = os.path.dirname(HERE)
    with open(os.path.join(root, "src", "web", "app.js"),
              encoding="utf-8-sig") as f:
        js = f.read()
    start = js.index("function filtered()")
    end = js.find("\nfunction ", start + 1)
    body = js[start:] if end < 0 else js[start:end]
    assert "r.orig" in body, "搜索把原文弄丢了"
    assert "r.trans" in body, "搜索没有覆盖译文"
    assert "r.ctx" in body, "搜索丢了上下文"

    with open(os.path.join(root, "src", "web", "index.html"),
              encoding="utf-8-sig") as f:
        html = f.read()
    ph = "原文 / 译文 / 文件名"
    assert 'id="search"' in html, "搜索框不见了"
    assert 'data-i18n-ph="%s"' % ph in html, "搜索框提示没写译文"

    with open(os.path.join(root, "src", "locales", "en-US.json"),
              encoding="utf-8-sig") as f:
        en = json.load(f)["strings"]
    assert ph in en, "英文语言包缺这条提示: %s" % ph
    return True


def check_trans_guard():
    """机翻在跑时，换表格的入口必须被挡住；结算只认当初发出去的那批行。

    前端已经用全遮盖挡住操作，这里是最后一道：load_locale 会把 self.rows 整个
    换掉，正在跑的机翻按旧索引读新表，轻则 IndexError，重则写到别人的行上。
    """
    api = web_app.Api()
    api._busy = True
    try:
        got = api.load_locale([0], "zh-CN")
        assert got == {"ok": False, "error": "busy"}, ("load_locale 没挡", got)
        got = api.clear_rows()
        assert got == {"ok": False, "error": "busy"}, ("clear_rows 没挡", got)
    finally:
        api._busy = False

    # 结算用调用方快照：就算 self.rows 中途被换成更短的一份也不能越界
    snap = [{"trans": "甲"}, {"trans": ""}, {"trans": "丙"}]
    api.rows = [{"trans": ""}]
    rep = api._trans_report([0, 1, 2], rows=snap)
    assert rep["ok"] == 2 and rep["left"] == 1, rep
    assert rep["failed"] == [1], rep

    # 前端侧：遮罩里必须有进度和停止入口，停止不能再挂在「翻译全部未填」上
    root = os.path.dirname(HERE)
    with open(os.path.join(root, "src", "web", "index.html"),
              encoding="utf-8-sig") as f:
        html = f.read()
    for marker in ('id="btn-tr-stop"', 'id="ov-bar"', 'id="ov-count"',
                   'id="ov-progress"'):
        assert marker in html, marker
    with open(os.path.join(root, "src", "web", "app.js"),
              encoding="utf-8-sig") as f:
        js = f.read()
    for marker in ("function enterTransMask", "function exitTransMask",
                   'app.inert = true', 'app.inert = false',
                   "$(\"btn-tr-stop\").onclick", "function syncEditorEnabled",
                   "function followEditingRow"):
        assert marker in js, marker
    # 编辑时靠「编辑条聚焦 / 输入」自动把行带回视野，不再走右键
    assert 'closest(".editor")' not in js, "编辑条的右键定位又回来了"
    # 「翻译全部未填」不再兼任停止按钮
    assert 'state.busy) { pywebview.api.stop_translate()' not in js, \
        "停止入口还留在「翻译全部未填」上"
    return True


def check_clear_resync():
    """清空译文要能真的重翻，而不是被自己的旧数据挡住。

    两条独立的原因，缺一条都会让用户看到「清空了还是翻不了 / 翻出来还是那句」：
      1. 前端清空只改了浏览器内存，后端 self.rows 仍留着旧译文，于是
         translate() 把那行当「本来就有译文」跳过；
      2. 机翻结果同时写进了 TM，清空后 _baseline_fill 原样填回来。
    """
    import json
    import time
    import tm as TMod

    root = os.path.dirname(HERE)

    # ---- 记忆：只让机翻/记忆来源失效，手改与官方不动
    tmp = tempfile.mkdtemp()
    try:
        mem = TMod.Memory(os.path.join(tmp, "memory.json"))
        mem.put("Off", "简体中文", "关闭", origin="machine")
        mem.put("Warning", "简体中文", "警示语", origin="manual")
        assert mem.forget("Off", "简体中文") is True, "机翻记忆没删掉"
        assert mem.get_entry("Off", "简体中文") is None, "删了却还在"
        assert mem.forget("Warning", "简体中文") is False, "手改记忆被删了"
        assert mem.get_entry("Warning", "简体中文")["text"] == "警示语"
        assert mem.forget("不存在", "简体中文") is False

        # ---- 后端：清空指定行，同时让记忆失效
        api = web_app.Api()
        api._memory = mem
        api.settings["target_lang"] = "简体中文"
        # 上面已经删过 Off，这里换一条新的，专门验清空时顺带失效记忆
        mem.put("Street Cred", "简体中文", "街头声望", origin="machine")
        api.rows = [{"orig": "Street Cred", "trans": "街头声望",
                     "transSrc": "machine"},
                    {"orig": "Warning", "trans": "警示语", "transSrc": "manual"}]
        got = api.clear_row_translations([0])
        assert got.get("ok") is True, got
        assert got.get("forgot") == 1, got
        assert api.rows[0]["trans"] == "", api.rows[0]
        assert "transSrc" not in api.rows[0], "清空后还挂着来源色条"
        assert api.rows[1]["trans"] == "警示语", "清空波及了别的行"
        assert mem.get_entry("Street Cred", "简体中文") is None, "清空没让记忆失效"
        # 越界与脏输入不能炸
        assert api.clear_row_translations([99, "x"])["count"] == 0

        # ---- 前端：筛选、同步与遮罩三处都得在
        with open(os.path.join(root, "src", "web", "app.js"),
                  encoding="utf-8-sig") as f:
            js = f.read()
        assert '"原文=译文"' in js, "筛选没加「原文=译文」"
        start = js.index("function filtered()")
        body = js[start:js.find("\nfunction ", start + 1)]
        assert '"原文=译文"' in body, "筛选只是加进了列表，没进过滤逻辑"
        # 已译行（引擎判定本来就是目标语言）必须在筛选里被排除，
        # 否则自带中文的模组一筛就是整张表
        assert "if (r.already) return false;" in body, "「原文=译文」没排除已译行"
        for marker in ("function syncClearedRows", "clear_row_translations",
                       "await _transSync"):
            assert marker in js, marker
        # 清空入口两处都要走同步
        assert js.count("syncClearedRows(") >= 3, "有清空入口没同步后端"
        # 「继续翻译剩下的」也要进全遮罩，且不能再改「翻译全部未填」的文案
        seg = js[js.index("async function continueTranslate"):]
        seg = seg[:seg.find("\nasync function " ) if seg.find("\nasync function ") > 0
                  else len(seg)]
        assert "enterTransMask(" in seg, "补翻没有进遮罩"
        assert 'btn-tr-all").textContent' not in js, "还在改「翻译全部未填」的文案"
        # 右键「重新翻译」：单行与「已筛选」两个入口，以及交给后端的 force 标志
        assert 'data-act="retrans"' in js, "右键菜单没有「重新翻译」"
        assert 'data-act="retrans-filtered"' in js, "右键菜单没有「重新翻译已筛选」"
        assert "function retranslateRow" in js, "重新翻译没有处理器"
        assert "function retranslateFiltered" in js, "批量重翻没有处理器"
        assert "translateRows(uniq, true)" in js, "重新翻译没走 force 模式"
        assert "state.trForce = !!e.force;" in js, "补翻会丢掉 force 标志"
        # 菜单文案固定：筛选后的条数表格右上角已经有了，不必在菜单里再写一遍
        assert "重新翻译已筛选的行" in js, "菜单文案没改成固定文本"
        # 遮罩要在发请求之前：批量重翻点下去就得有反应，不能等后端回话
        pre = js[js.index("async function translateRows"):]
        pre = pre[:pre.index("pywebview.api.translate")]
        assert "enterTransMask(" in pre, "发翻译请求之前没有上遮罩"

        # ---- force 重翻：已有译文的行也要真的再问一次模型
        import types
        import baseline as Lbase

        tmp2 = tempfile.mkdtemp()
        try:
            api2 = web_app.Api()
            api2.settings["target_lang"] = "简体中文"
            api2.settings["api_base"] = "http://127.0.0.1:1/v1"
            api2.settings["local_model_enabled"] = True
            api2.rows = [{"orig": "Off", "line": 1, "file": "a.reds",
                          "trans": "旧译"}]
            api2._memory = TMod.Memory(os.path.join(tmp2, "memory.json"))

            class _Win:
                def evaluate_js(self, script):
                    pass

            class _Gloss:
                def relevant(self, texts):
                    return []

                def prompt_block(self, terms):
                    return ""

            api2._window = _Win()
            api2._glossary = _Gloss()

            calls = {"n": 0}

            def fake_batch(base, model, texts, target=None, glossary=""):
                calls["n"] += 1
                return ["新译"] * len(texts)

            fake_mt = types.ModuleType("mt_client")
            fake_mt.resolve_model = lambda *a, **k: "fake"
            fake_mt.translate_batch = fake_batch
            fake_mt.warmup = lambda *a, **k: (False, "fake")
            old_mt = sys.modules.get("mt_client")
            sys.modules["mt_client"] = fake_mt
            old_load = Lbase.load_official
            Lbase.load_official = lambda enabled, lang: (None, "")
            try:
                got = api2.translate([0], [], force=True)
                assert got.get("ok"), got
                for _ in range(300):
                    if not api2._busy:
                        break
                    time.sleep(0.02)
                assert not api2._busy, "重翻线程没有结束"
                assert calls["n"] == 1, "「重新翻译」没有真的发给模型"
                assert api2.rows[0]["trans"] == "新译", api2.rows

                # 模型返回空：旧译文必须原样留着（不清空才有的好处），
                # 且账目要按「本轮真的写回了哪些行」算
                api2.rows = [{"orig": "Off", "line": 1, "file": "a.reds",
                              "trans": "旧译"}]
                fake_mt.translate_batch = lambda *a, **k: [""]
                got = api2.translate([0], [], force=True)
                assert got.get("ok"), got
                for _ in range(300):
                    if not api2._busy:
                        break
                    time.sleep(0.02)
                assert not api2._busy, "重翻线程没有结束"
                assert api2.rows[0]["trans"] == "旧译", "失败的重翻把旧译文弄丢了"

                rep = api2._trans_report([0], force=True, written=set(),
                                         rows=api2.rows)
                assert rep["ok"] == 0 and rep["left"] == 1, rep
                assert rep["force"] is True, rep
                rep2 = api2._trans_report([0], force=True, written={0},
                                          rows=[{"trans": "新译"}])
                assert rep2["ok"] == 1 and rep2["left"] == 0, rep2
                # 非 force 仍按 trans 非空结算（基线命中也算翻好了）
                rep3 = api2._trans_report([0], rows=[{"trans": "官方给的"}])
                assert rep3["ok"] == 1 and rep3["force"] is False, rep3
            finally:
                Lbase.load_official = old_load
                if old_mt is None:
                    sys.modules.pop("mt_client", None)
                else:
                    sys.modules["mt_client"] = old_mt
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

        with open(os.path.join(root, "src", "locales", "en-US.json"),
                  encoding="utf-8-sig") as f:
            en = json.load(f)["strings"]
        assert "原文=译文" in en, "英文语言包缺这条筛选"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return True


def check_reds_evidence_tiers():
    """没有位置证据的 REDscript 字符串不再一律当界面文本。

    · 赋值目标是 title/message 这类名字 -> 显示名称/界面文本
    · 赋值目标是 m_issue / m_error, 或出现在 Log/Print 调用里 -> 内部文本,
      默认不收, 打开 trace 才收
    · 其余 -> 长句文本(受 loose 开关控制)
    """
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "logic.reds")
        src = (
            'module Test\n'
            'public class Logic {\n'
            '  private let m_issue: String;\n'
            '  public func Run() -> Void {\n'
            '    this.m_issue = "native cycle changed outside the selected run";\n'
            '    LogChannel("[Test] reactor idle");\n'
            '    let card: SimpleScreenMessage;\n'
            '    card.message = "A witness called NCPD.";\n'
            '    let rows: array<String>;\n'
            '    ArrayPush(rows, "Militech Ajax Raptor Common");\n'
            '  }\n'
            '}\n'
        )
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(src)

        strict = [(t, v) for _l, t, _c, v in E.extract_from_file(p)]
        vals = [v for _t, v in strict]
        assert ("界面文本", "A witness called NCPD.") in strict, strict
        assert ("长句文本", "Militech Ajax Raptor Common") in strict, strict
        assert "native cycle changed outside the selected run" not in vals, strict
        assert "[Test] reactor idle" not in vals, strict

        # 关掉长句兜底后, 只剩有位置证据的界面文本
        only_ui = [v for _l, t, _c, v in E.extract_from_file(p, loose=False)]
        assert only_ui == ["A witness called NCPD."], only_ui

        # 打开 trace 才把内部状态/日志一起收进来
        traced = [v for _l, _t, _c, v in E.extract_from_file(p, trace=True)]
        assert "native cycle changed outside the selected run" in traced, traced
        assert "[Test] reactor idle" in traced, traced
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_layered_extraction():
    """语言包 + 源码/设置文本必须同时出现在一张表里。

    曾经的问题: 命中 Provider 语言包就直接 return, CET .lua 的设置文本
    (addSwitch / addSelectorString) 一条都不显示。
    """
    root = find_mod("Much Better Contracts")
    if not root:
        return "skip"
    # 这个 staging 目录随时可能被「应用到模组」翻过（主文件已是中文），
    # 拿它当测试输入等于在测"已翻译产物"。复制一份、用工具留下的 .bak
    # 还原成翻译前状态，测试结果就不再随本机的翻译进度漂移。
    import tempfile
    tmp = tempfile.mkdtemp(prefix="mbc_")
    try:
        return _check_layered_extraction_in(root, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _restore_from_bak(root, suffixes=None):
    """把 dir 下的 foo.bak 覆盖回 foo，返回覆盖数量。

    suffixes 限定只还原这些扩展名（None = 全部）。CET 的 .lua 一旦被翻成
    中文，locale 探测会把它当成 zh-CN 语言包而整份排除，源码层就看不到它了；
    只还原 .lua、保留 .reds 的已翻译状态，provider 配对才仍然成立。
    """
    n = 0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if not name.endswith(".bak"):
                continue
            dst = os.path.join(dirpath, name[:-4])
            if not os.path.isfile(dst):
                continue
            if suffixes and not dst.lower().endswith(tuple(suffixes)):
                continue
            shutil.copy2(os.path.join(dirpath, name), dst)
            n += 1
    return n


def _restore_from_zip(work, suffixes=None):
    """用下载包里的原文件覆盖回测试副本，返回覆盖数量。

    比 `.bak` 可靠：v2.43.3 起批量不再留备份，`.bak` 在这台机器上已经没有了，
    而下载包一直在。测试要的是「翻译前」的输入，不能用当前 staging 的样子。
    """
    import zipfile
    import appconfig

    name = os.path.basename(str(work).rstrip("\\/"))
    try:
        dl = (appconfig.load_settings().get("downloads_dir") or "").strip()
    except Exception:
        dl = ""
    zp = os.path.join(dl, name + ".zip") if dl else ""
    if not zp or not os.path.isfile(zp):
        return 0
    n = 0
    try:
        with zipfile.ZipFile(zp) as z:
            for member in z.namelist():
                if member.endswith("/"):
                    continue
                if suffixes and not member.lower().endswith(tuple(suffixes)):
                    continue
                dst = os.path.join(work, member.replace("/", os.sep))
                if not os.path.isfile(dst):
                    continue
                with z.open(member) as src, open(dst, "wb") as out:
                    out.write(src.read())
                n += 1
    except Exception:
        return n
    return n


def _check_layered_extraction_in(root, tmp):
    work = os.path.join(tmp, os.path.basename(str(root).rstrip("\\/")))
    shutil.copytree(root, work)
    # 先试下载包（一定还在），再退回 .bak（v2.43.3 之后基本没有了）
    if not _restore_from_zip(work, (".lua",)):
        _restore_from_bak(work, (".lua",))

    api = web_app.Api()
    r = api._do_load(work, {"localeFirst": True, "providerPair": True,
                            "lua": True, "loose": True, "locale": False,
                            "trace": False})
    assert r.get("ok"), r
    assert r.get("mode") == "provider", r.get("mode")
    assert r.get("localeRows", 0) > 0, r.get("localeRows")
    assert r.get("sourceRows", 0) > 0, r.get("sourceRows")
    rows = r["rows"]
    layers = {x.get("layer") for x in rows}
    assert layers == {"locale", "source"}, layers

    # 设置界面文本: 必须被抽出来, 而且类型要对。
    # 值不再死抠英文原文: 这个 staging 目录已经被批量翻成中文, 而下载包是
    # .rar 且版本不同（2.2 装的 / 2.3.1 下的）, 没有可靠的「翻译前」原件。
    # 断言「在不在」才是这条回归的本意（以前命中 Provider 就整份 return,
    # CET 的设置文本一条都不显示）。
    lua_rows = [x for x in rows if x["file"].endswith("nativeSettingsUI.lua")]
    assert lua_rows, "CET 设置文本一条都没有"
    types = {x["type"] for x in lua_rows}
    assert {"显示名称", "描述"} <= types, types
    assert len(lua_rows) >= 8, len(lua_rows)
    assert all(str(x.get("orig") or "").strip() for x in lua_rows)

    # 内部诊断字符串默认不应出现
    origs = {x["orig"] for x in rows}
    assert "native cycle changed outside the selected run" not in origs
    assert "all targets neutralized cycle=" not in origs

    # 语言包文件不能被源码层重复提取: 同一个文件只应出现在一层里
    by_file = {}
    for x in rows:
        by_file.setdefault(x["file"], set()).add(x.get("layer"))
    both = [f for f, ls in by_file.items() if len(ls) > 1]
    assert not both, both
    return True


def check_locale_pair_by_key():
    """语言包回填已有译文必须按条目名对齐, 不能按「第几条」。

    Appearance Menu Mod 2.15.1 是活例子: en_US.lua 与 zh_CN.lua 各有 974 条
    赋值, 但中文里含 `/`(启用/禁用)的句子被 _looks_like_path 当成路径丢掉,
    单英文词(Close/Save)又被 is_translatable 当成标识符丢掉, 两边「可翻译」
    条目数变成 674 / 963。旧代码用 existing[idx] 回填, 从第一条被丢的文本起
    整体错位 —— 表格里原文 "Default Aperture" 挂着译文
    "Failed to load pose: animation %s not found."。
    """
    tmp = tempfile.mkdtemp()
    try:
        loc = os.path.join(tmp, "Localization")
        os.makedirs(loc)
        en = ('return {\n'
              '\tButton_Save = "Save",\n'
              '\tButton_Close = "Close",\n'
              '\tWarn_AnimNotFound = "Failed to load pose: animation %s not found.",\n'
              '\tSlider_DefaultAperture = "Default Aperture",\n'
              '\tOnly_In_English = "Only In English",\n'
              '\tCheckbox_Dual = "Enable/disable NPC appearance",\n'
              '}\n')
        # 顺序打乱 + 缺一条 Warn_AnimNotFound + 含 `/` 的中文句子
        zh = ('return {\n'
              '\tSlider_DefaultAperture = "默认的相机孔径",\n'
              '\tButton_Close = "关闭",\n'
              '\tButton_Save = "保存",\n'
              '\tCheckbox_Dual = "启用/禁用 NPC 外观",\n'
              '\tOnly_In_English = "只出现在英文里",\n'
              '}\n')
        for name, text in (("en_US.lua", en), ("zh_CN.lua", zh)):
            with open(os.path.join(loc, name), "w", encoding="utf-8") as f:
                f.write(text)

        target = locale_pack.target_code("简体中文")
        files = locale_pack.detect(tmp, parse=True, target=target)
        srcs = locale_pack.pick_sources(files, target)
        assert len(srcs) == 1, [s.get("rel") for s in srcs]
        rows, _meta = locale_pack.extract(tmp, srcs, target)
        got = {r["ctx"]: r["trans"] for r in rows}
        want = {
            "Button_Save": "保存",
            "Button_Close": "关闭",
            "Slider_DefaultAperture": "默认的相机孔径",
            "Checkbox_Dual": "启用/禁用 NPC 外观",
            "Only_In_English": "只出现在英文里",
            # 中文包里缺这个条目: 宁可留空, 也不能借邻行的译文
            "Warn_AnimNotFound": "",
        }
        bad = {k: (got.get(k), want.get(k)) for k in want if got.get(k) != want.get(k)}
        assert not bad, bad
        # 单个英文单词的按钮 (Close/Save) 必须收进原文列
        assert "Button_Close" in got and "Button_Save" in got, sorted(got)
        # 含 `/` 的中文句子不能被当成资源路径丢掉
        assert got["Checkbox_Dual"] == "启用/禁用 NPC 外观"

        # ---- 写回: 必须按条目名写进目标文件, 不能沿用源文件的行号。
        # 这一步是真正会改坏磁盘的路径: 之前用 en 的行号写 zh, 把 A 的译文
        # 写进 B, 并报「未找到对应原文」。
        zh_path = os.path.join(loc, "zh_CN.lua")
        feed = [(r["ctx"], r["orig"], r["trans"]) for r in rows if r["trans"]]
        assert len(feed) == 5, len(feed)
        n, bad = locale_pack.apply_text_target(zh_path, feed)
        assert n == 5, (n, bad)
        assert not bad, bad
        with open(zh_path, encoding="utf-8") as f:
            after = f.read()
        assert 'Button_Save = "保存"' in after, after
        assert 'Button_Close = "关闭"' in after, after
        assert 'Slider_DefaultAperture = "默认的相机孔径"' in after, after
        assert 'Checkbox_Dual = "启用/禁用 NPC 外观"' in after, after
        assert 'Only_In_English = "只出现在英文里"' in after, after
        # 缺条目必须报失败, 而不是写到邻行
        n2, bad2 = locale_pack.apply_text_target(
            zh_path, [("No_Such_Key", "Nope", "不存在")])
        assert n2 == 0, n2
        assert bad2 and "没有这个条目" in bad2[0][2], bad2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_apply_ctx_backfill():
    """前端 apply 不带条目名时, 后端必须从当前表格反查补上。

    语言包写回靠 ctx 里的 `Name =` 定位。前端 items 只传 key(那是 JSON 的键),
    lua/reds 行的 key 是空的; 缺了 ctx 就只能退回按行号写, 而源文件与目标文件
    行号不同 —— AMM 的 BeginCombo_LookAt 就是这样拿到 BeginItem_TabTools 的
    译文。这条链路以前没有测试, 所以「后端逻辑对、前端没传参」能一路漏到用户。
    """
    tmp = tempfile.mkdtemp()
    try:
        loc = os.path.join(tmp, "Localization")
        os.makedirs(loc)
        # 目标文件比源文件少一条、行号也整体前移, 正是 AMM 的形态
        en = ('return {\n'
              '\tAAA_Save = "Save",\n'
              '\tBBB_Close = "Close",\n'
              '\tCCC_Third = "Third Item",\n'
              '\tDDD_Fourth = "Fourth Item",\n'
              '}\n')
        zh = ('return {\n'
              '\tAAA_Save = "保存",\n'
              '\tBBB_Close = "关闭",\n'
              '\tCCC_Third = "第三项",\n'
              '}\n')
        for name, text in (("en_US.lua", en), ("zh_CN.lua", zh)):
            with open(os.path.join(loc, name), "w", encoding="utf-8") as f:
                f.write(text)

        target = locale_pack.target_code("简体中文")
        files = locale_pack.detect(tmp, parse=True, target=target)
        srcs = locale_pack.pick_sources(files, target)
        rows, _meta = locale_pack.extract(tmp, srcs, target)
        assert len(rows) == 4, [r["orig"] for r in rows]
        assert all(r.get("ctx") for r in rows), [r.get("ctx") for r in rows]

        for r in rows:
            if not r["trans"]:
                r["trans"] = "第四项"
        api = web_app.Api("")
        api.settings["target_lang"] = "简体中文"
        api.rows = rows
        # 前端就是这样提交的: 没有 ctx
        items = [{"root": tmp, "file": r["file"], "line": r["line"],
                  "orig": r["orig"], "trans": r["trans"],
                  "target": r["target"], "kind": "text"} for r in rows]
        assert all("ctx" not in it for it in items)

        res = api._do_apply(items)
        assert res["ok"] == 3, res
        assert res["fail"] == 1, res
        assert "没有这个条目" in res["failed"][0]["reason"], res["failed"]

        with open(os.path.join(loc, "zh_CN.lua"), encoding="utf-8") as f:
            after = f.read()
        assert 'AAA_Save = "保存"' in after, after
        assert 'BBB_Close = "关闭"' in after, after
        assert 'CCC_Third = "第三项"' in after, after
        # 缺条目不能写到邻行去
        assert "第四项" not in after, after
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_lua_lang_sections():
    """一个 lua 文件里按语言键塞了多套语言时, 只取源语言段(+目标语言段)。

    QuickhackPanel 把 20 种语言放进同一个 texts 表: `["en-us"] = {...}` 一直到
    `["th-th"] = {...}`。拉丁字母写成的德语、法语、西语段在字符层面和英文无法
    区分, 以前会被整段当成待翻原文 —— 一个文件就多出 48 条外语长句, 点「翻译
    全部」再应用会把模组的多语言支持整段写成中文。
    """
    text = (
        'local texts = {\n'
        '    ["en-us"] = {\n'
        '        tab = "Quickhack Panel",\n'
        '        desc = "How quickhacks are listed in the panel.",\n'
        '    },\n'
        '    ["de-de"] = {\n'
        '        desc = "Reihenfolge der Quickhacks im Menue.",\n'
        '    },\n'
        '    ["fr-fr"] = {\n'
        '        desc = "Ordre des piratages rapides dans le panneau.",\n'
        '    },\n'
        '    ["zh-cn"] = {\n'
        '        desc = "面板中快速破解的排列顺序。",\n'
        '    },\n'
        '}\n'
        'local after = "Outside Section Text Here"\n')
    noise = {}
    items = E._extract_lua(text, loose=True, noise=noise, target_locale="zh-CN")
    origs = [it[3] for it in items]
    # 源语言段照常提取
    assert "How quickhacks are listed in the panel." in origs, origs
    # 德语 / 法语段必须整段丢掉
    assert not any("Reihenfolge" in o for o in origs), origs
    assert not any("Ordre des" in o for o in origs), origs
    # 段外的普通代码不受影响
    assert "Outside Section Text Here" in origs, origs
    assert noise.get(NOISE.FAMILY_LANG, 0) >= 2, noise
    # 只有一套语言时不能触发(避免误伤普通表)
    single = 'local t = {\n    ["en-us"] = {\n        desc = "Only One Language Here",\n    },\n}\n'
    items2 = E._extract_lua(single, loose=True, target_locale="zh-CN")
    assert "Only One Language Here" in [it[3] for it in items2], items2


def check_prop_loc_key():
    """@runtimeProperty 第二个参数若已经是查表键, 就不能再当文案翻。

    回归背景: Nearby Auto Loot 的
        @runtimeProperty("ModSettings.displayName", "NearbyLoot-Enabled")
    这类 key 只有两段, 达不到 is_loc_key 的「三段」门槛, 于是一个 20 条的
    .reds 里 13 条都是查表键。翻掉再写回就把 key 改成了中文, 归档里 zh-cn
    的条目再也查不到 —— 界面上因为 Codeware 查不到会回退显示 key 本身,
    看起来反而「正常」, 是最不容易发现的一类损坏。
    """
    src = "\n".join([
        'public class S extends ScriptableSystem {',
        '    @runtimeProperty("ModSettings.mod", "Nearby Auto Loot")',
        '    @runtimeProperty("ModSettings.displayName", "NearbyLoot-Enabled")',
        '    public let a: Bool = true;',
        '    @runtimeProperty("ModSettings.displayName", "NearbyLoot-GeneralCategory")',
        '    @runtimeProperty("ModSettings.category", "NearbyLoot-QuestCategory")',
        '    @runtimeProperty("ModSettings.category.order", "1")',
        '    public let b: Bool = true;',
        '}',
    ])
    noise = {}
    rows = E._extract_reds(src, ".reds", target_locale="zh-CN", stats={},
                           loose=True, trace=False, noise=noise)
    origs = [r[3] for r in rows]
    for key in ("NearbyLoot-Enabled", "NearbyLoot-GeneralCategory",
                "NearbyLoot-QuestCategory"):
        assert key not in origs, (key, origs)
    # 带空格的是真名字, 照旧可翻 (模组名的回退文案)
    assert "Nearby Auto Loot" in origs, origs
    # 数值属性根本不进表
    assert "1" not in origs, origs
    assert noise.get(NOISE.FAMILY_LOCKEY, 0) >= 3, noise
    # 形态判据: 只认「无空格 + 段内大写拐点或三段以上」
    for ok in ("Nearby Auto Loot", "Auto-Drive", "1.0", "Show Dev Option"):
        assert not E.is_prop_loc_key(ok), ok
    for bad in ("NearbyLoot-Enabled", "NearbyLoot-GeneralCategory",
                "UI-Labels-Resume", "Gameplay-Guard-SwitchSeats"):
        assert E.is_prop_loc_key(bad), bad


def check_pair_existing():
    """归档自己带目标语言时, 作者写好的译文要当基线, 不能被机翻覆盖。

    回归背景: Nearby Auto Loot 自带 19 种语言, zh-cn 14 条全是作者的中文。
    资源层以前把目标语言文件整批丢掉, 于是 14 行全被当成「还没翻」, 一键
    机翻会把「拾取容器物品」换成「战利品容器」这种更差的译法。
    """
    from localization import pipeline as Lpipe

    EN = "base\\localization\\en-us\\onscreens\\x.json"
    ZH = "base\\localization\\zh-cn\\onscreens\\x.json"
    DE = "base\\localization\\de-de\\onscreens\\x.json"
    rows = [
        {"file": EN, "lang": "en-us", "entryId": "K1", "orig": "Loot containers"},
        {"file": ZH, "lang": "zh-cn", "entryId": "K1", "orig": "拾取容器物品"},
        {"file": DE, "lang": "de-de", "entryId": "K1", "orig": "Behälter"},
        # 作者把英文原样抄进 zh-cn 当占位: 不能当成已译
        {"file": EN, "lang": "en-us", "entryId": "K2", "orig": "Quest loot"},
        {"file": ZH, "lang": "zh-cn", "entryId": "K2", "orig": "Quest loot"},
        # zh-cn 里根本没有这一条
        {"file": EN, "lang": "en-us", "entryId": "K3", "orig": "Radius (m)"},
        # 只有 en-us 与 zh-cn 的路径差异, 条目标识对得上
        {"file": EN, "lang": "en-us", "entryId": "K4", "orig": "Enable nearby pickup"},
        {"file": ZH, "lang": "zh-cn", "entryId": "K4", "orig": "启用附近拾取"},
    ]
    out = Lpipe.pair_existing(rows, "zh-cn", "zh-CN", "en-us")
    got = {(r["entryId"], r["lang"]): r for r in out}
    assert got[("K1", "en-us")].get("trans") == "拾取容器物品", got[("K1", "en-us")]
    assert got[("K1", "en-us")].get("already") == 1
    assert got[("K4", "en-us")].get("trans") == "启用附近拾取"
    # 占位英文 / 缺条目: 保持待翻
    assert not got[("K2", "en-us")].get("trans")
    assert not got[("K3", "en-us")].get("trans")
    # 其它语言的行不许被写成中文 (它们不是原文)
    assert not got[("K1", "de-de")].get("trans"), got[("K1", "de-de")]
    # 传入的 row 不能被就地改动: sess.rows 还要用来回写定位
    assert all(not r.get("trans") for r in rows)
    # 路径里的语言段被抹成 * 后两侧一致
    assert Lpipe.strip_lang(EN) == Lpipe.strip_lang(ZH) == Lpipe.strip_lang(DE)


def check_identifier_guard():
    """标识符 / 本地化键守卫。

    @runtimeProperty 的值只有在「纯显示位」才是文案。同一个串若还出现在
    CName(n"") / TweakDBID(t"") / ResRef(r"") / StringToName() 里，
    翻掉显示位就会让两边脱钩：查找永远落空、设置项联动静默失效，而界面上
    看起来一切正常。所以这类串整条放弃，并在报告里记一笔。
    """
    src = "\n".join([
        'module Guard',
        'public class Settings extends ScriptableSystem {',
        '    @runtimeProperty("ModSettings.mod", "Auto Drive Enhanced")',
        '    @runtimeProperty("ModSettings.displayName", "UI-Guard-Settings-a-label")',
        '    public let a: Bool = true;',
        '    @runtimeProperty("ModSettings.displayName", "Show Dev Option")',
        '    public let dev: Bool = false;',
        '    private let nm: CName = n"Auto Drive Enhanced";',
        '    private let key: CName = n"someRecord";',
        '    private func F() -> Void { StringToName("lookupKey") }',
        '}',
    ])
    noise = {}
    rows = E._extract_reds(src, ".reds", target_locale="zh-CN", stats={},
                           loose=True, trace=False, noise=noise)
    origs = [r[3] for r in rows]
    # 被 n"..." 引用过 —— 翻它会打断 GetCategories(n"Auto Drive Enhanced")
    assert "Auto Drive Enhanced" not in origs, origs
    # 本地化查表键 —— 译文在资源层归档里，源码层不该当文案翻
    assert "UI-Guard-Settings-a-label" not in origs, origs
    # 真文案照收
    assert "Show Dev Option" in origs, origs
    assert noise.get(NOISE.FAMILY_IDENT, 0) >= 1, noise
    assert noise.get(NOISE.FAMILY_LOCKEY, 0) >= 1, noise
    # 形态判据不能太贪: 带空格/标点的普通文案一律不算 key
    for ok in ("Max Speed Control", "Disable Auto drive MOD",
               "Show Dev Option (Don't touch, dev only)", "[For Dev]"):
        assert not E.is_loc_key(ok), ok
    for bad in ("UI-Guard-Settings-a-label", "Gameplay-Guard-SwitchSeats",
                "UI-Guard-Settings-drivingAI.Vanilla-label"):
        assert E.is_loc_key(bad), bad


def check_resource_layer():
    """归档内嵌的 localization 必须成为第三层。

    .reds 里只写 key、真文案躺在 .archive 里的模组（AutoDrive Enhanced 这类），
    只看明文文件会把整块界面文本漏掉，或者更糟：把 key 当文案翻掉。
    """
    root = find_mod("auto_drive_enhanced")
    if not root:
        return "skip"
    api = web_app.Api()
    r = api._do_load(root, {"localeFirst": True, "lua": True, "loose": True})
    assert r.get("ok"), r
    # 走的是源码模式入口，但资源层被并进来了
    assert r.get("mode") in ("content", "locale", "provider"), r.get("mode")
    n = int(r.get("resourceRows") or 0)
    info = (r.get("meta") or {}).get("resource") or {}
    assert n > 0, (n, info)
    assert info.get("archive"), info
    assert "en-us" in (info.get("languages") or {}), info
    res = [x for x in r["rows"] if x.get("layer") == "resource"]
    assert len(res) == n, (len(res), n)
    assert all(x.get("kind") == "resource" for x in res)
    assert all(str(x.get("entryId") or "") for x in res)
    assert all(x.get("file") for x in res)
    # 归档语料要能吃官方基线/记忆填充
    assert all("trans" in x for x in res)
    # 会话要在，否则「应用」时资源行组不了包
    assert api.l10n_session is not None
    return True


def check_resource_entry_id():
    """归档行提交必须带 entryId。

    回归背景: applyAll() 拼 items 时漏了 entryId，归档行按 (file, entryId)
    一条都对不上，点「应用」整批失败并报「没有可提交的译文」——用户看到的
    现象却是「.archive 读取之后应用失败」。前端已补齐字段，后端也留了按
    原文反查的兜底，这里把两边钉住。
    """
    rows = [
        {"file": "m\\a.json", "entryId": "K1", "orig": "Same"},
        {"file": "m\\a.json", "entryId": "K2", "orig": "Same"},  # 同文两条，不许猜
        {"file": "m\\a.json", "entryId": "K3", "orig": "Only"},
    ]
    items = [
        {"file": "m\\a.json", "orig": "Only", "trans": "甲"},    # 唯一命中，补上
        {"file": "m\\a.json", "orig": "Same", "trans": "乙"},    # 二义，补不上
    ]
    left = web_app.fill_resource_entry_ids(items, rows)
    assert items[0].get("entryId") == "K3", items
    assert not items[1].get("entryId"), items
    assert left == 1, left

    # 已经带主键的不许被覆盖
    items2 = [{"file": "m\\a.json", "entryId": "K1", "orig": "Same", "trans": "乙"}]
    assert web_app.fill_resource_entry_ids(items2, rows) == 0
    assert items2[0]["entryId"] == "K1"

    # 前端拼 items 的地方必须原样带上 entryId（防止再次漏字段）
    js_path = os.path.join(os.path.dirname(HERE), "src", "web", "app.js")
    with open(js_path, "r", encoding="utf-8") as f:
        js = f.read()
    assert "entryId: r.entryId" in js, "applyAll 构造 items 时漏了 entryId"
    return True


def check_apply_dispatch():
    """「应用」要按行的来源分流：归档行走组包，明文行走回写。

    一个模组可能两层都有。以前只要批次里出现一行资源文本，整批就都送给
    归档组包器，明文那半边的译文被静默丢掉。混合批次还必须共用同一个撤销
    事务——两层各建各的会留下两条记录，撤销要按两次。
    """
    api = web_app.Api()
    tmp = tempfile.mkdtemp(prefix="mt-dispatch-")
    tx_ids = []
    try:
        os.makedirs(os.path.join(tmp, "archive", "pc", "mod"))
        os.makedirs(os.path.join(tmp, "r6", "scripts"))
        calls = []
        seen = []

        def fake_res(items, tx=None, **kw):
            calls.append(("res", len(items)))
            seen.append(tx)
            if tx is not None:
                tx.snapshot(os.path.join(tmp, "archive", "pc", "mod", "a.archive"))
            return {"ok": len(items), "fail": 0, "errors": [], "canInstall": True,
                    "archive": "a.archive", "xl": "a.xl"}

        def fake_src(items, tx=None, **kw):
            calls.append(("src", len(items)))
            seen.append(tx)
            if tx is not None:
                tx.snapshot(os.path.join(tmp, "r6", "scripts", "a.reds"))
            return {"ok": len(items), "fail": 0, "errors": [], "undoId": "u1",
                    "undoAvailable": True, "failed": [], "failedTotal": 0}

        api._apply_resource = fake_res
        api._do_apply_source = fake_src

        r = api._do_apply([
            {"kind": "resource", "file": "x.json", "entryId": "1", "trans": "甲",
             "root": tmp},
            {"file": "a.reds", "line": 1, "orig": "Hello", "trans": "你好",
             "root": tmp},
        ])
        assert calls == [("res", 1), ("src", 1)], calls
        assert seen[0] is not None and seen[0] is seen[1], "两层没有共用一个撤销事务"
        assert r["mode"] == "mixed", r
        assert r["ok"] == 2 and r["fail"] == 0, r
        assert r["undoAvailable"] is True, r
        assert r["layers"]["resource"]["ok"] == 1, r["layers"]
        assert r["layers"]["source"]["ok"] == 1, r["layers"]

        calls.clear()
        api._do_apply([{"kind": "resource", "file": "x.json", "entryId": "1"}])
        assert calls == [("res", 1)], calls
        calls.clear()
        api._do_apply([{"file": "a.reds", "line": 1, "orig": "Hi"}])
        assert calls == [("src", 1)], calls
        tx_ids = [t.id for t in seen if t is not None]
    finally:
        # 自测建的事务不能留在用户数据里
        _drop_tx(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def check_noise_rules():
    """「误报」过滤: 硬噪音任何档位都不收, 软噪音只在长句兜底档丢。

    这些规则对应 v2.23 的精简误报: 图集转储 / 记录名 / 技术串 / 外文表 /
    双语表 key。任何一条失守都会让表格重新长出成百上千行垃圾。
    """
    # ---- 硬噪音: 直接不进表 ----
    for bad in ("kb_a:64:64:0|kb_b:32:32:1|kb_c:16:16:0",
                "ORDER BY entity_name ASC",
                "Items.tony_wet_t-shirt_red",
                "GarageCore.Economy.EconomyBridge::IsGarageBanned;String",
                "walk dist=%.1f busy=%.1f leashed=%d",
                "{drop_point}"):
        assert not E.is_translatable(bad), bad

    # ---- 真文本不能被误杀 ----
    for good in ("Select Category",
                 "Create & Organize Folders",
                 "YOUR EDDIES:",
                 "% chance of reinforcements being called",
                 "Cyberpunk 2077"):
        assert E.is_translatable(good) or E.is_label_text(good), good

    tmp = tempfile.mkdtemp()
    try:
        # ---- 外文表: 长句兜底档丢, 调试文本档留 ----
        p = os.path.join(tmp, "lines.reds")
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write('func Text() -> Void {\n'
                    '  let s: String = "[en_us][pl_pl]Tego właśnie chciałaś?";\n'
                    '  let t: String = "Żałosne, naprawdę nie wierzę w to wszystko.";\n'
                    '  let u: String = "Turnin\' your back on the problem, again?!";\n'
                    '}\n')
        vals = [v for _l, _t, _c, v in E.extract_from_file(p)]
        assert "Turnin' your back on the problem, again?!" in vals, vals
        assert not [v for v in vals if "pl_pl" in v or "Żałosne" in v], vals
        traced = [v for _l, _t, _c, v in E.extract_from_file(p, trace=True)]
        assert [v for v in traced if "Żałosne" in v], traced

        # ---- 一文件多语言: 只取源语言那一段 ----
        p2 = os.path.join(tmp, "multi.reds")
        with open(p2, "w", encoding="utf-8", newline="") as f:
            f.write('func Sub_EN_US() -> String {\n'
                    '  return "Sheesh. Give it a rest?";\n'
                    '}\n'
                    'func Sub_RU_RU() -> String {\n'
                    '  return "Да ладно, хватит уже.";\n'
                    '}\n')
        got = [v for _l, _t, _c, v in E.extract_from_file(p2)]
        assert got == ["Sheesh. Give it a rest?"], got

        # ---- 双语数据表: 查表 key 一个都不能进表 ----
        p3 = os.path.join(tmp, "bilingual.lua")
        lines = ["return {", '["cameraPrefix"]="相机 ",', '["catalog"]={']
        for i in range(40):
            lines.append('["Prop Wooden Item %d"]="木制道具 %d",' % (i, i))
        lines += ["},", "}", ""]
        with open(p3, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(lines))
        got3 = [v for _l, _t, _c, v in E.extract_from_file(
            p3, target_locale="zh-CN")]
        assert not [v for v in got3 if v.startswith("Prop Wooden Item")], got3[:8]

        # ---- 语言包副本: *Localization_de.reds 当语言包处理 ----
        assert E.is_locale_file("GarageCoreLocalization_de.reds")
        assert E.is_locale_file("strings_ko.lua")
        assert not E.is_locale_file("GarageCoreLocalization.reds")
        assert not E.is_locale_file("BoardEnglish.reds")

        # ---- TweakDBID / CName 前缀字面量: 作为参数传递, 永不进表 ----
        #
        # 真实的 REDscript 里这种串几乎总是直接当参数塞进别的函数:
        #   BoardOptionalRecipeData.Add(rows, t"Items.X_Recipe_Common", n"Family", "Show Name")
        # 玩家看到的是 TweakDB 查表后的显示名, 不是记录名本身。
        sub = os.path.join(tmp, "tweak")
        os.makedirs(sub, exist_ok=True)
        p4 = os.path.join(sub, "recipe.reds")
        with open(p4, "w", encoding="utf-8", newline="") as f:
            f.write('module Test\n'
                    'func Fill() -> Void {\n'
                    '  Add(rows, t"Items.FS12Shotgun_Recipe_Common", '
                    'n"Pump Action Shotgun Pack", "FS12 Shotgun");\n'
                    '}\n')
        got4 = [v for _l, _t, _c, v in E.extract_from_file(p4)]
        assert "Items.FS12Shotgun_Recipe_Common" not in got4, got4
        assert "Pump Action Shotgun Pack" not in got4, got4
        assert "FS12 Shotgun" in got4, got4   # 普通字符串参数仍然要翻

        # 跳过的条数必须记进「记录名」, 否则界面看不出这条规则生效了
        _d4, meta4 = E.extract_root_ex(sub)
        assert meta4["noise"].get("记录名", 0) == 2, meta4["noise"]
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_reds_interpolation():
    r"""s"..." 插值串要整体识别, 不能切成代码碎片。

    回归背景 (2026-10-31, RedWindows\RW_Builder.reds:182): 那一行是
        s"\(count) \(GetLocalizedTextByKey(n"X.parts"))   \(...) \(this.m_page + 1)/\(pages)"
    插值里嵌了 n"..." 的引号, 词法层按引号切字符串, 于是提前截断, 最后漏出
    一条 ")) \(this.m_page + 1)/\(pages)" 当作文案。全盘 30 处 / 14 文件,
    漏出 10 条这种半句代码。
    """
    line = ('  : s"\\(count) \\(GetLocalizedTextByKey(n"X.parts"))   '
            '\\(GetLocalizedTextByKey(n"X.page")) \\(this.m_page + 1)/\\(pages)");')
    toks, lits = E.scan_source(line, "reds")
    assert len(lits) == 1, [l.inner for l in lits]
    assert lits[0].prefix == "s", lits[0].prefix
    assert lits[0].inner.endswith("\\(pages)"), lits[0].inner
    assert 'n"X.parts"' in lits[0].inner

    # 整条是插值串 -> 按 r/n/t 一样当前缀处理, 一条都不产出
    rows = E._extract_reds(line, ".reds", target_locale="zh-CN", stats={},
                           loose=True, trace=False, noise={})
    assert rows == [], rows

    # \( ) 里再嵌字符串, 里面的括号不能提前收尾
    s = 'x = s"\\(ToString("a)b")) ok";'
    _, l2 = E.scan_source(s, "reds")
    assert len(l2) == 1 and l2[0].inner.endswith(" ok"), [l.inner for l in l2]

    # 碎片本身也要能被判成噪音 (纵深防御)
    assert NOISE.hard_family(r")) \(this.m_page + 1)/\(pages)") == NOISE.FAMILY_TECH
    assert NOISE.hard_family(r"Price: \(price)") == ""
    # 有真实文本的模板不能被判掉
    assert NOISE.hard_family(r"你有 \(count) 件物品") == ""


def check_code_fragment_guard():
    """代码碎片不许写回, 写回后代码骨架必须不变。

    同一条垃圾行如果被点了「应用」, 替换会连 \\(...) 的右括号一起吃掉,
    整行语法崩掉 -> REDscript 编译失败 -> 整个脚本层不加载。所以要在写盘前
    拒绝, 而不是靠「写坏了再撤销」。
    """
    root = tempfile.mkdtemp(prefix="frag_")
    try:
        body = "\n".join([
            "module FragTest",
            "",
            "public class FragTest {",
            '  public func F() -> Void {',
            '    let pages: Int32 = 2;',
            '    let count: Int32 = 3;',
            '    let text: String = "Pick up (E)";',
            '    Show(n"status", s"\\(count) \\(GetLocalizedTextByKey(n"X.parts")) '
            '\\(this.m_page + 1)/\\(pages)");',
            "  }",
            "}",
            "",
        ])
        p = os.path.join(root, "frag.reds")
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(body)
        before = body

        junk = r")) \(this.m_page + 1)/\(pages)"
        assert E.looks_like_code_fragment(junk)
        assert not E.looks_like_code_fragment("Pick up (E)")
        assert not E.looks_like_code_fragment("See you :)")

        # 收窄后的边界: 逗号开头 / 句中出现「);」都是正常文案, 不能拒。
        # 这两支以前把 43 条真实文案挡在门外 (P19)。
        assert not E.looks_like_code_fragment(", landings after it")
        assert not E.looks_like_code_fragment(
            "settings could not be saved (the folder is not writable); "
            "the previous settings file is kept")
        # 已经是目标语言的片段放行, 否则历史中文行永久改不动
        assert not E.looks_like_code_fragment(", 已计入")
        assert not E.looks_like_code_fragment(") 重试后")
        # 单字符右括号仍是碎片
        assert E.looks_like_code_fragment(")")

        n, fails = E.apply_many_ex(p, [(8, junk, "第 1/2 页")])
        assert n == 0, n
        assert fails and "代码片段" in fails[0][4], fails
        assert open(p, encoding="utf-8").read() == before, "拒绝后文件不该被改动"

        # 正常译文照常写回
        n2, f2 = E.apply_many_ex(p, [(7, "Pick up (E)", "拾取（E）")])
        assert n2 == 1 and not f2, (n2, f2)
        assert "拾取（E）" in open(p, encoding="utf-8").read()

        # 骨架校验: 吃掉引号/括号的替换必须被拒
        assert E.same_code_shape('Show(n"a", "abc");', 'Show(n"a", "中文");', "reds")
        assert not E.same_code_shape('Show(n"a", "abc");', 'Show(n"a" "中文");', "reds")

        # 纯文本文件不做骨架校验: 中文替换英文本来就会改变 token 序列
        p2 = os.path.join(root, "notes.txt")
        with open(p2, "w", encoding="utf-8", newline="") as f:
            f.write('"Enable nearby pickup"\n')
        n3, f3 = E.apply_many_ex(p2, [(1, "Enable nearby pickup", "启用附近拾取")])
        assert n3 == 1 and not f3, (n3, f3)
        assert "启用附近拾取" in open(p2, encoding="utf-8").read()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_parse_and_locale_robustness():
    """两个「静默给出错误结论」的坑 (P20)。

    1. scan_source 用 str.isalpha() 判断标识符, 而它对中日韩字符也返回 True,
       ASCII 正则匹配不到就是 AttributeError, 整份文件被 except 吞掉 ->
       界面上只表现为「这个模组没有文本」。
    2. is_target_language_text 以前精确查 "zh-CN", 传 "zh-cn" 静默 False ->
       整份中文文件重新变成待翻候选。
    """
    # 中文标识符 / 全角数字: 只要求分词器不崩, 且文件仍能提取
    code = ("public class Foo {\n"
            "  public func F() -> Void {\n"
            "    let x = node.粒子效果ystem;\n"
            "    emitter.max粒子效果 = Cast<Uint16>(0u);\n"
            "    let y = １２３;\n"
            '    Show(n"k", "Pick up the dropped item");\n'
            "  }\n"
            "}\n")
    toks, _ = E.scan_source(code, "reds")
    assert toks, "中文标识符把分词器打崩了"

    root = tempfile.mkdtemp(prefix="st_p20_")
    try:
        p = os.path.join(root, "a.reds")
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(code)
        errs = []
        items = E.extract_from_file(p, target_locale="zh-CN", errors=errs)
        assert items, "含中文标识符的文件一条都没提取出来"
        assert not errs, ("解析错误被记下来了", errs)
        data, meta = E.extract_root_ex(root, target_locale="zh-CN")
        assert sum(len(v) for v in data.values()) >= 1, data
        assert meta.get("parse_errors") == 0, meta.get("parse_errors")

        # 真的坏掉的文件要能计数, 不能再无声消失
        bad = os.path.join(root, "b.reds")
        with open(bad, "w", encoding="utf-8", newline="") as f:
            f.write("Show(n\"k\"\n")
        errs2 = []
        assert E.extract_from_file(bad, errors=errs2) == [] or errs2 is not None
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 目标语言代码大小写/连接符无关
    for tag in ("zh-CN", "zh-cn", "zh_CN", "Zh-Cn"):
        assert E.is_target_language_text("已计入", tag), tag
        assert not E.is_target_language_text("Counted", tag), tag
    assert E.is_target_language_text("カウント", "ja-jp")
    assert E.is_target_language_text("Привет", "ru-RU")
    assert not E.is_target_language_text("已计入", ""), "空码不该当目标语言"


def _lp_write(root, rel, text):
    p = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return p


def check_position_and_data_row_guards():
    """位置证据与数据行 (P21 / P18)。

    一类是「形态完全正常的单词，但位置在说它是名字」：call(obj,"Renew") 的
    方法名、cname("None")、slotWorldPosition(c,"Head") 的插槽名。翻掉之后
    文件照样能编译，只是功能静默失效——最坏的一类错误。
    另一类是竖线 id 表 "a|b|c|20"：翻掉任一段，语音池/fact 名就失配。
    两类都没有「像不像代码」的形状特征，只能靠位置和段结构判。
    """
    lua = "\n".join([
        "local function call(obj, name, ...)",
        "  return host.call(obj, name, ...)",
        "end",
        "local function slotWorldPosition(slotComp, name)",
        "  local ok, t = pcall(function()",
        '    return slotComp:GetSlotTransform(name)',
        "  end)",
        "  return t",
        "end",
        "local sys = getSystem()",
        'local ok, st = call(sys, "Renew", owner, scope, ttl)',
        'local nm = host.cname("None")',
        'local headWorld = slotWorldPosition(slotComp, "Head")',
        'nativeSettings.addSubcategory("/tppCamera/keys", "Key bindings")',
        'nativeSettings.addSwitch("/tppCamera/x", "Enable head tracking",',
        '                         "Turns head tracking on")',
        "",
    ])
    rows = E._extract_lua(lua, loose=True, trace=False)
    origs = [r[3] for r in rows]
    assert "Renew" not in origs, origs
    assert "None" not in origs, origs
    assert "Head" not in origs, origs
    # 真界面文案照收
    assert "Key bindings" in origs, origs
    assert "Enable head tracking" in origs, origs

    reds = "\n".join([
        "module T",
        "public class T extends ScriptableSystem {",
        "  private func Roster() -> String {",
        '    return "meta.read,perspective.read,events.push,"',
        '      + "events.lease,events.workspot,settings.read";',
        "  }",
        "  private func F() -> Void {",
        '    if Equals(p[0], "ponr") { }',
        '    let beat = "takemura|story_takemura|q112_done|20";',
        '    Show(n"k", "Enable nearby pickup");',
        "  }",
        "}",
        "",
    ])
    rrows = E._extract_reds(reds, ".reds", target_locale="zh-CN", stats={},
                            loose=True, trace=False)
    rorigs = [r[3] for r in rrows]
    assert "ponr" not in rorigs, rorigs
    assert not any("meta.read" in o for o in rorigs), rorigs
    assert not any("takemura|" in o for o in rorigs), rorigs
    assert "Enable nearby pickup" in rorigs, rorigs

    # 竖线表的白名单：真正的三选一界面文本不能被误杀
    assert NOISE.is_data_row("takemura|story_takemura|q112_done|20")
    assert NOISE.is_data_row("w:3.0|s:ghxst_sfx_connect|1")
    assert not NOISE.is_data_row("Alive|Dead|Unknown")
    assert not NOISE.is_data_row("Yes|No")

    # 写回守卫：段数/属性变了 = 硬拒；二选一文案照常放行
    bad_level, _why = E.guard_level(
        "a.reds", "takemura|story_takemura|q112_done|20",
        "武村|story_takemura|q112_done|20")
    assert bad_level == "hard", bad_level
    ok_level, _ = E.guard_level("a.reds", "Alive|Dead|Unknown", "生|死|未知")
    assert ok_level == "", ok_level
    # 代码碎片是 soft（可能误报），允许单条覆盖
    soft_level, _ = E.guard_level("a.reds", r")) \(a)/\(b)", "第 1 页")
    assert soft_level == "soft", soft_level
    assert E.looks_like_protocol_list("meta.read,events.push,")
    assert not E.looks_like_protocol_list("Hello, world")
    # 被调名暗示要按「词」判, 不能按子串: artifact 里含 fact
    assert E.name_callee_hint("slotWorldPosition")
    assert E.name_callee_hint("makeTweakDBID")
    assert E.name_callee_hint("GetItemInSlot")
    assert not E.name_callee_hint("ShowArtifact")
    assert not E.name_callee_hint("FactoryReset")


def check_soft_hard_and_isolation():
    """写回失败分两级，且一条坏行不该干掉整批 (P16)。

    以前 soft（形态可疑）和 hard（必定写坏）走同一条失败通道，用户只看到
    「应用失败」；骨架校验失败还是一次全局 return 0，把同文件里几十条好译文
    一起扣下，明细里每条都写同一句原因，看不出是谁。
    """
    root = tempfile.mkdtemp(prefix="guard2_")
    try:
        p = os.path.join(root, "a.lua")
        body = "\n".join([
            'ImGui.Text("Pick up the dropped item")',
            'ImGui.Text("Enable nearby pickup")',
            'ImGui.Text("Close the panel")',
            "",
        ])
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(body)

        # soft: 默认拒绝，且级别是 soft（前端据此给「仍要写入」）
        n, fails = E.apply_many_ex(p, [(1, r")) \(a)/\(b)", "第 1 页")])
        assert n == 0 and len(fails) == 1, (n, fails)
        assert fails[0][5] == "soft" and "代码片段" in fails[0][4], fails
        assert open(p, encoding="utf-8").read() == body

        # 单条覆盖：allow_soft 只放开 soft，不再报「代码片段」
        _n1, f1 = E.apply_one(p, 1, r")) \(a)/\(b)", "第 1 页", allow_soft=True)
        assert not any("代码片段" in x[4] for x in f1), f1
        # 上面这一下可能按行号兜底写过一次，先把文件恢复成原样再测隔离
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(body)

        # 一条会改坏骨架的行 + 两条好行：好行必须照样写进去
        orig_shape = E.same_code_shape
        try:
            E.same_code_shape = (lambda old, new, lang: "BADROW" not in new)
            n2, fails2 = E.apply_many_ex(p, [
                (1, "Pick up the dropped item", "拾取掉落的物品"),
                (2, "Enable nearby pickup", "BADROW"),
                (3, "Close the panel", "关闭面板"),
            ])
        finally:
            E.same_code_shape = orig_shape
        assert n2 == 2, (n2, fails2)
        assert len(fails2) == 1 and fails2[0][0] == 1, fails2
        assert fails2[0][5] == "hard" and "骨架" in fails2[0][4], fails2
        text = open(p, encoding="utf-8", newline="").read()
        assert "拾取掉落的物品" in text and "关闭面板" in text, text
        assert "BADROW" not in text, text
    finally:
        _drop_tx(root)
        shutil.rmtree(root, ignore_errors=True)


def check_user_skip_mark():
    """「标记为代码」要能持久化，并按 (相对路径, 原文 sha1) 命中 (P19-3/4)。

    位置判据挡不住的东西（纯符号、模组特有的协议串）只能靠人判断。判断必须
    落到文件里，否则用户每次加载都还会看到同一条，应用时还会再被拒一次。
    """
    tmp = tempfile.mkdtemp(prefix="skip_")
    old = os.environ.get("MT_DATA_DIR")
    os.environ["MT_DATA_DIR"] = tmp
    try:
        import user_skip
        root = os.path.join(tmp, "MyMod")
        os.makedirs(root)
        assert user_skip.count(root) == 0
        ok, n = user_skip.add(root, r"r6/scripts/a.reds", ")")
        assert ok and n == 1, (ok, n)
        # 路径大小写/分隔符归一后再比
        assert user_skip.is_marked(root, "r6\\scripts\\a.reds", ")")
        assert user_skip.marks(root) == {
            ("r6/scripts/a.reds", user_skip.sha1_of(")"))}
        # 同一条重复标记不重复计数
        user_skip.add(root, "r6/scripts/a.reds", ")")
        assert user_skip.count(root) == 1
        # 别的模组不受影响（同名文件、同一条原文也不串）
        other = os.path.join(tmp, "OtherMod")
        os.makedirs(other)
        assert not user_skip.is_marked(other, "r6/scripts/a.reds", ")")
        assert user_skip.remove(root, "r6/scripts/a.reds", ")")
        assert not user_skip.is_marked(root, "r6/scripts/a.reds", ")")
        assert user_skip.count(root) == 0
    finally:
        os.environ.pop("MT_DATA_DIR", None)
        if old is not None:
            os.environ["MT_DATA_DIR"] = old
        shutil.rmtree(tmp, ignore_errors=True)


def check_skip_persist():
    """勾「跳过」要记住：下次加载同一行默认就是勾上的，取消就删。

    跳过 ≠ 「标记为代码」：后者是隐藏（判断这段是代码，以后不再作为候选），
    跳过只是「我暂时不想翻」，所以下次还要看得见、还能取消。两份记录分开存
    （user_skip.json / user_skips.json），互不影响。
    """
    tmp = tempfile.mkdtemp(prefix="skiprows_")
    old = os.environ.get("MT_DATA_DIR")
    os.environ["MT_DATA_DIR"] = tmp
    try:
        import user_skip
        root = os.path.join(tmp, "MyMod")
        os.makedirs(root)
        assert user_skip.skip_marks(root) == set()
        ok, n = user_skip.set_skip(root, r"r6\scripts\a.reds", "GR-Foo", True)
        assert ok and n == 1, (ok, n)
        # 路径分隔符归一后再比
        assert user_skip.skip_marks(root) == {
            ("r6/scripts/a.reds", user_skip.sha1_of("GR-Foo"))}
        # 和「标记为代码」互不影响：跳过不隐藏，代码标记也不会被跳过覆盖
        assert user_skip.marks(root) == set()
        assert not user_skip.is_marked(root, "r6/scripts/a.reds", "GR-Foo")
        user_skip.add(root, "r6/scripts/a.reds", "GR-Foo")
        assert user_skip.skip_marks(root) == {
            ("r6/scripts/a.reds", user_skip.sha1_of("GR-Foo"))}
        # 取消跳过：条目要删干净（文件为空就整段清掉，别留空架子）
        ok, n = user_skip.set_skip(root, "r6/scripts/a.reds", "GR-Foo", False)
        assert ok and n == 0, (ok, n)
        assert user_skip.skip_marks(root) == set()
        # 别的模组不串
        other = os.path.join(tmp, "OtherMod")
        os.makedirs(other)
        user_skip.set_skip(other, "r6/scripts/a.reds", "GR-Foo", True)
        assert len(user_skip.skip_marks(other)) == 1
        assert user_skip.skip_marks(root) == set()
    finally:
        os.environ.pop("MT_DATA_DIR", None)
        if old is not None:
            os.environ["MT_DATA_DIR"] = old
        shutil.rmtree(tmp, ignore_errors=True)


def check_short_loc_key():
    """两段式查表键（`GR-UiHomes`）不当正文；`this.Text` 的 value 照收。

    Gang Retaliation 自己封了 `this.T("GR-…")` / `text("GR-…", "中文兜底")`
    两层查表函数，把键当成了界面文本（一个 mod 130 行）。键形状是「大写缩写
    前缀 + 驼峰段、无空格」，只在「没有位置证据」或「查表调用实参」处拦。
    """
    import mod_text_engine as E

    assert E.is_short_loc_key("GR-UiHomes")
    assert E.is_short_loc_key("MBC-Wire-CrimeRcr18")
    assert not E.is_short_loc_key("Auto-Drive")        # 前缀不是全大写缩写
    assert not E.is_short_loc_key("GR Ui Homes")       # 有空格
    assert not E.is_short_loc_key("Desperate Sons-of-Bitches")  # 有空格
    assert E.is_lookup_call("this.T") and E.is_lookup_call("text")
    assert E.is_lookup_call("GetLocalizedText") and not E.is_lookup_call("SetText")

    lua = (
        'local function ui()\n'
        '  ImGui.Text(text("GR-UiHomes", "安全的家园（附近无伏击）"))\n'
        '  ImGui.Text("Visible label")\n'
        'end\n'
    )
    reds = (
        'public class Foo extends IScriptable {\n'
        '    private func Bar() -> Void {\n'
        '        this.m_more.SetText(StrReplace(this.T("GR-HudMore"), "%d", "0"));\n'
        '        this.Text("MBC-Wire-CrimeRcr18", "Desperate Sons-of-Bitches");\n'
        '    }\n'
        '}\n'
    )
    tmp = tempfile.mkdtemp(prefix="mt-shortkey-")
    try:
        p = os.path.join(tmp, "ui.lua")
        io.open(p, "w", encoding="utf-8", newline="").write(lua)
        texts = [x[3] for x in E.extract_from_file(p)]
        assert "GR-UiHomes" not in texts, texts
        assert "Visible label" in texts, texts
        q = os.path.join(tmp, "hud.reds")
        io.open(q, "w", encoding="utf-8", newline="").write(reds)
        rtexts = [x[3] for x in E.extract_from_file(q)]
        assert "GR-HudMore" not in rtexts, rtexts
        assert "Desperate Sons-of-Bitches" in rtexts, rtexts
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_getter_literal_labels():
    """getLabel / getActionLabel 直接返回字面量时要能进表 (P11-3)。

    这种显示名一条判据都沾不上：前面不是 `label =`（中间隔了 `function`），
    也没有调用包装；长句兜底又要求首字母大写，而它以 `[` 开头，于是永远进
    不了表 —— 用户翻完整个模组，面板上那几个按钮还是英文。

    实测全量 286 个模组：命中的 12 处全是显示名（6 对「英文原版 + 已汉化」），
    没有一条是代码。
    """
    src = "\n".join([
        "local settings = {",
        "  forDev = {",
        '    name = "forDev",',
        '    getLabel=function() return "[For Dev] Restricted Gig Settings" end,',
        '    getDescription=function() return "Only for mod authors: raw ids" end,',
        "  },",
        "  button = {",
        '    getActionLabel=function(self) return "Show ID" end,',
        '    label = "Normal label",',
        "  },",
        "  indirect = {",
        "    getLabel=function(self) return self.label end,",
        "  },",
        "  multiline = {",
        "    getLabel = function()",
        '      return "Across lines"',
        "    end,",
        "  },",
        "}",
        "",
    ])
    rows = E._extract_lua(src, loose=True, trace=False)
    types = {r[3]: r[1] for r in rows}
    assert types.get("[For Dev] Restricted Gig Settings") == "显示名称", types
    assert types.get("Show ID") == "显示名称", types
    assert types.get("Only for mod authors: raw ids") == "描述", types
    assert types.get("Across lines") == "显示名称", types
    # 返回变量（display name 在别处赋值）走表字段那条路, 仍然照收
    assert types.get("Normal label") == "选项文本", types


def check_locale_pack_names():
    """语言包类名沿用 Provider 的命名, 撞名时放弃改名。

    回归背景 (2026-10-31, DigitalVixenCore): 一个 module 下并列 31 个子语言包,
    作者用 ``DVCore_ChineseSimplified`` / ``TrueFirstPerson_ChineseSimplified``
    区分。工具一律改写成裸的 ``ChineseSimplified``, 31 个类名撞在一起,
    REDscript 报 SYM_REDEFINITION —— 整个 redscript 层编译失败, 所有模组失效。
    """
    root = tempfile.mkdtemp(prefix="lp_names_")
    try:
        def pkg(mod, cls, text):
            return ("module %s\n\nimport Codeware.Localization.*\n\n"
                    "public class %s extends ModLocalizationPackage {\n"
                    "  protected func DefineTexts() -> Void {\n"
                    '    this.Text("K", "%s");\n  }\n}\n' % (mod, cls, text))

        def prov(mod, cls, cases):
            body = "".join('      case n"%s": return new %s();\n' % c for c in cases)
            return ("module %s\n\nimport Codeware.Localization.*\n\n"
                    "public class %s extends ModLocalizationProvider {\n"
                    "  public func GetPackage(language: CName) -> ref<ModLocalizationPackage> {\n"
                    "    switch language {\n%s"
                    "      default: return null;\n    };\n  }\n}\n"
                    % (mod, cls, body))

        # 1) Provider 里已经有 zh-cn 分支 —— 必须原样沿用 DVCore_ChineseSimplified
        mod = "DigitalVixenCore.Translations"
        _lp_write(root, "DVCore/English.reds", pkg(mod, "DVCore_English", "English"))
        _lp_write(root, "DVCore/ChineseSimplified.reds",
                  pkg(mod, "DVCore_ChineseSimplified", "中文"))
        _lp_write(root, "DVCore/Provider.reds", prov(
            mod, "DVCore_LocalizationProvider",
            [("en-us", "DVCore_English"), ("zh-cn", "DVCore_ChineseSimplified")]))
        _lp_write(root, "TFP/ChineseSimplified.reds",
                  pkg(mod, "TrueFirstPerson_ChineseSimplified", "中文"))
        rep = locale_pack.prepare_codeware_package(
            root, "DVCore/English.reds", "DVCore/ChineseSimplified.reds",
            target_locale="zh-CN")
        assert not rep["error"], rep
        assert rep["class"] == "DVCore_ChineseSimplified", rep
        text = open(os.path.join(root, "DVCore/ChineseSimplified.reds"),
                    encoding="utf-8").read()
        assert "class DVCore_ChineseSimplified" in text
        assert "class ChineseSimplified" not in text, "类名被改写成裸名"
        assert "new DVCore_ChineseSimplified()" in open(
            os.path.join(root, "DVCore/Provider.reds"), encoding="utf-8").read()

        # 2) Provider 没有 zh-cn 分支, 要生成新名字; 该名字已被人占用时必须放弃
        mod2 = "Bad.Translations"
        _lp_write(root, "Bad/English.reds", pkg(mod2, "Bad_English", "English"))
        _lp_write(root, "Bad/ChineseSimplified.reds", pkg(mod2, "Bad_English", "中文"))
        _lp_write(root, "Bad/ZhPackage.reds", pkg(mod2, "ChineseSimplified", "中文"))
        _lp_write(root, "Bad/Provider.reds", prov(
            mod2, "Bad_LocalizationProvider", [("en-us", "Bad_English")]))
        rep2 = locale_pack.prepare_codeware_package(
            root, "Bad/English.reds", "Bad/ChineseSimplified.reds",
            target_locale="zh-CN")
        assert rep2["error"], rep2
        assert rep2["applied"] is False
        assert "ChineseSimplified" in rep2["error"], rep2

        # 3) 同一 module 下并列两个 Provider, 只许动认领了当前包类的那个
        mod3 = "Two.Translations"
        _lp_write(root, "A/English.reds", pkg(mod3, "A_English", "English"))
        _lp_write(root, "A/ChineseSimplified.reds", pkg(mod3, "A_English", "中文"))
        _lp_write(root, "A/Provider.reds", prov(
            mod3, "A_Provider", [("en-us", "A_English")]))
        _lp_write(root, "B/ChineseSimplified.reds",
                  pkg(mod3, "B_ChineseSimplified", "中文"))
        _lp_write(root, "B/Provider.reds", prov(
            mod3, "B_Provider",
            [("en-us", "B_English"), ("zh-cn", "B_ChineseSimplified")]))
        b_before = open(os.path.join(root, "B/Provider.reds"),
                        encoding="utf-8").read()
        rep3 = locale_pack.prepare_codeware_package(
            root, "A/English.reds", "A/ChineseSimplified.reds",
            target_locale="zh-CN")
        assert not rep3["error"], rep3
        assert rep3["class"] == "ChineseSimplified", rep3
        a_text = open(os.path.join(root, "A/Provider.reds"), encoding="utf-8").read()
        assert 'case n"zh-cn": return new ChineseSimplified();' in a_text, a_text
        b_after = open(os.path.join(root, "B/Provider.reds"),
                       encoding="utf-8").read()
        assert b_after == b_before, "动了别的语言包的 Provider"
        assert "B_ChineseSimplified" in b_after
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_locale_pack_copy_guard():
    """「语言包 + 逻辑」混写的 .reds 不许整文件复制成语言包。

    回归背景 (2026-10-31, INCF-CORE): 一个文件里既有 EnglishPackage, 又有
    CompatibilityManager / RequirementsPopup / @addField/@wrapMethod。工具
    把它整份复制成 ChineseSimplified.reds, 于是 4 个类型重复声明 + 2 个
    重复注入, REDscript 报 SYM_REDEFINITION, 整个脚本层编译失败。
    """
    root = tempfile.mkdtemp(prefix="lp_copy_")
    try:
        mono = "\n".join([
            "module INCFCore",
            "",
            "public class EnglishPackage extends ModLocalizationPackage {",
            "  protected func DefineTexts() -> Void {",
            '    this.Text("UI-X", "Hi");',
            "  }",
            "}",
            "",
            "public class RequirementsPopup {",
            "}",
            "",
            "@addField(QuestTrackerGameController)",
            "private let m_x: Int32;",
            "",
        ])
        _lp_write(root, "mono.reds", mono)
        assert not locale_pack.reds_is_pure_package(mono)
        # v2.43.1 起不再直接拒绝: 把语言包那个类抽出来写成新文件, 逻辑与
        # @addField 留在原文件里。抽出来的必须只有 1 个类、this.Text 条数一致。
        out = locale_pack.ensure_target(root, "mono.reds", "ChineseSimplified.reds")
        got = open(out, encoding="utf-8-sig").read()
        assert locale_pack.reds_is_pure_package(got), got
        assert len(locale_pack.CODEWARE_PACKAGE_RE.findall(got)) == 1, got
        assert "RequirementsPopup" not in got, "逻辑类不该被抽进来"
        assert "@addField" not in got, "注入注解不该被抽进来"
        assert got.count("this.Text(") == mono.count("this.Text("), got
        assert "class ChineseSimplified extends ModLocalizationPackage" in got, got
        # 原文件一个字节都不能动
        assert open(os.path.join(root, "mono.reds"), encoding="utf-8-sig").read() == mono

        # 抽不出类时仍然拒绝, 且不留半成品
        broken = "module X\n\npublic class Helper {}\n\n@wrapMethod(Foo)\nfunc Bar() {}\n"
        _lp_write(root, "broken.reds", broken)
        try:
            locale_pack.ensure_target(root, "broken.reds", "Broken_zh.reds")
            assert False, "没有语言包类时应当拒绝"
        except ValueError as ex:
            assert "SYM_REDEFINITION" in str(ex), ex
        assert not os.path.exists(os.path.join(root, "Broken_zh.reds")), "拒绝后不能留半成品"

        pure = "\n".join([
            "module Pure.Core",
            "",
            "import Codeware.Localization.*",
            "",
            "public class Pure_English extends ModLocalizationPackage {",
            "  protected func DefineTexts() -> Void {",
            '    this.Text("K", "Hi");',
            "  }",
            "}",
            "",
        ])
        _lp_write(root, "pure.reds", pure)
        assert locale_pack.reds_is_pure_package(pure)
        locale_pack.ensure_target(root, "pure.reds", "pure_zh.reds")
        assert os.path.exists(os.path.join(root, "pure_zh.reds"))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_mod_batch():
    """批量（单模组功能）：复制规则 / 名字清洗 / 会话隔离。"""
    import mod_batch as MB

    # 名字清洗：Windows 非法字符压平，空名有兜底
    assert MB.safe_name("A/B:C*D") == "A_B_C_D", MB.safe_name("A/B:C*D")
    assert MB.safe_name("") == "mod"
    assert MB.safe_name("  . ") == "mod"

    src = tempfile.mkdtemp(prefix="mb_src_")
    dst = os.path.join(src, "out")
    os.makedirs(os.path.join(src, "sub"))
    os.makedirs(os.path.join(src, ".git"))
    for rel, body in (("a.reds", "class A {}"),
                      ("b.lua", "local x = 1"),
                      ("c.dds", "x"),
                      ("d.wav", "x"),
                      ("e.archive", "x"),
                      ("sub/f.json", "{}"),
                      (".git/HEAD", "ref: refs/heads/main")):
        p = os.path.join(src, rel.replace("/", os.sep))
        with io.open(p, "w", encoding="utf-8") as f:
            f.write(body)

    st = MB.copy_mod(src, dst)
    got = set()
    for dirpath, _dirs, files in os.walk(dst):
        for fn in files:
            got.add(os.path.relpath(os.path.join(dirpath, fn), dst)
                    .replace(os.sep, "/"))
    assert "a.reds" in got and "b.lua" in got, got
    assert "sub/f.json" in got, "目录结构没保留: %s" % sorted(got)
    assert "c.dds" not in got and "d.wav" not in got, "贴图/音频没跳过: %s" % sorted(got)
    assert "e.archive" not in got, "归档层没开却复制了 .archive"
    assert not any(g.startswith(".git") for g in got), ".git 不该被复制"
    assert st["files"] == 3 and st["skipped"] == 3, st

    dst2 = os.path.join(src, "out2")
    MB.copy_mod(src, dst2, with_archive=True)
    assert os.path.isfile(os.path.join(dst2, "e.archive")), "开了归档层却没复制"

    # 会话隔离：worker 有自己的 rows/settings，但共享翻译记忆
    api = web_app.Api()
    api.rows = [{"file": "x", "line": 1, "orig": "A", "trans": ""}]
    w = api.spawn_worker()
    assert w.rows is not api.rows, "worker 和主会话共用一份 rows"
    assert w.settings is not api.settings, "worker 改设置会污染主会话"
    assert w._memory is api._memory_store, "worker 没共享翻译记忆"
    w.rows = []
    assert len(api.rows) == 1, "worker 清空 rows 把主会话的表格也清了"

    shutil.rmtree(src, ignore_errors=True)


def check_markup_repair():
    """译文必须把 `<Rich ...>` / `</>` / `{占位符}` 带回来。

    标记丢了只是少一段高亮，`{value}` 被写成 `%s` 却是弹窗里原样显示 `%s`。
    位置固定（开头、结尾、关闭标记）的由工具补回去；补不回来的必须报出来，
    由写回阶段按软失败拦下。
    """
    fixed, lost = E.repair_markup(
        '<Rich color="X" style="Semi-Bold">-{int_0}% cost</> when hit',
        "命中时 -{int_0}% 消耗。")
    assert fixed.startswith('<Rich color="X"'), fixed
    assert fixed.endswith("</>"), fixed
    assert lost == [], lost

    fixed, lost = E.repair_markup("Base game value: {value}", "游戏默认值：%s")
    assert "{value}" in fixed and "%s" not in fixed, fixed
    assert lost == [], lost

    fixed, lost = E.repair_markup("A {x} B", "纯中文")
    assert lost == ["{x}"], lost

    # 句中两段不同颜色的标签补不回去：只丢配色，不拦。
    fixed, lost = E.repair_markup(
        'Hit for <Rich color="A">+{int_0}%</> and <Rich color="B">-{int_1}%</>',
        "伤害 +{int_0}%，代价 -{int_1}%")
    assert lost == [], lost

    fixed, lost = E.repair_markup("plain sentence", "普通句子")
    assert (fixed, lost) == ("普通句子", []), (fixed, lost)


def check_apply_hygiene():
    """批量写回：不留 `.bak`，不建撤销记录；标记丢失的那条不写。"""
    import mod_batch as MB

    class _NullMem(object):
        def put(self, *a, **k):
            return True

        def save(self):
            return True

        def get(self, *a, **k):
            return ""

    tmp = tempfile.mkdtemp()
    root = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, "sub"))
        for rel in ("a.reds.bak", "sub/b.lua.bak"):
            with io.open(os.path.join(tmp, rel.replace("/", os.sep)),
                         "w", encoding="utf-8") as f:
                f.write("x")
        with io.open(os.path.join(tmp, "keep.reds"), "w", encoding="utf-8") as f:
            f.write("y")
        assert MB.drop_backups(tmp) == 2
        assert os.path.isfile(os.path.join(tmp, "keep.reds")), "清理把正文删了"

        p = os.path.join(root, "sample.lua")
        with io.open(p, "w", encoding="utf-8", newline="") as f:
            f.write('Loc = {\n  hello = "Hello there",\n  tip = "<b>Hi</b>",\n'
                    '  raw = "A {x} B",\n}\n')
        api = web_app.Api()
        api._memory = _NullMem()
        api.rows = []
        api.root = root
        items = [
            {"file": "sample.lua", "line": 2, "ctx": "hello", "orig": "Hello there",
             "trans": "你好", "target": "", "kind": "text", "root": root},
            {"file": "sample.lua", "line": 3, "ctx": "tip", "orig": "<b>Hi</b>",
             "trans": "嗨", "target": "", "kind": "text", "root": root},
            {"file": "sample.lua", "line": 4, "ctx": "raw", "orig": "A {x} B",
             "trans": "纯中文", "target": "", "kind": "text", "root": root},
        ]
        res = api._do_apply(items, keep_backup=False, make_undo=False)
        text = io.open(p, encoding="utf-8").read()
        assert res["ok"] == 2 and res["fail"] == 1, res
        assert res["undoId"] == "", "批量不该建撤销记录: %r" % res["undoId"]
        assert "你好" in text and "<b>嗨</b>" in text, text
        assert "A {x} B" in text, "占位符丢失的那条不该写进去"
        assert not [n for n in os.listdir(root) if n.endswith(".bak")], "留了 .bak"
        why = [f.get("reason") for f in res.get("failed") or []]
        assert any("{x}" in w for w in why), why
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def check_package_class_langs():
    """一个 .reds 里多个语言包类时，只取源语言那一段。"""
    assert NOISE._class_lang("BNFTweaksLocalizationEnUs") == "en"
    assert NOISE._class_lang("BNFTweaksLocalizationFrFr") == "fr"
    assert NOISE._class_lang("JBlinkDashKorean") == "kr"
    assert NOISE._class_lang("EnglishPackage") == "en"
    assert NOISE._class_lang("Localization") == ""

    text = (
        "module X\n"
        "import Codeware.Localization.*\n"
        "public class EnUs extends ModLocalizationPackage {\n"
        '  func F() { this.Text("k", "Hello there"); }\n'
        "}\n"
        "public class FrFr extends ModLocalizationPackage {\n"
        '  func F() { this.Text("k", "Bonjour tout le monde"); }\n'
        "}\n"
    )
    secs = NOISE.lang_sections(text)
    assert len({s for _p, s in secs}) == 2, secs
    keep = NOISE.keep_sections(secs, prefer=("en",))
    assert keep is not None and len(keep) == 1, keep

    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "mixed.reds")
        with io.open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        rows = locale_pack._text_pairs_ex(p, only_class="EnUs")
        vals = [r[1] for r in rows]
        assert vals == ["Hello there"], vals
        rows = locale_pack._text_pairs_ex(p)
        assert len(rows) == 2, rows      # 不指定类时保持原样（两段都算）
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_own_product_and_prefilter():
    """自己的产物不能被当成源。

    部署过一次之后 mod 目录里就躺着 `Foo_zh-cn.archive` + `.xl`。老 `_resolve_dir`
    按「.xl 的 localization 段非空」挑，我们的包恰好非空，于是挑中自己写的中文
    包，把中文当原文再翻一遍。同时钉住 `localization_files` 的预筛口径。
    """
    import localization.archive as Larch
    import localization.wkit as Lwkit

    real_xl = (
        "localization:\n"
        "  onscreens:\n"
        "    en-us:\n"
        "      - localization\\\\en-us.json\n"
    )
    own_xl = (
        "localization:\n"
        "  extend: Foo.archive.xl\n"
        "  onscreens:\n"
        "    zh-cn:\n"
        "      - l10n\\\\foo\\\\zh-cn\\\\onscreens\\\\00_onscreens.json\n"
    )
    tmp = tempfile.mkdtemp(prefix="mt-own-")
    try:
        def put(name, text):
            p = os.path.join(tmp, name)
            with io.open(p, "w", encoding="utf-8", newline="") as f:
                f.write(text)

        open(os.path.join(tmp, "Foo.archive"), "wb").write(b"x")
        put("Foo.archive.xl", real_xl)
        open(os.path.join(tmp, "Foo_zh-cn.archive"), "wb").write(b"y")
        put("Foo_zh-cn.archive.xl", own_xl)

        t = Larch._resolve_dir(tmp)
        assert t.name == "Foo", t.name
        assert os.path.basename(str(t.xl_path)) == "Foo.archive.xl", t.xl_path
        assert os.path.basename(str(t.archive_path)) == "Foo.archive", t.archive_path

        # 只有产物时必须报错，不能把中文当原文
        solo = tempfile.mkdtemp(prefix="mt-own-solo-")
        try:
            open(os.path.join(solo, "Foo_zh-cn.archive"), "wb").write(b"y")
            with io.open(os.path.join(solo, "Foo_zh-cn.archive.xl"), "w",
                         encoding="utf-8", newline="") as f:
                f.write(own_xl)
            try:
                Larch._resolve_dir(solo)
                raise AssertionError("目录里只有自己的产物却没报错")
            except Larch.ArchiveError:
                pass
        finally:
            shutil.rmtree(solo, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 名字像、内容不是我们的写法（别人做的汉化）不能误伤
    assert not Larch.is_own_product("D:\\x\\SomeMod.archive")
    assert Larch.is_own_product("D:\\x\\SomeMod_zh-cn.archive")

    # 归档预筛只问「有没有 .json」，不问名字像不像本地化表：目录名实测毫无规律
    # （`translations\translation_strings.json`、`02_s15_local.json` 都见过），
    # 真正的类型交给解包后的 CR2W 头判断。名字不像的也要放行。
    files = ["localization\\en-us.json", "readme.txt",
             "mod\\data\\localization\\zh-cn.json", "x\\whatever.json"]
    got = Lwkit.localization_files(files)
    assert "localization\\en-us.json" in got, got
    assert "x\\whatever.json" in got, got
    assert "readme.txt" not in got, got


def check_deploy_and_manifest():
    """按文件覆盖 + 清单 + 「还原后被重新读回来」。

    钉住四件事：
      1. dry_run 不写盘，但数字算得对；
      2. 真部署只覆盖副本里有的文件，原目录其它文件一个字节不动；
      3. 清单记的性质（added）与 sha1 正确；
      4. 用户解压还原之后 hash 变了，copy_mod 必须把它读回来，
         不能因为路径还在清单里就永久跳过。
    """
    import mod_batch as MB
    import mod_deploy as MD

    old_cfg = MD.config_dir
    tmp = tempfile.mkdtemp(prefix="mt-deploy-")
    try:
        vault = os.path.join(tmp, "userdata")
        MD.config_dir = lambda: vault
        copy_dir = os.path.join(tmp, "copy")
        orig = os.path.join(tmp, "orig")
        for d in (copy_dir, orig):
            os.makedirs(d)

        def put(root, name, text):
            p = os.path.join(root, name)
            os.makedirs(os.path.dirname(p) or root, exist_ok=True)
            with io.open(p, "w", encoding="utf-8", newline="") as f:
                f.write(text)

        put(copy_dir, "a.reds", "中文 A")
        put(copy_dir, "zh-cn.reds", "中文包")
        put(orig, "a.reds", "english A")
        put(orig, "keep.bin", "keep me")
        put(orig, "only_orig.txt", "not in copy")

        dry = MD.deploy_mod(copy_dir, orig, dry_run=True)
        assert dry["ok"], dry
        assert dry["overwritten"] == 1, dry
        assert dry["added"] == 1, dry
        with io.open(os.path.join(orig, "a.reds"), encoding="utf-8") as f:
            assert f.read() == "english A", "dry_run 写了盘"

        rep = MD.deploy_mod(copy_dir, orig, pairs_list=[("english A", "中文 A")])
        assert rep["ok"], rep
        assert rep["overwritten"] == 1 and rep["added"] == 1, rep
        with io.open(os.path.join(orig, "a.reds"), encoding="utf-8") as f:
            assert f.read() == "中文 A"
        assert os.path.isfile(os.path.join(orig, "zh-cn.reds"))
        with io.open(os.path.join(orig, "keep.bin"), encoding="utf-8") as f:
            assert f.read() == "keep me", "副本里没有的文件被动了"
        assert os.path.isfile(os.path.join(orig, "only_orig.txt"))

        files = MD.deployed_files(orig)
        assert files.get("zh-cn.reds", ("", False))[1] is True, files
        assert files.get("a.reds", ("", True))[1] is False, files
        assert MD.pairs(orig) == [("english A", "中文 A")], MD.pairs(orig)

        # 原样再部署一次：内容一样就不重写
        rep2 = MD.deploy_mod(copy_dir, orig)
        assert rep2["overwritten"] == 0 and rep2["same"] == 2, rep2

        # 清单命中且 hash 相同 -> copy_mod 跳过自己的产物
        out = os.path.join(tmp, "out")
        st = MB.copy_mod(orig, out, with_archive=True, skip_assets=False)
        assert st["own"] == 1, st          # zh-cn.reds 被跳过
        assert os.path.isfile(os.path.join(out, "a.reds"))

        # 模拟用户解压还原：内容变了，就必须读回来
        put(orig, "zh-cn.reds", "author's own pack")
        out2 = os.path.join(tmp, "out2")
        st2 = MB.copy_mod(orig, out2, with_archive=True, skip_assets=False)
        assert st2["own"] == 0, st2
        assert os.path.isfile(os.path.join(out2, "zh-cn.reds"))
    finally:
        MD.config_dir = old_cfg
        shutil.rmtree(tmp, ignore_errors=True)


def check_build_skips_broken_resource():
    """一个资源转换失败不能拖垮整包。

    WolvenKit 的 `convert deserialize` 会**原生崩溃**（exit 0xC0000005，栈落在
    System.CommandLine 上，跟输入无关：同一个 JSON 再跑一次就过）。线上那次
    Equipment-EX 就栽在这上面：8 条译文全部记成失败，其实其它资源都好好的。
    这里钉住三件事：失败重试一次、还不行只跳过那一个、.xl 里不许声明被跳过的。
    """
    import localization.builder as B

    # 报错尾部要能捞到真正的异常，而不是每次一样的调用链
    lines = ["[ 0: Information ] - Found 1 files to process.",
             "Unhandled exception. System.NullReferenceException: Object reference",
             "   at WolvenKit.Core.Cr2wFile.Write()",
             "   at System.CommandLine.Invocation.InvocationPipeline.Invoke(...)"]
    import localization.wkit as W
    got = W._error_tail(lines)
    assert "NullReferenceException" in got, got

    calls = {"n": 0}

    def fake_deser(cli, src, outdir, log=None, expect_name=None):
        calls["n"] += 1
        if "01_b" in str(src):
            raise W.WkitError("WolvenKit convert 失败（exit 3221225477）", "boom")
        from pathlib import Path
        d = Path(outdir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / (expect_name or "x")
        p.write_bytes(b"CR2W" + b"\0" * 16)
        return p

    def fake_pack(cli, folder, outdir, log=None):
        from pathlib import Path
        out = Path(outdir)
        out.mkdir(parents=True, exist_ok=True)
        p = out / (Path(folder).name + ".archive")
        p.write_bytes(b"ARCHIVE")
        return p

    old_deser, old_pack = B.wkit.deserialize, B.wkit.pack
    tmp = tempfile.mkdtemp(prefix="mt-skipres-")
    try:
        B.wkit.deserialize, B.wkit.pack = fake_deser, fake_pack
        spec = B.make_spec("StubMod", extend="Stub.archive.xl")
        spec.onscreens.append(B.Resource("l10n\\m\\zh-cn\\onscreens\\00_a.json",
                                         {"a": 1}))
        spec.onscreens.append(B.Resource("l10n\\m\\zh-cn\\onscreens\\01_b.json",
                                         {"b": 2}))
        res = B.build(spec, "cli", os.path.join(tmp, "work"), os.path.join(tmp, "out"))
        assert res.skipped == ["l10n\\m\\zh-cn\\onscreens\\01_b.json"], res.skipped
        assert res.resources == ["l10n\\m\\zh-cn\\onscreens\\00_a.json"], res.resources
        assert calls["n"] == 3, calls            # 两个资源 + 失败那次重试
        xl = io.open(res.xl_path, encoding="utf-8").read()
        assert "00_a.json" in xl and "01_b.json" not in xl, xl
        assert res.archive.is_file()
        assert any("01_b.json" in w for w in res.warnings), res.warnings
        # 现场要留下来：出错的 JSON + CLI 输出，便于拿同一份文件复现
        faildir = os.path.join(tmp, "work", "StubMod", "failed")
        assert os.path.isdir(faildir), faildir
        names = os.listdir(faildir)
        assert names and any("01_b" in n for n in names), names
    finally:
        B.wkit.deserialize, B.wkit.pack = old_deser, old_pack
        shutil.rmtree(tmp, ignore_errors=True)


def check_batch_todo_filter():
    """批量面板能折起「无需翻译」的目录，且读取失败的不会被一起藏掉。

    255 个 staging 目录里只有 127 个有待译行，其余 todo == 0，一屏里近一半
    是噪音。过滤只按 todo 走，不重新扫盘。
    """
    base = os.path.dirname(HERE)
    with io.open(os.path.join(base, "src", "web", "app.js"), encoding="utf-8") as f:
        js = f.read()
    assert "state.mbHideTodo" in js, "面板没有「隐藏无需翻译」开关"
    assert "mbUsable(it) || it.error" in js, "过滤会把读取失败的目录一起藏掉"
    with io.open(os.path.join(base, "src", "web", "index.html"),
                 encoding="utf-8") as f:
        html = f.read()
    assert 'id="q-hide-todo"' in html, "面板里没有这个勾选框"
    import appconfig
    assert "batch_hide_todo" in appconfig.DEFAULT_SETTINGS
    # 扫描后不许重置勾选状态
    seg = js.split("function onMbScanned", 1)[-1].split("function ", 1)[0]
    assert "mbHideTodo" not in seg, "扫描会把勾选状态重置掉"
    return True


def check_dup_key_writeback():
    """同一个条目名在一份语言包里出现两次时，写回不能把后面的代码吃掉。

    回归（v2.44.3）：DVCore 的 `FACryCapacity/ChineseSimplified.reds` 在 155 行和
    220 行各有一条 `FACC.UI.rowBodySlots`。两条同名行指向同一个字面量区间，
    按偏移从后往前写第二遍时，第一次已经把文本改短，旧的 end 就多切掉了后面的
    `);` 与换行：

        this.Text("FACC.UI.rowBodySlots", "身体槽" this.Text("FACC.UI.rowBackpackSlots", ...);

    整份 REDscript 编译失败（REDScript compilation has failed）。现在同名条目只
    计一次、但每一处都写；写盘前还会对一次括号结构，对不上就整批放弃。
    """
    text = (
        "module X\r\n"
        "public class P extends ModLocalizationPackage {\r\n"
        "  func A() {\r\n"
        '    this.Text("K.a", "BODY SLOTS");\r\n'
        '    this.Text("K.b", "BAG SLOTS");\r\n'
        "  }\r\n"
        "  func B() {\r\n"
        '    this.Text("K.a", "BODY SLOTS");\r\n'
        "  }\r\n"
        "}\r\n"
    )
    tmp = tempfile.mkdtemp(prefix="mt-dupkey-")
    try:
        p = os.path.join(tmp, "ChineseSimplified.reds")
        with io.open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        rows = [("K.a", "BODY SLOTS", "身体槽"),
                ("K.b", "BAG SLOTS", "背包槽"),
                ("K.a", "BODY SLOTS", "身体槽")]     # 同一份里两条同名行
        n, fails = locale_pack.apply_text_target(p, rows)
        got = io.open(p, encoding="utf-8-sig", newline="").read()
        assert "身体槽" in got and "背包槽" in got, got
        merged = [ln for ln in got.splitlines() if ln.count("this.Text(") >= 2]
        assert not merged, merged
        assert got.count("身体槽") == 2, got        # 两处都写掉了
        assert "BODY SLOTS" not in got, got
        assert (locale_pack._punct_shape(got, "reds")
                == locale_pack._punct_shape(text, "reds")), got
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_archive_lang_filter():
    """归档里只序列化源语言 + 目标语言（其余十几种语言一次 2.8 秒，全白等）。

    Nearby Auto Loot 有 19 个语言 json，加载 58.8 秒；筛到 2 个后约 8 秒。
    语言码认不出来的文件必须保留——作者的命名毫无规律。
    """
    import localization.archive as Larch

    assert Larch.lang_of_rel("equipment_ex\\localization\\en-us.json") == "en-us"
    assert Larch.lang_of_rel("base\\localization\\en-us\\onscreens\\x.json") == "en-us"
    assert Larch.lang_of_rel("Localization\\ZH_CN.json") == "zh-cn"
    assert Larch.lang_of_rel("kiki\\kiki_buttercup_dress_localization.json") == ""
    assert Larch.lang_of_rel("base\\yv\\axl\\02_s15_local.json") == ""

    tmp = tempfile.mkdtemp(prefix="mt-langf-")
    try:
        rels = ["loc\\en-us.json", "loc\\zh-cn.json", "loc\\de-de.json",
                "loc\\ja-jp.json", "loc\\translation_strings.json"]
        files = {}
        for i, rel in enumerate(rels):
            p = os.path.join(tmp, "%d.json" % i)
            with open(p, "wb") as f:
                f.write(b"CR2W" + b"\0" * 8)
            files[rel] = p
        got = Larch.candidate_json(files, langs=("en-us", "zh-cn"))
        assert "loc\\en-us.json" in got, got
        assert "loc\\zh-cn.json" in got, got
        assert "loc\\de-de.json" not in got, got
        assert "loc\\ja-jp.json" not in got, got
        # 认不出语言的一律保留
        assert "loc\\translation_strings.json" in got, got
        # 不传 langs 时保持原样（全部候选）
        assert len(Larch.candidate_json(files)) == 5
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_toc_cache_key():
    """归档目录读取的缓存键必须扛得住「复制到输出目录」。

    批量每次都把模组复制一份再扫，路径和 mtime 全变。第一版缓存键带了 mtime，
    第二轮还是 51 秒——等于没缓存。
    """
    import localization.wkit as W

    tmp = tempfile.mkdtemp(prefix="mt-toc-")
    try:
        a = os.path.join(tmp, "A.archive")
        b = os.path.join(tmp, "A.archive")     # 同名同内容、不同目录
        os.makedirs(os.path.join(tmp, "copy"), exist_ok=True)
        with open(a, "wb") as f:
            f.write(b"CR2W" + b"x" * 1000)
        dst = os.path.join(tmp, "copy", "A.archive")
        shutil.copy2(a, dst)
        os.utime(dst, (1, 1))                   # 模拟复制后 mtime 变了
        assert W._toc_key(a) == W._toc_key(dst), (W._toc_key(a), W._toc_key(dst))
        # 内容变了就必须换键
        with open(dst, "r+b") as f:
            f.seek(0)
            f.write(b"XXXX")
        assert W._toc_key(a) != W._toc_key(dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_mt_batch_retry():
    """整批解析失败先整批重试一次，再退化成逐条（8 倍代价）。"""
    import mt_client as M

    calls = {"n": 0}

    def fake_chat(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return "抱歉，我只能输出解释，不能保证格式"
        return '["甲", "乙"]'

    old = M.chat
    M.chat = fake_chat
    try:
        out = M.translate_batch("http://127.0.0.1:1/v1", "m", ["a", "b"])
    finally:
        M.chat = old
    assert calls["n"] == 2, calls          # 恰好：首次 + 一次整批重试
    assert out == ["甲", "乙"], out


def check_package_if_guard():
    """抽语言包类时，import 紧贴的 `@if` 守卫必须一起带走。

    CyberwareEx 那种「语言包 + 逻辑」混写文件，可选子模块的 import 是
    `@if(ModuleExists(...))` 包着的。只保留 module/import 行会生成一个无保护的
    import：缺装子模块的机器直接 UNRESOLVED_IMPORT，整层 REDscript 编译失败
    （游戏日志里的 `CyberwareEx\\ChineseSimplified.reds:3:1`）。反过来也不能保留
    所有 `@` 行——修饰 `public func` 的注解会变成悬空注解。
    """
    guard = '@if(ModuleExists("CyberwareEx.Customization"))'
    text = (
        "module CyberwareEx\n"
        "import Codeware.Localization.*\n"
        + guard + "\n"
        "import CyberwareEx.Customization.*\n"
        "\n"
        + guard + "\n"
        "public func IsCustomMode() -> Bool { return false; }\n"
        "\n"
        "public class EnUs extends ModLocalizationPackage {\n"
        "  protected func DefineTexts() -> Void {\n"
        '    this.Text("UI-a", "Hello there");\n'
        "  }\n"
        "}\n"
    )
    got = locale_pack._package_class_block(text)
    assert got, "混写文件没抽出语言包类"
    _name, _block, head = got
    expect = ["module CyberwareEx",
              "import Codeware.Localization.*",
              guard,
              "import CyberwareEx.Customization.*"]
    assert head.splitlines() == expect, head

    tmp = tempfile.mkdtemp()
    try:
        d = os.path.join(tmp, "r6", "scripts", "CyberwareEx")
        os.makedirs(d)
        src = os.path.join(d, "CyberwareEx.reds")
        with io.open(src, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        out = locale_pack.ensure_target(
            tmp, "r6/scripts/CyberwareEx/CyberwareEx.reds",
            "r6/scripts/CyberwareEx/ChineseSimplified.reds")
        body = io.open(out, encoding="utf-8-sig").read()
        lines = body.splitlines()
        assert lines[0] == "module CyberwareEx", lines[:3]
        assert guard in lines[:6], "生成的副本里 `@if` 丢了: %r" % (lines[:6],)
        idx = lines.index(guard)
        assert lines[idx + 1].strip().startswith("import CyberwareEx."), lines[:6]
        assert body.count(guard) == 1, "函数注解被一起搬进语言包: %r" % (body[:400],)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_json_label_words():
    """json 语言包里的单个单词也要提取（车的颜色/部位名整片是英文的原因）。"""
    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(tmp, "en-us.json")
        data = {"All": "All", "Paint": "Paint", "Beige": "Beige",
                "Desc": "Pick a paint", "weapon": "weapon",
                "flag": "true", "num": "12"}
        with io.open(src, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))

        files = locale_pack.detect(tmp, parse=True, target="zh-CN")
        srcs = locale_pack.pick_sources(files, "zh-CN")
        rows, _meta = locale_pack.extract(tmp, srcs, "zh-CN")
        origs = {r["orig"] for r in rows}
        for want in ("All", "Paint", "Beige", "Pick a paint"):
            assert want in origs, "%r 没被提取: %r" % (want, sorted(origs))
        for bad in ("weapon", "true", "12"):
            assert bad not in origs, "%r 不该进待译行" % (bad,)
        assert not any(r.get("inTarget") for r in rows)

        mapping = {"All": "全部", "Paint": "漆面", "Beige": "米色",
                   "Desc": "选择漆面"}
        mapping = {r["key"]: mapping[r["orig"]] for r in rows
                   if r["orig"] in mapping}
        written, missing, err = locale_pack.write_json_target_ex(
            tmp, "en-us.json", "zh-cn.json", mapping)
        assert not err and not missing, (err, missing)
        out = json.load(io.open(os.path.join(tmp, "zh-cn.json"), encoding="utf-8"))
        assert out["All"] == "全部" and out["Beige"] == "米色", out
        assert out["weapon"] == "weapon", "不可译的键不该被改"

        # 目标已有全部叶子键 -> 允许跳过写回；源多了键 -> 必须写
        assert not locale_pack.json_needs_fill(tmp, "en-us.json", "zh-cn.json")
        with io.open(src, "w", encoding="utf-8") as f:
            data["extra"] = "true"
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        assert locale_pack.json_needs_fill(tmp, "en-us.json", "zh-cn.json")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_settled_no_rewrite():
    """译文本来就在目标文件里时不再重写它（第 11 轮 17,060 行的来源）。"""
    class _NullMem(object):
        def put(self, *a, **k):
            return True

        def save(self):
            return True

        def get(self, *a, **k):
            return ""

    root = tempfile.mkdtemp()
    try:
        en = os.path.join(root, "en.lua")
        zh = os.path.join(root, "zh_CN.lua")
        with io.open(en, "w", encoding="utf-8", newline="") as f:
            f.write('Loc = {\n  hello = "Hello there",\n}\n')
        with io.open(zh, "w", encoding="utf-8", newline="") as f:
            f.write('Loc = {\n  hello = "你好",\n}\n')
        before = io.open(zh, "rb").read()
        st = os.stat(zh)

        api = web_app.Api()
        api._memory = _NullMem()
        api.rows = []
        api.root = root
        items = [{"file": "en.lua", "target": "zh_CN.lua", "line": 2,
                  "ctx": "hello", "orig": "Hello there", "trans": "你好",
                  "target": "zh_CN.lua", "kind": "text", "root": root,
                  "inTarget": 1}]
        res = api._do_apply(items, keep_backup=False, make_undo=False)
        assert res["ok"] == 1 and res["settled"] == 1, res
        assert io.open(zh, "rb").read() == before, "目标文件被无谓重写了"
        assert os.stat(zh).st_mtime_ns == st.st_mtime_ns, "mtime 不该变"

        # 有一条是新译文 -> 整组照写
        items[0]["inTarget"] = 0
        items[0]["trans"] = "你好呀"
        res = api._do_apply(items, keep_backup=False, make_undo=False)
        assert res["ok"] == 1 and res["settled"] == 0, res
        assert "你好呀" in io.open(zh, encoding="utf-8").read()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_retranslate_forget():
    """「重译这个模组（丢弃本轮记忆）」：只删机翻/记忆来源，并删掉续跑记录那行。"""
    import mod_deploy as MD

    old_cfg = MD.config_dir
    tmp = tempfile.mkdtemp(prefix="mt-forget-")
    try:
        MD.config_dir = lambda: os.path.join(tmp, "userdata")
        root = os.path.join(tmp, "mod")
        os.makedirs(root)
        MD.record(root,
                  files={"zh-cn.json": {"sha1": "x", "added": True}},
                  pairs_list=[("Hello there", "你好"), ("Close", "关闭")])
        calls = []

        class _Mem(object):
            def forget(self, src, lang):
                calls.append((src, lang))
                return src != "Close"     # 官方/手改那条删不掉, 不能算数

        n = MD.forget_in(root, _Mem(), "简体中文")
        assert n == 1 and calls[0] == ("Hello there", "简体中文"), (n, calls)
        assert len(MD.pairs(root)) == 2, "清单里的译文对不该被删"

        jp = os.path.join(tmp, "run.jsonl")
        with io.open(jp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"__sig": "abc"}) + "\n")
            for k in ("Mod A", "Mod B"):
                f.write(json.dumps({"key": k, "rows": 1}) + "\n")
        api = web_app.Api()
        api._mb_journal_path = lambda: jp
        assert api._mb_journal_drop("Mod A") is True
        left = [json.loads(x) for x in io.open(jp, encoding="utf-8",
                                               errors="replace").read().splitlines()
                if x.strip()]
        assert [r.get("key") for r in left[1:]] == ["Mod B"], left
        assert left[0].get("__sig") == "abc", left
        assert api._mb_journal_drop("Nope") is False, "没这行也报删掉了"
    finally:
        MD.config_dir = old_cfg
        shutil.rmtree(tmp, ignore_errors=True)


def check_skip_up_to_date():
    """S6：已部署 + 源文件没变 + 上一轮没有漏译，才允许整块跳过。"""
    import mod_batch as MB
    import mod_deploy as MD

    old = MD.config_dir
    tmp = tempfile.mkdtemp(prefix="mt-s6-")
    try:
        MD.config_dir = lambda: os.path.join(tmp, "userdata")
        mod = os.path.join(tmp, "mod")
        os.makedirs(mod)
        zh = os.path.join(mod, "zh-cn.json")
        with io.open(zh, "w", encoding="utf-8") as f:
            f.write('{"All": "全部"}')
        good = {"rows": 1, "applied": 1, "untranslated": 0, "failed": 0}
        MD.record(mod, files={"zh-cn.json": {"sha1": MD.file_sha1(zh),
                                             "added": True}},
                  pairs_list=[("All", "全部")], done=good)
        assert MD.up_to_date(mod), "已部署且源未变，该跳没跳"

        # 用户解压还原了：内容变 -> 不能跳
        with io.open(zh, "w", encoding="utf-8") as f:
            f.write('{"All": "All"}')
        assert not MD.up_to_date(mod), "源文件变了还跳"

        # 上一轮还有漏译：说明这个模组没做完，不能跳
        with io.open(zh, "w", encoding="utf-8") as f:
            f.write('{"All": "全部"}')
        MD.record(mod, files={"zh-cn.json": {"sha1": MD.file_sha1(zh),
                                             "added": True}},
                  done={"rows": 2, "applied": 1, "untranslated": 1, "failed": 0})
        assert not MD.up_to_date(mod), "有漏译还跳"

        # 提取引擎换了一版（指纹对不上）：不能跳，否则新规则认出来的行永远进不了表
        MD._STAMP[:] = ["deadbeef"]
        assert not MD.up_to_date(mod), "提取引擎变了还跳"
        MD._STAMP[:] = []
        MD.record(mod, files={"zh-cn.json": {"sha1": MD.file_sha1(zh),
                                             "added": True}}, done=good)
        assert MD.up_to_date(mod), "指纹一致，该跳没跳"

        # 产物被删了：不能跳
        MD.record(mod, done=good)
        os.remove(zh)
        assert not MD.up_to_date(mod), "产物不在了还跳"

        # 部署关掉 / 只预演时，批量不该走跳过（否则这个模组什么都不产出）
        ctx_off = MB.Ctx(spawn_worker=lambda: None, out_root=tmp,
                         deploy=False, deploy_dry=False)
        ctx_dry = MB.Ctx(spawn_worker=lambda: None, out_root=tmp,
                         deploy=True, deploy_dry=True)
        assert MB._skip_reason(ctx_off, {"root": mod}) == ""
        assert MB._skip_reason(ctx_dry, {"root": mod}) == ""
    finally:
        MD.config_dir = old
        shutil.rmtree(tmp, ignore_errors=True)


def check_prefetch_pipeline():
    """S4：下一个模组的复制+加载提前做，事件顺序与结果不变。"""
    import mod_batch as MB

    tmp = tempfile.mkdtemp(prefix="mt-s4-")
    try:
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        cands = []
        for i in range(2):
            root = os.path.join(tmp, "m%d" % i)
            os.makedirs(root)
            with io.open(os.path.join(root, "a.reds"), "w", encoding="utf-8") as f:
                f.write("class A {}\n")
            cands.append({"key": "k%d" % i, "name": "m%d" % i, "root": root})

        log = []

        class W(object):
            def __init__(self, idx):
                self.idx = idx
                self.rows = []
                self._stop = threading.Event()

            def _do_load(self, dst, opts):
                log.append(("load", self.idx))
                self.rows = [{"file": "a.reds", "target": "a.reds", "line": 1,
                              "ctx": "a", "orig": "Hello there", "trans": "",
                              "kind": "text", "root": dst}]
                return {"ok": True, "rows": self.rows, "tmHits": 0}

            def _run_translate(self, todo, base, model, tgt):
                log.append(("mt", self.idx))
                for j in todo:
                    self.rows[j]["trans"] = "你好"

            def _do_apply(self, items, keep_backup=False, make_undo=False):
                log.append(("apply", self.idx))
                assert items and items[0]["trans"] == "你好", items
                return {"ok": len(items), "fail": 0, "errors": []}

        counter = itertools.count()
        ctx = MB.Ctx(spawn_worker=lambda: W(next(counter)),
                     out_root=out, deploy=False)
        events = []
        ctx.on_event = events.append
        res = MB.run(ctx, cands)
        assert len(res) == 2, res
        assert [r["key"] for r in res] == ["k0", "k1"], res
        assert all(r["ok"] and r["applied"] == 1 for r in res), res
        # 每个模组都会加载、翻译、写回；预处理线程不改变这个顺序
        for idx in (0, 1):
            assert ("load", idx) in log and ("mt", idx) in log \
                and ("apply", idx) in log, log
        assert [e["type"] for e in events if e.get("type") == "mb-item"
                and e.get("phase") == "done"] == ["mb-item", "mb-item"], events
        assert events[-1]["type"] == "mb-done" and events[-1]["done"] == 2, events[-1]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_wkit_no_pipe_hang():
    """WolvenKit 的输出不能走管道：孙进程继承写端会让调用方永久挂死。

    实测那次批量：CLI 早就退出了，Python 还堵在 communicate() 里；超时之后
    subprocess.run 会再 communicate() 一次（那次没有超时），于是永远不返回。
    现在输出写临时文件，只等主进程退出。
    """
    import localization.wkit as W

    hang = "import subprocess,sys\n" \
           "subprocess.Popen([sys.executable,'-c','import time;time.sleep(8)']," \
           " stdout=sys.stdout)\n" \
           "print('ok-line')\n"
    t0 = time.time()
    lines = W.run(sys.executable, ["-c", hang], timeout=25)
    dt = time.time() - t0
    assert "ok-line" in lines, lines
    assert dt < 5, "孙进程占着输出句柄时还等了 %.1f 秒" % dt

    t0 = time.time()
    try:
        W.run(sys.executable, ["-c", "import time;time.sleep(30)"], timeout=1)
        raise AssertionError("该超时没超时")
    except W.WkitError as ex:
        assert "超时" in str(ex), ex
    assert time.time() - t0 < 15, "超时后没及时收手"


def check_batch_flags_saved():
    """批量面板的三个勾选必须能存进设置。

    `save_settings` 是白名单式的：键不在名单里就丢掉。`batch_deploy` /
    `batch_deploy_dry` / `batch_hide_todo` 曾经漏在名单外，于是「跑完覆盖回原模组」
    勾上等于没勾——`batch_start` 读到的永远是默认 False，批量只产出副本、不回写。
    """
    old = web_app.save_settings
    saved = {}
    try:
        web_app.save_settings = lambda cfg: saved.update(cfg)
        api = web_app.Api()
        res = api.save_settings({"batch_deploy": True, "batch_deploy_dry": False,
                                 "batch_hide_todo": False})
        assert res.get("ok"), res
        assert saved.get("batch_deploy") is True, saved.get("batch_deploy")
        assert saved.get("batch_deploy_dry") is False, saved.get("batch_deploy_dry")
        assert saved.get("batch_hide_todo") is False, saved.get("batch_hide_todo")
        assert api.settings.get("batch_deploy") is True, api.settings.get("batch_deploy")
        # 名单外的键仍然不许进
        api.save_settings({"whatever": "x"})
        assert "whatever" not in saved, saved.get("whatever")

        # 部署开关要算进续跑指纹：上一轮没开部署、这一轮开了，不能整行跳过
        base = web_app.Api._mb_sig("st", False, True, "简体中文", "m", "out")
        on = web_app.Api._mb_sig("st", False, True, "简体中文", "m", "out",
                                 deploy=True, deploy_dry=False)
        assert base != on, "部署开关没进参数指纹"
    finally:
        web_app.save_settings = old


def check_pair_text_table():
    """`Key = { "标签", "描述" }` 表：短标签也要提取，数据表不许误伤。

    Immersive Third Person 的设置界面整张表都是这种形状，以前只有描述被
    「长句兜底」收走，标签永远进不了行 —— 界面上就是「一对里只译了后半」。
    """
    tmp = tempfile.mkdtemp(prefix="mt-pair-")
    try:
        root = os.path.join(tmp, "mod")
        os.makedirs(root)
        lua = (
            "local TEXT = {\n"
            "  bindToggle = { 'Switch perspective', 'Switch between first "
            "and third person.' },\n"
            "  photoModeLocked = { 'PERSPECTIVE SWITCH DISABLED IN PHOTO MODE' },\n"
            "  keyNote = 'Press a key or a controller button.',\n"
            "  distance = { 'Distance', 'Distance behind V.' },\n"
            "}\n"
            "local COLORS = { Lemon = 'FFFACD', Red = 'EF4C43', White = 'FDFBFC' }\n"
            "local PARTS = { a = { 'seat_front_left', 'seat_front_right' },\n"
            "                b = { 'player_crouch_enter', 'player_crouch_exit' },\n"
            "                c = { 'NPCStatPreset.AllDOT', 'CoverActions.X' } }\n"
        )
        with io.open(os.path.join(root, "init.lua"), "w", encoding="utf-8") as f:
            f.write(lua)
        data, _meta = E.extract_root_ex(root, keep_target=True)
        rows = [it for items in data.values() for it in items]
        origs = {o for (_l, _t, _c, o) in rows}
        for want in ("Switch perspective",
                     "PERSPECTIVE SWITCH DISABLED IN PHOTO MODE",
                     "Press a key or a controller button.", "Distance",
                     "Distance behind V."):
            assert want in origs, "%r 没被提取: %r" % (want, sorted(origs))
        for bad in ("FFFACD", "Lemon", "Red", "seat_front_left",
                    "player_crouch_enter", "NPCStatPreset.AllDOT"):
            assert bad not in origs, "%r 是数据，不该进待译行" % (bad,)
        # 描述按「描述」分类，标签按「显示名称」
        types = {o: t for (_l, t, _c, o) in rows}
        assert types["Distance behind V."] == "描述", types.get("Distance behind V.")
        assert types["Distance"] == "显示名称", types.get("Distance")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_code_token_and_safecall():
    """无空格却带代码符号的字面量不是界面文本；safeCall 的 label 只是日志标签。"""
    for bad in ("fppCamera:ResetPitch",
                "handle:GameSessionEvent",
                "TakeOverControlSystem:EnablePlayerTPPRepresenation(false)"):
        assert not E.is_translatable(bad), "%r 不该算可译文本" % (bad,)
    assert E.is_translatable("Cyberpunk: Edgerunners"), "带空格的冒号标题仍是文本"

    tmp = tempfile.mkdtemp(prefix="mt-tag-")
    try:
        root = os.path.join(tmp, "mod")
        os.makedirs(root)
        lua = (
            "local function safeCallQuiet(callback) return callback() end\n"
            "local function safeCall(label, callback, quiet)\n"
            "  return safeCallQuiet(callback)\n"
            "end\n"
            "local function switch(path, label, desc)\n"
            "  return label, desc\n"
            "end\n"
            "local function run()\n"
            "  safeCall('fppCamera:ResetPitch', function() end)\n"
            "  switch('p', 'Voice volume', 'How loud the voice is.')\n"
            "end\n"
        )
        with io.open(os.path.join(root, "init.lua"), "w", encoding="utf-8") as f:
            f.write(lua)
        data, _meta = E.extract_root_ex(root, keep_target=True)
        origs = {o for items in data.values() for (_l, _t, _c, o) in items}
        assert "fppCamera:ResetPitch" not in origs, sorted(origs)
        for want in ("Voice volume", "How loud the voice is."):
            assert want in origs, "正常包装函数的标签被一起挡掉了: %r" % (sorted(origs),)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_register_action_alias():
    """`local A = Settings.RegisterAction` 的别名要找全；查表函数别名不许当文本。

    NCPD First Response 的整个菜单都是 A(id, label, fn, desc)：不认别名就只能靠
    「长句兜底」（四个词起），于是两三词的动作名一直是英文，而已经是中文的行因为
    命中兜底的中文分支照常提取——界面上就是「一半中文一半英文」。
    反例是 MCM 的 `local t = ctx.text`：t("settings.section.general") 的实参是
    查表键，收进来就会把键翻掉（P13 那一类静默损坏），必须一条都不收。
    """
    tmp = tempfile.mkdtemp(prefix="mt-alias-")
    try:
        root = os.path.join(tmp, "mod")
        os.makedirs(root)
        lua = (
            "local Settings = require('modules/settings')\n"
            "local A = Settings.RegisterAction\n"
            "A('cruiser', 'Spawn police car', function() end, 'Picks any cruiser.')\n"
            "A('assist', 'Officer needs assistance (10-99)', function() end)\n"
            "local function backupMenu()\n"
            "  local opts = {}\n"
            "  return 'Request backup', opts\n"
            "end\n"
        )
        with io.open(os.path.join(root, "init.lua"), "w", encoding="utf-8") as f:
            f.write(lua)
        data, _meta = E.extract_root_ex(root, keep_target=True)
        rows = [it for items in data.values() for it in items]
        types = {o: t for (_l, t, _c, o) in rows}
        for want, typ in (("Spawn police car", "显示名称"),
                          ("Officer needs assistance (10-99)", "显示名称"),
                          ("Picks any cruiser.", "描述"),
                          ("Request backup", "显示名称")):
            assert want in types, "别名注册器的 %r 没被提取: %r" % (
                want, sorted(types))
            assert types[want] == typ, "%r 分类错: %r" % (want, types[want])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    tmp = tempfile.mkdtemp(prefix="mt-alias-key-")
    try:
        root = os.path.join(tmp, "mod")
        os.makedirs(root)
        lua = (
            "local ctx = { text = function(k) return k end }\n"
            "local t = ctx.text\n"
            "local menu = { label = t('settings.section.general') }\n"
        )
        with io.open(os.path.join(root, "init.lua"), "w", encoding="utf-8") as f:
            f.write(lua)
        data, _meta = E.extract_root_ex(root, keep_target=True)
        origs = {o for items in data.values() for (_l, _t, _c, o) in items}
        assert "settings.section.general" not in origs, (
            "查表键被当成界面文本了: %r" % (sorted(origs),))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_repair_markup_escaped():
    """原文的 `<Rich …>` 带 \\" 转义时，补标记不能把同一段标签再加一遍。

    SenSen 四个模组的说明文字整段变成 `<Rich …><Rich …>3% …</>`：游戏把标记
    当普通文字显示出来。原因是原文（.reds 字面量）里的引号是 `\\"`，模型的译文
    是裸引号，`lead in trans` 永远不命中，于是「补开头标签」又补了一份。
    """
    lead = ('<Rich color=\\"TooltipText.cyberwareDescriptionHighlightColor\\" '
            'style=\\"Semi-Bold\\">')
    plain = lead.replace('\\"', '"')
    src = lead + "{int_0}%</> of the damage you take comes back as RAM."

    # 模型把标记原样带回来了（裸引号）：一个字都不该补
    trans = plain + "{int_0}%</> 受到的伤害会转化为 RAM 返还。"
    fixed, lost = E.repair_markup(src, trans)
    assert fixed.count("<Rich") == 1, "标签被补了两遍: %r" % (fixed,)
    assert not lost, lost

    # 模型把开头标记删了：补一份，且引号仍是原文的转义形态
    fixed2, lost2 = E.repair_markup(src, "{int_0}%</> 受到的伤害会转化为 RAM 返还。")
    assert fixed2.count("<Rich") == 1, "该补的没补上: %r" % (fixed2,)
    assert fixed2.startswith("<Rich"), fixed2
    assert not lost2, lost2

    # 译文少了一个 </>：标记不闭合，游戏会把整段 <Rich …> 当普通文字显示
    two = ('<Rich color=\\"MainColors.Red\\">成本</>: '
           '<Rich color=\\"MainColors.Red\\">-{int_2}%</> max health.')
    trans3 = ('<Rich color="MainColors.Red">成本</>: '
              '<Rich color="MainColors.Red">-{int_2}% 最大生命值。')
    fixed3, _lost3 = E.repair_markup(two, trans3)
    assert fixed3.count("</>") == 2, "缺的关闭标记没补上: %r" % (fixed3,)
    assert fixed3.count("<Rich") == 2, fixed3


def check_cache_tools():
    """设置页「缓存清理」：统计准、只删选中的、印章和真数据不碰。

    这个功能会递归删用户目录，判据必须钉死：
      * 只删 SCOPES 里登记的目录内容，`memory.json` / `official_tm.sqlite` 一个字节不动；
      * 批量输出目录的 `.mb-out-root` 印章要留着（否则 `batch_clear` 下次拒绝认领）；
      * 不认识的 key 忽略；`../` 越界的路径直接拒绝。
    """
    import cache_tools as CT

    tmp = tempfile.mkdtemp(prefix="mt-cache-")
    try:
        base = os.path.join(tmp, "userdata")
        for rel in ("cache", "work/m1", "batch/out/modA", "undo/u1", "corpus/en",
                    "logs", "reports"):
            os.makedirs(os.path.join(base, *rel.split("/")), exist_ok=True)

        def put(rel, data):
            p = os.path.join(base, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with io.open(p, "wb") as f:
                f.write(data)

        put("cache/archive_toc.json", b"x" * 100)
        put("work/m1/a.json", b"y" * 200)
        put("work/m1/sub/b.json", b"z" * 300)
        put("batch/out/.mb-out-root", b"mark")
        put("batch/out/modA/init.lua", b"w" * 400)
        put("batch/run.jsonl", b"j" * 30)
        put("batch/last-run.json", b"r" * 40)
        put("batch/deployed.json", b"keep-me-3")
        put("undo/u1/0001.bin", b"u" * 50)
        put("corpus/en/lang.json", b"c" * 70)
        put("logs/batch.log", b"l" * 10)
        put("reports/scan.csv", b"r" * 20)
        put("memory.json", b"keep-me")
        put("official_tm.sqlite", b"keep-me-too")
        put("settings.json", b"{}")

        info = CT.scan(base)
        by_key = {it["key"]: it for it in info["items"]}
        assert by_key["work"]["bytes"] == 500, by_key["work"]
        assert by_key["work"]["files"] == 2, by_key["work"]
        assert by_key["batch"]["bytes"] == 470, by_key["batch"]
        assert by_key["logs"]["bytes"] == 30, by_key["logs"]
        # 印章算进大小，但不该被当成「产物」删掉
        assert by_key["cache"]["files"] == 1, by_key["cache"]

        res = CT.clear(["work", "batch", "nope"], base)
        assert res["ok"], res
        assert res["freed"] == 970, res
        assert not os.path.isdir(os.path.join(base, "work", "m1")), "work 没清干净"
        assert os.path.isdir(os.path.join(base, "batch", "out")), "输出目录本身不该删"
        assert os.path.isfile(os.path.join(base, "batch", "out", ".mb-out-root")), \
            "产物印章被删了，batch_clear 下次会拒绝认领"
        assert not os.path.exists(os.path.join(base, "batch", "out", "modA"))
        # 输出没了，续跑记录留着会让下一轮整块跳过 -> 必须一起删；部署清单要留
        assert not os.path.exists(os.path.join(base, "batch", "run.jsonl")), \
            "清空输出时 run.jsonl 没删，下一轮会把完成的模组全跳过"
        # 上一轮的导出报告跟着产物一起走，否则「导出结果」会导出一份已删掉产物的表
        assert not os.path.exists(os.path.join(base, "batch", "last-run.json")), \
            "清空输出时 last-run.json 没删"
        assert os.path.exists(os.path.join(base, "batch", "deployed.json")), \
            "部署清单不该被缓存清理删掉"
        # 没选中的、以及根本不是缓存的，一个都不能动
        for rel in ("undo/u1/0001.bin", "corpus/en/lang.json", "logs/batch.log",
                    "cache/archive_toc.json", "memory.json",
                    "official_tm.sqlite", "settings.json"):
            assert os.path.exists(os.path.join(base, *rel.split("/"))), \
                "误删: %s" % rel

        # 越界路径必须拒绝
        try:
            CT._abs(base, "../outside")
        except ValueError:
            pass
        else:
            raise AssertionError("_abs 没拦住 ../ 越界")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_batch_export_rows():
    """批量结果导出：一行一个 mod，取消掉的不算跑完，列数与表头对齐。

    导出的是「面板上那张表此刻的内容」，所以判据要钉住：
      * 取消的模组不出现（半截结果没有参考价值），没轮到的压根不在 list 里；
      * 状态文案与面板一致（完成 / 失败 / 已是最新 / 无译文可写）；
      * 部署那几列如实区分「已部署 / 预演 / 失败」，备注带上该看一眼的原因。
    """
    import mod_batch as MB

    ok = {"key": "k1", "name": "Mod A", "rows": 40, "todo": 12, "already": 28,
          "tm": 5, "machine": 7, "applied": 7, "failed": 0, "untranslated": 0,
          "copied": 33, "seconds": 12.5,
          "deploy": {"ok": True, "dry": False, "overwritten": 2, "added": 1,
                     "notes": [{"path": "a.reds", "why": "原件已含中文"}]}}
    cancelled = {"key": "k2", "name": "Mod B", "error": "cancelled"}
    failed = {"key": "k3", "name": "Mod C", "error": "复制后没有任何文件"}
    skipped = {"key": "k4", "name": "Mod D", "rows": 10, "todo": 0,
               "skippedDone": "已部署且源文件没动"}
    empty = {"key": "k5", "name": "Mod E", "rows": 3, "todo": 3, "applied": 0}
    dry = {"key": "k6", "name": "Mod F", "rows": 5, "todo": 5, "applied": 5,
           "deploy": {"ok": True, "dry": True, "overwritten": 0, "added": 0}}

    rows = MB.export_rows([ok, cancelled, failed, skipped, empty, dry])

    assert MB.export_status(cancelled) == "已取消", MB.export_status(cancelled)
    assert len(rows) == 5, rows
    assert len(MB.EXPORT_COLUMNS) == 16, MB.EXPORT_COLUMNS
    assert all(len(r) == len(MB.EXPORT_COLUMNS) for r in rows), rows
    assert [r[0] for r in rows] == ["Mod A", "Mod C", "Mod D", "Mod E", "Mod F"], rows
    assert [r[1] for r in rows] == ["完成", "失败", "已是最新", "无译文可写", "完成"], rows
    assert rows[0][2:12] == [40, 12, 28, 5, 7, 7, 0, 0, 33, 12.5], rows[0]
    assert rows[0][12] == "已部署" and rows[0][13] == 2 and rows[0][14] == 1, rows[0]
    assert "a.reds" in rows[0][15] and "原件已含中文" in rows[0][15], rows[0][15]
    assert rows[1][12] == "" and rows[1][13] == 0, rows[1]
    assert rows[2][15].startswith("跳过："), rows[2][15]
    assert rows[4][12] == "预演", rows[4]
    assert MB.export_rows([]) == [] and MB.export_rows(None) == []
    assert MB.export_rows([{"key": "k", "error": "cancelled"}]) == []


def check_spec_order():
    """占位符顺序：个数对、顺序错也要拦下来。

    MCM core.lua:1344 的真实事故：原文 "Applied %d change(s) for %s." 被译成
    "已为 %s 应用 %d 项更改。"，占位符一个不少，旧的校验只数个数，于是写回；
    游戏里 string.format 第 3 个参数要 %d 却拿到 mod 名，整块设置面板报
    "MCM action failed: ... bad argument #3 to 'format' (number expected, got string)"。
    """
    import mod_text_engine as M

    lvl, why = M.swapped_spec_order("Applied %d change(s) for %s.",
                                    "已为 %s 应用 %d 项更改。")
    assert lvl == "soft" and "占位符顺序变了" in why, (lvl, why)

    # 保住顺序的同一句、以及三个占位符里换了一个：前者放行, 后者照拦
    assert M.swapped_spec_order("Applied %d change(s) for %s.",
                                "已应用 %d 项更改：%s。") == ("", "")
    lvl, _ = M.swapped_spec_order("Applied %d change(s) for %s through %s",
                                  "已为 %s 通过 %s 应用 %d 项更改")
    assert lvl == "soft", lvl
    assert M.swapped_spec_order("Removed %d unavailable mod(s) from %s.",
                                "%d 个不可用的模组已从 %s 移除。") == ("", "")

    # 顺序本来就一样 / 只有一个占位符 / 没有占位符：都不报
    assert M.swapped_spec_order("%s: %d", "%s：%d") == ("", "")
    assert M.swapped_spec_order("Removed %s", "移除了 %s") == ("", "")
    assert M.swapped_spec_order("100% done", "完成 100%") == ("", "")
    # 个数都对不上是「丢失」那条检查的事, 这里不重复报
    assert M.swapped_spec_order("%d of %s", "共 %d 项") == ("", "")
    # 同类占位符互换看不出来, 不误报
    assert M.swapped_spec_order("%s gave %s", "%s 给了 %s") == ("", "")

    # 写回路径 (单文件) 也要拦住, 并且留在「可以单条覆盖」的软失败里
    lvl, why = M.guard_level("a.lua", "Applied %d change(s) for %s.",
                             "已为 %s 应用 %d 项更改。")
    assert lvl == "soft" and "占位符" in why, (lvl, why)


def check_acronym_guard():
    """代号不翻, 但字段名不背锅。

    MCM 的 provider 写着 shortName = "CET" / "RCF" / "MS"，模组列表每行前面那个
    小标签就是它：翻成中文只会变成「中央情报局」（CET 被猜成 Central Intelligence
    Agency）——缩写没有上下文，模型只能猜。ENC 的页签同理会变成「神经连接」
    （ENC = Enemies of NC 被猜成 Neural Connection）。

    判据是**值**：2-5 个字符的全大写串就是代号，不送翻译；shortName 这个字段本身
    照常收 —— 有些模组在里面写的是真文案（shortName = "Auto Drive"），按字段名
    排除会把整句遮住。
    """
    import mod_text_engine as E

    assert E.is_acronym("ENC") and E.is_acronym("CET") and E.is_acronym("RCF")
    assert E.is_acronym("MCM") and E.is_acronym("VDB")
    assert not E.is_acronym("IMPORT")        # 6 个字母: 普通词
    assert not E.is_acronym("OFF")           # 全大写但就是英文单词
    assert not E.is_acronym("ON") and not E.is_acronym("Auto Drive")

    src = (
        'local Provider = {\n'
        '  id = "cet",\n'
        '  name = "Native Settings UI",\n'
        '  shortName = "CET",\n'
        '}\n'
        'local Tool = {\n'
        '  shortName = "Auto Drive",\n'
        '}\n'
        'nativeSettings.addTab("/ENC", "ENC")\n'
        'nativeSettings.addCategory("/ENC/Overall", "Overall")\n'
        'local Other = {\n'
        '  label = "Badge Label",\n'
        '}\n'
        'local Switch = {\n'
        '  label = "OFF",\n'
        '}\n'
    )
    tmp = tempfile.mkdtemp(prefix="mt-badge-")
    try:
        p = os.path.join(tmp, "provider.lua")
        io.open(p, "w", encoding="utf-8", newline="").write(src)
        items = E.extract_from_file(p)
        texts = [x[3] for x in items]
        assert "CET" not in texts, texts          # 代号: 不收
        assert "ENC" not in texts, texts          # 页签标题也是代号
        assert "Auto Drive" in texts, texts       # shortName 里的真文案: 照收
        assert "Native Settings UI" in texts, texts
        assert "Badge Label" in texts, texts
        assert "Overall" in texts, texts
        assert "OFF" in texts, texts              # 全大写英文单词: 照收
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_spaced_loc_key():
    """带空格的查表键不是文案（`Gameplay-Items-Item Type-Prt_Fragment`）。

    Immersive Cyberware 的 `slotSupport.reds` 用 `@wrapMethod` 顶掉
    `UItemsHelper.GetSlotName`，直接 `return "Gameplay-Items-Item Type-Prt_Fragment"`。
    这是 onscreens 的键，游戏拿它去查「碎片」这个词；翻成中文就把引用改断了。

    这条只该拦「没有任何位置证据、靠像不像句子兜底」的字面量：`this.Text(key, value)`
    的 value（`Desperate Sons-of-Bitches`）同样是连字符写法，但它在 value 位置上，
    必须照收。
    """
    import mod_text_engine as E

    assert E.is_spaced_loc_key("Gameplay-Items-Item Type-Prt_Fragment")
    assert not E.is_spaced_loc_key("Part - Engine - Turbo")      # 人写的标签
    assert not E.is_spaced_loc_key("HUD-Integrated Optics Module")  # 只有一处连字符
    assert not E.is_spaced_loc_key("Displays your health bar.")

    reds = (
        '@wrapMethod(UIItemsHelper)\n'
        'public final static func GetSlotName(slotID: TweakDBID) -> String {\n'
        '    if (UIItemsHelper.IsEyeSlot(slotID)) {\n'
        '        return "Gameplay-Items-Item Type-Prt_Fragment";\n'
        '    }\n'
        '    return wrappedMethod(slotID);\n'
        '}\n'
        'public class Foo {\n'
        '    private func Bar() -> Void {\n'
        '        this.Text("MBC-Wire-CrimeRcr18", "Desperate Sons-of-Bitches");\n'
        '        let d: String = "In the grim darkness of the near future, there are only ads.";\n'
        '    }\n'
        '}\n'
    )
    lua = (
        'local function slotName(slot)\n'
        '  if slot then\n'
        '    return "Gameplay-Items-Item Type-Prt_Fragment"\n'
        '  end\n'
        '  return wrapped(slot)\n'
        'end\n'
    )
    tmp = tempfile.mkdtemp(prefix="mt-spaced-key-")
    try:
        p = os.path.join(tmp, "slotSupport.reds")
        io.open(p, "w", encoding="utf-8", newline="").write(reds)
        texts = [x[3] for x in E.extract_from_file(p)]
        assert "Gameplay-Items-Item Type-Prt_Fragment" not in texts, texts
        assert "Desperate Sons-of-Bitches" in texts, texts          # this.Text 的 value
        assert ("In the grim darkness of the near future, there are only ads."
                in texts), texts

        q = os.path.join(tmp, "slot.lua")
        io.open(q, "w", encoding="utf-8", newline="").write(lua)
        lua_texts = [x[3] for x in E.extract_from_file(q)]
        assert "Gameplay-Items-Item Type-Prt_Fragment" not in lua_texts, lua_texts
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_archive_prefilter():
    """归档预筛不能把「路径里没有 localization」的本地化表挡掉。

    Immersive Cyberware 的本地化表在
    `phoenicia\\immersivecyberware\\translations\\translation_strings.json`，
    旧判据只认 localization / l10n / onscreens 三个路径段，于是这个归档在解包前
    就被判成「没有本地化」，43 条物品名与描述（`Holo Display: Ads` 那句）永远
    进不了表。现在判据是「归档里有没有 .json」。
    """
    from localization import wkit as W

    toc = ["phoenicia\\immersivecyberware\\icons\\preview_icons.xbm",
           "phoenicia\\immersivecyberware\\translations\\translation_strings.json",
           "phoenicia\\immersivecyberware\\icons\\preview_icons.inkatlas"]
    assert W.localization_files(toc) == [toc[1]], W.localization_files(toc)
    # 纯资源归档: 一个 json 都没有, 预筛照样挡住, 不白解包
    assert W.localization_files(["a\\b.xbm", "a\\c.inkatlas", "a\\d.mesh"]) == []
    assert W.localization_files([]) == []

    # 判据改过要作废旧缓存: 键里必须带版本号, 否则老归档一直命中旧的 False
    tmp = tempfile.mkdtemp(prefix="mt-toc-")
    try:
        p = os.path.join(tmp, "x.archive")
        io.open(p, "wb").write(b"RDAR" + b"\x00" * 128)
        key = W._toc_key(p)
        assert key.startswith(W._TOC_KEY_VERSION + "|"), key
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_i18n_coverage():
    """界面用到的每个中文 key 都必须有英文译文，占位符不能丢。

    文本即键（locales/*.json）：源码里 `tr("…")` / `T("…")` / `data-i18n` 写的
    中文原文就是查表键。少一条就退回中文——界面上看着「翻了一半」，很难发现。
    """
    import i18n

    root = os.path.dirname(HERE)
    src = os.path.join(root, "src")
    cjk = re.compile(r"[\u4e00-\u9fff]")
    en = json.load(io.open(os.path.join(src, "locales", "en-US.json"),
                           encoding="utf-8-sig"))["strings"]

    esc = re.compile(r"\\(u[0-9a-fA-F]{4}|n|t|r|\"|'|\\|/)")

    def unesc(s):
        def rep(m):
            g = m.group(1)
            return {"n": "\n", "t": "\t", "r": "\r"}.get(g) or (
                chr(int(g[1:], 16)) if g.startswith("u") else g)
        return esc.sub(rep, s)

    used = {}
    html = io.open(os.path.join(src, "web", "index.html"),
                   encoding="utf-8").read()
    for k in re.findall(r'data-i18n(?:-ph)?="([^"]+)"', html):
        used.setdefault(k, "index.html")
    js = io.open(os.path.join(src, "web", "app.js"), encoding="utf-8").read()
    for k in re.findall(r'\bT\(\s*"((?:[^"\\]|\\.)*)"\s*\)', js):
        used.setdefault(unesc(k), "app.js")
    py = io.open(os.path.join(src, "web_app.py"), encoding="utf-8").read()
    for k in re.findall(r'(?:_tr|(?<![A-Za-z_])tr)\(\s*"((?:[^"\\]|\\.)*)"', py):
        used.setdefault(unesc(k), "web_app.py")

    missing = sorted(k for k in used if cjk.search(k) and k not in en)
    assert not missing, "缺少英文译文 %d 条: %r" % (len(missing), missing[:10])

    # 占位符必须原样保留，否则 .replace("%d", …) 会静默失败
    bad = []
    for k in used:
        if k not in en:
            continue
        for ph in set(re.findall(r"%[ds]", k)):
            if k.count(ph) != en[k].count(ph):
                bad.append((k, ph))
                break
    assert not bad, "占位符数量对不上: %r" % (bad[:5],)

    # 语言目录本身要能被加载（code/name/strings 齐全）
    i18n.load_locales(force=True)
    langs = dict(i18n.available())
    assert "en-US" in langs and langs.get("en-US") == "English", langs
    assert i18n.set_language("en-US") == "en-US"
    assert i18n.tr("失败") == "Failed", i18n.tr("失败")
    assert i18n.tr("这个中文没有译文") == "这个中文没有译文", "缺译文要退回中文"
    i18n.set_language("zh-CN")
    assert i18n.tr("失败") == "失败"


def check_official_weak_words():
    """语言包里的单词标签不采用官方弱匹配（Body→尸体 / Black→布莱克）。"""
    import baseline as Lbase

    class _Off(object):
        def __init__(self, mapping):
            self.mapping = mapping

        def lookup_many(self, texts, detailed=True):
            return {Lbase._normalize(t): self.mapping[t]
                    for t in texts if t in self.mapping}

        def close(self):
            pass

    off = _Off({"Body": {"dst": "尸体", "distinct": 1},
                "Black": {"dst": "布莱克", "distinct": 1},
                "Crosshair": {"dst": "准星", "distinct": 1},
                "Paint job": {"dst": "涂装", "distinct": 1}})
    rows = [{"orig": "Body", "kind": "json", "trans": ""},
            {"orig": "Black", "kind": "json", "trans": ""},
            {"orig": "Crosshair", "kind": "json", "trans": ""},
            {"orig": "Paint job", "kind": "json", "trans": ""},
            {"orig": "Body", "kind": "source", "trans": ""}]
    st = Lbase.fill_rows(rows, official=off, memory=None)
    assert not rows[0]["trans"], "语言包单词条不该采用官方弱匹配: %r" % (rows[0],)
    assert not rows[1]["trans"], "%r" % (rows[1],)
    assert not rows[2]["trans"], "%r" % (rows[2],)
    assert rows[3]["trans"] == "涂装", rows[3]
    assert rows[4]["trans"] == "尸体", "源码层单词条保持原行为: %r" % (rows[4],)
    assert st["left"] == 3 and st["official"] == 1, st

    # 记忆里已经存下的 official-weak 也不许顶上来（否则下一轮又填回尸体）
    class _Mem(object):
        def get_entry(self, src, lang):
            if src == "Body":
                return {"text": "尸体", "origin": "official-weak"}
            return None

    rows2 = [{"orig": "Body", "kind": "json", "trans": ""}]
    Lbase.fill_rows(rows2, official=None, memory=_Mem())
    assert not rows2[0]["trans"], rows2[0]


def main():
    print("== 回填自测 ==")
    check_apply_roundtrip()
    print("   OK: 提取/回填/引号保留/##ID 保留")
    check_codeware_text()
    print("   OK: Codeware this.Text 只取 value / 单词值 / 多行写法")
    check_target_language_skip()
    print("   OK: 提取跳过当前目标语言文本/语言包文件")
    check_wrapper_and_notify_extraction()
    print("   OK: 包装函数/通知/提示表/message 拼接提取")
    check_reds_locale_replace_all()
    print("   OK: StrReplaceAll 多语言表按目标语言提取/回填")
    check_lua_apply_with_comments()
    print("   OK: Lua 注释/长字符串后的文本可正常回填")
    check_apply_failure_details()
    print("   OK: 回填失败明细")
    check_clear_and_skip_features()
    print("   OK: 清空当前结果/译文、跳过翻译与 Excel 跳过列")
    check_translate_pipeline()
    print("   OK: 机翻工作线程 / mt_client 导入 / rows-progress-done 事件")
    check_translate_dedup()
    print("   OK: 同文去重 (同一句只发一次模型 / 重复行共用译文)")
    check_translate_retry_and_echo()
    print("   OK: 空译文与 echo 单条补翻 / 占位符密集串不进批量")
    check_codeware_provider_apply()
    print("   OK: Codeware 翻译文件类名 / Provider 自动注册")
    check_provider_pairs_and_undo()
    print("   OK: Provider 自动配对 / 精确写回 / 撤销上次应用")
    check_context_copy_ui()
    print("   OK: 右键菜单复制单元格 / 三类分组 / 文本失焦结算")
    check_resource_outdir_and_undo()
    print("   OK: 归档产物写在 .archive 旁边 / 资源模式撤销能删掉产物")
    check_mixed_single_undo()
    print("   OK: 混合批次单条撤销记录 / 还原翻译入口")
    check_revert_source()
    print("   OK: 还原翻译 (下载包取原件 / 删掉新建产物 / FOMOD 不用包)")
    check_archive_open_dir()
    print("   OK: 归档行定位到归档所在目录 (内部路径不再当磁盘路径拼)")
    check_locale_variant()
    print("   OK: 语言包变体 (languages/ 目录 / -sms 后缀 / jp-kr-ua-th-es-mx)")
    check_already_rows()
    print("   OK: 已译行保留 (翻完再提取不丢行 / 译文可改)")
    check_search_scoped()
    print("   OK: 搜索覆盖译文 (原文/译文/文件名/上下文)")
    check_reds_evidence_tiers()
    print("   OK: REDscript 证据分级 (界面/长句/内部日志)")
    res = check_layered_extraction()
    print("   OK: 语言包 + 源码设置文本分层合并"
          if res is True else "   跳过: 未安装 Much Better Contracts")
    check_noise_rules()
    print("   OK: 误报过滤 (图集/记录名/技术串/外文表/双语表)")
    check_trans_guard()
    print("   OK: 机翻互斥 (load_locale/clear_rows 拒绝 busy) / 结算用快照")
    check_clear_resync()
    print("   OK: 清空后能重翻 (后端同步 / 记忆失效 / 原文=译文筛选)")
    check_locale_pair_by_key()
    print("   OK: 语言包按条目名回填 (缺条目留空 / 单词按钮 / 中文含斜杠)")
    check_apply_ctx_backfill()
    print("   OK: 应用时补条目名 (前端不带 ctx 也能按键写回)")
    check_lua_lang_sections()
    print("   OK: lua 多语言段 (只取 en + 目标语言 / 段外不受影响)")
    check_prop_loc_key()
    print("   OK: @runtimeProperty 两段式查表键 (不再当成界面文本)")
    check_pair_existing()
    print("   OK: 归档自带中文当基线 (占位英文/缺条目不误判)")
    check_locale_pack_names()
    print("   OK: 语言包类名 (沿用 Provider 命名 / 撞名就放弃改名)")
    check_locale_pack_copy_guard()
    print("   OK: 语言包整文件复制守卫 (混写文件拒绝复制)")
    check_reds_interpolation()
    print(r'   OK: s"..." 插值串 (嵌套引号不截断 / 碎片判噪音)')
    check_code_fragment_guard()
    print("   OK: 代码碎片写回守卫 (拒绝写盘 / 骨架不变 / 逗号开头与目标语言放行)")
    check_parse_and_locale_robustness()
    print("   OK: 非 ASCII 标识符不吞文件 / 解析失败可计数 / 目标语言码大小写无关")
    check_position_and_data_row_guards()
    print("   OK: 位置证据 (方法名/插槽名/能力清单) 与竖线数据行不进候选")
    check_soft_hard_and_isolation()
    print("   OK: soft/hard 失败分级 / 单条覆盖 / 一条坏行不干掉整批")
    check_user_skip_mark()
    print("   OK: 「标记为代码」持久化 (按路径+原文 sha1, 分模组隔离)")
    check_skip_persist()
    print("   OK: 「跳过」勾选持久化 (下次加载默认勾上 / 取消即删 / 与代码标记分开)")
    check_short_loc_key()
    print("   OK: 两段式查表键 (GR-UiHomes) 不收, this.Text 的 value 照收")
    check_getter_literal_labels()
    print("   OK: getLabel/getActionLabel 直接返回字面量的显示名进表")
    check_identifier_guard()
    print("   OK: 标识符/本地化键守卫 (n\"\" 引用与查表键不参与翻译)")
    res3 = check_resource_layer()
    print("   OK: 归档 localization 并入第三层"
          if res3 is True else "   跳过: 未安装 Auto Drive Enhanced")
    check_apply_dispatch()
    print("   OK: 应用按层分流 (归档组包 / 明文回写 / 混合批次)")
    check_resource_entry_id()
    print("   OK: 归档行主键 (items 带 entryId / 缺主键按原文唯一反查)")
    check_vortex_map()
    print("   OK: 活动映射 (两种命名格式/版本归一/部署清单/陈旧判定)")
    check_baseline_layer()
    print("   OK: 官方基线 TM (手改>官方>机翻 / 目标语言守卫 / 命中不发模型)")
    check_translate_report()
    print("   OK: 机翻失败回执 (已翻译/未翻译/失败原因)")
    check_mt_budget()
    print("   OK: 输出上限 (每请求必带 max_tokens / 随长度伸缩 / 单条也有)")
    check_mt_warmup()
    print("   OK: 模型预热 (原生 /api/chat + keep_alive / 非 Ollama 后端不报错)")
    check_mod_batch()
    print("   OK: 批量复制 (跳过贴图/音频, 归档按需) / 名字清洗 / 会话隔离")
    check_markup_repair()
    print("   OK: 译文标记/占位符守恒 (<Rich ...>/</> 补回, %s 换回 {value}, 丢失拦下)")
    check_apply_hygiene()
    print("   OK: 批量写回不留 .bak / 不建撤销记录 / 标记丢失的行不写")
    check_package_class_langs()
    print("   OK: 多语言包类只取源语言段 / Provider 只取源语言类块")
    check_own_product_and_prefilter()
    print("   OK: 自己的翻译包不被当源 / 只有产物时拒绝 / 归档预筛口径")
    check_deploy_and_manifest()
    print("   OK: 按文件覆盖 / 预演不写盘 / 清单记 added+sha1 / 还原后被重新读回")
    check_build_skips_broken_resource()
    print("   OK: 资源转换崩溃重试一次 / 只跳过那一个 / .xl 不声明被跳过的")
    check_batch_todo_filter()
    print("   OK: 批量面板折叠「无需翻译」/ 读取失败的不藏 / 不随扫描重置")
    check_dup_key_writeback()
    print("   OK: 同名条目只算一次但每处都写 / 不合并行 / 括号结构不变")
    check_archive_lang_filter()
    print("   OK: 归档只序列化源/目标语言 / 认不出语言的保留")
    check_toc_cache_key()
    print("   OK: 目录缓存键扛得住复制（不带 mtime）/ 内容变则换键")
    check_mt_batch_retry()
    print("   OK: 批量解析失败先整批重试一次再逐条")
    check_package_if_guard()
    print("   OK: 抽语言包类保留 import 前的 @if 守卫 / 函数注解不搬")
    check_json_label_words()
    print("   OK: json 单个单词也提取 (数据值仍不收) / 缺键判断")
    check_settled_no_rewrite()
    print("   OK: 目标文件里已有的译文不再重写 (settled)")
    check_retranslate_forget()
    print("   OK: 重译按钮 (只删机翻记忆 / 删掉续跑记录那行)")
    check_skip_up_to_date()
    print("   OK: 已部署且源未变才整块跳过 (S6)")
    check_prefetch_pipeline()
    print("   OK: 预处理流水线 (S4)：下一个模组提前加载，结果与事件不变")
    check_wkit_no_pipe_hang()
    print("   OK: WolvenKit 输出不走管道 (孙进程占句柄也不挂) / 超时连树杀掉")
    check_batch_flags_saved()
    print("   OK: 批量面板三个勾选能存进设置 (覆盖回原模组/预演/折叠无需翻译)")
    check_pair_text_table()
    print("   OK: 成对文本表 Key = { 标签, 描述 } 也提取 / 数据表不误伤")
    check_code_token_and_safecall()
    print("   OK: 代码符号字面量不是文本 / safeCall 的 label 不进表")
    check_register_action_alias()
    print("   OK: 注册器别名 (A = Settings.RegisterAction) 找全 / 查表函数别名不进表")
    check_repair_markup_escaped()
    print("   OK: <Rich …> 的 \\\" 转义不会让补标记补两遍")
    check_cache_tools()
    print("   OK: 缓存清理只删选中的目录 / 印章与记忆文件不动")
    check_batch_export_rows()
    print("   OK: 批量结果导出 (取消的不导出 / 状态与部署列对得上)")
    check_spec_order()
    print("   OK: 占位符顺序变了不写回 (MCM core.lua 那类 format 崩)")
    check_acronym_guard()
    print("   OK: 代号不翻 (CET/ENC), 但 shortName 里的真文案照收")
    check_archive_prefilter()
    print("   OK: 归档预筛认 .json, translations 目录下的本地化表不被挡掉")
    check_spaced_loc_key()
    print("   OK: 带空格的查表键不收, this.Text 的 value 照收")
    check_official_weak_words()
    print("   OK: 语言包单词条不采用官方弱匹配 (Body→尸体 这类)")
    check_i18n_coverage()
    print("   OK: 界面中文 key 都有英文译文 / 占位符原样保留")

    if not os.path.isdir(VORTEX):
        print("\n未找到 Vortex 目录, 跳过真实模组测试: %s" % VORTEX)
        return 0

    failed = 0
    for keyword, expect, note in CASES:
        root = find_mod(keyword)
        if not root:
            print("\n跳过(未安装): %s" % keyword)
            continue
        data, meta = E.extract_root_ex(root)
        n = sum(len(v) for v in data.values())
        bad = check_false_positive(data)
        print("\n== %s ==" % note)
        print("   目录  : %s" % os.path.basename(root))
        print("   条数  : %d  %s" % (n, meta["types"]))
        print("   误报  : %d" % len(bad))
        if expect is not None and n != expect:
            print("   !! 期望 %d 条, 实际 %d 条" % (expect, n))
            failed += 1
        if bad:
            for f, it in bad[:5]:
                print("   !! 误报:", f, it)
            failed += 1
        if "MCM" in note and any(f.endswith(".reds") for f in data):
            print("   !! MCM 的 .reds 不应产出任何条目")
            failed += 1

    print("\n%s" % ("全部通过" if failed == 0 else "失败 %d 项" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

