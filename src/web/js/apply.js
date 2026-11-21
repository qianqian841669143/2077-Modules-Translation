/* ============================================================
   apply.js —— 应用 / Excel 导入导出
   2026-11-20 从 app.js 原样拆出，只搬运不改逻辑。
   2026-11-20 删掉「撤销上次应用」「还原翻译」：产物现在是独立 zip，
   要退就直接在 Vortex 里卸掉那个包，比恢复散文件干净。
   ============================================================ */
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
  // 分流：归档文本出归档包，设置层进账本，本地化文本进覆盖包（原模组不动）。
  const resN = items.filter((x) => x.kind === "resource").length;
  const ask = resN
    ? T("将提交 %d 条译文：%d 条是归档文本，其余按「设置进账本 / 本地化进覆盖包」分流。\n不会修改任何原模组文件。")
        .replace("%d", items.length).replace("%d", resN)
    : T("将把 %d 条译文应用（%s）。\n设置层写入账本，本地化文本并进覆盖包，不会修改任何原模组文件。")
        .replace("%d", items.length).replace("%s", state.root || state.modPath);
  if (!confirm(ask)) return;
  setBusy(true, T("正在生成汉化包 ..."));
  startMbPoll();          // applied 事件走 evbus
  let r = await pywebview.api.apply(items);
  if (r && r.ok && r.started === false && r.pending) {
    // 同批文件里还有没译的行：先说清楚，再决定要不要只写这一半
    setBusy(false);
    const files = (r.pendingFiles || []).join("\n");
    const ok = confirm(T("这些文件里还有 %d 条没译的行，本次只写入已译的 %d 条。\n\n%s\n\n继续吗？")
      .replace("%d", r.pending).replace("%d", items.length).replace("%s", files));
    if (!ok) return;
    setBusy(true, T("正在生成汉化包 ..."));
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
      const parts = [];
      if (r.ledger && r.ledger.entries) parts.push(T("账本 %d 条").replace("%d", r.ledger.entries));
      if (r.pack && r.pack.entries) parts.push(T("覆盖包 %d 个文件").replace("%d", r.pack.files || 0));
      if (r.archive && r.archive.archive) parts.push(T("归档包 1 个"));
      const detail = parts.length ? "（" + parts.join(" · ") + "）" : "";
      // 单模组现在出独立 zip（<模组名>_<目标语言>.zip），不再并进总包
      if (r.standalone && r.standalone.zip) {
        const zn = baseName(r.standalone.zip);
        toast(T("%d 条译文已应用%s。\n已生成 %s，装这一个包就行。")
          .replace("%d", r.ok).replace("%s", detail).replace("%s", zn), "ok");
        setStatus(T("应用完成: %d 条 · 已生成 %s")
          .replace("%d", r.ok).replace("%s", zn));
      } else {
        toast(T("%d 条译文已应用%s。\n设置层由 zzz_CP2077_Localization 运行时替换；其余已并入补丁包，重新安装该包即可覆盖。")
          .replace("%d", r.ok).replace("%s", detail), "ok");
        setStatus(T("应用完成: %d 条%s").replace("%d", r.ok).replace("%s", detail));
      }
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
  startMbPoll();          // applied（resource 模式）事件走 evbus
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

  if (r.standalone && r.standalone.zip) {
    setStatus(T("已生成 %s").replace("%s", r.standalone.zip));
  } else if (r.archive) {
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
    // 单模组出的是 zip：装这一个包，不用再去 staging 里翻文件
    if (r.standalone && r.standalone.zip) {
      setStatus(T("已生成 %s").replace("%s", r.standalone.zip)
        + " · " + T("Vortex 装这一个包就行。"));
    } else if (r.outdir) {
      // 产物就落在 mod 目录里：Vortex 管的模组要重新 Deploy 才能进游戏。
      setStatus(T("已输出到 %s").replace("%s", r.outdir)
        + " · " + T("Vortex 用户请重新 Deploy 后启动游戏。"));
    }
  }
  warns.slice(0, 3).forEach((w) => toast(w, "err"));
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
  startMbPoll();          // applied（单条）事件走 evbus
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
