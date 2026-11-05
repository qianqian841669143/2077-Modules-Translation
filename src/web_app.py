# -*- coding: utf-8 -*-
"""
web_app.py —— pywebview 版前端(HTML/CSS/JS) + Python 后端

界面在 web/ 下, 通过 js_api 调用这里的方法; 长任务用 evaluate_js 回推事件。
"""
import faulthandler
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

import webview

import i18n
import evbus
from appconfig import (VERSION, DEFAULT_SETTINGS, TARGET_LANGS, config_dir,
                       config_file, load_settings, save_settings,
                       normalize_api_base, disp_path, legacy_batch_out_dir)


def web_dir():
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "web")


def build_html(theme="light"):
    """把 index.html + style.css + app.js 合成单页, 交给 pywebview 直接渲染
    (比走本地 http 服务更稳, 也免去端口占用)"""
    d = web_dir()
    with open(os.path.join(d, "index.html"), "r", encoding="utf-8-sig") as f:
        html = f.read()
    if theme == "dark":
        # 在 WebView 开始绘制前就套用深色主题，避免启动时先闪一下白底。
        html = html.replace('data-theme="light"', 'data-theme="dark"', 1)
    with open(os.path.join(d, "style.css"), "r", encoding="utf-8-sig") as f:
        css = f.read()
    with open(os.path.join(d, "app.js"), "r", encoding="utf-8-sig") as f:
        js = f.read()
    html = html.replace('<link rel="stylesheet" href="style.css">',
                        "<style>\n%s\n</style>" % css)
    html = html.replace('<script src="app.js"></script>',
                        "<script>\n%s\n</script>" % js)
    return html


class _MergedReport(object):
    """多个归档各自校验后的合并报告（字段与 verifier 的报告对齐）。"""

    def __init__(self, ok, problems):
        self.ok = bool(ok)
        self.problems = list(problems or [])

    def as_dict(self):
        return {"ok": self.ok, "problems": list(self.problems)}


def fill_resource_entry_ids(items, rows):
    """给缺 entryId 的归档行按 (file, 原文) 反查补齐，返回仍缺主键的条数。

    归档条目用 entryId 做主键。前端历史上只传了 file/原文，少了主键，组包时
    一条都对不上——整批以「没有可提交的译文」失败，用户看到的却是「归档读取
    之后应用失败」，方向完全错了。这里做一层兜底：只认唯一命中，同一个文件里
    同原文有多条时没法判断是哪一条，宁可留着让调用方报错。
    """
    index = {}
    for r in rows or ():
        eid = r.get("entryId")
        if not eid:
            continue
        key = (str(r.get("file") or ""), str(r.get("orig") or ""))
        index.setdefault(key, set()).add(str(eid))
    for it in items or ():
        if it.get("entryId"):
            continue
        key = (str(it.get("file") or ""), str(it.get("orig") or ""))
        hits = index.get(key) or ()
        if len(hits) == 1:
            it["entryId"] = next(iter(hits))
    return sum(1 for it in (items or ()) if not it.get("entryId"))


class Api(object):
    def __init__(self, initial_path=""):
        ok = bool(initial_path) and (os.path.isdir(initial_path)
                                     or initial_path.lower().endswith(".zip"))
        self.initial_path = initial_path if ok else ""
        self.settings = load_settings()
        lang = self.settings.get("lang") or i18n.detect_system()
        if lang not in dict(i18n.available()):
            lang = "zh-CN"
        self.lang = i18n.set_language(lang)
        if not self.settings.get("lang"):
            cfg = dict(self.settings)
            cfg["lang"] = self.lang
            try:
                save_settings(cfg)
            except Exception:
                pass
            self.settings = cfg
        # 翻译记忆和术语表体积可能很大，改为第一次真正使用时再读取，
        # 避免启动阶段被磁盘 JSON 解析拖慢。
        self._memory = None
        self._memory_init = threading.Lock()
        self._glossary = None
        self._terms = None
        self.rows = []
        self.meta = {}
        self.root = ""          # 实际工作目录(压缩包会解到临时目录)
        self.archive = ""       # 原始压缩包路径
        # 资源模式（.archive / CR2W）的会话与产物；源码模式下保持 None
        self.l10n_session = None
        self.l10n_result = None
        # 上一次资源应用落在哪个 mod 根（撤销时用；单独打开归档时 self.root 为空）
        self.l10n_undo_root = ""
        # 还原预览的缓存：确认框点过之后直接复用，不再重扫一遍下载目录
        self._revert_cache = None
        # pywebview 启动时会递归遍历 js_api 的所有公开属性并注入到 JS。
        # 窗口对象必须保存为 _window；若保存成公开的 window，会被当成
        # 可递归对象继续遍历 WebView2 COM 属性，导致启动卡死/界面不刷新。
        self._window = None
        self._busy = False
        self._stop = threading.Event()
        self._maxed = False
        # 批量（单模组功能）：每个 mod 派生一个独立会话（spawn_worker），
        # 表格那边的 self.rows 完全不受影响；这里只留队列自己的标志。
        self._mb_cancel = threading.Event()
        self._mb_running = False
        self._mb_scanning = False
        self._mb_results = []
        self._mb_items = []
        self._mb_worker = None
        # S4 之后同一时刻可能有两个会话在跑（一个加载下一个模组、一个在翻当前
        # 模组），取消时要把它们都喊停，否则被取消的那一轮还在后台复制。
        self._mb_workers = []
        # 批量/扫描的事件队列：后台线程只往里塞，前端从 evbus 的只读 HTTP
        # 端口取走（fetch）。不再走 pywebview 的 JS 桥——桥每调一次都要新开
        # 线程再 evaluate_js 回写，跑到几十个 mod 会偶发 0xc0000005。
        # batch_poll 保留为兜底（fetch 被拦时前端退回它），两者共用这条 deque。
        self._bus = evbus.EventBus()
        self._bus.set_running_probe(lambda: bool(self._mb_running or
                                                 self._mb_scanning))
        self._mb_q = self._bus.q
        # Vortex 三层映射（下载包 / 安装缓存 / 游戏部署）的扫描缓存。
        # 目录一变就作废；只读不写，不会影响 Vortex。
        self._vmap = None

    # ------------------------------------------------------------ 延迟加载
    # 这两个属性也必须保持私有，避免 pywebview 在启动时触发磁盘加载
    # 并继续递归遍历 Memory / Glossary 对象。
    @property
    def _memory_store(self):
        # 批量线程与界面线程可能同时第一次用到记忆（S4 里一个在加载下一个模组、
        # 一个在翻当前模组）。不做保护就会各自构造一个 Memory：两份独立的字典，
        # 谁后落盘谁把对方的条目盖掉——不坏档，但等于丢翻译。
        if self._memory is None:
            with self._memory_init:
                if self._memory is None:
                    from tm import Memory
                    mem = Memory()
                    if mem.load_error:
                        # 记忆档读不动时会改名留证并从这里报一声：批量日志会被
                        # 下一轮覆盖，crash.log 留得住。
                        _note_log("翻译记忆读档失败：%s" % mem.load_error)
                    self._memory = mem
        return self._memory

    @property
    def _glossary_store(self):
        if self._glossary is None:
            from glossary import Glossary
            self._glossary = Glossary()
        return self._glossary

    @property
    def _term_hints(self):
        """官方语料里挖出的术语表（首次使用才读盘）。库不存在时返回 None。"""
        if self._terms is None:
            try:
                from localization import official_tm as Lotm
                th = Lotm.TermHints()
                if th.load():
                    self._terms = th
                else:
                    self._terms = False
            except Exception:
                self._terms = False
        return self._terms or None

    def _deploy_scan_roots(self):
        """查「语言包类名被谁占了」时要额外扫的根目录：部署侧的 r6\\scripts。

        批量里每个模组一份副本（`batch\\out\\<mod>`），副本之间互相看不见；
        而 ICA 那类套件是「三个子模组共用一个 module」，类名撞车只有把部署侧
        一起算进来才发现得了（v2.52.4，见 locale_pack._unique_class_name）。
        """
        gd = (self.settings.get("game_dir") or "").strip()
        if not gd:
            return ()
        p = os.path.join(gd, "r6", "scripts")
        return (p,) if os.path.isdir(p) else ()

    # ------------------------------------------------------------ 工具
    def _emit(self, obj):
        if not self._window:
            return
        try:
            payload = json.dumps(obj, ensure_ascii=False)
            self._window.evaluate_js("window.__evt && window.__evt(%s)" % payload)
        except Exception:
            pass

    def _tr(self, s):
        return i18n.tr(s)

    # ------------------------------------------------------- 官方译文基线
    #
    # 优先级：手改 > 官方基线 > 机翻记忆。
    # 三条路径（源码模式 / 资源模式 / 机翻前）都从这里过，保证顺序一致——
    # 以前只有资源模式和批量队列套官方语料，源码模式完全没套，白丢了一批
    # 现成的官方译法。

    def _baseline_fill(self, rows, use_memory=None, use_official=None):
        """把译文填进 rows，返回统计 dict（total/official/manual/memory/left）。"""
        import baseline as Lbase
        tgt = self.settings.get("target_lang", "简体中文")
        if use_memory is None:
            use_memory = bool(self.settings.get("use_tm", True))
        if use_official is None:
            use_official = bool(self.settings.get("use_official_tm", True))
        official, why = Lbase.load_official(use_official, tgt)
        try:
            stats = Lbase.fill_rows(
                rows,
                official=official,
                memory=self._memory_store if use_memory else None,
                memory_lang=tgt,
                allow_weak=bool(self.settings.get("official_weak", True)))
        except Exception as ex:
            # A broken baseline must not take the whole load down. Fall back to
            # "nothing filled" and surface the reason instead.
            stats = {"total": len(rows or []), "official": 0, "officialWeak": 0,
                     "memory": 0, "kept": 0, "left": len(rows or []),
                     "manual": 0}
            if not why:
                why = "基线填充异常：%s" % ex
        finally:
            if official is not None:
                try:
                    official.close()
                except Exception:
                    pass
        stats["officialAvailable"] = official is not None
        stats["officialReason"] = why
        stats["line"] = Lbase.summary(stats, official is not None, why)
        if stats.get("memory") or stats.get("manual"):
            try:
                self._memory_store.save()
            except Exception:
                pass
        return stats

    def baseline_info(self):
        """给设置页用：基线库状态 + 当前目标语言能不能用。"""
        import baseline as Lbase
        tgt = self.settings.get("target_lang", "简体中文")
        ok, why = Lbase.can_use_official(tgt)
        out = {"ok": True, "target": tgt, "usable": ok, "reason": why,
               "enabled": bool(self.settings.get("use_official_tm", True))}
        try:
            from localization import official_tm as Lotm
            st = Lotm.OfficialTM().stats()
            out.update({k: st.get(k) for k in
                        ("exists", "total", "onscreen", "subtitle", "built_at", "db")})
        except Exception as ex:
            out["exists"] = False
            out["error"] = str(ex)
        try:
            out["memory"] = self._memory_store.origin_counts()
        except Exception:
            out["memory"] = {}
        return out

    # ------------------------------------------------------------ 启动
    def boot(self):
        _startup_log("Api.boot begin")
        try:
            payload = self._boot_payload()
            _startup_log("Api.boot returned")
            return payload
        except Exception:
            import traceback
            try:
                with open(os.path.join(tempfile.gettempdir(), "mt_boot_err.txt"),
                          "w", encoding="utf-8") as f:
                    f.write(traceback.format_exc())
            except Exception:
                pass
            raise

    def ui_ready(self):
        _startup_log("frontend boot complete")
        return {"ok": True}

    def _boot_payload(self):
        return {
            "version": VERSION,
            "settings": self.settings,
            "lang": i18n.current(),
            "langs": [{"code": c, "name": n} for c, n in i18n.available()],
            "catalog": i18n.catalog(),
            "targetLangs": TARGET_LANGS,
            "configFile": config_file(),
            "initialPath": self.initial_path,
            # 批量事件的只读本机端口（fetch 用）。起不来就是空串，前端退回桥。
            "pollUrl": self._bus.start(),
        }

    def set_language(self, code):
        self.lang = i18n.set_language(code)
        cfg = dict(self.settings)
        cfg["lang"] = self.lang
        try:
            save_settings(cfg)
        except Exception:
            pass
        self.settings = cfg
        return {"lang": self.lang, "catalog": i18n.catalog()}

    def save_settings(self, cfg):
        new = dict(self.settings)
        for k in ("api_base", "model", "lang", "target_lang",
                  "game_dir", "wkit_path", "res_source_lang",
                  "staging_dir", "downloads_dir", "batch_out_dir"):
            if isinstance(cfg.get(k), str):
                new[k] = cfg[k]
        for k in ("dark_mode", "use_tm", "res_skip_target", "verify_active",
                  "official_weak", "use_official_tm", "local_model_enabled",
                  # 关窗时问一句「要停掉本地模型吗」：默认开，嫌烦的可以关掉。
                  "ask_unload_model",
                  # 界面不用 GPU 渲染（排查显卡驱动问题时用，下次启动生效）。
                  "webview_disable_gpu",
                  # 这三个是批量面板上的勾选：以前漏在这张白名单外，前端存了也
                  # 被丢掉，于是「跑完覆盖回原模组」永远读到默认的 False ——
                  # 勾上等于没勾，批量只产出到副本，一个字节也不会回写。
                  "batch_deploy", "batch_deploy_dry", "batch_hide_todo"):
            if isinstance(cfg.get(k), bool):
                new[k] = cfg[k]
        new["api_base"] = normalize_api_base(new.get("api_base", ""))
        # 路径字段去掉首尾空白与包裹的引号（用户常直接从资源管理器复制带引号）
        for k in ("game_dir", "wkit_path", "staging_dir", "downloads_dir",
                  "batch_out_dir"):
            v = (new.get(k) or "").strip().strip('"').strip("'")
            new[k] = os.path.normpath(v) if v else ""
        try:
            save_settings(new)
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
        # 目录变了就让旧的映射缓存失效
        for k in ("staging_dir", "downloads_dir", "game_dir"):
            if new.get(k) != self.settings.get(k):
                self._vmap = None
                break
        self.settings = new
        if new.get("lang") and new["lang"] != i18n.current():
            i18n.set_language(new["lang"])
        return {"ok": True, "settings": self.settings}

    # ------------------------------------------------------------ 活动映射
    #
    # Vortex 用硬链接把「安装缓存目录」和游戏目录连起来：改缓存里的文件就是改
    # 游戏。但缓存里同样堆着大量已经被替换、根本没部署的旧目录——照着目录名翻，
    # 一大半力气会花在游戏永远读不到的文件上。
    #
    # 这份映射回答三个问题，全部只读，不碰 Vortex 的任何状态:
    #   1. 哪些目录真的生效 —— 读 vortex.deployment.msgpack，不猜
    #   2. 下载包里有没有比装的新 —— 压缩包只读、不变，是天然的版本锚
    #   3. 正在加载的这个目录是活的还是死的

    def _vmap_dirs(self):
        return ((self.settings.get("staging_dir") or "").strip(),
                (self.settings.get("downloads_dir") or "").strip(),
                (self.settings.get("game_dir") or "").strip())

    def vortex_detect(self):
        """按 Vortex 日志和常见布局猜目录。只返回结果，不落盘。"""
        try:
            import vortex_map
            r = vortex_map.detect(self.settings.get("game_dir") or "")
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
        # 已经手填过的位置不覆盖，只补空的那一项
        staging, downloads, _game = self._vmap_dirs()
        if not r.get("staging"):
            r["staging"] = staging
        if not r.get("downloads"):
            r["downloads"] = downloads
        return r

    def vortex_scan(self, downloads=None, staging=None):
        """扫下载包 + 安装缓存 + 部署清单，返回可直接渲染的行。"""
        try:
            import vortex_map
        except Exception as ex:
            return {"ok": False, "error": self._tr("映射模块加载失败：%s") % ex}
        st_set, dl_set, game = self._vmap_dirs()

        def _clean(v):
            return v.strip().strip('"').strip("'") if isinstance(v, str) else ""

        st = _clean(staging) or st_set
        dl = _clean(downloads) or dl_set
        if not st and not dl:
            return {"ok": False,
                    "error": self._tr("请先在设置里填写「安装缓存目录」与「下载包目录」")}
        try:
            res = vortex_map.scan(downloads_dir=dl, staging_dir=st, game_dir=game)
        except Exception as ex:
            import traceback
            traceback.print_exc()
            return {"ok": False, "error": str(ex)}
        res["summary"] = vortex_map.summary_line(res)
        self._vmap = res
        return res

    def vortex_map(self):
        """取上一次扫描的缓存（界面重开面板时不用重扫）。"""
        return self._vmap or {"ok": False, "error": ""}

    def _active_hint(self, root):
        """加载某个目录前，看它在不在部署清单里。判断不了就返回 None，不打扰。"""
        if not self.settings.get("verify_active", True):
            return None
        staging = (self.settings.get("staging_dir") or "").strip()
        if not staging or not root:
            return None
        try:
            import vortex_map
        except Exception:
            return None
        try:
            base = os.path.normpath(os.path.abspath(staging))
            cur = os.path.normpath(os.path.abspath(root))
        except Exception:
            return None
        if os.path.dirname(cur) != base:
            return None            # 不是缓存里的一级目录，不做判断
        name = os.path.basename(cur)
        try:
            info = vortex_map.deployment_info(staging)
        except Exception:
            return None
        if not info.get("ok"):
            return None
        files = int((info.get("sources") or {}).get(name, 0))
        return {"dir": name, "active": files > 0, "files": files,
                "deployedAt": vortex_map.fmt_time(info.get("deployment_time", 0)),
                "method": info.get("method", "")}

    # ------------------------------------------------------------ 窗口
    def win_min(self):
        self._window.minimize()

    def win_max(self):
        if self._maxed:
            self._window.restore()
            self._maxed = False
        else:
            self._window.maximize()
            self._maxed = True
        return self._maxed

    def win_close(self):
        if not self._confirm_exit():
            return                      # 用户点了「取消」：窗口留着
        try:
            self._window.destroy()
        except Exception:
            pass
        threading.Timer(0.25, lambda: os._exit(0)).start()

    def _local_model_target(self):
        """本地模型还值不值得问一句：返回 (api_base, model)，不是本地就返回 ("", "")。

        只对**本机**的 Ollama 问：远端接口没有「卸掉本地模型」这回事，
        而且关窗时多发一次网络请求只会拖慢退出。
        """
        base = normalize_api_base(self.settings.get("api_base", ""))
        model = (self.settings.get("model") or "").strip()
        if not base or not model:
            return "", ""
        try:
            host = urlsplit(base).hostname or ""
        except Exception:
            host = ""
        if host.lower() not in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
            return "", ""
        try:
            import mt_client
            if not mt_client._is_ollama(base):
                return "", ""
        except Exception:
            return "", ""
        return base, model

    def _confirm_exit(self):
        """关窗前的「要不要停掉本地模型」三选一；返回 False 表示取消退出。

        Ollama 的模型默认常驻（keep_alive），关掉翻译器它也不会自己走：
        9B 级常占 6~8 GB、27B 常驻 21 GB，玩家接着开游戏就得跟它抢显存。
        用系统 MessageBox 而不是页面内弹窗：这一步在窗口销毁之前，
        页面里再弹什么都可能被一起关掉。
        """
        if not self.settings.get("ask_unload_model", True):
            return True
        base, model = self._local_model_target()
        if not base:
            return True

        MB_YESNOCANCEL = 0x03
        MB_ICONQUESTION = 0x20
        MB_SETFOREGROUND = 0x10000
        IDCANCEL, IDYES = 2, 6
        # 分句翻译再拼：整段带换行的中文没法进 locales（键里有 \n 就匹配不上）。
        text = "\n\n".join([
            self._tr("要停止本地模型吗？"),
            self._tr("模型 %s 还常驻在显存里，关掉翻译器它也不会自己退出。") % model,
            self._tr("停止后下次翻译要重新加载一次（几秒到几十秒，看模型大小）。"),
            self._tr("是：停止模型并退出      否：直接退出      取消：不退出"),
        ])
        try:
            import ctypes
            rc = ctypes.windll.user32.MessageBoxW(
                None, text, "2077 Mod Translator",
                MB_YESNOCANCEL | MB_ICONQUESTION | MB_SETFOREGROUND)
        except Exception:               # noqa: BLE001  弹不出来就别拦着用户
            return True
        if rc == IDCANCEL:
            return False
        if rc == IDYES:
            try:
                import mt_client
                mt_client.unload(base, model, timeout=15)
            except Exception:           # noqa: BLE001
                pass
        return True

    def win_resize(self, w, h):
        try:
            self._window.resize(max(900, int(w)), max(600, int(h)))
        except Exception:
            pass

    def open_dir(self, root, rel):
        """在资源管理器里定位到某个文件"""
        try:
            base = root or self.root
            path = os.path.normpath(os.path.join(base, rel.replace("/", os.sep)))
            if not os.path.exists(path):
                path = os.path.dirname(path)
            if os.path.isdir(path):
                os.startfile(path)
            else:
                import subprocess
                subprocess.Popen('explorer /select,"%s"' % path)
            return {"ok": True}
        except Exception as ex:
            return {"ok": False, "error": str(ex)}

    def open_archive_dir(self, archive_path):
        """资源行的「打开文件所在目录」：定位到这一行所属的 .archive。

        归档里的条目在磁盘上没有独立文件——`file` 是归档内部路径。这一行的
        「文件」其实就是那个 .archive，所以打开它所在的目录并选中它。归档
        被删掉/移走时退回打开父目录，别一声不响地打开错地方。
        """
        p = str(archive_path or "").strip()
        if not p:
            return {"ok": False, "error": self._tr("没有归档路径")}
        p = os.path.abspath(p)
        if os.path.isfile(p):
            try:
                import subprocess
                subprocess.Popen('explorer /select,"%s"' % p)
            except Exception:
                try:
                    os.startfile(os.path.dirname(p))
                except Exception as ex:
                    return {"ok": False, "error": str(ex)}
            return {"ok": True, "path": p}
        d = os.path.dirname(p)
        if d and os.path.isdir(d):
            try:
                os.startfile(d)
            except Exception as ex:
                return {"ok": False, "error": str(ex)}
            return {"ok": True, "path": d}
        return {"ok": False,
                "error": self._tr("归档已不在原位置: %s") % p}

    def open_root_dir(self):
        try:
            path = self.root or ""
            if path and os.path.isdir(path):
                os.startfile(path)
                return {"ok": True}
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
        return {"ok": False, "error": "no root"}

    # ------------------------------------------------------------ 对话框
    def pick_folder(self):
        try:
            res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            res = None
        return res[0] if res else ""

    def pick_zip(self):
        try:
            res = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("Zip (*.zip)", "All files (*.*)"))
        except Exception:
            res = None
        return res[0] if res else ""

    def _extract_zip(self, path):
        import shutil
        import zipfile
        base = os.path.join(tempfile.gettempdir(),
                            "2077mt_" + re.sub(r"[^A-Za-z0-9_.-]+", "_",
                                               os.path.basename(path))[:60])
        shutil.rmtree(base, ignore_errors=True)
        os.makedirs(base, exist_ok=True)
        with zipfile.ZipFile(path) as z:
            z.extractall(base)
        return base

    def pack_zip(self):
        if not self.root or not os.path.isdir(self.root):
            return {"ok": False, "error": "no root"}
        default = os.path.join(
            os.path.dirname(self.archive) if self.archive else self.root,
            (os.path.splitext(os.path.basename(self.archive))[0]
             if self.archive else os.path.basename(self.root)) + "_translated.zip")
        try:
            path = self._window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default,
                file_types=("Zip (*.zip)",))
        except Exception:
            path = ""
        if isinstance(path, (list, tuple)):
            path = path[0] if path else ""
        if not path:
            return {"ok": False}
        try:
            import zipfile
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                for dirpath, _dirs, files in os.walk(self.root):
                    for fn in files:
                        full = os.path.join(dirpath, fn)
                        arc = os.path.relpath(full, self.root).replace("\\", "/")
                        z.write(full, arc)
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
        return {"ok": True, "path": path}

    def pick_xlsx_open(self):
        try:
            res = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("Excel (*.xlsx;*.xlsm)", "All files (*.*)"))
        except Exception:
            res = None
        return res[0] if res else ""

    def pick_xlsx_save(self, default_name="translation.xlsx"):
        try:
            res = self._window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default_name,
                file_types=("Excel (*.xlsx)",))
        except Exception:
            res = None
        return res if isinstance(res, str) else (res[0] if res else "")

    def pick_txt_open(self):
        try:
            res = self._window.create_file_dialog(
                webview.OPEN_DIALOG, file_types=("Text (*.txt)", "All files (*.*)"))
        except Exception:
            res = None
        return res[0] if res else ""

    def pick_txt_save(self, default_name="glossary.txt"):
        try:
            res = self._window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default_name,
                file_types=("Text (*.txt)",))
        except Exception:
            res = None
        return res if isinstance(res, str) else (res[0] if res else "")

    # ------------------------------------------------------------ 提取
    def load_mod(self, path, opts=None):
        """异步: 立刻返回, 结果通过 loaded / error 事件回推"""
        opts = opts or {}
        if self._busy:
            return {"ok": False, "error": "busy"}
        self._busy = True
        self._emit({"type": "busy", "on": True, "text": i18n.tr("正在提取可翻译文本 ...")})

        def work():
            try:
                r = self._do_load(path, opts)
                if r.get("ok"):
                    self._emit(dict(r, type="loaded"))
                else:
                    self._emit({"type": "error", "message": r.get("error", "")})
            except Exception as ex:
                import traceback
                traceback.print_exc()
                self._emit({"type": "error", "message": str(ex)})
            finally:
                self._busy = False
                self._emit({"type": "busy", "on": False})

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "started": True}

    def _do_load(self, path, opts):
        import locale_pack
        import mod_text_engine as E
        opts = opts or {}
        # 切回源码模式：丢掉资源模式的会话，避免误装上一次的产物
        self.l10n_session = None
        self.l10n_result = None
        self.l10n_undo_root = ""
        self.archive = ""
        root = path
        if path and os.path.isfile(path) and path.lower().endswith(".zip"):
            try:
                root = self._extract_zip(path)
                self.archive = path
            except Exception as ex:
                return {"ok": False, "error": self._tr("解压失败: %s") % ex}
        if not root or not os.path.isdir(root):
            return {"ok": False, "error": self._tr("请先选择有效的模组目录")}
        self.root = root
        target = locale_pack.target_code(
            self.settings.get("target_lang", "简体中文"))

        # ---- 语言包层: 先尝试 Provider 配对，再退回普通语言包
        #
        # 这一层只负责「语言包文件」，不再提前 return：同一个模组往往既有语言包
        # (档位/剧情文本)，又有 CET .lua 设置界面和 @runtimeProperty 设置项。
        # 以前命中语言包就直接返回，结果设置文本整片消失。
        mode = "content"
        loc_rows, loc_meta, loc_info = [], {}, {}
        locale_files = []
        consumed_rels = set()
        tm_hits = 0
        if opts.get("localeFirst", True):
            try:
                locale_files = locale_pack.detect(root, parse=True, target=target)
            except Exception:
                locale_files = []

            # Provider 配对模式只接管 Codeware .reds 语言包；JSON/Lua 继续按原规则。
            if opts.get("providerPair", True):
                try:
                    pairs, pair_issues = locale_pack.discover_provider_pairs(root, target)
                except Exception:
                    pairs, pair_issues = [], []
                if pairs:
                    lrows = []
                    skipped = 0
                    seen_sources = set()
                    for pair in pairs:
                        try:
                            prows, pmeta = locale_pack.extract(
                                root, [pair["source_entry"]], target,
                                only_class=pair.get("source_class", ""))
                        except Exception:
                            continue
                        skipped += int(pmeta.get("skipped_target", 0) or 0)
                        seen_sources.add(pair["source"])
                        for r in prows:
                            r["provider"] = pair["provider"]
                            r["providers"] = list(pair.get("providers") or ())
                            r["pairId"] = pair["id"]
                            r["sourceClass"] = pair.get("source_class", "")
                            r["targetClass"] = pair.get("target_class", "")
                            r["targetLocale"] = target
                            r["pairMode"] = "provider"
                        lrows.extend(prows)

                    # 非 Codeware 的语言包（MCM JSON、Lua 等）继续提取。
                    extras = [f for f in locale_files if f.get("format") != "reds"]
                    extra_srcs = locale_pack.pick_sources(extras, target) if extras else []
                    for entry in extra_srcs:
                        if entry["rel"] in seen_sources:
                            continue
                        try:
                            xrows, xmeta = locale_pack.extract(root, [entry], target)
                        except Exception:
                            continue
                        skipped += int(xmeta.get("skipped_target", 0) or 0)
                        seen_sources.add(entry["rel"])
                        for r in xrows:
                            r["pairMode"] = "extra"
                        lrows.extend(xrows)

                    if lrows:
                        loc_rows = lrows
                        loc_meta = {
                            "files": len({r["file"] for r in lrows}),
                            "types": {},
                            "locale": True,
                            "mode": "provider",
                            "skipped_target": skipped,
                            "pairs": pairs,
                            "unpaired": pair_issues,
                            "providerPairs": len(pairs),
                            "sourceFiles": len(seen_sources),
                            "targetLocale": target,
                            "archive": self.archive,
                            "localeTotal": len(locale_files),
                        }
                        if pairs:
                            loc_meta["sourceLocale"] = pairs[0].get("source_locale", "")
                        first = pairs[0] if pairs else None
                        source_desc = (("%s → %s" % (
                            first["provider"], first["source"])) if first
                            else self._tr("语言包"))
                        if len(pairs) > 1:
                            source_desc += self._tr(" 等 %d 对") % len(pairs)
                        loc_info = {"mode": "provider", "source": source_desc,
                                    "target": target, "pairCount": len(pairs),
                                    "unpaired": len(pair_issues),
                                    "localeTotal": len(locale_files),
                                    "sourceFiles": len(seen_sources)}
                        mode = "provider"

            if not loc_rows and locale_files:
                srcs = locale_pack.pick_sources(locale_files, target)
                if srcs:
                    lrows, lmeta = locale_pack.extract(root, srcs, target)
                    if lrows:
                        loc_rows = lrows
                        lmeta["archive"] = self.archive
                        lmeta["mode"] = "locale"
                        lmeta["localeTotal"] = len(locale_files)
                        lmeta["sourceLocale"] = srcs[0]["locale"] or srcs[0]["rel"]
                        lmeta["sourceFiles"] = len(srcs)
                        lmeta["targetLocale"] = target
                        loc_meta = lmeta
                        loc_info = {"mode": "locale", "target": target,
                                    "source": (srcs[0]["rel"] if len(srcs) == 1 else
                                               self._tr("%s 等 %d 个文件")
                                               % (srcs[0]["rel"], len(srcs))),
                                    "localeTotal": len(locale_files),
                                    "sourceFiles": len(srcs)}
                        mode = "locale"

        # 语言包行的译文统一在两层合并后一起填（见下方 _baseline_fill 调用），
        # 这里不再单独跑一遍记忆，避免同一批文本查两次。

        # 这些文件的内容已经由语言包层处理，源码层不要再重复提取一遍。
        for r in loc_rows:
            if r.get("file"):
                consumed_rels.add(r["file"])
            if r.get("target"):
                consumed_rels.add(r["target"])

        # ---- 源码层: 语言包之外的一切（CET .lua 设置界面 / @runtimeProperty
        # 设置项 / 通知与提示文本）。语言包分支不再提前 return，所以这一层
        # 总能跑到；语言包本身已按相对路径排除，不会重复提取。
        try:
            tgt_files = locale_pack.detect(root, parse=False, target=target)
        except Exception:
            tgt_files = []
        exclude_rels = set(consumed_rels)
        for f in tgt_files:
            if f.get("locale") == target:
                exclude_rels.add(f["rel"])
        data, meta = E.extract_root_ex(
            root,
            include_lua=bool(opts.get("lua", True)),
            loose=bool(opts.get("loose", True)),
            include_locale_lua=bool(opts.get("locale", False)),
            target_locale=target,
            exclude_rels=exclude_rels,
            trace=bool(opts.get("trace", False)),
            keep_target=True)
        # 已经翻好的行不能因为「已是中文」就从表里消失：用户翻完一轮再回来，
        # 会以为那些行被漏提取了，也没法修「Dark Mods → 暗插件」这种别扭译法。
        # 这里把它们作为「已译」行并回来，译文预填当前值，可改、可回写。
        already_map = meta.pop("already", {}) or {}
        src_rows = []
        for f in sorted(set(data) | set(already_map)):
            for (ln, typ, ctx, orig) in data.get(f, []):
                src_rows.append({"file": f, "disp": disp_path(f, root), "line": ln,
                                 "type": typ, "ctx": ctx, "orig": orig,
                                 "trans": "", "pairMode": "source",
                                 "layer": "source"})
            for (ln, typ, ctx, orig) in already_map.get(f, []):
                src_rows.append({"file": f, "disp": disp_path(f, root), "line": ln,
                                 "type": typ, "ctx": ctx, "orig": orig,
                                 "trans": orig, "pairMode": "source",
                                 "layer": "source", "already": 1})
        # ---- 用户标记为「代码」的行直接过滤掉
        #
        # 位置判据只能挡住工具认得出来的名字; 剩下的（纯符号、模组特有的协议串）
        # 只能靠人判断。判断一旦落下就必须生效, 否则用户每次加载都还会看到它。
        try:
            import user_skip
            marked = user_skip.marks(root)
            try:
                skipped_marks = user_skip.skip_marks(root)
            except Exception:
                skipped_marks = set()
        except Exception:
            marked = set()
            skipped_marks = set()
        hidden_skip = 0
        if marked and src_rows:
            kept = []
            for r in src_rows:
                key = (str(r.get("file") or "").replace("\\", "/").lower(),
                       user_skip.sha1_of(r.get("orig") or ""))
                if key in marked:
                    hidden_skip += 1
                    continue
                kept.append(r)
            src_rows = kept
        # ---- 上次勾过「跳过」的行：行还在，但默认就是勾上的状态
        #
        # 跳过和「标记为代码」不一样：那个是隐藏（判断是代码），这个是「我暂时不想翻」，
        # 用户下次可能又想翻了，所以不能藏起来，只能把勾选状态带回来。
        auto_skip = 0
        if skipped_marks and src_rows:
            for r in src_rows:
                key = (str(r.get("file") or "").replace("\\", "/").lower(),
                       user_skip.sha1_of(r.get("orig") or ""))
                if key in skipped_marks:
                    r["skip"] = 1
                    auto_skip += 1
        # ---- 合并两层: 语言包行在前, 设置/源码文本在后
        for r in loc_rows:
            r["layer"] = "locale"

        # ---- 资源层: 归档内嵌的 localization
        #
        # 很多模组的 .reds 只写本地化 key（@runtimeProperty(...,"UI-Foo-bar-label")），
        # 真正的文案躺在 mod 自己的 .archive 里。只看明文文件，要么以为这个模组
        # 没有界面文本，要么把 key 当文案翻掉——两种都会翻车。这里把归档也扫进来，
        # 三层合并成一张表。找不到归档/CLI 只是「这个模组没有这一层」，不是错误。
        res_rows, res_sess, res_info = [], None, {}
        if opts.get("resource", True) and not self.archive:
            try:
                res_rows, res_sess, res_info = self._scan_resource_layer(root)
            except Exception as ex:      # 单个模组的归档坏了不该拖垮整次加载
                res_info = {"error": str(ex)}
                res_rows, res_sess = [], None
        if res_rows:
            self.l10n_session = res_sess
        rows = list(loc_rows) + src_rows + res_rows

        # ---- 三层填充：手改 > 官方基线 > 机翻记忆
        #
        # v2.25 之前这一层只查翻译记忆，官方语料只在资源模式和批量队列里生效，
        # 源码模式（.reds / .lua，也就是最常用的那条路）完全没吃到官方的红利。
        bstat = self._baseline_fill(rows)
        tm_hits = bstat.get("memory", 0) + bstat.get("manual", 0)
        src_types = dict(meta.get("types") or {})
        loc_types = {}
        for r in loc_rows:
            loc_types[r["type"]] = loc_types.get(r["type"], 0) + 1
        meta["source"] = {"files": int(meta.get("files", 0) or 0),
                          "types": src_types}
        meta["localeRows"] = len(loc_rows)
        meta["sourceRows"] = len(src_rows)
        meta["archive"] = self.archive
        meta["mode"] = mode
        if loc_meta:
            merged = dict(loc_meta)
            types = dict(loc_types)
            for k, v in src_types.items():
                types[k] = types.get(k, 0) + v
            merged["types"] = types
            merged["files"] = int(loc_meta.get("files", 0) or 0) + int(
                meta.get("files", 0) or 0)
            merged["skipped_target"] = (int(loc_meta.get("skipped_target", 0) or 0)
                                        + int(meta.get("skipped_target", 0) or 0))
            merged["locale_pairs"] = meta.get("locale_pairs", 0)
            merged["locale_pairs_used"] = meta.get("locale_pairs_used", 0)
            merged["noise"] = meta.get("noise", {})
            merged["parse_errors"] = (int(meta.get("parse_errors", 0) or 0)
                                      + int(loc_meta.get("parse_errors", 0) or 0))
            merged["parse_error_sample"] = (
                list(loc_meta.get("parse_error_sample") or [])
                + list(meta.get("parse_error_sample") or []))[:5]
            merged["layered"] = True
            merged["source"] = meta["source"]
            merged["localeRows"] = len(loc_rows)
            merged["sourceRows"] = len(src_rows)
            merged["archive"] = self.archive
            merged["mode"] = mode
            meta = merged
        # 资源层的信息与跨层冲突放在这里收尾：上面 if loc_meta 会重建 meta，
        # 提前写进去会被覆盖掉。
        meta["resourceRows"] = len(res_rows)
        meta["userSkip"] = hidden_skip
        meta["autoSkip"] = auto_skip
        if res_info:
            meta["resource"] = res_info
        if res_rows and (loc_rows or src_rows):
            plain = {(r.get("orig") or "").strip()
                     for r in list(loc_rows) + src_rows}
            plain.discard("")
            dup = plain & {(r.get("orig") or "").strip() for r in res_rows}
            dup.discard("")
            if dup:
                # 同一句既在明文里、又在归档里：两层各会翻一遍、各写一份。
                # 不阻断，只把账摊开，交给用户判断。
                meta["conflicts"] = len(dup)
                meta["conflictSample"] = sorted(dup)[:5]
        self.rows = rows
        self.meta = meta
        out = {"ok": True, "rows": rows, "meta": meta, "tmHits": tm_hits,
               "officialHits": (bstat.get("official", 0)
                                + bstat.get("officialWeak", 0)),
               "baseline": bstat,
               "root": root, "archive": self.archive, "mode": mode,
               "target": target,
               "skippedTarget": meta.get("skipped_target", 0),
               "parseErrors": int(meta.get("parse_errors", 0) or 0),
               "parseErrorSample": list(meta.get("parse_error_sample") or [])[:5],
               "localeRows": len(loc_rows), "sourceRows": len(src_rows),
               "resourceRows": len(res_rows), "userSkip": hidden_skip,
               "autoSkip": auto_skip}
        for k in ("source", "pairCount", "unpaired", "localeTotal", "sourceFiles"):
            if k in loc_info:
                out[k] = loc_info[k]
        # 活动映射：这个目录到底有没有被 Vortex 部署。判断不了就不加这个字段。
        try:
            hint = self._active_hint(root)
        except Exception:
            hint = None
        if hint:
            out["mapState"] = hint
        return out

    def _scan_resource_layer(self, root):
        """把模组归档里的 localization 也扫进来，作为第三层。

        一个模组可能有好几个归档（AMM 就有 3 个），本地化表只在其中一个里。
        所以这里逐个归档扫，最后包成一个 `SessionGroup`，上层仍然只看到一个
        会话。返回 (rows, group, info)。info["skipped"] 说明这一层为什么是空的：
        没有 WolvenKit、目录里没有归档、归档里没有本地化资源、扫描失败。
        这些都是「这个模组没有这一层」的正常情况，调用方不该当错误处理。
        """
        from localization import archive as Larch
        from localization import pipeline as Lpipe
        from localization import wkit as Lwkit

        cfg = self._l10n_cfg()
        try:
            cli = Lwkit.find_cli(cfg["wkit_path"] or None)
        except Exception:
            cli = None
        if not cli:
            return [], None, {"skipped": "no_wkit"}

        try:
            targets = [t for t in Larch.resolve_all(root) if t.archive_path is not None]
        except Exception as ex:
            return [], None, {"skipped": "no_archive", "detail": str(ex)}
        if not targets:
            return [], None, {"skipped": "no_archive"}

        sessions, rows, used, empty_arch = [], [], [], []

        # 先只读目录：归档里没有 localization 就不必解包（几百 MB 的归档解一遍
        # 要几十秒，只为确认"这里没有可翻的东西"太亏）。这一步只是预筛，目录里
        # 有 localization 目录却没条目也会放行，最终以行数为准。
        #
        # 读一次目录是 2 秒的固定开销（CLI 进程启动占大头），一个 72 归档的模组
        # 串行读要 147 秒。并行 4 路 + `has_localization` 自带的结果缓存，
        # 第一轮约 25 秒、第二轮 0 秒。
        from concurrent.futures import ThreadPoolExecutor

        def _toc(target):
            try:
                return bool(Lwkit.has_localization(cli, target.archive_path))
            except Exception:
                return True

        try:
            with ThreadPoolExecutor(max_workers=min(4, len(targets))) as ex:
                checks = list(ex.map(_toc, targets))
        except Exception:
            checks = [True] * len(targets)

        for target, has_loc in zip(targets, checks):
            if not has_loc:
                continue

            work = os.path.join(
                config_dir(), "work",
                re.sub(r"[^A-Za-z0-9_.-]+", "_", target.name)[:60] or "mod")
            sess = Lpipe.Session(target, cli, work, lang="zh-cn", log=None,
                                source_lang=cfg["source_lang"])
            try:
                sess.scan()
            except Exception:
                continue         # 一个归档坏了不该让其它归档一起哑掉
            if not sess.rows:
                empty_arch.append(str(target.archive_path or ""))
                continue

            got, skipped = self._filter_l10n_rows(sess, cfg)
            for r in got:
                r["layer"] = "resource"
                # 资源行的 file 是归档内部路径，磁盘上没有这个文件。要定位到
                # 「这一行所在的文件」，必须知道它在哪个归档里。
                r["archive"] = str(target.archive_path or "")
            rows.extend(got)
            sessions.append(sess)
            used.append({"archive": str(target.archive_path or ""),
                         "name": target.name, "rows": len(got),
                         "skipped": skipped})

        if not rows:
            info = {"skipped": "no_rows",
                    "archives": [str(t.archive_path or "") for t in targets],
                    "emptyArchives": empty_arch}
            if empty_arch:
                info["archive"] = empty_arch[0]
            return [], None, info

        group = Lpipe.SessionGroup(sessions, lang="zh-cn")
        meta = group.meta
        info = {
            "rows": len(rows),
            "skipped": sum(int(u["skipped"] or 0) for u in used),
            "archive": used[0]["archive"],
            "archives": [u["archive"] for u in used],
            "emptyArchives": empty_arch,
            "scan": used,
            "sourceLang": cfg["source_lang"],
            "languages": dict(meta.get("languages") or {}),
            "extend": group.target.extend_name() if group.target else "",
            "name": group.target.name if group.target else "",
        }
        return rows, group, info

    # ------------------------------------------------------------ 资源模式
    #
    # 与上面的源码模式并列：源码模式回写明文文件，资源模式改 .archive 里的 CR2W。
    # 产物是**新增**的 <name>.archive + <name>.archive.xl，原 mod 一个字节都不动。

    def _l10n_cfg(self):
        return {
            "game_dir": (self.settings.get("game_dir") or "").strip(),
            "wkit_path": (self.settings.get("wkit_path") or "").strip(),
            "source_lang": (self.settings.get("res_source_lang") or "en-us").strip() or "en-us",
            "skip_target": bool(self.settings.get("res_skip_target", True)),
        }

    def l10n_env(self):
        """资源模式环境自检。设置页与打开归档前都会调。"""
        try:
            from localization import archive as Larch
            from localization import wkit as Lwkit
        except Exception as ex:
            return {"ok": False,
                    "error": self._tr("资源模式引擎加载失败: %s") % ex,
                    "problems": []}
        cfg = self._l10n_cfg()
        cli = Lwkit.find_cli(cfg["wkit_path"] or None)
        game = Larch.find_game(cfg["game_dir"] or None)
        problems, info = Lwkit.preflight(cli, game)
        return {
            "ok": not problems,
            "cli": info["cli"], "cliVersion": info["cliVersion"],
            "game": info["game"], "archiveXL": info["archiveXL"],
            "problems": problems,
            "outputDir": self.l10n_output_dir(),
        }

    def l10n_pick(self):
        """选一个 mod：.archive.xl / .archive / .zip 都接受。"""
        try:
            res = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("mod 归档 (*.xl;*.archive;*.zip)",
                            "ArchiveXL (*.xl)", "归档 (*.archive)",
                            "压缩包 (*.zip)", "All files (*.*)"))
        except Exception:
            res = None
        return res[0] if res else ""

    def l10n_output_dir(self):
        """批量/兜底产物目录。

        单个 mod「生成翻译 mod」现在直接写到它自己的 .archive 旁边（见
        _resource_outdir），这里留给批量队列集中收集，以及 mod 目录不可写时
        的退路。刻意不写游戏目录，避免批量跑时污染游戏。
        """
        d = os.path.join(config_dir(), "output")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            return ""
        return d

    @staticmethod
    def _archive_mod_root(archive_path):
        r"""从 .archive 的绝对路径反推 mod 根目录。

        归档通常躺在 <mod>\archive\pc\mod\ 下，往上找到名为 archive 的那层，
        它的上一级就是模组根——撤销事务要把产物路径记在模组根之下才能还原。
        """
        p = str(archive_path or "")
        if not p:
            return ""
        cur = os.path.dirname(os.path.abspath(p))
        while True:
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            if os.path.basename(cur).lower() == "archive":
                return parent
            cur = parent
        return os.path.dirname(os.path.abspath(p))

    def _resource_outdir(self, sess=None):
        """资源模式产物目录：归档所在的 mod 目录，和原 .archive 并排。

        以前固定落 userdata\\output，用户还得自己拖回 mod 目录；可产物本来就是
        「这个 mod 的中文版」，跟原归档放一起才算落在它该在的地方。只有目录
        不可写（只读盘、权限不足）才退回 output，不让整个应用失败。
        """
        sess = sess if sess is not None else self.l10n_session
        target = getattr(sess, "target", None) if sess is not None else None
        arch = getattr(target, "archive_path", None) if target is not None else None
        if arch:
            try:
                d = os.path.dirname(os.path.abspath(str(arch)))
                os.makedirs(d, exist_ok=True)
                if os.access(d, os.W_OK):
                    return d
            except Exception:
                pass
        return self.l10n_output_dir()

    def l10n_open_output(self):
        """打开产物目录：应用过就开归档旁边，还没应用才是 output。"""
        d = ""
        result = getattr(self, "l10n_result", None)
        arch = getattr(result, "archive", None) if result is not None else None
        if arch:
            d = os.path.dirname(os.path.abspath(str(arch)))
        if not d or not os.path.isdir(d):
            d = self.l10n_output_dir()
        if not d or not os.path.isdir(d):
            return {"ok": False, "error": self._tr("输出目录不可用")}
        try:
            os.startfile(d)          # noqa: S606 - 桌面应用主动打开资源管理器
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
        return {"ok": True, "path": d}

    def load_resource(self, path, opts=None):
        """异步：解析归档并抽出条目，结果走 loaded 事件。"""
        opts = opts or {}
        if self._busy:
            return {"ok": False, "error": "busy"}
        self._busy = True
        self._emit({"type": "busy", "on": True,
                    "text": i18n.tr("正在解包并解析归档 ...")})

        def work():
            try:
                r = self._do_load_resource(path, opts)
                if r.get("ok"):
                    self._emit(dict(r, type="loaded"))
                else:
                    self._emit({"type": "error", "message": r.get("error", "")})
            except Exception as ex:
                import traceback
                traceback.print_exc()
                self._emit({"type": "error", "message": str(ex)})
            finally:
                self._busy = False
                self._emit({"type": "busy", "on": False})

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "started": True}

    def _do_load_resource(self, path, opts):
        from localization import archive as Larch
        from localization import pipeline as Lpipe
        from localization import wkit as Lwkit

        cfg = self._l10n_cfg()
        cli = Lwkit.find_cli(cfg["wkit_path"] or None)
        if not cli:
            return {"ok": False,
                    "error": self._tr("未找到 WolvenKit.CLI.exe，请先在设置里指定路径。")}

        try:
            target = Larch.resolve_target(path, workdir=os.path.join(
                config_dir(), "work", "zip"))
        except Exception as ex:
            return {"ok": False, "error": self._tr("解析目标失败: %s") % ex}
        if target.archive_path is None:
            # .xl 自定义命名时找不到同名归档，让用户直接选 .archive
            return {"ok": False,
                    "error": self._tr(
                        "找不到与 %s 配套的 .archive。请直接选择该 mod 的归档文件。")
                    % (target.xl_path.name if target.xl_path else path)}

        work = os.path.join(config_dir(), "work",
                            re.sub(r"[^A-Za-z0-9_.-]+", "_", target.name)[:60] or "mod")
        sess = Lpipe.Session(target, cli, work, lang="zh-cn", log=None,
                                source_lang=cfg["source_lang"])
        try:
            sess.scan()
        except Exception as ex:
            return {"ok": False, "error": self._tr("扫描失败: %s") % ex}
        if not sess.rows:
            return {"ok": False,
                    "error": self._tr("这个归档里没有可翻译的本地化资源")}

        rows, skipped = self._filter_l10n_rows(sess, cfg)
        if not rows:
            return {"ok": False,
                    "error": self._tr("过滤后没有可翻译条目（原语言 %s）")
                    % cfg["source_lang"]}
        for r in rows:
            r["layer"] = "resource"
            r["archive"] = str(target.archive_path or "")

        bstat = self._baseline_fill(rows)
        hits = bstat.get("memory", 0) + bstat.get("manual", 0)
        official_hits = (bstat.get("official", 0)
                         + bstat.get("officialWeak", 0))

        self.l10n_session = sess
        self.l10n_result = None
        self.l10n_undo_root = ""
        self.rows = rows
        meta = sess.meta
        meta["filtered"] = skipped
        meta["sourceLang"] = cfg["source_lang"]
        meta["baseline"] = bstat
        self.meta = meta
        return {
            "ok": True, "rows": rows, "meta": meta, "mode": "resource",
            "tmHits": hits, "officialHits": official_hits,
            "baseline": bstat,
            "root": "", "archive": str(target.archive_path or ""),
            "target": cfg["source_lang"], "source": target.name,
            "extend": target.extend_name(),
            "skippedTarget": skipped,
        }

    def _filter_l10n_rows(self, sess, cfg):
        """按语言筛选，返回 (rows, 跳过数)。

        策略：
          1. 目标语言（zh-cn）的文本一律不要——已经翻译过了。
          2. 优先只留原语言（默认 en-us）的文本。
          3. 若这个 mod 根本没有原语言（例如只有 zh-cn），退而保留其他非目标语言，
             否则会把「唯一可用的原文」一起滤掉。
        """
        target_lang = "zh-cn"
        src = cfg["source_lang"]
        langs = sess.meta.get("languages") or {}
        skipped = 0
        if cfg["skip_target"]:
            skipped = int(langs.get(target_lang, 0))

        # 目标语言里已有的译文先配回源语言行：模组自带 zh-cn 时（如 Nearby Auto
        # Loot 这种 19 语言全的），作者的中文比机翻好，不该被一键机翻盖掉。
        # 配不到的照旧留空，机翻只补真正缺的条目。
        try:
            from localization import pipeline as Lpipe
            all_rows = Lpipe.pair_existing(sess.rows, target_lang, "zh-CN", src)
        except Exception:
            all_rows = sess.rows

        keep = []
        for r in all_rows:
            lg = r.get("lang") or ""
            if cfg["skip_target"] and lg == target_lang:
                continue
            keep.append(r)

        from_src = [r for r in keep if (r.get("lang") or "") == src]
        if from_src:
            dropped_other = len(keep) - len(from_src)
            skipped += dropped_other
            return from_src, skipped
        return keep, skipped

    # ------------------------------------------------------------ 归档复用

    def _archive_sources(self, sess):
        """这次组包用到的源归档路径；有归档缺路径就返回空表（= 不复用）。"""
        out = []
        for one in getattr(sess, "sessions", [sess]):
            arch = str(getattr(getattr(one, "target", None),
                               "archive_path", "") or "")
            if not arch:
                return []
            out.append(arch)
        return out

    def _archive_outputs(self, sess, outdir):
        """算这次会产出的 (归档路径列表, .xl 路径列表)，与组包用的名字一致。"""
        from localization import builder as Lbuild
        outs, xls = [], []
        for one in getattr(sess, "sessions", [sess]):
            target = getattr(one, "target", None)
            name = Lbuild.default_output_name(
                getattr(target, "name", ""),
                getattr(one, "lang", "zh-cn") or "zh-cn")
            outs.append(os.path.join(outdir, name + ".archive"))
            xls.append(os.path.join(outdir, name + ".archive.xl"))
        return outs, xls

    def _reuse_archive(self, sess, items, outdir):
        """上次的产物还能不能直接用；能就返回 {"archive", "xl"}，否则 None。

        判据见 ``localization/reuse.py``：源归档 sha1、译文对指纹、产物 sha1
        三者都对得上才复用。任何一项不同（换了译文、模组更新、产物被删）
        都会退回原来的重打包路径。
        """
        try:
            from localization import reuse as Lreuse
            srcs = self._archive_sources(sess)
            if not srcs:
                return None
            outs, xls = self._archive_outputs(sess, outdir)
            rec = Lreuse.load().get(Lreuse.mod_key(self.root or "", srcs))
            if not rec:
                return None
            files = Lreuse.files_hash(list(outs) + list(xls))
            if not Lreuse.reusable(rec, Lreuse.src_hash(srcs),
                                   Lreuse.pairs_hash(items), files):
                return None
            return {"archive": outs[0] if os.path.exists(outs[0]) else "",
                    "xl": xls[0] if os.path.exists(xls[0]) else ""}
        except Exception:       # noqa: BLE001
            return None

    def _remember_archive(self, sess, items, outdir):
        """打包并校验通过之后记下指纹，下一轮同一条路就能直接复用。"""
        try:
            from localization import reuse as Lreuse
            srcs = self._archive_sources(sess)
            if not srcs:
                return
            outs, xls = self._archive_outputs(sess, outdir)
            files = Lreuse.files_hash(list(outs) + list(xls))
            if not any(files.values()):
                return
            Lreuse.remember(Lreuse.mod_key(self.root or "", srcs),
                            Lreuse.src_hash(srcs), Lreuse.pairs_hash(items),
                            files)
        except Exception:       # noqa: BLE001
            pass

    def _apply_resource(self, items, tx=None):
        """资源模式的「应用」= 组包，产物写进 mod 目录里归档的旁边。"""
        from localization import verifier as Lverify

        sess = self.l10n_session
        if sess is None:
            return {"ok": 0, "fail": len(items), "mode": "resource",
                    "error": self._tr("没有进行中的归档会话，请重新打开 mod。"),
                    "errors": [self._tr("没有进行中的归档会话")]}
        outdir = self._resource_outdir(sess)
        if not outdir:
            return {"ok": 0, "fail": len(items), "mode": "resource",
                    "errors": [self._tr("输出目录不可用")]}

        # 产物落进 mod 目录以后就是实打实的文件了：撤销必须能把它们删掉。
        # 以前它们躺在 userdata\output，删不删无所谓，所以资源模式没有事务。
        #
        # 混合批次由一个事务管两层（见 _apply_mixed）：那种情况下 tx 是传进来的，
        # 这里只往里记快照，提交/丢弃由调用方负责。
        undo_root = self._archive_mod_root(
            getattr(getattr(sess, "target", None), "archive_path", "") or "")
        own_tx = tx is None
        if own_tx and undo_root:
            try:
                import undo_store
                tx = undo_store.ApplyTransaction(undo_root, "生成翻译 mod")
            except Exception:
                tx = None

        applied = []
        # 归档行靠 entryId 定位条目；缺主键时按 (file, 原文) 反查补齐，
        # 一条都补不上就明确报错，别让它表现成「归档读取失败」。
        left = fill_resource_entry_ids(items, sess.rows)
        if left and left == len(items):
            return {"ok": 0, "fail": len(items), "mode": "resource",
                    "errors": [self._tr(
                        "这 %d 条归档行都没有条目 ID，无法定位到归档里的条目。"
                        "请重新加载该模组后再应用。") % len(items)]}
        n = sess.apply_rows(items, collect=applied)
        if not n:
            if own_tx and tx is not None:
                tx.discard()
            return {"ok": 0, "fail": len(items), "mode": "resource",
                    "errors": [self._tr("没有可提交的译文")]}

        # 内容与归档里已有的完全一致 -> 不重建。以前这里照样重打包一遍：
        # 字节、时间戳、Vortex 部署状态全变，玩家看到的东西一个都没变。
        if not sess.changed():
            if own_tx and tx is not None:
                tx.discard()
            prev_archive = prev_xl = ""
            try:
                from localization import builder as Lbuild
                out_name = Lbuild.default_output_name(
                    getattr(sess.target, "name", ""),
                    getattr(sess, "lang", "zh-cn") or "zh-cn")
                prev_archive = os.path.join(outdir, out_name + ".archive")
                prev_xl = os.path.join(outdir, out_name + ".archive.xl")
            except Exception:
                pass
            return {"ok": n, "fail": 0, "mode": "resource", "unchanged": True,
                    "errors": [], "archive": prev_archive if os.path.exists(
                        prev_archive) else "",
                    "xl": prev_xl if os.path.exists(prev_xl) else "",
                    "outdir": str(outdir), "resources": [],
                    "undoId": "", "undoAvailable": False,
                    "canInstall": False, "memorySaved": 0}

        # 归档产物复用（v2.52.0）：源归档与译文对都没变、上次打的包还在，
        # 就不再调 WolvenKit 重打一遍。这是批量里最大的一笔开销 —— 实测 21 个
        # 带归档的模组吃掉整轮 87% 的时间，而其中绝大多数模组一条译文都没变。
        reuse = self._reuse_archive(sess, items, outdir)
        if reuse:
            if own_tx and tx is not None:
                tx.discard()
            return {"ok": n, "fail": 0, "mode": "resource", "unchanged": True,
                    "reused": True, "errors": [],
                    "archive": reuse.get("archive", ""),
                    "xl": reuse.get("xl", ""),
                    "outdir": str(outdir), "resources": [],
                    "undoId": "", "undoAvailable": False,
                    "canInstall": False, "memorySaved": 0}

        # 产物路径能提前算出来（名字固定是 <mod>_zh-cn），先在写之前拍快照，
        # 撤销时按「本来不存在」把它们删掉；同名旧产物会被还原成覆盖前的内容。
        if tx is not None:
            try:
                from localization import builder as Lbuild
                names = set()
                for one in getattr(sess, "sessions", [sess]):
                    names.add(Lbuild.default_output_name(
                        getattr(getattr(one, "target", None), "name", ""),
                        getattr(one, "lang", "zh-cn") or "zh-cn"))
                for out_name in sorted(names):
                    for suffix in (".archive", ".archive.xl"):
                        tx.snapshot(os.path.join(outdir, out_name + suffix))
            except Exception:
                pass

        try:
            result = sess.build(outdir=outdir)
        except Exception as ex:
            import traceback
            traceback.print_exc()
            self._log_failure("apply-resource", traceback.format_exc())
            if own_tx and tx is not None:
                tx.discard()
            return {"ok": 0, "fail": len(items), "mode": "resource",
                    "errors": [str(ex)]}

        report = None
        # 转换失败被跳过的资源：这些行没有进包，不能算「写入成功」。
        skipped_res = [str(x) for x in (getattr(result, "skipped", []) or [])]
        missed_files = set(skipped_res)
        if missed_files:
            missed = [it for it in items
                      if str(it.get("file") or "") in missed_files]
            n = max(0, n - len(missed))
            self._log_failure("apply-resource-skip",
                              "跳过资源: %s" % ", ".join(skipped_res[:5]))
        try:
            parts = getattr(result, "parts", None)
            if parts:
                # 一个模组可能出了好几个包（每个归档一个），逐个校验再合并。
                problems, ok_all = [], True
                for one, res_one in parts:
                    rep = Lverify.verify_build(res_one, one.last_spec,
                                               one.cli, one.workdir)
                    ok_all = ok_all and bool(rep.ok)
                    problems.extend(list(rep.problems))
                report = _MergedReport(ok_all, problems)
            else:
                report = Lverify.verify_build(result, sess.last_spec, sess.cli,
                                              sess.workdir)
        except Exception as ex:
            if own_tx and tx is not None:
                tx.discard()
            return {"ok": n, "fail": len(items) - n, "mode": "resource",
                    "errors": [self._tr("产物校验异常: %s") % ex],
                    "archive": str(result.archive or ""),
                    "xl": str(result.xl_path or "")}

        self.l10n_result = result
        self.l10n_undo_root = undo_root
        undo_id = ""
        if own_tx and tx is not None:
            try:
                undo_id = tx.commit()
            except Exception:
                tx.discard()
                undo_id = ""
        problems = list(report.problems)
        if missed_files:
            problems.insert(0, self._tr(
                "%d 个资源转换失败已跳过（对应 %d 条未写入）：%s")
                % (len(skipped_res), len(missed),
                   "、".join(sorted(missed_files)[:3])))
        # 组包并校验通过才写记忆。以前资源模式一条都不写，同一个 mod 再打开
        # 一次还得重新机翻一遍，官方基线也跟着白跑。
        saved = self._remember_applied(items, applied) if report.ok else 0
        # 打包 + 校验都过了才记复用指纹：校验没过的包下一轮必须重打。
        if report.ok:
            self._remember_archive(sess, items, outdir)
        return {
            "ok": n, "fail": len(items) - n, "mode": "resource",
            "errors": problems[:10],
            "archive": str(result.archive or ""),
            "xl": str(result.xl_path or ""),
            "resources": list(result.resources),
            "warnings": list(result.warnings),
            "report": report.as_dict(),
            "outdir": str(outdir),
            "undoId": undo_id,
            "undoAvailable": bool(undo_id),
            "canInstall": report.ok,
            "memorySaved": saved,
        }

    @staticmethod
    def _memory_origin(kind):
        """把行上的 transSrc 映射成记忆来源。规则见 baseline.memory_origin()。"""
        import baseline as Lbase
        return Lbase.memory_origin(kind)

    def _remember_applied(self, items, applied):
        """把真正写成功的行记进翻译记忆，返回写入条数。

        applied 是 (file, entryId) 的白名单，来自 Session.apply_rows 的回执；
        传 None 表示不筛选。
        """
        if not items:
            return 0
        keep = {tuple(k) for k in applied} if applied is not None else None
        # 资源模式固定产出 zh-cn（见 _filter_l10n_rows 与 Session 的 lang="zh-cn"），
        # 所以记忆语言必须写死，不能跟着 settings.target_lang 走——否则用户把
        # 目标语言切到英文时，这批中文译文会被塞进英文记忆槽。
        tgt = "简体中文"
        saved = 0
        try:
            for it in items:
                orig = str(it.get("orig") or "")
                trans = str(it.get("trans") or "")
                if not orig or not trans:
                    continue
                if keep is not None:
                    key = (str(it.get("file") or ""), str(it.get("entryId") or ""))
                    if key not in keep:
                        continue
                self._memory_store.put(
                    orig, tgt, trans,
                    origin=self._memory_origin(it.get("transSrc")))
                saved += 1
            if saved:
                self._memory_store.save()
        except Exception:
            return 0
        return saved

    def l10n_install(self):
        """把刚组好的产物装进游戏（只写 archive\\pc\\mod 下两个文件）。"""
        from localization import archive as Larch
        from localization import builder as Lbuild

        if self.l10n_result is None:
            return {"ok": False, "error": self._tr("还没有可安装的产物")}
        game = Larch.find_game(self._l10n_cfg()["game_dir"] or None)
        if not game:
            return {"ok": False,
                    "error": self._tr("未找到游戏目录，请先在设置里指定")}
        try:
            written = Lbuild.install(self.l10n_result, game)
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
        return {"ok": True, "files": [str(p) for p in written], "game": game}

    def l10n_uninstall(self):
        from localization import archive as Larch
        from localization import builder as Lbuild

        if self.l10n_result is None:
            return {"ok": False, "error": self._tr("还没有可移除的产物")}
        game = Larch.find_game(self._l10n_cfg()["game_dir"] or None)
        if not game:
            return {"ok": False, "error": self._tr("未找到游戏目录")}
        try:
            removed = Lbuild.uninstall(self.l10n_result, game)
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
        return {"ok": True, "files": [str(p) for p in removed]}

    # ------------------------------------------------------------ 官方语料
    #
    # 官方语言包按主键对齐后可得到 en→zh 的现成译文（约 6 万条 onscreen +
    # 十余万条字幕）。模组里凡是引用官方文本的条目都能直接命中，
    # 术语与语气天然一致，比机翻可靠。

    def l10n_official_stats(self):
        try:
            from localization import official_tm as Lotm
        except Exception as ex:
            return {"exists": False, "error": str(ex)}
        try:
            return Lotm.OfficialTM().stats()
        except Exception as ex:
            return {"exists": False, "error": str(ex)}

    def build_official_tm(self):
        """异步构建官方双语库；进度通过 busy 事件回推。"""
        if self._busy:
            return {"ok": False, "error": "busy"}
        self._busy = True
        self._emit({"type": "busy", "on": True,
                    "text": i18n.tr("正在读取官方语言包 ...")})

        def work():
            try:
                from localization import archive as Larch
                from localization import official_tm as Lotm
                from localization import wkit as Lwkit

                cfg = self._l10n_cfg()
                cli = Lwkit.find_cli(cfg["wkit_path"] or None)
                game = Larch.find_game(cfg["game_dir"] or None)
                if not cli:
                    raise RuntimeError(self._tr("未找到 WolvenKit.CLI.exe"))
                if not game:
                    raise RuntimeError(self._tr("未找到游戏目录"))

                def prog(done, total, label):
                    self._emit({"type": "busy", "on": True,
                                "text": "%s (%d/%d)" % (label, done, total)})

                meta = Lotm.build(game=game, cli=cli,
                                  src_lang="en-us", dst_lang="zh-cn",
                                  progress=prog)
                self._emit({"type": "official-built", "meta": meta})
            except Exception as ex:
                import traceback
                traceback.print_exc()
                self._emit({"type": "error", "action": "official",
                            "message": str(ex)})
            finally:
                self._busy = False
                self._emit({"type": "busy", "on": False})

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "started": True}

    # ------------------------------------------------------------ 批量（单模组功能）
    #
    # v2.42 起，原来那套「扫游戏目录 .xl、只写输出目录」的批量队列整个删掉了。
    # 现在批量跑的就是单模组那条路：复制目录 → 三层提取 → 机翻 → 写回副本，
    # 每个 mod 一个独立会话（见 spawn_worker），原目录全程只读。

    def _batch_out_problem(self, d):
        """输出目录能不能用：返回空串表示可用, 否则返回给用户看的原因。

        这些校验不是客套: ``mod_batch.run_one`` 一开头就
        ``shutil.rmtree(out_root/<mod 名>)``。目录一旦能自由设置, 指到 staging
        或游戏目录就等于删模组本体, 指到盘根就等于清盘。
        """
        base = os.path.abspath(d)
        drive, tail = os.path.splitdrive(base)
        if not (tail or "").strip("\\/"):
            return self._tr("不能直接用盘根目录, 请新建一个子目录")
        cfgd = os.path.normcase(os.path.abspath(config_dir()))
        if os.path.normcase(base) == cfgd:
            return self._tr("不能指向程序自己的数据目录")

        def _inside(a, b):
            a = os.path.normcase(os.path.abspath(a))
            b = os.path.normcase(os.path.abspath(b))
            return a == b or a.startswith(b + os.sep)

        for key, label in (("staging_dir", "安装缓存目录"),
                           ("game_dir", "游戏目录")):
            other = (self.settings.get(key) or "").strip()
            if not other or not os.path.isabs(other):
                continue
            if _inside(base, other) or _inside(other, base):
                return self._tr("不能和%s重叠").replace("%s", self._tr(label))
        return ""

    def _batch_out_mark(self, d):
        """给产物目录盖章 (``.mb-out-root``)。

        「清空输出」是递归删除, 只能清我们自己盖章过的目录: 用户把输出目录填成
        某个已经有东西的文件夹时, 宁可拒绝也不要删别人的文件。旧版固定目录
        (userdata\\batch\\out) 本来就是我们建的, 直接认领。
        """
        p = os.path.join(d, ".mb-out-root")
        if os.path.isfile(p):
            return True
        try:
            legacy = os.path.abspath(legacy_batch_out_dir())
            if os.path.normcase(os.path.abspath(d)) != os.path.normcase(legacy):
                if os.listdir(d):
                    return False
            with open(p, "w", encoding="utf-8") as f:
                f.write("2077ModTranslator batch output\n")
            return True
        except Exception:
            return False

    def _batch_out_check(self, create=True):
        """返回 (目录, 错误)。目录为空串时错误说明原因。"""
        raw = (self.settings.get("batch_out_dir") or "").strip().strip('"').strip("'")
        if not raw:
            return "", self._tr("还没有设置批量输出目录 (留空 = 不进行批量翻译)")
        try:
            d = os.path.abspath(os.path.normpath(raw))
        except Exception:
            return "", self._tr("输出目录路径无效")
        why = self._batch_out_problem(d)
        if why:
            return "", why
        if create:
            try:
                os.makedirs(d, exist_ok=True)
            except Exception as ex:
                return "", self._tr("输出目录创建失败: %s").replace("%s", str(ex))
        elif not os.path.isdir(d):
            return "", self._tr("输出目录不存在")
        if not create:
            return d, ""
        self._batch_out_mark(d)
        return d, ""

    def _batch_out_root(self):
        """批量产物根目录; 不可用时返回空串。

        v2.43.0 起改为读设置 ``batch_out_dir``, 空值就是「不跑」——
        不再有固定的兜底目录, 否则用户以为没在跑, 文件却悄悄写进 userdata。
        """
        return self._batch_out_check()[0]

    # ---------------------------------------------------------- 批量事件 / 续跑
    #
    # 2026-11-01 两次 0xc0000005（python314.dll）：崩溃转储里最后一条事件是
    # mb-item 的 evaluate_js 脚本，说明后台线程直接调 pywebview 的
    # window.evaluate_js 是元凶——那是跨到 WebView2 UI 线程的操作，单次没事，
    # 连着推几十个 mod 就会踩中。改成「后台线程只塞 deque，前端定时 batch_poll
    # 取走」：队列操作只碰 Python 对象，跨线程安全。

    def _batch_log(self, line):
        """批量进度日志。窗口被 C 层崩溃带走时，这里能看到断在第几个 mod。"""
        try:
            d = os.path.join(config_dir(), "logs")
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "batch.log")
            if os.path.exists(p) and os.path.getsize(p) > 512 * 1024:
                os.replace(p, p + ".old")
            with open(p, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%m-%d %H:%M:%S"), line))
        except Exception:
            pass

    def _mb_push(self, obj):
        """把事件放进队列（可在任意线程调用），顺手记日志与续跑记录。"""
        q = getattr(self, "_mb_q", None)
        if q is not None:
            try:
                q.append(obj)
            except Exception:
                pass
        t = obj.get("type") or ""
        if t == "mb-item":
            if obj.get("phase") == "start":
                self._batch_log("start %s/%s %s" % (
                    int(obj.get("index") or 0) + 1, int(obj.get("total") or 0),
                    obj.get("name") or ""))
            else:
                self._batch_log("done  %s ok=%s rows=%s todo=%s applied=%s failed=%s reason=%s %s" % (
                    obj.get("name") or "", obj.get("ok"), obj.get("rows"),
                    obj.get("todo"), obj.get("applied"), obj.get("failed"),
                    json.dumps(obj.get("failReasons") or {}, ensure_ascii=False),
                    obj.get("error") or ""))
                self._mb_journal(obj)
        elif t in ("mb-start", "mb-scanned", "mb-done"):
            keys = ("total", "done", "cancelled", "usable", "cached")
            self._batch_log("%s %s" % (t, {k: obj[k] for k in keys if k in obj}))
        elif t == "mb-stage":
            # 崩在 C 层时 Python 这一侧没有 traceback，日志就是唯一的现场：
            # 阶段（复制/加载/机翻/写回/部署）比「第几个模组」更接近真相。
            self._batch_log("stage %s %s %ss" % (
                obj.get("name") or "", obj.get("label") or "",
                obj.get("seconds")))

    def batch_poll(self, limit=300):
        """前端定时取事件。批量线程不碰 WebView2，只有这里（UI 线程侧的轮询）碰。"""
        out = []
        q = getattr(self, "_mb_q", None)
        if q is not None:
            n = 0
            while q and n < int(limit or 300):
                try:
                    out.append(q.popleft())
                except Exception:
                    break
                n += 1
        return {"ok": True, "events": out,
                "running": bool(self._mb_running or self._mb_scanning)}

    def _mb_journal_path(self):
        d = os.path.join(config_dir(), "batch")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            return ""
        return os.path.join(d, "run.jsonl")

    @staticmethod
    def _mb_sig(staging, with_archive, skip_assets, target, model, out_root="",
                deploy=False, deploy_dry=True):
        """参数指纹：换了模型 / 目标语言 / 复制规则 / 输出目录，上次的续跑记录就作废。

        输出目录必须算进去：换个目录跑，journal 里那些条目指向旧目录, 但「换个
        目录接着续」会让用户以为新目录已经跑过一遍。

        部署开关也要算进去：上一轮没开部署、这一轮开了，那些「已完成」只是把副本
        写好了，还没回写原模组。不换指纹就会被整行跳过，于是勾了覆盖却什么都不发生
        —— 正好是 v2.45.3 修的那个坑的同款症状。
        """
        raw = "|".join([staging or "", "1" if with_archive else "0",
                        "1" if skip_assets else "0", target or "", model or "",
                        os.path.normcase(out_root or ""),
                        "1" if deploy else "0", "1" if deploy_dry else "0"])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def _mb_journal(self, r):
        """每跑完一个 mod 追加一行。崩了以后重跑能接着来，不用从头再等一小时。"""
        p = self._mb_journal_path()
        if not p:
            return
        try:
            rec = {"key": r.get("key"), "ok": bool(r.get("ok")),
                   "dst": r.get("dst") or "", "rows": r.get("rows"),
                   "todo": r.get("todo"), "applied": r.get("applied"),
                   "failed": r.get("failed"), "error": r.get("error") or "",
                   # 失败原因与「没翻出来所以没写」的条数也要留档。只记总数时，
                   # 事后无从判断是没匹配到原文、代码骨架被拒，还是目标 key 缺失。
                   "reasons": r.get("failReasons") or {},
                   "untouched": int(r.get("untouched") or 0),
                   # 目标文件里本来就有的译文（这次没重写）。第 11 轮 17,060 行
                   # 几乎全是这类，留在记录里才能看出「重跑变快」是不是它们省的。
                   "settled": int(r.get("settled") or 0),
                   # S6：整块跳过的模组（已部署且源文件没动）。带了原因才好分辨
                   # 「真的没东西要翻」和「跳错了」。
                   "skipped": r.get("skippedDone") or "",
                   "errors": [str(x) for x in (r.get("errors") or [])][:5],
                   "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _mb_journal_open(self, sig, staging):
        """开始新一轮：写参数指纹头。同参数的旧记录留着续跑，参数变了就重开。"""
        p = self._mb_journal_path()
        if not p:
            return {}
        done = self._mb_journal_read(sig)
        if done:
            return done
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(json.dumps({"__sig": sig, "staging": staging,
                                    "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass
        return {}

    def _mb_journal_read(self, sig):
        """读回同一套参数下已经成功的条目（产物目录还在才算数）。"""
        p = self._mb_journal_path()
        done = {}
        if not p or not os.path.isfile(p):
            return done
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue          # 崩溃留下的半行，跳过
                    if o.get("__sig"):
                        if o["__sig"] != sig:
                            return {}     # 参数变了，旧记录全部作废
                        continue
                    k = str(o.get("key") or "")
                    dst = o.get("dst") or ""
                    if k and o.get("ok") and dst and os.path.isdir(dst):
                        done[k] = o
        except Exception:
            return {}
        return done

    def spawn_worker(self):
        """派生一个独立会话，给批量在副本上跑单模组流程。

        会话状态（rows / root / meta / l10n_session / _stop / _busy）全挂在
        Api 实例上，新开一个实例就天然隔离；翻译记忆与术语表必须共享实例，
        否则每个 mod 都要重读一遍磁盘上的 JSON。
        """
        w = Api()
        w._window = None
        w.settings = dict(self.settings)
        w._memory = self._memory_store
        w._glossary = self._glossary_store
        w._terms = self._terms
        w._emit = lambda obj: None
        return w

    def batch_scan(self, staging=""):
        """异步：列出 staging 下的一级目录并预扫行数。不复制、不翻译。"""
        import locale_pack
        if self._mb_running:
            return {"ok": False, "error": self._tr("批量正在运行")}
        root = (staging or self.settings.get("staging_dir") or "").strip()
        if not root or not os.path.isdir(root):
            return {"ok": False,
                    "error": self._tr("请先在设置里指定 Vortex 安装缓存目录")}
        target = locale_pack.target_code(self.settings.get("target_lang", "简体中文"))
        self._bus.clear()
        self._mb_scanning = True
        self._mb_push({"type": "busy", "on": True,
                       "text": i18n.tr("正在预扫模组目录 ...")})

        def work():
            try:
                import mod_batch as MB
                items = MB.list_dirs(root)
                total = len(items)
                # 部署清单只读一次：127 个模组各读一遍那份（可能上千条 pairs 的）
                # json 会把预扫拖慢好几秒。面板靠它决定要不要给「重译」按钮。
                try:
                    import mod_deploy
                    dep = {k for k, v in (mod_deploy.load().get("mods") or {}).items()
                           if (v or {}).get("pairs")}
                except Exception:
                    dep = set()
                keep = []
                for i, it in enumerate(items):
                    if self._mb_cancel.is_set():
                        break
                    info = MB.probe_text(it["root"], target, measure=True)
                    it.update(info)
                    it["deployed"] = (os.path.normcase(os.path.abspath(it["root"]))
                                      in dep)
                    keep.append(it)
                    if i % 5 == 0 or i == total - 1:
                        self._mb_push({"type": "mb-scan", "done": i + 1,
                                       "total": total, "name": it["name"]})
                self._mb_items = keep
                usable = [x for x in keep if x.get("todo")]
                self._mb_push({"type": "mb-scanned", "items": keep,
                               "total": len(keep), "usable": len(usable),
                               "rows": sum(int(x.get("todo") or 0) for x in keep),
                               "staging": root})
            except Exception as ex:
                import traceback
                traceback.print_exc()
                self._batch_log("scan error %s" % (ex,))
                self._mb_push({"type": "error", "action": "mb-scan",
                               "message": str(ex)})
            finally:
                self._mb_scanning = False
                self._mb_push({"type": "busy", "on": False})

        self._mb_cancel.clear()
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "started": True}

    def batch_start(self, keys, with_archive=False, skip_assets=True):
        """异步跑批量：复制 → 提取 → 机翻 → 写回副本。"""
        if self._mb_running:
            return {"ok": False, "error": self._tr("批量已在运行")}
        items = [it for it in (self._mb_items or []) if it.get("todo")]
        if keys:
            want = {str(k) for k in keys}
            items = [it for it in items if str(it.get("key")) in want]
        if not items:
            return {"ok": False,
                    "error": self._tr("没有可跑的 mod，请先扫描或勾选条目")}
        base = normalize_api_base(self.settings.get("api_base", ""))
        if not base:
            return {"ok": False,
                    "error": self._tr("还没有填写本地模型 API 地址。\n现在打开设置吗?")}
        out_root, out_why = self._batch_out_check()
        if not out_root:
            return {"ok": False, "error": out_why}
        tgt = self.settings.get("target_lang", "简体中文")
        model = self.settings.get("model", "").strip()

        # 续跑：同一套参数下已经跑完、产物目录还在的，直接跳过。
        # 崩溃 / 手动关掉之后再点一次「开始」就接着来，不用从头再等一小时。
        staging = (self.settings.get("staging_dir") or "").strip()
        sig = self._mb_sig(staging, with_archive, skip_assets, tgt, model, out_root,
                           deploy=bool(self.settings.get("batch_deploy")),
                           deploy_dry=bool(self.settings.get("batch_deploy_dry", True)))
        done = self._mb_journal_open(sig, staging)
        cached = [it for it in items if str(it.get("key")) in done]
        if cached:
            items = [it for it in items if str(it.get("key")) not in done]
            self._batch_log("resume: skip %d done, %d left" % (len(cached), len(items)))
        if not items:
            return {"ok": True, "started": False, "cached": len(cached),
                    "total": 0,
                    "message": self._tr("这 %d 个 mod 上次已经跑完了。要重跑请先「清空输出」。")
                               % len(cached)}

        self._mb_running = True
        self._mb_cancel.clear()
        self._bus.clear()
        self._mb_results = []
        self._mb_worker = None
        self._mb_workers = []

        def spawn():
            w = self.spawn_worker()
            self._mb_worker = w
            self._mb_workers.append(w)
            return w

        def work():
            try:
                import mod_batch as MB
                ctx = MB.Ctx(spawn_worker=spawn, out_root=out_root,
                             with_archive=bool(with_archive),
                             skip_assets=bool(skip_assets),
                             base=base, model=model, target=tgt,
                             on_event=self._mb_push, cancel=self._mb_cancel,
                             deploy=bool(self.settings.get("batch_deploy")),
                             deploy_dry=bool(self.settings.get("batch_deploy_dry", True)))
                self._mb_results = MB.run(ctx, items)
            except Exception as ex:
                import traceback
                traceback.print_exc()
                self._batch_log("run error %r" % (ex,))
                self._mb_push({"type": "error", "action": "mb",
                               "message": str(ex)})
            finally:
                try:
                    p = self._batch_deploy_csv(self._mb_results)
                    if p:
                        self._mb_push({"type": "mb-deploy-csv", "path": p})
                except Exception:
                    pass
                try:
                    self._mb_save_last_run(self._mb_results)
                except Exception:
                    pass
                self._mb_running = False
                self._mb_worker = None

        self._batch_log("run start: %d mods, archive=%s assets_skip=%s base=%s model=%s"
                        % (len(items), bool(with_archive), bool(skip_assets),
                           base, model))
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "started": True, "total": len(items),
                "cached": len(cached), "outRoot": out_root}

    def batch_cancel(self):
        self._mb_cancel.set()
        # S4 下可能有两个会话（当前模组 + 正在预处理的下一个模组），都喊停
        workers = list(getattr(self, "_mb_workers", []) or [])
        if getattr(self, "_mb_worker", None) is not None:
            workers.append(self._mb_worker)
        for w in workers:
            try:
                w._stop.set()
            except Exception:
                pass
        return {"ok": True}

    def batch_results(self):
        return {"ok": True, "running": self._mb_running,
                "results": self._mb_results}

    def batch_info(self):
        """面板打开时取一次：staging 目录、输出根、上次扫描结果。"""
        out, out_why = self._batch_out_check(create=False)
        return {"ok": True,
                "staging": (self.settings.get("staging_dir") or "").strip(),
                "outRoot": out,
                "outError": out_why,
                "running": self._mb_running,
                "usable": len([x for x in (self._mb_items or []) if x.get("todo")]),
                "scanned": len(self._mb_items or []),
                "deploy": bool(self.settings.get("batch_deploy")),
                "deployDry": bool(self.settings.get("batch_deploy_dry", True)),
                "hideTodo": bool(self.settings.get("batch_hide_todo", True)),
                "target": self.settings.get("target_lang", "简体中文")}

    def _batch_deploy_csv(self, results):
        """把这一轮的部署结果落成 CSV，返回路径；没部署时返回空串。

        一个 mod 一行：覆盖 / 新增 / 相同 / 失败 / 备注。备注里记的是
        「照覆盖但值得看一眼」的情况（原件已含中文、写入失败……）。
        """
        rows = [r for r in (results or [])
                if isinstance(r.get("deploy"), dict) or r.get("skippedDone")]
        if not rows:
            return ""
        import csv
        p = os.path.join(config_dir(), "batch",
                         "deploy-%s.csv" % time.strftime("%Y%m%d-%H%M%S"))
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["mod", "预演", "覆盖", "新增", "相同",
                            "未译", "失败", "备注"])
                for r in rows:
                    d = r["deploy"] if isinstance(r.get("deploy"), dict) else {}
                    why = []
                    if r.get("skippedDone"):
                        why.append("已是最新，跳过：%s" % r["skippedDone"])
                    if d.get("error"):
                        why.append(str(d["error"]))
                    for n in (d.get("notes") or [])[:5]:
                        why.append("%s: %s" % (n.get("path"), n.get("why")))
                    w.writerow([r.get("name"), "1" if d.get("dry") else "0",
                                d.get("overwritten", 0), d.get("added", 0),
                                d.get("same", 0), r.get("untranslated", 0),
                                r.get("failed", 0), " | ".join(why)])
            self._batch_log("deploy csv: %s" % p)
        except Exception as ex:
            self._batch_log("deploy csv 失败: %r" % (ex,))
            return ""
        return p

    def _mb_last_run_path(self):
        d = os.path.join(config_dir(), "batch")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            return ""
        return os.path.join(d, "last-run.json")

    def _mb_save_last_run(self, results):
        """把这轮结果留一份在磁盘上，「导出结果」重开程序之后还能导。

        结果原来只在内存里：跑完 49 个模组、升个版本或重启一次就没了，而重跑
        会因续跑把这些模组整块跳过——用户再也导不出这一轮的结果。
        """
        p = self._mb_last_run_path()
        if not p or not results:
            return ""
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "results": results}, f, ensure_ascii=False)
            return p
        except Exception as ex:
            self._batch_log("last-run.json 写入失败: %r" % (ex,))
            return ""

    def batch_export(self):
        """把本轮跑完的 mod 存成 CSV（一行一个，Excel 双击就开）。

        导的是面板上那张表此刻的内容：数据来自 `_mb_results`，取消掉的、
        还没轮到的不算跑完。重开程序之后这个属性是空的，退回读 `last-run.json`
        ——那是上一轮跑完时落下的同一份结果。
        """
        if self._mb_running:
            return {"ok": False, "error": self._tr("批量正在运行，跑完再导出")}
        import mod_batch as MB
        rows = MB.export_rows(self._mb_results)
        stale = False
        if not rows:
            try:
                with open(self._mb_last_run_path(), "r", encoding="utf-8") as f:
                    rows = MB.export_rows(json.load(f).get("results") or [])
                stale = bool(rows)
            except Exception:
                rows = []
        if not rows:
            return {"ok": False, "error": self._tr("本轮还没有跑完的 mod")}
        default = "batch-results-%s.csv" % time.strftime("%Y%m%d-%H%M%S")
        try:
            path = self._window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default,
                file_types=("CSV (*.csv)",))
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
        if isinstance(path, (list, tuple)):
            path = path[0] if path else ""
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            import csv
            folder = os.path.dirname(path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(MB.EXPORT_COLUMNS)
                w.writerows(rows)
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
        self._batch_log("results csv: %s (%d rows)" % (path, len(rows)))
        return {"ok": True, "path": path, "rows": len(rows), "stale": stale}

    def batch_open_output(self):
        d, why = self._batch_out_check(create=False)
        if not d:
            return {"ok": False, "error": why}
        try:
            os.startfile(d)
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
        return {"ok": True, "path": d}

    def batch_forget(self, key):
        """丢弃这个模组上一轮写进翻译记忆的译文，并删掉它的续跑记录。

        复原流程是「解压还原 → 重跑」，而还原只把文件换回原件：翻译记忆里那一
        份（可能正是要丢掉的那份坏译文）还在，重跑会先命中 TM、原样搬回来，看着
        像「改了没生效」。这里按部署清单记的 (原文, 译文) 对调 `forget()`，只删
        machine/memory 来源，手改与官方语料不动；再把 run.jsonl 里这个模组那行
        删掉，否则续跑还会整行跳过它。
        """
        if self._mb_running:
            return {"ok": False, "error": self._tr("批量正在运行")}
        key = str(key or "")
        root = ""
        for it in (self._mb_items or []):
            if str(it.get("key")) == key:
                root = it.get("root") or ""
                break
        if not root:
            staging = (self.settings.get("staging_dir") or "").strip()
            cand = os.path.join(staging, key) if staging and key else ""
            if cand and os.path.isdir(cand):
                root = cand
        if not root or not os.path.isdir(root):
            return {"ok": False, "error": self._tr("找不到这个模组目录")}
        import mod_deploy
        tgt = self.settings.get("target_lang", "简体中文")
        try:
            n = mod_deploy.forget_in(root, self._memory_store, tgt)
        except Exception as ex:      # noqa: BLE001 - 记忆库坏了不该让面板崩
            return {"ok": False, "error": str(ex)}
        if n:
            try:
                self._memory_store.save()
            except Exception:
                pass
        dropped = self._mb_journal_drop(key)
        return {"ok": True, "forgotten": n, "journal": dropped,
                "root": root, "pairs": len(mod_deploy.pairs(root))}

    def _mb_journal_drop(self, key):
        """把 run.jsonl 里这个模组那行删掉（续跑就不会再跳过它）。"""
        p = self._mb_journal_path()
        if not p or not os.path.isfile(p):
            return False
        key = str(key or "")
        kept, hit = [], False
        try:
            with open(p, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line.strip():
                        continue
                    if not line.lstrip().startswith("{"):
                        kept.append(line)
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        kept.append(line)
                        continue
                    if "__sig" in rec:
                        kept.append(line)
                    elif str(rec.get("key")) == key:
                        hit = True
                    else:
                        kept.append(line)
            if not hit:
                return False
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(kept) + "\n")
            os.replace(tmp, p)
            return True
        except Exception:
            return False

    def batch_clear(self):
        """清空批量输出（下次重跑覆盖）。只清我们自己盖过章的目录。

        目录可配置之后, 这段代码就是「递归删任意用户指定的目录」, 必须先确认
        那目录是本工具建的: 没有 ``.mb-out-root`` 标记就拒绝。
        """
        if self._mb_running:
            return {"ok": False, "error": self._tr("批量正在运行")}
        d, why = self._batch_out_check(create=False)
        if not d:
            return {"ok": False, "error": why}
        if not os.path.isfile(os.path.join(d, ".mb-out-root")):
            # 旧版固定目录本来就是我们建的, 直接认领; 其余目录一律拒绝。
            if os.path.normcase(d) != os.path.normcase(
                    os.path.abspath(legacy_batch_out_dir())):
                return {"ok": False,
                        "error": self._tr("这个目录里没有批量产物标记 ("
                                          ".mb-out-root), 已拒绝清空")}
            self._batch_out_mark(d)
        import shutil
        n = 0
        try:
            for name in os.listdir(d):
                if name == ".mb-out-root":
                    continue
                p = os.path.join(d, name)
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                    n += 1
                except OSError:
                    pass
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
        # 续跑记录一起删掉，否则「清空输出」后重跑还会被当成已完成跳过。
        try:
            jp = self._mb_journal_path()
            if jp and os.path.isfile(jp):
                os.remove(jp)
        except Exception:
            pass
        # 上一轮的导出报告也一起删：产物没了，那份报告只剩误导。
        try:
            lp = self._mb_last_run_path()
            if lp and os.path.isfile(lp):
                os.remove(lp)
        except Exception:
            pass
        return {"ok": True, "removed": n}

    # ------------------------------------------------------------ 语言包
    def locale_scan(self):
        import locale_pack
        root = self.root or ""
        if not root or not os.path.isdir(root):
            return {"ok": False, "files": [], "error": self._tr("请先选择有效的模组目录")}
        target = locale_pack.target_code(self.settings.get("target_lang", "简体中文"))
        files = locale_pack.detect(root, parse=True, target=target)
        locales = []
        seen = set()
        for f in files:
            if f["locale"] and f["locale"] not in seen:
                seen.add(f["locale"])
                locales.append({"code": f["locale"],
                                "label": locale_pack.LOCALE_LABELS.get(f["locale"],
                                                                        f["locale"])})
        return {"ok": True, "files": files, "locales": locales,
                "targets": [{"code": c, "label": l} for c, l in
                            locale_pack.LOCALE_LABELS.items()]}

    def load_locale(self, indices, target):
        import locale_pack
        # 与 load_mod / load_resource 保持一致：机翻在跑的时候不能换表格。
        # 这条路径直接替换 self.rows，正在跑的 _run_translate 会按旧索引读到
        # 新表，轻则 IndexError，重则把上一批译文写到别的行上。
        if self._busy:
            return {"ok": False, "error": "busy"}
        root = self.root or ""
        files = locale_pack.detect(root)
        picked = [files[int(i)] for i in indices if 0 <= int(i) < len(files)]
        # 当前目标语言文件只能作为「已有译文」来源回填, 不能当原文再抽一遍。
        picked = [f for f in picked if f["locale"] != target]
        if not picked:
            return {"ok": False,
                    "error": self._tr("所选文件都是当前目标语言，已跳过。请选择源语言文件。")}
        rows, meta = locale_pack.extract(root, picked, target)
        hits = 0
        if self.settings.get("use_tm", True):
            tgt = self.settings.get("target_lang", "简体中文")
            for r in rows:
                t = self._memory_store.get(r["orig"], tgt)
                if t:
                    r["trans"] = t
                    hits += 1
            if hits:
                self._memory_store.save()
        self.rows = rows
        self.meta = meta
        return {"ok": True, "rows": rows, "meta": meta, "tmHits": hits,
                "root": root, "target": target}

    # ------------------------------------------------------------ 机翻
    def translate(self, indices=None, skip_indices=None, force=False):
        """机翻。force=True 是「重新翻译」：跳过基线，把这几行重新发给模型。

        普通机翻会先查官方语料与翻译记忆，命中就不发模型。用户右键点
        「重新翻译」时要的正好相反——手上有译文、但不想再用它，希望模型
        重新给一个。所以 force 模式既不看现有译文，也不查基线。
        """
        import mt_client
        if self._busy:
            return {"ok": False, "error": "busy"}
        skip = set()
        for x in (skip_indices or []):
            try:
                skip.add(int(x))
            except Exception:
                pass
        if indices is None:
            targets = [i for i, r in enumerate(self.rows)
                       if i not in skip and not r["trans"].strip()]
        else:
            targets = [int(i) for i in indices
                       if 0 <= int(i) < len(self.rows) and int(i) not in skip]
        if not targets:
            return {"ok": False, "error": self._tr("没有「未填译文」的行。")}
        base = normalize_api_base(self.settings.get("api_base", ""))
        if not base:
            return {"ok": False, "error": self._tr("还没有填写本地模型 API 地址。\n现在打开设置吗?")}
        if not self.settings.get("local_model_enabled", True):
            return {"ok": False,
                    "error": self._tr("本地模型已停用。在设置里重新启用后再翻译。")}
        self._busy = True
        self._stop.clear()
        tgt = self.settings.get("target_lang", "简体中文")
        model = self.settings.get("model", "").strip()

        def work():
            try:
                self._run_translate(targets, base, model, tgt, force=bool(force))
            except Exception as ex:
                self._emit({"type": "error", "message": str(ex)})
            finally:
                self._busy = False

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "total": len(targets)}

    def _trans_report(self, targets, reasons=None, stopped=False, memory=0,
                      official=0, dedup=0, rows=None, force=False, written=None):
        """机翻结束后统一报一次账：翻了多少、还剩多少、为什么剩、去重省了多少。

        「还剩」不只看异常：模型偶尔会返回空串，那种也算没翻出来。
        前端据此弹最上层的提示窗，给「继续 / 确认」两个选择。

        rows 用调用方那一份快照：机翻期间 self.rows 万一被别的路径换掉，
        结算也必须对着当初发出去的那批行算。

        force（重新翻译）必须按「本轮真的写回了哪些行」结算：这些行上本来
        就有旧译文，按 trans 非空算会把模型失败的行也报成成功，用户点了
        「继续翻译剩下的」也补不回来。
        """
        rows = self.rows if rows is None else rows
        ok = 0
        left = []
        for i in targets:
            try:
                if force:
                    filled = i in (written or ())
                else:
                    filled = bool((rows[i]["trans"] or "").strip())
            except Exception:
                filled = False
            if filled:
                ok += 1
            else:
                left.append(int(i))
        rep = {"type": "trans-report", "total": len(targets), "ok": ok,
               "left": len(left), "failed": left[:800],
               "memory": int(memory or 0),
               "official": max(0, int(official or 0)),
               # 同文去重省下的模型调用次数。前端会显示成「同文合并 N 条」。
               "dedup": max(0, int(dedup or 0)),
               "stopped": bool(stopped),
               # 前端「继续翻译剩下的」要沿用同一模式，否则补翻会退回基线，
               # 旧译文原样填回来，看起来像补成功了。
               "force": bool(force),
               "reasons": [r for r in (reasons or []) if r]}
        self._emit(rep)
        return rep

    def _run_translate(self, targets, base, model, tgt, force=False):
        # translate() 与 _run_translate() 是两个独立方法。mt_client 必须在
        # 真正执行翻译的方法里导入；只在 translate() 内 import 会让工作线程
        # 报 NameError: name 'mt_client' is not defined，表现为每次机翻都失败。
        import mt_client

        # 整轮只认这一份快照。机翻要跑几分钟，期间哪怕有路径换掉了 self.rows，
        # 后果也只会是"这一轮白跑"，而不是把 A 句的译文写进 B 行。
        rows = self.rows

        # 1) 先过基线：手改 > 官方译文 > 翻译记忆。
        #
        # 以前这里只查翻译记忆，官方语料只出现在提示词里。整句命中官方译法
        # 的那部分（实测某个模组的界面词有 30%）本来可以一条都不发给模型。
        before = {}
        for i in targets:
            before[i] = str(rows[i].get("trans") or "").strip()
        if force:
            # 「重新翻译」：基线整个跳过。官方语料与翻译记忆里存的多半就是
            # 用户当下不满意的那个译法，查了等于原样填回来。
            bstat = {"official": 0, "officialWeak": 0}
        else:
            bstat = self._baseline_fill([rows[i] for i in targets])
        # 官方给的都算 official：稳的 + 待核的都省掉了模型调用
        official_n = (int(bstat.get("official", 0) or 0)
                      + int(bstat.get("officialWeak", 0) or 0))
        hits = []
        misses = []
        kept = 0
        # 本轮真正写回的行。force 模式下这些行本来就有旧译文，结算必须按
        # 这份集合算，否则模型失败的行会被旧译文冒充成「已完成」。
        written = set()
        if force:
            misses = list(targets)
        for i in ([] if force else targets):
            now = str(rows[i].get("trans") or "").strip()
            if now and not before[i]:
                # 带上来源，前端才能画色条、导出才能写对「译文来源」。
                hits.append({"i": i, "trans": now,
                             "src": rows[i].get("transSrc") or ""})
            elif not now:
                misses.append(i)
            else:
                # 发出去时是空的、回来时后端已经有译文：说明这行在前端被清空
                # 过但没同步到后端。既不重翻也不覆盖，但要算进账——以前这类
                # 行既不在 hits 也不在 misses，报表里凭空少一条，表现就是
                # 「翻译了 0 条」而用户明明点了。
                kept += 1
        mem_n = max(0, len(hits) - official_n)
        if hits:
            self._emit({"type": "rows", "items": hits})
            self._emit({"type": "progress", "done": len(hits), "total": len(targets),
                        "memory": len(hits)})
        if not misses:
            self._emit({"type": "done", "translated": len(hits) + kept,
                        "memory": len(hits), "stopped": False})
            self._trans_report(targets, memory=mem_n, official=official_n,
                               rows=rows, force=force, written=written)
            return

        try:
            mdl = mt_client.resolve_model(base, model)
        except Exception as ex:
            self._emit({"type": "error", "message": str(ex)})
            self._trans_report(
                targets, memory=mem_n, official=official_n,
                reasons=[self._tr("本地模型接口连不上：%s") % ex], rows=rows,
                force=force, written=written)
            return

        done = len(hits) + kept
        # 批大小：短文本 8 条一批（实测 0.11 秒/行），长文本降到 4 条。
        # 长文本一批的输出更容易超预算，一旦解析失败要退化成逐条，代价是 4~8 倍。
        avg_len = (sum(len(str(rows[i].get("orig") or "")) for i in misses)
                   / max(1, len(misses)))
        batch = 4 if avg_len > 160 else 8

        # 2) 按原文去重：同一句话只发给模型一次。
        #
        # 模型温度不为 0 时，同一个输入可能给出两种译法；重复发送既浪费上下文，
        # 又会让同一个词在同一个 mod 里前后不一致（实测有的 mod 里同一句
        # "Close" 出现 40 多次）。这里按原文分组，只翻一次，再展开回所有重复行，
        # 同一个原文的译文必然一致。key 用 strip 后的文本归组，但发给模型和写进
        # 记忆的仍是行上的原始文本，免得记忆键和查询键对不上。
        groups = {}
        rep_text = {}
        order = []
        for i in misses:
            raw = str(rows[i].get("orig") or "")
            key = raw.strip()
            if key not in groups:
                groups[key] = []
                rep_text[key] = raw
                order.append(key)
            groups[key].append(i)
        dedup_saved = len(misses) - len(order)

        # 占位符密集的串不进批量（P17）：批量里这类最容易整条被跳过或原样
        # 返回，而它们恰恰是最不能出错的一批（通知模板、多行说明）。
        def _dense(s):
            return (len(re.findall(r"\{[^{}]*\}", s or "")) >= 3
                    or (s or "").count("\n") >= 4)

        def _echo(out, src):
            """模型把英文原样吐回来。以前它照样算「已填」。"""
            a = (out or "").strip()
            b = (src or "").strip()
            if not a or a != b:
                return False
            return bool(re.search(r"[A-Za-z]{3,}", b))

        heavy = [k for k in order if _dense(rep_text[k])]
        if heavy:
            _hs = set(heavy)
            order = [k for k in order if k not in _hs]
        blanks = []          # 批量里被模型跳过的 key（空串 / 原样返回）
        echo_n = 0

        # 预热：先把模型载入并按 keep_alive 保温。首次载入 27B 要 35-80 秒，
        # 以前这段时间整段算在第一个批次的 180 秒里（实测日志里空闲后的首个
        # 请求就是 1m30s / 45s）。只有 Ollama 原生 /api/chat 认 keep_alive，
        # 换个后端会 404，那不算错误，继续走 OpenAI 兼容路径。
        self._emit({"type": "progress", "done": done, "total": len(targets),
                    "memory": len(hits),
                    "note": self._tr("正在载入本地模型（首次约需 1 分钟）…")})
        # 一轮批量里每个模组都会走到这里。模型已经载入、keep_alive 2 小时，
        # 再 warmup 一次就是白花一次往返（127 个模组 = 127 次）。同一对
        # (接口, 模型) 半小时内只热一次。
        try:
            now = time.time()
            warm = (getattr(self, "_mt_warm_key", None), getattr(self, "_mt_warm_at", 0))
            if warm[0] != (base, mdl) or (now - warm[1]) > 1800:
                mt_client.warmup(base, mdl)
                self._mt_warm_key = (base, mdl)
                self._mt_warm_at = now
        except Exception:
            pass

        failed_batches = 0
        failed_rows = 0
        consec = 0
        last_err = ""
        for k in range(0, len(order), batch):
            if self._stop.is_set():
                self._emit({"type": "done", "translated": done, "memory": len(hits),
                            "stopped": True})
                self._trans_report(
                    targets, memory=len(hits) - official_n, official=official_n,
                    stopped=True, dedup=dedup_saved,
                    reasons=[self._tr("已手动停止，剩下的还没有翻译")],
                    rows=rows, force=force, written=written)
                return
            keys = order[k:k + batch]
            texts = [rep_text[c] for c in keys]
            terms = self._glossary_store.relevant(texts)
            gloss = self._glossary_store.prompt_block(terms)
            # 官方语料挖出来的术语：让整批译文在专名与术语上保持一致。
            # 已经在用户术语表里的词不再重复（用户的优先）。
            if self.settings.get("use_official_tm", True):
                th = self._term_hints
                if th is not None:
                    try:
                        skip = {str(t.get("src", "")).strip().lower()
                                for t in (terms or []) if t.get("src")}
                        extra = th.prompt_block(texts, skip=skip)
                        if extra:
                            gloss = (gloss + "\n\n" + extra).strip()
                    except Exception:
                        pass
            try:
                outs = mt_client.translate_batch(base, mdl, texts, target=tgt,
                                                 glossary=gloss)
            except Exception as ex:
                # 单批失败不再作废整轮。超时是偶发的（实测 58 个请求里 2 次），
                # 以前一次就把已翻好的部分晾在那里、要用户重新点一遍；长任务
                # 还有一半没跑完就整轮中止，等于白跑。
                failed_batches += 1
                failed_rows += sum(len(groups[c]) for c in keys)
                consec += 1
                last_err = str(ex)
                self._emit({"type": "progress", "done": done,
                            "total": len(targets), "memory": len(hits),
                            "note": self._tr("这一批没翻成，先跳过：%s") % ex})
                # 连续失败且一条都没翻出来 = 服务多半挂了，再跑也是白等。
                if consec >= 5 and done == len(hits):
                    self._emit({"type": "error", "message": str(ex)})
                    self._trans_report(
                        targets, memory=len(hits) - official_n, official=official_n,
                        stopped=True, dedup=dedup_saved,
                        reasons=[self._tr("调用本地模型失败：%s") % ex],
                        rows=rows, force=force, written=written)
                    return
                continue
            consec = 0
            items = []
            for key, out in zip(keys, outs or []):
                text = (out or "").strip()
                if not text:
                    # 批量里被整条跳过。以前这里静默 continue，用户点应用时
                    # 才发现文件只写回了一半，而报告里只有一句「空译文 N 条」。
                    blanks.append(key)
                    continue
                if _echo(text, rep_text[key]):
                    echo_n += 1
                    blanks.append(key)
                    continue
                for i in groups[key]:
                    rows[i]["trans"] = text
                    rows[i]["transSrc"] = "machine"
                    items.append({"i": i, "trans": text, "src": "machine"})
                    written.add(i)
                    # 记忆按行上的原始原文存，和 _baseline_fill 的查询键一致
                    self._memory_store.put(rows[i]["orig"], tgt, text)
                done += len(groups[key])
            self._memory_store.save()
            self._emit({"type": "rows", "items": items})
            self._emit({"type": "progress", "done": done, "total": len(targets),
                        "memory": len(hits)})

        # ---- 单条补翻：占位符密集的 + 批量里被跳过的（P12 / P17）
        #
        # 批量提示词对「2-4 个词的按钮名」和「一堆 {…} 占位符的通知模板」都
        # 不友好，模型会整条跳过或原样返回。以前这些行既不写盘也不报警，用户
        # 点完应用只看到「成功 26 条」，剩下的 5 条静默消失。
        singles = list(heavy) + list(blanks)
        retried = 0
        if singles and not self._stop.is_set():
            left = []
            for n_s, key in enumerate(singles, 1):
                if self._stop.is_set():
                    break
                src_text = rep_text[key]
                gl = ""
                try:
                    gl = self._glossary_store.prompt_block(
                        self._glossary_store.relevant([src_text]))
                except Exception:
                    gl = ""
                text = ""
                for attempt in (0, 1):
                    try:
                        text = mt_client.translate_one(base, mdl, src_text,
                                                       target=tgt, glossary=gl)
                    except Exception:
                        text = ""
                    text = (text or "").strip()
                    if text and not _echo(text, src_text):
                        break
                    if attempt == 0:
                        # 第二次把「占位符与换行必须原样保留」说死, 再说一遍
                        # 「只翻译自然语言部分」——否则模型仍然整条照抄。
                        gl = (gl + "\n\n保留 {…} 占位符与 \\n 换行原样不动，"
                                   "只翻译自然语言部分。").strip()
                if not text or _echo(text, src_text):
                    left.append(key)
                    continue
                made = []
                for i in groups[key]:
                    rows[i]["trans"] = text
                    rows[i]["transSrc"] = "machine"
                    made.append({"i": i, "trans": text, "src": "machine"})
                    written.add(i)
                    self._memory_store.put(rows[i]["orig"], tgt, text)
                done += len(groups[key])
                retried += 1
                self._emit({"type": "rows", "items": made})
                self._emit({"type": "progress", "done": done,
                            "total": len(targets), "memory": len(hits),
                            "note": self._tr("单条补翻 %d / %d") % (n_s, len(singles))})
            blanks = left
            # 补翻循环里以前每成一条就 save 一次：每次都要把整份记忆（十几万
            # 条、好几 MB）重新序列化再写盘，崩溃窗口被放大到每条一次。攒到
            # 循环结束落一次盘就够了。
            self._memory_store.save()

        # 重翻时旧译文还在，按 trans 非空判断会把失败行也算成「翻好了」。
        if force:
            empty = [i for i in misses if i not in written]
        else:
            empty = [i for i in misses if not (rows[i]["trans"] or "").strip()]
        reasons = []
        if failed_batches:
            # 跳过的批次也要说清楚为什么，否则用户只看到「少了几条」。
            if last_err:
                reasons.append(self._tr("调用本地模型失败：%s") % last_err)
            reasons.append(self._tr("有 %d 批没翻成（共 %d 条），可以直接再点一次补翻")
                           % (failed_batches, failed_rows))
        if empty:
            reasons.append(self._tr("模型返回了空译文（%d 条）") % len(empty))
        if echo_n:
            reasons.append(self._tr(
                "模型把英文原样返回（%d 条，已按单条重试）") % echo_n)
        if retried:
            reasons.append(self._tr("单条补翻成功 %d 条") % retried)
        self._emit({"type": "done", "translated": done, "memory": len(hits),
                    "stopped": False})
        self._trans_report(targets, memory=len(hits) - official_n,
                           official=official_n, reasons=reasons,
                           dedup=dedup_saved, rows=rows,
                           force=force, written=written)

    def stop_translate(self):
        self._stop.set()
        return True

    def _forget_memory_for(self, rows):
        """让这些原文对应的机翻记忆失效，返回删掉的条数。

        清空译文等于「这条我不要了」。机翻结果在写回表格的同时就进了 TM，
        只清表格的话，下一轮 _baseline_fill 会原样填回来——用户看到的是
        「清空了也白清」。手改与官方来源的条目不动，见 tm.Memory.forget。
        """
        tgt = self.settings.get("target_lang", "简体中文")
        n = 0
        for r in (rows or []):
            try:
                if self._memory_store.forget(r.get("orig") or "", tgt):
                    n += 1
            except Exception:
                pass
        if n:
            try:
                self._memory_store.save()
            except Exception:
                pass
        return n

    def clear_translations(self):
        """清空当前提取结果的译文，保留原文行。"""
        for r in self.rows:
            r["trans"] = ""
            r.pop("transSrc", None)
        dropped = self._forget_memory_for(self.rows)
        return {"ok": True, "forgot": dropped}

    def clear_row_translations(self, indices=None):
        """清空指定行的译文（右键「清空选中的行译文」、编辑条里删空）。

        前端清空只改了浏览器内存，后端 self.rows 还留着旧译文。下次
        translate() 会把那些行当成「本来就有译文」而跳过，表现就是
        「清空之后点翻译没反应」。这里补上同步；顺便让对应记忆失效，
        重翻才会真的再问一次模型，而不是把同一条从 TM 里搬回来。
        """
        if self._busy:
            return {"ok": False, "error": "busy"}
        picked = []
        for x in (indices or []):
            try:
                i = int(x)
            except Exception:
                continue
            if 0 <= i < len(self.rows):
                picked.append(self.rows[i])
        for r in picked:
            r["trans"] = ""
            r.pop("transSrc", None)
        dropped = self._forget_memory_for(picked)
        return {"ok": True, "count": len(picked), "forgot": dropped}

    def clear_rows(self):
        """清空当前提取结果和后端缓存的全部行。"""
        if self._busy:
            return {"ok": False, "error": "busy"}
        self.rows = []
        self.meta = {}
        return {"ok": True}

    # ------------------------------------------------------------ 回填
    def apply(self, items, force=False):
        """异步: 立刻返回, 结果通过 applied / error 事件回推"""
        if not items:
            return {"ok": False, "error": self._tr("还没有可应用的译文。")}
        if self._busy:
            return {"ok": False, "error": "busy"}
        # 写回前对账（P12）：同一批文件里还有没译的行，说明这次只会写回一部分。
        # 以前是写完了用户才发现「少了 5 条」，而报告里只有一句「空译文 5 条」。
        if not force:
            pending = self._pending_in_same_files(items)
            if pending.get("count"):
                return {"ok": True, "started": False,
                        "pending": pending["count"],
                        "pendingFiles": pending["files"][:5],
                        "error": ""}
        self._busy = True
        self._emit({"type": "busy", "on": True, "text": i18n.tr("正在写回模组文件 ...")})

        def work():
            try:
                r = self._do_apply(items)
                if int(r.get("fail") or 0) and r.get("errors"):
                    self._log_failure(
                        "apply",
                        "fail=%s ok=%s\n%s" % (
                            r.get("fail"), r.get("ok"),
                            "\n".join(str(x) for x in r["errors"][:10])))
                self._emit(dict(r, type="applied"))
            except Exception as ex:
                import traceback
                traceback.print_exc()
                self._log_failure("apply", traceback.format_exc())
                self._emit({"type": "error", "action": "apply",
                            "message": str(ex)})
            finally:
                self._busy = False
                self._emit({"type": "busy", "on": False})

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "started": True}

    def apply_one_row(self, item):
        """单条覆盖写回（P16）。

        只给 soft 级失败用：代码碎片这类「形态可疑，但用户已经看过上下文、
        确定是文案」的行。批量永远不开放这条路 —— 一次写坏几十行，撤销只能
        回滚文件，回滚不了用户没看见的错误。
        """
        if not item:
            return {"ok": False, "error": self._tr("没有要写入的条目。")}
        if self._busy:
            return {"ok": False, "error": "busy"}
        self._busy = True
        self._emit({"type": "busy", "on": True, "text": i18n.tr("正在写回模组文件 ...")})

        def work():
            try:
                r = self._do_apply([item], allow_soft=True)
                self._emit(dict(r, type="applied", single=True))
            except Exception as ex:
                import traceback
                self._log_failure("apply", traceback.format_exc())
                self._emit({"type": "error", "action": "apply", "message": str(ex)})
            finally:
                self._busy = False
                self._emit({"type": "busy", "on": False})

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "started": True}

    def mark_as_code(self, file, orig):
        """把 (相对路径, 原文) 记进「这是代码」排除表, 以后不再当候选。

        用户点一次「标记为代码」，比每次应用都撞一次「疑似代码片段」有用得多；
        这也是唯一能把用户判断留下来的形态（弹窗本身不留任何东西）。
        """
        import user_skip
        root = self.root or ""
        if not root or not file:
            return {"ok": False, "error": self._tr("还没有加载模组。")}
        ok, n = user_skip.add(root, file, orig)
        if not ok:
            return {"ok": False, "error": self._tr("写入排除表失败。")}
        # 已经加载在表格里的行一并标掉, 不用重新加载
        hit = 0
        for r in (self.rows or []):
            if (str(r.get("file") or "") == str(file)
                    and str(r.get("orig") or "") == str(orig)):
                r["skip"] = 1
                r["codeMarked"] = 1
                hit += 1
        return {"ok": True, "marked": hit, "total": n}

    def remember_skips(self, items):
        """记住 / 忘掉「跳过」勾选，下次加载同一行默认就是勾上的。

        items: [[相对路径, 原文, 是否跳过], …]。只对明文层（.reds/.lua）生效——
        跳过状态是按 (相对路径, 原文 sha1) 记的，归档层的行没有磁盘路径。
        """
        import user_skip
        root = self.root or ""
        if not root:
            return {"ok": False, "error": self._tr("还没有加载模组。")}
        n = 0
        for it in (items or []):
            try:
                rel, orig, value = it[0], it[1], bool(it[2])
            except Exception:
                continue
            if not rel or not str(orig or "").strip():
                continue
            ok, _cnt = user_skip.set_skip(root, rel, orig, value)
            if ok:
                n += 1
        return {"ok": True, "remembered": n}

    def _pending_in_same_files(self, items):
        """这次要写的文件里，还有多少条没译的行。

        只统计「同一次提交里出现的文件」——别的文件没译跟这次写回没关系，
        拿它们凑数只会让提示变成噪音。
        """
        files = {str(it.get("file") or "") for it in items}
        files.discard("")
        counts = {}
        for r in (self.rows or []):
            f = str(r.get("file") or "")
            if f not in files:
                continue
            if r.get("already") or r.get("skip"):
                continue
            if not str(r.get("orig") or "").strip():
                continue
            if str(r.get("trans") or "").strip():
                continue
            counts[f] = counts.get(f, 0) + 1
        return {"count": sum(counts.values()),
                "files": sorted(counts, key=lambda x: -counts[x])}

    def _log_failure(self, kind, detail):
        """把会冒到用户面前的失败落到文件里。

        exe 是 windowed 的，traceback 打到 stderr 就等于扔了；用户报「应用失败」
        时没有现场可查，只能回去猜。这里留一份最小现场：时间 / 版本 / 错误。
        日志不参与任何逻辑，写失败也吞掉。
        """
        try:
            import time
            d = os.path.join(config_dir(), "logs")
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "%s.log" % re.sub(r"[^A-Za-z0-9_-]+", "_", kind))
            # 超过 256 KB 就直接重开，避免无限增长
            if os.path.exists(p) and os.path.getsize(p) > 256 * 1024:
                os.replace(p, p + ".old")
            with open(p, "a", encoding="utf-8") as f:
                f.write("=" * 60 + "\n")
                f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), VERSION))
                f.write("root: %s\n" % (self.root or ""))
                f.write("archive: %s\n" % (self.archive or ""))
                f.write("%s\n" % detail)
        except Exception:
            pass

    def _do_apply(self, items, allow_soft=False, keep_backup=True, make_undo=True):
        """按行的来源分流：归档资源行走组包，其余写回明文源文件。

        一个模组可能两层都有（.reds 里有硬编码文案，.archive 里有本地化表），
        必须各走各的路径，不能让一层的产物把另一层顶掉。

        keep_backup / make_undo 只在批量里关掉：批量把整个模组复制到输出根、
        在副本上写，写完这份副本就是交付物。留 633 个 `.bak`（19 MB）和 127 条
        指向副本的撤销记录，既进不了游戏又白占磁盘；改错了重跑一遍就行。
        """
        if not items:
            return {"ok": 0, "fail": 0, "errors": []}
        res = [it for it in items if (it.get("kind") or "") == "resource"]
        src = [it for it in items if (it.get("kind") or "") != "resource"]
        if res and src:
            return self._apply_mixed(res, src, allow_soft=allow_soft,
                                     keep_backup=keep_backup,
                                     make_undo=make_undo)
        if res:
            return self._apply_resource(res)
        return self._do_apply_source(src, allow_soft=allow_soft,
                                     keep_backup=keep_backup,
                                     make_undo=make_undo)

    def _apply_mixed(self, res_items, src_items, allow_soft=False,
                     keep_backup=True, make_undo=True):
        """两层同时提交：资源层出新归档，源码层回写文件，结果分别上报。

        两层共用一个撤销事务。以前各建各的：一次混合应用留下两条记录，用户
        得连按两次撤销；只撤到其中一条时，另一层已经改过却还在「已应用」的
        状态里。现在只要是同一次点击写下去的，就一定能一次退干净。
        """
        out = {"ok": 0, "fail": 0, "errors": [], "layers": {}, "mode": "mixed"}
        root = (src_items[0].get("root") if src_items else "") or self.root
        tx = None
        if root:
            try:
                import undo_store
                tx = undo_store.ApplyTransaction(root, "应用到模组")
            except Exception:
                tx = None
        try:
            r1 = self._apply_resource(res_items, tx=tx)
        except Exception as ex:
            r1 = {"ok": 0, "fail": len(res_items), "errors": [str(ex)]}
        out["ok"] += int(r1.get("ok") or 0)
        out["fail"] += int(r1.get("fail") or 0)
        out["errors"].extend(r1.get("errors") or [])
        out["layers"]["resource"] = {
            "ok": r1.get("ok", 0), "fail": r1.get("fail", 0),
            "archive": r1.get("archive", ""), "xl": r1.get("xl", ""),
            "canInstall": bool(r1.get("canInstall"))}
        if r1.get("canInstall"):
            out["archive"] = r1.get("archive", "")
            out["xl"] = r1.get("xl", "")
            out["canInstall"] = True

        try:
            r2 = self._do_apply_source(src_items, tx=tx, allow_soft=allow_soft,
                                       keep_backup=keep_backup,
                                       make_undo=make_undo)
        except Exception as ex:
            r2 = {"ok": 0, "fail": len(src_items), "errors": [str(ex)]}
        out["ok"] += int(r2.get("ok") or 0)
        out["fail"] += int(r2.get("fail") or 0)
        out["errors"].extend(r2.get("errors") or [])
        out["layers"]["source"] = {
            "ok": r2.get("ok", 0), "fail": r2.get("fail", 0)}

        # 两层都动完了才提交。只要有一个文件被写过，这条记录就得留下——
        # 哪怕另一层失败了，已经落盘的那部分也必须能撤回来。
        undo_id = ""
        if tx is not None:
            try:
                undo_id = tx.commit()
            except Exception as ex:
                out["errors"].append("撤销记录创建失败: %s" % ex)
                tx.discard()
                undo_id = ""
        if undo_id:
            out["undoId"] = undo_id
            out["undoAvailable"] = True
        out["failed"] = r2.get("failed") or []
        out["failedTotal"] = r2.get("failedTotal", 0)
        out["settled"] = int(r2.get("settled") or 0)
        return out

    def _fill_ctx_from_rows(self, items):
        """前端不带条目名时, 从当前表格按 (file, line, orig) 反查补齐。

        语言包模式的行在 ctx 里存着 `Name = "..."` 的条目名, 写回必须靠它定位。
        前端 apply 只传 key(那是 JSON 的键), lua/reds 行的 key 是空的 —— 缺了
        条目名就只能退回按行号写, 而源文件与目标文件行号不同, 会把 A 的译文
        写进 B (AMM 的 BeginCombo_LookAt 拿到 BeginItem_TabTools 的译文)。
        """
        rows = self.rows or []
        if not items or not rows:
            return 0
        index = {}
        for r in rows:
            try:
                ident = (str(r.get("file") or ""), int(r.get("line") or 0),
                         str(r.get("orig") or ""))
            except (TypeError, ValueError):
                continue
            index.setdefault(ident, r)
        filled = 0
        for it in items:
            if it.get("ctx"):
                continue
            try:
                ident = (str(it.get("file") or ""), int(it.get("line") or 0),
                         str(it.get("orig") or ""))
            except (TypeError, ValueError):
                continue
            hit = index.get(ident)
            if hit is not None and hit.get("ctx"):
                it["ctx"] = hit.get("ctx")
                filled += 1
        return filled

    def _drop_bak_if_same(self, path):
        """写回前留的 `.bak` 与正文逐字节相同就删掉。

        备份是在「知不知道有没有替换」之前就做了的：一条都没写进去时，副本里
        会多出一份和正文完全相同的 `.bak`（实测 5 处），部署时会被一起拷进
        游戏目录。批量里根本不建备份，这里只影响单模组模式。
        """
        bak = path + ".bak"
        try:
            if (os.path.exists(bak) and os.path.exists(path)
                    and os.path.getsize(bak) == os.path.getsize(path)):
                with open(bak, "rb") as f1, open(path, "rb") as f2:
                    if f1.read() == f2.read():
                        os.remove(bak)
        except OSError:
            pass

    def _do_apply_source(self, items, tx=None, allow_soft=False,
                         keep_backup=True, make_undo=True):
        import locale_pack
        import mod_text_engine as E
        import undo_store

        modroot = items[0].get("root") or self.root
        # 语言包行必须带回条目名, 否则写回会退回按行号(会错位)
        self._fill_ctx_from_rows(items)
        ok = fail = settled = 0
        errors = []
        created = []
        prepared_providers = []
        failed = []
        backup = getattr(E, "backup_file", None) if keep_backup else None
        # 混合批次由 _apply_mixed 建一个事务、两层共用；单独走源码模式时自己建。
        own_tx = tx is None and make_undo
        if own_tx:
            try:
                tx = undo_store.ApplyTransaction(modroot, "应用到模组")
            except Exception:
                tx = None
        pair_cache = {}

        def default_target_code():
            return locale_pack.target_code(
                self.settings.get("target_lang", "简体中文"))

        def target_code_for(tgt_rel):
            stem = os.path.splitext(os.path.basename(tgt_rel.replace("\\", "/")))[0]
            try:
                return locale_pack.canonical_locale(stem) or default_target_code()
            except Exception:
                return default_target_code()

        def pair_for(src_rel, tgt_rel):
            """返回提取阶段对应的 Pair；找不到则使用旧的窄匹配逻辑。"""
            code = target_code_for(tgt_rel)
            if code not in pair_cache:
                try:
                    pairs, _issues = locale_pack.discover_provider_pairs(modroot, code)
                except Exception:
                    pairs = []
                pair_cache[code] = {p["source"]: p for p in pairs}
            return pair_cache[code].get(src_rel)

        def snapshot(path):
            if tx is not None:
                try:
                    tx.snapshot(path)
                except Exception:
                    pass

        def add_fail(it, reason, level="hard"):
            failed.append({
                "file": it.get("file", ""),
                "line": it.get("line", 0),
                "orig": it.get("orig", ""),
                "trans": it.get("trans", ""),
                "target": it.get("target", ""),
                "reason": str(reason),
                # soft = 形态可疑, 可以单条覆盖; hard = 写进去必坏, 不给开关
                "level": level,
            })

        json_groups = {}
        text_groups = {}
        # 译文得把原文的包装标记/占位符带回来。模型把 `<Rich ...>` 整段当 HTML
        # 吞掉是常态，把 `{value}` 换成 `%s` 更坏（游戏里原样显示）。位置固定的
        # 直接补回去；补不回来的这一条不写，记软失败，宁可留英文也别写坏。
        #
        # 顺序也管：`string.format` 按位置取参数，中文语序一调、占位符跟着换位，
        # 个数一个不少但运行时 `bad argument #3 to 'format'`（MCM 面板整块操作
        # 报错就是这么来的）。这类不写回，留给用户单条补翻。
        rejected = {}
        for it in items:
            orig = str(it.get("orig") or "")
            trans = str(it.get("trans") or "")
            if not orig or not trans:
                continue
            fixed, lost = E.repair_markup(orig, trans)
            if fixed != trans:
                it["trans"] = fixed
            if lost:
                rejected[id(it)] = ("标记/占位符丢失: " + " ".join(lost[:3]), "soft")
                continue
            lvl, why = E.swapped_spec_order(orig, it["trans"])
            if not lvl:
                # 模型跑飞：同一段连写几十遍 / 占位符凭空多出一大截。语言包那一层
                # （apply_text_target）不走 guard_level，所以这里统一先拦一道。
                lvl, why = E.repeat_run_guard(orig, it["trans"])
            if not lvl:
                lvl, why = E.placeholder_explosion(orig, it["trans"])
            if lvl:
                rejected[id(it)] = (why, lvl)
        if rejected:
            fail += len(rejected)
            for it in items:
                got = rejected.get(id(it))
                if got:
                    add_fail(it, got[0], got[1])
            items = [it for it in items if id(it) not in rejected]
        for it in items:
            if it.get("kind") == "json":
                key = (it["file"], it.get("target") or it["file"])
                json_groups.setdefault(key, {})[it["key"]] = it
            else:
                tgt = it.get("target") or it["file"]
                text_groups.setdefault((it["file"], tgt), []).append(it)

        for (src, tgt), group in json_groups.items():
            dst = os.path.join(modroot, tgt.replace("/", os.sep))
            # 整组译文都是从目标文件里读回来的：写回去是空操作。批量重跑时
            # 这类组占绝大多数（第 11 轮 104 个 todo=0 的模组，17,060 行），
            # 每轮都重写一遍既慢又会让整份 json 的字节顺序被重排一次。
            # 注意 group 是 {key: item}，要取 values()。
            if (all(it.get("inTarget") for it in group.values())
                    and os.path.exists(dst)):
                # 源文件新增、目标里还没有的键（不可译的那些）仍得补
                if not locale_pack.json_needs_fill(modroot, src, tgt):
                    ok += len(group)
                    settled += len(group)
                    continue
            snapshot(dst)
            snapshot(dst + ".bak")
            mapping = {k: it["trans"] for k, it in group.items()}
            try:
                if os.path.exists(dst) and backup:
                    backup(dst)
                written, missing, err = locale_pack.write_json_target_ex(
                    modroot, src, tgt, mapping)
                ok += len(written)
                if err:
                    for it in group.values():
                        add_fail(it, err)
                for key in missing:
                    add_fail(group[key], "目标 JSON 中找不到对应 key")
                fail += len(group) - len(written)
                if err or missing:
                    errors.append("%s (%s)" % (
                        tgt, err or ("%d 个 key 未找到" % len(missing))))
            except Exception as ex:
                fail += len(group)
                errors.append("%s (%s)" % (tgt, ex))
                for it in group.values():
                    add_fail(it, ex)

        for (src, tgt), group in text_groups.items():
            dst_path = os.path.join(modroot, tgt.replace("/", os.sep))
            snapshot(dst_path)
            snapshot(dst_path + ".bak")
            made = False
            if tgt != src and not os.path.exists(dst_path):
                try:
                    locale_pack.ensure_target(modroot, src, tgt)
                    created.append(tgt)
                    made = True
                except Exception as ex:
                    fail += len(group)
                    errors.append("%s (%s)" % (tgt, ex))
                    for it in group:
                        add_fail(it, ex)
                    continue
            if backup and os.path.exists(dst_path):
                try:
                    backup(dst_path)
                except Exception:
                    pass
            if tgt != src and tgt.lower().endswith(".reds"):
                try:
                    pair = pair_for(src, tgt)
                    prep = locale_pack.prepare_codeware_package(
                        modroot, src, tgt,
                        target_locale=target_code_for(tgt),
                        provider_rels=(pair.get("providers") if pair else None),
                        before_write=snapshot,
                        scan_roots=self._deploy_scan_roots())
                    if prep.get("error"):
                        raise RuntimeError(prep["error"])
                    prepared_providers.extend(prep.get("providers") or [])
                except Exception as ex:
                    # 这一步失败时，上面那句 ensure_target 刚复制出来的英文包必须
                    # 收回去。留着它等于在同一个 module 里声明第二个 class English
                    # （IAH 的 ChineseSimplified.reds 与 en-us.reds 就是逐字节相同），
                    # REDscript 报 SYM_REDEFINITION，整个模组都加载不了。
                    if made:
                        for p in (dst_path, dst_path + ".bak"):
                            try:
                                if os.path.exists(p):
                                    os.remove(p)
                            except OSError:
                                pass
                        if tgt in created:
                            created.remove(tgt)
                    fail += len(group)
                    errors.append("%s (Codeware Provider: %s)" % (tgt, ex))
                    for it in group:
                        add_fail(it, ex)
                    continue
            # 整组译文都是从目标文件里读回来的：上面 ensure_target / Provider
            # 该补的已经补完，正文不必再写一遍（同 json 那条）。目标文件不存在
            # 时不算空操作——那种情况正需要写回把条目填进去。
            if (all(it.get("inTarget") for it in group)
                    and os.path.exists(dst_path)):
                self._drop_bak_if_same(dst_path)
                ok += len(group)
                settled += len(group)
                continue
            if tgt != src and all(it.get("ctx") for it in group):
                # 语言包: 目标文件与源文件的行号、条目数都不同 (AMM 的
                # en_US.lua 1115 行 / zh_CN.lua 1113 行), 只能按 `Name =`
                # 的条目名写回。按行号写会把 A 的译文写进 B。
                keyed = [(it.get("ctx") or "", it["orig"], it["trans"])
                         for it in group]
                by_key = {}
                for it in group:
                    by_key.setdefault((it.get("ctx") or "", it["orig"]), it)
                try:
                    n, bad = locale_pack.apply_text_target(dst_path, keyed)
                    ok += n
                    fail += len(bad)
                    for key, orig, reason in bad:
                        hit = by_key.get((key, orig))
                        if hit is not None:
                            add_fail(hit, reason)
                    if bad:
                        errors.append("%s (%d 条未写入)" % (tgt, len(bad)))
                except Exception as ex:  # noqa: BLE001
                    fail += len(group)
                    errors.append("%s (%s)" % (tgt, ex))
                    for it in group:
                        add_fail(it, ex)
            else:
                pairs = [(int(it["line"]), it["orig"], it["trans"]) for it in group]
                try:
                    n, details = E.apply_many_ex(dst_path, pairs,
                                                 allow_soft=allow_soft)
                    ok += n
                    fail += len(details)
                    for idx, _line, _orig, _trans, reason, level in details:
                        add_fail(group[idx], reason, level)
                    if details:
                        errors.append("%s (%d 条未匹配)" % (tgt, len(details)))
                except Exception as ex:  # noqa: BLE001
                    fail += len(group)
                    errors.append("%s (%s)" % (tgt, ex))
                    for it in group:
                        add_fail(it, ex)

            # 备份是在「知不知道有没有替换」之前就做了的：一条都没写进去时，
            # 副本里会多出一份和正文完全相同的 .bak（实测 5 处）。这里比一次
            # 字节，没变就撤掉，别把 20 MB 的垃圾一起部署进游戏目录。
            self._drop_bak_if_same(dst_path)

        undo_id = ""
        if own_tx and tx is not None:
            try:
                undo_id = tx.commit()
            except Exception as ex:
                errors.append("撤销记录创建失败: %s" % ex)

        # 写回成功的行记进翻译记忆，来源按行上的 transSrc 如实记录。
        #
        # 以前这里只要不是官方就一律记 manual，等于把每条机翻结果都升到最高
        # 优先级——之后官方语料即使给出更好的译法也盖不动它了。现在只有
        # 用户真动过手（或老前端没带来源）才记 manual。
        manual = 0
        if ok:
            bad = {(f.get("file"), f.get("line"), f.get("orig")) for f in failed}
            try:
                tgt_lang = self.settings.get("target_lang", "简体中文")
                for it in items:
                    orig = it.get("orig") or ""
                    trans = it.get("trans") or ""
                    if not orig or not trans:
                        continue
                    if (it.get("file"), it.get("line"), orig) in bad:
                        continue
                    # 「已译」行是模组里本来就翻好的文本，原样写回时别把它
                    # 当成一次新的手改记进记忆库——那等于拿中文喂中文。
                    if it.get("already") and trans == orig:
                        continue
                    # 译文是从目标文件读回来的（这次根本没写它）：它本来就在
                    # 记忆库该有的位置，不该被当成「本轮写回成功」重新记一遍。
                    if it.get("inTarget"):
                        continue
                    # The row's origin travels with the item now. Falling back to
                    # _i keeps older front-ends working, but _i only lines up with
                    # self.rows when nothing is merged or de-duplicated.
                    kind = str(it.get("transSrc") or "")
                    oi = it.get("_i")
                    if not kind and oi is not None:
                        try:
                            kind = self.rows[int(oi)].get("transSrc") or ""
                        except Exception:
                            kind = ""
                    # origin 的取舍统一放在 _memory_origin()，资源模式用同一份。
                    origin = self._memory_origin(kind)
                    self._memory_store.put(orig, tgt_lang, trans, origin=origin)
                    manual += 1
                if manual:
                    self._memory_store.save()
            except Exception:
                manual = 0
        # 候选总数：用户看到「候选 31 · 写入 26 · 跳过 5」才知道这 5 条去哪了
        per_file = {}
        for it in items:
            per_file.setdefault(str(it.get("file") or ""), 0)
        for r in (self.rows or []):
            f = str(r.get("file") or "")
            if f in per_file and not r.get("already") and not r.get("skip") \
                    and str(r.get("trans") or "").strip():
                per_file[f] = per_file.get(f, 0) + 1
        candidates = sum(per_file.values()) or len(items)
        return {"ok": ok, "fail": fail, "errors": errors[:20],
                "created": sorted(set(created)),
                "providers": sorted(set(prepared_providers)),
                "undoId": undo_id, "undoAvailable": bool(undo_id),
                "manualSaved": manual, "candidates": candidates,
                "settled": settled,
                "failed": failed[:2000], "failedTotal": len(failed)}

    def undo_last(self, root=""):
        """异步撤销当前模组最近一次应用到模组。"""
        import undo_store
        if self._busy:
            return {"ok": False, "error": "busy"}
        # 资源模式的产物落在归档所在的 mod 目录，源码模式落在 self.root。
        # 单独打开归档时 self.root 是空的，得记住那次应用的根。
        root = root or getattr(self, "l10n_undo_root", "") or self.root or ""
        if not root or not os.path.isdir(root):
            return {"ok": False, "error": self._tr("没有可用于撤销的模组目录")}
        if not undo_store.latest(root):
            return {"ok": False, "error": self._tr("没有可撤销的应用记录")}
        self._busy = True
        self._emit({"type": "busy", "on": True, "text": i18n.tr("正在撤销上次应用 ...")})
        threading.Thread(target=self._undo_work, args=(root,), daemon=True).start()
        return {"ok": True, "started": True}

    def _undo_work(self, root):
        import undo_store
        try:
            r = undo_store.undo_last(root)
            if r.get("ok"):
                self._emit(dict(r, type="undone"))
            else:
                self._emit({"type": "error", "action": "undo",
                            "message": r.get("error", "")})
        except Exception as ex:
            import traceback
            traceback.print_exc()
            self._emit({"type": "error", "action": "undo", "message": str(ex)})
        finally:
            self._busy = False
            self._emit({"type": "busy", "on": False})

    def undo_status(self, root=""):
        """返回当前模组可撤销的写回次数和最近一次信息。"""
        import undo_store
        root = root or getattr(self, "l10n_undo_root", "") or self.root or ""
        items = undo_store.list_transactions(root, 20) if root else []
        return {"ok": True, "count": len(items),
                "last": items[0] if items else None}

    # ------------------------------------------------------------ 还原翻译
    #
    # 「撤销」退一步，「还原」退到底：把这个模组里所有被本工具碰过的文件，
    # 按下载包里的原件恢复，本工具新建的文件删掉。原版文件就躺在 Vortex 的
    # 下载目录里，比本地快照更权威，也不占额外磁盘。

    def revert_plan(self, root=""):
        """还原预览：从哪取、恢复几个、删几个、哪些对不上。"""
        import revert_source
        import undo_store

        root = root or getattr(self, "l10n_undo_root", "") or self.root or ""
        files, first_at, count = undo_store.whitelist(root)
        plan = revert_source.build_plan(
            root, files, (self.settings.get("downloads_dir") or "").strip(),
            snapshots=undo_store.earliest_snapshots(root))
        plan["firstAt"] = first_at
        plan["records"] = count
        # 预览要遍历下载目录、还要对每个包跑一遍 tar 列表，几秒钟。确认之后
        # 真执行时直接复用这份结果，别让用户对着同一个进度条等两遍。
        self._revert_cache = plan
        return plan

    def revert_mod(self, root=""):
        """异步执行还原翻译。"""
        if self._busy:
            return {"ok": False, "error": "busy"}
        plan = self._revert_cache
        root = root or getattr(self, "l10n_undo_root", "") or self.root or ""
        if not plan or os.path.normcase(plan.get("root") or "") != \
                os.path.normcase(os.path.abspath(root or "")):
            plan = self.revert_plan(root)
        self._revert_cache = None
        if not plan.get("ok"):
            return {"ok": False,
                    "error": plan.get("error") or self._tr("没有可还原的内容")}
        self._busy = True
        self._emit({"type": "busy", "on": True,
                    "text": i18n.tr("正在还原模组文件 ...")})
        threading.Thread(target=self._revert_work, args=(plan,),
                         daemon=True).start()
        return {"ok": True, "started": True}

    def _revert_work(self, plan):
        import revert_source
        import undo_store

        root = plan.get("root") or ""
        try:
            # 先拍一张「还原前」的快照：还原点错了还能用「撤销上次应用」退回来。
            # 快照必须在写之前拍，所以放在 run_plan 前头。
            tx = None
            try:
                tx = undo_store.ApplyTransaction(root, "还原翻译前")
                targets = [x["rel"] for x in plan.get("restore") or []]
                targets += list(plan.get("remove") or [])
                for rel in targets:
                    try:
                        tx.snapshot(os.path.join(root, rel.replace("/", os.sep)))
                    except Exception:
                        pass
            except Exception:
                tx = None

            rep = revert_source.run_plan(plan)
            undo_id = ""
            if tx is not None:
                try:
                    undo_id = tx.commit() if (rep["files"] or rep["errors"]) else ""
                    if not undo_id:
                        tx.discard()
                except Exception:
                    tx.discard()
                    undo_id = ""
            # 产物已经没了，可安装的对象也就不存在了
            self.l10n_result = None
            self._emit({"type": "reverted", "ok": bool(rep.get("ok")),
                        "files": rep.get("files", 0),
                        "restored": rep.get("restored") or [],
                        "removed": rep.get("removed") or [],
                        "errors": rep.get("errors") or [],
                        "skipped": plan.get("skip") or [],
                        "source": plan.get("source") or "",
                        "undoId": undo_id,
                        "undoAvailable": bool(undo_id),
                        "root": root})
        except Exception as ex:
            import traceback
            traceback.print_exc()
            self._log_failure("revert", traceback.format_exc())
            self._emit({"type": "error", "action": "revert", "message": str(ex)})
        finally:
            self._busy = False
            self._emit({"type": "busy", "on": False})

    # ------------------------------------------------------------ Excel
    def export_xlsx(self, path, rows):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = self._tr("翻译")
        ws.append([self._tr("文件"), self._tr("行号"), self._tr("类型"),
                   self._tr("上下文"), self._tr("原文"), self._tr("译文"),
                   self._tr("跳过"), self._tr("译文来源")])
        for c in ws[1]:
            c.font = openpyxl.styles.Font(bold=True)
        import baseline as Lbase
        for r in rows:
            # 资源模式的行没有 line，用 entryId 顶上，避免导出直接 KeyError
            origin = r.get("transSrc") or ""
            ws.append([r["file"], r.get("line") or r.get("entryId") or "",
                       self._tr(r.get("type", "")), r.get("ctx", ""),
                       r.get("orig", ""), r.get("trans", ""),
                       self._tr("是") if r.get("skip") else "",
                       self._tr(Lbase.ORIGIN_LABEL.get(origin, "")) if origin else ""])
        for j, w in enumerate([46, 8, 12, 22, 70, 60, 8, 10], 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(j)].width = w
        ws.freeze_panes = "A2"
        wb.save(path)
        return {"ok": True, "path": path}

    def import_xlsx(self, path):
        import openpyxl
        ws = openpyxl.load_workbook(path, read_only=True).active
        header = [str(c.value).strip() if c.value is not None else ""
                  for c in next(ws.iter_rows(max_row=1))]
        gi = lambda name: header.index(name)
        try:
            ci_file, ci_line = gi("文件"), gi("行号")
            ci_orig, ci_trans = gi("原文"), gi("译文")
        except ValueError:
            ci_file, ci_line = gi(self._tr("文件")), gi(self._tr("行号"))
            ci_orig, ci_trans = gi(self._tr("原文")), gi(self._tr("译文"))
        ci_skip = None
        for name in (self._tr("跳过"), "跳过", "Skip", "skip"):
            if name in header:
                ci_skip = header.index(name)
                break
        items = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row[ci_file] is None:
                continue
            raw_skip = row[ci_skip] if ci_skip is not None and ci_skip < len(row) else None
            skip = str(raw_skip or "").strip().lower() in (
                "1", "true", "yes", "y", "是", "跳过", "skip")
            items.append({"file": str(row[ci_file]).replace("\\", "/").strip(),
                          "line": str(row[ci_line]).strip(),
                          "orig": str(row[ci_orig]),
                          "trans": str(row[ci_trans] or "").strip(),
                          "skip": skip})
        return {"ok": True, "items": items}

    # ------------------------------------------------------------ 术语表
    def glossary_get(self):
        return self._glossary_store.terms

    def glossary_save(self, terms):
        out = []
        for t in terms:
            if isinstance(t, dict) and str(t.get("src", "")).strip():
                out.append({"src": str(t["src"]).strip(),
                            "dst": str(t.get("dst", "")).strip(),
                            "note": str(t.get("note", "")).strip()})
        self._glossary_store.terms = out
        self._glossary_store.save()
        return {"ok": True, "terms": self._glossary_store.terms}

    def glossary_reset(self):
        self._glossary_store.reset_defaults()
        return {"ok": True, "terms": self._glossary_store.terms}

    def glossary_export(self):
        path = self.pick_txt_save("glossary.txt")
        if not path:
            return {"ok": False}
        self._glossary_store.export_txt(path)
        return {"ok": True, "path": path}

    def glossary_import(self):
        path = self.pick_txt_open()
        if not path:
            return {"ok": False}
        self._glossary_store.import_txt(path, merge=True)
        self._glossary_store.save()
        return {"ok": True, "terms": self._glossary_store.terms}

    # ------------------------------------------------------------ 记忆
    def memory_count(self):
        return self._memory_store.count()

    def memory_clear(self):
        self._memory_store.clear()
        return {"ok": True}

    # ------------------------------------------------------------ 缓存清理
    def cache_info(self):
        """设置页问「各类缓存占多少地方」。只统计，不动任何文件。"""
        import cache_tools
        try:
            out = cache_tools.scan(config_dir())
        except Exception as ex:
            return {"ok": False, "error": str(ex), "items": [], "total": 0}
        out["ok"] = True
        return out

    def cache_clear(self, keys):
        """清掉勾选的缓存。正在批量/机翻时拒绝——清的是别人正在写的目录。"""
        if self._mb_running:
            return {"ok": False,
                    "error": self._tr("批量正在运行，先停下来再清理缓存")}
        if self._busy:
            return {"ok": False,
                    "error": self._tr("机翻或写回正在运行，先等它结束")}
        import cache_tools
        try:
            return cache_tools.clear(keys, config_dir())
        except Exception as ex:
            return {"ok": False, "error": str(ex), "freed": 0, "files": 0}

    # ------------------------------------------------------------ 其它
    def test_api(self, url):
        import mt_client
        base = normalize_api_base(url)
        if not base:
            return self._tr("请先填写 API 地址")
        try:
            ids = mt_client.list_models(base)
        except Exception as ex:
            return str(ex)
        if not ids:
            return self._tr("连接成功 (HTTP %s)，但未解析到模型列表") % 200
        return self._tr("连接成功 (HTTP %s) · 可用模型 %d 个: %s") % (
            200, len(ids), "、".join(ids[:5]))

    def local_model_status(self):
        """本地模型的开关与现状（不触发任何加载）。"""
        base = normalize_api_base(self.settings.get("api_base", ""))
        out = {"enabled": bool(self.settings.get("local_model_enabled", True)),
               "apiBase": base,
               "model": (self.settings.get("model") or "").strip(),
               "ollama": False, "models": []}
        if not base:
            return out
        try:
            import mt_client
            out["ollama"] = bool(mt_client._is_ollama(base))
            out["models"] = mt_client.list_models(base)
        except Exception as ex:
            out["error"] = str(ex)
        return out

    def local_model_set(self, enabled=True):
        """启用 / 停用本地模型。停用时立刻把模型从显存里卸掉。"""
        new = dict(self.settings)
        new["local_model_enabled"] = bool(enabled)
        try:
            save_settings(new)
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
        self.settings = new
        if enabled:
            return {"ok": True, "enabled": True}
        try:
            import mt_client
            ok, detail = mt_client.unload(
                normalize_api_base(new.get("api_base", "")),
                (new.get("model") or "").strip())
        except Exception as ex:
            ok, detail = False, str(ex)
        return {"ok": True, "enabled": False,
                "unloaded": bool(ok), "detail": str(detail)}

    def report(self):
        meta = self.meta or {}
        return {
            "types": meta.get("types", {}),
            "files": meta.get("files", 0),
            "lua": meta.get("lua_files", 0),
            # 「非 lua 源文件」这一格 .xml 本来就算在里面, tweakdb yaml 同理:
            # 只在 yaml 里写着文案的模组, 报告里不该显示「源文件 0 个」。
            "reds": meta.get("reds_files", 0) + meta.get("yaml_files", 0),
            "localeJson": meta.get("locale_json", 0),
            "localeLua": meta.get("locale_lua", 0),
            "localePairs": meta.get("locale_pairs", 0),
            "localePairsUsed": meta.get("locale_pairs_used", 0),
            "noise": meta.get("noise", {}),
        }


_MUTEX = []
_CRASH_FILES = []     # crash.log 的句柄必须活到进程结束，别让 GC 关掉


def _startup_log(msg):
    """可选启动诊断：设置环境变量 MT_STARTUP_LOG=<文件路径> 后记录各阶段。"""
    path = os.environ.get("MT_STARTUP_LOG")
    if not path:
        return
    try:
        import time
        with open(path, "a", encoding="utf-8") as f:
            f.write("%.3f %s\n" % (time.time(), msg))
    except Exception:
        pass


def _note_log(msg):
    """把不该静默过去的事记进 logs/crash.log。

    crash.log 是常驻的诊断档（见 `_crash_ready`），不随批量轮次轮转；数据
    受损这种事必须留得住证据，否则用户只看到「译文全没了」。
    """
    try:
        d = os.path.join(config_dir(), "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "crash.log"), "a", buffering=1,
                  encoding="utf-8") as f:
            f.write("[%s] %s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def single_instance():
    """同一时间只允许一个实例: 双开会让 WebView2 / pythonnet 抢资源, 表现为
    "Failed to start embedded python interpreter" 或界面卡死。"""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        h = k32.CreateMutexW(None, False, "Cyberpunk2077ModTranslator.Mutex")
        if k32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            return False
        _MUTEX.append(h)
        return True
    except Exception:
        return True


def _crash_ready():
    """把会直接带走进程的 C 层崩溃写成日志。

    2026-11-01 批量跑到第 28 个 mod 时整窗消失，事件日志里只有
    `2077ModTranslator.exe … python314.dll … 0xc0000005`，没有 Python 栈，
    等于没有现场。faulthandler 会在收到访问冲突时把**所有线程**的 Python
    栈打出来，看得见是哪一行。
    """
    try:
        d = os.path.join(config_dir(), "logs")
        os.makedirs(d, exist_ok=True)
        f = open(os.path.join(d, "crash.log"), "a", buffering=1, encoding="utf-8")
        _CRASH_FILES.append(f)
        f.write("\n==== %s  %s  pid=%s ====\n"
                % (time.strftime("%Y-%m-%d %H:%M:%S"), VERSION, os.getpid()))
        faulthandler.enable(f, all_threads=True)
    except Exception:
        pass

    def _hook(kind):
        def _dump(exc_type, exc, tb):
            try:
                import traceback
                with open(os.path.join(config_dir(), "logs", "crash.log"),
                          "a", encoding="utf-8") as f:
                    f.write("---- %s %s ----\n"
                            % (time.strftime("%Y-%m-%d %H:%M:%S"), kind))
                    traceback.print_exception(exc_type, exc, tb, file=f)
            except Exception:
                pass
        return _dump

    try:
        sys.excepthook = _hook("uncaught")
        threading.excepthook = lambda a: _hook("thread")(
            a.exc_type, a.exc_value, a.exc_traceback)
    except Exception:
        pass


def _apply_webview_args(settings):
    """给 WebView2 传启动参数（目前只有「界面不用 GPU」这一种）。

    为什么需要这个开关：2026-11-05 查到这台机器的 NVIDIA 内核驱动
    `nvlddmkm.sys` 被 Windows 判成两次 0xD1 蓝屏的相关驱动（系统日志 1019），
    同一天还有两百多条 GPU 看门狗 live dump（`LiveKernelReports\\WATCHDOG4400`）。
    而界面是 WebView2 + GPU 合成：显卡驱动一卡，宿主进程会跟着一起消失。
    勾上「界面不使用 GPU 渲染」之后界面走纯 CPU 合成，就能把「驱动问题」和
    「程序问题」分开——如果关掉 GPU 后批量能跑完，而别的 GPU 程序仍然打看门狗，
    那问题就在驱动那侧。

    WebView2 只在创建环境时读一次 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`，
    所以必须在 `webview.create_window()` 之前设进环境变量，而且下次启动才生效。
    也支持环境变量 `MT_WEBVIEW_NO_GPU=1` 临时强制打开（不改设置就能试）。
    """
    extra = []
    if settings.get("webview_disable_gpu") or os.environ.get("MT_WEBVIEW_NO_GPU"):
        extra = ["--disable-gpu", "--disable-gpu-compositing"]
    if not extra:
        return ""
    old = (os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS") or "").strip()
    args = (old + " " + " ".join(extra)).strip() if old else " ".join(extra)
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = args
    return args


def main():
    _startup_log("main start")
    _crash_ready()
    if not single_instance():
        _startup_log("single_instance: already running")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, "2077 模组翻译器已经在运行了。\n请切换到已打开的窗口。",
                "2077 Mod Translator", 0x40)
        except Exception:
            pass
        return
    initial = ""
    for a in sys.argv[1:]:
        if os.path.isdir(a) or (os.path.isfile(a) and a.lower().endswith(".zip")):
            initial = a
            break
    _startup_log("single_instance: ok")
    api = Api(initial)
    _startup_log("Api initialized")
    # 必须在 create_window 之前设：WebView2 的环境只创建一次，之后再改环境变量
    # 不会生效（勾选里那句「下次启动生效」就是这个原因）。
    _wv_args = _apply_webview_args(api.settings)
    if _wv_args:
        _note_log("界面禁用 GPU 渲染：WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=%s"
                  % _wv_args)
    dark = bool(api.settings.get("dark_mode"))
    theme = "dark" if dark else "light"
    bg = "#17181C" if dark else "#F7F8FA"
    window = webview.create_window(
        "2077 Mod Translator",
        html=build_html(theme=theme),
        js_api=api,
        width=1440, height=900,
        min_size=(1000, 640),
        frameless=True,
        easy_drag=False,
        resizable=True,
        background_color=bg,
    )
    api._window = window
    _startup_log("window created")

    # 明确指定 Edge WebView2 内核，避免每次启动做多余的后端探测。
    # 仍使用私密模式：退出时 pywebview 可清理临时用户数据，避免缓存锁残留。
    _startup_log("webview.start begin")
    try:
        webview.start(debug=False, gui="edgechromium", private_mode=True)
    except Exception as ex:
        _startup_log("webview.start error: %r" % (ex,))
        raise
    _startup_log("webview.start returned")


if __name__ == "__main__":
    main()
