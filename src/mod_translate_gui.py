# -*- coding: utf-8 -*-
"""
mod_translate_gui.py —— 《赛博朋克 2077》模组翻译器(图形界面版) v1.1
功能: 打开模组目录 → 在表格里逐行填中文 → 导出 Excel 或一键回填源文件(自动备份)
文本来源: REDscript(.reds) / 输入 XML / CET Lua(Mod Settings、Native Settings UI、ImGui)
运行方式:
    python mod_translate_gui.py
或右键文件 → 用 Python 打开。
"""
import json
import os
import re
import sys
import shutil
import threading
import queue
import urllib.error
import urllib.request
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import openpyxl

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from mod_text_engine import (extract_root_ex, apply_translation, backup_file,
                             SETTING_TYPES, UI_TYPES)
import mt_client
import ui_theme as T
import i18n
from i18n import tr
from tm import Memory
from glossary import Glossary

from appconfig import (VERSION, DEFAULT_SETTINGS, TARGET_LANGS,
                        config_dir, config_file, load_settings, save_settings,
                        normalize_api_base, shorten_path)


class App:
    COLUMNS = ("seq", "file", "line", "type", "ctx", "orig", "trans")
    HEADERS = {"seq": "序号", "file": "文件", "line": "行号", "type": "类型",
               "ctx": tr("上下文"), "orig": tr("原文"), "trans": tr("译文 (双击可编辑)")}
    WIDTHS = {"seq": 52, "file": 320, "line": 56, "type": 84,
              "ctx": 150, "orig": 330, "trans": 360}

    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        # ---- 界面语言: 设置里没有就跟随系统
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
        self.memory = Memory()
        self.glossary = Glossary()
        root.title(tr("赛博朋克 2077 模组翻译器 ") + VERSION)
        # 高 DPI 缩放: Tk 的 scaling 以「每磅像素」计, 96 DPI 时约 1.333
        try:
            self.sc = max(1.0, float(root.tk.call("tk", "scaling")) / (96.0 / 72.0))
        except Exception:
            self.sc = 1.0
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        w = min(int(1340 * self.sc), int(sw * 0.92))
        h = min(int(820 * self.sc), int(sh * 0.86))
        x = max(0, int((sw - w) / 2))
        y = max(0, int((sh - h) / 2.4))
        root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        root.minsize(int(760 * self.sc), int(460 * self.sc))
        self._geom = "%dx%d+%d+%d" % (w, h, x, y)
        self._mt_busy = False
        self._mt_stop = threading.Event()
        self.rows = []          # [{file,line,type,ctx,orig,trans}]
        self.cur = -1
        self._inline = None
        self._cancel = False
        self._q = queue.Queue()
        # ---- 界面状态变量(换肤重建时复用)
        self.dark = bool(self.settings.get("dark_mode"))
        self.dark_var = tk.BooleanVar(value=self.dark)
        self.path_var = tk.StringVar()
        self.filter_var = tk.StringVar(value=tr("全部"))
        self.search_var = tk.StringVar()
        self.stats_var = tk.StringVar(value=tr("未加载 — 先选模组目录点「加载 / 提取」"))
        self.status_var = tk.StringVar(
            value=tr("就绪 | 提示: Vortex 用户应用后需在 Vortex 中重新 Deploy"))
        self.trans_var = tk.StringVar()
        self.lua_var = tk.BooleanVar(value=True)
        self.loose_var = tk.BooleanVar(value=True)
        self.locale_var = tk.BooleanVar(value=False)
        self._hover_iid = None
        T.apply_theme(root, self.sc, "dark" if self.dark else "light")
        # 去掉系统标题栏, 改用自绘标题栏(失败则退回系统标题栏)
        root.update_idletasks()          # 必须先映射窗口, 才拿得到 HWND
        self._hwnd = T.strip_caption(root)
        self._build()
        self.root.after(80, self._poll)

    def _poll(self):
        try:
            while True:
                fn = self._q.get_nowait()
                try:
                    fn()
                except Exception as ex:
                    self.status_var.set(tr("内部错误: %s") % ex)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _build(self):
        sc = self.sc
        if self._hwnd:
            T.TitleBar(self.root, tr("赛博朋克 2077 模组翻译器"), VERSION,
                       sc=sc).pack(side="top", fill="x")
        self._build_menubar(self.root)
        pad = int(14 * sc)
        gap = int(8 * sc)

        # ================= 顶部卡片: 目录 + 操作 =================
        top = ttk.Frame(self.root, style="Card.TFrame",
                        padding=(int(14 * sc), int(12 * sc)))
        top.pack(side="top", fill="x", padx=pad, pady=(pad, gap))

        row1 = ttk.Frame(top, style="CardFlat.TFrame")
        row1.pack(fill="x")
        ttk.Label(row1, text=tr("模组目录"), style="Card.TLabel").pack(side="left",
                                                                  padx=(0, int(8 * sc)))
        ttk.Entry(row1, textvariable=self.path_var).pack(side="left", fill="x",
                                                         expand=True)
        ttk.Button(row1, text=tr("浏览"), style="Secondary.TButton",
                   command=self.browse).pack(side="left", padx=(gap, 0))
        ttk.Button(row1, text=tr("加载 / 提取"), style="Primary.TButton",
                   command=self.load).pack(side="left", padx=(gap, 0))
        ttk.Button(row1, text=tr("应用到模组"), style="Primary.TButton",
                   command=self.apply).pack(side="left", padx=(gap, 0))
        ttk.Button(row1, text=tr("读入翻译表"), style="Secondary.TButton",
                   command=self.load_table).pack(side="left", padx=(gap, 0))
        ttk.Button(row1, text=tr("导出 Excel"), style="Secondary.TButton",
                   command=self.export_table).pack(side="left", padx=(gap, 0))

        ttk.Separator(top, orient="horizontal").pack(fill="x",
                                                     pady=(int(10 * sc), int(8 * sc)))

        opt = ttk.Frame(top, style="CardFlat.TFrame")
        opt.pack(fill="x")
        ttk.Label(opt, text=tr("提取范围"), style="Card.TLabel").pack(
            side="left", padx=(0, int(8 * sc)))
        T.ToggleChip(opt, tr(".lua 设置文本"), self.lua_var, sc).pack(
            side="left", padx=(0, int(8 * sc)))
        T.ToggleChip(opt, tr("长句兜底"), self.loose_var, sc).pack(
            side="left", padx=(0, int(8 * sc)))
        T.ToggleChip(opt, tr("语言包文件"), self.locale_var, sc).pack(side="left")
        ttk.Button(opt, text=tr("扫描报告"), style="Secondary.TButton",
                   command=self.show_report).pack(side="right")
        ttk.Button(opt, text=tr("设置"), style="Secondary.TButton",
                   command=self.open_settings).pack(side="right", padx=(0, gap))

        # ================= 筛选条 =================
        bar = ttk.Frame(self.root)
        bar.pack(side="top", fill="x", padx=pad + int(2 * sc), pady=(0, gap))
        ttk.Label(bar, text=tr("筛选"), style="Bar.TLabel").pack(side="left",
                                                             padx=(0, int(6 * sc)))
        om = ttk.Combobox(bar, textvariable=self.filter_var, state="readonly",
                          width=11,
                          values=[tr("全部"), tr("未填译文"), tr("已填译文"), tr("设置项"),
                                  tr("界面文本"), tr("长句文本")])
        om.pack(side="left")
        om.bind("<<ComboboxSelected>>", lambda e: self.refresh_tree())
        ttk.Label(bar, text=tr("搜索"), style="Bar.TLabel").pack(
            side="left", padx=(int(16 * sc), int(6 * sc)))
        se = ttk.Entry(bar, textvariable=self.search_var, width=30)
        se.pack(side="left")
        se.bind("<KeyRelease>", lambda e: self.refresh_tree())
        ttk.Label(bar, textvariable=self.stats_var, style="Bar.TLabel").pack(side="right")

        # ================= 状态栏 (最底) =================
        sb = tk.Label(self.root, textvariable=self.status_var, anchor="w",
                      bg=T.CARD, fg=T.MUTED, font=(T.FONT, 8),
                      padx=int(14 * sc), pady=int(7 * sc),
                      highlightthickness=1, highlightbackground=T.BORDER)
        sb.pack(side="bottom", fill="x")

        # ================= 编辑条 =================
        bottom = ttk.Frame(self.root, style="Card.TFrame",
                           padding=(int(12 * sc), int(8 * sc)))
        bottom.pack(side="bottom", fill="x", padx=pad, pady=(0, gap))
        ttk.Button(bottom, text=tr("↑ 上一行"), style="Secondary.TButton",
                   command=lambda: self.step(-1)).pack(side="left")
        ttk.Button(bottom, text=tr("↓ 下一行"), style="Secondary.TButton",
                   command=lambda: self.step(1)).pack(side="left", padx=(gap, 0))
        self.entry = ttk.Entry(bottom, textvariable=self.trans_var)
        self.entry.pack(side="left", fill="x", expand=True, padx=int(10 * sc))
        self.entry.bind("<Return>", lambda e: self.commit_edit())
        ttk.Button(bottom, text=tr("翻译当前行"), style="Secondary.TButton",
                   command=self.translate_current).pack(side="right", padx=(gap, 0))
        self.btn_all = ttk.Button(bottom, text=tr("翻译全部未填"), style="Primary.TButton",
                                  command=self.translate_all)
        self.btn_all.pack(side="right", padx=(gap, 0))

        # ================= 表格 =================
        mid = ttk.Frame(self.root, style="Card.TFrame")
        mid.pack(fill="both", expand=True, padx=pad, pady=(0, gap))
        self.tree = ttk.Treeview(mid, columns=self.COLUMNS, show="headings",
                                 height=18, selectmode="browse")
        for c in self.COLUMNS:
            self.tree.heading(c, text=tr(self.HEADERS[c]))
            anchor = "center" if c in ("seq", "line", "type") else "w"
            self.tree.column(c, width=int(self.WIDTHS[c] * sc), anchor=anchor,
                             stretch=(c in ("file", "orig", "trans")))
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("odd", background=T.ROW_ALT)
        self.tree.tag_configure("filled", background=T.ROW_FILLED)
        self.tree.tag_configure("t_long", foreground=T.LONG_FG)
        self.tree.tag_configure("t_ui", foreground=T.UI_FG)
        self.tree.tag_configure("hover", background=T.HOVER)
        self.tree.bind("<Double-1>", self.on_dclick)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Motion>", self.on_hover)
        self.tree.bind("<Leave>", self.clear_hover)
        self._tip = None

    # ---------------- 菜单(自绘菜单栏, 跟随主题) ----------------
    def _build_menubar(self, parent):
        bar = tk.Frame(parent, bg=T.CARD, bd=0, highlightthickness=0)
        bar.pack(side="top", fill="x")
        pad = int(10 * self.sc)

        def add(label, builder):
            mb = tk.Menubutton(bar, text=label, bg=T.CARD, fg=T.TEXT,
                               activebackground=T.PRIMARY_SOFT,
                               activeforeground=T.PRIMARY,
                               font=(T.FONT, 9), bd=0, relief="flat",
                               highlightthickness=0, padx=pad, pady=int(5 * self.sc),
                               cursor="hand2")
            m = tk.Menu(mb, tearoff=0, bg=T.CARD, fg=T.TEXT,
                        activebackground=T.PRIMARY_SOFT,
                        activeforeground=T.PRIMARY, bd=0, font=(T.FONT, 9))
            builder(m)
            mb.configure(menu=m)
            mb.pack(side="left")
            mb.bind("<Enter>", lambda e, b=mb: b.configure(bg=T.PRIMARY_SOFT))
            mb.bind("<Leave>", lambda e, b=mb: b.configure(bg=T.CARD))
            return mb

        def file_menu(m):
            m.add_command(label=tr("打开模组目录..."), command=self.browse)
            m.add_command(label=tr("重新提取"), command=self.load)
            m.add_separator()
            m.add_command(label=tr("读入翻译表..."), command=self.load_table)
            m.add_command(label=tr("导出 Excel..."), command=self.export_table)
            m.add_separator()
            m.add_command(label=tr("退出"), command=self.root.destroy)

        def set_menu(m):
            m.add_command(label=tr("本地模型 API 地址..."), command=self.open_settings)
            m.add_separator()
            m.add_checkbutton(label=tr("深色模式"), variable=self.dark_var,
                              command=self.toggle_dark)
            langm = tk.Menu(m, tearoff=0, bg=T.CARD, fg=T.TEXT,
                            activebackground=T.PRIMARY_SOFT,
                            activeforeground=T.PRIMARY, bd=0, font=(T.FONT, 9))
            self._lang_var = tk.StringVar(value=i18n.current())
            for code, name in i18n.available():
                langm.add_radiobutton(label=name, value=code, variable=self._lang_var,
                                      command=lambda c=code: self.set_language(c))
            m.add_cascade(label=tr("界面语言"), menu=langm)
            m.add_separator()
            m.add_command(label=tr("打开配置文件所在目录"),
                          command=lambda: self._open_dir(config_dir()))

        def tool_menu(m):
            m.add_command(label=tr("翻译当前行"), command=self.translate_current)
            m.add_command(label=tr("翻译全部未填"), command=self.translate_all)
            m.add_separator()
            m.add_command(label=tr("术语表") + "...", command=self.open_glossary)
            m.add_command(label=tr("清空翻译记忆") + "...", command=self.clear_memory)
            m.add_separator()
            m.add_command(label=tr("扫描报告"), command=self.show_report)

        def help_menu(m):
            m.add_command(label=tr("使用说明"), command=self.show_help)
            m.add_command(label=tr("关于"), command=self.show_about)

        add(tr("文件"), file_menu)
        add(tr("设置"), set_menu)
        add(tr("工具"), tool_menu)
        add(tr("帮助"), help_menu)
        return bar

    def set_language(self, code):
        if code == i18n.current():
            return
        self.lang = i18n.set_language(code)
        cfg = dict(self.settings)
        cfg["lang"] = self.lang
        try:
            save_settings(cfg)
        except Exception:
            pass
        self.settings = cfg
        self.root.title(tr("赛博朋克 2077 模组翻译器 ") + VERSION)
        self.apply_look()

    def clear_memory(self):
        n = self.memory.count()
        if not n:
            messagebox.showinfo(tr("翻译记忆"), tr("翻译记忆: %d 条") % 0)
            return
        if messagebox.askyesno(tr("清空翻译记忆"),
                               tr("确定清空全部 %d 条记忆?") % n):
            self.memory.clear()
            self.status_var.set(tr("已清空翻译记忆"))

    def toggle_dark(self):
        self.dark = bool(self.dark_var.get())
        cfg = dict(self.settings)
        cfg["dark_mode"] = self.dark
        try:
            save_settings(cfg)
        except Exception:
            pass
        self.settings = cfg
        self.apply_look()

    def apply_look(self):
        """切换浅色/深色: 重建界面(行数据与筛选条件都保留)"""
        self._hide_tip()
        for w in list(self.root.winfo_children()):
            if w.winfo_class() == "Menu":
                continue
            w.destroy()
        T.apply_theme(self.root, self.sc, "dark" if self.dark else "light")
        self.root.title(tr("赛博朋克 2077 模组翻译器 ") + VERSION)
        self._hover_iid = None
        self._build()
        try:
            self.root.geometry(self._geom)
        except Exception:
            pass
        self.refresh_tree()
        self.status_var.set(tr("已切换到%s模式") % (tr("深色") if self.dark else tr("浅色")))

    def _open_dir(self, d):
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
        except Exception as ex:
            messagebox.showerror(tr("打开失败"), str(ex))

    # ---------------- 设置: 本地模型 API 地址 ----------------
    def open_settings(self):
        dlg = tk.Toplevel(self.root)
        dlg.title(tr("设置 - 本地部署模型"))
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.configure(bg=T.CARD)
        dlg.update_idletasks()
        dlg_hwnd = T.strip_caption(dlg)
        if dlg_hwnd:
            T.TitleBar(dlg, tr("设置 - 本地部署模型"), sc=self.sc, dialog=True,
                       on_close=dlg.destroy).pack(side="top", fill="x")
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        pad = int(18 * self.sc)
        body = ttk.Frame(dlg, padding=pad, style="Card.TFrame", borderwidth=0)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=tr("本地部署模型访问 API 地址"),
                  style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(body,
                  text=tr("需为 OpenAI 兼容接口(带 /v1)；留空表示暂不启用机器翻译。"),
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=2,
                                            sticky="w",
                                            pady=(int(4 * self.sc), int(10 * self.sc)))

        url_var = tk.StringVar(value=self.settings.get("api_base", ""))
        model_var = tk.StringVar(value=self.settings.get("model", ""))
        status_var = tk.StringVar(value="")

        ttk.Label(body, text=tr("API 地址"), style="Card.TLabel").grid(
            row=2, column=0, sticky="e", padx=(0, int(10 * self.sc)))
        ent = ttk.Entry(body, textvariable=url_var, width=48)
        ent.grid(row=2, column=1, sticky="we")

        ttk.Label(body, text=tr("模型名称"), style="Card.TLabel").grid(
            row=3, column=0, sticky="e", padx=(0, int(10 * self.sc)),
            pady=(int(8 * self.sc), 0))
        ttk.Entry(body, textvariable=model_var, width=48).grid(
            row=3, column=1, sticky="we", pady=(int(8 * self.sc), 0))
        ttk.Label(body, text=tr("可选，留空则自动用第一个模型"),
                  style="Hint.TLabel").grid(row=4, column=1, sticky="w",
                                            pady=(int(2 * self.sc), 0))
        ttk.Label(body, justify="left", style="Hint.TLabel",
                  text=tr("例:  Ollama  http://127.0.0.1:11434/v1\n")
                       + "      LM Studio  http://127.0.0.1:1234/v1\n"
                         "      vLLM / llama.cpp  http://127.0.0.1:8000/v1").grid(
            row=5, column=0, columnspan=2, sticky="w",
            pady=(int(12 * self.sc), int(6 * self.sc)))

        ttk.Separator(body, orient="horizontal").grid(
            row=6, column=0, columnspan=2, sticky="we",
            pady=(int(6 * self.sc), int(10 * self.sc)))

        # ---- 界面语言
        ui_names = [name for _code, name in i18n.available()]
        ui_codes = [code for code, _name in i18n.available()]
        ui_var = tk.StringVar(value=dict(i18n.available()).get(i18n.current(),
                                                              i18n.current()))
        ttk.Label(body, text=tr("界面语言"), style="Card.TLabel").grid(
            row=7, column=0, sticky="e", padx=(0, int(10 * self.sc)))
        cb_ui = ttk.Combobox(body, textvariable=ui_var, values=ui_names,
                             state="readonly", width=22)
        cb_ui.grid(row=7, column=1, sticky="w")

        # ---- 目标语言
        target_var = tk.StringVar(value=self.settings.get("target_lang", "简体中文"))
        ttk.Label(body, text=tr("目标语言"), style="Card.TLabel").grid(
            row=8, column=0, sticky="e", padx=(0, int(10 * self.sc)),
            pady=(int(8 * self.sc), 0))
        ttk.Combobox(body, textvariable=target_var, values=TARGET_LANGS,
                     state="readonly", width=22).grid(
            row=8, column=1, sticky="w", pady=(int(8 * self.sc), 0))

        # ---- 翻译记忆
        tm_var = tk.BooleanVar(value=bool(self.settings.get("use_tm", True)))
        ttk.Checkbutton(body, text=tr("使用翻译记忆(加载时自动填充)"),
                        variable=tm_var, style="Card.TCheckbutton").grid(
            row=9, column=0, columnspan=2, sticky="w",
            pady=(int(10 * self.sc), 0))

        lbl = ttk.Label(body, textvariable=status_var, style="Hint.TLabel",
                        wraplength=int(430 * self.sc), justify="left")
        lbl.grid(row=10, column=0, columnspan=2, sticky="w",
                 pady=(int(6 * self.sc), int(10 * self.sc)))

        btns = ttk.Frame(body, style="CardFlat.TFrame")
        btns.grid(row=11, column=0, columnspan=2, sticky="we",
                  pady=(int(8 * self.sc), 0))
        ttk.Button(btns, text=tr("测试连接"), style="Secondary.TButton", width=10,
                   command=lambda: self._test_api(url_var.get(), status_var)).pack(side="left")
        ttk.Button(btns, text=tr("保存"), style="Primary.TButton", width=8,
                   command=lambda: self._save_settings(dlg, url_var, model_var,
                                                       status_var, ui_var, ui_codes,
                                                       target_var, tm_var)).pack(side="right")
        ttk.Button(btns, text=tr("取消"), style="Secondary.TButton", width=8,
                   command=dlg.destroy).pack(side="right", padx=(0, int(8 * self.sc)))

        body.columnconfigure(1, weight=1)
        dlg.update_idletasks()
        try:
            x = self.root.winfo_rootx() + (self.root.winfo_width() - dlg.winfo_width()) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - dlg.winfo_height()) // 3
            dlg.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except Exception:
            pass
        ent.focus_set()
        dlg.grab_set()
        return dlg

    def _test_api(self, url, status_var):
        base = normalize_api_base(url)
        if not base:
            status_var.set(tr("请先填写 API 地址"))
            return
        status_var.set(tr("正在连接 %s/models ...") % base)

        def work():
            msg = ""
            try:
                req = urllib.request.Request(base + "/models",
                                             headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=6) as r:
                    code = getattr(r, "status", 0)
                    raw = r.read(300000).decode("utf-8", "replace")
                ids = []
                try:
                    data = json.loads(raw)
                except Exception:
                    data = None
                if isinstance(data, dict):
                    for it in (data.get("data") or []):
                        if isinstance(it, dict) and it.get("id"):
                            ids.append(str(it["id"]))
                    if not ids:
                        for it in (data.get("models") or []):
                            if isinstance(it, dict) and it.get("name"):
                                ids.append(str(it["name"]))
                if ids:
                    msg = tr("连接成功 (HTTP %s) · 可用模型 %d 个: %s") % (
                        code, len(ids), "、".join(ids[:5]))
                else:
                    msg = tr("连接成功 (HTTP %s)，但未解析到模型列表") % code
            except urllib.error.HTTPError as ex:
                msg = tr("连接失败: HTTP %s %s") % (ex.code, ex.reason)
            except urllib.error.URLError as ex:
                msg = tr("连接失败: %s（服务没启动或地址不对）") % ex.reason
            except Exception as ex:
                msg = tr("连接失败: %s") % ex
            self._q.put(lambda: status_var.set(msg))

        threading.Thread(target=work, daemon=True).start()

    def _save_settings(self, dlg, url_var, model_var, status_var, ui_var=None,
                       ui_codes=None, target_var=None, tm_var=None):
        base = normalize_api_base(url_var.get())
        if base and not re.match(r"^https?://[^\s/]+", base, re.I):
            status_var.set(tr("地址格式不对，示例: http://127.0.0.1:11434/v1"))
            return
        cfg = dict(self.settings)
        cfg["api_base"] = base
        cfg["model"] = model_var.get().strip()
        if target_var is not None:
            cfg["target_lang"] = target_var.get()
        if tm_var is not None:
            cfg["use_tm"] = bool(tm_var.get())
        new_lang = None
        if ui_var is not None and ui_codes:
            names = [dict(i18n.available()).get(c, c) for c in ui_codes]
            try:
                new_lang = ui_codes[names.index(ui_var.get())]
            except Exception:
                new_lang = None
            if new_lang:
                cfg["lang"] = new_lang
        try:
            path = save_settings(cfg)
        except Exception as ex:
            messagebox.showerror(tr("保存失败"),
                                 tr("无法写入配置文件:\n%s") % ex, parent=dlg)
            return
        self.settings = cfg
        dlg.destroy()
        if new_lang and new_lang != i18n.current():
            self.set_language(new_lang)
            return
        self.status_var.set(tr("设置已保存 · 本地模型 API: %s（%s）")
                            % (base or tr("未设置"), path))

    # ---------------- 术语表 ----------------
    def open_glossary(self):
        win = tk.Toplevel(self.root)
        win.title(tr("术语表"))
        win.configure(bg=T.BG)
        win.geometry("%dx%d" % (int(760 * self.sc), int(580 * self.sc)))
        win.update_idletasks()
        if T.strip_caption(win):
            T.TitleBar(win, tr("术语表"), self.sc, dialog=True,
                       on_close=win.destroy).pack(side="top", fill="x")
        win.bind("<Escape>", lambda e: win.destroy())

        body = ttk.Frame(win, style="Card.TFrame", padding=int(12 * self.sc))
        body.pack(fill="both", expand=True, padx=int(12 * self.sc),
                  pady=int(12 * self.sc))

        cols = ("src", "dst", "note")
        heads = (tr("原文术语"), tr("译文术语"), tr("备注"))
        tv = ttk.Treeview(body, columns=cols, show="headings", selectmode="browse")
        for c, h, w in zip(cols, heads, (240, 240, 220)):
            tv.heading(c, text=h)
            tv.column(c, width=int(w * self.sc), anchor="w", stretch=(c == "note"))
        vs = ttk.Scrollbar(body, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vs.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        def refresh():
            tv.delete(*tv.get_children())
            for i, t in enumerate(self.glossary.terms):
                tv.insert("", "end", iid=str(i),
                          values=(t["src"], t["dst"], t.get("note", "")))

        def selected_index():
            sel = tv.selection()
            return int(sel[0]) if sel else None

        def edit_dialog(idx=None):
            d = tk.Toplevel(win)
            dlg_title = tr("术语表 - 编辑")
            d.title(dlg_title)
            d.transient(win)
            d.resizable(False, False)
            d.configure(bg=T.CARD)
            d.update_idletasks()
            if T.strip_caption(d):
                T.TitleBar(d, dlg_title, self.sc, dialog=True,
                           on_close=d.destroy).pack(side="top", fill="x")
            f = ttk.Frame(d, padding=int(16 * self.sc), style="Card.TFrame",
                          borderwidth=0)
            f.pack(fill="both", expand=True)
            cur = self.glossary.terms[idx] if idx is not None else \
                {"src": "", "dst": "", "note": ""}
            sv, dv, nv = (tk.StringVar(value=cur["src"]),
                          tk.StringVar(value=cur["dst"]),
                          tk.StringVar(value=cur.get("note", "")))
            for r, (label, var) in enumerate(zip(
                    (tr("原文术语"), tr("译文术语"), tr("备注")), (sv, dv, nv))):
                ttk.Label(f, text=label, style="Card.TLabel").grid(
                    row=r, column=0, sticky="e", padx=(0, int(10 * self.sc)),
                    pady=int(6 * self.sc))
                ttk.Entry(f, textvariable=var, width=40).grid(
                    row=r, column=1, sticky="we", pady=int(6 * self.sc))

            def ok():
                if not sv.get().strip() or not dv.get().strip():
                    return
                if idx is None:
                    self.glossary.add(sv.get(), dv.get(), nv.get())
                else:
                    self.glossary.update(idx, sv.get(), dv.get(), nv.get())
                self.glossary.save()
                refresh()
                d.destroy()

            bar = ttk.Frame(f, style="CardFlat.TFrame")
            bar.grid(row=3, column=0, columnspan=2, sticky="e",
                     pady=(int(12 * self.sc), 0))
            ttk.Button(bar, text=tr("保存"), style="Primary.TButton", width=8,
                       command=ok).pack(side="right")
            ttk.Button(bar, text=tr("取消"), style="Secondary.TButton", width=8,
                       command=d.destroy).pack(side="right", padx=(0, int(8 * self.sc)))
            d.grab_set()
            d.bind("<Return>", lambda e: ok())
            return d

        def add():
            edit_dialog(None)

        def edit():
            i = selected_index()
            if i is None:
                messagebox.showinfo(tr("提示"), tr("请先选中一行"), parent=win)
                return
            edit_dialog(i)

        def remove():
            i = selected_index()
            if i is None:
                messagebox.showinfo(tr("提示"), tr("请先选中一行"), parent=win)
                return
            if messagebox.askyesno(tr("删除"), tr("确认删除这条术语?"), parent=win):
                self.glossary.remove(i)
                self.glossary.save()
                refresh()

        def reset():
            self.glossary.reset_defaults()
            refresh()
            self.status_var.set(tr("术语已恢复默认"))

        def do_import():
            f = filedialog.askopenfilename(
                parent=win, title=tr("导入术语表"),
                filetypes=[(tr("文本文件"), "*.txt"), (tr("全部文件"), "*.*")])
            if not f:
                return
            try:
                self.glossary.import_txt(f, merge=True)
                self.glossary.save()
                refresh()
                self.status_var.set(tr("术语表已保存"))
            except Exception as ex:
                messagebox.showerror(tr("读入失败"), str(ex), parent=win)

        def do_export():
            f = filedialog.asksaveasfilename(
                parent=win, title=tr("导出术语表"),
                initialfile="glossary.txt",
                filetypes=[(tr("文本文件"), "*.txt")])
            if not f:
                return
            try:
                self.glossary.export_txt(f)
            except Exception as ex:
                messagebox.showerror(tr("导出失败"), str(ex), parent=win)

        bar = ttk.Frame(body, style="CardFlat.TFrame")
        bar.grid(row=1, column=0, columnspan=2, sticky="we",
                 pady=(int(10 * self.sc), 0))
        ttk.Button(bar, text=tr("新增"), style="Secondary.TButton",
                   command=add).pack(side="left")
        ttk.Button(bar, text=tr("编辑"), style="Secondary.TButton",
                   command=edit).pack(side="left", padx=(int(6 * self.sc), 0))
        ttk.Button(bar, text=tr("删除"), style="Secondary.TButton",
                   command=remove).pack(side="left", padx=(int(6 * self.sc), 0))
        ttk.Button(bar, text=tr("导入..."), style="Secondary.TButton",
                   command=do_import).pack(side="left", padx=(int(18 * self.sc), 0))
        ttk.Button(bar, text=tr("导出..."), style="Secondary.TButton",
                   command=do_export).pack(side="left", padx=(int(6 * self.sc), 0))
        ttk.Button(bar, text=tr("恢复默认"), style="Secondary.TButton",
                   command=reset).pack(side="right")
        tv.bind("<Double-1>", lambda e: edit())
        refresh()
        return win

    # ---------------- 帮助 ----------------
    def show_help(self):
        here = os.path.dirname(os.path.abspath(__file__))
        cands = [
            os.path.join(os.path.dirname(sys.executable), "使用说明.txt"),
            os.path.join(here, "..", "docs", "使用说明.txt"),
            os.path.join(here, "..", "使用说明.txt"),
        ]
        for c in cands:
            c = os.path.abspath(c)
            if os.path.exists(c):
                try:
                    os.startfile(c)
                    return
                except Exception:
                    break
        messagebox.showinfo(
            tr("使用说明"),
            tr("1. 选择模组目录 → 加载/提取\n")
            + tr("2. 在「译文」列双击填中文\n")
            + tr("3. 点「应用到模组」（自动备份 .bak）\n")
            + tr("4. Vortex 用户应用后需重新 Deploy\n\n")
            + tr("设置菜单可配置本地部署模型的 API 地址。"))

    def show_about(self):
        messagebox.showinfo(
            tr("关于"),
            tr("赛博朋克 2077 模组翻译器 %s\n\n")
            + tr("文本来源: REDscript(.reds) / 输入 XML / CET Lua(.lua)\n")
            + tr("配置文件: %s\n")
            + tr("本地模型 API: %s")
            % (VERSION, config_file(), self.settings.get("api_base") or tr("未设置")))

    # ---------------- 数据加载 ----------------
    def browse(self):
        d = filedialog.askdirectory(
            title=tr("选择模组目录(或直接选 Vortex 下的整个文件夹)"),
            initialdir=self.path_var.get() or r"E:\Vortex Mods\cyberpunk2077")
        if d:
            self.path_var.set(d)
            self.load()

    def load(self):
        modroot = self.path_var.get().strip()
        if not os.path.isdir(modroot):
            messagebox.showerror(tr("错误"), tr("请先选择有效的模组目录"))
            return
        self.status_var.set(tr("正在提取可翻译文本 ..."))
        self.root.update_idletasks()
        inc_lua = bool(self.lua_var.get())
        loose = bool(self.loose_var.get())
        inc_loc = bool(self.locale_var.get())

        def work():
            data, meta, err = {}, {}, None
            try:
                data, meta = extract_root_ex(modroot, include_lua=inc_lua,
                                             loose=loose, include_locale_lua=inc_loc)
            except Exception as ex:
                err = str(ex)
            self._q.put(lambda: self.on_loaded(data, err, meta))
        threading.Thread(target=work, daemon=True).start()

    def on_loaded(self, data, err, meta=None):
        meta = meta or {}
        self.meta = meta
        self.scanned_root = self.path_var.get().strip()
        if err:
            self.status_var.set(tr("错误"))
            messagebox.showerror(tr("提取失败"), err)
            return
        old = {(r["file"], r["line"], r["orig"]): r["trans"] for r in self.rows}
        self.rows = []
        for f in sorted(data):
            for (ln, typ, ctx, orig) in data[f]:
                t = old.get((f, ln, orig), "")
                self.rows.append({"file": f, "line": ln, "type": typ,
                                  "ctx": ctx, "orig": orig, "trans": t})
        short = {r["file"]: shorten_path(r["file"]) for r in self.rows}
        common = os.path.commonprefix(list(short.values()))
        common = common[:common.rfind("/") + 1]
        for r in self.rows:
            d = short[r["file"]]
            if len(d) == len(r["file"]):
                r["disp"] = r["file"]          # 没裁到前缀, 保持完整路径
            else:
                r["disp"] = d[len(common):] if d.startswith(common) else d
        self.cur = -1
        # ---- 翻译记忆: 自动填充空译文
        self._tm_hits = 0
        if self.settings.get("use_tm", True):
            tgt = self.settings.get("target_lang", "简体中文")
            for r in self.rows:
                if not r["trans"].strip():
                    hit = self.memory.get(r["orig"], tgt)
                    if hit:
                        r["trans"] = hit
                        self._tm_hits += 1
            if self._tm_hits:
                self.memory.save()
        self.refresh_tree()
        # 让 Tk 在布局完成后再归位, 避免首行被滚出视野
        self.root.after_idle(self.scroll_top)
        msg = (tr("已加载 %d 段文本 / %d 个文件") % (len(self.rows), len(data)))
        msg += tr("。双击「译文」列填入中文，完成后点「应用到模组」。")
        if len(self.rows) > self.MAX_SHOW:
            msg += tr(" 表格只显示前 %d 条，请用筛选/搜索缩小范围。") % self.MAX_SHOW
        if meta.get("locale_json"):
            msg += tr("  [提示] 该模组有 %d 个语言包 json，文本在 json 里，本工具暂不提取。") % meta["locale_json"]
        if meta.get("locale_lua") and not self.locale_var.get():
            msg += tr("  已跳过 %d 个语言包文件(勾选「语言包文件」可提取)。") % meta["locale_lua"]
        self.status_var.set(msg)

    # ---------------- 表格筛选 ----------------
    def filtered(self):
        f = self.filter_var.get()
        kw = self.search_var.get().strip()
        out = []
        for r in self.rows:
            if f == tr("未填译文") and r["trans"].strip():
                continue
            if f == tr("已填译文") and not r["trans"].strip():
                continue
            if f == tr("设置项") and r["type"] not in SETTING_TYPES:
                continue
            if f == tr("界面文本") and r["type"] != "界面文本":
                continue
            if f == tr("长句文本") and r["type"] != "长句文本":
                continue
            if kw and (kw not in r["orig"] and kw not in r["file"]
                       and kw not in r["ctx"]
                       and kw not in (r.get("disp") or "")):
                continue
            out.append(r)
        return out
    MAX_SHOW = 3000

    def _row_tags(self, r, i):
        tags = ["filled"] if r["trans"].strip() else (["odd"] if i % 2 == 0 else [])
        if r["type"] == "长句文本":
            tags.append("t_long")
        elif r["type"] == "界面文本":
            tags.append("t_ui")
        return tuple(tags)

    def refresh_tree(self):
        self._hover_iid = None
        self.tree.delete(*self.tree.get_children())
        data = self.filtered()
        shown = data[:self.MAX_SHOW]
        if len(data) > self.MAX_SHOW:
            self.status_var.set(tr("结果 %d 条，表格只显示前 %d 条 —— 请用上方「筛选/搜索」缩小范围")
                                % (len(data), self.MAX_SHOW))
        ids = []
        for i, r in enumerate(shown, 1):
            iid = self.tree.insert("", "end", iid=str(i), values=(
                i, r.get("disp") or r["file"], r["line"], tr(r["type"]), r["ctx"],
                r["orig"], r["trans"]))
            self.tree.item(iid, tags=self._row_tags(r, i))
            ids.append((i, r))
        self._view = ids
        if ids:
            self.tree.yview_moveto(0)
        self.update_stats()

    def scroll_top(self):
        try:
            if self.tree.get_children():
                self.tree.yview_moveto(0)
        except Exception:
            pass

    def update_stats(self):
        filled = sum(1 for r in self.rows if r["trans"].strip())
        self.stats_var.set(tr("共 %d 段 | 已填 %d | 未填 %d")
                           % (len(self.rows), filled, len(self.rows) - filled))

    # ---------------- 选择与编辑 ----------------
    def view_index(self, iid):
        for i, r in self._view:
            if i == iid:
                return i
        return None

    def select_row(self, iid):
        r = self.rows_view()[iid - 1] if self._view else None
        idx = self.view_index(iid)
        if idx is None:
            return
        rr = self._view[idx - 1][1]
        self.cur = self.rows.index(rr)
        self.tree.selection_set(str(iid))
        self.tree.see(str(iid))
        self.trans_var.set(rr["trans"])
        self.entry.focus_set()

    def rows_view(self):
        return [r for _, r in self._view]

    def step(self, d):
        n = len(self._view)
        if n == 0:
            return
        sel = self.tree.selection()
        cur = int(sel[0]) if sel else 0
        nxt = max(1, min(n, cur + d))
        self.select_row(nxt)
        self.entry.focus_set()

    def cur_key(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def commit_edit(self):
        sel = self.tree.selection()
        if not sel:
            return
        r = self.rows_view()[int(sel[0]) - 1]
        r["trans"] = self.trans_var.get().strip()
        self.tree.set(sel[0], "trans", r["trans"])
        self.tree.item(sel[0], tags=self._row_tags(r, int(sel[0])))
        self.update_stats()
        self.trans_var.set("")
        self.step(1)
    def on_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            r = self.rows_view()[int(sel[0]) - 1]
        except Exception:
            return
        self.cur = self.rows.index(r)
        self.trans_var.set(r["trans"])

    def on_dclick(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        col = self.tree.identify_column(event.x)
        if col != "#7":
            self.select_row(int(row))
            return
        self.edit_inline(row)

    def edit_inline(self, row):
        r = self.rows_view()[int(row) - 1]
        self.select_row(int(row))
        x1, y1, x2, y2 = self.tree.bbox(row, "#7")
        if x1 == x2 == y1 == y2 == 0:
            return
        if self._inline is not None:
            self._inline.destroy()
        self._inline = None
        self._cancel = False
        box = ttk.Entry(self.tree, font=("Microsoft YaHei UI", 9))
        box.insert(0, r["trans"])
        box.place(x=x1, y=y1, width=max(12, x2 - x1 + 60), height=y2 - y1)
        box.focus_set()
        box.select_range(0, "end")

        def do_commit():
            if self._cancel or not box.winfo_exists():
                return
            r["trans"] = box.get().strip()
            self._inline = None
            box.destroy()
            self.tree.set(str(int(row)), "trans", r["trans"])
            self.tree.item(str(int(row)), tags=self._row_tags(r, int(row)))
            self.trans_var.set(r["trans"])
            self.update_stats()
            self.trans_var.set("")
            self.step(1)

        def do_cancel(_):
            self._cancel = True
            if self._inline is not None and self._inline.winfo_exists():
                self._inline.destroy()
            self._inline = None

        box.bind("<Return>", lambda e: do_commit())
        box.bind("<Escape>", do_cancel)
        box.bind("<FocusOut>", lambda e: do_commit())
        self._inline = box

    # ---------------- 翻译表 Excel ----------------
    def load_table(self):
        f = filedialog.askopenfilename(
            title=tr("选择 Excel 翻译表"),
            filetypes=[(tr("Excel 文件"), "*.xlsx *.xlsm")])
        if not f:
            return
        try:
            ws = openpyxl.load_workbook(f, read_only=True).active
            header = [str(c.value).strip() if c.value is not None else "" for c in next(ws.iter_rows(max_row=1))]
            gi = lambda name: header.index(name)
            ci_file, ci_line, ci_orig, ci_trans = gi(tr("文件")), gi(tr("行号")), gi(tr("原文")), gi(tr("译文"))
            hit = 0
            index = {}
            for r in self.rows:
                index.setdefault((r["file"], str(r["line"]), r["orig"]), []).append(r)
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row[ci_file] is None:
                    continue
                key = (str(row[ci_file]).replace("\\", "/").strip(),
                       str(row[ci_line]).strip(), str(row[ci_orig]))
                for r in index.get(key, []):
                    if not r["trans"].strip():
                        r["trans"] = str(row[ci_trans] or "").strip()
                        hit += 1
                        break
            self.refresh_tree()
            self.status_var.set(tr("已从翻译表填入 %d 条译文。") % hit)
        except Exception as ex:
            messagebox.showerror(tr("读入失败"), str(ex))
    def export_table(self):
        if not self.rows:
            messagebox.showinfo(tr("提示"), tr("表格是空的，先加载模组目录。"))
            return
        if not self.path_var.get():
            self.path_var.set(os.getcwd())
        f = filedialog.asksaveasfilename(
            title=tr("导出 Excel 翻译表"),
            initialfile=os.path.join(self.path_var.get(), tr("翻译表.xlsx")),
            defaultextension=".xlsx",
            filetypes=[(tr("Excel 文件"), "*.xlsx")])
        if not f:
            return
        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = tr("翻译")
            head = [tr("文件"), tr("行号"), tr("类型"), tr("上下文"), tr("原文"), tr("译文")]
            ws.append(head)
            for c in ws[1]:
                c.font = openpyxl.styles.Font(bold=True)
            for i, r in enumerate(self.rows, 1):
                ws.append([r["file"], r["line"], tr(r["type"]), r["ctx"],
                           r["orig"], r["trans"]])
            widths = [34, 8, 10, 20, 60, 60]
            for j, w in enumerate(widths, 1):
                ws.column_dimensions[openpyxl.utils.get_column_letter(j)].width = w
            ws.freeze_panes = "A2"
            wb.save(f)
            self.status_var.set(tr("已导出: %s") % f)
        except Exception as ex:
            messagebox.showerror(tr("导出失败"), str(ex))

    def on_hover(self, event):
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        self._paint_hover(row)
        if not row or col != "#2" or not self._view:
            self._hide_tip()
            return
        try:
            r = self.rows_view()[int(row) - 1]
        except Exception:
            return
        if self._tip and self._tip[0] == (row, r["file"]):
            return
        self._hide_tip()
        tw = tk.Toplevel(self.root)
        tw.wm_overrideredirect(True)
        tw.wm_geometry("+%d+%d" % (event.x_root + 14, event.y_root + 18))
        tk.Label(tw, text=r["file"], background=T.TOOLTIP_BG, foreground=T.TOOLTIP_FG,
                 relief="flat", borderwidth=0, padx=int(8 * self.sc),
                 pady=int(4 * self.sc), font=(T.MONO, 9)).pack()
        self._tip = ((row, r["file"]), tw)

    def _paint_hover(self, iid):
        """给鼠标所在行加浅色高亮(不改变已填/斑马纹的判定)"""
        if iid == self._hover_iid:
            return
        old, self._hover_iid = self._hover_iid, iid
        if old and self.tree.exists(old):
            try:
                r = self.rows_view()[int(old) - 1]
                self.tree.item(old, tags=self._row_tags(r, int(old)))
            except Exception:
                pass
        if iid and self.tree.exists(iid) and iid not in self.tree.selection():
            try:
                r = self.rows_view()[int(iid) - 1]
                self.tree.item(iid, tags=tuple(self._row_tags(r, int(iid))) + ("hover",))
            except Exception:
                pass

    def clear_hover(self, event=None):
        self._hide_tip()
        self._paint_hover("")

    def _hide_tip(self):
        if self._tip:
            try:
                self._tip[1].destroy()
            except Exception:
                pass
            self._tip = None

    # ---------------- 机翻 (本地部署模型) ----------------
    def _mt_api(self):
        base = normalize_api_base(self.settings.get("api_base", ""))
        if not base:
            if messagebox.askyesno(tr("未配置本地模型"),
                                   tr("还没有填写本地模型 API 地址。\n现在打开设置吗?")):
                self.open_settings()
            return None
        return base

    def _mt_apply(self, pairs):
        """主线程: 把译文写进行数据并刷新表格"""
        idx = {id(r): str(i) for i, r in self._view}
        for r, text in pairs:
            r["trans"] = (text or "").strip()
            iid = idx.get(id(r))
            if iid:
                self.tree.set(iid, "trans", r["trans"])
                self.tree.item(iid, tags=self._row_tags(r, int(iid)))
        self.update_stats()

    def translate_current(self):
        if self._mt_busy:
            self.status_var.set(tr("正在批量机翻, 请稍候"))
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(tr("提示"), tr("先在表格里选中一行。"))
            return
        try:
            r = self.rows_view()[int(sel[0]) - 1]
        except Exception:
            return
        tgt = self.settings.get("target_lang", "简体中文")
        hit = self.memory.get(r["orig"], tgt) if self.settings.get("use_tm", True) else None
        if hit:
            self._mt_apply([(r, hit)])
            self.status_var.set(tr("已翻译当前行: %s") % hit[:60])
            return
        base = self._mt_api()
        if not base:
            return
        model = self.settings.get("model", "").strip()
        gloss = self.glossary.prompt_block(self.glossary.relevant([r["orig"]]))
        self.btn_all.state(["disabled"])
        self.status_var.set(tr("正在翻译: %s ...") % r["orig"][:40])

        def work():
            try:
                m = mt_client.resolve_model(base, model)
                out = mt_client.translate_one(base, m, r["orig"], target=tgt,
                                              glossary=gloss)
                self.memory.put(r["orig"], tgt, out)
                self.memory.save()
                self._q.put(lambda: self._mt_one_done(r, out))
            except Exception as ex:
                # Bind ex now: Python 3 clears the except variable at block exit,
                # so a bare closure would raise NameError when the queue runs it.
                self._q.put(lambda e=ex: self._mt_fail(e))

        threading.Thread(target=work, daemon=True).start()

    def _mt_one_done(self, r, text):
        self.btn_all.state(["!disabled"])
        self._mt_apply([(r, text)])
        self.status_var.set(tr("已翻译当前行: %s") % (text or "")[:60])

    def _mt_fail(self, ex):
        self.btn_all.state(["!disabled"])
        self._mt_busy = False
        self.btn_all.configure(text=tr("翻译全部未填"))
        self.status_var.set(tr("机翻失败: %s") % ex)
        messagebox.showerror(tr("机翻失败"), str(ex))

    def _mt_finish(self, done, err, mem_hits=0):
        self._mt_busy = False
        self.btn_all.configure(text=tr("翻译全部未填"))
        self.btn_all.state(["!disabled"])
        if err:
            self.status_var.set(tr("机翻中止: %s（已完成 %d 条）") % (err, done))
            messagebox.showerror(tr("机翻中止"), str(err))
        elif mem_hits:
            self.status_var.set(
                tr("机翻完成: %d 条（其中 %d 条来自翻译记忆）。请逐条检查后再点「应用到模组」。")
                % (done + mem_hits, mem_hits))
        else:
            self.status_var.set(tr("机翻完成: %d 条。请逐条检查后再点「应用到模组」。") % done)

    def translate_all(self):
        if self._mt_busy:                      # 再点一次 = 停止
            self._mt_stop.set()
            self.btn_all.configure(text=tr("正在停止..."))
            return
        targets = [r for r in self.rows if not r["trans"].strip()]
        if not targets:
            messagebox.showinfo(tr("提示"), tr("没有「未填译文」的行。"))
            return
        tgt = self.settings.get("target_lang", "简体中文")

        # ---- 先走翻译记忆
        hits, misses = [], []
        if self.settings.get("use_tm", True):
            for r in targets:
                hit = self.memory.get(r["orig"], tgt)
                if hit:
                    hits.append((r, hit))
                else:
                    misses.append(r)
        else:
            misses = targets
        if hits:
            self._mt_apply(hits)
            self.memory.save()
        if not misses:
            self.memory.save()
            self.status_var.set(tr("机翻完成: %d 条（其中 %d 条来自翻译记忆）。请逐条检查后再点「应用到模组」。")
                                % (len(hits), len(hits)))
            return

        base = self._mt_api()
        if not base:
            return
        model = self.settings.get("model", "").strip()
        total = len(misses)
        self._mt_busy = True
        self._mt_stop.clear()
        self.btn_all.configure(text=tr("停止机翻"))
        self.status_var.set(tr("机翻准备中 ... 共 %d 条") % total)

        def work():
            done = 0
            batch = 8
            try:
                m = mt_client.resolve_model(base, model)
            except Exception as ex:
                self._q.put(lambda e=ex: self._mt_finish(0, e))
                return
            # 预热：先把模型载入并按 keep_alive 保温。首次载入 27B 要几十秒，
            # 以前这段时间算在第一个批次的超时里。非 Ollama 后端会失败，忽略。
            try:
                mt_client.warmup(base, m)
            except Exception:
                pass
            fail_n = 0
            consec = 0
            for i in range(0, total, batch):
                if self._mt_stop.is_set():
                    break
                chunk = misses[i:i + batch]
                texts = [r["orig"] for r in chunk]
                gloss = self.glossary.prompt_block(self.glossary.relevant(texts))
                try:
                    outs = mt_client.translate_batch(
                        base, m, texts, target=tgt, glossary=gloss,
                        on_retry=lambda: self._q.put(
                            lambda: self.status_var.set(
                                tr("模型返回格式不对, 改为逐条翻译 ..."))))
                except Exception as ex:
                    # 单批失败不结束整轮：记一笔，跳过，继续跑后面的。
                    fail_n += 1
                    consec += 1
                    self._q.put(lambda e=ex: self.status_var.set(
                        tr("这一批没翻成，先跳过：%s") % e))
                    if consec >= 5 and done == 0:
                        self._q.put(lambda e=ex, d=done: self._mt_finish(d, e))
                        return
                    continue
                consec = 0
                for r, out in zip(chunk, outs):
                    self.memory.put(r["orig"], tgt, out)
                self.memory.save()
                done += len(chunk)
                self._q.put(lambda ch=chunk, o=outs: self._mt_apply(list(zip(ch, o))))
                self._q.put(lambda d=done: self.status_var.set(
                    tr("机翻中 %d / %d ...") % (d, total)))
            self._q.put(lambda d=done: self._mt_finish(d, None, len(hits)))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 扫描报告 ----------------
    def show_report(self):
        if not getattr(self, "meta", None):
            messagebox.showinfo(tr("提示"), tr("先加载一个模组目录。"))
            return
        meta = self.meta
        by_file = {}
        for r in self.rows:
            by_file.setdefault(r["file"], {})
            by_file[r["file"]][r["type"]] = by_file[r["file"]].get(r["type"], 0) + 1
        lines = [tr("模组目录: ") + getattr(self, "scanned_root", ""),
                 "",
                 tr("源文件:  .reds/.xml %d 个,  .lua %d 个") % (meta.get("reds_files", 0),
                                                          meta.get("lua_files", 0)),
                 tr("命中文件: %d 个,  可译文本: %d 段") % (meta.get("files", 0), len(self.rows)),
                 tr("语言包:  locales/*.json %d 个,  语言包 lua %d 个") % (meta.get("locale_json", 0),
                                                                  meta.get("locale_lua", 0)),
                 "",
                 tr("按类型:")]
        for k, v in sorted(meta.get("types", {}).items(), key=lambda kv: -kv[1]):
            lines.append("    %-12s %d" % (tr(k), v))
        lines.append("")
        lines.append(tr("按文件:"))
        for f in sorted(by_file, key=lambda x: -sum(by_file[x].values())):
            detail = ", ".join("%s %d" % (tr(k), v) for k, v in
                               sorted(by_file[f].items(), key=lambda kv: -kv[1]))
            lines.append("    %-62s %4d   (%s)" % (f, sum(by_file[f].values()), detail))
        win = tk.Toplevel(self.root)
        win.title(tr("扫描报告"))
        win.configure(bg=T.BG)
        win.geometry("%dx%d" % (int(900 * self.sc), int(600 * self.sc)))
        win.update_idletasks()
        win_hwnd = T.strip_caption(win)
        if win_hwnd:
            T.TitleBar(win, tr("扫描报告"), sc=self.sc, dialog=True,
                       on_close=win.destroy).pack(side="top", fill="x")
        txt = tk.Text(win, wrap="none", font=(T.MONO, 9), bg=T.CARD, fg=T.TEXT,
                      relief="flat", borderwidth=0, padx=int(12 * self.sc),
                      pady=int(10 * self.sc), insertbackground=T.TEXT)
        ys = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=ys.set)
        txt.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        txt.insert("1.0", "\n".join(lines))
        txt.configure(state="disabled")

    # ---------------- 应用到模组 ----------------
    def apply(self):
        filled = [r for r in self.rows if r["trans"].strip() and r["trans"] != r["orig"]]
        if not filled:
            messagebox.showinfo(tr("提示"), tr("还没有可应用的译文。"))
            return
        modroot = self.path_var.get().strip()
        if not os.path.isdir(modroot):
            messagebox.showerror(tr("错误"), tr("模组目录无效。"))
            return
        msg = tr("将把 %d 条译文写回模组源文件 %s\n(自动创建 .bak 备份，可随时恢复)。") % (len(filled), modroot)
        if not messagebox.askyesno(tr("确认应用"), msg):
            return
        self.status_var.set(tr("正在写回模组文件 ..."))
        self.root.update_idletasks()

        def work():
            ok, fail, fails = 0, 0, []
            # 先备份所有将被改动的文件(在任何写入之前)
            for f in {r["file"] for r in filled}:
                fp = os.path.join(modroot, f.replace("/", os.sep))
                if os.path.exists(fp):
                    try:
                        backup_file(fp)
                    except Exception:
                        pass
            for r in filled:
                fp = os.path.join(modroot, r["file"].replace("/", os.sep))
                try:
                    if apply_translation(fp, int(r["line"]), r["orig"], r["trans"]):
                        ok += 1
                    else:
                        fail += 1
                        fails.append(r["file"] + ":" + str(r["line"]))
                except Exception as ex:
                    fail += 1
                    fails.append("%s (%s)" % (r["file"], ex))
            self._q.put(lambda: self.apply_done(ok, fail, fails, filled))

        threading.Thread(target=work, daemon=True).start()

    def apply_done(self, ok, fail, fails, filled):
        self.status_var.set(tr("应用完成: 成功 %d 条, 失败 %d 条。") % (ok, fail))
        if fail:
            detail = "\n".join(fails[:20])
            messagebox.showwarning(tr("部分失败"), tr("失败 %d 条:\n%s") % (fail, detail))
        else:
            messagebox.showinfo(tr("完成"), tr("%d 条译文已写回模组。\nVortex 用户请重新 Deploy 后启动游戏。") % ok)

def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = App(root)
    # 支持: 命令行参数 / 把模组文件夹拖到 exe 图标上
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--settings" in sys.argv:
        root.after(300, app.open_settings)
    if args and os.path.isdir(args[0]):
        app.path_var.set(args[0])
        root.after(200, app.load)
    root.mainloop()


if __name__ == "__main__":
    main()

