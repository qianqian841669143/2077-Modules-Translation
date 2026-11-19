/* ============================================================
   core.js —— 基础设施：常量、state、工具函数、启动流程、状态栏
   2026-11-20 从 app.js 原样拆出，只搬运不改逻辑。
   ============================================================ */
/* ============================================================
   2077 Mod Translator — 前端逻辑
   ============================================================ */
const FILTERS = ["全部", "未填译文", "已填译文", "原文=译文", "官方译文", "官方待核", "记忆译文",
                 "设置项", "界面文本", "长句文本", "已跳过", "未跳过"];
const SETTING_TYPES = ["显示名称", "描述", "分类", "模组名", "选项文本"];

const state = {
  rows: [], view: [], settings: {}, catalog: {}, lang: "zh-CN", version: "",
  filter: "全部", search: "", sel: -1, busy: false, uiBusy: false,
  opts: { localeFirst: true, providerPair: true, lua: true, loose: true, locale: false, trace: false },
  modPath: "", root: "", archive: "", meta: {}, glossary: [], dirty: false,
  page: 1, pageSize: 200, raw: [], merged: false,
  hidden: new Set(), onlyFile: null, transMode: "",
  localeFiles: [], localeTargets: [], applyErrors: [],
  resMode: false, resResult: null, revertUndo: false,
  // 批量（单模组功能）：候选来自 staging 目录，逐 mod 复制后翻译
  mbItems: [], mbSel: new Set(), mbDone: {}, mbRunning: false,
  // 把「无需翻译」的目录折起来（todo == 0）。状态不随扫描重置。
  mbHideTodo: true, mbHideTodoInit: false,
  // Vortex 活动映射：下载包 ↔ 安装缓存 ↔ 游戏部署
  vmap: null, vmapFilter: "all", vmapSearch: "",
  // 上一次机翻没翻出来的行号（「继续」只补这些）
  trFailed: [], trBusy: false, trForce: false
};

const $ = (id) => document.getElementById(id);
const T = (s) => (state.catalog && state.catalog[s]) || s;

/* ---------------- 启动 ---------------- */
let _booted = false;

function apiReady() {
  return !!(window.pywebview && window.pywebview.api
            && typeof window.pywebview.api.boot === "function");
}

function waitApi(timeout) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    (function tick() {
      if (apiReady()) return resolve(true);
      if (Date.now() - t0 > (timeout || 60000)) return resolve(false);
      setTimeout(tick, 100);
    })();
  });
}

async function boot() {
  if (_booted) return;
  _booted = true;
  if (!(await waitApi())) {
    _booted = false;
    try {
      document.getElementById("status").textContent = T("正在初始化界面…");
    } catch (_) {}
    return;
  }
  try {
    await _boot();
  } catch (e) {
    _booted = false;
    console.error("boot failed", e);
    try {
      document.getElementById("status").textContent =
        "BOOT ERROR: " + (e && (e.stack || e.message || String(e)));
    } catch (_) {}
  }
}

// 三种时机都试: pywebview 事件 / DOM 就绪 / 定时兜底
window.addEventListener("pywebviewready", boot);
document.addEventListener("DOMContentLoaded", boot);
setInterval(function () { if (!_booted) boot(); }, 800);

async function _boot() {
  const d = await pywebview.api.boot();
  state.settings = d.settings || {};
  state.catalog = d.catalog || {};
  state.lang = d.lang;
  state.version = d.version;
  state.configFile = d.configFile;
  _mbPollUrl = d.pollUrl || "";   // 批量事件的只读本机端口
  $("app-ver").textContent = d.version;
  applyTheme();
  applyI18n();
  fillSelects(d);
  bind();
  render();
  applyThemeCss();      // 按已存主题刷一次（build_html 已注入，这里只兜底）
  refreshNavStatus();   // 侧边栏底部的包大小 / 模组数 / 条数
  setStatus(T("就绪 | 提示: Vortex 用户应用后需在 Vortex 中重新 Deploy"));
  if (d.initialPath) {
    $("path").value = d.initialPath;
    loadMod();
  }
  try { await pywebview.api.ui_ready(); } catch (_) {}
}

function applyTheme() {
  document.documentElement.dataset.theme = state.settings.dark_mode ? "dark" : "light";
}

/* 配色主题：只覆盖 CSS 变量，不动 data-theme（浅/深仍由 dark_mode 决定）。
   CSS 由后端 theme_css() 给出；空名 = 内置配色，直接把 style 标签摘掉。 */
async function applyThemeCss() {
  let r = null;
  try { r = await pywebview.api.theme_css(state.settings.theme || ""); } catch (_) { r = null; }
  const css = (r && r.css) || "";
  let el = document.getElementById("theme-extra");
  if (!css) {
    if (el) el.remove();
    return;
  }
  if (!el) {
    el = document.createElement("style");
    el.id = "theme-extra";
    document.head.appendChild(el);
  }
  el.textContent = css;
}

/* ---------------- 页面切换（左侧导航） ----------------
   三个页面共用同一条状态栏和标题栏；[data-page] 标归属，[data-page-btn] 是导航项。
   用 hidden 属性切换而不是 display：各元素原本的 display（flex/block）保留，
   切回来时不用重新算布局。 */
const PAGES = ["translate", "manage", "pack"];
let _page = "translate";

function switchPage(name) {
  if (PAGES.indexOf(name) < 0) name = "translate";
  _page = name;
  document.querySelectorAll("[data-page]").forEach((el) => {
    el.hidden = el.getAttribute("data-page") !== name;
  });
  document.querySelectorAll("[data-page-btn]").forEach((el) => {
    el.classList.toggle("on", el.getAttribute("data-page-btn") === name);
  });
  if (name === "manage") refreshMods();
  if (name === "pack") refreshPackStatus();
  refreshNavStatus();
}

/* 侧边栏底部：包多大 / 几个模组 / 多少条，任何页面都看得到 */
async function refreshNavStatus() {
  const el = $("nav-status");
  if (!el) return;
  let s = null;
  try { s = await pywebview.api.pack_status(); } catch (_) { s = null; }
  if (!s) { el.textContent = ""; return; }
  const lg = s.ledger || {};
  el.innerHTML =
    "<div>" + esc(T("包")) + " " + (s.exists ? fmtBytes(s.size) : "—") + "</div>" +
    "<div>" + (lg.mods || 0) + " " + esc(T("模组")) + "</div>" +
    "<div>" + Number(lg.entries || 0).toLocaleString() + " " + esc(T("条")) + "</div>";
}

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = T(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => {
    el.placeholder = T(el.dataset.i18nPh || el.getAttribute("data-i18n-ph"));
  });
  document.title = T("赛博朋克 2077 模组翻译器");
  // applyI18n 会用 data-i18n 覆盖按钮文案，模式相关的文案要重新套一遍
  if (typeof applyModeUi === "function") applyModeUi();
}

function fillSelects(d) {
  const f = $("filter");
  f.innerHTML = FILTERS.map((k) => `<option value="${k}">${T(k)}</option>`).join("");
  f.value = state.filter;

  const lang = $("set-lang");
  lang.innerHTML = (d.langs || []).map(
    (l) => `<option value="${l.code}">${l.name}</option>`).join("");
  lang.value = state.lang;

  const tgt = $("set-target");
  tgt.innerHTML = (d.targetLangs || []).map(
    (l) => `<option value="${l}">${esc(T(l))}</option>`).join("");
  tgt.value = state.settings.target_lang || "简体中文";
}

/* ---------------- 事件绑定 ---------------- */
/* ---------------- 「更多」下拉菜单 ---------------- */
function bindMoreMenu() {
  const btn = $("btn-more"), menu = $("menu-more");
  if (!btn || !menu) return;
  const close = () => menu.classList.remove("on");
  btn.onclick = (e) => {
    e.stopPropagation();
    menu.classList.toggle("on");
  };
  menu.onclick = (e) => {
    if (e.target.closest(".btn")) setTimeout(close, 0);
  };
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".menu-wrap")) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
  });
}

/* ---------------- 已过滤噪音统计 ---------------- */
function updateNoiseInfo() {
  const el = $("noise-info");
  if (!el) return;
  const noise = (state.meta && state.meta.noise) || {};
  const total = Object.keys(noise).reduce((a, k) => a + (Number(noise[k]) || 0), 0);
  if (!total) { el.textContent = ""; el.title = ""; return; }
  const parts = Object.keys(noise)
    .filter((k) => Number(noise[k]) > 0)
    .map((k) => T(k) + " " + noise[k]);
  el.textContent = T("已过滤噪音 %d 条").replace("%d", total) + ": " + parts.join(" · ");
  el.title = el.textContent;
}

function setStatus(s) { $("status").textContent = s; }

function setBusy(on, text) {
  state.uiBusy = !!on;
  const ov = $("overlay");
  if (ov) {
    ov.classList.toggle("on", !!on);
    if (text) $("overlay-text").textContent = text;
  }
  document.querySelectorAll(".btn, .chip").forEach((b) => { b.disabled = !!on; });
}
function toast(msg, kind) {
  const el = document.createElement("div");
  el.className = "toast " + (kind || "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 2600);
  setTimeout(() => el.remove(), 3000);
}

/* ---------------- 窗口边缘缩放 ---------------- */
let resizing = null;
function edgeAt(e) {
  const t = 6;
  const w = window.innerWidth, h = window.innerHeight;
  const right = e.clientX > w - t, bottom = e.clientY > h - t;
  if (right && bottom) return "rb";
  if (right) return "r";
  if (bottom) return "b";
  return null;
}
document.addEventListener("mousemove", (e) => {
  if (resizing) return;
  const edge = edgeAt(e);
  document.body.style.cursor = edge === "r" ? "ew-resize" : edge === "b" ? "ns-resize" : edge === "rb" ? "nwse-resize" : "";
});
document.addEventListener("mousedown", (e) => {
  const edge = edgeAt(e);
  if (!edge) return;
  e.preventDefault();
  resizing = { edge, x: e.screenX, y: e.screenY, w: window.innerWidth, h: window.innerHeight };
});
let raf = null;
document.addEventListener("mousemove", (e) => {
  if (!resizing) return;
  const dx = e.screenX - resizing.x, dy = e.screenY - resizing.y;
  const w = resizing.w + (resizing.edge.includes("r") ? dx : 0);
  const h = resizing.h + (resizing.edge.includes("b") ? dy : 0);
  if (raf) return;
  raf = requestAnimationFrame(() => {
    raf = null;
    pywebview.api.win_resize(w, h);
  });
});
document.addEventListener("mouseup", () => { resizing = null; });
