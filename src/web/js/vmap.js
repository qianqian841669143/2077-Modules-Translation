/* ============================================================
   vmap.js —— Vortex 活动映射
   2026-11-20 从 app.js 原样拆出，只搬运不改逻辑。
   ============================================================ */
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
    const name = r.title || r.dir || T("(未命名)");
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

