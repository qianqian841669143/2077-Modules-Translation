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
  resMode: false, resResult: null,
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
    const detail = (e && (e.stack || e.message || String(e))) || String(e);
    try {
      document.getElementById("status").textContent = "BOOT ERROR: " + detail;
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
  // 提取范围现在存进配置了：老配置里没有这一项就用默认值，别把开关清成 false
  state.opts = Object.assign({}, state.opts, (state.settings.extract_opts || {}));
  state.catalog = d.catalog || {};
  state.lang = d.lang;
  state.version = d.version;
  state.configFile = d.configFile;
  _mbPollUrl = d.pollUrl || "";   // 批量事件的只读本机端口
  $("app-ver").textContent = d.version;
  applyTheme();
  applyI18n();
  syncOptChips();       // 把配置里的提取范围套回 chip 的 on/off
  fillSelects(d);
  bind();
  render();
  applyThemeCss();      // 按已存主题刷一次（build_html 已注入，这里只兜底）
  refreshNavStatus();   // 顶栏计数 + 左栏底部的包大小
  updateAppSub();       // 顶栏副标题：当前模组 / 文件数 / 段数
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

/* ---------------- 页面切换（顶部 tab） ----------------
   三个页面共用同一条状态栏和标题栏；[data-page] 标归属，[data-page-btn] 是 tab。
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

/* 顶栏 tab 上的计数 + 翻译页左栏底部的词典摘要。
   任何页面都看得到，所以放在切页里统一刷。 */
async function refreshNavStatus() {
  let s = null;
  try { s = await pywebview.api.pack_status(); } catch (_) { s = null; }
  const lg = (s && s.ledger) || {};
  const nM = $("tab-mods-n"), nP = $("tab-pack-n"), foot = $("side-foot");
  if (nM) nM.textContent = (state.stagingCount || 0) || "";
  if (nP) nP.textContent = (lg.mods || 0) || "";
  if (!foot) return;
  foot.innerHTML =
    '<span class="tag">' + esc(T("包")) + "</span>" +
    (s && s.exists ? fmtBytes(s.size) : "—") + "<br>" +
    '<span class="tag">' + esc(T("词典")) + "</span>" +
    (lg.mods || 0) + " " + esc(T("模组")) + " · " +
    Number(lg.entries || 0).toLocaleString() + " " + esc(T("条"));
}

/* ---------------- 翻译页左栏：进度 + 文件列表 ----------------
   数据全部来自 state.rows，不需要后端再给一份。 */
function renderSide() {
  const rows = state.rows || [];
  const box = $("file-list");
  const pct = $("side-pct"), bar = $("side-bar"), note = $("side-note");
  if (!box) return;
  if (!rows.length) {
    box.innerHTML = "";
    if (pct) pct.textContent = "—";
    if (bar) bar.style.width = "0";
    if (note) note.textContent = T("没加载模组");
    renderDetail();
    return;
  }
  const filled = rows.filter((r) => r.trans).length;
  const skipped = rows.filter((r) => r.skip).length;
  const p = Math.round((filled * 100) / rows.length);
  if (pct) pct.textContent = p + " %";
  if (bar) bar.style.width = p + "%";
  if (note) {
    let s = T("共 %d 段 · 已填 %d · 未填 %d")
      .replace("%d", rows.length).replace("%d", filled)
      .replace("%d", rows.length - filled);
    if (skipped) s += " · " + T("跳过 %d").replace("%d", skipped);
    note.innerHTML = s;
  }
  const map = new Map();
  rows.forEach((r) => {
    const k = r.disp || r.file || "-";
    let g = map.get(k);
    if (!g) { g = { n: 0, f: 0, type: r.type || "" }; map.set(k, g); }
    g.n += 1;
    if (r.trans) g.f += 1;
  });
  const items = [
    '<div class="fitem' + (state.onlyFile ? "" : " on") + '" data-file="">' +
    '<div class="n"><span class="nm">' + esc(T("全部文件")) +
    '</span><span class="cnt">' + rows.length +
    "</span></div><div class=\"d\">" + map.size + " " + esc(T("个文件")) +
    "</div></div>",
  ];
  [...map.entries()].sort((a, b) => b[1].n - a[1].n).forEach(([name, g]) => {
    const on = state.onlyFile === name ? " on" : "";
    const done = g.f === g.n;
    items.push('<div class="fitem' + on + '" data-file="' + esc(name) +
      '" title="' + esc(name) + '">' +
      '<div class="n"><span class="nm">' + esc(name) +
      '</span><span class="cnt">' + g.n + "</span></div>" +
      '<div class="d">' + esc(T(g.type || "")) + " · " +
      '<span class="' + (done ? "g" : "") + '">' + g.f + "/" + g.n + "</span></div></div>");
  });
  box.innerHTML = items.join("");
}

/* ---------------- 翻译页右栏：当前行的出处 ---------------- */
function renderDetail() {
  const box = $("detail-body"), keys = $("detail-keys"), warn = $("detail-warn");
  if (!box) return;
  const r = (state.rows || []).find((x) => x._i === state.sel);
  if (!r) {
    box.textContent = T("点一行看这段文本从哪来。");
    if (keys) keys.innerHTML = "";
    if (warn) warn.innerHTML = "";
    return;
  }
  const kv = (k, v) =>
    '<div class="kv"><span>' + esc(k) + "</span><b>" + esc(String(v)) + "</b></div>";
  const where = r.archive
    ? r.archive + " > " + (r.disp || r.file || "")
    : (r.disp || r.file || "-");
  box.innerHTML =
    kv(T("序号"), "#" + (state.sel + 1)) +
    kv(T("类型"), T(r.type || "-")) +
    kv(T("文件"), where) +
    kv(T("行号"), r.line || r.entryId || "-") +
    (r.count > 1 ? kv(T("同文件重复"), "×" + r.count) : "");
  const tags = [];
  if (r.already) tags.push(T("模组里本来就是这个语言的文本。改译文列即可覆盖。"));
  if (warn) warn.innerHTML = tags.map((t) => esc(t)).join("<br>");
  if (keys) keys.innerHTML = "";
}

/* 顶栏副标题：当前模组 · 文件数 · 段数。没打开模组时只写安装缓存统计。 */
function updateAppSub() {
  const el = $("app-sub");
  if (!el) return;
  const rows = (state.rows || []).length;
  const name = state.meta && (state.meta.name || state.meta.mod);
  const parts = [];
  if (name) {
    parts.push("<b>" + esc(name) + "</b>");
    const files = new Set((state.rows || []).map((r) => r.disp || r.file));
    parts.push(files.size + " " + esc(T("个文件含文本")));
    parts.push(esc(T("已提取 %d 段").replace("%d", rows)));
  } else {
    parts.push(esc(T("没打开模组")));
    if (state.stagingCount) {
      parts.push(esc(T("安装缓存里 %d 个模组").replace("%d", state.stagingCount)));
    }
  }
  el.innerHTML = parts.join(" · ");
}

/* 设置面板：把 .setpane 里「h4 小节标题 + 后面的一串控件」包成一组，
   分类导航只显示当前那一组（原来是滚动定位，用户要的是只显示当前分类）。 */
function buildSetGroups() {
  const pane = $("set-pane");
  if (!pane || pane.dataset.grouped) return;
  const kids = [...pane.children];
  let cur = null;
  kids.forEach((el) => {
    if (el.classList.contains("setsec")) {
      cur = document.createElement("div");
      cur.className = "setgroup";
      cur.dataset.sec = el.id;
      pane.appendChild(cur);
      cur.appendChild(el);
    } else if (cur) {
      cur.appendChild(el);
    }
  });
  pane.dataset.grouped = "1";
}

function showSetGroup(sec) {
  const pane = $("set-pane");
  if (!pane) return;
  pane.querySelectorAll(".setgroup").forEach((g) => {
    g.classList.toggle("on", g.dataset.sec === sec);
  });
  pane.scrollTop = 0;
}

/* 提取范围那几个 chip（现在在设置面板里）：按 state.opts 同步 on/off */
function syncOptChips() {
  document.querySelectorAll("[data-opt]").forEach((el) => {
    el.classList.toggle("on", !!state.opts[el.dataset.opt]);
  });
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

/* ---------------- 已过滤噪音统计（详情栏底部） ----------------
   原来是一整行长句挂在工具条上，开着模组就顶到「应用」按钮旁边。
   现在按详情的 kv 风格一行一类：本地化键 / 记录名 / 标识符 / 位置标识符。 */
function updateNoiseInfo() {
  const el = $("noise-info");
  if (!el) return;
  const sec = $("noise-sec");
  const noise = (state.meta && state.meta.noise) || {};
  const keys = Object.keys(noise).filter((k) => Number(noise[k]) > 0);
  const total = keys.reduce((a, k) => a + (Number(noise[k]) || 0), 0);
  if (!total) {
    el.innerHTML = "";
    el.title = "";
    if (sec) sec.hidden = true;
    return;
  }
  if (sec) sec.hidden = false;
  const head = T("共 %d 条").replace("%d", total.toLocaleString());
  el.title = T("已过滤噪音 %d 条").replace("%d", total);
  el.innerHTML =
    '<div class="kv nsum"><span>' + esc(head) + "</span></div>" +
    keys.map((k) => '<div class="kv"><span>' + esc(T(k)) + "</span><b>" +
      Number(noise[k]).toLocaleString() + "</b></div>").join("");
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
  // 12px 而不是 6px：窗口是 frameless，没有系统边框，拖拽热区全靠这段判断。
  // 6px 在高分屏上基本点不中，鼠标稍微往里一点就变回普通箭头。
  const t = 12;
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
