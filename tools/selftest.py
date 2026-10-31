# -*- coding: utf-8 -*-
"""
回归自测: 检查提取引擎在已知模组上的表现。

    python tools/selftest.py
    python tools/selftest.py --vortex "E:\\Vortex Mods\\cyberpunk2077"

没有测试模组时只做「语法/基本行为」自检, 不会失败。
"""
import io
import os
import shutil
import sys
import tempfile

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
    ("Anti-Tracking Breach-27505", 67, "REDscript @runtimeProperty"),
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
        idx, _line, orig, _trans, reason = failed[0]
        assert idx == 1 and orig == "Missing source line", failed
        assert reason, failed
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
    assert "def _do_apply_source(self, items, tx=None)" in src
    assert "_apply_resource(res_items, tx=tx)" in src
    assert "_do_apply_source(src_items, tx=tx)" in src
    # 事务必须在两层都跑完之后才提交，否则先提交的那层盖不住后写的文件
    seg = src[src.index("def _apply_mixed"):]
    seg = seg[:seg.index("def _do_apply_source")]
    assert seg.index("_do_apply_source(src_items, tx=tx)") < seg.index("tx.commit()"), \
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


def _check_layered_extraction_in(root, tmp):
    work = os.path.join(tmp, os.path.basename(str(root).rstrip("\\/")))
    shutil.copytree(root, work)
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

    # 设置界面文本: 键名/描述必须都在, 而且是强证据类型
    lua_rows = [x for x in rows if x["file"].endswith("nativeSettingsUI.lua")]
    got = {(x["type"], x["orig"]) for x in lua_rows}
    for want in (("显示名称", "Enable Debug Log"),
                 ("描述", "Master toggle for debug output. When off, "
                          "only ERROR-level emits."),
                 ("显示名称", "Debug Log Level"),
                 ("分类", "Much Better Contracts")):
        assert want in got, (want, sorted(got))

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

        def fake_res(items, tx=None):
            calls.append(("res", len(items)))
            seen.append(tx)
            if tx is not None:
                tx.snapshot(os.path.join(tmp, "archive", "pc", "mod", "a.archive"))
            return {"ok": len(items), "fail": 0, "errors": [], "canInstall": True,
                    "archive": "a.archive", "xl": "a.xl"}

        def fake_src(items, tx=None):
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

