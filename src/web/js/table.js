/* ============================================================
   table.js —— 表格：加载、筛选、选择、渲染、分页、编辑
   2026-11-20 从 app.js 原样拆出，只搬运不改逻辑。
   ============================================================ */
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
  // 后端把事件放进 evbus 队列（后台线程不再碰 evaluate_js），这里必须先把
  // 轮询打开，否则 loaded / busy 事件堆在队列里没人取，界面就永远停在
  // 「正在提取可翻译文本」——就是之前的死锁症状。
  startMbPoll();
  let r;
  try {
    r = await pywebview.api.load_mod(path, state.opts);
  } catch (ex) {
    // 桥调用本身失败（api 还没注入 / 参数序列化不过去）时，后端根本收不到
    // 这次请求，也就不会有任何 busy/loaded 事件回来。不兜底的话遮罩会一直盖着。
    setBusy(false);
    toast((ex && ex.message) || String(ex) || "error", "err");
    return;
  }
  if (!r || !r.ok) {
    setBusy(false);
    if (r && r.error !== "busy") toast((r && r.error) || "error", "err");
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
  updateAppSub();          // 顶栏副标题：模组名 · 文件数 · 段数
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
  if (r.autoSkip) {
    // 上次勾过「跳过」的行：还在表里，但默认就是勾上的
    msg += " · " + T("%d 条按上次的「跳过」自动勾选").replace("%d", r.autoSkip);
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
  $("btn-apply").textContent = on ? T("处理中 ...") : T("应用");
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

// 把「跳过」勾选记到后端：下次加载同一行默认就是勾上的。
// 只对明文层的行有意义（归档行没有磁盘路径，后端会忽略）。
function rememberSkips(rows, value) {
  const items = [];
  const seen = new Set();
  const add = (r) => {
    if (!r || !r.file) return;
    const key = r.file + "\u0000" + (r.orig || "");
    if (seen.has(key)) return;
    seen.add(key);
    items.push([r.file, r.orig || "", !!value]);
  };
  (rows || []).forEach((row) => {
    if (!row) return;
    add(row);
    // 合并视图下一行代表同一句出现的好几处，各自的文件都要记
    (row.dups || []).forEach(add);
  });
  if (!items.length) return;
  try {
    Promise.resolve(pywebview.api.remember_skips(items)).catch(() => {});
  } catch (_) { /* 没有后端时静默 */ }
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
  rememberSkips(rows, value);
  state.dirty = true;
  render();
  setStatus(value
    ? T("已跳过 %d 条内容；机翻和应用时都会忽略。").replace("%d", rows.length)
    : T("已恢复 %d 条内容的翻译。").replace("%d", rows.length));
}

function setFileSkip(file, value) {
  let n = 0;
  const touched = [];
  state.raw.forEach((row) => {
    if (row.file !== file) return;
    if (!!row.skip !== !!value) n += 1;
    row.skip = !!value;
    touched.push(row);
  });
  state.rows.forEach((row) => {
    if (row.file !== file) return;
    row.skip = !!value;
    (row.dups || []).forEach((d) => { d.skip = !!value; });
  });
  rememberSkips(touched, value);
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
    const tcls = r.type === "长句文本" ? "ftype t-long" : "ftype t-n";
    const tline = r.line || r.entryId || "-";
    parts.push(
      `<div class="${cls.join(" ")}" data-i="${r._i}">` +
      `<div class="td num" data-col="num">${idx + 1}` +
      (r.count > 1 ? `<span class="dup-badge" title="${esc(T("同文件内重复 ×%d").replace("%d", r.count))}">×${r.count}</span>` : "") +
      `</div>` +
      `<div class="td skip" data-col="skip"><input type="checkbox" class="skip-box"` +
      `${r.skip ? " checked" : ""} title="${esc(T("跳过此行翻译"))}"></div>` +
      `<div class="td file" data-col="file" title="${esc(
        r.archive ? r.archive + " > " + r.file : fullPathOf(r))}">` +
      `<div class="frow"><span class="open-dir" data-file="${esc(r.file)}" title="${esc(
        r.archive ? T("打开归档所在目录") : T("打开文件所在目录"))}">` +
      `<svg viewBox="0 0 16 16"><path d="M1.5 4.5h4l1.2 1.6h7.8v6.4a1 1 0 0 1-1 1H2.5a1 1 0 0 1-1-1z"/>` +
      `<path d="M1.5 4.5V3.4a1 1 0 0 1 1-1h3.2l1.3 1.7"/></svg></span>` +
      `<span class="fname">${esc(r.disp || r.file)}</span></div>` +
      `<div class="fsub"><span class="fline">${esc(String(tline))}</span>` +
      `<span class="${tcls}">${esc(T(r.type))}</span></div></div>` +
      `<div class="td orig" data-col="orig" title="${esc(r.ctx ? r.ctx + "\n" + r.orig : r.orig)}">` +
      (r.already ? `<span class="already-tag" title="${esc(
        T("模组里本来就是这个语言的文本。改译文列即可覆盖。"))}">${esc(T("已译"))}</span>` : "") +
      `<span class="otext">${esc(r.orig)}</span>` +
      (r.ctx ? `<div class="osub">${esc(r.ctx)}</div>` : "") + `</div>` +
      `<div class="${transCls(r)}" data-col="trans" title="${esc(transTitle(r))}">${esc(r.trans)}</div>` +
      `</div>`);
  });
  tbody.innerHTML = parts.join("");
  $("empty").style.display = (state.rows.length && list.length) ? "none" : "flex";
  if (state.rows.length && !list.length) $("empty").textContent = T("没有匹配的行");
  updateStats(list);
  renderSide();            // 左栏：进度 + 文件列表
  renderDetail();          // 右栏：当前行的出处
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
  renderDetail();          // 右栏「详情」跟着走
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
  // 机翻的 progress / trans-report / done 也走 evbus（_emit 不再直接
  // evaluate_js），所以这里必须先把轮询打开，否则遮罩会永远停在「0 / N」，
  // 后端跑完了前端也收不到 done。
  startMbPoll();
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

