/* ============================================================
   events.js —— 机翻事件、onEvt 总分发
   2026-11-20 从 app.js 原样拆出，只搬运不改逻辑。
   ============================================================ */
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
  startMbPoll();          // 同 translateRows：不先开轮询，进度事件永远取不到
  enterTransMask(r.total);
  setStatus(T("重试剩下的 %d 条 ...").replace("%d", r.total));
}

function onEvt(e) {
  if (!e) return;
  if (e.type === "trans-report") { onTransReport(e); return; }
  if (e.type === "loaded") { onLoaded(e); return; }
  if (e.type === "applied") { onApplied(e); return; }
  if (e.type === "official-built") { onOfficialBuilt(e); return; }
  if (e.type === "mb-scan") { onMbScan(e); return; }
  if (e.type === "mb-scanned") { onMbScanned(e); return; }
  // 模组列表的预扫描（只数条数，不进批量队列）
  if (e.type === "mods-scan") { onModsScan(e); return; }
  if (e.type === "mods-scanned") { onModsScanned(e); return; }
  if (e.type === "mb-start") { onMbStart(e); return; }
  if (e.type === "mb-item") { onMbItem(e); return; }
  if (e.type === "mb-done") { onMbDone(e); return; }
  if (e.type === "mb-pack") { onMbPack(e); return; }
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
    const doneTxt = Number(e.done || 0).toLocaleString();
    const totTxt = Number(e.total || 0).toLocaleString();
    if (state.mbRunning) {
      // 批量：进度条和 ov-count 归队列管（那两个说的是「第几个模组」），这里
      // 只补遮罩第三行的当前模组条数。以前两边都写 ov-count / ov-bar，遮罩上
      // 的数字每几百毫秒就在「第几个模组」和「第几条文本」之间来回跳。
      const now = $("ov-now");
      if (now) {
        const nm = now.dataset.name || "";
        now.textContent = (nm ? nm + " · " : "") + doneTxt + " / " + totTxt + " " + T("条");
      }
    } else {
      $("ov-bar").style.width = pct + "%";
      $("ov-count").textContent = doneTxt + " / " + totTxt;
    }
    // note: 批次跳过、模型预热这类一次性说明，下一批进度会把它盖掉。
    const line = e.note || T("机翻中 %d / %d ...").replace("%d", e.done).replace("%d", e.total);
    setStatus(line);
    if (e.note) $("overlay-text").textContent = e.note;
  } else if (e.type === "done") {
    exitTransMask();
    let msg = e.memory
      ? T("机翻完成: %d 条（其中 %d 条来自翻译记忆）。请逐条检查后再点「应用」。")
          .replace("%d", e.translated).replace("%d", e.memory)
      : T("机翻完成: %d 条。请逐条检查后再点「应用」。")
          .replace("%d", e.translated);
    setStatus(msg);
    render();
  } else if (e.type === "error") {
    // 先关遮罩：下面几个分支会提前 return，放后面就关不掉了。
    if (state.busy) exitTransMask();
    toast(e.message, "err");
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
  el.textContent = T("事件通道已经 %s 秒没有响应，批量可能卡在某个模组上。"
    + "可以关掉窗口重开，再点「开始批量翻译」——已完成的模组会自动跳过。")
    .replace("%s", sec);
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
    // 否则预扫一开始就会被这里停掉。前端还 busy（单模组提取刚发出、
    // 事件还没回到队列）时也不能停，否则那一次的事件永远取不到。
    //
    // 这里必须同时看 uiBusy：单模组的「提取」遮罩是 setBusy() 开的，它只动
    // state.uiBusy，不动机翻用的 state.busy。只看 busy 的话，loadMod 里刚
    // startMbPoll() 之后 200 ms，第一次 poll 就会因为「队列空 + 没在跑 +
    // busy 为 false」把自己停掉——随后 4 秒后提取成功，loaded/busy 事件全
    // 堆在 deque 里没人取，界面永远停在「正在提取可翻译文本」。
    if (!evs.length && !r.running && !state.busy && !state.uiBusy) stopMbPoll();
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

