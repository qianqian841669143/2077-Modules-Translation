/* ============================================================
   dialogs.js —— 语言包、汉化包、设置、术语表、扫描报告
   2026-11-20 从 app.js 原样拆出，只搬运不改逻辑。
   ============================================================ */
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

/* ---------------- 汉化包状态 ---------------- */
function openPackStatus() {
  switchPage("pack");       // 页面切换里会刷数据
}

async function refreshPackStatus() {
  $("pack-text").textContent = T("读取中 ...");
  $("pack-tip").textContent = "";
  let s = null;
  try { s = await pywebview.api.pack_status(); } catch (_) { s = null; }
  if (!s) { $("pack-text").textContent = T("读不到汉化包信息。"); return; }
  renderPackStatus(s);
}

function tf(tpl, ...args) {
  let s = T(tpl);
  args.forEach((v) => { s = s.replace("%d", v); });
  return s;
}

function renderPackStatus(s) {
  const L = [];
  const lg = s.ledger || {};
  L.push(T("汉化包") + "   " + (s.zip || "-"));
  if (s.exists) {
    L.push(T("大小") + "     " + fmtBytes(s.size) + "   ·   " + s.mtime);
  } else {
    L.push(T("状态") + "     " + T("还不存在 — 还没点过「应用」"));
  }
  if (s.error) L.push(T("错误") + "     " + s.error);
  L.push("");
  L.push(tf("条目：共 %d 个", s.entries || 0));
  L.push("   " + [
    T("账本") + " " + (s.ledgerInZip || 0),
    T("覆盖文本") + " " + (s.textInZip || 0),
    T("归档") + " " + (s.archiveInZip || 0),
    T("运行时") + " " + (s.overrideInZip || 0),
    T("说明") + " " + (s.patchInZip || 0),
  ].join("  ·  "));
  L.push("");
  L.push(T("账本目录") + " " + (lg.dir || "-"));
  L.push("  " + tf("文件 %d 个 · 进包 %d 个", lg.files || 0, s.ledgerInZip || 0));
  L.push("  " + tf("模组 %d 个 / %d 条", lg.mods || 0, lg.entries || 0));
  L.push("  all.lua  " + fmtBytes(lg.all) + "  ·  " + (lg.mtime || "-"));
  L.push("  " + T("同步状态") + " " + (lg.inSync
    ? "✓ " + T("已同步")
    : "✗ " + T("未同步 — 点右下角「同步账本」")));
  L.push("");
  L.push(tf("覆盖记录：%d 个模组（zh_state.json）", s.packed || 0));
  if (!s.hasRuntime) {
    L.push("");
    L.push("✗ " + T("缺运行时 init.lua：包装上去也不会替换任何文本"));
  }
  $("pack-text").textContent = L.join("\n");
  $("pack-tip").textContent = lg.inSync
    ? T("账本已随包走，重装这个 zip 不会丢译文。")
    : T("账本没进包：现在重装 zip 会覆盖掉 staging 里的账本，先点「同步账本」。");
}

async function refreshPackDirTip() {
  const el = $("pack-dir-tip");
  if (!el) return;
  el.textContent = "";
  try {
    const s = await pywebview.api.pack_status();
    if (s && s.zip) el.textContent = T("当前汉化包：%s").replace("%s", s.zip);
  } catch (_) { /* 设置页不因为这个报错 */ }
}

/* 配色主题下拉：列表来自 userdata\themes\*.css，用户自己丢一个文件进去就能用 */
async function refreshThemes() {
  const sel = $("set-theme");
  if (!sel) return;
  let r = null;
  try { r = await pywebview.api.themes(); } catch (_) { r = null; }
  const list = (r && r.themes) || [{ id: "", name: "默认" }];
  const cur = (r && r.current != null) ? r.current : (state.settings.theme || "");
  sel.innerHTML = list.map((t) =>
    `<option value="${esc(t.id || "")}">${esc(T(t.name || t.id || "默认"))}</option>`).join("");
  sel.value = cur || "";
}

/* ---------------- 模组清单 ---------------- */
let _modsData = null;
let _modsFilter = "all";

function syncModsChips() {
  ["all", "done", "todo"].forEach((k) => {
    const b = $("mods-filter-" + k);
    if (b) b.classList.toggle("on", _modsFilter === k);
  });
}

async function openMods() {
  $("mods-search").value = "";
  _modsFilter = "all";
  syncModsChips();
  switchPage("manage");     // 页面切换里会拉数据
}

async function refreshMods() {
  const box = $("mods-list");
  box.innerHTML = `<p class="hint" style="padding:12px">${esc(T("读取中 ..."))}</p>`;
  let r = null;
  try { r = await pywebview.api.list_mods(); } catch (_) { r = null; }
  if (!r || !r.ok) {
    box.innerHTML = `<p class="hint" style="padding:12px">${esc(T("读不到模组清单。"))}</p>`;
    return;
  }
  _modsData = r;
  renderMods();
}

function renderMods() {
  const d = _modsData || { mods: [], summary: {} };
  const q = ($("mods-search").value || "").trim().toLowerCase();
  const rows = (d.mods || []).filter((m) => {
    const done = (m.entries || 0) > 0;
    if (_modsFilter === "done" && !done) return false;
    if (_modsFilter === "todo" && done) return false;
    if (q && !String(m.name || "").toLowerCase().includes(q)) return false;
    return true;
  });
  const box = $("mods-list");
  if (!rows.length) {
    box.innerHTML = `<p class="hint" style="padding:12px">${esc(T("没有匹配的模组。"))}</p>`;
  } else {
    box.innerHTML = rows.map((m) => {
      const done = (m.entries || 0) > 0;
      const cls = m.orphan ? "s-orphan" : (done ? "s-done" : "s-todo");
      const st = m.orphan ? T("目录已不存在") : (done ? T("已汉化") : T("未汉化"));
      return `<div class="m-row ${cls}" title="${esc(m.name || "")}">
        <span class="m-name">${esc(m.name || "")}</span>
        <span class="m-num">${done ? Number(m.entries).toLocaleString() : "-"}</span>
        <span class="m-state">${esc(st)}</span>
      </div>`;
    }).join("");
  }
  const s = d.summary || {};
  $("mods-summary").textContent =
    T("共 %d 个模组：已汉化 %d，未汉化 %d，目录缺失 %d；合计 %d 条")
      .replace("%d", s.total || 0).replace("%d", s.done || 0)
      .replace("%d", s.todo || 0).replace("%d", s.orphan || 0)
      .replace("%d", Number(s.entries || 0).toLocaleString());
}

function openSettings() {
  $("set-api").value = state.settings.api_base || "";
  $("set-model").value = state.settings.model || "";
  $("set-lang").value = state.lang;
  $("set-theme").value = state.settings.theme || "";
  refreshThemes();
  $("set-target").value = state.settings.target_lang || "简体中文";
  $("set-tm").checked = !!state.settings.use_tm;
  $("set-dark").checked = !!state.settings.dark_mode;
  $("set-no-gpu").checked = !!state.settings.webview_disable_gpu;
  $("set-game").value = state.settings.game_dir || "";
  $("set-wkit").value = state.settings.wkit_path || "";
  $("set-staging").value = state.settings.staging_dir || "";
  $("set-downloads").value = state.settings.downloads_dir || "";
  $("set-batch-out").value = state.settings.batch_out_dir || "";
  $("set-pack-dir").value = state.settings.zh_pack_dir || "";
  refreshPackDirTip();
  $("set-verify-active").checked = state.settings.verify_active !== false;
  $("vmap-info").textContent = state.vmap ? (state.vmap.summary || "") : "";
  $("set-src-lang").value = state.settings.res_source_lang || "en-us";
  $("set-skip-target").checked = state.settings.res_skip_target !== false;
  $("set-official-tm").checked = state.settings.use_official_tm !== false;
  $("set-official-weak").checked = state.settings.official_weak !== false;
  $("set-local").checked = state.settings.local_model_enabled !== false;
  $("set-ask-unload").checked = state.settings.ask_unload_model !== false;
  refreshLocalModel();
  refreshModelList();
  $("l10n-check-result").textContent = "";
  refreshOfficialInfo();
  $("test-result").textContent = "";
  $("cfg-path").textContent = (state.configFile || "") ? (T("配置文件: %s").replace("%s", state.configFile)) : "";
  refreshMemoryInfo();
  refreshCacheInfo();
  openModal("modal-settings");
}

async function saveSettings(opts) {
  opts = opts || {};
  const cfg = {
    api_base: $("set-api").value.trim(),
    model: $("set-model").value.trim(),
    lang: $("set-lang").value,
    theme: $("set-theme").value,
    target_lang: $("set-target").value,
    use_tm: $("set-tm").checked,
    dark_mode: $("set-dark").checked,
    webview_disable_gpu: $("set-no-gpu").checked,
    game_dir: $("set-game").value.trim(),
    wkit_path: $("set-wkit").value.trim(),
    staging_dir: $("set-staging").value.trim(),
    downloads_dir: $("set-downloads").value.trim(),
    batch_out_dir: $("set-batch-out").value.trim(),
    zh_pack_dir: $("set-pack-dir").value.trim(),
    verify_active: $("set-verify-active").checked,
    res_source_lang: $("set-src-lang").value.trim() || "en-us",
    res_skip_target: $("set-skip-target").checked,
    use_official_tm: $("set-official-tm").checked,
    official_weak: $("set-official-weak").checked,
    local_model_enabled: $("set-local").checked,
    ask_unload_model: $("set-ask-unload").checked
  };
  const r = await pywebview.api.save_settings(cfg);
  if (!r.ok) { toast(r.error, "err"); return false; }
  const langChanged = cfg.lang !== state.lang;
  const dirsChanged = (cfg.staging_dir !== ((state.settings || {}).staging_dir || ""))
    || (cfg.downloads_dir !== ((state.settings || {}).downloads_dir || ""));
  state.settings = r.settings;
  if (dirsChanged) state.vmap = null;
  refreshPackDirTip();
  applyThemeCss();      // 保存后按已存值重刷，取消预览留下的临时配色被纠正
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
  const be = st.backend || (st.ollama ? "ollama" : "");
  if (be === "llamacpp") bits.push("llama.cpp");
  else if (be === "ollama") bits.push("Ollama");
  if (st.models && st.models.length) {
    bits.push(T("%d 个可用模型").replace("%d", st.models.length));
  }
  // llama.cpp 的权重就是 llama-server 进程本身，没有卸载接口：说清楚，
  // 免得用户反复点「立即释放显存」以为按钮坏了。
  if (be === "llamacpp") bits.push(T("模型常驻显存，要释放请停止 llama-server"));
  if (st.enabled === false) bits.push(T("已停用"));
  if (st.error) bits.push(String(st.error).slice(0, 60));
  el.textContent = bits.join(" · ");
  $("set-local").checked = st.enabled !== false;
}

/* ---------------- 模型下拉 ----------------
 * llama.cpp 的模型 id 是磁盘路径（D:\Models\...\xxx.gguf），直接铺在下拉里
 * 太长，所以 option 的 label 只显示「文件名 · 上下文 · 量化 · 大小」，
 * value 仍保留真 id —— 请求里必须用它。
 */
function fmtCtx(n) {
  const v = Number(n) || 0;
  if (v <= 0) return "";
  if (v % (1024 * 1024) === 0) return (v / 1024 / 1024) + "M";
  if (v >= 1024 * 1024) return (v / 1024 / 1024).toFixed(1) + "M";
  return Math.round(v / 1024) + "K";
}
function modelLabel(m) {
  const id = String((m && m.id) || "");
  const bits = [id.split(/[\\/]/).pop() || id];
  const ctx = fmtCtx(m.ctx);
  if (ctx) bits.push(ctx);
  if (m.params) bits.push(m.params);
  if (m.quant) bits.push(m.quant);
  if (m.size) bits.push(fmtBytes(m.size));
  if (m.multimodal) bits.push(T("多模态"));
  return bits.join(" · ");
}
async function refreshModelList(quiet) {
  const dl = $("model-list");
  if (!dl) return;
  let r = null;
  try { r = await pywebview.api.local_model_options($("set-api").value.trim()); } catch (_) { r = null; }
  dl.innerHTML = "";
  if (!r || !r.ok) {
    if (!quiet && r && r.error) toast(String(r.error).slice(0, 80), "err");
    return;
  }
  (r.models || []).forEach((m) => {
    const o = document.createElement("option");
    o.value = String(m.id || "");
    o.label = modelLabel(m);
    dl.appendChild(o);
  });
}

/* ---------------- 缓存清理 ---------------- */
function fmtBytes(n) {
  let v = Number(n) || 0;
  if (v < 1024) return v + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let i = -1;
  do { v /= 1024; i++; } while (v >= 1024 && i < units.length - 1);
  return (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + " " + units[i];
}

async function refreshCacheInfo() {
  const box = $("cache-list");
  if (!box) return;
  $("cache-info").textContent = T("正在统计…");
  let info = null;
  try { info = await pywebview.api.cache_info(); } catch (_) { info = null; }
  if (!info || !info.ok || !info.items) {
    box.innerHTML = "";
    $("cache-info").textContent = (info && info.error) || T("读不到缓存大小");
    return;
  }
  box.innerHTML = info.items.map((it) => `
    <label class="check cache-row${it.exists ? "" : " gone"}"
           title="${esc(it.dirs && it.dirs.length ? it.dirs.join(" · ") : "-")}">
      <input type="checkbox" data-cache="${esc(it.key)}"
             data-name="${esc(T(it.label))}"${it.exists ? "" : " disabled"}>
      <span class="cache-name">${esc(T(it.label))}</span>
      <span class="cache-note">${esc(T(it.note))}</span>
      <span class="cache-size">${esc(it.exists ? fmtBytes(it.bytes) : T("已空"))}</span>
    </label>`).join("");
  $("cache-info").textContent = T("合计 %s").replace("%s", fmtBytes(info.total));
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
