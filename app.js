/* OpsBoard frontend — dependency-free.
 *
 * State-management rules this file follows:
 *  1. The server is the source of truth. Local state is a cache plus a few
 *     explicitly "pending" optimistic entries.
 *  2. Every mutation of a work item sends the `version` the user was looking at.
 *     A 409 response carries the current server state; we reconcile with it
 *     instead of guessing.
 *  3. Retry-prone actions (create, claim, transition, comment) carry an
 *     Idempotency-Key generated ONCE per user intent and reused on retry, so a
 *     flaky network can never create two items or two comments.
 *  4. List requests are sequence-numbered; a slow, older response can never
 *     overwrite a newer one (classic search-as-you-type race).
 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const label = (s) => String(s || "").replace(/_/g, " ");
const fmt = (iso) => (iso ? new Date(iso).toLocaleString() : "—");
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : Date.now() + "-" + Math.random().toString(16).slice(2));

const state = {
  token: localStorage.getItem("opsboard_token"),
  me: null,
  list: { items: [], cursor: null, seq: 0, loading: false },
  sel: null, // { item, activity: [], lastActivityId, dirty, stale, conflict }
  pollTimer: null,
};

/* ------------------------------------------------------------------ API */

class ApiError extends Error {
  constructor(status, body) {
    const e = (body && body.error) || {};
    super(e.message || `HTTP ${status}`);
    this.status = status; this.code = e.code || "http_error"; this.current = e.current; this.details = e.details;
  }
}

async function api(method, path, body, { idemKey, retries = 0 } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (idemKey) headers["Idempotency-Key"] = idemKey;
  for (let attempt = 0; ; attempt++) {
    let res;
    try {
      res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (netErr) {
      // Network failure: we don't know whether the server applied the request.
      // Only safe to retry automatically when the request is idempotent.
      if ((idemKey || method === "GET") && attempt < retries) { await sleep(400 * 2 ** attempt); continue; }
      throw new ApiError(0, { error: { code: "network", message: "Network error — the action may or may not have been applied. Retry is safe." } });
    }
    if (res.status === 401 && path !== "/api/login") { logout(); throw new ApiError(401, await res.json().catch(() => null)); }
    if (res.status >= 500 && (idemKey || method === "GET") && attempt < retries) { await sleep(400 * 2 ** attempt); continue; }
    const data = res.status === 204 ? null : await res.json().catch(() => null);
    if (!res.ok) throw new ApiError(res.status, data);
    return data;
  }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function toast(msg, isErr = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), isErr ? 6000 : 3500);
}

/* ------------------------------------------------------------------ auth */

async function boot() {
  if (!state.token) return showLogin();
  try {
    state.me = await api("GET", "/api/me");
  } catch { return showLogin(); }
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  const roles = state.me.is_admin ? "admin" : state.me.teams.map((t) => `${t.name} (${t.role})`).join(", ");
  $("#me-label").textContent = `${state.me.name} · ${roles}`;
  const teamOpts = state.me.teams.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
  $("#f-team").innerHTML = `<option value="">All my teams</option>${teamOpts}`;
  const writable = state.me.teams.filter((t) => t.role !== "viewer");
  $("#new-form [name=team_id]").innerHTML = writable.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
  $("#new-item-btn").disabled = writable.length === 0;
  await Promise.all([loadDashboard(), loadList(true), loadNotifications()]);
  setInterval(() => { loadDashboard(); loadNotifications(); }, 15000);
}

function showLogin() {
  $("#app-view").classList.add("hidden");
  $("#login-view").classList.remove("hidden");
}

function logout() {
  api("POST", "/api/logout").catch(() => {});
  localStorage.removeItem("opsboard_token");
  state.token = null;
  location.reload();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    const { token } = await api("POST", "/api/login", { email: $("#login-email").value, password: $("#login-password").value });
    state.token = token; localStorage.setItem("opsboard_token", token);
    boot();
  } catch (err) { $("#login-error").textContent = err.message; }
});
$("#logout-btn").addEventListener("click", logout);

/* ------------------------------------------------------------------ dashboard */

const TILES = [
  ["my_active", "My active work", { assignee: "me", status: "active" }],
  ["unassigned", "Unassigned (needs owner)", { assignee: "unassigned", status: "active" }],
  ["p1_active", "P1 active", { priority: "P1", status: "active" }],
  ["awaiting_my_approval", "Awaiting approval", { status: "awaiting_approval" }],
  ["overdue", "Overdue", { status: "active", overdue: true }],
];

async function loadDashboard() {
  try {
    const d = await api("GET", "/api/dashboard", undefined, { retries: 1 });
    $("#tiles").innerHTML = TILES.map(([k, text]) =>
      `<button class="card tile ${(k === "p1_active" || k === "overdue") && d[k] ? "hot" : ""}" data-tile="${k}">
         <span class="num">${d[k]}</span>${esc(text)}</button>`).join("");
  } catch { /* non-critical */ }
}

$("#tiles").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-tile]");
  if (!btn) return;
  const [, , f] = TILES.find(([k]) => k === btn.dataset.tile);
  $("#f-assignee").value = f.assignee || "";
  $("#f-status").value = f.status ?? "";
  $("#f-priority").value = f.priority || "";
  $("#f-overdue").checked = !!f.overdue;
  $("#f-q").value = "";
  $("#f-sort").value = "priority";
  loadList(true);
});

/* ------------------------------------------------------------------ list */

function filterParams() {
  const p = new URLSearchParams();
  const v = (id) => $(id).value.trim();
  if (v("#f-q")) p.set("q", v("#f-q"));
  if (v("#f-team")) p.set("team_id", v("#f-team"));
  if (v("#f-status")) p.append("status", v("#f-status"));
  if (v("#f-priority")) p.append("priority", v("#f-priority"));
  if (v("#f-assignee")) p.set("assignee", v("#f-assignee"));
  if ($("#f-overdue").checked) p.set("overdue", "true");
  p.set("sort", v("#f-sort"));
  p.set("limit", "30");
  return p;
}

async function loadList(reset) {
  const seq = ++state.list.seq; // newest request wins
  const p = filterParams();
  if (!reset && state.list.cursor) p.set("cursor", state.list.cursor);
  $("#list-status").textContent = "Loading…";
  try {
    const res = await api("GET", "/api/items?" + p.toString(), undefined, { retries: 1 });
    if (seq !== state.list.seq) return; // a newer search started; drop this stale response
    state.list.items = reset ? res.items : state.list.items.concat(res.items);
    state.list.cursor = res.next_cursor;
    renderList();
    $("#list-status").textContent = state.list.items.length ? "" : "No matching work items.";
  } catch (err) {
    if (seq === state.list.seq) $("#list-status").textContent = "Failed to load: " + err.message;
  }
}

function rowHtml(it) {
  const owner = it.assignee_name ? esc(it.assignee_name) : "<em>unassigned</em>";
  const overdue = it.due_at && new Date(it.due_at) < new Date() && !["resolved", "closed"].includes(it.status);
  return `<li data-id="${it.id}" class="${state.sel?.item.id === it.id ? "selected" : ""}">
    <span class="prio ${it.priority}">${it.priority}</span>
    <div><div class="title">${esc(it.title)}</div>
      <div class="meta">#${it.id} · ${esc(it.team_name)} · ${label(it.kind)} · ${owner}${overdue ? ' · <b style="color:var(--danger)">overdue</b>' : ""}</div></div>
    <span class="status">${label(it.status)}</span></li>`;
}

function renderList() {
  $("#item-list").innerHTML = state.list.items.map(rowHtml).join("");
  $("#load-more").classList.toggle("hidden", !state.list.cursor);
}

function patchListRow(item) {
  const i = state.list.items.findIndex((x) => x.id === item.id);
  if (i >= 0) { state.list.items[i] = { ...state.list.items[i], ...item }; renderList(); }
}

let debounce;
$("#f-q").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(() => loadList(true), 250); });
for (const id of ["#f-team", "#f-status", "#f-priority", "#f-assignee", "#f-sort", "#f-overdue"]) {
  $(id).addEventListener("change", () => loadList(true));
}
$("#load-more").addEventListener("click", () => loadList(false));
$("#item-list").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-id]");
  if (li) openItem(Number(li.dataset.id));
});

/* ------------------------------------------------------------------ detail */

async function openItem(id) {
  if (state.sel?.dirty && state.sel.item.id !== id && !confirm("Discard your unsaved edits?")) return;
  clearInterval(state.pollTimer);
  $("#detail").innerHTML = '<p class="muted">Loading…</p>';
  try {
    const [item, act] = await Promise.all([
      api("GET", `/api/items/${id}`, undefined, { retries: 1 }),
      api("GET", `/api/items/${id}/activity?limit=50`, undefined, { retries: 1 }),
    ]);
    state.sel = { item, activity: act.items.slice().reverse(), nextBefore: act.next_before,
                  lastActivityId: act.items.length ? act.items[0].id : 0, dirty: false, stale: null, conflict: null, pending: [] };
    renderDetail();
    renderList();
    state.pollTimer = setInterval(pollSelected, 4000);
  } catch (err) {
    $("#detail").innerHTML = `<p class="error">${esc(err.message)}</p>`;
  }
}

/* Poll for changes made by other people. Comments don't bump the version, so
 * activity is polled incrementally (after=lastActivityId) as well. */
async function pollSelected() {
  const sel = state.sel;
  if (!sel || document.hidden) return;
  try {
    const [item, act] = await Promise.all([
      api("GET", `/api/items/${sel.item.id}`),
      api("GET", `/api/items/${sel.item.id}/activity?after=${sel.lastActivityId}`),
    ]);
    if (state.sel !== sel) return;
    if (act.items.length) {
      sel.activity.push(...act.items.filter((a) => !sel.activity.some((x) => x.id === a.id)));
      sel.lastActivityId = act.items[act.items.length - 1].id;
    }
    if (item.version !== sel.item.version) {
      if (sel.dirty) {
        sel.stale = item; // don't clobber the user's typing — warn instead
      } else {
        sel.item = item;
        patchListRow(item);
      }
    }
    if (act.items.length || item.version !== sel.item.version || sel.stale) renderDetail(true);
  } catch { /* transient; next tick retries */ }
}

function renderDetail(preserveForm = false) {
  const sel = state.sel; if (!sel) return;
  const it = sel.item, perm = it.permissions || {};
  const draft = preserveForm && sel.dirty ? readForm() : null;
  const transitions = (perm.transitions || []).map((to) =>
    `<button data-to="${to}" class="${to === "rejected" ? "danger" : ""}">→ ${label(to)}</button>`).join("");

  $("#detail").innerHTML = `
    <div class="detail-head">
      <h2>${esc(it.title)}</h2>
      <span class="prio ${it.priority}">${it.priority}</span>
      <span class="status">${label(it.status)}</span>
      <span class="muted">#${it.id} · v${it.version}</span>
    </div>
    ${sel.stale ? `<div class="banner">⚠ ${esc(staleWho())} changed this item while you were editing (now v${sel.stale.version}).
        <button id="stale-load">Discard my edits & load latest</button></div>` : ""}
    ${sel.conflict ? conflictHtml(sel.conflict) : ""}
    <dl class="facts">
      <dt>Team</dt><dd>${esc(it.team_name)}</dd>
      <dt>Kind</dt><dd>${label(it.kind)}</dd>
      <dt>Owner</dt><dd>${it.assignee_name ? esc(it.assignee_name) : "<em>Unassigned</em>"} ${sel.pendingClaim ? '<span class="muted">(claiming…)</span>' : ""}</dd>
      <dt>Requested by</dt><dd>${esc(it.creator_name)}</dd>
      <dt>Approval</dt><dd>${it.requires_approval ? "Required" : "Not required"}</dd>
      <dt>Due</dt><dd>${it.due_at ? fmt(it.due_at) : "—"}</dd>
      <dt>Updated</dt><dd>${fmt(it.updated_at)}</dd>
    </dl>
    <div class="actions">
      ${perm.claim ? '<button id="claim-btn" class="primary">Take ownership</button>' : ""}
      ${perm.release ? '<button id="release-btn">Release</button>' : ""}
      ${transitions}
      ${perm.assign ? '<select id="assign-sel"><option value="">Assign to…</option></select>' : ""}
    </div>
    ${perm.edit ? editFormHtml(it, perm) : `<p class="pre">${esc(it.description) || '<span class="muted">No description</span>'}</p>`}
    <div class="section-title">Discussion & history</div>
    ${perm.comment ? `<div class="stack"><textarea id="comment-body" rows="2" placeholder="Add a comment…"></textarea>
       <div class="row end"><button id="comment-btn">Comment</button></div></div>` : ""}
    <ul class="timeline">${sel.pending.map(pendingHtml).join("")}${sel.activity.slice().reverse().map(activityHtml).join("")}</ul>
    ${sel.nextBefore ? '<button id="older-btn">Older history</button>' : ""}
  `;
  if (draft) writeForm(draft);
  bindDetail();
}

function staleWho() {
  const last = state.sel.activity[state.sel.activity.length - 1];
  return last ? last.actor_name : "Someone";
}

function editFormHtml(it, perm) {
  return `<form id="edit-form" class="stack">
    <label>Title <input name="title" value="${esc(it.title)}" maxlength="200" required></label>
    <label>Description <textarea name="description" rows="4">${esc(it.description)}</textarea></label>
    <div class="row">
      <label>Priority <select name="priority">${["P1", "P2", "P3", "P4"].map((p) => `<option ${p === it.priority ? "selected" : ""}>${p}</option>`).join("")}</select></label>
      <label>Due <input name="due_at" type="date" value="${it.due_at ? it.due_at.slice(0, 10) : ""}"></label>
      ${perm.change_approval ? `<label class="inline"><input name="requires_approval" type="checkbox" ${it.requires_approval ? "checked" : ""}> Requires approval</label>` : ""}
    </div>
    <div class="row end"><span class="muted" id="dirty-label">${state.sel.dirty ? "Unsaved changes" : ""}</span>
      <button type="submit" class="primary" ${state.sel.dirty ? "" : "disabled"}>Save</button></div>
  </form>`;
}

function conflictHtml(c) {
  const fields = Object.keys(c.mine).filter((k) => String(c.theirs[k] ?? "") !== String(c.base[k] ?? ""));
  return `<div class="banner"><div style="flex:1 1 100%">
      <b>Your save was rejected:</b> someone saved a newer version (v${c.theirs.version}) first.
      ${fields.length ? `They changed: ${fields.map(esc).join(", ")}.` : "They changed other fields or its state."}</div>
      <button id="conflict-theirs">Discard mine, use theirs</button>
      <button id="conflict-mine">Re-apply my changes on top</button></div>`;
}

function activityHtml(a) {
  const d = a.data; let text;
  switch (a.type) {
    case "created": text = "created this item"; break;
    case "commented": text = "commented"; break;
    case "claimed": text = "took ownership"; break;
    case "assigned": text = "changed owner" + (d.status ? ` (status → ${label(d.status[1])})` : ""); break;
    case "transitioned": text = `moved ${label(d.status[0])} → <b>${label(d.status[1])}</b>`; break;
    case "updated": text = "changed " + Object.entries(d).map(([k, [o, n]]) => `${esc(k)}: ${esc(short(o))} → ${esc(short(n))}`).join("; "); break;
    default: text = esc(a.type);
  }
  const body = d.body || d.comment;
  return `<li><b>${esc(a.actor_name)}</b> ${text} <span class="when">· ${fmt(a.created_at)}</span>
    ${body ? `<div class="body">${esc(body)}</div>` : ""}</li>`;
}
const short = (v) => { const s = v === null || v === undefined ? "—" : String(v); return s.length > 40 ? s.slice(0, 40) + "…" : s; };

function pendingHtml(p) {
  return `<li class="pending"><b>${esc(state.me.name)}</b> commented <span class="when">· ${p.failed ? "failed to send" : "sending…"}</span>
    <div class="body">${esc(p.body)}</div>${p.failed ? `<button data-retry="${p.key}">Retry</button>` : ""}</li>`;
}

function readForm() {
  // Use .elements: on the <form> itself, `form.title` is the form's title attribute, not the input.
  const form = $("#edit-form"); if (!form) return null;
  const f = form.elements;
  const out = { title: f.title.value.trim(), description: f.description.value, priority: f.priority.value,
                due_at: f.due_at.value ? new Date(f.due_at.value + "T00:00:00Z").toISOString() : null };
  if (f.requires_approval) out.requires_approval = f.requires_approval.checked;
  return out;
}
function writeForm(d) {
  const form = $("#edit-form"); if (!form || !d) return;
  const f = form.elements;
  f.title.value = d.title; f.description.value = d.description; f.priority.value = d.priority;
  f.due_at.value = d.due_at ? d.due_at.slice(0, 10) : "";
  if (f.requires_approval && "requires_approval" in d) f.requires_approval.checked = d.requires_approval;
}
function changedFields(base, draft) {
  const out = {};
  for (const [k, v] of Object.entries(draft)) {
    const b = k === "due_at" ? (base.due_at ? base.due_at.slice(0, 10) : null) : base[k];
    const n = k === "due_at" ? (v ? v.slice(0, 10) : null) : v;
    if (String(b ?? "") !== String(n ?? "")) out[k] = v;
  }
  return out;
}

function bindDetail() {
  const sel = state.sel, it = sel.item;

  $("#edit-form")?.addEventListener("input", () => {
    sel.dirty = Object.keys(changedFields(sel.item, readForm())).length > 0;
    $("#edit-form button[type=submit]").disabled = !sel.dirty;
    $("#dirty-label").textContent = sel.dirty ? "Unsaved changes" : "";
  });
  $("#edit-form")?.addEventListener("submit", (e) => { e.preventDefault(); saveEdits(changedFields(sel.item, readForm()), sel.item); });

  $("#stale-load")?.addEventListener("click", () => { sel.item = sel.stale; sel.stale = null; sel.dirty = false; patchListRow(sel.item); renderDetail(); });
  $("#conflict-theirs")?.addEventListener("click", () => { sel.item = sel.conflict.theirs; sel.conflict = null; sel.dirty = false; patchListRow(sel.item); renderDetail(); });
  $("#conflict-mine")?.addEventListener("click", () => {
    const { mine, theirs } = sel.conflict;
    sel.item = theirs; sel.conflict = null; renderDetail();
    saveEdits(mine, theirs); // deliberate overwrite, now against the version the user has SEEN
  });

  $("#claim-btn")?.addEventListener("click", claim);
  $("#release-btn")?.addEventListener("click", () => assign(null));
  for (const b of document.querySelectorAll("[data-to]")) b.addEventListener("click", () => transition(b.dataset.to));
  $("#comment-btn")?.addEventListener("click", () => {
    const body = $("#comment-body").value.trim();
    if (!body) return;
    $("#comment-body").value = "";
    sendComment({ key: uuid(), body, failed: false });
  });
  for (const b of document.querySelectorAll("[data-retry]")) {
    b.addEventListener("click", () => {
      const p = sel.pending.find((x) => x.key === b.dataset.retry);
      if (p) { p.failed = false; sel.pending = sel.pending.filter((x) => x !== p); sendComment(p); }
    });
  }
  $("#older-btn")?.addEventListener("click", async () => {
    const res = await api("GET", `/api/items/${it.id}/activity?before=${sel.nextBefore}&limit=50`);
    sel.activity.unshift(...res.items.slice().reverse());
    sel.nextBefore = res.next_before;
    renderDetail(true);
  });
  const assignSel = $("#assign-sel");
  if (assignSel) {
    api("GET", `/api/teams/${it.team_id}/members`).then((members) => {
      assignSel.innerHTML = '<option value="">Assign to…</option>' + members.filter((m) => m.role !== "viewer")
        .map((m) => `<option value="${m.id}" ${m.id === it.assignee_id ? "disabled" : ""}>${esc(m.name)} (${m.role})</option>`).join("");
    }).catch(() => {});
    assignSel.addEventListener("change", () => assignSel.value && assign(Number(assignSel.value)));
  }
}

/* ------------------------------------------------------------------ mutations */

function applyServerItem(item) {
  if (!state.sel || state.sel.item.id !== item.id) return;
  const keepPerms = state.sel.item.permissions;
  state.sel.item = item;
  patchListRow(item);
  // Mutation responses don't include permission hints; refetch them, then re-render.
  state.sel.item.permissions = keepPerms;
  renderDetail(true);
  api("GET", `/api/items/${item.id}`).then((fresh) => {
    if (state.sel?.item.id === fresh.id && fresh.version >= state.sel.item.version) { state.sel.item = fresh; renderDetail(true); }
  }).catch(() => {});
  pollSelected();
}

async function saveEdits(changes, base) {
  const sel = state.sel;
  if (!Object.keys(changes).length) return;
  try {
    const item = await api("PATCH", `/api/items/${base.id}`, { version: base.version, ...changes });
    sel.dirty = false; sel.stale = null;
    toast("Saved");
    applyServerItem(item);
    renderDetail();
  } catch (err) {
    if (err.code === "version_conflict" && err.current) {
      sel.conflict = { mine: changes, theirs: err.current, base };
      sel.stale = null;
      renderDetail(true);
    } else toast(err.message, true);
  }
}

/* Optimistic claim: show me as owner immediately, then reconcile with the
 * server's decision. If someone else won the race, roll back to THEIR state. */
async function claim() {
  const sel = state.sel, before = sel.item;
  sel.item = { ...before, assignee_id: state.me.id, assignee_name: state.me.name,
               permissions: { ...before.permissions, claim: false } };
  sel.pendingClaim = true; renderDetail(true);
  try {
    const item = await api("POST", `/api/items/${before.id}/claim`, undefined, { idemKey: uuid(), retries: 2 });
    sel.pendingClaim = false;
    toast(item.already_owned ? "You already own this item" : "You now own this item");
    applyServerItem(item);
    loadDashboard();
  } catch (err) {
    sel.pendingClaim = false;
    sel.item = err.current ? { ...err.current, permissions: before.permissions } : before;
    if (err.current) applyServerItem(err.current); else renderDetail(true);
    toast(err.code === "already_claimed" ? `Too late — ${err.message.toLowerCase()}` : err.message, true);
  }
}

async function assign(userId) {
  const sel = state.sel;
  try {
    const item = await api("POST", `/api/items/${sel.item.id}/assign`, { version: sel.item.version, assignee_id: userId });
    toast(userId ? "Owner changed" : "Released");
    applyServerItem(item); loadDashboard();
  } catch (err) { handleMutationError(err); }
}

async function transition(to) {
  const sel = state.sel;
  let comment = null;
  if (to === "rejected") {
    comment = prompt("Reason for rejection (required):");
    if (!comment) return;
  }
  const key = uuid(); // one key per click; reused by automatic retries
  try {
    const item = await api("POST", `/api/items/${sel.item.id}/transition`, { version: sel.item.version, to, comment },
                           { idemKey: key, retries: 2 });
    toast(`Moved to ${label(to)}`);
    applyServerItem(item); loadDashboard();
  } catch (err) { handleMutationError(err); }
}

async function sendComment(p) {
  const sel = state.sel;
  sel.pending.push(p); renderDetail(true);
  try {
    const a = await api("POST", `/api/items/${sel.item.id}/comments`, { body: p.body }, { idemKey: p.key, retries: 2 });
    sel.pending = sel.pending.filter((x) => x !== p);
    if (!sel.activity.some((x) => x.id === a.id)) sel.activity.push(a);
    sel.lastActivityId = Math.max(sel.lastActivityId, a.id);
    renderDetail(true);
  } catch (err) {
    p.failed = true; renderDetail(true);
    toast(err.message, true);
  }
}

function handleMutationError(err) {
  const sel = state.sel;
  if (err.status === 409 && err.current) {
    toast("This item was changed by someone else — showing the latest version. Please review and retry.", true);
    sel.item = { ...err.current, permissions: sel.item.permissions };
    applyServerItem(err.current);
  } else toast(err.message, true);
}

/* ------------------------------------------------------------------ create */

let createKey = null; // one key per dialog session, so a double-submit can't create two items
$("#new-item-btn").addEventListener("click", () => {
  $("#new-form").reset(); $("#new-error").textContent = ""; createKey = uuid();
  $("#new-dialog").showModal();
});
$("#new-form").addEventListener("submit", async (e) => {
  if (e.submitter?.value === "cancel") return;
  e.preventDefault();
  const f = e.target.elements, btn = $("#new-submit");
  const body = {
    team_id: Number(f.team_id.value), title: f.title.value.trim(), description: f.description.value,
    kind: f.kind.value, priority: f.priority.value, requires_approval: f.requires_approval.checked,
    assignee_id: f.assign_me.checked ? state.me.id : null,
    due_at: f.due_at.value ? new Date(f.due_at.value + "T00:00:00Z").toISOString() : null,
  };
  btn.disabled = true;
  try {
    const item = await api("POST", "/api/items", body, { idemKey: createKey, retries: 2 });
    $("#new-dialog").close();
    toast(item.idempotent_replay ? "Already created (duplicate submit ignored)" : `Created #${item.id}`);
    await loadList(true); loadDashboard(); openItem(item.id);
  } catch (err) {
    $("#new-error").textContent = err.message;
  } finally { btn.disabled = false; }
});

/* ------------------------------------------------------------------ notifications */

async function loadNotifications() {
  try {
    const n = await api("GET", "/api/notifications?limit=20");
    $("#notif-count").textContent = n.unread;
    $("#notif-count").classList.toggle("hidden", !n.unread);
    $("#notif-panel").innerHTML = n.items.length
      ? `<div class="row end"><button id="notif-all">Mark all read</button></div>` + n.items.map((x) =>
          `<div class="n ${x.read ? "" : "unread"}" data-item="${x.item_id}" data-nid="${x.id}">${esc(x.message)}
           <div class="muted">${fmt(x.created_at)}</div></div>`).join("")
      : '<p class="muted">No notifications yet.</p>';
  } catch { /* non-critical */ }
}
$("#notif-btn").addEventListener("click", () => $("#notif-panel").classList.toggle("hidden"));
$("#notif-panel").addEventListener("click", async (e) => {
  if (e.target.id === "notif-all") { await api("POST", "/api/notifications/read", { ids: null }); return loadNotifications(); }
  const n = e.target.closest("[data-item]");
  if (!n) return;
  $("#notif-panel").classList.add("hidden");
  api("POST", "/api/notifications/read", { ids: [Number(n.dataset.nid)] }).then(loadNotifications);
  openItem(Number(n.dataset.item));
});

window.addEventListener("beforeunload", (e) => { if (state.sel?.dirty) { e.preventDefault(); e.returnValue = ""; } });
boot();
