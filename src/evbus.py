# -*- coding: utf-8 -*-
"""evbus.py —— 批量进度的本机事件出口（127.0.0.1 回环 HTTP）。

为什么要这个：pywebview 的 JS 桥每调用一次 API 都会新开一个线程，跑完还要
在同一个线程里 evaluate_js 把返回值写回界面（webview/util.py 的
js_bridge_call），也就是 pythonnet 每次都要走进 WebView2 的 COM。批量跑到
几十个 mod 时这种跨线程 COM 往返会偶发 0xc0000005（三次崩溃转储都落在
python314.dll，异常地址分别是 0x10 / 0xC0 / -1，见 docs）。

所以批量进度不再走桥：后端线程只往 deque 里 append，前端用 fetch 从这个
只读端口取。全程不碰 COM，也不占用固定端口（bind 到 0 由系统分配）。

安全边界：只听 127.0.0.1（外部网卡访问不到），只提供 GET /poll，
每次启动生成一次性 token，路径不带 token 一律 404。
"""
import collections
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


class EventBus(object):
    """线程安全的事件队列 + 一个只读的 HTTP 出口。"""

    def __init__(self, maxlen=20000):
        # deque 的 append/popleft 在 GIL 下是原子操作，跨线程不用额外加锁。
        self.q = collections.deque(maxlen=int(maxlen))
        self._token = secrets.token_hex(16)
        self._port = 0
        self._srv = None
        self._thread = None
        self._probe = None          # 返回「后端是否还在跑」的函数

    # ------------------------------------------------------------ 生产端
    def push(self, obj):
        """任意线程可调。队列满了由 deque 自己丢最老的。"""
        try:
            self.q.append(obj)
        except Exception:
            pass

    def clear(self):
        try:
            self.q.clear()
        except Exception:
            pass

    # ------------------------------------------------------------ 消费端
    def drain(self, limit=300):
        out = []
        limit = int(limit or 300)
        while len(out) < limit:
            try:
                out.append(self.q.popleft())
            except IndexError:
                break
        return out

    def poll(self, limit=300):
        return {"ok": True, "events": self.drain(limit),
                "running": self.running()}

    def running(self):
        f = self._probe
        if not f:
            return False
        try:
            return bool(f())
        except Exception:
            return False

    def set_running_probe(self, fn):
        self._probe = fn

    # ------------------------------------------------------------ 服务端
    def start(self):
        """起服务（幂等），返回前端要 fetch 的 URL。失败返回空串。"""
        if self._srv is not None:
            return self.url
        try:
            bus = self
            token = self._token

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, *a):     # 别往 stderr 刷访问日志
                    pass

                def do_OPTIONS(self):
                    self._reply(204, None)

                def do_GET(self):
                    try:
                        u = urlparse(self.path)
                        if u.path != "/poll":
                            self._reply(404, {"ok": False, "error": "not found"})
                            return
                        qs = parse_qs(u.query)
                        if (qs.get("token") or [""])[0] != token:
                            self._reply(404, {"ok": False, "error": "not found"})
                            return
                        try:
                            limit = int((qs.get("limit") or ["300"])[0])
                        except Exception:
                            limit = 300
                        limit = max(1, min(limit, 2000))
                        self._reply(200, bus.poll(limit))
                    except Exception:
                        try:
                            self._reply(500, {"ok": False, "error": "internal"})
                        except Exception:
                            pass

                def _reply(self, code, obj):
                    body = b"" if obj is None else json.dumps(
                        obj, ensure_ascii=False).encode("utf-8")
                    self.send_response(code)
                    if obj is not None:
                        self.send_header("Content-Type",
                                         "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    # 页面是 html= 直接喂给 WebView2 的（about:blank 源），
                    # 跨源取回环需要放行；本来也只是本机只读进度。
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Access-Control-Allow-Headers", "*")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if body:
                        self.wfile.write(body)
                    self.close_connection = True

            srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            srv.daemon_threads = True
            self._srv = srv
            self._port = int(srv.server_address[1])
            t = threading.Thread(target=srv.serve_forever, name="evbus",
                                 daemon=True)
            t.start()
            self._thread = t
        except Exception:
            self._srv = None
            self._port = 0
        return self.url

    def stop(self):
        srv = self._srv
        self._srv = None
        self._port = 0
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass

    @property
    def url(self):
        if not self._port:
            return ""
        return "http://127.0.0.1:%d/poll?token=%s&limit=300" % (
            self._port, self._token)
