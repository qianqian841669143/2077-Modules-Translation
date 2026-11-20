/* ============================================================
   bind.js —— 所有按钮 / 输入框的事件绑定
   2026-11-20 从 app.js 原样拆出，只搬运不改逻辑。
   ============================================================ */
function bind() {
  $("win-min").onclick = () => pywebview.api.win_min();
  $("win-max").onclick = () => pywebview.api.win_max();
  $("win-close").onclick = () => pywebview.api.win_close();
  // 顶栏空白处双击 = 最大化（原来挂在 34px 的系统标题条上，那条已经去掉了）
  const tb = document.querySelector(".head");
  if (tb) {
    tb.ondblclick = (e) => {
      if (e.target.closest(".winbtn, .btn, .tab, input")) return;
      pywebview.api.win_max();
    };
  }

  initSplitters();   // 翻译页左右两栏可拖宽（双击复位）

  $("btn-browse").onclick = async () => {
    const p = await pywebview.api.pick_folder();
    // 只在输入框里放路径；state.modPath 等加载成功后由 onLoaded 写（P4）。
    // 以前这里就把它改了，选完文件夹又放弃加载时，state.modPath 已经指向
    // 一个跟表格毫无关系的目录。
    if (p) { $("path").value = p; loadMod(); }
  };
  $("btn-load").onclick = () => loadMod();
  $("btn-apply").onclick = () => applyAll();
  $("btn-export").onclick = () => exportXlsx();
  $("btn-import").onclick = () => importXlsx();
  $("btn-report").onclick = () => showReport();
  $("btn-clear-trans").onclick = () => clearCurrentTranslations();
  $("btn-clear-rows").onclick = () => clearCurrentRows();
  $("btn-clear-scope").onclick = () => clearScope();
  bindMoreMenu();
  document.addEventListener("contextmenu", (e) => {
    // 模组行的右键：作用对象是这一个模组，菜单项走 data-mact 那条分支
    const mg = e.target.closest("#mods-list .grp.mod");
    if (mg) {
      const mi = parseInt(mg.getAttribute("data-i"), 10);
      const mm = _modsRows[mi];
      if (mm) {
        e.preventDefault();
        showModCtxMenu(e.clientX, e.clientY, mm);
      } else {
        closeCtxMenu();
      }
      return;
    }
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
  const mscroll = document.querySelector(".mscroll");
  if (mscroll) mscroll.addEventListener("scroll", closeCtxMenu, { passive: true });
  window.addEventListener("scroll", closeCtxMenu, { passive: true });
  document.addEventListener("click", (e) => {
    const item = e.target.closest("#ctxmenu .item");
    if (!item) { closeCtxMenu(); return; }
    // 模组行菜单：data-mact 的项作用在 _modsCtx 上，和表格行那套没关系
    if (item.dataset.mact) {
      const mod = _modsCtx;
      closeCtxMenu();
      if (!mod || item.classList.contains("disabled")) return;
      onModCtxAct(item.dataset.mact, mod);
      return;
    }
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
    if (act === "copy-orig") {
      copyToClipboard(r.orig || "", T("已复制原文"));
      return;
    }
    if (act === "mark-code") { markRowAsCode(r); return; }
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
  // ---- 左侧导航：翻译 / 模组清单 / 汉化包 ----
  document.querySelectorAll("[data-page-btn]").forEach((b) => {
    b.onclick = () => switchPage(b.getAttribute("data-page-btn"));
  });
  // ---- 翻译页左栏：点文件卡片只看这个文件，再点一次取消 ----
  const flist = $("file-list");
  if (flist) {
    flist.onclick = (e) => {
      const it = e.target.closest(".fitem");
      if (!it) return;
      const f = it.getAttribute("data-file") || "";
      if (!f) { if (state.onlyFile) clearScope(); else renderSide(); return; }
      if (state.onlyFile === f) clearScope(); else onlyFile(f);
      renderSide();
    };
  }
  // ---- 模组清单 ----
  $("btn-mods-refresh").onclick = () => refreshMods();
  $("mods-search").oninput = () => { if (_modsData) renderMods(); };
  ["all", "done", "todo", "empty", "orphan"].forEach((k) => {
    const b = $("mods-filter-" + k);
    if (b) b.onclick = () => { _modsFilter = k; syncModsChips(); renderMods(); };
  });
  const ho = $("mods-hide-orphan");
  if (ho) ho.onclick = () => { _modsHideOrphan = !_modsHideOrphan; syncModsChips(); renderMods(); };
  const mlist = $("mods-list");
  if (mlist) {
    mlist.onclick = (e) => {
      if (e.target.closest(".mck")) return;      // 勾选框不触发展开
      const g = e.target.closest(".grp");
      if (!g) return;
      const i = parseInt(g.getAttribute("data-i"), 10);
      // 下标是「当前筛选后的那一行」，不是 _modsData.mods 的下标：
      // 一搜索/一筛，两边就对不上，点开的会是另一个模组。
      const m = _modsRows[i];
      if (!m) return;
      if (_modsOpen.has(m.name)) _modsOpen.delete(m.name);
      else _modsOpen.add(m.name);
      renderMods();
    };
    mlist.onchange = (e) => {
      const ck = e.target.closest(".mck input");
      if (!ck) return;
      const name = ck.getAttribute("data-name") || "";
      if (!name) return;
      if (ck.checked) _modsSel.add(name); else _modsSel.delete(name);
      syncModsBar();
    };
  }
  const mckAll = $("mods-check-all");
  if (mckAll) {
    mckAll.onchange = () => {
      const rows = _modsRows || [];
      if (mckAll.checked) {
        rows.forEach((m) => { if (modStatus(m) !== "orphan") _modsSel.add(m.name); });
      } else {
        rows.forEach((m) => _modsSel.delete(m.name));
      }
      // 只同步勾选框，不重建列表：重建会把滚动条弹回顶部
      if (mlist) {
        mlist.querySelectorAll(".mck input").forEach((c) => {
          if (!c.disabled) c.checked = mckAll.checked;
        });
      }
      syncModsBar();
    };
  }
  const mScan = $("btn-prescan");
  if (mScan) mScan.onclick = () => modsPrescan();
  const mStart = $("btn-mods-start");
  if (mStart) mStart.onclick = () => startModsBatch();
  $("btn-pack-sync").onclick = async () => {
    const b = $("btn-pack-sync");
    b.disabled = true;
    b.textContent = T("同步中 ...");
    try {
      const r = await pywebview.api.sync_pack();
      if (!r.ok) { toast(r.error || "error", "err"); return; }
      toast(T("账本已同步") + " · " + r.files + " " + T("个文件"), "ok");
      if (r.status) renderPackStatus(r.status);
    } finally {
      b.disabled = false;
      b.textContent = T("同步账本");
    }
  };
  $("btn-pack-open").onclick = async () => {
    const r = await pywebview.api.open_pack();
    if (!r.ok) toast(r.error || "error", "err");
  };
  $("btn-pack-refresh").onclick = () => refreshPackStatus();
  // ---- 资源模式（.archive 内的 CR2W 本地化）----
  $("btn-l10n").onclick = async () => {
    const p = await pywebview.api.l10n_pick();
    if (!p) return;
    setBusy(true, T("正在解包并解析归档 ..."));
    startMbPoll();
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
    startMbPoll();        // official-built / busy 事件走 evbus
    const r = await pywebview.api.build_official_tm();
    if (!r.ok) {
      if (r.error !== "busy") toast(r.error || "error", "err");
      return;
    }
    $("official-info").textContent = T("正在读取官方语言包，约 1 分钟 ...");
  };
  $("btn-locale").onclick = () => openLocale();  $("btn-loc-load").onclick = () => loadLocale();
  // 「更多 → 重新打包」：把当前所有译文重新并进汉化包 zip，打完刷一遍状态。
  // （菜单里原来的「打包为 zip」是归档模式用的，由 table.js 控制显隐，别混）
  $("btn-repack").onclick = async () => {
    const b = $("btn-repack");
    const old = b.textContent;
    b.disabled = true;
    b.textContent = T("打包中 ...");
    try {
      const r = await pywebview.api.pack_zip();
      if (!r || !r.ok) { toast((r && r.error) || "error", "err"); return; }
      toast(T("已打包: %s").replace("%s", r.path), "ok");
      try { await refreshPackStatus(); } catch (_) {}
    } finally {
      b.disabled = false;
      b.textContent = old || T("重新打包");
    }
  };
  $("btn-pack").onclick = async () => {
    const r = await pywebview.api.pack_zip();
    if (r.ok) toast(T("已打包: %s").replace("%s", r.path), "ok");
  };
  $("loc-src").onchange = () => renderLocaleList();
  $("btn-settings").onclick = () => openSettings();
  // 设置面板左侧分类：只显示当前分类
  const setnav = $("set-nav");
  if (setnav) {
    setnav.onclick = (e) => {
      const b = e.target.closest("button[data-sec]");
      if (!b) return;
      setnav.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      showSetGroup(b.getAttribute("data-sec"));
    };
  }
  // ---- 批量翻译的输入/开关已经直接摆在模组列表页上（#mpaths / #mactions），
  // 这里只把初值填一遍：安装缓存目录、输出目录、部署开关。 ----
  try { refreshQueueInfo(); } catch (_) {}
  $("btn-q-pick").onclick = () => pickMbDir();
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
  $("btn-q-export").onclick = () => exportQueueResults();
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
    // 模组列表那 5 个筛选 chip 也带 .chip，但它们在 141 行已经绑过自己的
    // onclick。这里再绑一遍会把筛选整片覆盖掉——点了没反应，还往
    // state.opts[undefined] 里塞脏数据。
    if (c.closest(".mtools")) return;
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
  $("btn-tr-all").onclick = () => translateVisibleUnfilled();
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
  const closeMask = (m) => {
    if (!m) return;
    m.classList.remove("on");
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
    // 地址改过之后顺手刷新模型下拉，省得用户不知道要先点哪儿
    refreshModelList(true);
  };
  // 「启用此模型」：按当前设置里的地址 + 模型名，让本机 llama-server 正跑着它。
  // 在跑别的权重（Q8_0 ↔ Q4_K_M）时后端会先结束旧进程再换，不必去命令行。
  $("btn-model-on").onclick = async () => {
    const btn = $("btn-model-on");
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = T("启动中…");
    $("test-result").textContent = T("正在检查本地服务…");
    let r = null;
    try { r = await pywebview.api.enable_local_model(); } catch (_) { r = null; }
    btn.disabled = false;
    btn.textContent = label;
    if (!r) { toast(T("操作失败：后端没有响应"), "err"); return; }
    const steps = (r.steps || []).join(" · ");
    if (r.ok) {
      toast(r.detail || T("已启用"), "ok");
      $("test-result").textContent = steps || (r.detail || "");
      refreshModelList(true);
      refreshLocalModel();
    } else {
      toast(r.error || r.detail || T("启用失败"), "err");
      $("test-result").textContent = steps || (r.error || r.detail || "");
    }
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
  // ---- 批量列表：全选 / 全不选 / 反选（310 个目录逐个点不现实）----
  $("btn-q-all").onclick = () => setQueueSel("all");
  $("btn-q-none").onclick = () => setQueueSel("none");
  $("btn-q-invert").onclick = () => setQueueSel("invert");
  $("btn-pick-pack-dir").onclick = async () => {
    const p = await pywebview.api.pick_folder();
    if (p) $("set-pack-dir").value = p;
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
  // 主题切换即时预览：先改 state 再重刷 CSS，点「取消」不回滚也没关系——
  // 保存时才写设置，重开设置面板会按已保存的值重新填。
  $("set-theme").onchange = (e) => {
    state.settings.theme = e.target.value;
    applyThemeCss();
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
  $("btn-cache-refresh").onclick = () => { refreshCacheInfo(); };
  $("btn-cache-clear").onclick = async () => {
    const box = $("cache-list");
    const picked = [...box.querySelectorAll("input[data-cache]:checked")];
    if (!picked.length) { toast(T("先勾选要清理的项目"), "warn"); return; }
    const names = picked.map((x) => x.dataset.name || x.dataset.cache).join("、");
    if (!confirm(T("删除这些缓存？\n\n%s\n\n删掉以后下次用到会重新生成；翻译记忆与官方语料不动。")
                 .replace("%s", names))) return;
    const r = await pywebview.api.cache_clear(picked.map((x) => x.dataset.cache));
    if (!r || !r.ok) {
      toast((r && r.error) || T("清理失败"), "err");
      refreshCacheInfo();
      return;
    }
    refreshCacheInfo();
    toast(T("已清理 %s").replace("%s", fmtBytes(r.freed)), "ok");
  };
  $("btn-glossary").onclick = async () => {
    state.glossary = await pywebview.api.glossary_get();
    renderGlossary(); openModal("modal-glossary");
  };
}

/* 翻译页左右两栏可拖动改宽度（双击复位）。
   默认 240 / 220 —— 中间那张表格才是主角，侧栏给个够用就行。
   宽度存 localStorage，下次开机照旧；不写进 settings，免得每拖一下就落盘。 */
function initSplitters() {
  const root = document.documentElement;
  const vkey = (k) => "--" + k + "-w";
  const load = () => {
    try { return JSON.parse(localStorage.getItem("ui.splits") || "{}"); }
    catch (_) { return {}; }
  };
  const save = (k, w) => {
    try {
      const s = load();
      if (w) s[k] = w; else delete s[k];
      localStorage.setItem("ui.splits", JSON.stringify(s));
    } catch (_) {}
  };
  const got = load();
  ["side", "detail"].forEach((k) => {
    if (got[k]) root.style.setProperty(vkey(k), got[k] + "px");
  });
  const work = document.querySelector(".work");
  if (!work) return;
  work.querySelectorAll(".sp").forEach((sp) => {
    const k = sp.getAttribute("data-sp");
    if (!k) return;
    const col = () => work.querySelector(k === "side" ? ".side" : ".detail");
    sp.addEventListener("dblclick", () => {
      root.style.removeProperty(vkey(k));
      save(k, 0);
    });
    sp.addEventListener("pointerdown", (e) => {
      const el = col();
      if (!el || !e.isPrimary) return;
      e.preventDefault();
      sp.classList.add("on");
      try { sp.setPointerCapture(e.pointerId); } catch (_) {}
      const x0 = e.clientX;
      const w0 = el.getBoundingClientRect().width;
      const move = (ev) => {
        const dx = (k === "side") ? ev.clientX - x0 : x0 - ev.clientX;
        // 150px 的时候文件卡里的路径只剩「en-us.j…」，太窄了，最少留 200
        const w = Math.max(200, Math.min(640, Math.round(w0 + dx)));
        root.style.setProperty(vkey(k), w + "px");
      };
      const up = () => {
        sp.classList.remove("on");
        sp.removeEventListener("pointermove", move);
        sp.removeEventListener("pointerup", up);
        sp.removeEventListener("pointercancel", up);
        sp.removeEventListener("lostpointercapture", up);
        save(k, Math.round(el.getBoundingClientRect().width));
      };
      sp.addEventListener("pointermove", move);
      sp.addEventListener("pointerup", up);
      sp.addEventListener("pointercancel", up);
      sp.addEventListener("lostpointercapture", up);
    });
  });
}

/* ---------------- 数据 ---------------- */
