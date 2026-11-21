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

/* 这个 zip 装了什么：新架构只有「内置插件 + 分片词典」，账本 / all.lua /
   init.lua 那套概念已经不存在了。 */
function renderPackStatus(s) {
  const L = [];
  L.push(T("汉化包") + "   " + (s.zip || "-"));
  if (s.exists) {
    L.push(T("大小") + "     " + fmtBytes(s.size) + "   ·   " + (s.mtime || "-"));
  } else {
    L.push(T("状态") + "     " + T("还不存在 — 还没点过「应用」"));
  }
  if (s.error) L.push(T("错误") + "     " + s.error);
  L.push("");
  L.push(tf("条目：共 %d 个", s.entries || 0));
  L.push(tf("模组 %d 个 · 分片 %d 个 · 包内文件 %d 个",
    s.mods || 0, s.shards || 0, s.files || 0));
  $("pack-text").textContent = L.join("\n");

  const box = $("pack-cards");
  if (!box) return;
  const n = (v) => Number(v || 0).toLocaleString();
  const row = (k, v) => '<div class="pk"><span>' + esc(k) +
    "</span><b>" + esc(String(v)) + "</b></div>";
  const head = (cls, name, badge, badgeCls) => '<div class="phead">' +
    '<span class="pdot ' + cls + '"></span>' +
    '<span class="pname" title="' + esc(name) + '">' + esc(name) + "</span>" +
    (badge ? '<span class="pbadge ' + badgeCls + '">' + esc(badge) + "</span>" : "") +
    "</div>";
  const of = String(s.zip || "").split(/[\\/]/).pop();
  const dirOf = String(s.zip || "").replace(/[\\/][^\\/]+$/, "");
  const cards = [];

  // 1) 包本身
  cards.push('<div class="pcard">' +
    head(s.exists ? "ok" : "wa", of || T("汉化包"),
         T(s.exists ? "已生成" : "未生成"), s.exists ? "ok" : "wa") +
    '<div class="pmeta">' + (s.exists
      ? esc(fmtBytes(s.size)) + " · " + esc(s.mtime || "-")
      : esc(T("还没点过「应用」，包还不存在"))) + "</div>" +
    '<div class="pmeta pmeta-path" title="' + esc(s.zip || "") + '">' +
    esc(dirOf || "-") + "</div>" +
    row(T("条目总数"), n(s.entries)) +
    row(T("模组"), n(s.mods)) +
    row(T("分片"), n(s.shards)) +
    row(T("包内文件"), n(s.files)) +
    "</div>");

  // 2) 分片明细：每个 .tsv 就是一个模组的词典，看得到各多少条
  const sd = Array.isArray(s.shardsDetail) ? s.shardsDetail : [];
  if (sd.length) {
    const tot = sd.reduce((a, d) => a + Number(d.rows || 0), 0);
    cards.push('<div class="pcard pwide">' +
      head("ok", T("分片明细"), tf("%d 个 · %d 条", sd.length, tot), "ok") +
      '<div class="pshards">' +
      sd.map((d) => '<div class="psh"><span title="' + esc(d.name) + '.tsv">' +
        esc(d.name || "") + "</span><b>" + n(d.rows) + "</b></div>").join("") +
      "</div></div>");
  }

  // 3) 检查：哪里会出问题就写在这里
  const warns = [];
  if (!s.exists) warns.push(T("包还没生成：点一次「应用」就会产出。"));
  if (s.error) warns.push(s.error);
  cards.push('<div class="pcard">' +
    head(warns.length ? "wa" : "ok", T("检查"),
         warns.length ? T("有 %d 项要注意").replace("%d", warns.length) : T("没问题"),
         warns.length ? "wa" : "ok") +
    (warns.length
      ? warns.map((w) => '<div class="pwarn">' + esc(w) + "</div>").join("")
      : '<div class="pok">' + esc(T("插件和词典都在包里，重装这一个 zip 就是全部。")) + "</div>") +
    "</div>");

  box.innerHTML = cards.join("");
  $("pack-tip").textContent = "";
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
let _modsHideOrphan = false;
const _modsOpen = new Set();     // 展开着的模组（按目录名记）
const _modsSel = new Set();      // 勾选着的模组（按目录名记，就是 batch_start 的 key）
let _modsRows = [];              // 上一次渲染出来的行（全选只作用在当前筛选上）
let _modsCtx = null;             // 右键菜单当前作用的那一个模组

/* Viewer 目录名带着版本和哈希：Appearance Menu Mod 790 2.15.1 2026-10-25T12-38Z ebPkbzzu3
   清单里没必要显示这些，能剥就剥，剥不出来就原样显示。 */
function prettyName(n) {
  const s = String(n || "");
  let m = s.match(/-\d{3,}(?:-\d+)+$/);                       // Name-26359-1-0-1766387572
  if (m) return s.slice(0, m.index).replace(/[.\s]+$/, "");
  m = s.match(/\s+\d[\d.]*\s+\d{3,}\s/);                      // Name 1.0.7 36443 1.0.7 2026-…Z hash
  if (m) return s.slice(0, m.index).trim();
  m = s.match(/\s+\d{3,}\s+[\d.]+\s+\d{4}-\d\d-\d\d/);        // Name 790 2.15.1 2026-10-25T…
  if (m) return s.slice(0, m.index).trim();
  return s;
}

/* 账本里的文件是相对游戏根目录的（../../../../bin/x64/…），列出来只取尾巴 */
function shortPath(p) {
  const s = String(p || "").replace(/^(\.\.\/)+/, "");
  const parts = s.split("/");
  let out = parts[parts.length - 1] || s;
  for (let i = parts.length - 2; i >= 0 && out.length < 30; i--) {
    out = parts[i] + "/" + out;
  }
  return out.length < s.length ? "…/" + out : out;
}

function syncModsChips() {
  ["all", "done", "todo", "empty", "orphan"].forEach((k) => {
    const b = $("mods-filter-" + k);
    if (b) b.classList.toggle("on", _modsFilter === k);
  });
  const ho = $("mods-hide-orphan");
  if (ho) ho.classList.toggle("on", _modsHideOrphan);
}

/* 四种状态：已汉化 / 待翻译 / 无文本 / 孤立。
   后端给了 status 就用它，老账本没这个字段时按老规则退化。 */
function modStatus(m) {
  if (m.status) return m.status;
  if (m.orphan) return "orphan";
  return (m.entries || 0) > 0 ? "done" : "empty";
}

const MOD_STATE = {
  done: { cls: "d", txt: "已汉化" },
  todo: { cls: "t", txt: "待翻译" },
  empty: { cls: "s", txt: "无文本" },
  orphan: { cls: "o", txt: "孤立" },
};

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

/* 模组行的右键：作用对象是这一个模组，不是整批。
   只放不需要动账本的几项——打开目录、载入到翻译页、复制路径、展开收起。
   「重新提取」「删除这个模组的汉化」要改账本，等后端接口单独做。 */
function showModCtxMenu(x, y, m) {
  const el = $("ctxmenu");
  if (!el || !m) return;
  _modsCtx = m;
  _ctxTarget = null;              // 别让表格那套点击逻辑把这一下当成行操作
  _ctxCell = null;
  // 筛选/搜索时列表本来就强制全展开（见 renderMods 的 autoOpen），
  // 这时候给「展开/收起」按钮等于给一个点了没反应的开关。
  const search = ($("mods-search") || {}).value || "";
  const autoOpen = _modsFilter !== "all" || !!String(search).trim();
  const open = autoOpen || _modsOpen.has(m.name);
  el.innerHTML =
    `<div class="ctxgroup">${esc(prettyName(m.name))}</div>` +
    `<div class="item" data-mact="open">${esc(T("打开目录"))}</div>` +
    `<div class="item" data-mact="load">${esc(T("载入到翻译页"))}</div>` +
    `<div class="item" data-mact="copy">${esc(T("复制目录路径"))}</div>` +
    (autoOpen ? "" :
      `<div class="sep"></div>` +
      `<div class="item" data-mact="toggle">${esc(
        open ? T("收起文件列表") : T("展开文件列表"))}</div>`);
  el.classList.add("on");
  const rect = el.getBoundingClientRect();
  el.style.left = Math.max(6, Math.min(x, window.innerWidth - rect.width - 8)) + "px";
  el.style.top = Math.max(6, Math.min(y, window.innerHeight - rect.height - 8)) + "px";
}

function modCtxPath(m) {
  const root = String((_modsData && _modsData.staging) || "")
    .replace(/[\\/]+$/, "");
  return (root && m && m.name) ? root + "\\" + m.name : "";
}

function onModCtxAct(act, m) {
  if (!m) return;
  if (act === "toggle") {
    if (_modsOpen.has(m.name)) _modsOpen.delete(m.name);
    else _modsOpen.add(m.name);
    renderMods();
    return;
  }
  const full = modCtxPath(m);
  if (!full) { toast(T("还没有设置模组目录"), "err"); return; }
  if (act === "copy") {
    copyToClipboard(full, T("已复制目录路径"));
    return;
  }
  if (act === "open") {
    try { pywebview.api.open_dir(full, ""); } catch (_) { /* 没有后端就静默 */ }
    return;
  }
  if (act === "load") {
    $("path").value = full;
    switchPage("translate");
    loadMod();
  }
}

function renderMods() {
  const d = _modsData || { mods: [], summary: {} };
  const q = ($("mods-search").value || "").trim().toLowerCase();
  const all = d.mods || [];
  // 目录没了（卸载 / 改名）就把它从勾选里剔掉，别让「已选 N」虚高
  const alive = new Set(all.map((m) => m.name));
  [..._modsSel].forEach((k) => { if (!alive.has(k)) _modsSel.delete(k); });
  const cnt = { all: all.length, done: 0, todo: 0, empty: 0, orphan: 0 };
  all.forEach((m) => { cnt[modStatus(m)] += 1; });
  ["all", "done", "todo", "empty", "orphan"].forEach((k) => {
    const el = $("mc-" + k);
    if (el) el.textContent = cnt[k];
  });
  const rows = all.filter((m) => {
    const st = modStatus(m);
    if (_modsFilter !== "all" && st !== _modsFilter) return false;
    if (_modsHideOrphan && st === "orphan") return false;
    if (q) {
      const hay = ((m.name || "") + " " + (m.staging || m.folder || "")).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  // 一个模组一行：勾选框 / 名字 / 状态 / 已填 / 总条数，展开后列出还要翻哪些文件
  const box = $("mods-list");
  const autoOpen = _modsFilter !== "all" || !!q;
  const parts = [];
  rows.forEach((m, idx) => {
    const st = modStatus(m);
    const s = MOD_STATE[st] || MOD_STATE.empty;
    const e = Number(m.entries || 0);
    const f = Number(m.filled || 0);
    const todo = Math.max(0, e - f);
    // 没账本（或账本里一条都没有）= 还没提取过：条数是「不知道」，不是 0。
    // 「无文本」是提取过、确实没有可翻的字符串，那是已知结果，不打这个标。
    const unknown = st !== "empty" && st !== "orphan" && st !== "done" && !e;
    const pct = e ? Math.min(100, Math.round((f * 100) / e)) : 0;
    // 只有「有账本」的模组才知道具体文件；没提取过的给一句提示
    const files = Array.isArray(m.files) ? m.files : [];
    const open = autoOpen || _modsOpen.has(m.name);
    const checked = _modsSel.has(m.name);
    parts.push(`<div class="mgrp">
      <div class="grp mod" data-i="${idx}">
        <label class="mck"><input type="checkbox" data-name="${esc(m.name)}"` +
        (checked ? " checked" : "") + (st === "orphan" ? " disabled" : "") +
        `></label>
        <span class="gn"><span class="ar">${open ? "▾" : "▸"}</span>` +
        `<span class="nm">${esc(prettyName(m.name))}</span>` +
        (st === "orphan" ? `<span class="o">${esc(T("目录已不在"))}</span>` : "") +
        (unknown ? `<span class="tag">${esc(T("未分析"))}</span>` : "") +
        `</span>
        <span class="gs"><span class="pill ${s.cls}">${esc(T(s.txt))}</span></span>
        <span class="mt">` +
        (e ? `<span class="minibar"><i style="width:${pct}%"></i></span>` : "") +
        (e
          ? `<b class="fnum${todo ? " todo" : ""}">${f.toLocaleString()}</b>` +
            `<span class="sep">/</span>` +
            `<b class="total">${e.toLocaleString()}</b>`
          : `<b class="total">${unknown ? "?" : "\u2014"}</b>`) +
        `</span>
      </div>`);
    if (open) {
      if (!files.length) {
        // 空列表有四种来路，别用一个「没有未填的条目」糊过去：
        // 目录没了 / 没扫过 / 扫过但只知道条数 / 账本里确实没有可翻文本
        let msg;
        if (st === "orphan") msg = "目录已不在";
        else if (unknown) msg = "还没提取过，不知道有哪些文件";
        else if (!e) msg = "没有可翻的文本";
        else if (m.hasLedger) msg = "没有未填的条目";
        else msg = "只统计了条数，点「预扫描」可以列出文件";
        parts.push(`<div class="mfiles"><div class="mfile empty">${
          esc(T(msg))}</div></div>`);
      } else {
        // 要翻的排在前面（后端已排好），已翻完的照样列出来——用户要看的是
        // 这个模组到底有哪些文件、各多少条，不是只看还剩多少。
        const list = files.slice(0, 60);
        parts.push(`<div class="mfiles">` + list.map((x) => {
          const td = Number(x.todo || 0);
          const tt = Number(x.total || 0);
          return `<div class="mfile" title="${esc(x.file || "")}">
            <span class="fpath mono">${esc(shortPath(x.file))}</span>
            <span class="fnum ${td ? "need" : ""}">${
              td ? td + " " + esc(T("待翻译")) : esc(T("已翻完"))} / ${tt}</span>
          </div>`;
        }).join("") +
        (files.length > list.length ? `<div class="mfile empty">${esc(
          T("还有 %d 个文件没展开").replace("%d", files.length - list.length))}</div>` : "") +
        `</div>`);
      }
    }
    parts.push(`</div>`);
  });
  box.innerHTML = parts.length ? parts.join("")
    : `<p class="hint" style="padding:14px 20px">${esc(T("没有匹配的模组。"))}</p>`;
  _modsRows = rows;
  // 滚动条实际占多宽由浏览器定（style.css 有一条全局 10px 的 ::-webkit-scrollbar），
  // 量一次写进 --sbw：上面的功能栏靠它知道右边该留多少，才能和表格右边界齐平。
  const sc = box.closest(".mscroll");
  if (sc) {
    const g = sc.offsetWidth - sc.clientWidth;
    if (g > 0) document.documentElement.style.setProperty("--sbw", g + "px");
  }
  syncModsBar();

  // 右上角：已汉化 N / 总数 + 进度条
  const p = cnt.all ? Math.round((cnt.done * 100) / cnt.all) : 0;
  const sum = $("mods-summary");
  if (sum) {
    sum.innerHTML = esc(T("已汉化")) + " <b>" + cnt.done + "</b> / " + cnt.all +
      " " + esc(T("个模组")) +
      `<span class="minibar"><i style="width:${p}%"></i></span>`;
  }
  state.stagingCount = cnt.all;
  const tabN = $("tab-mods-n");
  if (tabN) tabN.textContent = cnt.all || "";
}

/* 批量条的状态：选中几个 / 开始按钮文字 / 全选框的半选状态 / 输出路径。
   勾选只改这一条，不重建列表（重建会把滚动条弹回顶部）。 */
function syncModsBar() {
  const rows = _modsRows || [];
  // 孤立的（目录已经不在）勾不上：全选框只按「能勾的」算满不满
  const pickable = rows.filter((m) => modStatus(m) !== "orphan");
  const selRows = pickable.filter((m) => _modsSel.has(m.name)).length;
  const n = _modsSel.size;
  const el = $("mods-sel-n");
  if (el) {
    el.innerHTML = esc(T("已选")) + " <b>" + n + "</b> / "
      + esc(T("当前筛选")) + " " + rows.length;
  }
  const all = $("mods-check-all");
  if (all) {
    all.checked = pickable.length > 0 && selRows === pickable.length;
    all.indeterminate = selRows > 0 && selRows < pickable.length;
  }
  const btn = $("btn-mods-start");
  if (btn) {
    if (state.mbRunning) {
      btn.disabled = true;
      btn.textContent = T("批量进行中…");
    } else {
      btn.disabled = !n;
      btn.textContent = n
        ? T("开始批量翻译 %d 个").replace("%d", n)
        : T("开始批量翻译");
    }
  }
}

/* 批量条的「开始」：勾了哪几个就跑哪几个。
   不用先预扫（batch_scan 要一分钟），后端拿目录名现找。 */
async function startModsBatch() {
  if (state.mbRunning) { toast(T("批量正在运行"), "err"); return; }
  const names = [..._modsSel];
  if (!names.length) { toast(T("请先勾选要翻译的模组"), "err"); return; }
  const qo = $("q-out");
  const outDir = String((qo && qo.value)
    || (state.settings || {}).batch_out_dir || "").trim();
  if (!outDir) {
    toast(T("请先在最上面填好批量输出目录"), "err");
    if (qo) qo.focus();
    return;
  }
  const withArchive = $("q-archive") ? !!$("q-archive").checked : false;
  const skipAssets = $("q-assets") ? !$("q-assets").checked : true;
  const dv = $("q-deploy"), dry = $("q-deploy-dry");
  const doDeploy = dv ? !!dv.checked : false;
  const doDry = dry ? !!dry.checked : true;
  let msg = T("将要处理 %d 个 mod：先复制到输出目录，再翻译、写回副本。\n同名目录会被覆盖。继续？")
    .replace("%d", names.length);
  if (doDeploy) {
    msg += "\n\n" + (doDry
      ? T("本轮只预演部署：算出会覆盖/新增哪些文件，不写原模组目录。")
      : T("本轮跑完会**原地覆盖**原模组目录里对应的文件（副本里没有的文件保持原样）。"));
  }
  if (!confirm(msg)) return;
  // 面板里的输出目录 / 部署开关要落盘：后端 batch_start 读的是 settings
  const saved = await pywebview.api.save_settings({
    batch_out_dir: outDir, batch_deploy: doDeploy, batch_deploy_dry: doDry,
  });
  if (!saved || !saved.ok) {
    toast((saved && saved.error) || T("输出目录保存失败"), "err");
    return;
  }
  state.settings = saved.settings;
  const btn = $("btn-mods-start");
  if (btn) btn.disabled = true;
  const r = await pywebview.api.batch_start(names, withArchive, skipAssets);
  if (!r || !r.ok) {
    toast((r && r.error) || "error", "err");
    if (((r && r.error) || "").includes("API")) openSettings();
    syncModsBar();
    return;
  }
  if (r.started === false) {          // 全都在上一次跑完了
    toast(r.message || "ok", "ok");
    const qs = $("q-summary");
    if (qs) qs.textContent = r.message || "";
    syncModsBar();
    return;
  }
  state.mbRunning = true;
  state.mbCached = r.cached || 0;
  startMbPoll();
  enterBatchMask();      // 别等第一次轮询，立刻就盖住
  syncModsBar();
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
  // 每次打开都回到第一个分类，别停在上次看的那个
  buildSetGroups();
  showSetGroup("sec-model");
  const pane0 = $("set-pane");
  if (pane0) pane0.scrollTop = 0;
  const nav = $("set-nav");
  if (nav) nav.querySelectorAll("button").forEach((x, i) => x.classList.toggle("on", i === 0));
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
    ask_unload_model: $("set-ask-unload").checked,
    extract_opts: state.opts
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
