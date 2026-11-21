/* ============================================================
   queue.js —— 批量翻译面板
   2026-11-20 从 app.js 原样拆出，只搬运不改逻辑。
   ============================================================ */
function openQueue() {
  // 跑着的时候遮罩盖在最上层，入口点不到；万一被程序化调用，就只提示一句。
  if (state.mbRunning) { toast(T("批量翻译正在进行，进度显示在遮罩上")); return; }
  openModal("modal-queue");
  refreshQueueInfo();
  if (state.mbItems.length) renderQueue();
  startMbPoll();   // 面板一打开就开始取事件：后面可能已经有一轮在跑
}

/* 批量遮罩：和机翻同一个思路，跑的时候整块界面盖住，停止入口在遮罩里。
 *
 * 遮罩永远在最上层（style.css 里 .overlay 的 z-index 是 400，比 .mask.top 的
 * 300 还高）。2026-11-20 去掉了遮罩上的「显示面板」入口：那个口子一开就回不来
 * （openQueue 在跑着时只会再收一次遮罩），界面在半锁状态（state.busy 还是 true）
 * 下能被随便点。现在遮罩一旦盖上就盖到跑完，进度全靠遮罩自己说：
 *   第几行：模组级进度（17 / 40 个模组）—— 队列写
 *   进度条：模组级 —— 队列写
 *   第三行：当前模组名 + 该模组内部的条数进度 —— queue 写名字，events 写条数
 */
function enterBatchMask() {
  const ov = $("overlay");
  if (!ov) return;
  ov.classList.add("on", "batch");
  const t = $("overlay-text");
  if (t) t.textContent = T("批量翻译进行中…");
  const cnt = $("ov-count");
  if (cnt && !state.mbIndex) cnt.textContent = "0 / " + Number(state.mbTotal || 0) + " " + T("个模组");
  const now = $("ov-now");
  if (now) { now.textContent = ""; delete now.dataset.name; }
  const st = $("btn-tr-stop");
  if (st) { st.dataset.mode = "batch"; st.textContent = T("停止批量"); st.disabled = false; }
  const app = document.querySelector("main.app");
  if (app) app.inert = true;
  state.busy = true;
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
}

/* 预扫描遮罩：扫目录 / 扫归档这段时间也要看得见在动。
 * 和批量遮罩用同一块 #overlay，只是文案和停止按钮的字不同 —— 预扫描同样能停
 * （batch_cancel 会让扫描循环在下一个模组处 break）。 */
function enterScanMask(classify) {
  const ov = $("overlay");
  if (!ov) return;
  // 每个模组都会来一条 mb-scan，遮罩只建一次，否则进度条每 200ms 被清零
  if (state.mbScanMaskOn) {
    const t0 = $("overlay-text");
    if (t0) t0.textContent = classify ? T("正在扫描归档…") : T("正在预扫模组目录…");
    return;
  }
  state.mbScanMaskOn = true;
  ov.classList.add("on", "batch");
  const t = $("overlay-text");
  if (t) t.textContent = classify ? T("正在扫描归档…") : T("正在预扫模组目录…");
  const cnt = $("ov-count");
  if (cnt) cnt.textContent = "";
  const now = $("ov-now");
  if (now) { now.textContent = ""; delete now.dataset.name; }
  const bar = $("ov-bar");
  if (bar) bar.style.width = "0%";
  const st = $("btn-tr-stop");
  if (st) { st.dataset.mode = "batch"; st.textContent = T("停止预扫"); st.disabled = false; st.style.display = ""; }
  const app = document.querySelector("main.app");
  if (app) app.inert = true;
  state.busy = true;
}

function exitBatchMask() {
  const ov = $("overlay");
  if (ov) ov.classList.remove("on", "batch");
  state.mbScanMaskOn = false;
  const st = $("btn-tr-stop");
  // display 也要还原：打包阶段 enterPackingMask 把它藏起来了（那一段停不掉）
  if (st) {
    delete st.dataset.mode;
    st.textContent = T("停止机翻");
    st.disabled = false;
    st.style.display = "";
  }
  const now = $("ov-now");
  if (now) { now.textContent = ""; delete now.dataset.name; }
  const app = document.querySelector("main.app");
  if (app) app.inert = false;
  state.busy = false;
}

/* 翻译全跑完、正在重建词典包（重扫全部模组 + 重打 zip）。
 *
 * 2026-11-22 之前遮罩在 mb-done 就摘了，只留状态栏一行字。问题是这一步实测
 * 35~50 秒：界面看着能点，点「批量」还会撞上后端 _mb_running，用户只能猜
 * 后台到底有没有在干活。现在遮罩一路盖到 mb-pack（见 onMbPack）。
 *
 * 这期间已经没有可停的东西了：batch_cancel 只置标志位喊 worker，重建和打包
 * 都不看它。所以把「停止批量」按钮收起来，不给一个按了没反应的入口。 */
function enterPackingMask() {
  const ov = $("overlay");
  if (!ov) return;
  ov.classList.add("on", "batch");
  const t = $("overlay-text");
  if (t) t.textContent = T("正在重建词典包…");
  const cnt = $("ov-count");
  if (cnt) cnt.textContent = "";
  const now = $("ov-now");
  if (now) { now.textContent = T("重扫全部模组，重新打包"); delete now.dataset.name; }
  const st = $("btn-tr-stop");
  if (st) { delete st.dataset.mode; st.disabled = true; st.style.display = "none"; }
  const app = document.querySelector("main.app");
  if (app) app.inert = true;
  state.busy = true;
}

async function pickMbOutDir() {
  const p = await pywebview.api.pick_folder();
  if (p) { $("q-out").value = p; syncQueueStart(); }
}

/* 输出目录为空 = 不允许跑。按钮状态跟着输入框走，避免点了才报错。 */
function syncQueueStart() {
  const btn = $("btn-q-start");
  // 跑的时候也别让人点「导出结果」：结果还在长，导出来是半截的
  const exp = $("btn-q-export");
  if (exp) exp.disabled = state.mbRunning;
  // 跑的时候列表还在刷，全选/反选点了会跟正在跑的条目错位
  ["btn-q-all", "btn-q-none", "btn-q-invert"].forEach((id) => {
    const b = $(id);
    if (b) b.disabled = state.mbRunning;
  });
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

/* noMask=true：调用方刚处理完 mb-done，这时候绝不能再把遮罩盖回来。
 * 后端把 _mb_running 置 false 是在 _flush_pack()（并 zip、同步账本）之后，
 * 比 mb-done 晚好几秒；「刚跑完」这一刻去问 running，回答仍然是 true。
 * 2026-11-20 就是这里把遮罩重新盖上去的：309/309 跑完，界面却永远停在
 * 「批量翻译进行中…」，退出只能关窗口。 */
async function refreshQueueInfo(noMask) {
  try {
    const r = await pywebview.api.batch_info();
    if (!r || !r.ok) return;
    if (!$("q-staging").value) $("q-staging").value = r.staging || "";
    if (!$("q-out").value) $("q-out").value = r.outRoot || (state.settings || {}).batch_out_dir || "";
    initDeployBoxes(r);
    initTodoFilter(r);
    syncQueueStart();
    if (r.running && !noMask) {
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
  const withArchive = $("q-archive") ? !!$("q-archive").checked : false;
  $("q-list").innerHTML = "";
  state.mbItems = [];
  state.mbScanMaskOn = false;
  $("q-summary").textContent = T("正在预扫，全部跑完大约 1 分钟 ...");
  const r = await pywebview.api.batch_scan(dir, withArchive);
  if (!r || !r.ok) toast((r && r.error) || "error", "err");
  else startMbPoll();
}

// 模组列表的「预扫描」：只补条数，不复制、不翻译。结果由后端落进
// userdata\mods_scan.json，扫完 list_mods() 读回，"未分析" 就变成真实数字。
async function modsPrescan() {
  const btn = $("btn-prescan");
  if (!btn || btn.disabled) return;
  if (state.mbRunning) {
    toast(T("批量正在运行，先等它跑完"), "err");
    return;
  }
  const el = $("q-staging");
  const dir = el ? String(el.value || "").trim() : "";
  const withArchive = $("q-archive") ? !!$("q-archive").checked : false;
  btn.disabled = true;
  btn.textContent = T("正在预扫描 ...");
  state.mbScanMaskOn = false;
  let r = null;
  try {
    r = await pywebview.api.mods_prescan(dir, false, withArchive);
  } catch (ex) {
    // 老后端没有这个方法（源码新、exe 还是旧的）：别把按钮永远卡在禁用态
    r = { ok: false,
          error: T("预扫描接口不可用（要重新打包程序才生效）：")
            + String(ex || "error") };
  }
  if (!r || !r.ok) {
    btn.disabled = false;
    btn.textContent = T("预扫描");
    toast((r && r.error) || "error", "err");
    return;
  }
  startMbPoll();        // 进度和「扫完了」都从事件通道来，别在这里傻等
}

function onModsScan(e) {
  const btn = $("btn-prescan");
  const note = String(e.note || "");
  const done = Number(e.done || 0);
  const total = Number(e.total || 0);
  const isArchive = e.phase === "archive";
  if (btn) {
    btn.textContent = T("预扫 %d/%d").replace("%d", done).replace("%d", total);
  }
  // 预扫描也要盖遮罩：以前只有按钮上一行小字，扫 309 个目录的一分钟里
  // 看不出跑到哪、卡在哪个模组。
  if (!state.mbRunning) enterScanMask(isArchive);
  const t = $("overlay-text");
  if (t) t.textContent = note || (isArchive ? T("正在扫描归档…") : T("正在预扫模组目录…"));
  const cnt = $("ov-count");
  if (cnt && total) cnt.textContent = done + " / " + total + " " + T("个模组");
  const bar = $("ov-bar");
  if (bar && total) bar.style.width = Math.round((done / total) * 100) + "%";
  const now = $("ov-now");
  if (now) now.textContent = note || String(e.name || "");
}

function onModsScanned(e) {
  const btn = $("btn-prescan");
  if (btn) {
    btn.disabled = false;
    btn.textContent = T("预扫描");
  }
  const n = Number(e.scanned || 0);
  const skip = Number(e.upToDate || 0);
  if (!n && !skip) {
    toast(T("没有需要预扫描的模组"));
  } else {
    let msg = T("预扫描完成：%d 个模组，合计 %d 条")
      .replace("%d", n).replace("%d", Number(e.rows || 0));
    if (skip) msg += " " + T("（%d 个未更新，已跳过）").replace("%d", skip);
    toast(msg);
  }
  exitBatchMask();
  try { refreshMods(); } catch (_) {}
}

function mbUsable(it) {
  return !!(it && it.todo);
}

function mbSize(n) {
  if (!n) return "-";
  return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB"
    : Math.max(1, Math.round(n / 1024)) + " KB";
}

function setQueueSel(mode) {
  if (state.mbRunning) return;
  // 只对「可用」的条目操作：无需翻译的、读取失败的永远勾不上（后端也会拒）
  const usable = state.mbItems.filter(mbUsable).map((x) => String(x.key));
  if (mode === "all") {
    state.mbSel = new Set(usable);
  } else if (mode === "none") {
    state.mbSel = new Set();
  } else {
    const next = new Set();
    usable.forEach((k) => { if (!state.mbSel.has(k)) next.add(k); });
    state.mbSel = next;
  }
  renderQueue();
}

function renderQueue() {
  const box = $("q-list");
  // 这张列表已经不在面板里显示了（勾选和开始都在模组列表页那条批量条上）。
  // 每来一个事件就重建几百行隐藏 DOM 是白烧 CPU，没人看就不建。
  if (box && box.offsetParent === null) return;
  const all = state.mbItems;
  if (!all.length) {
    box.innerHTML = "";
    const c0 = $("q-sel-count");
    if (c0) c0.textContent = "";
    return;
  }
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
    // 「重译」只给部署过的模组：还原之后重跑，翻译记忆里仍是上一轮那份（可能
    // 正是要丢掉的），得先按部署清单把它的机翻记忆删掉，重跑才会真重翻。
    const again = it.deployed
      ? `<button class="q-forget" title="${esc(T("重译这个模组（丢弃本轮记忆）"))}">↻</button>`
      : "";
    return `<div class="q-row ${cls}" data-key="${esc(key)}">
      <input type="checkbox" ${sel ? "checked" : ""} ${usable ? "" : "disabled"}>
      <span class="q-name" title="${esc(it.root || "")}">${esc(it.name || key)}</span>
      <span class="q-num">${usable ? Number(it.todo).toLocaleString() : "-"}</span>
      <span class="q-num">${it.alreadyRows ? Number(it.alreadyRows).toLocaleString() : "-"}</span>
      <span class="q-num">${mbSize(it.bytes)}</span>
      <span class="q-state">${esc(stateText)}</span>
      ${again || "<span></span>"}
      ${sub ? `<span class="q-sub">${esc(sub)}</span>` : ""}
    </div>`;
  });
  box.innerHTML = rows.join("") + (hidden
    ? `<p class="hint">${esc(T("已隐藏 %d 个无需翻译的目录（%d 个里）")
      .replace("%d", hidden).replace("%d", all.length))}</p>`
    : "");
  const usableN = all.filter(mbUsable).length;
  const selN = all.filter((it) => mbUsable(it)
    && state.mbSel.has(String(it.key))).length;
  const cnt = $("q-sel-count");
  if (cnt) {
    cnt.textContent = usableN
      ? T("已勾选 %d / 可用 %d").replace("%d", selN).replace("%d", usableN)
      : "";
  }
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
  box.querySelectorAll(".q-forget").forEach((btn) => {
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      const row = btn.closest(".q-row");
      const key = row ? row.dataset.key : "";
      const it = state.mbItems.find((x) => String(x.key) === key);
      const name = (it && it.name) || key;
      if (!confirm(T("丢弃「%s」上一轮的机翻记忆？\n\n只删机翻与记忆条目（手改和官方语料不动），"
        + "并删掉它的续跑记录。之后重跑这个模组会重新问模型。").replace("%s", name))) return;
      btn.disabled = true;
      try {
        const r = await pywebview.api.batch_forget(key);
        if (r && r.ok) {
          toast(T("已丢弃 %d 条机翻记忆（清单里共 %d 对）")
            .replace("%d", r.forgotten || 0).replace("%d", r.pairs || 0));
          if (it) it.deployed = false;
          renderQueue();
        } else {
          toast((r && r.error) || T("操作失败"), "err");
        }
      } catch (e) {
        toast(String(e), "err");
      } finally {
        btn.disabled = false;
      }
    };
  });
}

function onMbScan(e) {
  const note = String(e.note || "");
  const name = String(e.name || "");
  const done = Number(e.done || 0);
  const total = Number(e.total || 0);
  const isArchive = e.phase === "archive";
  let line;
  if (note) {
    line = note;
  } else if (total) {
    line = T("预扫 %d / %d：%s")
      .replace("%d", done).replace("%d", total).replace("%s", name);
    if (e.upToDate) line += " " + T("（未更新，跳过）");
  } else {
    line = T("正在预扫模组目录…");
  }
  $("q-summary").textContent = line;
  if (!state.mbRunning) enterScanMask(isArchive);
  const t = $("overlay-text");
  if (t) t.textContent = note || (isArchive ? T("正在扫描归档…") : T("正在预扫模组目录…"));
  const cnt = $("ov-count");
  if (cnt && total) cnt.textContent = done + " / " + total + " " + T("个模组");
  const bar = $("ov-bar");
  if (bar && total) bar.style.width = Math.round((done / total) * 100) + "%";
  const now = $("ov-now");
  if (now) now.textContent = note || name;
}

function onMbScanned(e) {
  exitBatchMask();
  state.mbItems = e.items || [];
  state.mbDone = {};
  state.mbSel = new Set(state.mbItems.filter(mbUsable).map((x) => String(x.key)));
  renderQueue();
  $("q-summary").textContent = T("共 %d 个目录，%d 个需要翻译（合计 %d 行待翻）；勾选后开始。")
    .replace("%d", e.total || 0).replace("%d", e.usable || 0)
    .replace("%d", e.rows || 0);
}

/* 遮罩上的进度，优先按「条」算。
 *
 * Y = 本轮总条数（后端 mb-start 的 textTotal，来自预扫缓存的 entries）；
 * X = 已完成模组的「本来中文 + 这轮写回」之和，加上当前模组正在机翻的条数。
 * Y 拿不到（没预扫就直接跑）就退回按模组数显示 —— 一直停在 0 / 0 的死数字
 * 比没有进度更糟。
 */
function renderMaskCount() {
  const cnt = $("ov-count");
  if (!cnt) return;
  const done = Number(state.mbTextDone || 0) + Number(state.mbCur || 0);
  // 分母：预扫的明文层总量，和已完成模组实际行数的较大者。
  // 归档里的行预扫看不到，若只用预扫值，带归档的模组一跑就冲到 100%；
  // 用实际行数托底，分母会随进度长大但不会说谎。没预扫过就整块退回模组数。
  const est = Number(state.mbTextTotal || 0);
  const real = Number((state.mbRowsStat || {}).total || 0);
  const total = est > 0 ? Math.max(est, real) : 0;
  const md = Number(state.mbModDone || 0);
  const mt = Number(state.mbTotal || 0);
  const mods = T("%d / %d 个模组").replace("%d", md).replace("%d", mt);
  if (total > 0) {
    const shown = Math.min(done, total);
    $("ov-bar").style.width = Math.min(100, Math.round((shown / total) * 100)) + "%";
    cnt.textContent = T("已翻译 %d / %d 条")
      .replace("%d", shown.toLocaleString())
      .replace("%d", total.toLocaleString()) + "（" + mods + "）";
    return;
  }
  $("ov-bar").style.width = mt ? Math.min(100, Math.round((md / mt) * 100)) + "%" : "0%";
  cnt.textContent = T("已翻译 %d 条").replace("%d", done.toLocaleString())
    + "（" + mods + "）";
}

function onMbStart(e) {
  state.mbRunning = true;
  state.mbTotal = e.total || 0;
  state.mbIndex = 0;
  // 本轮「已翻译 / 总文本数」的累计器，供 renderMaskCount 用
  state.mbRowsStat = { total: 0, hit: 0 };
  // 遮罩进度按「条」走：总量来自预扫缓存，拿不到就退回按模组数（见 renderMaskCount）
  state.mbTextTotal = Number(e.textTotal || 0);
  state.mbTextDone = 0;
  state.mbCur = 0;
  state.mbModDone = 0;
  $("btn-q-cancel").style.display = "";
  $("q-progress").classList.add("on");
  $("q-bar").style.width = "0%";
  let msg = T("开始处理 %d 个 mod ...").replace("%d", e.total || 0);
  if (state.mbCached) {
    msg += " " + T("（跳过上次已完成的 %d 个）").replace("%d", state.mbCached);
  }
  $("q-summary").textContent = msg;
  enterBatchMask();
  renderMaskCount();
  syncQueueStart();
  try { if (typeof syncModsBar === "function") syncModsBar(); } catch (_) {}
}

function onMbItem(e) {
  const key = String(e.key || "");
  if (!key) return;
  const total = e.total || 1;
  if (e.phase === "start") {
    state.mbIndex = e.index || 0;
    state.mbCur = 0;          // 上一个模组的机翻进度已经结算进 mbTextDone
    state.mbDone[key] = { state: "", text: T("处理中 ...") };
    renderQueue();
    $("q-bar").style.width = Math.round(((e.index || 0) / total) * 100) + "%";
    renderMaskCount();
    // 遮罩第三行：当前模组名。名字存在 data-name 里，条数由 events.js 的
    // progress 事件拼在后面——那边拿不到模组名。
    const now = $("ov-now");
    if (now) {
      now.dataset.name = e.name || key;
      now.textContent = now.dataset.name;
    }
    $("q-summary").textContent = T("(%d/%d) 正在处理 %s")
      .replace("%d", (e.index || 0) + 1).replace("%d", e.total || 0)
      .replace("%s", e.name || key);
    return;
  }
  let st = "ok";
  let txt = T("完成");
  if (e.error === "cancelled") { st = "skip"; txt = T("已取消"); }
  else if (e.error) { st = "err"; txt = T("失败"); }
  else if (e.skippedDone) { txt = T("已是最新"); }
  else if (!e.applied) { st = "skip"; txt = T("无译文可写"); }
  const bits = [];
  if (e.skippedDone) bits.push(e.skippedDone);
  else bits.push(T("复制 %d 个文件").replace("%d", e.copied || 0));
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
  // 累计「已翻译 / 总文本数」：这个模组的总行数进 total，本来就是中文的
  // （already）和这一轮写回去的（applied）进 hit。
  const gstat = state.mbRowsStat;
  if (gstat) {
    gstat.total += Number(e.rows || 0);
    gstat.hit += Number(e.already || 0) + Number(e.applied || 0);
  }
  renderQueue();
  $("q-bar").style.width = Math.round(((e.done || 0) / total) * 100) + "%";
  // 这个模组结算：接下来的条数从「已完成」走，机翻进度归零
  state.mbModDone = Number(e.done || 0);
  state.mbCur = 0;
  state.mbTextDone = Number((gstat && gstat.hit) || 0);
  renderMaskCount();
  // 这个模组做完了：第三行只剩名字，条数看上面的总进度
  const now = $("ov-now");
  if (now && now.dataset.name) now.textContent = now.dataset.name;
}

function onMbDone(e) {
  state.mbRunning = false;
  state.mbCur = 0;
  $("btn-q-cancel").style.display = "none";
  $("q-progress").classList.remove("on");
  $("q-bar").style.width = "0%";
  // 不摘遮罩：后端紧接着就重扫重建词典包（35~50 秒），见 enterPackingMask。
  // 摘遮罩的时机挪到 onMbPack / 打包失败。
  enterPackingMask();
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
  // toast 排到 mb-pack 之后：toasts 的 z-index 是 99，遮罩是 400，
  // 现在弹出来会被盖住，用户什么也看不到。
  state.mbSummary = { msg: msg, err: !!err };
  refreshQueueInfo(true);     // 刚收到 mb-done：只刷目录信息，别再盖遮罩
  syncQueueStart();     // 跑完了把「全选 / 反选 / 开始」重新放开
  // 状态变了：模组列表页那几条要重新读一遍，批量条的按钮也要跟着放开
  try { if (typeof refreshMods === "function" && _modsData) refreshMods(); } catch (_) {}
  try { if (typeof syncModsBar === "function") syncModsBar(); } catch (_) {}
}

/* 翻译全跑完、正在重建词典包。2026-11-22 起这一段遮罩一直盖着（见
 * enterPackingMask），所以条数要同时写到遮罩第三行和状态栏。
 * 数字由后端从本轮结果里算，比前端累计准：前端漏掉了被续跑跳过、
 * 没发 mb-item 的那些模组。 */
function onMbPacking(e) {
  const rows = Number(e.rows || 0);
  const done = Number(e.done || 0);
  const msg = T("正在把补丁并入汉化包…");
  const nums = T("已翻译 %d / %d 条")
    .replace("%d", done.toLocaleString()).replace("%d", rows.toLocaleString());
  enterPackingMask();
  const now = $("ov-now");
  // note 是归档扫描那段的进展（"正在扫描模组归档（30/306）..."），
  // 它比条数统计更能说明「现在卡在哪」，有就优先显示。
  if (now) now.textContent = e.note || (rows ? nums : msg);
  setStatus(e.note || (rows ? msg + " " + nums : msg));
}

function onMbPack(e) {
  const zip = e.zip || "";
  const files = Number(e.files || 0);
  // 打包是批量流程的最后一步，遮罩到这里才摘（见 enterPackingMask）。
  // 中途失败也要摘：后端除了发 mb-pack 带空 zip，还可能只发 error(mb-pack)。
  exitBatchMask();
  const pending = state.mbSummary || null;
  state.mbSummary = null;
  const extra = zip
    ? T("覆盖文件已并入汉化包：%d 个 → %s").replace("%d", files).replace("%s", zip)
    : "";
  const el = $("q-summary");
  if (extra && el) el.textContent = (el.textContent ? el.textContent + " " : "") + extra;
  // 批量结果那句 toast 一直压到这一步才弹（遮罩 z-index 400 > toasts 99）
  if (pending) toast(pending.msg, pending.err ? "err" : "ok");
  if (extra) toast(extra, "ok");
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
  // 「连同贴图/音频」那个开关已经删掉了：默认不复制贴图音频
  const skipAssets = $("q-assets") ? !$("q-assets").checked : true;
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

/* 导出本轮跑完的结果：一行一个 mod，内容就是面板上那张表现在显示的。
 * 后端从 _mb_results 取数，所以取消掉的、还没轮到的不会出现。 */
async function exportQueueResults() {
  if (state.mbRunning) { toast(T("批量正在运行，跑完再导出"), "err"); return; }
  let r;
  try {
    r = await pywebview.api.batch_export();
  } catch (e) {
    toast(String(e), "err");
    return;
  }
  if (!r || !r.ok) {
    if (!(r && r.cancelled)) toast((r && r.error) || T("导出失败"), "err");
    return;
  }
  const msg = r.stale
    ? T("已导出上次跑完的 %d 行: %s")
    : T("已导出 %d 行: %s");
  toast(msg.replace("%d", r.rows || 0).replace("%s", r.path), "ok");
}

/* 「清空输出」按钮和 clearQueueOut() 已移除：副本现在每个模组跑完就被
   mod_batch._drop_copies 删掉了，没有东西可清。留着这个入口只会让人以为
   还得手动扫尾，而它连带删续跑记录（run.jsonl）—— 那正是模组列表要用的。 */

/* ---------------- 应用 / Excel ---------------- */
