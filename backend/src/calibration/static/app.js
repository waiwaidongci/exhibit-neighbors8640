// 试展跨场评分校准台前端（原生 ES2017+，无构建步骤）
const $ = (sel) => document.querySelector(sel);
const fmt = (x, n = 4) => (x === null || x === undefined || Number.isNaN(x))
  ? "—" : Number(x).toFixed(n);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ts = (t) => t ? new Date(t * 1000).toLocaleString("zh-CN",
  { timeZone: "Asia/Shanghai", hour12: false }) : "—";

let currentPoll = null;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...opts,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

// ----------------------------- 受理表单 ----------------------------- //
$("#input-anchor-mode").addEventListener("change", (e) => {
  $("#wrap-explicit").classList.toggle("hidden", e.target.value !== "explicit");
});

$("#btn-demo").addEventListener("click", async () => {
  const payload = await api("/api/demo-payload");
  $("#input-tolerance").value = payload.tolerance;
  $("#input-sessions").value = JSON.stringify(payload.sessions, null, 2);
  $("#input-observations").value = JSON.stringify(payload.observations, null, 2);
  setMsg("演示数据已填入：A 一致组 / B 冲突三角 / C 单桥锚点 / 孤立场", "ok");
});

$("#btn-submit").addEventListener("click", async () => {
  try {
    const payload = {
      name: `研究 ${new Date().toISOString().slice(0, 16)}`,
      tolerance: Number($("#input-tolerance").value),
      sessions: JSON.parse($("#input-sessions").value),
      observations: JSON.parse($("#input-observations").value),
    };
    if ($("#input-anchor-mode").value === "explicit") {
      payload.anchor_exhibit_ids = JSON.parse($("#input-anchors").value);
    }
    const idem = "idem-" + crypto.randomUUID();
    const out = await api("/api/studies", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idem },
      body: JSON.stringify(payload),
    });
    setMsg(out.deduplicated
      ? `命中幂等键，复用研究 ${out.study_id}`
      : `已冻结受理：${out.study_id}`, "ok");
    await loadList();
    openStudy(out.study_id);
  } catch (err) {
    setMsg(err.message, "err");
  }
});

function setMsg(text, kind) {
  const el = $("#create-msg");
  el.textContent = text;
  el.className = "msg " + (kind || "");
}

// ----------------------------- 研究列表 ----------------------------- //
async function loadList() {
  const { studies } = await api("/api/studies");
  const tbody = $("#study-rows");
  if (!studies.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="muted">暂无研究，先在上方发起。</td></tr>';
    return;
  }
  tbody.innerHTML = studies.map((s) => `
    <tr class="clickable" data-id="${esc(s.study_id)}">
      <td>${esc(s.name)}<br><span class="mono">${esc(s.study_id.slice(0, 12))}</span></td>
      <td><span class="badge ${esc(s.status)}">${esc(s.status)}</span>
          ${s.error ? `<div class="mono" style="color:var(--bad)">${esc(s.error)}</div>` : ""}</td>
      <td class="mono">${esc(s.stage)}</td>
      <td>${fmt(s.tolerance, 2)}</td>
      <td class="mono">${esc(s.input_sha256.slice(0, 16))}…</td>
      <td class="mono">${ts(s.created_at)}</td>
    </tr>`).join("");
  tbody.querySelectorAll("tr[data-id]").forEach((tr) =>
    tr.addEventListener("click", () => openStudy(tr.dataset.id)));
}

// ----------------------------- 研究详情 ----------------------------- //
$("#btn-back").addEventListener("click", () => {
  if (currentPoll) { clearInterval(currentPoll); currentPoll = null; }
  $("#panel-detail").classList.add("hidden");
  $("#panel-list").classList.remove("hidden");
});

async function openStudy(studyId) {
  $("#panel-list").classList.add("hidden");
  $("#panel-detail").classList.remove("hidden");
  $("#d-id").textContent = studyId;
  await renderDetail(studyId);
  if (currentPoll) clearInterval(currentPoll);
  currentPoll = setInterval(async () => {
    const status = await api(`/api/studies/${studyId}`);
    if (status.status === "PUBLISHED" || status.status === "FAILED") {
      clearInterval(currentPoll);
      currentPoll = null;
    }
    await renderDetail(studyId);
  }, 1200);
}

async function renderDetail(studyId) {
  const data = await api(`/api/studies/${studyId}/result`);
  const meta = $("#detail-meta");
  meta.innerHTML = `
    <span>状态 <b class="badge ${esc(data.status)}">${esc(data.status)}</b></span>
    <span>检查点 <b>${esc(data.stage)}</b></span>
    <span>容差 <b>${fmt(data.tolerance, 2)}</b></span>
    <span>冻结版本 <b>v1（受理即冻结，SHA256 校验）</b></span>
    <span>输入 SHA256 <b>${esc(data.input_sha256.slice(0, 24))}…</b></span>
    ${data.status === "FAILED" ? `<span style="color:var(--bad)">错误 <b>${esc(
      (await api(`/api/studies/${studyId}`)).error || "")}</b></span>` : ""}`;

  $("#event-list").innerHTML = (data.events || []).map((e) => `
    <li><b>${esc(e.event)}</b> <span class="mono">${ts(e.created_at)}</span>
      ${e.detail ? `<div class="detail">${esc(e.detail)}</div>` : ""}</li>`).join("");

  renderComponents(data);
  renderStability(data);
}

function renderComponents(data) {
  const result = data.result || {};
  const comps = result.components || [];
  if (!comps.length) {
    $("#components").innerHTML = '<p class="muted">尚未完成图构建。</p>';
    return;
  }
  $("#components").innerHTML = comps.map((c) => {
    const biasLine = Object.entries(c.biases || {}).map(([s, b]) =>
      `<span class="pill">${esc(s)} b=${fmt(b)}</span>`).join(" ");
    const edgeLine = (c.edges || []).map((e) =>
      `<div class="kv">边 ${esc(e.a)} ↔ ${esc(e.b)}：Δ=${fmt(e.delta)}
       权重 ${e.weight}，残差 ${fmt(e.residual)}，
       锚点 ${e.anchor_evidence.map((v) => esc(v.anchor_exhibit_id)).join("、")}</div>`
    ).join("");
    const cycles = (c.cycle_basis || []).map((cy) => `
      <div class="cycle">
        <div>环路 #${cy.cycle_no}
          <span class="path">${cy.sessions.map(esc).join(" → ")} → ${esc(
            cy.sessions[0])}</span></div>
        <div>闭合偏差 <b>${fmt(cy.closure)}</b>（绝对值 ${fmt(cy.abs_closure)}
          / 容差 ${fmt(cy.tolerance, 2)}）
          <span class="pill ${cy.exceeded ? "bad" : "good"}">
          ${cy.exceeded ? "超差·隔离" : "闭合良好"}</span></div>
        <details><summary>查看逐边依据</summary>
          ${cy.edges.map((ed) => `<div class="kv path">${esc(ed.traversed.join(" → "))}
            符号 ${ed.sign}，Δ=${fmt(ed.delta)}，锚点
            ${ed.anchor_exhibit_ids.map(esc).join("、")}</div>`).join("")}
        </details></div>`).join("");
    const board = c.quarantined ? "" : `
      <table class="inner"><thead><tr>
        <th>名次</th><th>展品</th><th>校准均分</th><th>评分数</th><th>依据（每场原始分 − 场偏差）</th>
      </tr></thead><tbody>
      ${(c.board || []).map((r) => `<tr>
        <td>${r.rank}</td><td class="mono">${esc(r.exhibit_id)}</td>
        <td><b>${fmt(r.mean_calibrated)}</b></td><td>${r.n_observations}</td>
        <td>${r.evidence.map((v) => `<span class="pill">${esc(v.session_id)}:
          ${fmt(v.raw_score, 2)} − ${fmt(v.session_bias)} =
          ${fmt(v.calibrated_score)}</span>`).join(" ")}</td></tr>`).join("")}
      </tbody></table>`;
    return `<div class="comp ${c.quarantined ? "quarantine" : ""}">
      <h4>分量 #${c.component_id}
        <span class="pill">${c.sessions.map(esc).join("、")}</span>
        <span class="pill ${c.quarantined ? "bad" : "good"}">
          ${c.quarantined ? "隔离不出榜" : "正常出榜"}</span>
        <span class="pill">引用场 ${esc(c.ref_session)}</span></h4>
      <div class="kv">场次偏差：${biasLine}</div>
      ${edgeLine}
      ${cycles ? `<div class="kv">基本回路（${c.cycle_basis.length}）：</div>${cycles}` : ""}
      ${c.quarantined
        ? `<p style="color:var(--bad)">闭环偏差超容差，该组隔离；其他连通组照常出榜。</p>`
        : board}
    </div>`;
  }).join("");
}

function renderStability(data) {
  const st = (data.result || {}).stability;
  const el = $("#stability");
  if (!st) { el.innerHTML = '<p class="muted">稳定性分析尚未完成。</p>'; return; }
  const unstable = (st.unstable || []).map((u) => `
    <div class="unstable">
      <b class="mono">${esc(u.exhibit_id)}</b>
      <span class="pill bad">${u.reasons.map(esc).join("、")}</span>
      <details><summary>触发移除的锚点（${u.triggers.length}）</summary>
      ${u.triggers.map((t) => `<div class="kv">移除
        <b>${esc(t.removed_anchor)}</b> → ${esc(t.reason)}：${esc(t.detail)}</div>`
      ).join("")}</details></div>`).join("");
  el.innerHTML = `
    <div class="kv">逐一移除锚点复算 <b>${st.anchor_runs}</b> 次
      ${st.skipped_anchor_exhibit_ids && st.skipped_anchor_exhibit_ids.length
        ? `（超出上限跳过 ${st.skipped_anchor_exhibit_ids.length} 个锚点）` : ""}。
      不稳展品 <b style="color:var(--warn)">${(st.unstable || []).length}</b> 个，
      稳定展品 <b>${(st.stable_exhibit_ids || []).length}</b> 个。</div>
    ${unstable || '<p class="muted">未发现名次逆转或失去可比性的展品。</p>'}`;
}

loadList().catch((err) => setMsg(err.message, "err"));
