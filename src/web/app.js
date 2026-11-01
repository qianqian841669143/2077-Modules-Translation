/* ============================================================
   2077 Mod Translator — 前端逻辑
   ============================================================ */
const FILTERS = ["全部", "未填译文", "已填译文", "原文=译文", "官方译文", "官方待核", "记忆译文",
                 "设置项", "界面文本", "长句文本", "已跳过", "未跳过"];
const SETTING_TYPES = ["显示名称", "描述", "分类", "模组名", "选项文本"];

const state = {
  rows: [], view: [], settings: {}, catalog: {}, lang: "zh-CN", version: "",
  filter: "全部", search: "", sel: -1, busy: false,
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
    (l) => `<option value="${l}">${l}</option>`).join("");
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

function bind() {
  $("win-min").onclick = () => pywebview.api.win_min();
  $("win-max").onclick = () => pywebview.api.win_max();
  $("win-close").onclick = () => pywebview.api.win_close();
  document.querySelector(".titlebar").ondblclick = (e) => {
    if (e.target.closest(".winbtn")) return;
    pywebview.api.win_max();
  };

  $("btn-browse").onclick = async () => {
    const p = await pywebview.api.pick_folder();
    // 只在输入框里放路径；state.modPath 等加载成功后由 onLoaded 写（P4）。
    // 以前这里就把它改了，选完文件夹又放弃加载时，state.modPath 已经指向
    // 一个跟表格毫无关系的目录。
    if (p) { $("path").value = p; loadMod(); }
  };
  $("btn-load").onclick = () => loadMod();
  $("btn-apply").onclick = () => applyAll();
  $("btn-undo").onclick = () => undoLastApply();
  $("btn-export").onclick = () => exportXlsx();
  $("btn-import").onclick = () => importXlsx();
  $("btn-report").onclick = () => showReport();
  $("btn-clear-trans").onclick = () => clearCurrentTranslations();
  $("btn-clear-rows").onclick = () => clearCurrentRows();
  $("btn-clear-scope").onclick = () => clearScope();
  $("btn-revert").onclick = () => revertMod();
  bindMoreMenu();
  document.addEventListener("contextmenu", (e) => {
    const row = e.target.closest(".trow");
    if (!row) return;
    e.preventDefault();
    const r = state.rows[Number(row.dataset.i)];
    if (r) showCtxMenu(e.clientX, e.clientY, r, cellAtPoint(row, e.clientX, e.clientY));
  });
  // 菜单是固定定位的浮层，位置只在打开的瞬间算一次。按 Esc、滚表格、滚窗口
  // 都该让它消失——否则它会悬在跟它无关的位置，点下去还作用到原来那一行。
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeCtxMenu();
  });
  $("tbody").addEventListener("scroll", closeCtxMenu, { passive: true });
  window.addEventListener("scroll", closeCtxMenu, { passive: true });
  document.addEventListener("click", (e) => {
    const item = e.target.closest("#ctxmenu .item");
    if (!item) { closeCtxMenu(); return; }
    const r = _ctxTarget;
    const cell = _ctxCell;
    const act = item.dataset.act;
    closeCtxMenu();
    if (!r) return;
    if (item.classList.contains("disabled")) return;
    if (act === "copy-cell") {
      const label = cell ? cell.label : "";
      copyToClipboard(cell && cell.value,
        label ? T("已复制%s").replace("%s", label) : T("已复制"));
      return;
    }
    if (act === "open") {
      if (r.archive) pywebview.api.open_archive_dir(r.archive);
      else pywebview.api.open_dir(state.root || state.modPath, r.file);
      return;
    }
    if (act === "merge") { toggleMerge(); return; }
    // 第一类：目标永远是左键选中的那一行，不是右击的那一行。
    const selRow = state.sel >= 0 ? state.rows[state.sel] : null;
    if (act === "locate") { locateSelected(); return; }
    if (act === "toggle-skip") {
      if (selRow) setRowsSkip([selRow], !selRow.skip);
      return;
    }
    if (act === "clear-row-trans") {
      if (selRow) clearRowTranslation(selRow);
      return;
    }
    if (act === "retrans") {
      if (selRow) retranslateRow(selRow);
      return;
    }
    if (act === "retrans-filtered") { retranslateFiltered(); return; }
    // 第二类：作用在右击行所属的文件上。
    if (act === "skip-file") {
      const allSkipped = !state.rows.some((x) => x.file === r.file && !x.skip);
      setFileSkip(r.file, !allSkipped);
    }
    else if (act === "hide") hideFile(r.file);
    else if (act === "show-hidden") clearHidden();
    else if (act === "only") {
      (state.onlyFile === r.file) ? cancelOnlyFile() : onlyFile(r.file);
    }
    else if (act === "hide-trans") toggleTranslated("hide");
    else if (act === "only-trans") toggleTranslated("only");
  });
  $("btn-open-dir").onclick = () => {
    if (state.sel >= 0 && state.rows[state.sel]) {
      pywebview.api.open_dir(state.root || state.modPath, state.rows[state.sel].file);
    } else if (state.root || state.modPath) {
      pywebview.api.open_root_dir(state.root || state.modPath);
    } else {
      toast(T("请先选择有效的模组目录"), "err");
    }
  };
  $("pg-first").onclick = () => gotoPage(1);
  $("pg-prev").onclick = () => gotoPage(state.page - 1);
  $("pg-next").onclick = () => gotoPage(state.page + 1);
  $("pg-last").onclick = () => gotoPage(999999);
  $("pg-size").onchange = (e) => {
    state.pageSize = parseInt(e.target.value, 10) || 200;
    state.page = 1;
    render();
  };
  $("btn-zip").onclick = async () => {
    const p = await pywebview.api.pick_zip();
    if (p) { $("path").value = p; loadMod(); }
  };
  // ---- 资源模式（.archive 内的 CR2W 本地化）----
  $("btn-l10n").onclick = async () => {
    const p = await pywebview.api.l10n_pick();
    if (!p) return;
    setBusy(true, T("正在解包并解析归档 ..."));
    const r = await pywebview.api.load_resource(p);
    if (!r.ok) {
      setBusy(false);
      if (r.error !== "busy") toast(r.error || "error", "err");
    }
  };
  $("btn-l10n-check").onclick = async () => {
    const el = $("l10n-check-result");
    el.textContent = T("检测中 ...");
    const r = await pywebview.api.l10n_env();
    if (r.ok) {
      el.textContent = T("正常 · CLI %s · 游戏已定位").replace("%s", r.cliVersion || "?");
    } else {
      el.textContent = (r.problems || []).concat(r.error ? [r.error] : []).join(" · ");
    }
  };
  $("btn-l10n-install").onclick = async () => {
    const r = await pywebview.api.l10n_install();
    if (!r.ok) { toast(r.error || "error", "err"); return; }
    toast(T("已安装 %d 个文件到游戏").replace("%d", (r.files || []).length), "ok");
    setStatus(T("已安装到游戏 · 启动游戏后生效"));
  };
  $("btn-l10n-out").onclick = async () => {
    const r = await pywebview.api.l10n_open_output();
    if (!r.ok) toast(r.error || "error", "err");
  };
  $("btn-build-official").onclick = async () => {
    const r = await pywebview.api.build_official_tm();
    if (!r.ok) {
      if (r.error !== "busy") toast(r.error || "error", "err");
      return;
    }
    $("official-info").textContent = T("正在读取官方语言包，约 1 分钟 ...");
  };
  $("btn-locale").onclick = () => openLocale();  $("btn-loc-load").onclick = () => loadLocale();
  $("btn-pack").onclick = async () => {
    const r = await pywebview.api.pack_zip();
    if (r.ok) toast(T("已打包: %s").replace("%s", r.path), "ok");
  };
  $("loc-src").onchange = () => renderLocaleList();
  $("btn-settings").onclick = () => openSettings();
  $("btn-queue").onclick = () => openQueue();
  $("btn-q-pick").onclick = () => pickMbDir();
  $("btn-q-scan").onclick = () => scanQueue();
  $("btn-q-start").onclick = () => startQueue();
  $("btn-q-cancel").onclick = async () => {
    $("btn-q-cancel").style.display = "none";
    await pywebview.api.batch_cancel();
  };
  $("btn-q-out-pick").onclick = () => pickMbOutDir();
  $("btn-q-out-open").onclick = async () => {
    const r = await pywebview.api.batch_open_output();
    if (!r.ok) toast(r.error || "error", "err");
  };
  $("q-out").oninput = () => syncQueueStart();
  $("btn-q-clear").onclick = () => clearQueueOut();
  $("btn-copy-apply-errors").onclick = () => {
    const text = state.applyErrors.map((x) => [
      x.file || "", x.line || "", x.orig || "", x.trans || "",
      T(x.reason || "")
    ].join("\t")).join("\n");
    copyToClipboard(text, T("失败列表已复制"));
  };

  document.querySelectorAll(".chip").forEach((c) => {
    if (c.closest("#vmap-filters")) return;   // 映射面板的 chip 有自己的处理
    c.onclick = () => {
      c.classList.toggle("on");
      state.opts[c.dataset.opt] = c.classList.contains("on");
    };
  });

  $("filter").onchange = (e) => {
    state.filter = e.target.value; state.page = 1; render();
  };
  $("search").oninput = (e) => {
    state.search = e.target.value; state.page = 1; render();
  };

  $("btn-prev").onclick = () => step(-1);
  $("btn-next").onclick = () => step(1);
  $("trans").onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); commitEditor(true); }
  };
  $("trans").oninput = () => {
    if (state.sel >= 0) { state.dirty = true; _transDirty = true; }
    followEditingSoon();            // 打字时让表格跟上当前行
  };
  // 焦点回到编辑条（Tab、点进来、程序回填后）也要把那一行留在视野里
  $("trans").addEventListener("focus", followEditingRow);
  // 失焦（点按钮、切窗口、点别的行）也结算一次，别把刚敲的字丢掉。
  $("trans").onblur = () => { flushEditor(); };
  $("btn-tr-row").onclick = () => {
    if (state.sel < 0 || !state.rows[state.sel]) {
      toast(T("请先选中一行"), "err");
      return;
    }
    const row = state.rows[state.sel];
    if (row.skip) { toast(T("此行已跳过翻译"), "err"); return; }
    translateRows(originalIndicesForRow(row));
  };
  // 机翻期间这个按钮被遮罩挡住，停止入口在遮罩里（btn-tr-stop）。
  $("btn-tr-all").onclick = () => translateRows(null);
  // 遮罩上的这颗按钮是两个流程共用的出口：机翻走 stop_translate，批量走
  // batch_cancel（由 enterBatchMask 切 data-mode）。
  $("btn-tr-stop").onclick = async () => {
    const b = $("btn-tr-stop");
    const mode = b && b.dataset.mode;
    if (b) b.disabled = true;
    $("ov-count").textContent = T("正在停止...");
    setStatus(T("正在停止..."));
    if (mode === "batch") await pywebview.api.batch_cancel();
    else await pywebview.api.stop_translate();
  };
  $("btn-ov-panel").onclick = () => showBatchPanel();

  const closeMask = (m) => {
    if (!m) return;
    m.classList.remove("on");
    // 批量还在跑：面板一关就把遮罩放回去。遮罩永远在最上层，才是真正挡住
    // 主界面的那一层；只靠面板会把「关掉面板还能点别的」这个口子留着。
    if (m.id === "modal-queue" && state.mbRunning) enterBatchMask();
  };
  document.querySelectorAll("[data-close]").forEach((b) => {
    b.onclick = () => closeMask(b.closest(".mask"));
  });
  document.querySelectorAll(".mask").forEach((m) => {
    m.onclick = (e) => { if (e.target === m) closeMask(m); };
  });

  $("btn-test").onclick = async () => {
    $("test-result").textContent = T("正在连接 %s/models ...").replace("%s", "");
    $("test-result").textContent = await pywebview.api.test_api($("set-api").value);
  };
  // 本地模型开关：一勾一取消就生效，不必等「保存」。
  // 停用会顺手把模型从显存里卸掉——27B 常驻 21 GB，翻完不卸就在跟游戏抢卡。
  $("set-local").onchange = async () => {
    const on = $("set-local").checked;
    let r = null;
    try { r = await pywebview.api.local_model_set(on); } catch (_) { r = null; }
    if (!r) { toast(T("操作失败：后端没有响应"), "err"); refreshLocalModel(); return; }
    if (r.ok === false) { toast(r.error || T("操作失败"), "err"); refreshLocalModel(); return; }
    if (on) toast(T("已启用本地模型"), "ok");
    else if (r.unloaded) toast(T("已停用本地模型，显存已释放（%s）").replace("%s", r.detail || ""), "ok");
    else toast(T("已停用本地模型 · %s").replace("%s", r.detail || ""), "");
    refreshLocalModel();
  };
  $("btn-model-off").onclick = async () => {
    let r = null;
    try { r = await pywebview.api.local_model_set(false); } catch (_) { r = null; }
    $("set-local").checked = false;
    if (r && r.unloaded) toast(T("显存已释放（%s）").replace("%s", r.detail || ""), "ok");
    else toast((r && r.detail) || T("释放失败"), "err");
    refreshLocalModel();
  };
  $("btn-save-settings").onclick = () => saveSettings();
  $("btn-pick-staging").onclick = async () => {
    const p = await pywebview.api.pick_folder();
    if (p) $("set-staging").value = p;
  };
  $("btn-pick-downloads").onclick = async () => {
    const p = await pywebview.api.pick_folder();
    if (p) $("set-downloads").value = p;
  };
  $("btn-pick-batch-out").onclick = async () => {
    const p = await pywebview.api.pick_folder();
    if (p) $("set-batch-out").value = p;
  };
  $("btn-vortex-detect").onclick = detectVortexDirs;
  $("btn-vortex-scan").onclick = async () => {
    if (!(await saveSettings({ close: false, quiet: true }))) return;
    await scanVortex(true);
  };
  $("btn-vmap").onclick = openVmap;
  $("btn-vmap-rescan").onclick = () => scanVortex(true);
  $("btn-vmap-copy").onclick = copyVmap;
  $("btn-vmap-open").onclick = () => {
    const p = (state.settings || {}).staging_dir || "";
    if (p) pywebview.api.open_dir(p, "");
    else toast(T("还没有设置安装缓存目录"), "err");
  };
  $("vmap-filters").querySelectorAll(".chip").forEach((c) => {
    c.onclick = () => {
      $("vmap-filters").querySelectorAll(".chip").forEach((x) => x.classList.remove("on"));
      c.classList.add("on");
      state.vmapFilter = c.dataset.state;
      renderVmap();
    };
  });
  $("vmap-search").oninput = (e) => {
    state.vmapSearch = e.target.value.trim().toLowerCase();
    renderVmap();
  };
  $("btn-tr-continue").onclick = continueTranslate;
  $("btn-tr-confirm").onclick = () => {
    closeModal("modal-tr-result");
    state.trFailed = [];
    render();
  };
  $("set-dark").onchange = (e) => {
    state.settings.dark_mode = e.target.checked; applyTheme();
  };

  $("btn-g-add").onclick = () => {
    const src = $("g-src").value.trim(), dst = $("g-dst").value.trim();
    if (!src || !dst) return;
    state.glossary.push({ src, dst, note: $("g-note").value.trim() });
    $("g-src").value = $("g-dst").value = $("g-note").value = "";
    renderGlossary();
  };
  $("btn-g-save").onclick = async () => {
    const r = await pywebview.api.glossary_save(state.glossary);
    state.glossary = r.terms; renderGlossary(); toast(T("术语表已保存"), "ok");
  };
  $("btn-g-reset").onclick = async () => {
    const r = await pywebview.api.glossary_reset();
    state.glossary = r.terms; renderGlossary(); toast(T("术语已恢复默认"), "ok");
  };
  $("btn-g-import").onclick = async () => {
    const r = await pywebview.api.glossary_import();
    if (r.ok) { state.glossary = r.terms; renderGlossary(); toast(T("术语表已保存"), "ok"); }
  };
  $("btn-g-export").onclick = async () => { await pywebview.api.glossary_export(); };
  $("btn-mem-clear").onclick = async () => {
    await pywebview.api.memory_clear();
    refreshMemoryInfo();
    toast(T("已清空翻译记忆"), "ok");
  };
  $("btn-glossary").onclick = async () => {
    state.glossary = await pywebview.api.glossary_get();
    renderGlossary(); openModal("modal-glossary");
  };
}

/* ---------------- 数据 ---------------- */
async function loadMod() {
  const path = $("path").value.trim();
  if (!path) { toast(T("请先选择有效的模组目录"), "err"); return; }
  // 换模组会把当前表格整份换掉（P6）。没应用的译文、没来得及写回的机翻结果
  // 都在内存里，以前加载一下就没了，连提示都没有。
  if (state.dirty) {
    const ok = confirm(T("当前表格里还有没应用的改动，加载新模组会把它们丢掉。\n\n继续加载吗？"));
    if (!ok) { $("path").value = state.modPath; return; }
  }
  setBusy(true, T("正在提取可翻译文本 ..."));
  const r = await pywebview.api.load_mod(path, state.opts);
  if (!r.ok) {
    setBusy(false);
    if (r.error !== "busy") toast(r.error || "error", "err");
    // 加载没成功，路径输入框就得退回它真正指向的那份数据（P4）。
    // 以前这里已经把 state.modPath 改了，之后 root = state.root || state.modPath
    // 会指向一个跟表格毫无关系的目录——再点应用就写到别人家里去了。
    $("path").value = state.modPath;
  }
}

/* 三层填充的账：官方基线 / 沿用你改过的 / 记忆。
   返回空串表示这次没有任何填充，调用方自己决定要不要提示。 */
function baselineLine(r) {
  const bl = (r && r.baseline) || null;
  if (!bl) return "";
  const parts = [];
  if (bl.official) {
    parts.push(T("官方基线 %d 条（%s%）")
      .replace("%d", bl.official)
      .replace("%s", (100 * bl.official / Math.max(1, bl.total || 1)).toFixed(1)));
  }
  if (bl.officialWeak) {
    parts.push(T("官方待核 %d 条").replace("%d", bl.officialWeak));
  }
  if (bl.manual) parts.push(T("沿用你改过的 %d 条").replace("%d", bl.manual));
  if (bl.memory) parts.push(T("记忆 %d 条").replace("%d", bl.memory));
  if (parts.length) return parts.join(" · ");
  if (bl.officialAvailable === false && bl.officialReason) {
    return T("官方基线未启用：%s").replace("%s", bl.officialReason);
  }
  return "";
}

/* 译文来源：官方基线 / 记忆 / 机翻 / 手改。
   用左侧色条区分而不是加徽章——表格已经很挤，色条不占字宽。
   来源只影响视觉与筛选，不参与复制和导出（导出走的是纯文本）。 */
const TRANSRC_LABEL = {
  official: "官方译文", "official-weak": "官方译文（待核）",
  manual: "你改过的", memory: "来自记忆", machine: "机翻"
};
const TRANSRC_CLASS = {
  official: "ori-official", "official-weak": "ori-official-weak",
  manual: "ori-manual", memory: "ori-memory", machine: "ori-machine"
};

function transCls(r) {
  const k = TRANSRC_CLASS[r.transSrc];
  return k ? "td trans " + k : "td trans";
}

function transTitle(r) {
  const label = TRANSRC_LABEL[r.transSrc];
  if (!label || !r.trans) return r.trans || "";
  return T(label) + "：" + r.trans;
}

function onLoaded(r) {
  setBusy(false);
  // 先切断编辑状态，再替换 rows。反过来的话 render() 末尾的 syncSelection
  // 会调 clearSelection → flushEditor → 拿着上一份的 sel 往新表的同一索引里写。
  state.sel = -1;
  _transDirty = false;
  if ($("trans")) $("trans").value = "";
  state.modPath = $("path").value.trim();
  state.root = r.root || state.modPath;
  state.archive = r.archive || "";
  $("btn-pack").style.display = state.archive ? "" : "none";
  state.meta = r.meta || {};
  state.resMode = r.mode === "resource";
  // 换了一份数据，上一次的产物就与当前表格无关了：撤销与「安装到游戏」
  // 都必须收回，否则会去装上一批译文打包出来的归档。
  state.resResult = null;
  state.revertUndo = false;
  state.raw = (r.rows || []).map((x, i) => ({ ...x, _i: i }));
  state.rows = state.raw;
  state.merged = false;
  state.hidden = new Set();
  state.onlyFile = null;
  state.transMode = "";
  // 上一份的筛选与搜索不能带进新数据：搜索词会把新表格直接滤空，而状态栏
  // 还在报「已加载 N 段文本」，看起来像提取失败。与 clearCurrentRows 一致。
  state.filter = "全部";
  state.search = "";
  $("filter").value = "全部";
  $("search").value = "";
  // 新表刚进来，里面还没有任何「未应用的改动」（P6）。
  // 以前这个标志只置不清，加载一次以后永远是 true，拿它做确认框等于每次都弹。
  state.dirty = false;
  updateScopeInfo();
  updateNoiseInfo();
  state.page = 1;
  applyModeUi();
  render();
  syncEditorEnabled();
  $("empty").textContent = (!state.rows.length && r.skippedTarget)
    ? T("没有可提取的原文（已跳过 %d 条目标语言文本）").replace("%d", r.skippedTarget)
    : T("未加载 — 先选模组目录点「加载 / 提取」");
  const tb = $("tbody");
  requestAnimationFrame(function () { tb.scrollTop = 0; });

  let msg = T("已加载 %d 段文本 / %d 个文件")
    .replace("%d", state.rows.length)
    .replace("%d", Object.keys(groupFiles()).length);
  if (r.mode === "provider") {
    msg = T("Provider 配对模式: %s").replace("%s", r.source)
      + " · " + msg;
    if (r.pairCount) msg += " · " + T("已配对 %d 组").replace("%d", r.pairCount);
    if (r.unpaired) msg += " · " + T("未配对 %d 个 Provider").replace("%d", r.unpaired);
  } else if (r.mode === "locale") {
    msg = T("语言包模式: %s → %s").replace("%s", r.source).replace("%s", r.target)
      + " · " + msg;
  } else if (r.mode === "resource") {
    msg = T("资源模式: %s（原文 %s）").replace("%s", r.source).replace("%s", r.target)
      + " · " + msg;
    if (r.extend) msg += " · " + T("挂接 %s").replace("%s", r.extend);
  } else if (state.opts.localeFirst) {
    msg = T("内容模式（模组无语言包）") + " · " + msg;
  }
  if (r.skippedTarget) {
    const doneN = state.rows.filter((x) => x.already).length;
    msg += doneN
      ? " · " + T("已跳过 %d 条目标语言文本（其中 %d 条已译，已列在表里）")
          .replace("%d", r.skippedTarget).replace("%d", doneN)
      : " · " + T("已跳过 %d 条目标语言文本").replace("%d", r.skippedTarget);
  }
  if (r.parseErrors) {
    // 解析失败的文件以前是静默消失的：界面上只表现为「这个模组没有文本」。
    msg += " · " + T("%d 个文件解析失败（已跳过）").replace("%d", r.parseErrors);
  }
  if (r.userSkip) {
    // 用户自己标成「代码」的行：不再出现，但要让人知道它们还在排除表里
    msg += " · " + T("%d 条已按「标记为代码」隐藏").replace("%d", r.userSkip);
  }
  if (r.localeRows || r.sourceRows) {
    msg += " · " + T("语言包 %d 行 / 设置与源码 %d 行")
      .replace("%d", r.localeRows || 0).replace("%d", r.sourceRows || 0);
    const longN = state.rows.filter((x) => x.type === "长句文本").length;
    if (longN) {
      msg += " · " + T("其中长句 %d 条, 关掉「长句兜底」可只看界面文本")
        .replace("%d", longN);
    }
  }
  // 归档层：.reds 只写 key、文案在 .archive 里的模组，靠这一层才看得到真文本。
  if (r.resourceRows) {
    msg += " · " + T("归档资源 %d 行").replace("%d", r.resourceRows);
    const rinfo = (r.meta && r.meta.resource) || {};
    if (rinfo.skipped) {
      msg += T("（已跳过 %d 条目标语言）").replace("%d", rinfo.skipped);
    }
  }
  if (r.meta && r.meta.conflicts) {
    msg += " · " + T("%d 句同时出现在明文与归档里").replace("%d", r.meta.conflicts);
  }
  if (r.meta && r.meta.locale_pairs) {
    msg += " · " + T("多语言替换表 %d 项，仅提取目标语言 %d 项")
      .replace("%d", r.meta.locale_pairs)
      .replace("%d", r.meta.locale_pairs_used || 0);
  }
  // 三层填充的账：官方基线 / 沿用你改过的 / 记忆。
  const blLine = baselineLine(r);
  if (blLine) msg += " · " + blLine;
  else if (r.tmHits && !r.baseline) {
    msg += " " + T("记忆命中 %d 条").replace("%d", r.tmHits);
  }
  if (r.officialHits && !r.baseline) {
    msg += " · " + T("官方语料命中 %d 条").replace("%d", r.officialHits);
  }
  // 活动映射：这个目录有没有被 Vortex 部署。没部署的话，写回去游戏也读不到。
  const ms = r.mapState;
  if (ms && ms.active) {
    msg += " · " + T("已生效（%d 个文件，%s 部署）")
      .replace("%d", ms.files || 0).replace("%s", ms.deployedAt || "?");
  } else if (ms && !ms.active) {
    msg += " · " + T("未生效：该目录不在 Vortex 部署清单里");
    toast(T("这个目录没有生效（Vortex 没把它部署进游戏）。\n改动写入后游戏读不到——先在 Vortex 里 Deploy，或改加载已生效的版本。"), "err");
  }
  setStatus(msg);
}

function groupFiles() {
  const m = {};
  state.rows.forEach((r) => { m[r.file] = 1; });
  return m;
}

function baseName(p) {
  return String(p || "").split(/[\\/]/).pop();
}

/* 两种模式的界面差异集中在这里：资源模式没有行号、没有撤销、没有 zip 打包。 */
function applyModeUi() {
  const on = !!state.resMode;
  $("btn-l10n-out").style.display = on ? "" : "none";
  $("btn-l10n-install").style.display = (on && state.resResult) ? "" : "none";
  // 资源模式过去没有撤销（产物在 userdata\output，删不删无所谓）。现在产物
  // 落到 mod 目录里成了实打实的文件，必须有办法撤掉，且只在真产出了才亮。
  $("btn-undo").style.display =
    (!on || (state.resResult && state.resResult.undoAvailable) || state.revertUndo)
      ? "" : "none";
  $("btn-pack").style.display = (!on && state.archive) ? "" : "none";
  const th = $("thead").querySelector(".th.line");
  if (th) th.textContent = on ? T("条目 ID") : T("行号");
  $("btn-apply").textContent = on ? T("生成翻译 mod") : T("应用到模组");
}

function filtered() {
  const kw = state.search.trim().toLowerCase();
  return state.rows.filter((r) => {
    if (state.onlyFile && r.file !== state.onlyFile) return false;
    if (state.hidden.has(r.file)) return false;
    if (state.transMode === "hide" && r.trans) return false;
    if (state.transMode === "only" && !r.trans) return false;
    const f = state.filter;
    if (f === "未填译文" && r.trans) return false;
    if (f === "已填译文" && !r.trans) return false;
    // 原文=译文：模型偶尔把原文原样吐回来（专名、术语、偷懒），表面上
    // 「已填」，其实一个字都没翻。这类行只能靠比对才找得出来。
    if (f === "原文=译文") {
      // 引擎已判定「本来就是目标语言」的行：trans 预填成原文，不是漏翻
      if (r.already) return false;
      const o = String(r.orig || "").trim();
      const t = String(r.trans || "").trim();
      if (!t || o !== t) return false;
    }
    if (f === "官方译文" && r.transSrc !== "official") return false;
    if (f === "官方待核" && r.transSrc !== "official-weak") return false;
    if (f === "记忆译文" && r.transSrc !== "memory") return false;
    if (f === "设置项" && !SETTING_TYPES.includes(r.type)) return false;
    if (f === "界面文本" && r.type !== "界面文本") return false;
    if (f === "长句文本" && r.type !== "长句文本") return false;
    if (f === "已跳过" && !r.skip) return false;
    if (f === "未跳过" && r.skip) return false;
    if (kw) {
      // 译文也要能搜：翻完想回头核对某句时，中文关键词只存在于 trans 里。
      const hay = (r.orig + "\n" + (r.trans || "") + "\n"
                   + (r.disp || r.file) + "\n" + (r.ctx || "")).toLowerCase();
      if (!hay.includes(kw)) return false;
    }
    return true;
  });
}

/* ---------------- 渲染 ---------------- */
const MAX_RENDER = 3000;

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function updateScopeInfo() {
  const n = state.hidden.size;
  const parts = [];
  if (state.onlyFile) parts.push(T("只显示 %s").replace("%s", state.onlyFile.split("/").pop()));
  if (n) parts.push(T("已隐藏 %d 个文件").replace("%d", n));
  if (state.transMode === "hide") parts.push(T("已隐藏已翻译"));
  if (state.transMode === "only") parts.push(T("只显示已翻译"));
  $("scope-info").textContent = parts.join(" · ");
  $("btn-clear-scope").style.display = parts.length ? "" : "none";
}

function hideFile(file) {
  if (!file) return;
  state.hidden.add(file);
  if (state.onlyFile === file) state.onlyFile = null;
  state.page = 1;
  updateScopeInfo();
  render();
  setStatus(T("已隐藏 %d 个文件").replace("%d", state.hidden.size));
}

function onlyFile(file) {
  if (!file) return;
  state.hidden.delete(file);
  state.onlyFile = file;
  state.page = 1;
  updateScopeInfo();
  render();
}

function clearScope() {
  state.hidden = new Set();
  state.onlyFile = null;
  state.transMode = "";
  state.page = 1;
  updateScopeInfo();
  render();
  setStatus(T("已显示全部行"));
}

// 「只显示此文件」的逆操作：只放开这一条限制，别顺手把隐藏和已翻译筛选也清了。
function cancelOnlyFile() {
  state.onlyFile = null;
  state.page = 1;
  updateScopeInfo();
  render();
  setStatus(T("已显示所有文件"));
}

// 被隐藏的文件在表格里右击不到，这是唯一能把它们找回来的入口。
function clearHidden() {
  if (!state.hidden.size) return;
  const n = state.hidden.size;
  state.hidden = new Set();
  state.page = 1;
  updateScopeInfo();
  render();
  setStatus(T("已恢复隐藏的 %d 个文件").replace("%d", n));
}


function translatedInScope() {
  return state.rows.filter((r) => {
    if (!r.trans) return false;
    if (state.onlyFile && r.file !== state.onlyFile) return false;
    if (state.hidden.has(r.file)) return false;
    return true;
  }).length;
}

function setTransMode(mode) {
  const n = translatedInScope();
  if (!n) { toast(T("当前范围内没有已翻译的行"), "err"); return; }
  state.transMode = mode;
  state.page = 1;
  updateScopeInfo();
  render();
  setStatus(mode === "hide"
    ? T("已隐藏 %d 条已翻译行").replace("%d", n)
    : T("只显示已翻译的行"));
}

function clearTransMode() {
  state.transMode = "";
  state.page = 1;
  updateScopeInfo();
  render();
  setStatus(T("已显示全部译文状态"));
}

function toggleTranslated(mode) {
  if (state.transMode === mode) clearTransMode();
  else setTransMode(mode);
}

/* ---------------- 跳过翻译 / 清空 ---------------- */

// 行身份 = 文件 + 条目 ID + 原文。
//
// entryId 必须算进去：资源模式里同一个文件、同一句原文可以对应多个不同条目
// （ADE 的 110 条里有 44 条同文重复），以前只按 (文件, 原文) 认人，「跳过此行」
// 会把这 44 条一起跳过，「清空此行译文」也一起清空。源码模式没有 entryId，
// 拼出来是空串，行为与以前完全一致。
function rowKey(x) {
  return String(x.file || "") + "\u0000"
       + String(x.entryId || "") + "\u0000"
       + String(x.orig || "");
}

// 一行在「合并视图」里代表整组同文条目；在普通视图里就只有它自己。
// 跳过/清空按这个集合生效：合并时整组一起动，展开时只动被点的那一条。
function rowKeysFor(row) {
  const keys = new Set();
  if (!row) return keys;
  keys.add(rowKey(row));
  (row.dups || []).forEach((d) => keys.add(rowKey(d)));
  return keys;
}

function originalIndicesForRow(row) {
  if (row && Array.isArray(row.dups) && row.dups.length) {
    return row.dups.map((d) => Number(d._i)).filter((n) => Number.isInteger(n));
  }
  return row && row._i != null ? [Number(row._i)] : [];
}

function setRowsSkip(rows, value) {
  const keys = new Set();
  (rows || []).forEach((row) => {
    if (!row) return;
    row.skip = !!value;
    // 合并视图下这一行代表整组，整组跟着跳；普通视图没有 dups，只动自己。
    (row.dups || []).forEach((d) => {
      d.skip = !!value;
      keys.add(rowKey(d));
    });
    keys.add(rowKey(row));
  });
  state.raw.forEach((raw) => {
    if (keys.has(rowKey(raw))) raw.skip = !!value;
  });
  state.dirty = true;
  render();
  setStatus(value
    ? T("已跳过 %d 条内容；机翻和应用时都会忽略。").replace("%d", rows.length)
    : T("已恢复 %d 条内容的翻译。").replace("%d", rows.length));
}

function setFileSkip(file, value) {
  let n = 0;
  state.raw.forEach((row) => {
    if (row.file !== file) return;
    if (!!row.skip !== !!value) n += 1;
    row.skip = !!value;
  });
  state.rows.forEach((row) => {
    if (row.file !== file) return;
    row.skip = !!value;
    (row.dups || []).forEach((d) => { d.skip = !!value; });
  });
  state.dirty = true;
  render();
  setStatus(value
    ? T("已跳过此文件 %d 条内容；机翻和应用时都会忽略。").replace("%d", n)
    : T("已恢复此文件 %d 条内容的翻译。").replace("%d", n));
}

async function clearRowTranslation(row) {
  if (!row) return;
  if (!String(row.trans || "").trim()) {
    toast(T("这一行还没有译文。"), "err");
    return;
  }
  if (!confirm(T("清空这一行的译文？\n原文和模组文件都不会改动，只是把已填的译文去掉。"))) {
    return;
  }
  // 先同步后端（含让这条机翻记忆失效），再清本地，否则重翻会拿到旧结果
  await syncClearedRows(originalIndicesForRow(row));
  const keys = rowKeysFor(row);
  state.raw.forEach((raw) => {
    if (keys.has(rowKey(raw))) raw.trans = "";
  });
  row.trans = "";
  (row.dups || []).forEach((d) => { d.trans = ""; });
  state.dirty = true;
  render();
  setStatus(T("已清空此行译文。"));
}

async function clearCurrentTranslations() {
  if (state.busy) return;
  if (!state.rows.length) { toast(T("表格是空的，先加载模组目录。"), "err"); return; }
  if (!confirm(T("清空当前提取结果中的全部译文？\n原文和表格行会保留，模组文件不会修改。"))) return;
  try { await pywebview.api.clear_translations(); } catch (_) {}
  state.raw.forEach((r) => { r.trans = ""; });
  state.rows.forEach((r) => { r.trans = ""; (r.dups || []).forEach((d) => { d.trans = ""; }); });
  state.sel = -1;
  $("trans").value = "";
  state.dirty = true;
  render();
  syncEditorEnabled();
  setStatus(T("已清空当前提取结果的全部译文（模组文件未修改）。"));
}

async function clearCurrentRows() {
  if (state.busy) return;
  if (!state.rows.length && !state.raw.length) {
    toast(T("当前没有提取结果。"), "err");
    return;
  }
  if (!confirm(T("清空当前提取结果和全部译文？\n这不会修改模组文件，模组目录会保留。"))) return;
  try { await pywebview.api.clear_rows(); } catch (_) {}
  state.rows = [];
  state.raw = [];
  state.view = [];
  state.meta = {};
  state.sel = -1;
  state.page = 1;
  state.dirty = false;
  state.merged = false;
  state.hidden = new Set();
  state.onlyFile = null;
  state.transMode = "";
  state.filter = "全部";
  state.search = "";
  state.applyErrors = [];
  $("filter").value = "全部";
  $("search").value = "";
  $("trans").value = "";
  $("empty").textContent = T("未加载 — 先选模组目录点「加载 / 提取」");
  updateScopeInfo();
  updateNoiseInfo();
  render();
  syncEditorEnabled();
  setStatus(T("已清空当前提取结果（模组文件未修改）。"));
}

let _ctxTarget = null;
let _ctxCell = null;

const COL_NAMES = {
  num: "序号", skip: "跳过", file: "文件", line: "行号", type: "类型",
  ctx: "上下文", orig: "原文", trans: "译文"
};

function cellAtPoint(row, x, y) {
  const cells = Array.from(row.querySelectorAll(".td"));
  return cells.find((cell) => {
    const r = cell.getBoundingClientRect();
    return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  }) || null;
}

// 文件列的「完整路径」：模组根 + 相对路径。
// 资源行没有独立的磁盘文件——它的 file 是归档内部路径，这一行真正对应的
// 文件就是那个 .archive，所以给归档的绝对路径。
function fullPathOf(row) {
  const f = String((row && row.file) || "");
  if (!f) return "";
  if (row.layer === "resource") return row.archive || f;
  if (/^[A-Za-z]:[\\/]/.test(f) || f.indexOf("\\\\") === 0) return f;
  const root = String(state.root || "").replace(/[\\/]+$/, "");
  if (!root) return f;
  return root + "\\" + f.replace(/\//g, "\\");
}

function ctxCellInfo(row, cell) {
  if (!cell) return null;
  const col = cell.dataset.col || "";
  const label = T(COL_NAMES[col] || "单元格");
  // 「跳过」是勾选框而不是文本，这一格不参与复制。
  if (col === "skip") return { col, label, value: "", disabled: true };
  let value = "";
  if (col === "num") {
    value = (cell.textContent || "").replace(/×\d+\s*$/, "").trim();
  } else if (col === "file") {
    value = fullPathOf(row);       // 显示的是裁切后的相对路径，复制出去要完整的
  } else if (col === "line") {
    value = row.line == null || row.line === "" ? "" : String(row.line);
    if (!value) value = String(row.entryId || "");   // 资源模式用条目 ID 顶行号
  } else if (col === "type") {
    value = T(row.type || "");
  } else if (col === "ctx") {
    value = row.ctx || "";
  } else if (col === "orig") {
    value = row.orig || "";
  } else if (col === "trans") {
    value = row.trans || "";
  }
  value = String(value);
  return { col, label, value, disabled: !value };
}

function showCtxMenu(x, y, row, cell) {
  _ctxTarget = row;
  _ctxCell = ctxCellInfo(row, cell);
  const m = $("ctxmenu");
  const file = row.file || "";
  const isOnly = state.onlyFile === file;
  const isHideTrans = state.transMode === "hide";
  const isOnlyTrans = state.transMode === "only";
  const hideLabel = isHideTrans ? T("显示已翻译的行") : T("隐藏已翻译的行");
  const onlyLabel = isOnlyTrans ? T("显示所有行") : T("只显示已翻译的行");

  // 第一类只认左键选中的那一行：右击的位置只是「在哪儿打开菜单」，不是操作
  // 目标。没选中就整组置灰，让「点一行再动手」这件事在菜单里看得见。
  const hasSel = state.sel >= 0 && !!state.rows[state.sel];
  const selRow = hasSel ? state.rows[state.sel] : null;
  const dis1 = hasSel ? "" : " disabled";
  const skipLabel = selRow && selRow.skip ? T("恢复选中的行") : T("跳过选中的行");

  // 复制：整张表除「跳过」列外任意单元格都能复制；没落在格子上（行内空白、
  // 滚动条）或这一格是空的，都置灰——点了才有提示等于没提示。
  const canCopy = !!( _ctxCell && !_ctxCell.disabled && _ctxCell.value);
  const copyItem = `<div class="item${canCopy ? "" : " disabled"}" data-act="copy-cell">${
    esc(T("复制单元格内容"))}</div>`;

  // 第二类作用在右击行所属的文件上。
  const allSkipped = !state.rows.some((x) => x.file === file && !x.skip);
  // 文件的行一旦隐藏就点不到了，「显示此文件的行」在菜单里永远不可达；
  // 真正能把这些行找回来的入口是这句「恢复隐藏的 N 个文件」。
  const restoreItem = state.hidden.size
    ? `<div class="item" data-act="show-hidden">${esc(
        T("恢复隐藏的 %d 个文件").replace("%d", state.hidden.size))}</div>`
    : "";
  const mergeItem = state.resMode
    ? ""
    : `<div class="item" data-act="merge">${esc(
        state.merged ? T("还原重复行") : T("合并重复行"))}</div>`;

  // 「已筛选」= 当前视图（搜索 + 筛选 + 文件过滤 + 隐藏之后剩下的行）。
  // 数字不写进菜单：表格右上角「共 N 条」就是筛选后的总数，再写一遍是重复。
  const hasFiltered = filtered().length > 0;

  m.innerHTML =
    copyItem +
    `<div class="item${hasFiltered ? "" : " disabled"}" data-act="retrans-filtered">${esc(
      T("重新翻译已筛选的行"))}</div>` +
    `<div class="sep"></div>` +
    `<div class="ctxgroup${hasSel ? "" : " disabled"}">${esc(T("左键选中"))}</div>` +
    `<div class="item${dis1}" data-act="locate">${esc(T("定位到已选择的行"))}</div>` +
    `<div class="item${dis1}" data-act="toggle-skip">${esc(skipLabel)}</div>` +
    `<div class="item${dis1}" data-act="clear-row-trans">${esc(T("清空选中的行译文"))}</div>` +
    `<div class="item${dis1}" data-act="retrans">${esc(T("重新翻译选中的行"))}</div>` +
    `<div class="sep"></div>` +
    `<div class="item" data-act="skip-file">${esc(
      allSkipped ? T("恢复此文件所有行") : T("跳过此文件所有行"))}</div>` +
    `<div class="item" data-act="hide">${esc(T("隐藏此文件的行"))}</div>` +
    restoreItem +
    `<div class="item" data-act="only">${esc(isOnly ? T("显示所有文件") : T("只显示此文件"))}</div>` +
    `<div class="item" data-act="hide-trans">${esc(hideLabel)}</div>` +
    `<div class="item" data-act="only-trans">${esc(onlyLabel)}</div>` +
    mergeItem +
    `<div class="sep"></div>` +
    `<div class="item" data-act="open">${esc(
      row.archive ? T("打开归档所在目录") : T("打开文件所在目录"))}</div>`;
  m.classList.add("on");
  const rect = m.getBoundingClientRect();
  m.style.left = Math.max(6, Math.min(x, window.innerWidth - rect.width - 8)) + "px";
  m.style.top = Math.max(6, Math.min(y, window.innerHeight - rect.height - 8)) + "px";
}

function closeCtxMenu() { $("ctxmenu").classList.remove("on"); }

function _fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.left = "-9999px";
  ta.style.top = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
  ta.remove();
  return ok;
}

function copyToClipboard(text, okMsg) {
  const value = String(text == null ? "" : text);
  if (!value) { toast(T("没有可复制的内容"), "err"); return; }
  const done = () => toast(okMsg, "ok");
  const fallback = () => {
    if (_fallbackCopy(value)) done();
    else toast(T("复制失败"), "err");
  };
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      const p = navigator.clipboard.writeText(value);
      if (p && p.then) p.then(done, fallback);
      else done();
    } else {
      fallback();
    }
  } catch (_) {
    fallback();
  }
}

/* 同一文件内、"原文"相同的行合并成一行, 翻译一次即可写回所有重复位置 */
function buildMerged() {
  const map = new Map();
  const out = [];
  state.raw.forEach((r) => {
    const key = r.file + "\u0000" + r.orig;
    const hit = map.get(key);
    const slot = (x) => ({ file: x.file, line: x.line, orig: x.orig,
                           target: x.target || "", kind: x.kind || "",
                           key: x.key || "", _i: x._i, skip: !!x.skip });
    if (hit) {
      hit.dups.push(slot(r));
      hit.count += 1;
      if (!hit.trans && r.trans) hit.trans = r.trans;
      hit.skip = !!hit.skip && !!r.skip;
      return;
    }
    const row = { ...r, count: 1, dups: [slot(r)] };
    map.set(key, row);
    out.push(row);
  });
  out.forEach((r, i) => { r._i = i; });
  return out;
}

function expandRows(rows) {
  const out = [];
  (rows || []).forEach((r) => {
    if (r.dups && r.dups.length) {
      r.dups.forEach((d) => out.push({ ...d, trans: r.trans || "",
                                       skip: !!r.skip, _i: out.length }));
    } else {
      out.push({ ...r, skip: !!r.skip, _i: out.length });
    }
  });
  return out;
}

function toggleMerge() {
  if (!state.raw.length) { toast(T("表格是空的，先加载模组目录。"), "err"); return; }
  if (!state.merged) {
    const before = state.raw.length;
    state.rows = buildMerged();
    state.merged = true;
    state.page = 1;
    state.sel = -1;
    render();
    setStatus(T("已合并 %d 行重复文本（原 %d 行）")
      .replace("%d", before - state.rows.length).replace("%d", before));
  } else {
    state.rows = state.raw;
    state.merged = false;
    state.page = 1;
    state.sel = -1;
    render();
    setStatus(T("已还原为 %d 行").replace("%d", state.rows.length));
  }
}

function pageCount(list) {
  return Math.max(1, Math.ceil((list ? list.length : 0) / state.pageSize));
}

function gotoPage(p) {
  const total = pageCount(filtered());
  state.page = Math.max(1, Math.min(total, p));
  render();
  $("tbody").scrollTop = 0;
}

function render() {
  const list = filtered();
  const total = pageCount(list);
  if (state.page > total) state.page = total;
  if (state.page < 1) state.page = 1;
  const start = (state.page - 1) * state.pageSize;
  state.view = list.slice(start, start + state.pageSize);
  $("pg-info").textContent = T("第 %d / %d 页 · 共 %d 条")
    .replace("%d", state.page).replace("%d", total).replace("%d", list.length);
  $("pg-first").disabled = $("pg-prev").disabled = state.page <= 1;
  $("pg-next").disabled = $("pg-last").disabled = state.page >= total;
  const tbody = $("tbody");
  const parts = [];
  state.view.forEach((r, idx) => {
    const cls = ["trow"];
    if (r.trans) cls.push("filled");
    if (r.skip) cls.push("skipped");
    if (r.already) cls.push("already");
    if (r._i === state.sel) cls.push("sel");
    const tcls = r.type === "长句文本" ? "td type t-long" : "td type";
    parts.push(
      `<div class="${cls.join(" ")}" data-i="${r._i}">` +
      `<div class="td num" data-col="num">${idx + 1}` +
      (r.count > 1 ? `<span class="dup-badge" title="${esc(T("同文件内重复 ×%d").replace("%d", r.count))}">×${r.count}</span>` : "") +
      `</div>` +
      `<div class="td skip" data-col="skip"><input type="checkbox" class="skip-box"` +
      `${r.skip ? " checked" : ""} title="${esc(T("跳过此行翻译"))}"></div>` +
      `<div class="td file" data-col="file" title="${esc(
        r.archive ? r.archive + " > " + r.file : fullPathOf(r))}">` +
      `<span class="open-dir" data-file="${esc(r.file)}" title="${esc(
        r.archive ? T("打开归档所在目录") : T("打开文件所在目录"))}">` +
      `<svg viewBox="0 0 16 16"><path d="M1.5 4.5h4l1.2 1.6h7.8v6.4a1 1 0 0 1-1 1H2.5a1 1 0 0 1-1-1z"/>` +
      `<path d="M1.5 4.5V3.4a1 1 0 0 1 1-1h3.2l1.3 1.7"/></svg></span>` +
      `<span class="fname">${esc(r.disp || r.file)}</span></div>` +
      `<div class="td line" data-col="line">${r.line || esc(r.entryId || "") || "-"}</div>` +
      `<div class="${tcls}" data-col="type">${esc(T(r.type))}</div>` +
      `<div class="td ctx" data-col="ctx">${esc(r.ctx)}</div>` +
      `<div class="td orig" data-col="orig" title="${esc(r.orig)}">` +
      (r.already ? `<span class="already-tag" title="${esc(
        T("模组里本来就是这个语言的文本。改译文列即可覆盖。"))}">${esc(T("已译"))}</span>` : "") +
      `${esc(r.orig)}</div>` +
      `<div class="${transCls(r)}" data-col="trans" title="${esc(transTitle(r))}">${esc(r.trans)}</div>` +
      `</div>`);
  });
  tbody.innerHTML = parts.join("");
  $("empty").style.display = (state.rows.length && list.length) ? "none" : "flex";
  if (state.rows.length && !list.length) $("empty").textContent = T("没有匹配的行");
  updateStats(list);
  syncSelection(list);
  // 每次重画后校正一次编辑条：没选中行就不该能打字。
  syncEditorEnabled();
}

function updateStats(list) {
  const rows = list || state.rows;
  const filled = rows.filter((r) => r.trans).length;
  const skipped = rows.filter((r) => r.skip).length;
  const done = rows.filter((r) => r.already).length;
  let s = T("共 %d 段 | 已填 %d | 未填 %d | 跳过 %d")
    .replace("%d", rows.length)
    .replace("%d", filled)
    .replace("%d", rows.length - filled)
    .replace("%d", skipped);
  if (done) s += " | " + T("其中已译 %d").replace("%d", done);
  $("stats").textContent = s;
}

function scrollToSel() {
  const el = $("tbody").querySelector(`.trow[data-i="${state.sel}"]`);
  if (el) el.scrollIntoView({ block: "nearest" });
}

/* 编辑时表格自动跟着走：编辑条获得焦点、或内容发生变化时，把「正在编辑的
   那一行」翻到当前页并滚进视野。用户眼睛在输入框上，表格自己跟过来就好，
   不必再按按钮或右键去"跳回"。

   注意这里不调 selectRow：它会把焦点抢回输入框，和 focus 事件凑成环。 */
function followEditingRow() {
  if (state.sel < 0) return;
  // 当前页就有这一行：直接滚，不必再跑一遍全表过滤（打字时会频繁调到这里）
  let el = $("tbody").querySelector(`.trow[data-i="${state.sel}"]`);
  if (!el) {
    const pos = filtered().findIndex((r) => r._i === state.sel);
    if (pos < 0) return;            // 被筛选/隐藏了，交给 syncSelection 处理
    const page = Math.floor(pos / state.pageSize) + 1;
    if (page !== state.page) { state.page = page; render(); }
    el = $("tbody").querySelector(`.trow[data-i="${state.sel}"]`);
  }
  if (el) el.scrollIntoView({ block: "nearest" });
}

let _followTimer = 0;
function followEditingSoon() {
  clearTimeout(_followTimer);
  _followTimer = setTimeout(followEditingRow, 120);
}

/* ---------------- 选择 / 编辑 ---------------- */
$("tbody") && null;
document.addEventListener("click", (e) => {
  if (e.target.closest(".skip-box")) return;
  const od = e.target.closest(".open-dir");
  if (od) {
    e.stopPropagation();
    const tr = od.closest(".trow");
    const row = tr ? state.rows[Number(tr.dataset.i)] : null;
    // 资源行没有独立磁盘文件，它的「文件」就是那个归档
    if (row && row.archive) pywebview.api.open_archive_dir(row.archive);
    else pywebview.api.open_dir(state.root || state.modPath, od.dataset.file);
    return;
  }
  const row = e.target.closest(".trow");
  if (!row) return;
  selectRow(Number(row.dataset.i));
});
document.addEventListener("dblclick", (e) => {
  const cell = e.target.closest(".td.trans");
  if (!cell) return;
  inlineEdit(cell);
});
document.addEventListener("change", (e) => {
  const box = e.target.closest(".skip-box");
  if (!box) return;
  const tr = box.closest(".trow");
  const row = tr ? state.rows[Number(tr.dataset.i)] : null;
  if (row) setRowsSkip([row], !!box.checked);
});

// 编辑框里的字以前只在回车那一下才落进数据模型：点别的行、点「应用到模组」、
// 切到别的窗口，刚敲的译文就没了，而且没有任何提示。现在换行与失焦两个时机
// 都先结算一次，写的是「当时选中的那一行」，不会串到新选中的行上。
//
// _transDirty 只认「用户真的敲过」：程序回填（选中别的行、机翻结果回来）也会
// 改输入框的值，不加这个标志的话，一次回填就会被当成用户输入写进上一行。
let _transDirty = false;

function flushEditor() {
  if (!_transDirty) return false;
  _transDirty = false;
  if (state.sel < 0) return false;
  const r = state.rows[state.sel];
  const el = $("trans");
  if (!r || !el) return false;
  const v = String(el.value || "").trim();
  if (v === String(r.trans || "")) return false;
  // 删空 = 清空：后端那份也要一起清，否则重翻时会被当成「本来就有译文」跳过
  if (!v) syncClearedRows(originalIndicesForRow(r));
  r.trans = v;
  r.transSrc = "manual";   // 用户亲手改的，来源归他，别让官方语料盖回去
  state.dirty = true;
  return true;
}

function selectRow(i) {
  flushEditor();           // 先结算上一行，再换目标
  state.sel = i;
  document.querySelectorAll(".trow.sel").forEach((el) => el.classList.remove("sel"));
  const el = $("tbody").querySelector(`.trow[data-i="${i}"]`);
  if (el) el.classList.add("sel");
  const r = state.rows[i];
  $("trans").value = r ? r.trans || "" : "";
  _transDirty = false;     // 这次是程序回填，不是用户输入
  syncEditorEnabled();
  $("trans").focus();
  scrollToSel();
}

// 把某一行拉回视野：先确认它还没被筛掉，再翻到它所在的页、滚到中间。
// 「定位到已选择的行」与后续快捷键都走这里，避免各处各自算一遍页码。
function locateRow(i) {
  const list = filtered();
  const pos = list.findIndex((r) => r._i === i);
  if (pos < 0) {
    toast(T("这一行正被筛选或隐藏，先恢复显示才能定位"), "err");
    return false;
  }
  const page = Math.floor(pos / state.pageSize) + 1;
  if (page !== state.page) { state.page = page; render(); }
  selectRow(i);
  const el = $("tbody").querySelector(`.trow[data-i="${i}"]`);
  if (el) el.scrollIntoView({ block: "center" });
  return true;
}

// 右键菜单「定位到已选择的行」：入口是右键，目标是「已选中」的那一行。
// 右击的那一行不一定是选中行（用户可能只是想在别处打开菜单），所以不能拿
// 它当定位目标——那样点完看到的还是原地没动，等于没定位。
function locateSelected() {
  if (state.sel < 0 || !state.rows[state.sel]) {
    toast(T("还没有选择任何行，先点一行再试"), "err");
    return false;
  }
  return locateRow(state.sel);
}

// 选中的行被筛选/隐藏掉之后，它的「选中」状态就没有意义了：
// 编辑框还挂着它的译文、快捷键还指向它，可表格里根本看不到这一行。
// 这里统一收口，保证「不可见 = 未选中」。
function clearSelection() {
  flushEditor();          // 选中行被藏起来之前，先把它输入框里的字收回来
  state.sel = -1;
  const t = $("trans");
  if (t) t.value = "";
  _transDirty = false;
  document.querySelectorAll(".trow.sel").forEach((el) => el.classList.remove("sel"));
  syncEditorEnabled();
}

/* 没有选中行时编辑条没有目标：能打字，但写不到任何地方，也没有提示。
   这里统一收口，让「能不能编辑」跟着选中状态走。 */
function syncEditorEnabled() {
  const on = state.sel >= 0 && !!state.rows[state.sel] && !state.busy;
  const t = $("trans");
  if (t) {
    t.disabled = !on;
    t.placeholder = on
      ? T("选中一行后在里输入译文, 回车确认并跳到下一行")
      : T("先在表格里点一行");
  }
  const b = $("btn-tr-row");
  if (b) b.disabled = !on;
}

function syncSelection(list) {
  if (state.sel < 0) return;
  if (!state.rows[state.sel]) { clearSelection(); return; }
  const rows = list || filtered();
  if (!rows.some((r) => r._i === state.sel)) {
    clearSelection();
    setStatus(T("选中的行已被筛选/隐藏，已取消选择"));
  }
}

function inlineEdit(cell) {
  const row = cell.closest(".trow");
  const i = Number(row.dataset.i);
  const r = state.rows[i];
  const input = document.createElement("input");
  input.className = "cell";
  input.value = r.trans || "";
  cell.textContent = "";
  cell.appendChild(input);
  input.focus();
  input.select();
  const commit = (save) => {
    if (save) {
      r.trans = input.value.trim();
      // 表格里删空也是清空：后端那份不同步，重翻就会被当成「已有译文」跳过
      if (!r.trans) syncClearedRows(originalIndicesForRow(r));
    }
    render();
    selectRow(i);
  };
  input.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(true); }
    if (e.key === "Escape") { e.preventDefault(); commit(false); }
  };
  input.onblur = () => commit(true);
}

function commitEditor(advance) {
  if (state.sel < 0) return;
  flushEditor();
  render();
  if (advance) step(1);
}

function step(d) {
  const list = filtered();
  if (!list.length) return;
  const cur = list.findIndex((r) => r._i === state.sel);
  const nxt = cur < 0 ? 0 : Math.min(list.length - 1, Math.max(0, cur + d));
  const target = list[nxt];
  const page = Math.floor(nxt / state.pageSize) + 1;
  if (page !== state.page) {
    state.page = page;
    render();
  }
  selectRow(target._i);
}

/* ---------------- 机翻 ---------------- */

/* 清空译文必须同步到后端。后端 self.rows 还留着旧译文时，下一轮机翻会把
   那些行当成「本来就有译文」而跳过，表现就是「清空了反而翻不动」。
   这里用一条串行链，保证「清空」先落地、再开始查未填，避免两个 RPC 抢跑。 */
let _transSync = Promise.resolve();
function syncClearedRows(indices) {
  const idx = (indices || []).map((n) => Number(n))
    .filter((n) => Number.isInteger(n));
  if (!idx.length) return _transSync;
  _transSync = _transSync
    .then(() => pywebview.api.clear_row_translations(idx))
    .catch(() => null);   // 后端没响应也不能让这条链断掉，否则后续机翻卡死
  return _transSync;
}

/* force=true 是「重新翻译」：手上已有的译文不算数，后端也不许查基线。
   普通机翻要跳过已填的行，重翻则相反——要的正是把那几行重新发出去。 */
async function translateRows(indices, force) {
  if (state.busy) return;
  await _transSync;   // 刚清空的行要先落到后端，否则会被当成「已填」跳过
  const skipIndices = state.raw
    .filter((r) => r.skip)
    .map((r) => Number(r._i))
    .filter((n) => Number.isInteger(n));
  let targetIndices = null;
  let candidates = [];
  if (indices === null || indices === undefined) {
    candidates = state.raw.filter((r) => !r.skip && !String(r.trans || "").trim());
    targetIndices = candidates.map((r) => Number(r._i))
      .filter((n) => Number.isInteger(n));
  } else {
    targetIndices = Array.from(new Set((indices || []).map((i) => Number(i))
      .filter((n) => Number.isInteger(n))));
    // 按索引建表再查：批量重翻可能一次几万行，逐条 find 是平方复杂度。
    const byIdx = new Map();
    state.raw.forEach((r) => byIdx.set(Number(r._i), r));
    candidates = targetIndices.map((i) => byIdx.get(i))
      .filter((r) => r && !r.skip && (force || !String(r.trans || "").trim()));
  }
  const skipped = state.raw.filter((r) => r.skip).length;
  if (!candidates.length) {
    toast(skipped
      ? T("没有可翻译的未填行（已跳过 %d 条）。").replace("%d", skipped)
      : T("没有「未填译文」的行。"), "err");
    return;
  }
  state.trForce = !!force;
  // 先上遮罩再发请求：批量重翻要跑几分钟，点完到 RPC 返回之间的空窗会让
  // 用户以为没反应、再点一次又撞上 busy。候选数只是预估值，后端返回真实
  // total 后再校正一次（enterTransMask 是幂等的）。
  enterTransMask(Array.isArray(targetIndices) ? targetIndices.length : 0);
  let r = null;
  try {
    r = await pywebview.api.translate(targetIndices, skipIndices, !!force);
  } catch (_) {
    r = null;
  }
  if (!r || !r.ok) {
    exitTransMask();
    if (r && r.error === "busy") return;
    toast((r && r.error) || T("操作失败：后端没有响应"), "err");
    if (r && (r.error || "").includes("API")) openSettings();
    return;
  }
  enterTransMask(r.total);
  let msg = T("机翻准备中 ... 共 %d 条").replace("%d", r.total);
  if (skipped) msg += " · " + T("已跳过 %d 条").replace("%d", skipped);
  setStatus(msg);
}

/* 重新翻译：走 force 模式，不清空旧译文。
   后端 force 会无视现有译文、也不查基线；旧值留着当垫底，模型没翻成的行
   不会变成空白。右键的单行入口与「已筛选」批量入口共用这一条路径。 */
async function retranslateRows(rows) {
  if (state.busy) return;
  const idx = [];
  (rows || []).forEach((row) => {
    if (!row || row.skip) return;          // 跳过行本来就不翻，别混进计数
    idx.push(...originalIndicesForRow(row));
  });
  const uniq = Array.from(new Set(idx.filter((n) => Number.isInteger(n))));
  if (!uniq.length) { toast(T("没有可重翻的行。"), "err"); return; }
  await translateRows(uniq, true);
}

async function retranslateRow(row) {
  if (state.busy || !row) return;
  if (row.skip) { toast(T("此行已跳过翻译"), "err"); return; }
  await retranslateRows([row]);
}

/* 「重新翻译已筛选的 N 行」：作用域就是当前视图，筛选里有什么就重翻什么。
   这是给「模型偷懒把原文原样吐回来」那批行准备的二次翻译入口。 */
async function retranslateFiltered() {
  if (state.busy) return;
  const list = filtered();
  const n = list.reduce(
    (a, r) => a + (r.skip ? 0 : originalIndicesForRow(r).length), 0);
  if (!n) { toast(T("当前筛选没有可重翻的行。"), "err"); return; }
  if (!confirm(T("重新翻译当前筛选的 %d 行？\n会覆盖这些行现有的译文，且不查官方基线与翻译记忆。")
               .replace("%d", n))) return;
  await retranslateRows(list);
}

/* ---------------- 机翻遮罩 ---------------- */

/* 机翻一跑几分钟，期间不该允许任何别的操作。用一层全遮盖解决：
   遮罩本身就是最强的互斥，不必再把每个按钮逐个禁用。
   main 加 inert 是因为遮罩挡得住鼠标，挡不住键盘——焦点若还停在输入框，
   Tab / Enter 照样能落到下层的控件上。overlay 在 main 之外，所以停止
   按钮不会被一起锁死。 */
function enterTransMask(total) {
  state.busy = true;
  const ov = $("overlay");
  if (ov) ov.classList.add("on", "tr");
  if ($("overlay-text")) $("overlay-text").textContent = T("机翻进行中…");
  $("progress").classList.add("on");
  $("bar").style.width = "0%";
  $("ov-bar").style.width = "0%";
  $("ov-count").textContent = "0 / " + Number(total || 0).toLocaleString();
  const app = document.querySelector("main.app");
  if (app) app.inert = true;
  if (document.activeElement && document.activeElement.blur) {
    document.activeElement.blur();
  }
  if ($("btn-tr-stop")) $("btn-tr-stop").focus();
  syncEditorEnabled();
}

function exitTransMask() {
  state.busy = false;
  const ov = $("overlay");
  if (ov) ov.classList.remove("on", "tr");
  const app = document.querySelector("main.app");
  if (app) app.inert = false;
  $("progress").classList.remove("on");
  $("bar").style.width = "0%";
  $("ov-bar").style.width = "0%";
  syncEditorEnabled();
}

/* ---------------- Vortex 活动映射 ---------------- */

async function detectVortexDirs() {
  const btn = $("btn-vortex-detect");
  btn.disabled = true;
  try {
    const r = await pywebview.api.vortex_detect();
    if (!r || !r.ok) {
      toast((r && r.error) || T("没探测到 Vortex 目录"), "err");
      return;
    }
    if (r.staging) $("set-staging").value = r.staging;
    if (r.downloads) $("set-downloads").value = r.downloads;
    if (r.game && !$("set-game").value.trim()) $("set-game").value = r.game;
    const bits = [];
    if (r.staging) bits.push(T("安装缓存: %s").replace("%s", r.staging));
    if (r.downloads) bits.push(T("下载包: %s").replace("%s", r.downloads));
    $("vmap-info").textContent = bits.join(" · ");
    toast(bits.length
      ? T("已填入探测到的目录，确认后点「保存」")
      : T("没探测到 Vortex 目录，请手动填写"), bits.length ? "ok" : "err");
  } finally {
    btn.disabled = false;
  }
}

async function scanVortex(openIt) {
  const btn = $("btn-vortex-scan");
  if (btn) btn.disabled = true;
  setStatus(T("正在扫描 Vortex 目录 ..."));
  try {
    const r = await pywebview.api.vortex_scan(
      $("set-downloads") ? $("set-downloads").value : "",
      $("set-staging") ? $("set-staging").value : "");
    if (!r || !r.ok) {
      toast((r && r.error) || T("扫描失败"), "err");
      setStatus(T("扫描失败"));
      return null;
    }
    state.vmap = r;
    if ($("vmap-info")) $("vmap-info").textContent = r.summary || "";
    renderVmap();
    if (openIt) openModal("modal-vmap");
    setStatus(T("活动映射: %s").replace("%s", r.summary || ""));
    return r;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function openVmap() {
  if (!state.vmap) {
    const s = state.settings || {};
    let st = (s.staging_dir || "").trim();
    let dl = (s.downloads_dir || "").trim();
    if (!st || !dl) {
      const d = await pywebview.api.vortex_detect();
      if (!d || !d.ok || (!d.staging && !d.downloads)) {
        toast(T("请先在设置里填写「安装缓存目录」与「下载包目录」"), "err");
        openSettings();
        return;
      }
      st = st || d.staging || "";
      dl = dl || d.downloads || "";
      const saved = await pywebview.api.save_settings(
        { staging_dir: st, downloads_dir: dl });
      if (saved && saved.ok) state.settings = saved.settings;
    }
    if (!(await scanVortex(false))) return;
  }
  renderVmap();
  openModal("modal-vmap");
}

function vmapView() {
  const v = state.vmap || {};
  const rows = v.rows || [];
  const f = state.vmapFilter || "all";
  const q = state.vmapSearch || "";
  return rows.filter((r) => {
    if (f !== "all" && r.state !== f) return false;
    if (!q) return true;
    const hay = [r.title, r.dir, r.archive, r.mod_id]
      .map((x) => String(x == null ? "" : x)).join(" ").toLowerCase();
    return hay.indexOf(q) >= 0;
  });
}

function renderVmap() {
  const v = state.vmap || {};
  if (!v.ok) { $("vmap-list").innerHTML = ""; return; }
  const dep = v.deployment || {};
  const st = v.stats || {};
  let env = T("安装缓存: %s（%d 个目录）")
    .replace("%s", (v.staging || {}).path || "-")
    .replace("%d", st.stagingDirs || 0);
  env += " · " + T("下载包: %s（%d 个）")
    .replace("%s", (v.downloads || {}).path || "-")
    .replace("%d", st.archives || 0);
  if (dep.ok) {
    env += " · " + T("部署清单 %d 个文件 / %d 个来源，%s 部署")
      .replace("%d", dep.files || 0)
      .replace("%d", dep.sources || 0)
      .replace("%s", dep.deployedAt || "?");
  } else {
    env += " · " + T("读不到部署清单，生效状态判断不了");
  }
  $("vmap-env").textContent = env;

  const rows = vmapView();
  state.vmapView = rows;
  const labels = v.labels || {};
  const box = $("vmap-list");
  if (!rows.length) {
    box.innerHTML = `<div class="hint" style="padding:14px">${esc(T("没有符合条件的行"))}</div>`;
    return;
  }
  box.innerHTML = rows.map((r, i) => {
    const label = T(labels[r.state] || r.state);
    const name = r.title || r.dir || "(未命名)";
    const n = r.state === "not_installed" ? "-" : String(r.files || 0);
    return `<div class="vmap-row${r.active ? "" : " off"}" data-i="${i}"
                 title="${esc(r.dir || r.path || name)}">
      <span><span class="vmap-tag ${esc(r.state)}">${esc(label)}</span></span>
      <span class="name">${esc(name)}</span>
      <span class="mid">${esc(String(r.mod_id == null ? "" : r.mod_id))}</span>
      <span class="ver${r.drift ? " up" : ""}" title="${esc(r.version || "")}">${esc(r.version || "-")}</span>
      <span class="ver${r.drift ? " up" : ""}" title="${esc(r.archive || "")}">${esc(r.archive_version || "-")}</span>
      <span class="n">${esc(n)}</span>
    </div>`;
  }).join("");

  box.querySelectorAll(".vmap-row").forEach((el) => {
    const row = rows[Number(el.dataset.i)];
    if (!row) return;
    el.onclick = () => {
      if (!row.path) { toast(T("这一条没有安装目录"), "err"); return; }
      $("path").value = row.path;
      setStatus(T("已选中: %s").replace("%s", row.title || row.dir));
    };
    el.ondblclick = () => {
      if (!row.path) return;
      $("path").value = row.path;
      closeModal("modal-vmap");
      loadMod();
    };
  });
}

function copyVmap() {
  const rows = state.vmapView || [];
  if (!rows.length) { toast(T("没有可复制的行"), "err"); return; }
  const labels = (state.vmap || {}).labels || {};
  const head = [T("状态"), T("模组"), "ID", T("已安装"), T("下载包"),
    T("文件"), T("目录")].join("\t");
  const body = rows.map((r) => [
    T(labels[r.state] || r.state), r.title || "", r.mod_id == null ? "" : r.mod_id,
    r.version || "", r.archive_version || "", r.files || 0, r.dir || ""
  ].join("\t"));
  copyToClipboard(head + "\n" + body.join("\n"), T("清单已复制"));
}

/* ---------------- 机翻结果提示 ---------------- */

function onTransReport(e) {
  // done 一般先到，这里再来一次只是兜底（exitTransMask 是幂等的）。
  exitTransMask();
  state.trFailed = (e.failed || []).map(Number).filter((n) => Number.isInteger(n));
  // 「继续翻译剩下的」要沿用同一模式：重翻失败后补翻如果退回普通模式，
  // 基线会把旧译文原样填回来，看起来像补成功了。
  state.trForce = !!e.force;
  const left = Number(e.left || 0);
  const ok = Number(e.ok || 0);
  const official = Number(e.official || 0);
  const memory = Number(e.memory || 0);
  $("tr-ok").textContent = ok.toLocaleString();
  $("tr-left").textContent = left.toLocaleString();
  $("tr-mem").textContent = (official + memory).toLocaleString();
  $("tr-total").textContent = Number(e.total || 0).toLocaleString();
  $("tr-res-title").textContent = left ? T("翻译未完成") : T("翻译结果");

  const reasons = (e.reasons || []).filter(Boolean);
  const box = $("tr-reasons");
  if (!left && !reasons.length) {
    box.innerHTML = `<div class="tr-reason ok">${esc(T("全部翻完了，没有失败。"))}</div>`;
  } else {
    box.innerHTML = reasons
      .map((r) => `<div class="tr-reason">${esc(T(r))}</div>`).join("");
    if (!reasons.length && left) {
      box.innerHTML = `<div class="tr-reason">${esc(T("有 %d 行没有拿到译文。").replace("%d", left))}</div>`;
    }
  }
  // 把「基线/记忆」那一块拆开讲清楚：官方给了多少、记忆给了多少。
  const srcBits = [];
  if (official) srcBits.push(T("官方基线 %d 条").replace("%d", official));
  if (memory) srcBits.push(T("翻译记忆 %d 条").replace("%d", memory));
  let tip = left
    ? T("点「继续翻译剩下的」只补这 %d 行，已经翻好的不会重来。").replace("%d", left)
    : "";
  if (srcBits.length) tip = (tip ? tip + " " : "") + T("其中 %s。").replace("%s", srcBits.join("、"));
  // 同文去重：同一句原文只发给模型一次，其余重复行直接复用同一份译文。
  const dedup = Number(e.dedup || 0);
  if (dedup) {
    tip = (tip ? tip + " " : "") + T("同一句原文只翻一次，重复的 %d 行直接复用。")
      .replace("%d", dedup);
  }
  $("tr-tip").textContent = tip;
  $("btn-tr-continue").style.display = left ? "" : "none";
  // 只在没翻完的时候弹最上层提示；全成功就走原来的状态栏提示
  if (left || e.stopped) openModal("modal-tr-result");
}

async function continueTranslate() {
  const idx = (state.trFailed || []).slice();
  closeModal("modal-tr-result");
  if (!idx.length) { render(); return; }
  if (state.busy) return;
  const skipIndices = state.raw.filter((r) => r.skip)
    .map((r) => Number(r._i)).filter((n) => Number.isInteger(n));
  const r = await pywebview.api.translate(idx, skipIndices, !!state.trForce);
  if (!r || !r.ok) {
    if (r && r.error === "busy") return;
    toast((r && r.error) || "error", "err");
    return;
  }
  state.trFailed = [];
  enterTransMask(r.total);
  setStatus(T("重试剩下的 %d 条 ...").replace("%d", r.total));
}

function onEvt(e) {
  if (!e) return;
  if (e.type === "trans-report") { onTransReport(e); return; }
  if (e.type === "loaded") { onLoaded(e); return; }
  if (e.type === "applied") { onApplied(e); return; }
  if (e.type === "undone") { onUndone(e); return; }
  if (e.type === "reverted") { onReverted(e); return; }
  if (e.type === "official-built") { onOfficialBuilt(e); return; }
  if (e.type === "mb-scan") { onMbScan(e); return; }
  if (e.type === "mb-scanned") { onMbScanned(e); return; }
  if (e.type === "mb-start") { onMbStart(e); return; }
  if (e.type === "mb-item") { onMbItem(e); return; }
  if (e.type === "mb-done") { onMbDone(e); return; }
  if (e.type === "error" && (e.action === "mb" || e.action === "mb-scan")) {
    state.mbRunning = false;
    $("btn-q-cancel").style.display = "none";
    exitBatchMask();          // worker 崩了没发 mb-done，遮罩得在这里收
    syncQueueStart();
    $("q-summary").textContent = e.message || "";
    toast(e.message || "error", "err");
    return;
  }
  if (e.type === "busy") { setBusy(!!e.on, e.text); return; }
  if (e.type === "rows") {
    // 机翻结果落到表里也算「未应用的改动」（P6）：以前这些结果不算 dirty，
    // 加载新模组时整批静默丢掉，用户还以为是模型没翻。
    state.dirty = true;
    e.items.forEach((it) => {
      const origIndex = Number(it.i);
      const raw = state.raw.find((x) => Number(x._i) === origIndex);
      if (raw) {
        raw.trans = it.trans;
        if (it.src) raw.transSrc = it.src;
      }
      const row = state.merged
        ? state.rows.find((x) => (x.dups || []).some((d) => Number(d._i) === origIndex))
        : state.rows.find((x) => Number(x._i) === origIndex);
      if (!row) return;
      row.trans = it.trans;
      if (it.src) row.transSrc = it.src;
      (row.dups || []).forEach((d) => {
        if (Number(d._i) === origIndex) { d.trans = it.trans; if (it.src) d.transSrc = it.src; }
      });
      const rowIndex = Number(row._i);
      const el = $("tbody").querySelector(`.trow[data-i="${rowIndex}"] .td.trans`);
      if (el) {
        el.textContent = it.trans;
        el.title = transTitle(row);
        // 来源色条跟着更新，否则机翻结果还挂着「官方」绿条。
        el.className = transCls(row);
      }
      const tr = $("tbody").querySelector(`.trow[data-i="${rowIndex}"]`);
      if (tr) tr.classList.add("filled");
    });
    if (state.sel >= 0) {
      $("trans").value = (state.rows[state.sel] || {}).trans || "";
      _transDirty = false;   // 机翻结果回填，不是用户输入
    }
    updateStats();
  } else if (e.type === "progress") {
    $("progress").classList.add("on");
    const pct = e.total ? Math.round((e.done / e.total) * 100) : 0;
    $("bar").style.width = pct + "%";
    // 进度同时投到遮罩里：机翻时整块界面被盖住，只有遮罩上的这条看得见。
    $("ov-bar").style.width = pct + "%";
    $("ov-count").textContent =
      Number(e.done || 0).toLocaleString() + " / " + Number(e.total || 0).toLocaleString();
    // note: 批次跳过、模型预热这类一次性说明，下一批进度会把它盖掉。
    const line = e.note || T("机翻中 %d / %d ...").replace("%d", e.done).replace("%d", e.total);
    setStatus(line);
    if (e.note) $("overlay-text").textContent = e.note;
  } else if (e.type === "done") {
    exitTransMask();
    let msg = e.memory
      ? T("机翻完成: %d 条（其中 %d 条来自翻译记忆）。请逐条检查后再点「应用到模组」。")
          .replace("%d", e.translated).replace("%d", e.memory)
      : T("机翻完成: %d 条。请逐条检查后再点「应用到模组」。")
          .replace("%d", e.translated);
    setStatus(msg);
    render();
  } else if (e.type === "error") {
    // 先关遮罩：下面几个分支会提前 return，放后面就关不掉了。
    if (state.busy) exitTransMask();
    toast(e.message, "err");
    if (e.action === "undo") {
      setStatus(T("撤销失败: %s").replace("%s", e.message));
      return;
    }
    if (e.action === "revert") {
      setStatus(T("还原失败: %s").replace("%s", e.message));
      return;
    }
    // 应用失败与机翻失败是两码事：以前一律按「机翻失败」报，状态栏文字
    // 会把用户指到错误的方向。
    if (e.action === "apply") {
      setStatus(T("应用失败: %s").replace("%s", e.message));
      return;
    }
    setStatus(T("机翻失败: %s").replace("%s", e.message));
  }
}
window.__evt = onEvt;

/* ---------------- 批量（单模组功能） ---------------- */

/* 批量/扫描的事件走轮询而不是后端推过来。
 *
 * 走过的两个坑：最初后端在批处理线程里直接调 window.evaluate_js，那是跨线程
 * 碰 WebView2 的 UI 线程，连着几十个 mod 就 0xc0000005；改成 pywebview 的
 * batch_poll 之后，桥每调一次仍要新开线程再 evaluate_js 回写（util.py 的
 * js_bridge_call），跑到第 54 个 mod 又崩了。
 * 现在后端把事件放进 deque，由一个只读的本机 HTTP 端口（boot 里下发的
 * pollUrl）供这里 fetch 取走——整条路径不经过 COM。fetch 起不来时退回桥。
 */
let _mbPollTimer = null;
let _mbPollBusy = false;
let _mbPollUrl = "";            // 后端的只读事件端口，空串表示退回桥
let _mbPollFails = 0;           // 连续失败次数，用来判断批量是不是卡死了

function showStall(fails) {
  const el = $("q-stall");
  if (!el) return;
  const sec = Math.round(fails * 0.2);
  if (sec < 3) {
    el.style.display = "none";
    el.textContent = "";
    return;
  }
  el.style.display = "";
  el.textContent = "事件通道已经 " + sec + " 秒没有响应，批量可能卡在某个模组上。"
    + "可以关掉窗口重开，再点「开始批量翻译」——已完成的模组会自动跳过。";
}

function startMbPoll() {
  if (_mbPollTimer) return;
  _mbPollFails = 0;
  showStall(0);
  _mbPollTimer = setInterval(pollMbEvents, 200);
}

function stopMbPoll() {
  if (_mbPollTimer) {
    clearInterval(_mbPollTimer);
    _mbPollTimer = null;
  }
}

async function pollMbEvents() {
  if (_mbPollBusy) return;      // 上一次还没回来，别叠加请求
  _mbPollBusy = true;
  try {
    let r;
    if (_mbPollUrl) {
      const resp = await fetch(_mbPollUrl, { cache: "no-store" });
      if (!resp.ok) {
        // 端口还在但拒绝请求（token 变了 / 路径不对）：这条通道没救了，退回桥。
        _mbPollUrl = "";
        throw new Error("bus " + resp.status);
      }
      r = await resp.json();
    } else {
      r = await pywebview.api.batch_poll();
    }
    _mbPollFails = 0;
    showStall(0);
    if (!r || !r.ok) { stopMbPoll(); return; }
    const evs = r.events || [];
    evs.forEach(onEvt);
    // 队列空了且后端也没在跑 → 收工。扫描中 running 也算 true，
    // 否则预扫一开始就会被这里停掉。
    if (!evs.length && !r.running) stopMbPoll();
  } catch (_) {
    _mbPollFails++;
    // 后端卡在某个模组里时 GIL 被占死，fetch 会一直超时。这时**不能**退回桥：
    // 桥每调一次都要新开线程再 evaluate_js，只会把坑挖得更深（v2.42.4 实测
    // 堆到 215 个线程）。原地重试，通道恢复后照常接着取事件；60 秒仍不通
    // 就停轮询别再空转，提示留在面板上。
    if (_mbPollFails >= 300 || !_mbPollUrl) stopMbPoll();
    showStall(_mbPollFails);
  } finally {
    _mbPollBusy = false;
  }
}

function openQueue() {
  if (state.mbRunning) { showBatchPanel(); return; }  // 跑着的时候遮罩在最上层
  openModal("modal-queue");
  refreshQueueInfo();
  if (state.mbItems.length) renderQueue();
  startMbPoll();   // 面板一打开就开始取事件：后面可能已经有一轮在跑
}

/* 批量遮罩：和机翻同一个思路，跑的时候整块界面盖住，停止入口在遮罩里。
 *
 * 遮罩永远在最上层（style.css 里 .overlay 的 z-index 是 400，比 .mask.top 的
 * 300 还高）。批量面板本身是 .mask，跑起来会被遮罩盖住——要看列表就点遮罩上
 * 的「显示面板」，遮罩临时收起；面板一关，遮罩立刻回来接着挡。
 */
function enterBatchMask() {
  const ov = $("overlay");
  if (!ov) return;
  ov.classList.add("on", "batch");
  const t = $("overlay-text");
  if (t) t.textContent = T("批量翻译进行中…");
  const cnt = $("ov-count");
  if (cnt && !state.mbIndex) cnt.textContent = "0 / " + Number(state.mbTotal || 0);
  const st = $("btn-tr-stop");
  if (st) { st.dataset.mode = "batch"; st.textContent = T("停止批量"); st.disabled = false; }
  if ($("btn-ov-panel")) $("btn-ov-panel").style.display = "";
  const app = document.querySelector("main.app");
  if (app) app.inert = true;
  state.busy = true;
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
}

function exitBatchMask() {
  const ov = $("overlay");
  if (ov) ov.classList.remove("on", "batch");
  const st = $("btn-tr-stop");
  if (st) { delete st.dataset.mode; st.textContent = T("停止机翻"); st.disabled = false; }
  if ($("btn-ov-panel")) $("btn-ov-panel").style.display = "none";
  const app = document.querySelector("main.app");
  if (app) app.inert = false;
  state.busy = false;
}

/* 遮罩上的「显示面板」：先收起遮罩再看列表。面板本身是全屏 .mask，收起期间
 * 依旧挡得住主界面；面板关掉时再把遮罩放回去。 */
function showBatchPanel() {
  const ov = $("overlay");
  if (ov) ov.classList.remove("on");
  openModal("modal-queue");
}

async function pickMbOutDir() {
  const p = await pywebview.api.pick_folder();
  if (p) { $("q-out").value = p; syncQueueStart(); }
}

/* 输出目录为空 = 不允许跑。按钮状态跟着输入框走，避免点了才报错。 */
function syncQueueStart() {
  const btn = $("btn-q-start");
  if (!btn) return;
  const hasOut = !!($("q-out").value || "").trim();
  if (state.mbRunning) { btn.disabled = true; return; }
  btn.disabled = !hasOut;
  btn.title = hasOut ? "" : T("请先设置批量输出目录");
}

/* 部署开关只在第一次打开面板时按设置初始化，之后由用户自己按。 */
function initDeployBoxes(r) {
  const dv = $("q-deploy"), dry = $("q-deploy-dry");
  if (!dv || !dry) return;
  if (!state.mbDeployInit) {
    state.mbDeployInit = true;
    const s = state.settings || {};
    dv.checked = (r && r.deploy != null) ? !!r.deploy : !!s.batch_deploy;
    dry.checked = (r && r.deployDry != null) ? !!r.deployDry
      : (s.batch_deploy_dry == null ? true : !!s.batch_deploy_dry);
    dv.onchange = () => { syncDeployBoxes(); };
  }
  syncDeployBoxes();
}

/* 「隐藏无需翻译」开关：只在第一次打开面板时按设置初始化，之后不随扫描重置。 */
function initTodoFilter(r) {
  const box = $("q-hide-todo");
  if (!box) return;
  if (!state.mbHideTodoInit) {
    state.mbHideTodoInit = true;
    const s = state.settings || {};
    state.mbHideTodo = (r && r.hideTodo != null) ? !!r.hideTodo
      : (s.batch_hide_todo == null ? true : !!s.batch_hide_todo);
    box.checked = state.mbHideTodo;
    box.onchange = async () => {
      state.mbHideTodo = !!box.checked;
      renderQueue();
      try {
        const saved = await pywebview.api.save_settings({
          batch_hide_todo: state.mbHideTodo,
        });
        if (saved && saved.ok) state.settings = saved.settings;
      } catch (_) {}
    };
  }
  box.checked = state.mbHideTodo;
}

function syncDeployBoxes() {
  const dv = $("q-deploy"), dry = $("q-deploy-dry");
  if (!dv || !dry) return;
  dry.disabled = !dv.checked;
  dry.parentElement.style.opacity = dv.checked ? "" : ".5";
}

async function refreshQueueInfo() {
  try {
    const r = await pywebview.api.batch_info();
    if (!r || !r.ok) return;
    if (!$("q-staging").value) $("q-staging").value = r.staging || "";
    if (!$("q-out").value) $("q-out").value = r.outRoot || (state.settings || {}).batch_out_dir || "";
    initDeployBoxes(r);
    initTodoFilter(r);
    syncQueueStart();
    if (r.running) {
      state.mbRunning = true;
      $("btn-q-cancel").style.display = "";
      // 面板开着的时候不要再把遮罩压上来（那会把自己盖住）
      const qm = $("modal-queue");
      if (!(qm && qm.classList.contains("on"))) enterBatchMask();
    }
  } catch (_) {}
}

async function pickMbDir() {
  const r = await pywebview.api.pick_folder();
  if (r) $("q-staging").value = r;
}

async function scanQueue() {
  if (state.mbRunning) return;
  const dir = $("q-staging").value.trim();
  $("q-list").innerHTML = "";
  state.mbItems = [];
  $("q-summary").textContent = T("正在预扫，全部跑完大约 1 分钟 ...");
  const r = await pywebview.api.batch_scan(dir);
  if (!r || !r.ok) toast((r && r.error) || "error", "err");
  else startMbPoll();
}

function mbUsable(it) {
  return !!(it && it.todo);
}

function mbSize(n) {
  if (!n) return "-";
  return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB"
    : Math.max(1, Math.round(n / 1024)) + " KB";
}

function renderQueue() {
  const box = $("q-list");
  const all = state.mbItems;
  if (!all.length) { box.innerHTML = ""; return; }
  // 「无需翻译」的目录折起来：255 个 staging 目录里有 128 个 todo == 0，
  // 一屏里近一半是噪音。读取失败的照样显示，那是要处理的信号。
  const items = state.mbHideTodo
    ? all.filter((it) => mbUsable(it) || it.error)
    : all;
  const hidden = all.length - items.length;
  const rows = items.map((it) => {
    const key = String(it.key || "");
    const done = state.mbDone[key] || {};
    const usable = mbUsable(it);
    const sel = usable && state.mbSel.has(key);
    const cls = done.state ? "done-" + done.state : "";
    const stateText = done.text || (it.error ? T("读取失败")
      : (usable ? T("待翻译") : T("无需翻译")));
    const sub = done.detail || it.error
      || (it.alreadyRows ? T("已有中文 %d 行").replace("%d", it.alreadyRows) : "");
    return `<div class="q-row ${cls}" data-key="${esc(key)}">
      <input type="checkbox" ${sel ? "checked" : ""} ${usable ? "" : "disabled"}>
      <span class="q-name" title="${esc(it.root || "")}">${esc(it.name || key)}</span>
      <span class="q-num">${usable ? Number(it.todo).toLocaleString() : "-"}</span>
      <span class="q-num">${it.alreadyRows ? Number(it.alreadyRows).toLocaleString() : "-"}</span>
      <span class="q-num">${mbSize(it.bytes)}</span>
      <span class="q-state">${esc(stateText)}</span>
      ${sub ? `<span class="q-sub">${esc(sub)}</span>` : ""}
    </div>`;
  });
  box.innerHTML = rows.join("") + (hidden
    ? `<p class="hint">${esc(T("已隐藏 %d 个无需翻译的目录（%d 个里）")
      .replace("%d", hidden).replace("%d", all.length))}</p>`
    : "");
  box.querySelectorAll(".q-row").forEach((el) => {
    el.onclick = () => {
      const key = el.dataset.key;
      const it = state.mbItems.find((x) => String(x.key) === key);
      if (!it || !mbUsable(it) || state.mbRunning) return;
      if (state.mbSel.has(key)) state.mbSel.delete(key);
      else state.mbSel.add(key);
      renderQueue();
    };
  });
}

function onMbScan(e) {
  $("q-summary").textContent = T("预扫 %d / %d：%s")
    .replace("%d", e.done || 0).replace("%d", e.total || 0)
    .replace("%s", e.name || "");
}

function onMbScanned(e) {
  state.mbItems = e.items || [];
  state.mbDone = {};
  state.mbSel = new Set(state.mbItems.filter(mbUsable).map((x) => String(x.key)));
  renderQueue();
  $("q-summary").textContent = T("共 %d 个目录，%d 个需要翻译（合计 %d 行待翻）；勾选后开始。")
    .replace("%d", e.total || 0).replace("%d", e.usable || 0)
    .replace("%d", e.rows || 0);
}

function onMbStart(e) {
  state.mbRunning = true;
  state.mbTotal = e.total || 0;
  state.mbIndex = 0;
  $("btn-q-cancel").style.display = "";
  $("q-progress").classList.add("on");
  $("q-bar").style.width = "0%";
  let msg = T("开始处理 %d 个 mod ...").replace("%d", e.total || 0);
  if (state.mbCached) {
    msg += " " + T("（跳过上次已完成的 %d 个）").replace("%d", state.mbCached);
  }
  $("q-summary").textContent = msg;
  enterBatchMask();
  $("ov-count").textContent = "0 / " + Number(state.mbTotal || 0).toLocaleString();
  syncQueueStart();
}

function onMbItem(e) {
  const key = String(e.key || "");
  if (!key) return;
  const total = e.total || 1;
  if (e.phase === "start") {
    state.mbIndex = e.index || 0;
    state.mbDone[key] = { state: "", text: T("处理中 ...") };
    renderQueue();
    $("q-bar").style.width = Math.round(((e.index || 0) / total) * 100) + "%";
    $("ov-bar").style.width = Math.round(((e.index || 0) / total) * 100) + "%";
    $("ov-count").textContent = (Number(e.index || 0) + 1).toLocaleString()
      + " / " + Number(e.total || 0).toLocaleString();
    $("q-summary").textContent = T("(%d/%d) 正在处理 %s")
      .replace("%d", (e.index || 0) + 1).replace("%d", e.total || 0)
      .replace("%s", e.name || key);
    return;
  }
  let st = "ok";
  let txt = T("完成");
  if (e.error === "cancelled") { st = "skip"; txt = T("已取消"); }
  else if (e.error) { st = "err"; txt = T("失败"); }
  else if (!e.applied) { st = "skip"; txt = T("无译文可写"); }
  const bits = [];
  bits.push(T("复制 %d 个文件").replace("%d", e.copied || 0));
  bits.push(T("待翻 %d").replace("%d", e.todo || 0));
  if (e.tm) bits.push(T("记忆命中 %d").replace("%d", e.tm));
  if (e.machine) bits.push(T("机翻 %d").replace("%d", e.machine));
  if (e.applied) bits.push(T("写入 %d").replace("%d", e.applied));
  if (e.failed) bits.push(T("失败 %d").replace("%d", e.failed));
  if (e.untranslated) bits.push(T("未译 %d").replace("%d", e.untranslated));
  if (e.deploy && e.deploy.ok) {
    bits.push((e.deploy.dry ? T("预演 ") : "")
      + T("覆盖 %d").replace("%d", e.deploy.overwritten || 0)
      + "/" + T("新增 %d").replace("%d", e.deploy.added || 0));
  } else if (e.deploy && e.deploy.error) {
    bits.push(T("部署失败: %s").replace("%s", e.deploy.error));
  }
  if (e.seconds != null) bits.push(e.seconds + "s");
  if ((e.errors || []).length) bits.push(String(e.errors[0]));
  state.mbDone[key] = { state: st, text: txt, detail: bits.join(" · ") };
  renderQueue();
  $("q-bar").style.width = Math.round(((e.done || 0) / total) * 100) + "%";
  $("ov-bar").style.width = Math.round(((e.done || 0) / total) * 100) + "%";
  $("ov-count").textContent = Number(e.done || 0).toLocaleString()
    + " / " + Number(e.total || 0).toLocaleString();
}

function onMbDone(e) {
  state.mbRunning = false;
  $("btn-q-cancel").style.display = "none";
  $("q-progress").classList.remove("on");
  $("q-bar").style.width = "0%";
  exitBatchMask();
  const rs = e.results || [];
  const ok = rs.filter((r) => r.ok && !r.error).length;
  const err = rs.filter((r) => r.error && r.error !== "cancelled").length;
  const mt = rs.reduce((a, r) => a + (r.machine || 0), 0);
  const ap = rs.reduce((a, r) => a + (r.applied || 0), 0);
  let msg = T("完成 %d 个（成功 %d，失败 %d），机翻 %d 条，写入 %d 条。")
    .replace("%d", rs.length).replace("%d", ok).replace("%d", err)
    .replace("%d", mt).replace("%d", ap);
  const dep = rs.filter((r) => r.deploy && r.deploy.ok);
  if (dep.length) {
    const ov = dep.reduce((a, r) => a + (r.deploy.overwritten || 0), 0);
    const ad = dep.reduce((a, r) => a + (r.deploy.added || 0), 0);
    msg += " " + (dep[0].deploy.dry ? T("部署预演：覆盖 %d 个文件，新增 %d 个。")
      : T("已部署：覆盖 %d 个文件，新增 %d 个。"))
      .replace("%d", ov).replace("%d", ad);
  }
  if (e.cancelled) msg = T("已取消。") + " " + msg;
  $("q-summary").textContent = msg;
  toast(msg, err ? "err" : "ok");
  refreshQueueInfo();
}

async function startQueue() {
  if (state.mbRunning) return;
  const outDir = ($("q-out").value || "").trim();
  if (!outDir) {
    toast(T("请先设置批量输出目录（留空则不进行翻译）"), "err");
    $("q-out").focus();
    return;
  }
  const keys = [...state.mbSel];
  if (!keys.length) { toast(T("请先勾选要翻译的 mod"), "err"); return; }
  const withArchive = $("q-archive").checked;
  const skipAssets = !$("q-assets").checked;
  const doDeploy = $("q-deploy").checked;
  const doDry = $("q-deploy-dry").checked;
  let msg = T("将要处理 %d 个 mod：先复制到输出目录，再翻译、写回副本。\n同名目录会被覆盖。继续？")
    .replace("%d", keys.length);
  if (doDeploy) {
    msg += "\n\n" + (doDry
      ? T("本轮只预演部署：算出会覆盖/新增哪些文件，不写原模组目录。")
      : T("本轮跑完会**原地覆盖**原模组目录里对应的文件（副本里没有的文件保持原样）。"));
  }
  if (!confirm(msg)) return;
  // 面板里改的输出目录要落盘：后端 batch_start 读的是 settings，不先保存就是
  // 「填了 A、跑的还是 B」。
  const saved = await pywebview.api.save_settings({
    batch_out_dir: outDir, batch_deploy: doDeploy, batch_deploy_dry: doDry,
  });
  if (!saved || !saved.ok) {
    toast((saved && saved.error) || T("输出目录保存失败"), "err");
    return;
  }
  state.settings = saved.settings;
  const r = await pywebview.api.batch_start(keys, withArchive, skipAssets);
  if (!r || !r.ok) {
    toast((r && r.error) || "error", "err");
    if (((r && r.error) || "").includes("API")) openSettings();
    return;
  }
  if (r.started === false) {          // 全都在上一次跑完了
    toast(r.message || "ok", "ok");
    $("q-summary").textContent = r.message || "";
    return;
  }
  state.mbRunning = true;
  state.mbCached = r.cached || 0;
  startMbPoll();
}

async function clearQueueOut() {
  if (state.mbRunning) return;
  if (!confirm(T("清空批量输出目录？里面是上一次跑出来的副本，删了就没了。"))) return;
  const r = await pywebview.api.batch_clear();
  if (!r || !r.ok) { toast((r && r.error) || "error", "err"); return; }
  state.mbDone = {};
  renderQueue();
  toast(T("已清空 %d 项").replace("%d", r.removed || 0), "ok");
}

/* ---------------- 应用 / Excel ---------------- */
async function undoLastApply() {
  if (state.busy) return;
  // 资源模式的根由后端记着（单独打开归档时 state.root 是空的），传空即可。
  const root = state.resMode ? "" : (state.root || state.modPath);
  if (!root && !state.resMode) { toast(T("请先选择有效的模组目录"), "err"); return; }
  let st = null;
  try { st = await pywebview.api.undo_status(root); } catch (_) {}
  if (!st || !st.count || !st.last) {
    toast(T("没有可撤销的应用记录"), "err");
    return;
  }
  const when = st.last.created_at || "";
  if (!confirm(T("撤销最近一次应用到模组？\n时间: %s\n将恢复 %d 个文件，本次新建的目标文件会被删除。")
      .replace("%s", when).replace("%d", st.last.files || 0))) return;
  setBusy(true, T("正在撤销上次应用 ..."));
  const r = await pywebview.api.undo_last(root);
  if (!r.ok) {
    setBusy(false);
    if (r.error !== "busy") toast(r.error || "error", "err");
  }
}

function onUndone(r) {
  setBusy(false);
  const n = r.files || ((r.restored || []).length + (r.deleted || []).length);
  toast(T("已撤销上次应用，恢复 %d 个文件。").replace("%d", n), "ok");
  setStatus(T("已撤销上次应用，恢复 %d 个文件。Vortex 用户请重新 Deploy。").replace("%d", n));
  // 产物已经删掉了，「安装到游戏」和「撤销」都不该再亮着。
  state.revertUndo = false;
  if (state.resMode) { state.resResult = null; applyModeUi(); }
}

/* ---------------- 还原翻译 ----------------
 *
 * 撤销退一步，还原退到底：所有被本工具碰过的文件按下载包里的原件恢复，
 * 本工具新建的文件删掉。范围只到「事务记录里的路径」，绝不拿包去覆盖整个
 * 模组目录——用户自己改的、别的工具改的文件必须原样留着。
 */
async function revertMod() {
  if (state.busy) return;
  const root = state.resMode ? "" : (state.root || state.modPath);
  if (!root && !state.resMode) {
    toast(T("请先选择有效的模组目录"), "err");
    return;
  }
  let plan = null;
  setStatus(T("正在核对可还原的文件 ..."));
  try { plan = await pywebview.api.revert_plan(root); } catch (_) { plan = null; }
  if (!plan) { toast(T("后端没有响应"), "err"); return; }
  if (!plan.ok) {
    const why = plan.error || T("没有可还原的内容");
    toast(why, "err");
    setStatus(why);
    return;
  }
  const src = plan.source === "package"
    ? T("来源：下载包 %s").replace("%s",
        (plan.packages || []).slice(0, 2).map((p) => p.name).join("、"))
    : plan.source === "snapshot"
      ? T("来源：本地快照（没找到可用的下载包）")
      : T("来源：无（只删除本工具新建的文件）");
  const bits = [T("将恢复 %d 个文件").replace("%d", (plan.restore || []).length)];
  if ((plan.remove || []).length) {
    bits.push(T("删除 %d 个新建文件").replace("%d", plan.remove.length));
  }
  if ((plan.skip || []).length) {
    bits.push(T("跳过 %d 个（包里没有）").replace("%d", plan.skip.length));
  }
  const warn = (plan.warnings || []).slice(0, 3).join("\n");
  let ask = T("把这个模组还原成未翻译前的样子？") + "\n\n" + src + "\n" + bits.join(" · ");
  if (warn) ask += "\n\n" + warn;
  ask += "\n\n" + T("还原后会先拍一张快照，仍可用「撤销上次应用」退回还原前的状态。");
  if (!confirm(ask)) return;
  setBusy(true, T("正在还原模组文件 ..."));
  const r = await pywebview.api.revert_mod(root);
  if (!r.ok) {
    setBusy(false);
    if (r.error !== "busy") toast(r.error || "error", "err");
  }
}

function onReverted(r) {
  setBusy(false);
  const n = r.files || 0;
  const errs = r.errors || [];
  if (r.ok) {
    toast(T("已还原 %d 个文件。").replace("%d", n), "ok");
    setStatus(T("已还原 %d 个文件，模组回到翻译前的状态。Vortex 用户请重新 Deploy 后再启动游戏。")
      .replace("%d", n));
  } else {
    toast(T("还原完成，但有 %d 个文件没成功。").replace("%d", errs.length), "err");
    setStatus(T("还原未完全成功：恢复 %d 个、删除 %d 个、失败 %d 个。")
      .replace("%d", (r.restored || []).length)
      .replace("%d", (r.removed || []).length)
      .replace("%d", errs.length));
    if (errs.length) {
      showApplyErrors({ ok: n, fail: errs.length, failedTotal: errs.length,
                        errors: errs.map((e) => (e.path || "") + " — " + (e.reason || "")),
                        failed: [] });
    }
  }
  // 表格里还是还原前那份译文；模组文件已经改回去了，别让人以为还能直接再应用。
  state.revertUndo = !!r.undoId;
  if (state.resMode) { state.resResult = null; applyModeUi(); }
  $("btn-l10n-install").style.display = "none";
}

async function applyAll() {
  if (state.resMode) return applyResource();
  const root = state.root || state.modPath;
  const items = [];
  expandRows(state.rows).forEach((r) => {
    if (!r.trans || r.trans === r.orig || r.skip) return;
    // entryId 必须带上：内容模式下混进来的归档行，组包时是按 (file, entryId)
    // 定位条目的。以前这里漏了它，资源层整批对不上号，点「应用」就报
    // 「没有可提交的译文」——看上去像是归档读取失败，其实是提交时丢了主键。
    // ctx 是语言包的条目名(如 BeginCombo_LookAt)。写回 lua/reds 语言包时
    // 必须用它定位 —— 目标语言文件与源文件的行号、条目数都不一样, 按行号
    // 写会把 A 的译文写进 B。以前这里漏了它, 应用后整张表错位。
    items.push({ root, file: r.file, line: r.line, orig: r.orig, trans: r.trans,
                 target: r.target || "", kind: r.kind || "", key: r.key || "",
                 ctx: r.ctx || "",
                 entryId: r.entryId || "", transSrc: r.transSrc || "" });
  });
  if (!items.length) { toast(T("还没有可应用的译文。"), "err"); return; }
  // 混合模组：归档里的文案要出新 mod，明文里的文案要回写文件，一次提交两件事。
  const resN = items.filter((x) => x.kind === "resource").length;
  const ask = resN
    ? T("将提交 %d 条译文：%d 条打包成新的翻译 mod，其余写回源文件。\n原 mod 不会被修改。")
        .replace("%d", items.length).replace("%d", resN)
    : T("将把 %d 条译文写回模组源文件 %s\n(自动创建 .bak 备份，可随时恢复)。")
        .replace("%d", items.length).replace("%s", state.root || state.modPath);
  if (!confirm(ask)) return;
  setBusy(true, T("正在写回模组文件 ..."));
  let r = await pywebview.api.apply(items);
  if (r && r.ok && r.started === false && r.pending) {
    // 同批文件里还有没译的行：先说清楚，再决定要不要只写这一半
    setBusy(false);
    const files = (r.pendingFiles || []).join("\n");
    const ok = confirm(T("这些文件里还有 %d 条没译的行，本次只会写回已译的 %d 条。\n\n%s\n\n继续写回吗？")
      .replace("%d", r.pending).replace("%d", items.length).replace("%s", files));
    if (!ok) return;
    setBusy(true, T("正在写回模组文件 ..."));
    r = await pywebview.api.apply(items, true);
  }
  if (!r.ok) {
    setBusy(false);
    if (r.error !== "busy") toast(r.error || "error", "err");
  }
}

function onApplied(r) {
  setBusy(false);
  // 写回成功：内存里的改动已经落盘，后面的「加载」不该再拦一次（P6）。
  // 失败的不清 —— 那些行还在表里等着重试。
  if (!r.fail) state.dirty = false;
  if (r.mode === "resource") { onAppliedResource(r); return; }
  // 混合批次：归档层出的是新 mod，源码层写的是原文件，两本账分开报。
  if (r.mode === "mixed" && r.layers) {
    const L = r.layers, bits = [];
    if (L.resource) bits.push(T("归档 %d 条").replace("%d", L.resource.ok || 0));
    if (L.source) bits.push(T("源码 %d 条").replace("%d", L.source.ok || 0));
    if (bits.length) {
      toast(bits.join(" · "), r.fail ? "err" : "ok");
      setStatus(bits.join(" · "));
    }
    const ar = (L.resource || {}).archive;
    if (ar) {
      toast(T("翻译物已生成：%s（在输出目录，未安装）").replace("%s", baseName(ar)), "ok");
    }
  }
  if (r.created && r.created.length) {
    toast(T("已生成 %d 个语言包文件").replace("%d", r.created.length), "ok");
  }
  if (r.providers && r.providers.length) {
    toast(T("已自动注册 %d 个语言加载入口").replace("%d", r.providers.length), "ok");
  }
  if (r.fail) {
    toast(T("应用完成: 成功 %d 条, 失败 %d 条。").replace("%d", r.ok).replace("%d", r.fail), "err");
    setStatus(T("应用完成: 成功 %d 条, 失败 %d 条。").replace("%d", r.ok).replace("%d", r.fail));
    showApplyErrors(r);
  } else {
    // 候选 > 写入时把差额报出来：不然「少了 5 条」只能靠用户自己数
    const cand = Number(r.candidates || 0);
    if (cand > Number(r.ok || 0)) {
      const line = T("候选 %d 条 · 写入 %d 条 · 跳过 %d 条")
        .replace("%d", cand).replace("%d", r.ok).replace("%d", cand - r.ok);
      toast(line, "warn");
      setStatus(line);
    } else {
      toast(T("%d 条译文已写回模组。\nVortex 用户请重新 Deploy 后启动游戏。").replace("%d", r.ok), "ok");
      setStatus(T("应用完成: 成功 %d 条, 失败 %d 条。").replace("%d", r.ok).replace("%d", 0));
    }
  }
}

/* ---------------- 资源模式：生成 + 安装 ---------------- */

async function applyResource() {
  const items = state.rows
    .filter((r) => r.trans && r.trans !== r.orig && !r.skip)
    .map((r) => ({ kind: "resource", file: r.file, entryId: r.entryId,
                   orig: r.orig, trans: r.trans, transSrc: r.transSrc || "" }));
  if (!items.length) { toast(T("还没有可提交的译文。"), "err"); return; }
  if (!confirm(T("将把 %d 条译文打包成一个新的翻译 mod。\n产物写到该 mod 的 .archive 旁边（原归档不会被修改）。")
      .replace("%d", items.length))) return;
  setBusy(true, T("正在打包翻译 mod ..."));
  const r = await pywebview.api.apply(items);
  if (!r.ok && r.error && r.error !== "busy") {
    setBusy(false);
    toast(r.error, "err");
  }
}

function onAppliedResource(r) {
  state.resResult = r;
  const done = r.ok || 0;
  const rep = r.report || {};
  const warns = (r.warnings || []).concat(rep.warnings || []);
  const errs = (r.errors || []).filter(Boolean);

  // 一条都没打包出来（会话丢了 / 没有可提交的译文 / 组包失败）：直接报错。
  // 以前会走下面的成功分支，弹出「已生成翻译 mod：0 条译文 / 0 个资源」，
  // 把失败说成了成功。
  if (!done && errs.length) {
    setStatus(T("应用失败: %s").replace("%s", errs[0]));
    toast(errs[0], "err");
    showApplyErrors({ ok: 0, fail: r.fail || 0,
                      failedTotal: errs.length, errors: errs, failed: [] });
    return;
  }

  // 内容与归档里已有的完全一致：没重建，也不该让人以为重新打包了一遍。
  if (r.unchanged) {
    setStatus(T("%d 条译文与归档里已有的内容一致，没有重新打包。")
      .replace("%d", done));
    toast(T("%d 条译文与归档里已有的内容一致，没有重新打包。").replace("%d", done), "ok");
    return;
  }

  if (r.archive) {
    setStatus(T("已生成 %s / %s → %s")
      .replace("%s", baseName(r.archive))
      .replace("%s", baseName(r.xl || ""))
      .replace("%s", r.outdir || ""));
  }
  if (rep.ok === false) {
    toast(T("产物校验未通过，请查看明细"), "err");
    showApplyErrors({
      ok: done, fail: r.fail || 0,
      failedTotal: (rep.problems || []).length,
      errors: rep.problems || [], failed: []
    });
  } else {
    const n = (r.resources || []).length;
    let msg = T("已生成翻译 mod：%d 条译文 / %d 个资源")
      .replace("%d", done).replace("%d", n);
    if (r.memorySaved) {
      // 资源模式以前不写记忆，同一个 mod 再开一次还得重翻——这里让用户看得见。
      msg += " · " + T("已记入翻译记忆 %d 条").replace("%d", r.memorySaved);
    }
    toast(msg, "ok");
    // 产物就落在 mod 目录里：Vortex 管的模组要重新 Deploy 才能进游戏。
    if (r.outdir) {
      setStatus(T("已输出到 %s").replace("%s", r.outdir)
        + " · " + T("Vortex 用户请重新 Deploy 后启动游戏。"));
    }
  }
  warns.slice(0, 3).forEach((w) => toast(w, "err"));
  $("btn-l10n-install").style.display = "";
  applyModeUi();
}

/* ---------------- 官方双语语料 ---------------- */

async function refreshOfficialInfo() {
  const el = $("official-info");
  if (!el) return;
  let st = null;
  try { st = await pywebview.api.baseline_info(); } catch (_) { st = null; }
  if (!st || !st.exists) {
    el.textContent = T("尚未构建（约 13 万对，1 分钟内完成）");
    return;
  }
  let line = T("%s 对 · %s 构建")
    .replace("%s", st.total || 0)
    .replace("%s", st.built_at || "?");
  if (!st.usable) {
    line += " · " + T("当前目标语言不套用：%s").replace("%s", T(st.reason));
  } else if (!st.enabled) {
    line += " · " + T("已关闭");
  } else {
    line += " · " + T("作为基线优先套用");
  }
  el.textContent = line;
}

function onOfficialBuilt(e) {
  const m = e.meta || {};
  const info = $("official-info");
  if (info) {
    info.textContent = T("%s 对 · %s 构建")
      .replace("%s", m.pairs || 0).replace("%s", m.built_at || "?");
  }
  toast(T("官方语料已就绪：%d 对（onscreen %d / 字幕 %d）")
    .replace("%d", Number(m.pairs || 0))
    .replace("%d", Number(m.onscreen || 0))
    .replace("%d", Number(m.subtitle || 0)), "ok");
  setStatus(T("官方语料已就绪，重新打开 mod 即可自动套用"));
}

function showApplyErrors(r) {
  state.applyErrors = Array.isArray(r.failed) ? r.failed : [];
  state.applyErrorOk = r.ok || 0;
  state.applyErrorTotal = r.failedTotal || r.fail || state.applyErrors.length;
  state.applyErrorExtra = (r.ok || r.fail) ? "" : (r.errors && r.errors.length
    ? `<pre class="report">${esc(r.errors.join("\n"))}</pre>` : "");
  renderApplyErrorList();
  openModal("modal-apply-errors");
}

function renderApplyErrorList() {
  const list = state.applyErrors || [];
  const total = state.applyErrorTotal || list.length;
  $("apply-errors-summary").textContent =
    T("成功 %d 条，失败 %d 条；以下为失败明细（显示 %d / %d 条）。")
      .replace("%d", state.applyErrorOk || 0).replace("%d", total)
      .replace("%d", list.length).replace("%d", total);
  const box = $("apply-errors");
  if (!list.length) {
    box.innerHTML = `<div class="hint">${esc(T("没有单条失败明细；请查看文件级错误。"))}</div>`
      + (state.applyErrorExtra || "");
    return;
  }
  box.innerHTML = list.map((x, i) => {
    // soft = 形态可疑（可能误报），给单条覆盖；hard = 写进去必坏，不给开关
    const soft = (x.level || "") === "soft";
    const acts = [];
    if (soft) {
      acts.push(`<button class="btn tiny force" data-force="${i}">${esc(T("仍要写入"))}</button>`);
    }
    acts.push(`<button class="btn tiny mark" data-mark="${i}">${esc(T("标记为代码"))}</button>`);
    return `
      <div class="apply-error">
        <div class="apply-error-title">${i + 1}. ${esc(x.file || "-")}${x.line ? ":" + esc(String(x.line)) : ""}
          <span class="lv ${soft ? "soft" : "hard"}">${esc(soft ? T("可疑·可覆盖") : T("必坏·不可写入"))}</span></div>
        <div class="apply-error-row"><span>${esc(T("原文"))}</span>${esc(x.orig || "")}</div>
        <div class="apply-error-row"><span>${esc(T("译文"))}</span>${esc(x.trans || "")}</div>
        <div class="apply-error-row reason"><span>${esc(T("原因"))}</span>${esc(T(x.reason || "") || x.reason || "")}</div>
        <div class="apply-error-acts">${acts.join("")}</div>
      </div>`;
  }).join("");
  box.querySelectorAll("button[data-force]").forEach((b) => {
    b.onclick = () => forceApplyOne(Number(b.dataset.force));
  });
  box.querySelectorAll("button[data-mark]").forEach((b) => {
    b.onclick = () => markAsCode(Number(b.dataset.mark));
  });
}

async function forceApplyOne(i) {
  const x = (state.applyErrors || [])[i];
  if (!x) return;
  const ask = T("这一条被判为「疑似代码片段」，写进去可能把语法改坏。\n\n原文：%s\n译文：%s\n\n确认要单条写入吗？")
    .replace("%s", x.orig || "").replace("%s", x.trans || "");
  if (!confirm(ask)) return;
  setBusy(true, T("正在写回模组文件 ..."));
  const r = await pywebview.api.apply_one_row({
    root: state.root || state.modPath,
    file: x.file, line: x.line, orig: x.orig, trans: x.trans,
    target: x.target || "", ctx: x.ctx || "",
  });
  if (!r.ok) { setBusy(false); if (r.error !== "busy") toast(r.error || "error", "err"); }
}

async function markAsCode(i) {
  const x = (state.applyErrors || [])[i];
  if (!x) return;
  const r = await pywebview.api.mark_as_code(x.file, x.orig);
  if (!r.ok) { toast(r.error || "error", "err"); return; }
  // 这条不用再看了，从列表里划掉；下次加载它直接不出现
  state.applyErrors.splice(i, 1);
  renderApplyErrorList();
  toast(T("已标记为代码：以后不再作为候选出现。"), "ok");
}

async function exportXlsx() {
  if (!state.rows.length) { toast(T("表格是空的，先加载模组目录。"), "err"); return; }
  const p = await pywebview.api.pick_xlsx_save(T("翻译表.xlsx"));
  if (!p) return;
  const r = await pywebview.api.export_xlsx(p, expandRows(state.rows));
  if (r.ok) toast(T("已导出: %s").replace("%s", p), "ok");
}

async function importXlsx() {
  const p = await pywebview.api.pick_xlsx_open();
  if (!p) return;
  const r = await pywebview.api.import_xlsx(p);
  if (!r.ok) { toast(T("读入失败"), "err"); return; }
  const index = {};
  state.rows.forEach((row) => {
    (index[row.file + "|" + row.line + "|" + row.orig] ||= []).push(row);
  });
  let hit = 0;
  r.items.forEach((it) => {
    const list = index[it.file + "|" + it.line + "|" + it.orig];
    if (!list) return;
    const row = list.find((x) => !x.trans) || list[0];
    if (!row) return;
    if (it.trans) { row.trans = it.trans; hit++; }
    if (typeof it.skip === "boolean") {
      row.skip = it.skip;
      (row.dups || []).forEach((d) => { d.skip = it.skip; });
      const keys = rowKeysFor(row);
      state.raw.forEach((raw) => { if (keys.has(rowKey(raw))) raw.skip = it.skip; });
    }
  });
  render();
  toast(T("已从翻译表填入 %d 条译文。").replace("%d", hit), "ok");
}

/* ---------------- 语言包 ---------------- */
async function openLocale() {
  if (!state.root) { toast(T("请先选择有效的模组目录"), "err"); return; }
  const r = await pywebview.api.locale_scan();
  if (!r.ok) { toast(r.error || "", "err"); return; }
  if (!r.files.length) { toast(T("该模组没有找到语言包文件"), "err"); return; }
  state.localeFiles = r.files;
  state.localeTargets = r.targets || [];
  const src = $("loc-src");
  src.innerHTML = '<option value="">' + T("全部语言") + "</option>" +
    r.locales.map((l) => `<option value="${l.code}">${l.label} (${l.code})</option>`).join("");
  const dst = $("loc-dst");
  dst.innerHTML = state.localeTargets.map(
    (t) => `<option value="${t.code}">${t.label} (${t.code})</option>`).join("");
  dst.value = "zh-CN";
  const hasEn = r.locales.some((l) => l.code === "en-US");
  src.value = hasEn ? "en-US" : (r.locales.length ? r.locales[0].code : "");
  renderLocaleList();
  openModal("modal-locale");
}

function renderLocaleList() {
  const want = $("loc-src").value;
  const files = state.localeFiles.filter((f) => !want || f.locale === want);
  $("loc-list").innerHTML = files.map((f, i) => `
    <label class="loc-row">
      <input type="checkbox" data-rel="${f.rel}" ${i === 0 ? "checked" : ""}>
      <span class="loc-file" title="${esc(f.rel)}">${esc(f.rel)}</span>
      <span class="loc-lang">${esc(f.locale || "-")}</span>
      <span>${esc(f.format.toUpperCase())}</span>
      <span class="loc-cnt">${f.count}</span>
    </label>`).join("");
  updateLocCount();
  $("loc-list").querySelectorAll("input").forEach((c) => { c.onchange = updateLocCount; });
}

function updateLocCount() {
  const n = $("loc-list").querySelectorAll("input:checked").length;
  $("loc-count").textContent = T("已选 %d 个文件").replace("%d", n);
}

async function loadLocale() {
  const boxes = Array.from($("loc-list").querySelectorAll("input:checked"));
  if (!boxes.length) { toast(T("请先选中语言包文件"), "err"); return; }
  const skip = state.localeFiles.filter((f) => !$("loc-src").value || f.locale === $("loc-src").value);
  const picked = boxes.map((b) => skip.findIndex((f) => f.rel === b.dataset.rel));
  const target = $("loc-dst").value;
  const r = await pywebview.api.load_locale(picked, target);
  if (!r.ok) { toast(r.error || "", "err"); return; }
  // 与 onLoaded 同一套：先切断编辑，再换数据，最后把筛选和搜索复位。
  state.sel = -1;
  _transDirty = false;
  if ($("trans")) $("trans").value = "";
  state.raw = (r.rows || []).map((x, i) => ({ ...x, _i: i }));
  state.rows = state.raw;
  state.merged = false;
  state.hidden = new Set();
  state.onlyFile = null;
  state.transMode = "";
  state.filter = "全部";
  state.search = "";
  $("filter").value = "全部";
  $("search").value = "";
  updateScopeInfo();
  render();
  syncEditorEnabled();
  $("empty").textContent = (!state.rows.length && r.skippedTarget)
    ? T("没有可提取的原文（已跳过 %d 条目标语言文本）").replace("%d", r.skippedTarget)
    : T("未加载 — 先选模组目录点「加载 / 提取」");
  $("tbody").scrollTop = 0;
  closeModal("modal-locale");
  let msg = T("已加载 %d 段文本 / %d 个文件")
    .replace("%d", state.rows.length)
    .replace("%d", Object.keys(groupFiles()).length);
  if (r.mode === "locale") {
    msg = T("语言包模式: %s → %s").replace("%s", r.source).replace("%s", r.target)
      + " · " + msg;
  } else if (state.opts.localeFirst) {
    msg = T("内容模式（模组无语言包）") + " · " + msg;
  }
  msg += " · " + T("目标语言文件: %s").replace("%s", target);
  if (r.skippedTarget) {
    const doneN = state.rows.filter((x) => x.already).length;
    msg += doneN
      ? " · " + T("已跳过 %d 条目标语言文本（其中 %d 条已译，已列在表里）")
          .replace("%d", r.skippedTarget).replace("%d", doneN)
      : " · " + T("已跳过 %d 条目标语言文本").replace("%d", r.skippedTarget);
  }
  if (r.meta && r.meta.locale_pairs) {
    msg += " · " + T("多语言替换表 %d 项，仅提取目标语言 %d 项")
      .replace("%d", r.meta.locale_pairs)
      .replace("%d", r.meta.locale_pairs_used || 0);
  }
  const blLine2 = baselineLine(r);
  if (blLine2) msg += " · " + blLine2;
  else if (r.tmHits) msg += " " + T("记忆命中 %d 条").replace("%d", r.tmHits);
  if (r.parseErrors) {
    msg += " · " + T("%d 个文件解析失败（已跳过）").replace("%d", r.parseErrors);
  }
  setStatus(msg);
}

/* ---------------- 弹窗 ---------------- */
function openModal(id) {
  $(id).classList.add("on");
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
}
function closeModal(id) { $(id).classList.remove("on"); }

function openSettings() {
  $("set-api").value = state.settings.api_base || "";
  $("set-model").value = state.settings.model || "";
  $("set-lang").value = state.lang;
  $("set-target").value = state.settings.target_lang || "简体中文";
  $("set-tm").checked = !!state.settings.use_tm;
  $("set-dark").checked = !!state.settings.dark_mode;
  $("set-game").value = state.settings.game_dir || "";
  $("set-wkit").value = state.settings.wkit_path || "";
  $("set-staging").value = state.settings.staging_dir || "";
  $("set-downloads").value = state.settings.downloads_dir || "";
  $("set-batch-out").value = state.settings.batch_out_dir || "";
  $("set-verify-active").checked = state.settings.verify_active !== false;
  $("vmap-info").textContent = state.vmap ? (state.vmap.summary || "") : "";
  $("set-src-lang").value = state.settings.res_source_lang || "en-us";
  $("set-skip-target").checked = state.settings.res_skip_target !== false;
  $("set-official-tm").checked = state.settings.use_official_tm !== false;
  $("set-official-weak").checked = state.settings.official_weak !== false;
  $("set-local").checked = state.settings.local_model_enabled !== false;
  refreshLocalModel();
  $("l10n-check-result").textContent = "";
  refreshOfficialInfo();
  $("test-result").textContent = "";
  $("cfg-path").textContent = (state.configFile || "") ? (T("配置文件: %s").replace("%s", state.configFile)) : "";
  refreshMemoryInfo();
  openModal("modal-settings");
}

async function saveSettings(opts) {
  opts = opts || {};
  const cfg = {
    api_base: $("set-api").value.trim(),
    model: $("set-model").value.trim(),
    lang: $("set-lang").value,
    target_lang: $("set-target").value,
    use_tm: $("set-tm").checked,
    dark_mode: $("set-dark").checked,
    game_dir: $("set-game").value.trim(),
    wkit_path: $("set-wkit").value.trim(),
    staging_dir: $("set-staging").value.trim(),
    downloads_dir: $("set-downloads").value.trim(),
    batch_out_dir: $("set-batch-out").value.trim(),
    verify_active: $("set-verify-active").checked,
    res_source_lang: $("set-src-lang").value.trim() || "en-us",
    res_skip_target: $("set-skip-target").checked,
    use_official_tm: $("set-official-tm").checked,
    official_weak: $("set-official-weak").checked,
    local_model_enabled: $("set-local").checked
  };
  const r = await pywebview.api.save_settings(cfg);
  if (!r.ok) { toast(r.error, "err"); return false; }
  const langChanged = cfg.lang !== state.lang;
  const dirsChanged = (cfg.staging_dir !== ((state.settings || {}).staging_dir || ""))
    || (cfg.downloads_dir !== ((state.settings || {}).downloads_dir || ""));
  state.settings = r.settings;
  if (dirsChanged) state.vmap = null;
  if (langChanged) {
    const d = await pywebview.api.boot();
    state.catalog = d.catalog; state.lang = d.lang;
    applyI18n(); fillSelects(d); render();
  } else {
    applyTheme();
  }
  if (opts.close !== false) closeModal("modal-settings");
  if (!opts.quiet) {
    toast(T("设置已保存 · 本地模型 API: %s（%s）")
          .replace("%s", r.settings.api_base || T("未设置"))
          .replace("%s", ""), "ok");
  }
  return true;
}

async function refreshLocalModel() {
  const el = $("local-state");
  if (!el) return;
  let st = null;
  try { st = await pywebview.api.local_model_status(); } catch (_) { st = null; }
  if (!st) { el.textContent = ""; return; }
  const bits = [];
  if (st.ollama) bits.push("Ollama");
  if (st.models && st.models.length) {
    bits.push(T("%d 个可用模型").replace("%d", st.models.length));
  }
  if (st.enabled === false) bits.push(T("已停用"));
  if (st.error) bits.push(String(st.error).slice(0, 60));
  el.textContent = bits.join(" · ");
  $("set-local").checked = st.enabled !== false;
}

async function refreshMemoryInfo() {
  const el = $("mem-info");
  let info = null;
  try { info = await pywebview.api.baseline_info(); } catch (_) { info = null; }
  if (info && info.memory && info.memory.total != null) {
    const m = info.memory;
    const bits = [];
    if (m.manual) bits.push(T("手改 %d").replace("%d", m.manual));
    if (m.official) bits.push(T("官方 %d").replace("%d", m.official));
    const mach = (m.machine || 0) + (m.memory || 0);
    if (mach) bits.push(T("机翻 %d").replace("%d", mach));
    el.textContent = T("翻译记忆: %d 条").replace("%d", m.total)
      + (bits.length ? "（" + bits.join(" / ") + "）" : "");
    return;
  }
  const n = await pywebview.api.memory_count();
  el.textContent = T("翻译记忆: %d 条").replace("%d", n);
}

function renderGlossary() {
  const box = $("gloss-list");
  box.innerHTML = state.glossary.map((t, i) => `
    <div class="grow-row">
      <input class="input" value="${esc(t.src)}" data-k="src" data-i="${i}">
      <input class="input" value="${esc(t.dst)}" data-k="dst" data-i="${i}">
      <input class="input" value="${esc(t.note || "")}" data-k="note" data-i="${i}">
      <button class="del" data-del="${i}">✕</button>
    </div>`).join("");
  box.querySelectorAll("input").forEach((inp) => {
    inp.oninput = () => {
      const i = Number(inp.dataset.i), k = inp.dataset.k;
      state.glossary[i][k] = inp.value;
    };
  });
  box.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = () => {
      state.glossary.splice(Number(b.dataset.del), 1);
      renderGlossary();
    };
  });
}

async function showReport() {
  const r = await pywebview.api.report();
  const lines = [];
  lines.push(T("源文件:  .reds/.xml %d 个,  .lua %d 个")
    .replace("%d", r.reds).replace("%d", r.lua));
  lines.push(T("命中文件: %d 个,  可译文本: %d 段")
    .replace("%d", r.files).replace("%d", state.rows.length));
  lines.push(T("语言包:  locales/*.json %d 个,  语言包 lua %d 个")
    .replace("%d", r.localeJson).replace("%d", r.localeLua));
  if (r.localePairs) {
    lines.push(T("多语言替换表: 识别 %d 项，仅提取目标语言 %d 项")
      .replace("%d", r.localePairs).replace("%d", r.localePairsUsed || 0));
  }
  lines.push("");
  lines.push(T("按类型:"));
  Object.entries(r.types || {}).forEach(([k, v]) => {
    lines.push("    " + T(k) + "  " + v);
  });
  const noise = (r.noise || {});
  const noiseKeys = Object.keys(noise).filter((k) => Number(noise[k]) > 0);
  if (noiseKeys.length) {
    const total = noiseKeys.reduce((a, k) => a + (Number(noise[k]) || 0), 0);
    lines.push("");
    lines.push(T("已过滤噪音 %d 条").replace("%d", total));
    noiseKeys.forEach((k) => {
      lines.push("    " + T(k) + "  " + noise[k]);
    });
  }
  $("report-text").textContent = lines.join("\n");
  openModal("modal-report");
}

/* ---------------- 状态 / Toast ---------------- */
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
