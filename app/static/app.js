/* 会议决议执行跟踪系统 —— 纯原生 JS，无构建依赖 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = { users: [], actor: null, view: "meetings" };

const ITEM_BADGE = {
  pending: ["gray", "待提交"], submitted: ["teal", "待验收"],
  rejected: ["red", "验收不通过"], approved: ["green", "验收通过"],
  appeal_pending: ["amber", "复议中"],
};
const RES_BADGE = {
  draft: ["gray", "草稿"], confirmed: ["teal", "已分派"],
  completed: ["green", "已完成"], revoked: ["dark", "已撤销"],
  archived: ["amber", "已归档"],
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function badge(status, map) {
  const [cls, text] = map[status] || ["gray", status];
  return `<span class="badge ${cls}">${text}</span>`;
}
function userName(uid) {
  const u = state.users.find(x => x.id === uid);
  return u ? u.name : uid;
}
function userLabel(uid) {
  const u = state.users.find(x => x.id === uid);
  if (!u) return esc(uid);
  return `${esc(u.name)}<span class="muted"> · ${esc(u.department)}</span>` +
    (u.active ? "" : ' <span class="badge gray">已离职</span>');
}
function fmtTime(t) {
  return esc(String(t).replace("T", " ").slice(0, 16));
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `请求失败 (${res.status})`);
    err.status = res.status;
    err.info = data.info;
    throw err;
  }
  return data;
}
function post(path, body) {
  return api(path, { method: "POST", body: JSON.stringify({ actor_id: state.actor, ...body }) });
}

let toastTimer;
function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3600);
}

/* ---------------- 弹层 ---------------- */
function modal(title, bodyHtml, footerHtml = "") {
  const root = $("#modalRoot");
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal">
        <div class="modal-head"><div class="card-title">${title}</div>
          <span class="spacer"></span>
          <button class="btn ghost sm" onclick="closeModal()">关闭 ✕</button></div>
        <div class="modal-body">${bodyHtml}</div>
        ${footerHtml ? `<div class="modal-foot">${footerHtml}</div>` : ""}
      </div>
    </div>`;
  $("#modalRoot .modal-mask").addEventListener("click", e => {
    if (e.target.classList.contains("modal-mask")) closeModal();
  });
}
function closeModal() { $("#modalRoot").innerHTML = ""; }
window.closeModal = closeModal;

/* ---------------- 启动 ---------------- */
async function init() {
  const meta = await api("/api/meta");
  state.users = meta.users;
  const saved = localStorage.getItem("actor");
  state.actor = state.users.some(u => u.id === saved) ? saved : "u_qian";
  $("#actorSelect").innerHTML = state.users.map(u =>
    `<option value="${u.id}" ${u.id === state.actor ? "selected" : ""}>
      ${u.name}（${u.department}${u.active ? "" : "·已离职"}）</option>`).join("");
  $("#actorSelect").addEventListener("change", e => {
    state.actor = e.target.value;
    localStorage.setItem("actor", state.actor);
    render();
  });
  $$("#tabs .tab").forEach(btn => btn.addEventListener("click", () => {
    state.view = btn.dataset.view;
    render();
  }));
  render();
}

function render() {
  $$("#tabs .tab").forEach(b => b.classList.toggle("active", b.dataset.view === state.view));
  ({
    meetings: viewMeetings, resolutions: viewResolutions, todos: viewTodos,
    evidence: viewEvidence, reviews: viewReviews, archive: viewArchive,
    messages: viewMessages,
  })[state.view]();
}

/* ---------------- 会议记录（议题可折叠） ---------------- */
async function viewMeetings() {
  const main = $("#main");
  main.innerHTML = `<div class="muted" style="margin-bottom:12px">原始会议记录按议题折叠展示，决议均回溯到对应会议与议题。</div>`;
  const { meetings } = await api("/api/meetings");
  if (!meetings.length) return main.insertAdjacentHTML("beforeend", '<div class="empty">暂无会议记录</div>');
  for (const m of meetings) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="card-head" data-toggle="m">
        <span class="caret">▼</span>
        <div class="card-title">${esc(m.title)}</div>
        <span class="spacer"></span>
        <span class="muted">${esc(m.held_at)} · ${esc(m.location)}</span>
      </div>
      <div class="card-body collapse-body" data-body="m">
        <dl class="kv">
          <dt>会议秘书</dt><dd>${userLabel(m.secretary_id)}</dd>
          <dt>主持人</dt><dd>${userLabel(m.host_id)}</dd>
        </dl>
        <div class="section-label">会议议题与原始记录</div>
        <div id="topics-${m.id}"></div>
      </div>`;
    main.appendChild(card);
    const box = $(`#topics-${m.id}`);
    m.topics.forEach((t, i) => {
      const tEl = document.createElement("div");
      tEl.className = "item";
      tEl.innerHTML = `
        <div class="item-head" data-toggle="t" style="cursor:pointer">
          <span class="caret">▼</span>
          <b>${esc(t.title)}</b>
          <span class="spacer"></span>
          <span class="muted">点击折叠/展开</span>
        </div>
        <div class="collapse-body" data-body="t" style="margin-top:8px">
          <div style="white-space:pre-wrap;color:var(--charcoal-soft)">${esc(t.body)}</div>
        </div>`;
      if (i !== 0) $(".collapse-body", tEl).classList.add("collapsed"),
        tEl.classList.add("collapsed");
      box.appendChild(tEl);
    });
    $(".card-head", card).addEventListener("click", () => {
      $('[data-body="m"]', card).classList.toggle("collapsed");
      card.classList.toggle("collapsed");
    });
    $$("[data-toggle='t']", box).forEach(h => h.addEventListener("click", () => {
      const body = h.nextElementSibling;
      body.classList.toggle("collapsed");
      h.closest(".item").classList.toggle("collapsed");
    }));
  }
}

/* ---------------- 决议分派列表 ---------------- */
async function viewResolutions() {
  const main = $("#main");
  main.innerHTML = `<div class="muted" style="margin-bottom:12px">
    汇总状态由各责任子项实时计算；同一决议的多项责任分别跟踪。</div>
    <div id="resList"></div>`;
  const { resolutions } = await api("/api/resolutions");
  state._resolutions = resolutions;
  const box = $("#resList");
  if (!resolutions.length) return box.innerHTML = '<div class="empty">暂无决议</div>';
  for (const r of resolutions) box.appendChild(renderResolutionCard(r));
}

function renderResolutionCard(r) {
  const card = document.createElement("div");
  card.className = "card";
  const isHost = r.meeting ? false : null;
  card.innerHTML = `
    <div class="card-head" style="cursor:default">
      <span class="code">${esc(r.code)}</span>
      <div class="card-title">${esc(r.title)}</div>
      <span class="spacer"></span>
      ${badge(r.rollup, RES_BADGE)}
      ${r.status === "confirmed" ? `<span class="badge gray">${esc(r.rollup_cn)}</span>` : ""}
      ${r.overdue_count ? `<span class="overdue-tag">${r.overdue_count} 项逾期</span>` : ""}
    </div>
    <div class="card-body">
      <div class="muted" style="margin-bottom:10px">
        来源：${esc(r.meeting_title)} · ${esc(String(r.held_at).slice(0, 10))}
        ${r.status === "revoked" ? `· <b style="color:var(--danger)">撤销原因：${esc(r.revoke_reason)}</b>` : ""}
      </div>
      <div id="items-${r.id}"></div>
      <div class="row" style="margin-top:14px">
        <button class="btn sm" data-act="detail">详情（原文 / 证据 / 版本链）</button>
        ${r.status === "draft" ? `<button class="btn sm secondary" data-act="confirm">主持人确认分派</button>` : ""}
        ${r.status === "confirmed" ? `
          <button class="btn sm ghost" data-act="reschedule">改期</button>
          <button class="btn sm ghost" data-act="reassign">责任人变更</button>
          <button class="btn sm secondary" data-act="archive">归档</button>
          <button class="btn sm danger" data-act="revoke">撤销</button>` : ""}
        ${r.status === "archived" ? `<button class="btn sm ghost" data-act="note">补充附注</button>` : ""}
      </div>
    </div>`;
  const list = $(`#items-${r.id}`, card);
  r.items.forEach(it => list.appendChild(renderItemRow(it, r, false)));
  $("[data-act='detail']", card).addEventListener("click", () => openResolution(r.id));
  $("[data-act='confirm']", card)?.addEventListener("click", () => confirmResolution(r));
  $("[data-act='revoke']", card)?.addEventListener("click", () => revokeResolution(r));
  $("[data-act='archive']", card)?.addEventListener("click", () => archiveResolution(r));
  $("[data-act='reschedule']", card)?.addEventListener("click", () => rescheduleModal(r));
  $("[data-act='reassign']", card)?.addEventListener("click", () => reassignModal(r));
  $("[data-act='note']", card)?.addEventListener("click", () => noteModal(r));
  return card;
}

function renderItemRow(it, r, detailed) {
  const el = document.createElement("div");
  el.className = "item" + (it.overdue ? " overdue" : "");
  const [, stext] = ITEM_BADGE[it.item_status] || ["gray", it.item_status];
  el.innerHTML = `
    <div class="item-head">
      <b>${it.seq}. ${esc(it.content)}</b>
      <span class="spacer"></span>
      <span class="badge ${ITEM_BADGE[it.item_status][0]}">${stext}</span>
    </div>
    <div class="item-meta">
      责任部门：<b>${esc(it.department)}</b> ·
      责任人：<b>${userLabel(it.assignee_id)}</b> ·
      期限：<b>${esc(it.due_date)}</b>
      ${it.overdue ? `<span class="overdue-tag">逾期 ${it.overdue_days} 天</span>` : ""}
      · 验收人：${it.verifier_ids.map(v => `<b>${esc(userName(v))}</b>`).join("、") || '<span class="muted">未指定</span>'}
      · 第 ${it.current_round} 轮
      ${it.latest_evidence ? `· 最新证据：<a class="file" href="/api/attachments/${it.latest_evidence.id}">📎 ${esc(it.latest_evidence.filename)}</a>` : ""}
    </div>
    ${it.delay_reason ? `<div class="delay-reason">延期原因：${esc(it.delay_reason)}</div>` : ""}
    ${it.last_verdict ? `
      <div class="item-meta" style="margin-top:6px">
        最近验收（${fmtTime(it.last_verdict.created_at)}，${esc(it.last_verdict.verifier_name)}）：
        <span class="badge ${it.last_verdict.approved ? "green" : "red"}">
          ${it.last_verdict.approved ? "通过" : "不通过"}</span>
        ${esc(it.last_verdict.comment)}
      </div>` : ""}
    ${it.appeal_pending ? `<div class="item-meta" style="color:var(--warn);margin-top:6px">⚠ 责任人已申请复议，等待主持人裁定</div>` : ""}
    <div class="row" style="margin-top:8px"></div>`;
  const actions = $(".row", el);
  if (detailed) {
    actions.appendChild(itemActionButtons(it, r));
  }
  return el;
}

function itemActionButtons(it, r) {
  const wrap = document.createElement("span");
  wrap.className = "pill-group";
  const btns = [];
  const editable = ["confirmed"].includes(r.status);
  if (editable && it.assignee_id === state.actor && it.item_status !== "approved")
    btns.push(["upload", "上传/替换证据", ""]);
  if (editable && it.verifier_ids.includes(state.actor) && it.latest_evidence &&
      it.item_status !== "approved" && !it.appeal_pending)
    btns.push(["verdict", "提交验收", "secondary"]);
  if (editable && it.assignee_id === state.actor && it.item_status === "rejected")
    btns.push(["appeal", "申请复议", "ghost"]);
  if (editable && it.verifier_ids.includes(state.actor))
    btns.push(["history", "证据版本链", "ghost"]);
  btns.push(["history", "证据记录", "ghost"]);
  for (const [act, text, cls] of btns) {
    const b = document.createElement("button");
    b.className = `btn sm ${cls}`;
    b.textContent = text;
    b.addEventListener("click", () => ({
      upload: () => uploadModal(it, r), verdict: () => verdictModal(it, r),
      appeal: () => appealModal(it, r), history: () => historyModal(it),
    })[act]());
    wrap.appendChild(b);
  }
  return wrap;
}

/* ---------------- 决议详情：原文与最新证据并排 ---------------- */
async function openResolution(rid) {
  const { resolution: r } = await api(`/api/resolutions/${rid}`);
  const evidenceRows = r.items.filter(i => i.latest_evidence).map(i => `
    <li><b>${esc(i.content)}</b><br>
      <a class="file" href="/api/attachments/${i.latest_evidence.id}">📎 ${esc(i.latest_evidence.filename)}</a>
      <span class="muted">（第 ${i.current_round} 轮，${fmtTime(i.latest_evidence.created_at)}）</span>
      <div class="muted">${esc(i.latest_evidence.note)}</div></li>`).join("");
  modal(`${esc(r.code)} ${esc(r.title)}`, `
    <div class="row" style="margin-bottom:12px">
      ${badge(r.status, RES_BADGE)}
      <span class="badge gray">通过 ${r.approved_count}/${r.total_count} 项</span>
      <span class="spacer"></span>
      <span class="muted">${esc(r.meeting.title)}</span>
    </div>
    <div class="side-by-side">
      <div>
        <div class="section-label" style="margin-top:0">决议原文（验收依据）</div>
        <div class="original-text">${esc(r.original_text)}</div>
      </div>
      <div>
        <div class="section-label" style="margin-top:0">最新执行证据</div>
        <div class="evidence-box">
          ${evidenceRows ? `<ul class="plain">${evidenceRows}</ul>` : '<div class="muted">尚无证据提交</div>'}
        </div>
      </div>
    </div>
    <div class="section-label">责任子项（分别跟踪）</div>
    <div id="detailItems"></div>
    <div class="section-label">附注</div>
    <div class="evidence-box">
      ${r.notes.length ? `<ul class="plain">${r.notes.map(n =>
        `<li><b>${esc(n.author_name)}</b> <span class="muted">${fmtTime(n.created_at)}</span><br>${esc(n.body)}</li>`).join("")}</ul>`
        : '<span class="muted">暂无附注</span>'}
      <button class="btn sm ghost" id="addNoteBtn" style="margin-top:8px">
        ${r.status === "archived" ? "归档后补充附注（唯一允许的变更）" : "补充附注"}</button>
    </div>
    <div class="section-label">版本链（改期 / 改派 / 撤销 / 证据替换 / 验收 / 复议）</div>
    <ul class="timeline">
      ${r.log.map(l => `
        <li><span class="t-time">${fmtTime(l.created_at)}</span>
          <span class="kind-tag">${esc(l.kind)}</span>
          ${esc(l.actor_name || "系统")}：${esc(l.summary)}</li>`).join("")}
    </ul>`);
  const box = $("#detailItems");
  r.items.forEach(it => box.appendChild(renderItemRow(it, r, true)));
  $("#addNoteBtn").addEventListener("click", () => noteModal(r, true));
}

/* ---------------- 决议级操作 ---------------- */
async function confirmResolution(r) {
  if (!confirm(`确认以「${userName(state.actor)}」身份确认并分派决议 ${r.code}？\n将向各执行人、验收人发送待办通知。`)) return;
  try {
    await post(`/api/resolutions/${r.id}/confirm`, {});
    toast("决议已确认分派，待办与通知已生成");
    render();
  } catch (e) { toast(e.message, "error"); }
}
async function revokeResolution(r) {
  const reason = prompt(`撤销决议 ${r.code} 的原因：\n撤销后所有未完成待办将被回收，版本链保留。`);
  if (reason === null) return;
  if (!reason.trim()) return toast("必须填写撤销原因", "warn");
  try {
    await post(`/api/resolutions/${r.id}/revoke`, { reason });
    toast("决议已撤销，相关待办已回收");
    render();
  } catch (e) { toast(e.message, "error"); }
}
async function archiveResolution(r) {
  const note = prompt(`归档决议 ${r.code} 的备注（可留空）：\n仅当所有子项验收通过时才能归档。`);
  if (note === null) return;
  try {
    await post(`/api/resolutions/${r.id}/archive`, { note });
    toast("决议已归档，归档后仅可补充附注");
    render();
  } catch (e) { toast(e.message, "error"); }
}
function rescheduleModal(r) {
  const pending = r.items.filter(i => i.item_status !== "approved");
  modal("会后改期", `
    <div class="field"><label>责任子项</label>
      <select id="rsItem">${pending.map(i => `<option value="${i.id}">${esc(i.content)}（现期限 ${i.due_date}）</option>`).join("")}</select></div>
    <div class="field"><label>新完成期限</label><input type="date" id="rsDue"></div>
    <div class="field"><label>改期原因（必填，随版本链保存）</label><textarea id="rsReason" rows="3"></textarea></div>`,
    `<button class="btn ghost" onclick="closeModal()">取消</button>
     <button class="btn" id="rsOk">确认改期并通知</button>`);
  $("#rsOk").addEventListener("click", async () => {
    try {
      await post("/api/items/reschedule", {
        item_id: $("#rsItem").value, new_due: $("#rsDue").value, reason: $("#rsReason").value,
      });
      closeModal(); toast("改期成功，已通知执行人"); render();
    } catch (e) { toast(e.message, "error"); }
  });
}
function reassignModal(r) {
  const actives = state.users.filter(u => u.active);
  modal("责任人变更（离职交接等）", `
    <div class="field"><label>责任子项</label>
      <select id="raItem">${r.items.map(i =>
        `<option value="${i.id}">${esc(i.content)}（现责任人 ${esc(userName(i.assignee_id))}）</option>`).join("")}</select></div>
    <div class="field"><label>新责任人</label>
      <select id="raUser">${actives.map(u => `<option value="${u.id}">${u.name}（${u.department}）</option>`).join("")}</select></div>
    <div class="field"><label>原因</label><input id="raReason" value="责任人离职"></div>`,
    `<button class="btn ghost" onclick="closeModal()">取消</button>
     <button class="btn" id="raOk">确认改派</button>`);
  $("#raOk").addEventListener("click", async () => {
    try {
      await post("/api/items/reassign", {
        item_id: $("#raItem").value, new_assignee_id: $("#raUser").value, reason: $("#raReason").value,
      });
      closeModal(); toast("改派成功：旧待办回收、新待办开启"); render();
    } catch (e) { toast(e.message, "error"); }
  });
}
function noteModal(r, fromDetail) {
  modal(r.status === "archived" ? "归档后补充附注" : "补充附注", `
    <div class="field"><label>附注内容</label><textarea id="noteBody" rows="4"
      placeholder="${r.status === "archived" ? "决议已归档，附注是唯一允许新增的内容" : ""}"></textarea></div>`,
    `<button class="btn ghost" onclick="closeModal()">取消</button>
     <button class="btn" id="noteOk">保存附注</button>`);
  $("#noteOk").addEventListener("click", async () => {
    try {
      await post(`/api/resolutions/${r.id}/notes`, { body: $("#noteBody").value });
      closeModal(); toast("附注已保存");
      if (fromDetail) openResolution(r.id); else render();
    } catch (e) { toast(e.message, "error"); }
  });
}

/* ---------------- 证据提交 / 替换 ---------------- */
async function viewEvidence() {
  const main = $("#main");
  main.innerHTML = `<div class="muted" style="margin-bottom:12px">
    当前操作人：<b>${esc(userName(state.actor))}</b>。只有责任人本人能上传证据；
    同一轮再次上传会<b>替换</b>旧证据，旧文件与记录保留在版本链中；验收不通过后重新提交自动进入下一轮。</div>
    <div id="evList"></div>`;
  const { todos } = await api(`/api/todos?user=${state.actor}`);
  const mine = todos.filter(t => t.kind === "executor");
  const open = mine.filter(t => t.state === "open");
  const box = $("#evList");
  if (!open.length) box.innerHTML = '<div class="empty">当前没有需要提交证据的执行任务</div>';
  const { resolutions } = await api("/api/resolutions");
  for (const t of open) {
    const r = resolutions.find(x => x.id === t.resolution_id);
    const it = r?.items.find(x => x.id === t.item_id);
    if (!r || !it) continue;
    const el = document.createElement("div");
    el.className = "card";
    el.innerHTML = `
      <div class="card-body">
        <div class="row">
          <b>${esc(it.content)}</b>
          <span class="spacer"></span>
          ${badge(it.item_status, ITEM_BADGE)}
          <span class="code">${esc(r.code)}</span>
          <span class="${it.overdue ? "overdue-tag" : "muted"}">
            期限 ${esc(it.due_date)}${it.overdue ? ` · 逾期 ${it.overdue_days} 天` : ""}</span>
        </div>
        ${it.delay_reason ? `<div class="delay-reason">延期原因：${esc(it.delay_reason)}</div>` : ""}
        ${it.latest_evidence ? `<div class="item-muted" style="margin:8px 0">
          当前第 ${it.current_round} 轮证据：
          <a class="file" href="/api/attachments/${it.latest_evidence.id}">📎 ${esc(it.latest_evidence.filename)}</a>
          <span class="muted">（再次上传将替换）</span></div>` : ""}
        <form class="form-inline" style="margin-top:10px">
          <div class="field"><label>过程证据文件</label><input type="file" required></div>
          <div class="field" style="flex:1;min-width:200px"><label>说明</label><input type="text" placeholder="证据说明（可选）"></div>
          <button class="btn" type="submit">上传证据</button>
          <button class="btn ghost" type="button" data-history>版本链</button>
        </form>
      </div>`;
    $("form", el).addEventListener("submit", async e => {
      e.preventDefault();
      const fd = new FormData();
      fd.append("actor_id", state.actor);
      fd.append("item_id", it.id);
      fd.append("file", $("input[type=file]", el).files[0]);
      fd.append("note", $("input[type=text]", el).value);
      try {
        const out = await api("/api/items/evidence", { method: "POST", body: fd });
        toast(out.replaced ? `证据已替换（旧文件 ${out.replaced} 保留在版本链），进入第 ${out.round} 轮`
                           : `证据已提交（第 ${out.round} 轮），已通知验收人`);
        render();
      } catch (ex) { toast(ex.message, "error"); }
    });
    $("[data-history]", el).addEventListener("click", () => historyModal(it));
    box.appendChild(el);
  }
}

async function historyModal(it) {
  const { evidence } = await api(`/api/resolutions/x/evidence?item=${it.id}`);
  modal(`证据版本链：${it.content}`, `
    <ul class="timeline">
      ${evidence.map(e => `<li>
        <span class="t-time">第 ${e.round} 轮 · ${fmtTime(e.created_at)}</span>
        <a class="file" href="/api/attachments/${e.id}">📎 ${esc(e.filename)}</a>
        ${e.superseded_by ? '<span class="badge amber">已被替换（保留留痕）</span>' : '<span class="badge teal">当前版本</span>'}
        <div class="muted">${esc(e.uploader_name)}：${esc(e.note)}</div></li>`).join("")}
    </ul>`);
}

function uploadModal(it, r) {
  modal(`上传证据：${it.content}`, `
    <div class="muted" style="margin-bottom:10px">决议 ${esc(r.code)} · 第 ${it.current_round} 轮
    ${it.latest_evidence ? "· 将替换当前证据" : ""}</div>
    <form id="upForm">
      <div class="field"><label>过程证据文件</label><input type="file" id="upFile" required></div>
      <div class="field"><label>说明</label><input type="text" id="upNote"></div>
    </form>`,
    `<button class="btn ghost" onclick="closeModal()">取消</button>
     <button class="btn" id="upOk">上传</button>`);
  $("#upOk").addEventListener("click", async () => {
    const f = $("#upFile").files[0];
    if (!f) return toast("请选择文件", "warn");
    const fd = new FormData();
    fd.append("actor_id", state.actor);
    fd.append("item_id", it.id);
    fd.append("file", f);
    fd.append("note", $("#upNote").value);
    try {
      const out = await api("/api/items/evidence", { method: "POST", body: fd });
      closeModal();
      toast(out.replaced ? `已替换旧证据，旧文件保留留痕` : `证据已提交，已通知验收人`);
      render();
    } catch (e) { toast(e.message, "error"); }
  });
}

/* ---------------- 待办 ---------------- */
async function viewTodos() {
  const main = $("#main");
  main.innerHTML = `
    <div class="searchbar">
      <select id="todoUser"></select>
      <button class="btn ghost" id="recycleBtn">查看回收/关闭记录</button>
    </div>
    <div id="todoBox"></div>`;
  $("#todoUser").innerHTML = state.users.map(u =>
    `<option value="${u.id}" ${u.id === state.actor ? "selected" : ""}>${u.name}（${u.department}）</option>`).join("");
  $("#todoUser").addEventListener("change", e => loadTodos(e.target.value, false));
  $("#recycleBtn").addEventListener("click", () => {
    const showing = $("#recycleBtn").dataset.on === "1";
    loadTodos($("#todoUser").value, !showing);
  });
  loadTodos(state.actor, false);
}

async function loadTodos(uid, recycleAll) {
  const btn = $("#recycleBtn");
  btn.dataset.on = recycleAll ? "1" : "";
  btn.textContent = recycleAll ? "返回我的待办" : "查看回收/关闭记录";
  const box = $("#todoBox");
  if (recycleAll) {
    const { todos } = await api("/api/recycle-bin");
    box.innerHTML = `<div class="section-label" style="margin-top:0">已回收待办（撤销决议、离职改派、归档）</div>` +
      todoList(todos, true);
    return;
  }
  const { todos } = await api(`/api/todos?user=${uid}`);
  const open = todos.filter(t => t.state === "open");
  const done = todos.filter(t => t.state !== "open");
  box.innerHTML =
    `<div class="section-label" style="margin-top:0">待处理（${open.length}）</div>` + todoList(open, false) +
    `<div class="section-label">历史（${done.length}）</div>` + todoList(done, true);
}

function todoList(todos, dim) {
  if (!todos.length) return '<div class="empty">无记录</div>';
  return `<div class="card">${todos.map(t => `
    <div class="todo ${t.state === "recycled" ? "state-recycled" : ""}">
      <div class="todo-icon ${t.kind === "verifier" ? "verify" : ""}">${t.kind === "verifier" ? "验" : "执"}</div>
      <div style="flex:1">
        <div><b>${esc(t.content)}</b>
          <span class="code" style="margin-left:6px">${esc(t.resolution_code)}</span>
          ${t.res_status === "revoked" ? '<span class="badge dark">决议已撤销</span>' : ""}
          ${t.res_status === "archived" ? '<span class="badge amber">已归档</span>' : ""}
        </div>
        <div class="muted">${t.kind_cn}任务 · ${esc(t.resolution_title)} · 期限 ${esc(t.due_date)}
          ${t.overdue ? `<span class="overdue-tag">逾期 ${t.overdue_days} 天</span>` : ""}</div>
        ${t.state !== "open" ? `<div class="muted">${t.state_cn}：${esc(t.close_reason)} · ${fmtTime(t.closed_at)}</div>` : ""}
      </div>
      ${t.state === "open" ? `<button class="btn sm" data-rid="${t.resolution_id}">前往处理</button>` : ""}
    </div>`).join("")}</div>`;
}

document.addEventListener("click", e => {
  const b = e.target.closest(".todo [data-rid]");
  if (b) { state.view = "resolutions"; render(); setTimeout(() => openResolution(b.dataset.rid), 120); }
});

/* ---------------- 验收与复议 ---------------- */
async function viewReviews() {
  const main = $("#main");
  main.innerHTML = `
    <div class="muted" style="margin-bottom:12px">
      验收人须按<b>决议原文</b>核对证据；执行人不能验收自己的工作；同一轮次两名验收人同时提交时，
      后到一方将收到 <b>409 冲突提示</b>。可切换两名验收人分别提交以观察效果。
    </div>
    <div class="section-label" style="margin-top:0">待我验收</div><div id="verifyBox"></div>
    <div class="section-label">待裁定的复议申请（主持人）</div><div id="appealBox"></div>
    <div class="section-label">我被驳回的事项（可申请复议）</div><div id="rejectBox"></div>`;
  const { resolutions } = await api("/api/resolutions");
  const verify = [], rejected = [], appeals = [];
  for (const r of resolutions) {
    for (const it of r.items) {
      if (r.status !== "confirmed") continue;
      if (it.verifier_ids.includes(state.actor) && it.latest_evidence &&
          it.item_status !== "approved" && !it.appeal_pending) verify.push([r, it]);
      if (it.assignee_id === state.actor && it.item_status === "rejected") rejected.push([r, it]);
    }
    for (const it of r.items) {
      for (const rr of it.review_requests || []) {
        if (rr.status === "pending") appeals.push([r, it, rr]);
      }
    }
  }
  $("#verifyBox").innerHTML = verify.length ? "" : '<div class="empty">暂无待验收事项</div>';
  for (const [r, it] of verify) $("#verifyBox").appendChild(verdictCard(r, it));
  $("#rejectBox").innerHTML = rejected.length ? "" : '<div class="empty">暂无被驳回事项</div>';
  for (const [r, it] of rejected) {
    const el = document.createElement("div");
    el.className = "item";
    el.innerHTML = `<div class="item-head"><b>${esc(it.content)}</b><span class="spacer"></span>
      <span class="code">${esc(r.code)}</span>${badge("rejected", ITEM_BADGE)}
      <button class="btn sm">申请复议</button></div>
      <div class="item-meta">驳回意见：${esc(it.last_verdict?.comment || "")}</div>`;
    $("button", el).addEventListener("click", () => appealModal(it, r));
    $("#rejectBox").appendChild(el);
  }
  const hostMeetings = new Set();
  try {
    const { meetings } = await api("/api/meetings");
    meetings.forEach(m => { if (m.host_id === state.actor) hostMeetings.add(m.id); });
  } catch {}
  const hostAppeals = appeals.filter(([r]) => hostMeetings.has(r.meeting_id));
  $("#appealBox").innerHTML = hostAppeals.length ? "" : '<div class="empty">暂无待裁定复议</div>';
  for (const [r, it, rr] of hostAppeals) {
    const el = document.createElement("div");
    el.className = "item";
    el.innerHTML = `<div class="item-head"><b>${esc(it.content)}</b><span class="spacer"></span>
      <span class="code">${esc(r.code)}</span><span class="badge amber">复议中</span>
      <button class="btn sm secondary">裁定复议</button></div>
      <div class="item-meta">责任人 ${esc(userName(rr.requested_by))} 申请：${esc(rr.reason)}</div>`;
    $("button", el).addEventListener("click", () => decideModal(r, it, rr));
    $("#appealBox").appendChild(el);
  }
}

function verdictCard(r, it) {
  const el = document.createElement("div");
  el.className = "card";
  el.innerHTML = `
    <div class="card-body">
      <div class="row">
        <b>${esc(it.content)}</b><span class="spacer"></span>
        <span class="code">${esc(r.code)}</span>
        <span class="badge teal">第 ${it.current_round} 轮证据待验收</span>
      </div>
      <div class="side-by-side" style="margin-top:10px">
        <div class="original-text" style="font-size:12px">${esc(r.original_text)}</div>
        <div class="evidence-box">
          <a class="file" href="/api/attachments/${it.latest_evidence.id}">📎 ${esc(it.latest_evidence.filename)}</a>
          <div class="muted">${esc(it.latest_evidence.note)} · ${esc(userName(it.assignee_id))} 提交于 ${fmtTime(it.latest_evidence.created_at)}</div>
          <div class="field" style="margin-top:10px"><label>对照决议原文的验收意见</label>
            <textarea rows="3" placeholder="说明与决议原文逐条核对的结果"></textarea></div>
          <div class="row">
            <button class="btn" data-approved="1">验收通过</button>
            <button class="btn danger" data-approved="0">验收不通过</button>
            <span class="spacer"></span>
            <span class="muted">验收人：${it.verifier_ids.map(v => esc(userName(v))).join("、")}</span>
          </div>
        </div>
      </div>
    </div>`;
  $$("button[data-approved]", el).forEach(b => b.addEventListener("click", async () => {
    try {
      await post("/api/items/verdict", {
        item_id: it.id, approved: b.dataset.approved === "1",
        comment: $("textarea", el).value,
      });
      toast(b.dataset.approved === "1" ? "验收通过，执行待办关闭" : "已驳回，执行人可重新提交或申请复议");
      render();
    } catch (e) {
      if (e.status === 409 && e.info) {
        toast(`冲突：${e.message}。已有结论：${e.info.verifier_name} → ${e.info.approved ? "通过" : "不通过"}`, "error");
      } else if (e.status === 403) toast(`越权被拒：${e.message}`, "error");
      else toast(e.message, "error");
    }
  }));
  return el;
}

function verdictModal(it, r) {
  modal(`验收：${it.content}`, `请对照决议原文在详情页核对证据后提交结论。`,
    `<button class="btn ghost" onclick="closeModal()">取消</button>
     <button class="btn" onclick="closeModal();document.querySelector('[data-view=reviews]').click()">前往验收页</button>`);
}

function appealModal(it, r) {
  modal(`申请复议：${it.content}`, `
    <div class="field"><label>复议理由（针对验收不通过意见逐条说明）</label>
      <textarea id="apReason" rows="4"></textarea></div>`,
    `<button class="btn ghost" onclick="closeModal()">取消</button>
     <button class="btn" id="apOk">提交复议申请</button>`);
  $("#apOk").addEventListener("click", async () => {
    try {
      await post("/api/items/review-request", { item_id: it.id, reason: $("#apReason").value });
      closeModal(); toast("复议申请已提交，等待主持人裁定"); render();
    } catch (e) { toast(e.message, "error"); }
  });
}

function decideModal(r, it, rr) {
  modal(`裁定复议：${it.content}`, `
    <dl class="kv">
      <dt>决议</dt><dd>${esc(r.code)} ${esc(r.title)}</dd>
      <dt>驳回意见</dt><dd>${esc(it.last_verdict?.comment || "")}</dd>
      <dt>复议理由</dt><dd>${esc(rr.reason)}</dd>
    </dl>
    <div class="field" style="margin-top:12px"><label>裁定说明</label><textarea id="dcNote" rows="3"></textarea></div>`,
    `<button class="btn ghost" onclick="closeModal()">取消</button>
     <button class="btn danger" id="dcNo">驳回，维持不通过</button>
     <button class="btn" id="dcYes">支持，验收通过</button>`);
  const decide = async ok => {
    try {
      await post("/api/reviews/decide", { request_id: rr.id, approve_appeal: ok, note: $("#dcNote").value });
      closeModal(); toast(ok ? "复议通过，该事项记为验收通过" : "已驳回，维持不通过"); render();
    } catch (e) { toast(e.message, "error"); }
  };
  $("#dcYes").addEventListener("click", () => decide(true));
  $("#dcNo").addEventListener("click", () => decide(false));
}

/* ---------------- 归档检索 ---------------- */
async function viewArchive() {
  const main = $("#main");
  main.innerHTML = `
    <div class="muted" style="margin-bottom:12px">仅检索已归档决议；归档后内容冻结，只允许补充附注。</div>
    <form class="searchbar" id="arcForm">
      <input id="arcQ" placeholder="按决议编号、标题或原文关键词检索…">
      <button class="btn">检索</button>
    </form>
    <div id="arcResults"></div>`;
  const load = async q => {
    const { results } = await api(`/api/archive?q=${encodeURIComponent(q || "")}`);
    $("#arcResults").innerHTML = results.length ? "" : '<div class="empty">没有匹配的归档决议</div>';
    for (const r of results) {
      const el = document.createElement("div");
      el.className = "card";
      el.innerHTML = `
        <div class="card-body">
          <div class="row"><span class="code">${esc(r.code)}</span><b>${esc(r.title)}</b>
            <span class="spacer"></span>
            <span class="muted">归档于 ${fmtTime(r.archived_at)} · ${esc(userName(r.archived_by))}</span>
            <button class="btn sm">查看 / 补充附注</button></div>
          ${r.archive_note ? `<div class="muted" style="margin-top:6px">归档说明：${esc(r.archive_note)}</div>` : ""}
        </div>`;
      $("button", el).addEventListener("click", () => openResolution(r.id));
      $("#arcResults").appendChild(el);
    }
  };
  $("#arcForm").addEventListener("submit", e => { e.preventDefault(); load($("#arcQ").value); });
  load("");
}

/* ---------------- 消息回执 ---------------- */
async function viewMessages() {
  const main = $("#main");
  main.innerHTML = `
    <div class="muted" style="margin-bottom:12px">消息由模拟适配器处理，不真正外发；每条返回模拟回执编号。</div>
    <div class="card"><div id="msgBox" class="card-body"></div></div>`;
  const { messages } = await api("/api/messages");
  $("#msgBox").innerHTML = messages.length ? `<ul class="plain">${messages.map(m => `
    <li><span class="kind-tag">${esc(m.channel)}</span><b>${esc(m.title)}</b>
      <span class="muted">→ ${esc(userName(m.target))} · ${fmtTime(m.created_at)}</span><br>
      ${esc(m.body)}<br><span class="muted">回执：${esc(m.receipt)}</span></li>`).join("")}</ul>`
    : '<div class="empty">暂无消息</div>';
}

init().catch(e => toast(`初始化失败：${e.message}`, "error"));
