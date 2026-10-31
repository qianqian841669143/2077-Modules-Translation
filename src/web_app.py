# -*- coding: utf-8 -*-
"""
web_app.py —— pywebview 版前端(HTML/CSS/JS) + Python 后端

界面在 web/ 下, 通过 js_api 调用这里的方法; 长任务用 evaluate_js 回推事件。
"""
import json
import os
import re
import sys
import tempfile
import threading

import webview

import i18n
from appconfig import (VERSION, DEFAULT_SETTINGS, TARGET_LANGS, config_dir,
                       config_file, load_settings, save_settings,
                       normalize_api_base, disp_path)


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
        # 批量队列：与单 mod 会话完全独立，用单独的事件与取消标志，
        # 免得批量跑的时候把表格里的 self.rows 改乱。
        self._queue_cancel = threading.Event()
        self._queue_running = False
        self._queue_results = []
        self._queue_items = []
        # Vortex 三层映射（下载包 / 安装缓存 / 游戏部署）的扫描缓存。
        # 目录一变就作废；只读不写，不会影响 Vortex。
        self._vmap = None

    # ------------------------------------------------------------ 延迟加载
    # 这两个属性也必须保持私有，避免 pywebview 在启动时触发磁盘加载
    # 并继续递归遍历 Memory / Glossary 对象。
    @property
    def _memory_store(self):
        if self._memory is None:
            from tm import Memory
            self._memory = Memory()
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
                  "staging_dir", "downloads_dir"):
            if isinstance(cfg.get(k), str):
                new[k] = cfg[k]
        for k in ("dark_mode", "use_tm", "res_skip_target", "verify_active",
                  "official_weak", "use_official_tm", "local_model_enabled"):
            if isinstance(cfg.get(k), bool):
                new[k] = cfg[k]
        new["api_base"] = normalize_api_base(new.get("api_base", ""))
        # 路径字段去掉首尾空白与包裹的引号（用户常直接从资源管理器复制带引号）
        for k in ("game_dir", "wkit_path", "staging_dir", "downloads_dir"):
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
        try:
            self._window.destroy()
        except Exception:
            pass
        threading.Timer(0.25, lambda: os._exit(0)).start()

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
                                root, [pair["source_entry"]], target)
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
                            first["provider"], first["source"])) if first else "语言包")
                        if len(pairs) > 1:
                            source_desc += " 等 %d 对" % len(pairs)
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
                                               "%s 等 %d 个文件" % (srcs[0]["rel"], len(srcs))),
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
               "localeRows": len(loc_rows), "sourceRows": len(src_rows),
               "resourceRows": len(res_rows)}
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

        返回 (rows, session, info)。info["skipped"] 说明这一层为什么是空的：
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
            target = Larch.resolve_target(
                root, workdir=os.path.join(config_dir(), "work", "zip"))
        except Exception as ex:
            return [], None, {"skipped": "no_archive", "detail": str(ex)}
        if target.archive_path is None:
            return [], None, {"skipped": "no_archive"}

        # 先只读目录：归档里没有 localization 就不必解包（几百 MB 的归档
        # 解一遍要几十秒，只为确认"这里没有可翻的东西"太亏）。
        try:
            if not Lwkit.has_localization(cli, target.archive_path):
                return [], None, {"skipped": "no_localization",
                                  "archive": str(target.archive_path or "")}
        except Exception:
            pass

        work = os.path.join(
            config_dir(), "work",
            re.sub(r"[^A-Za-z0-9_.-]+", "_", target.name)[:60] or "mod")
        sess = Lpipe.Session(target, cli, work, lang="zh-cn", log=None)
        try:
            sess.scan()
        except Exception as ex:
            return [], None, {"skipped": "scan_failed", "detail": str(ex)}
        if not sess.rows:
            return [], sess, {"skipped": "no_rows",
                              "archive": str(target.archive_path or "")}

        rows, skipped = self._filter_l10n_rows(sess, cfg)
        for r in rows:
            r["layer"] = "resource"
            # 资源行的 file 是归档内部路径，磁盘上没有这个文件。要定位到
            # 「这一行所在的文件」，必须知道它在哪个归档里。
            r["archive"] = str(target.archive_path or "")
        info = {
            "rows": len(rows),
            "skipped": skipped,
            "archive": str(target.archive_path or ""),
            "sourceLang": cfg["source_lang"],
            "languages": dict(sess.meta.get("languages") or {}),
            "extend": target.extend_name() if hasattr(target, "extend_name") else "",
            "name": target.name,
        }
        return rows, sess, info

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
            return {"ok": False, "error": "资源模式引擎加载失败: %s" % ex, "problems": []}
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
        sess = Lpipe.Session(target, cli, work, lang="zh-cn", log=None)
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

        keep = []
        for r in sess.rows:
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

        # 产物路径能提前算出来（名字固定是 <mod>_zh-cn），先在写之前拍快照，
        # 撤销时按「本来不存在」把它们删掉；同名旧产物会被还原成覆盖前的内容。
        if tx is not None:
            try:
                from localization import builder as Lbuild
                out_name = Lbuild.default_output_name(
                    getattr(sess.target, "name", ""),
                    getattr(sess, "lang", "zh-cn") or "zh-cn")
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
        try:
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
        # 组包并校验通过才写记忆。以前资源模式一条都不写，同一个 mod 再打开
        # 一次还得重新机翻一遍，官方基线也跟着白跑。
        saved = self._remember_applied(items, applied) if report.ok else 0
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

    # ------------------------------------------------------------ 批量队列
    #
    # 批量跑的是「游戏里已经装好的 mod」，与表格里那一个会话互不影响：
    # 队列自己开 Session、自己填译、自己组包，结果只写输出目录。
    # 版本迁移靠 per-mod 的 entryId 快照（batch.State），mod 更新后
    # 没变的条目直接复用旧译文，只翻新增/改动的。

    def _batch_paths(self):
        base = os.path.join(config_dir(), "batch")
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            pass
        return (os.path.join(base, "index.json"),
                os.path.join(base, "state.json"),
                os.path.join(base, "work"))

    def _batch_state(self):
        from localization import batch as Lbatch
        _idx, state, _work = self._batch_paths()
        return Lbatch, Lbatch.State(state)

    def queue_info(self):
        """面板打开时先取这份：环境、缓存、已有快照数。"""
        try:
            from localization import archive as Larch
            from localization import wkit as Lwkit
        except Exception as ex:
            return {"ok": False, "error": "批量引擎加载失败: %s" % ex}
        cfg = self._l10n_cfg()
        cli = Lwkit.find_cli(cfg["wkit_path"] or None)
        game = Larch.find_game(cfg["game_dir"] or None)
        if not cli or not game:
            return {"ok": False,
                    "error": self._tr("请先在设置里指定游戏目录与 WolvenKit.CLI")}
        Lbatch, state = self._batch_state()
        idx_path, _s, _w = self._batch_paths()
        try:
            index = Lbatch.Index(idx_path)
        except Exception:
            index = None
        mods = len(state.data)
        return {
            "ok": True,
            "game": game,
            "cli": str(cli),
            "outputDir": self.l10n_output_dir(),
            "stateMods": mods,
            "snapshots": sum(len((v or {}).get("entries") or {})
                             for v in state.data.values()),
            "cached": len(index.data) if index is not None else 0,
            "running": self._queue_running,
            "target": cfg["source_lang"],
        }

    def queue_scan(self, refresh=False):
        """异步探测游戏里装了哪些含本地化资源的 mod。"""
        if self._queue_running:
            return {"ok": False, "error": self._tr("队列正在运行")}
        cfg = self._l10n_cfg()
        self._emit({"type": "busy", "on": True,
                    "text": i18n.tr("正在探测已安装的 mod ...")})

        def work():
            try:
                from localization import archive as Larch
                from localization import batch as Lbatch
                from localization import wkit as Lwkit

                cli = Lwkit.find_cli(cfg["wkit_path"] or None)
                game = Larch.find_game(cfg["game_dir"] or None)
                if not cli or not game:
                    raise RuntimeError(self._tr("请先在设置里指定游戏目录与 WolvenKit.CLI"))
                idx_path, _state, work_root = self._batch_paths()
                index = None if refresh else Lbatch.Index(idx_path)
                if refresh:
                    index = Lbatch.Index(idx_path)
                    index.data.clear()

                def prog(done, total, label):
                    self._emit({"type": "busy", "on": True,
                                "text": "%s (%d/%d)" % (label or "", done, total)})

                items = Lbatch.list_candidates(game, cli, work_root, index,
                                               progress=prog,
                                               cancel=self._queue_cancel)
                self._queue_items = items
                usable = [it for it in items if it.get("total")]
                self._emit({"type": "queue-scanned", "items": items,
                            "usable": len(usable), "total": len(items)})
            except Exception as ex:
                import traceback
                traceback.print_exc()
                self._emit({"type": "error", "action": "queue-scan",
                            "message": str(ex)})
            finally:
                self._emit({"type": "busy", "on": False})

        self._queue_cancel.clear()
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "started": True}

    def _batch_translate_factory(self, base, model, tgt):
        """造一个「喂一批原文、还一批译文」的函数，提示词逻辑与单条机翻一致。"""
        import mt_client
        mdl = mt_client.resolve_model(base, model)

        glossary = self._glossary_store
        use_official = bool(self.settings.get("use_official_tm", True))
        hints = self._term_hints if use_official else None
        warmed = {"done": False}

        def translate_texts(texts):
            # 队列路径的第一批同样会白付一次 35-80 秒的载入。这里预热一次并
            # 把 keep_alive 拉长；失败（非 Ollama 后端）只当没做，不重试。
            if not warmed["done"]:
                warmed["done"] = True
                try:
                    mt_client.warmup(base, mdl)
                except Exception:
                    pass
            terms = glossary.relevant(texts)
            gloss = glossary.prompt_block(terms)
            if hints is not None:
                try:
                    skip = {str(t.get("src", "")).strip().lower()
                            for t in (terms or []) if t.get("src")}
                    extra = hints.prompt_block(texts, skip=skip)
                    if extra:
                        gloss = (gloss + "\n\n" + extra).strip()
                except Exception:
                    pass
            return mt_client.translate_batch(base, mdl, texts, target=tgt,
                                             glossary=gloss)

        return translate_texts

    def queue_start(self, keys):
        """异步跑队列。keys 为空时跑全部「有本地化资源」的候选。"""
        if self._queue_running:
            return {"ok": False, "error": self._tr("队列已在运行")}
        cfg = self._l10n_cfg()
        items = [it for it in (self._queue_items or []) if it.get("total")]
        if keys:
            want = {str(k) for k in keys}
            items = [it for it in items if it.get("key") in want]
        if not items:
            return {"ok": False,
                    "error": self._tr("没有可跑的 mod，请先扫描或勾选条目")}

        base = normalize_api_base(self.settings.get("api_base", ""))
        tgt = self.settings.get("target_lang", "简体中文")
        model = self.settings.get("model", "").strip()
        if not base:
            return {"ok": False,
                    "error": self._tr("还没有填写本地模型 API 地址。\n现在打开设置吗?")}
        outdir = self.l10n_output_dir()
        if not outdir:
            return {"ok": False, "error": self._tr("输出目录不可用")}

        self._queue_running = True
        self._queue_cancel.clear()
        self._queue_results = []

        def work():
            try:
                from localization import batch as Lbatch
                from localization import official_tm as Lotm
                from localization import wkit as Lwkit

                cli = Lwkit.find_cli(cfg["wkit_path"] or None)
                _idx, state_path, work_root = self._batch_paths()
                official = None
                if self.settings.get("use_official_tm", True):
                    try:
                        tm = Lotm.OfficialTM()
                        official = tm if tm.exists() else None
                    except Exception:
                        official = None

                ctx = Lbatch.Ctx(
                    cli=cli, workdir=work_root, outdir=outdir,
                    source_lang=cfg["source_lang"],
                    skip_target=cfg["skip_target"],
                    memory=self._memory_store, memory_lang=tgt,
                    official=official,
                    translate_texts=self._batch_translate_factory(base, model, tgt),
                    state=Lbatch.State(state_path),
                    on_event=self._emit,
                    on_progress=lambda key, done, total: self._emit(
                        {"type": "queue-progress", "key": key,
                         "done": done, "total": total}),
                    cancel=self._queue_cancel,
                )
                self._queue_results = Lbatch.run(ctx, items)
            except Exception as ex:
                import traceback
                traceback.print_exc()
                self._emit({"type": "error", "action": "queue",
                            "message": str(ex)})
            finally:
                self._queue_running = False

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "started": True, "total": len(items)}

    def queue_cancel(self):
        self._queue_cancel.set()
        return {"ok": True}

    def queue_results(self):
        return {"ok": True, "running": self._queue_running,
                "results": self._queue_results}

    def queue_forget(self, key):
        """丢掉某个 mod 的迁移快照（下次当作首次翻译）。"""
        try:
            Lbatch, state = self._batch_state()
            state.drop(str(key))
            state.save()
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
        return {"ok": True}

    def queue_open_output(self):
        return self.l10n_open_output()

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
        batch = 8

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

        # 预热：先把模型载入并按 keep_alive 保温。首次载入 27B 要 35-80 秒，
        # 以前这段时间整段算在第一个批次的 180 秒里（实测日志里空闲后的首个
        # 请求就是 1m30s / 45s）。只有 Ollama 原生 /api/chat 认 keep_alive，
        # 换个后端会 404，那不算错误，继续走 OpenAI 兼容路径。
        self._emit({"type": "progress", "done": done, "total": len(targets),
                    "memory": len(hits),
                    "note": self._tr("正在载入本地模型（首次约需 1 分钟）…")})
        try:
            mt_client.warmup(base, mdl)
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
    def apply(self, items):
        """异步: 立刻返回, 结果通过 applied / error 事件回推"""
        if not items:
            return {"ok": False, "error": self._tr("还没有可应用的译文。")}
        if self._busy:
            return {"ok": False, "error": "busy"}
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

    def _do_apply(self, items):
        """按行的来源分流：归档资源行走组包，其余写回明文源文件。

        一个模组可能两层都有（.reds 里有硬编码文案，.archive 里有本地化表），
        必须各走各的路径，不能让一层的产物把另一层顶掉。
        """
        if not items:
            return {"ok": 0, "fail": 0, "errors": []}
        res = [it for it in items if (it.get("kind") or "") == "resource"]
        src = [it for it in items if (it.get("kind") or "") != "resource"]
        if res and src:
            return self._apply_mixed(res, src)
        if res:
            return self._apply_resource(res)
        return self._do_apply_source(src)

    def _apply_mixed(self, res_items, src_items):
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
            r2 = self._do_apply_source(src_items, tx=tx)
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
        return out

    def _do_apply_source(self, items, tx=None):
        import locale_pack
        import mod_text_engine as E
        import undo_store

        modroot = items[0].get("root") or self.root
        ok = fail = 0
        errors = []
        created = []
        prepared_providers = []
        failed = []
        backup = getattr(E, "backup_file", None)
        # 混合批次由 _apply_mixed 建一个事务、两层共用；单独走源码模式时自己建。
        own_tx = tx is None
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

        def add_fail(it, reason):
            failed.append({
                "file": it.get("file", ""),
                "line": it.get("line", 0),
                "orig": it.get("orig", ""),
                "trans": it.get("trans", ""),
                "target": it.get("target", ""),
                "reason": str(reason),
            })

        json_groups = {}
        text_groups = {}
        for it in items:
            if it.get("kind") == "json":
                key = (it["file"], it.get("target") or it["file"])
                json_groups.setdefault(key, {})[it["key"]] = it
            else:
                tgt = it.get("target") or it["file"]
                text_groups.setdefault((it["file"], tgt), []).append(it)

        for (src, tgt), group in json_groups.items():
            dst = os.path.join(modroot, tgt.replace("/", os.sep))
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
            if tgt != src and not os.path.exists(dst_path):
                try:
                    locale_pack.ensure_target(modroot, src, tgt)
                    created.append(tgt)
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
                        before_write=snapshot)
                    if prep.get("error"):
                        raise RuntimeError(prep["error"])
                    prepared_providers.extend(prep.get("providers") or [])
                except Exception as ex:
                    fail += len(group)
                    errors.append("%s (Codeware Provider: %s)" % (tgt, ex))
                    for it in group:
                        add_fail(it, ex)
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
                    n, details = E.apply_many_ex(dst_path, pairs)
                    ok += n
                    fail += len(details)
                    for idx, _line, _orig, _trans, reason in details:
                        add_fail(group[idx], reason)
                    if details:
                        errors.append("%s (%d 条未匹配)" % (tgt, len(details)))
                except Exception as ex:  # noqa: BLE001
                    fail += len(group)
                    errors.append("%s (%s)" % (tgt, ex))
                    for it in group:
                        add_fail(it, ex)

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
        return {"ok": ok, "fail": fail, "errors": errors[:20],
                "created": sorted(set(created)),
                "providers": sorted(set(prepared_providers)),
                "undoId": undo_id, "undoAvailable": bool(undo_id),
                "manualSaved": manual,
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
            "reds": meta.get("reds_files", 0),
            "localeJson": meta.get("locale_json", 0),
            "localeLua": meta.get("locale_lua", 0),
            "localePairs": meta.get("locale_pairs", 0),
            "localePairsUsed": meta.get("locale_pairs_used", 0),
            "noise": meta.get("noise", {}),
        }


_MUTEX = []


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


def main():
    _startup_log("main start")
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
