# -*- coding: utf-8 -*-
"""
ui_theme.py —— DeepSeek 风格主题(浅色 / 深色)

配色取自 DeepSeek 网页端观感: 白/深灰卡片、#4D6BFE 主色、细描边、淡蓝选中。
所有颜色集中在 LIGHT / DARK 两个字典里, 换肤只改这里。
"""
import ctypes
import sys
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont
from tkinter import ttk

try:
    from i18n import tr
except Exception:  # theme can be imported standalone, before locales ship
    def tr(text):
        return text

FONT = "Microsoft YaHei UI"
MONO = "Consolas"

LIGHT = dict(
    BG="#F7F8FA", CARD="#FFFFFF", BORDER="#E4E7EC",
    TEXT="#1F2937", MUTED="#667085",
    PRIMARY="#4D6BFE", PRIMARY_DARK="#3B55E6", PRIMARY_PRESS="#3049CC",
    PRIMARY_SOFT="#EEF2FF",
    HEADER="#F5F6F8", ROW_ALT="#FBFCFD", ROW_FILLED="#F2FBF5",
    SELECT="#E8EEFF", SELECT_FG="#1D4ED8",
    INPUT="#FFFFFF", CHIP="#F2F4F7", HOVER="#F1F4FB",
    TOOLTIP_BG="#1F2937", TOOLTIP_FG="#FFFFFF",
    DISABLED_BG="#EEF0F3", DISABLED_FG="#A0A6B1",
    SCROLL="#D0D5DD", SCROLL_HOVER="#98A2B3",
    LONG_FG="#98A2B3", UI_FG="#475467",
    SECONDARY_FG="#344054", SECONDARY_HOVER="#F2F4F7", SECONDARY_PRESS="#EAECF0",
)

DARK = dict(
    BG="#17181C", CARD="#1F2126", BORDER="#2E3138",
    TEXT="#E6E8EC", MUTED="#9AA0AA",
    PRIMARY="#6E86FF", PRIMARY_DARK="#5A74F0", PRIMARY_PRESS="#4A63E0",
    PRIMARY_SOFT="#2A3150",
    HEADER="#23252B", ROW_ALT="#1C1E23", ROW_FILLED="#1D2A22",
    SELECT="#2C3350", SELECT_FG="#C3CDFF",
    INPUT="#25272D", CHIP="#2A2D34", HOVER="#262A33",
    TOOLTIP_BG="#3A3F4A", TOOLTIP_FG="#FFFFFF",
    DISABLED_BG="#2A2D34", DISABLED_FG="#5B606A",
    SCROLL="#3A3F48", SCROLL_HOVER="#4C525C",
    LONG_FG="#6B7280", UI_FG="#AAB1BD",
    SECONDARY_FG="#D5D9E0", SECONDARY_HOVER="#2A2D34", SECONDARY_PRESS="#31353D",
)

# 模块级当前配色(供 T.CARD 这类直接引用)
globals().update(LIGHT)
_STATE = {"mode": "light", "palette": LIGHT}


def palette():
    return _STATE["palette"]


def mode():
    return _STATE["mode"]


def apply_theme(root, sc=1.0, mode_name="light"):
    pal = DARK if mode_name == "dark" else LIGHT
    _STATE["mode"] = "dark" if mode_name == "dark" else "light"
    _STATE["palette"] = pal
    globals().update(pal)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(bg=pal["BG"])
    try:
        root.option_add("*Font", (FONT, 9))
    except Exception:
        pass
    for k, v in (("*TCombobox*Listbox.background", pal["CARD"]),
                 ("*TCombobox*Listbox.foreground", pal["TEXT"]),
                 ("*TCombobox*Listbox.selectBackground", pal["SELECT"]),
                 ("*TCombobox*Listbox.selectForeground", pal["SELECT_FG"]),
                 ("*TCombobox*Listbox.font", (FONT, 9)),
                 ("*Menu.background", pal["CARD"]),
                 ("*Menu.foreground", pal["TEXT"]),
                 ("*Menu.activeBackground", pal["PRIMARY_SOFT"]),
                 ("*Menu.activeForeground", pal["PRIMARY"]),
                 ("*Menu.selectColor", pal["TEXT"]),
                 ("*Menu.borderWidth", 1),
                 ("*Menu.relief", "solid")):
        try:
            root.option_add(k, v)
        except Exception:
            pass

    style.configure(".", background=pal["BG"], foreground=pal["TEXT"],
                    font=(FONT, 9), borderwidth=0, focuscolor=pal["PRIMARY"])
    style.configure("TFrame", background=pal["BG"])
    style.configure("Card.TFrame", background=pal["CARD"], relief="solid",
                    borderwidth=1, bordercolor=pal["BORDER"])
    style.configure("CardFlat.TFrame", background=pal["CARD"], borderwidth=0,
                    relief="flat")
    style.configure("TLabel", background=pal["BG"], foreground=pal["TEXT"])
    style.configure("Card.TLabel", background=pal["CARD"], foreground=pal["TEXT"])
    style.configure("Hint.TLabel", background=pal["CARD"], foreground=pal["MUTED"],
                    font=(FONT, 8))
    style.configure("Bar.TLabel", background=pal["BG"], foreground=pal["MUTED"],
                    font=(FONT, 8))
    style.configure("Title.TLabel", background=pal["CARD"], foreground=pal["TEXT"],
                    font=(FONT, 10, "bold"))
    style.configure("TSeparator", background=pal["BORDER"])

    def _button(name, bg, fg, border, hover, press, pad_x=14, pad_y=6):
        style.configure(name, background=bg, foreground=fg, bordercolor=border,
                        lightcolor=bg, darkcolor=bg, relief="solid",
                        borderwidth=1, focuscolor=bg,
                        padding=(pad_x, pad_y), font=(FONT, 9))
        style.map(name,
                  background=[("disabled", pal["DISABLED_BG"]), ("pressed", press),
                              ("active", hover)],
                  foreground=[("disabled", pal["DISABLED_FG"]), ("active", fg)],
                  bordercolor=[("disabled", pal["BORDER"]), ("active", border)],
                  lightcolor=[("disabled", pal["DISABLED_BG"]), ("pressed", press),
                              ("active", hover)],
                  darkcolor=[("disabled", pal["DISABLED_BG"]), ("pressed", press),
                             ("active", hover)])

    _button("Primary.TButton", pal["PRIMARY"], "#FFFFFF", pal["PRIMARY"],
            pal["PRIMARY_DARK"], pal["PRIMARY_PRESS"])
    _button("Secondary.TButton", pal["CARD"], pal["SECONDARY_FG"], pal["BORDER"],
            pal["SECONDARY_HOVER"], pal["SECONDARY_PRESS"])
    _button("Ghost.TButton", pal["BG"], pal["MUTED"], pal["BG"],
            pal["SECONDARY_HOVER"], pal["SECONDARY_PRESS"], pad_x=10, pad_y=5)

    style.configure("TEntry", fieldbackground=pal["INPUT"], foreground=pal["TEXT"],
                    bordercolor=pal["BORDER"], lightcolor=pal["BORDER"],
                    darkcolor=pal["BORDER"], insertcolor=pal["TEXT"],
                    padding=(8, 6))
    style.map("TEntry",
              bordercolor=[("focus", pal["PRIMARY"])],
              lightcolor=[("focus", pal["PRIMARY"])],
              darkcolor=[("focus", pal["PRIMARY"])])
    style.configure("TCombobox", fieldbackground=pal["INPUT"],
                    background=pal["INPUT"], foreground=pal["TEXT"],
                    arrowcolor=pal["MUTED"], bordercolor=pal["BORDER"],
                    lightcolor=pal["BORDER"], darkcolor=pal["BORDER"],
                    padding=(8, 5))
    style.map("TCombobox",
              fieldbackground=[("readonly", pal["INPUT"])],
              foreground=[("readonly", pal["TEXT"])],
              bordercolor=[("focus", pal["PRIMARY"])],
              lightcolor=[("focus", pal["PRIMARY"])],
              darkcolor=[("focus", pal["PRIMARY"])])

    style.configure("TCheckbutton", background=pal["BG"], foreground=pal["TEXT"],
                    font=(FONT, 9), padding=(2, 2))
    style.map("TCheckbutton", background=[("active", pal["BG"])])
    style.configure("Card.TCheckbutton", background=pal["CARD"],
                    foreground=pal["TEXT"], font=(FONT, 9), padding=(2, 2))
    style.map("Card.TCheckbutton", background=[("active", pal["CARD"])])

    style.configure("Treeview", background=pal["CARD"], fieldbackground=pal["CARD"],
                    foreground=pal["TEXT"], borderwidth=0, relief="flat",
                    rowheight=int(round(28 * sc)), font=(FONT, 9))
    style.map("Treeview",
              background=[("selected", pal["SELECT"])],
              foreground=[("selected", pal["SELECT_FG"])])
    style.configure("Treeview.Heading", background=pal["HEADER"],
                    foreground=pal["MUTED"], relief="flat", borderwidth=0,
                    font=(FONT, 9, "bold"), padding=(8, 7))
    style.map("Treeview.Heading",
              background=[("active", pal["SECONDARY_HOVER"])],
              foreground=[("active", pal["TEXT"])])

    for orient in ("Vertical", "Horizontal"):
        style.configure(orient + ".TScrollbar", background=pal["SCROLL"],
                        troughcolor=pal["BG"], bordercolor=pal["BG"],
                        arrowcolor=pal["MUTED"], lightcolor=pal["SCROLL"],
                        darkcolor=pal["SCROLL"], gripcount=0, borderwidth=0)
        style.map(orient + ".TScrollbar",
                  background=[("active", pal["SCROLL_HOVER"])])
    return style


class ToggleChip(tk.Checkbutton):
    """药丸形开关: 选中时淡蓝底 + 主色文字 + ✓"""

    def __init__(self, master, text, variable, sc=1.0, command=None, **kw):
        pal = palette()
        self._label = text
        self._var = variable
        super().__init__(
            master, text=text, variable=variable, indicatoron=0,
            bd=0, relief="flat", highlightthickness=0, cursor="hand2",
            padx=int(11 * sc), pady=int(5 * sc), font=(FONT, 9),
            background=kw.pop("background", pal["CHIP"]),
            foreground=pal["TEXT"],
            activebackground=pal["PRIMARY_SOFT"], activeforeground=pal["PRIMARY"],
            selectcolor=pal["PRIMARY_SOFT"], command=command, **kw)
        variable.trace_add("write", lambda *a: self._sync())
        self._sync()

    def _sync(self):
        pal = palette()
        on = bool(self._var.get())
        self.configure(text=("\u2713  " + self._label) if on else self._label,
                       foreground=pal["PRIMARY"] if on else pal["TEXT"])


# ================================================================ 无边框窗口
_WS_CAPTION = 0x00C00000
_GWL_STYLE = -16
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOZORDER, _SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x20
_WM_NCLBUTTONDOWN = 0x00A1
_WM_NCLBUTTONDBLCLK = 0x00A3
_HTCAPTION = 2
_SW_MINIMIZE, _SW_MAXIMIZE, _SW_RESTORE = 6, 3, 9
_IS_WIN = sys.platform.startswith("win")


def _user32():
    u = ctypes.windll.user32
    try:
        u.GetWindowLongPtrW.restype = ctypes.c_longlong
        u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetWindowLongPtrW.restype = ctypes.c_longlong
        u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint]
        u.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t,
                                   ctypes.c_ssize_t]
    except Exception:
        pass
    return u


def hwnd_of(win):
    """取 Tk 窗口对应的 Win32 顶层窗口句柄"""
    if not _IS_WIN:
        return None
    try:
        return _user32().GetParent(win.winfo_id())
    except Exception:
        return None


def strip_caption(win, rounded=True):
    """去掉系统标题栏(保留可缩放边框/最小化/最大化/Snap), 返回 hwnd"""
    hwnd = hwnd_of(win)
    if not hwnd:
        return None
    try:
        u = _user32()
        get = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
        setl = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
        style = get(hwnd, _GWL_STYLE)
        setl(hwnd, _GWL_STYLE, style & ~_WS_CAPTION)
        if rounded:
            try:      # Win11 圆角 (DWMWA_WINDOW_CORNER_PREFERENCE = 33)
                pref = ctypes.c_int(2)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    wintypes.HWND(hwnd), 33, ctypes.byref(pref), ctypes.sizeof(pref))
            except Exception:
                pass
        u.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                       _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOZORDER | _SWP_FRAMECHANGED)
        return hwnd
    except Exception:
        return None


def begin_drag(hwnd):
    """交给系统做拖动(带 Aero Snap), 双击同理"""
    if not hwnd:
        return
    try:
        u = _user32()
        u.ReleaseCapture()
        u.SendMessageW(hwnd, _WM_NCLBUTTONDOWN, _HTCAPTION, 0)
    except Exception:
        pass


def toggle_dblclick(hwnd):
    if not hwnd:
        return
    try:
        u = _user32()
        u.ReleaseCapture()
        u.SendMessageW(hwnd, _WM_NCLBUTTONDBLCLK, _HTCAPTION, 0)
    except Exception:
        pass


def show_window(hwnd, cmd):
    if not hwnd:
        return
    try:
        _user32().ShowWindow(hwnd, cmd)
    except Exception:
        pass


def is_maximized(win):
    try:
        return win.state() == "zoomed"
    except Exception:
        return False


def _glyphs():
    try:
        fams = set(tkfont.families())
    except Exception:
        fams = set()
    if "Segoe MDL2 Assets" in fams:
        return {"Segoe MDL2 Assets": True, "min": "\uE921", "max": "\uE922",
                "restore": "\uE923", "close": "\uE8BB"}
    return {"Segoe MDL2 Assets": False, "min": "\u2014", "max": "\u25A1",
            "restore": "\u2750", "close": "\u2715"}


class TitleBar(tk.Frame):
    """自绘标题栏: 图标 + 标题 + 最小化/最大化/关闭(拖动与双击交给系统)"""

    def __init__(self, master, title, subtitle="", sc=1.0, dialog=False,
                 on_close=None):
        pal = palette()
        self.win = master.winfo_toplevel()
        self.hwnd = hwnd_of(self.win)
        self.dialog = dialog
        self.on_close = on_close
        self._dragged = False
        h = int(38 * sc)
        super().__init__(master, bg=pal["CARD"], height=h,
                         highlightthickness=0, bd=0)
        self.pack_propagate(False)
        g = _glyphs()
        self._g = g
        self._glyph_font = ("Segoe MDL2 Assets", int(9 * sc)) if g["Segoe MDL2 Assets"] \
            else (FONT, int(10 * sc))

        # ---- 左: 图标 + 标题
        self._left = tk.Frame(self, bg=pal["CARD"])
        self._left.pack(side="left", fill="y")
        self._logo = tk.Canvas(self._left, width=int(18 * sc), height=int(18 * sc),
                               bg=pal["CARD"], highlightthickness=0, bd=0)
        self._logo.pack(side="left", padx=(int(12 * sc), int(8 * sc)))
        self._logo.create_rectangle(0, 0, int(18 * sc), int(18 * sc),
                                    fill=pal["PRIMARY"], outline=pal["PRIMARY"])
        self._logo.create_text(int(9 * sc), int(9 * sc), text="M", fill="#FFFFFF",
                               font=(FONT, int(9 * sc), "bold"))
        self._title = tk.Label(self._left, text=title, bg=pal["CARD"], fg=pal["TEXT"],
                               font=(FONT, int(9 * sc), "bold"))
        self._title.pack(side="left")
        self._sub = None
        if subtitle:
            self._sub = tk.Label(self._left, text=subtitle, bg=pal["CARD"],
                                 fg=pal["MUTED"], font=(FONT, int(8 * sc)))
            self._sub.pack(side="left", padx=(int(8 * sc), 0))

        # ---- 右: 窗口按钮
        self._btns = tk.Frame(self, bg=pal["CARD"])
        self._btns.pack(side="right", fill="y")
        if dialog:
            self._button(g["close"], int(44 * sc), h, pal, close=True)
        else:
            self._btn_max = self._button(g["max"], int(44 * sc), h, pal,
                                         cmd=self._toggle_max)
            self._button(g["min"], int(44 * sc), h, pal,
                         cmd=lambda: show_window(self.hwnd, _SW_MINIMIZE))
            self._button(g["close"], int(44 * sc), h, pal, close=True)

        # ---- 交互: 拖动 / 双击最大化 / 右键菜单
        drag_widgets = [self, self._left, self._logo, self._title]
        if self._sub is not None:
            drag_widgets.append(self._sub)
        for w in drag_widgets:
            w.bind("<B1-Motion>", self._on_move)
            w.bind("<Double-Button-1>", self._on_dbl)
            w.bind("<Button-3>", self._on_menu)
            w.bind("<ButtonRelease-1>", self._on_release)
            try:
                w.configure(cursor="arrow")
            except Exception:
                pass

    # ------------------------------------------------------------ 内部
    def _button(self, glyph, w, h, pal, cmd=None, close=False):
        holder = tk.Frame(self._btns, bg=pal["CARD"], width=w, height=h)
        holder.pack(side="left", fill="y")
        holder.pack_propagate(False)
        lbl = tk.Label(holder, text=glyph, bg=pal["CARD"],
                       fg=pal["MUTED"] if close else pal["TEXT"],
                       font=self._glyph_font, bd=0, highlightthickness=0)
        lbl.pack(fill="both", expand=True)
        hover_bg = "#E81123" if close else pal["SECONDARY_HOVER"]
        hover_fg = "#FFFFFF" if close else pal["TEXT"]

        def enter(_=None):
            lbl.configure(bg=hover_bg, fg=hover_fg,
                          text=glyph)

        def leave(_=None):
            lbl.configure(bg=pal["CARD"],
                          fg=pal["MUTED"] if close else pal["TEXT"])

        lbl.bind("<Enter>", enter)
        lbl.bind("<Leave>", leave)
        if close:
            lbl.bind("<Button-1>", lambda e: self._close())
        elif cmd:
            lbl.bind("<Button-1>", lambda e: cmd())
        return lbl

    def _close(self):
        if self.on_close:
            self.on_close()
        else:
            self.win.destroy()

    def _toggle_max(self):
        show_window(self.hwnd,
                    _SW_RESTORE if is_maximized(self.win) else _SW_MAXIMIZE)

    def _on_move(self, event):
        if self._dragged:
            return
        self._dragged = True
        begin_drag(self.hwnd)

    def _on_release(self, event):
        self._dragged = False

    def _on_dbl(self, event):
        if self.dialog:
            return
        toggle_dblclick(self.hwnd)

    def _on_menu(self, event):
        pal = palette()
        m = tk.Menu(self, tearoff=0, bg=pal["CARD"], fg=pal["TEXT"],
                    activebackground=pal["PRIMARY_SOFT"],
                    activeforeground=pal["PRIMARY"], bd=0)
        if not self.dialog:
            m.add_command(label=tr("还原") if is_maximized(self.win) else tr("最大化"),
                          command=self._toggle_max)
            m.add_command(label=tr("最小化"),
                          command=lambda: show_window(self.hwnd, _SW_MINIMIZE))
            m.add_separator()
        m.add_command(label=tr("关闭"), command=self._close)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()
