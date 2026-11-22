// Chat UI for the orchestrator API. Plain JS, same origin, no dependencies.
// Everything the server returns is rendered with textContent, never as HTML.
"use strict";

const KEY_STORE = "orch.apiKey";
const $ = (id) => document.getElementById(id);
const state = { key: null, me: null, attached: [] };

// ------------------------------------------------------------------ helpers

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

function storeGet() { try { return sessionStorage.getItem(KEY_STORE); } catch { return null; } }
function storeSet(v) { try { v ? sessionStorage.setItem(KEY_STORE, v) : sessionStorage.removeItem(KEY_STORE); } catch { /* private mode */ } }

function newId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return Date.now().toString(36) + Math.random().toString(36).slice(2);
}

function short(value, limit = 4000) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return text.length > limit ? text.slice(0, limit) + "\n... (truncated)" : text;
}

function formatValue(v) {
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 4 });
  if (typeof v === "string") return v;
  if (v && typeof v === "object" && !Array.isArray(v)) {
    const entries = Object.entries(v);
    if (entries.length && entries.length <= 8 && entries.every(([, x]) => x === null || typeof x !== "object")) {
      return entries.map(([k, x]) => `${k}: ${formatValue(x)}`).join("  ·  ");
    }
  }
  return short(v, 600);
}

async function api(method, path, { body, raw, headers = {} } = {}) {
  const init = { method, headers: { Authorization: `Bearer ${state.key}`, ...headers } };
  if (raw !== undefined) init.body = raw;
  else if (body !== undefined) { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (res.status === 401) { signOut("Your API key was not accepted. Sign in again."); throw new Error("unauthorized"); }
  return { ok: res.ok, status: res.status, data };
}

function errorText(data, status) {
  if (data && data.error) return typeof data.error === "string" ? data.error : (data.error.message || data.error.code);
  if (data && data.detail) return typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
  return `Request failed (HTTP ${status}).`;
}

// ------------------------------------------------------------------ sign in

async function signIn(key) {
  state.key = key;
  const res = await fetch("/me", { headers: { Authorization: `Bearer ${key}` } });
  if (!res.ok) { state.key = null; return false; }
  state.me = await res.json();
  storeSet(key);
  $("who").textContent = `${state.me.user_id} · ${state.me.roles.join(", ") || "no roles"}`;
  $("who").hidden = $("signout").hidden = false;
  $("signin").hidden = true;
  $("chat").hidden = false;
  autosize();
  $("text").focus();
  return true;
}

function signOut(message) {
  state.key = state.me = null;
  storeSet(null);
  $("who").hidden = $("signout").hidden = true;
  $("chat").hidden = true;
  $("signin").hidden = false;
  $("key").value = "";
  $("signin-error").hidden = !message;
  $("signin-error").textContent = message || "";
  $("key").focus();
}

// ------------------------------------------------------------------ files

async function attach(file) {
  const path = `users/${state.me.user_id}/${file.name}`;
  const chip = el("span", { class: "chip", text: `Uploading ${file.name}...` });
  $("chips").append(chip);
  try {
    const { ok, status, data } = await api("PUT", `/files/${encodeURI(path)}?overwrite=true`, { raw: file });
    if (!ok) { chip.textContent = `${file.name}: ${errorText(data, status)}`; chip.classList.add("error"); setTimeout(() => chip.remove(), 6000); return; }
    state.attached.push(data.path);
    chip.textContent = data.path;
    chip.append(el("button", { type: "button", title: "Remove", "aria-label": `Remove ${data.path}`, text: "×",
      onclick: () => { state.attached = state.attached.filter((p) => p !== data.path); chip.remove(); } }));
  } catch (e) { chip.remove(); }
}

// ------------------------------------------------------------------ messages

function addMessage(kind, ...children) {
  $("empty")?.remove();
  const node = el("div", { class: `msg ${kind}` }, ...children);
  $("log").append(node);
  node.scrollIntoView({ block: "end", behavior: "smooth" });
  return node;
}

function statusBadge(record) {
  const s = record.state;
  const partial = record.report && record.report.status === "partial";
  const label = s === "completed" ? (partial ? "partly completed" : "completed") : s.replace("_", " ");
  const tone = s === "completed" && !partial ? "ok" : s === "awaiting_approval" || partial ? "warn" : "bad";
  return el("span", { class: `status ${tone}`, text: label });
}

function highlights(record) {
  const box = el("div", { class: "answer" });
  for (const r of (record.report && record.report.task_results) || []) {
    if (r.status !== "succeeded" || !r.output) continue;
    const o = r.output;
    let value = null;
    if ("result" in o) value = formatValue(o.result);
    else if (typeof o.answer === "string") value = o.answer;
    else if (Array.isArray(o.rows)) value = `${o.rows.length} rows from ${o.path || "file"}`;
    else if (Array.isArray(o.results)) value = `${o.results.length} results`;
    if (value == null) continue;
    box.append(el("div", { class: "item" }, el("span", { class: "label", text: r.task_id }), el("span", { class: "value", text: value })));
  }
  return box.childElementCount ? box : null;
}

function steps(record) {
  const results = (record.report && record.report.task_results) || [];
  if (!results.length) return null;
  const box = el("details", {}, el("summary", { text: `Steps (${results.length})` }));
  for (const r of results) {
    const step = el("div", { class: "step" },
      el("div", { class: "head" },
        el("strong", { text: r.task_id }),
        el("code", { text: r.tool_id ? `${r.tool_id}.${r.operation}` : "" }),
        el("span", { class: "muted", text: r.status })));
    if (r.output) step.append(el("pre", { text: short(r.output) }));
    if (r.error) step.append(el("pre", { class: "error", text: short({ code: r.error.code, message: r.error.message, details: r.error.details }) }));
    box.append(step);
  }
  const plan = record.plan && record.plan.tasks;
  if (plan) box.append(el("div", { class: "step" }, el("strong", { text: "Plan" }), el("pre", { text: short(plan) })));
  return box;
}

async function approvalCard(record, taskId, approvalId, onDecided) {
  const card = el("div", { class: "approval" }, el("strong", { text: `Approval needed: ${taskId}` }));
  const { ok, data } = await api("GET", `/approvals/${encodeURIComponent(approvalId)}`);
  if (ok) {
    card.append(el("code", { text: `${data.tool_id}.${data.operation}${data.resource ? " on " + data.resource : ""}` }));
    card.append(el("pre", { text: data.args_preview }));
  }
  const canApprove = state.me.roles.includes("approver");
  if (!canApprove) {
    card.append(el("span", { class: "muted", text: "Waiting for someone with the approver role. Ask them to approve it, then press Continue." }));
    return card;
  }
  const actions = el("div", { class: "actions" });
  const decide = async (approve) => {
    for (const b of actions.querySelectorAll("button")) b.disabled = true;
    const res = await api("POST", `/approvals/${encodeURIComponent(approvalId)}/${approve ? "approve" : "deny"}`, { body: {} });
    if (!res.ok) { actions.append(el("span", { class: "error", text: errorText(res.data, res.status) })); return; }
    actions.replaceChildren(el("span", { class: approve ? "status ok" : "status bad", text: approve ? "approved" : "denied" }));
    onDecided();
  };
  actions.append(
    el("button", { type: "button", class: "primary", text: "Approve", onclick: () => decide(true) }),
    el("button", { type: "button", class: "secondary", text: "Deny", onclick: () => decide(false) }));
  card.append(actions);
  return card;
}

async function renderRecord(bubble, record, status) {
  bubble.replaceChildren();
  if (!record || !record.state) {
    bubble.append(el("p", { class: "error", text: errorText(record, status) }));
    return;
  }
  bubble.append(statusBadge(record));
  const h = highlights(record);
  if (h) bubble.append(h);
  if (record.summary) bubble.append(el("p", { class: "summary", text: record.summary }));
  else if (record.error) bubble.append(el("p", { class: "error", text: record.error.message || record.error.code }));

  const pending = Object.entries(record.pending_approvals || {});
  if (pending.length) {
    let left = pending.length;
    const cont = el("button", { type: "button", class: "primary", text: "Continue", hidden: "" });
    const decided = () => { if (--left === 0) cont.hidden = false; };
    for (const [taskId, approvalId] of pending) bubble.append(await approvalCard(record, taskId, approvalId, decided));
    if (!state.me.roles.includes("approver")) cont.hidden = false;
    cont.addEventListener("click", () => resume(record.request_id, cont));
    bubble.append(el("div", { class: "approval-continue" }, cont));
  }
  const s = steps(record);
  if (s) bubble.append(s);
}

async function resume(requestId, button) {
  button.disabled = true;
  const bubble = addMessage("agent", el("span", { class: "thinking", text: "Continuing" }));
  try {
    const { status, data } = await api("POST", `/requests/${encodeURIComponent(requestId)}/resume`);
    await renderRecord(bubble, data, status);
  } catch (e) {
    bubble.replaceChildren(el("p", { class: "error", text: e.message === "unauthorized" ? "Signed out." : "Could not reach the server." }));
  }
  bubble.scrollIntoView({ block: "end", behavior: "smooth" });
}

async function send(text) {
  const files = [...state.attached];
  addMessage("user", text, files.length ? el("div", { class: "files", text: `Files: ${files.join(", ")}` }) : null);
  const bubble = addMessage("agent", el("span", { class: "thinking", text: "Planning and running" }));
  $("send").disabled = true;
  try {
    const { status, data } = await api("POST", "/requests", {
      body: { text, files, allow_partial: $("partial").checked },
      headers: { "Idempotency-Key": newId() },
    });
    await renderRecord(bubble, data, status);
  } catch (e) {
    bubble.replaceChildren(el("p", { class: "error", text: e.message === "unauthorized" ? "Signed out." : "Could not reach the server." }));
  } finally {
    $("send").disabled = false;
    bubble.scrollIntoView({ block: "end", behavior: "smooth" });
  }
}

// ------------------------------------------------------------------ wiring

function autosize() {
  const t = $("text");
  t.style.height = "auto";
  t.style.height = `${t.scrollHeight + 2}px`;
  t.style.overflowY = t.scrollHeight > 200 ? "auto" : "hidden";
}

document.addEventListener("DOMContentLoaded", async () => {
  $("signin-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const ok = await signIn($("key").value.trim()).catch(() => false);
    if (!ok) { $("signin-error").textContent = "That API key was not accepted."; $("signin-error").hidden = false; }
  });
  $("signout").addEventListener("click", () => signOut());
  $("file").addEventListener("change", (ev) => { for (const f of ev.target.files) attach(f); ev.target.value = ""; });
  $("text").addEventListener("input", autosize);
  $("text").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); $("composer").requestSubmit(); }
  });
  $("composer").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const text = $("text").value.trim();
    if (!text || $("send").disabled) return;
    $("text").value = "";
    autosize();
    send(text);
  });

  const saved = storeGet();
  if (!(saved && await signIn(saved).catch(() => false))) signOut();
});
