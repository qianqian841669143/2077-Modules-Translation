/* ============================================================
   queue.js —— 批量翻译面板
   2026-11-20 从 app.js 原样拆出，只搬运不改逻辑。
   ============================================================ */
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
  syncQueueStart();     // 跑完了把「全选 / 反选 / 开始」重新放开
}

function onMbPack(e) {
  const zip = e.zip || "";
  const files = Number(e.files || 0);
  if (!zip) return;
  const extra = T("覆盖文件已并入汉化包：%d 个 → %s")
    .replace("%d", files).replace("%s", zip);
  const el = $("q-summary");
  if (el) el.textContent = (el.textContent ? el.textContent + " " : "") + extra;
  toast(extra, "ok");
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
