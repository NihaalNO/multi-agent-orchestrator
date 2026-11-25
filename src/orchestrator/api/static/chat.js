// Chat UI for the orchestrator API. Plain JS, same origin, no dependencies.
// Everything the server returns is rendered with textContent, never as HTML.
"use strict";

const KEY_STORE = "orch.apiKey";
const $ = (id) => document.getElementById(id);
// history: ids of this conversation's completed requests, oldest first. Follow-ups send the last
// MAX_CONTEXT of them so the agent can use their results ("now double it").
const MAX_CONTEXT = 5;
const state = { key: null, me: null, attached: [], history: [] };

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

// Keys of LLM providers, recognised by their published prefixes. They are never sent anywhere:
// the server holds its own provider key; users sign in with their orchestrator key.
const PROVIDER_KEYS = [
  [/^gsk_/, "Groq"],
  [/^sk-ant-/, "Anthropic"],
  [/^AIza/, "Google Gemini"],
  [/^tvly-/, "Tavily"],
  [/^sk-(proj-)?[A-Za-z0-9_-]{20,}$/, "OpenAI"],
];

// Orchestrator keys (`hash-key --generate`, dev server) are secrets.token_urlsafe(32): exactly 43
// URL-safe characters, a length no provider key above has. Never mistake one for a provider key.
const ORCHESTRATOR_KEY = /^[A-Za-z0-9_-]{43}$/;

function providerOf(key) {
  if (ORCHESTRATOR_KEY.test(key)) return null;
  for (const [pattern, name] of PROVIDER_KEYS) if (pattern.test(key)) return name;
  return null;
}

async function signIn(key) {
  state.key = key;
  const res = await fetch("/me", { headers: { Authorization: `Bearer ${key}` } });
  if (!res.ok) { state.key = null; return false; }
  state.me = await res.json();
  storeSet(key);
  $("who").textContent = `${state.me.user_id} · ${state.me.roles.join(", ") || "no roles"}`;
  $("who").hidden = $("signout").hidden = false;
  $("signin").hidden = true;
  resetConversation();
  $("chat").hidden = false;
  autosize();
  $("text").focus();
  return true;
}

// A fresh, empty conversation. Called on every sign-in and sign-out, so nothing one user saw or
// attached is left on the page for the next person using this tab.
function resetConversation() {
  state.attached = [];
  state.history = [];
  $("chips").replaceChildren();
  $("text").value = "";
  const empty = $("empty-template").content.cloneNode(true);
  for (const node of empty.querySelectorAll("[data-user]")) node.textContent = state.me ? state.me.user_id : "you";
  $("log").replaceChildren(empty);
}

function signOut(message) {
  state.key = state.me = null;
  storeSet(null);
  resetConversation();
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

// "Planning and running (12s)": LLM planning can take a while (provider rate limits, retries),
// so show that the request is still alive. The timer stops when the bubble is replaced.
function working(label) {
  const node = el("span", { class: "thinking", text: label });
  const started = Date.now();
  const timer = setInterval(() => {
    if (!node.isConnected) return clearInterval(timer);
    const s = Math.round((Date.now() - started) / 1000);
    if (s >= 3) node.textContent = `${label} (${s}s)`;
  }, 1000);
  return node;
}

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
    else if (Array.isArray(o.results)) {
      box.append(el("div", { class: "item" }, el("span", { class: "label", text: r.task_id }),
        el("span", { class: "muted", text: `${o.results.length} web results${o.backend ? " via " + o.backend : ""}` })));
      if (o.results.length) box.append(sources(o.results));
      continue;
    }
    if (value == null) continue;
    box.append(el("div", { class: "item" }, el("span", { class: "label", text: r.task_id }), el("span", { class: "value", text: value })));
  }
  return box.childElementCount ? box : null;
}

// ------------------------------------------------------------------ grounded answer

const CLAIM_STATUS = {
  supported: ["ok", "✓ checked", "Every number matches the evidence it cites."],
  unverified: ["warn", "⚠ unverified", "Code could not confirm this against the cited evidence."],
  uncited: ["bad", "✗ no evidence", "This statement cites no existing evidence."],
};

// Answer text with [E1] citations turned into chips that jump to the evidence item.
function citedText(text, anchor) {
  const p = el("p", { class: "grounded" });
  let last = 0;
  for (const m of text.matchAll(/\[(E\d+)(\?)?\]/g)) {
    p.append(text.slice(last, m.index));
    if (m[2]) p.append(el("span", { class: "cite bad", title: "This citation points to no evidence", text: `${m[1]}?` }));
    else p.append(el("a", { class: "cite", href: `#${anchor}-${m[1]}`, text: m[1],
      onclick: (ev) => { ev.preventDefault(); showEvidence(anchor, m[1]); } }));
    last = m.index + m[0].length;
  }
  p.append(text.slice(last));
  return p;
}

function showEvidence(anchor, id) {
  const item = document.getElementById(`${anchor}-${id}`);
  if (!item) return;
  item.closest("details").open = true;
  item.scrollIntoView({ block: "center", behavior: "smooth" });
  item.classList.add("flash");
  setTimeout(() => item.classList.remove("flash"), 1600);
}

function groundedAnswer(answer, requestId) {
  const anchor = `ev-${requestId}`;
  const box = el("div", { class: "answer-block" }, citedText(answer.text, anchor));

  const claims = answer.claims || [];
  const ok = claims.filter((c) => c.status === "supported").length;
  const why = el("details", { class: "why" }, el("summary", {
    text: `How this answer is supported: ${ok} of ${claims.length} claim${claims.length === 1 ? "" : "s"} checked` }));
  const list = el("ul", { class: "claims" });
  for (const c of claims) {
    const [tone, label, help] = CLAIM_STATUS[c.status] || CLAIM_STATUS.uncited;
    const refs = el("span", { class: "refs" });
    for (const id of c.evidence || []) refs.append(el("a", { class: "cite", href: `#${anchor}-${id}`, text: id,
      onclick: (ev) => { ev.preventDefault(); showEvidence(anchor, id); } }));
    list.append(el("li", {}, el("span", { class: `status ${tone}`, title: help, text: label }), " ",
      el("span", { text: c.statement }), " ", refs, c.note ? el("span", { class: "note", text: c.note }) : null));
  }
  why.append(list);
  if (answer.cites_inline === false) {
    box.append(el("p", { class: "muted small", text: "The answer text cites no evidence inline; each claim's sources are listed below." }));
  }
  // Anything unconfirmed, or an answer whose text links no sources, is shown, not tucked away.
  if (ok < claims.length || answer.cites_inline === false) why.open = true;
  box.append(why);

  if ((answer.gaps || []).length) {
    box.append(el("div", { class: "gaps" }, el("strong", { text: "Not answered by the evidence:" }),
      el("ul", {}, ...answer.gaps.map((g) => el("li", { text: g })))));
  }

  const evidence = el("details", { class: "evidence" },
    el("summary", { text: `Evidence (${(answer.evidence || []).length}) from the tools that ran` }));
  const ol = el("ol", { class: "sources" });
  for (const e of answer.evidence || []) {
    const href = safeUrl(e.url || "");
    const head = href ? el("a", { href, target: "_blank", rel: "noopener noreferrer", text: e.label })
                      : el("span", { class: "label-strong", text: e.label });
    const date = e.published_at ? new Date(e.published_at).toLocaleDateString() : null;
    const meta = [e.id, e.kind === "web" ? "web" : e.kind === "computed" ? "computed with Python" : e.kind,
                  `step ${e.task_id}`, date].filter(Boolean).join(" · ");
    ol.append(el("li", { id: `${anchor}-${e.id}` }, head, el("span", { class: "meta", text: meta }),
      el("q", { text: e.text })));
  }
  evidence.append(ol);
  box.append(evidence);
  if (answer.model) box.append(el("p", { class: "muted small", text: `Written by ${answer.model}; checked by code.` }));
  return box;
}

// Only web addresses become links; anything else (javascript:, data:, ...) stays plain text.
function safeUrl(url) {
  try { const u = new URL(url); return u.protocol === "https:" || u.protocol === "http:" ? u.href : null; } catch { return null; }
}

function sources(results) {
  const list = el("ol", { class: "sources" });
  for (const s of results.slice(0, 10)) {
    const href = safeUrl(s.url);
    const title = href
      ? el("a", { href, target: "_blank", rel: "noopener noreferrer", text: s.title || s.url })
      : el("span", { text: s.title || s.url || "" });
    const meta = [s.source, s.published_at ? new Date(s.published_at).toLocaleDateString() : null].filter(Boolean).join(" · ");
    list.append(el("li", {}, title, meta ? el("span", { class: "meta", text: meta }) : null,
      s.snippet ? el("q", { text: s.snippet }) : null));
  }
  return list;
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
  const used = (record.context || []).length;
  if (used) bubble.append(el("span", { class: "muted small context-note",
    text: ` built on ${used} earlier message${used === 1 ? "" : "s"} in this conversation` }));
  if (record.answer) {
    bubble.append(groundedAnswer(record.answer, record.request_id));
  } else {
    if (record.answer_note) bubble.append(el("p", { class: "muted small", text: `No written answer: ${record.answer_note}` }));
    const h = highlights(record);
    if (h) bubble.append(h);
  }
  // The summary's first line ("Request <id>: <state>.") repeats the badge; keep the rest.
  const summary = (record.summary || "").replace(/^Request \S+: [a-z_]+\.\n?/, "").trim();
  if (summary) bubble.append(el("p", { class: "summary", text: summary }));
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
  if (record.receipt) bubble.append(receiptLine(record.receipt));
  if (record.plan && record.report && (record.state === "completed" || record.state === "failed")) {
    bubble.append(replayControl(record.request_id));
  }
}

// ------------------------------------------------------------------ replay

const REPLAY_OUTCOME = {
  identical: ["ok", "same"],
  differs: ["warn", "changed"],
  failed: ["bad", "failed"],
  not_rerun: ["", "not re-run"],
};

// "Would this still give the same answer today?" Runs the stored plan again (read and compute
// steps only, under the user's current permissions) and shows each step's outcome.
function replayControl(requestId) {
  const box = el("div", { class: "replay" });
  const button = el("button", { type: "button", class: "secondary small-btn", text: "Replay",
    title: "Run the same steps again now and compare each result with this one",
    onclick: async () => {
      button.disabled = true;
      const status = working("Replaying the steps");
      box.replaceChildren(button, status);
      try {
        const { ok, status: code, data } = await api("POST", `/requests/${encodeURIComponent(requestId)}/replay`);
        status.remove();
        box.append(ok ? replayResult(data) : el("p", { class: "error small", text: errorText(data, code) }));
      } catch (e) {
        status.remove();
        box.append(el("p", { class: "error small", text: e.message === "unauthorized" ? "Signed out." : "Could not reach the server." }));
      } finally {
        button.disabled = false;
        button.textContent = "Replay again";
      }
    } });
  box.append(button);
  return box;
}

function replayResult(r) {
  const headline = {
    identical: "Replayed now: every re-run step gave the same result.",
    differs: "Replayed now: some results changed since the original run.",
    incomplete: "Replayed now: some steps could not run again.",
  }[r.verdict];
  const out = el("div", { class: "replay-result" }, el("p", { class: "small", text: headline }));
  const list = el("ul", { class: "replay-steps" });
  for (const s of r.steps) {
    const [tone, label] = REPLAY_OUTCOME[s.outcome] || ["", s.outcome];
    const timing = s.original_ms != null && s.replay_ms != null
      ? `${Math.round(s.original_ms)} → ${Math.round(s.replay_ms)} ms` : "";
    const detail = [s.note, s.changed && s.changed.length ? `changed: ${s.changed.join(", ")}` : ""].filter(Boolean).join(" · ");
    list.append(el("li", {},
      el("span", { class: `status ${tone}`, text: label }), " ",
      el("span", { class: "step-name", text: s.task_id }), " ",
      el("span", { class: "muted", text: timing }),
      detail ? el("span", { class: "note", text: detail }) : null));
  }
  out.append(list);
  const facts = [];
  if (r.policy_changed === true) facts.push("Your permissions policy has changed since the original run.");
  if (r.receipt_matches_record === false) facts.push("Warning: the original record no longer matches its receipt.");
  else if (r.receipt_matches_record === true) facts.push("The original record still matches its signed receipt.");
  facts.push(r.note);
  out.append(el("p", { class: "muted small", text: facts.join(" ") }));
  return out;
}

// The request's receipt: hashes and ids only, signed when the server has a key. Downloadable for
// offline checks with `python -m orchestrator verify-receipt`.
function receiptLine(r) {
  const signed = r.signature ? `signed with key ${r.key_id}` : "not signed (no key on this server)";
  const save = el("button", { type: "button", class: "link small-link", text: "Download receipt",
    onclick: () => {
      const blob = new Blob([JSON.stringify(r, null, 2)], { type: "application/json" });
      const a = el("a", { href: URL.createObjectURL(blob), download: `receipt-${r.request_id}.json` });
      document.body.append(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    } });
  return el("p", { class: "muted small receipt",
    title: "Proof of what ran: hashes of the plan, every step's arguments and output, the answer and the policy, chained to the previous receipt" },
    `Receipt #${r.sequence} · ${signed} · `, save);
}

async function resume(requestId, button) {
  button.disabled = true;
  const bubble = addMessage("agent", working("Continuing"));
  try {
    const { status, data } = await api("POST", `/requests/${encodeURIComponent(requestId)}/resume`);
    await renderRecord(bubble, data, status);
  } catch (e) {
    bubble.replaceChildren(el("p", { class: "error", text: e.message === "unauthorized" ? "Signed out." : "Could not reach the server." }));
  }
  bubble.scrollIntoView({ block: "end", behavior: "smooth" });
}

// ------------------------------------------------------------------ live progress

// POST /requests/stream: server-sent events read from the response body (EventSource cannot send
// the Authorization header). Falls back to the plain endpoint when streaming is not available.
async function streamRequest(body, headers, onProgress) {
  let res;
  try {
    res = await fetch("/requests/stream", {
      method: "POST",
      headers: { Authorization: `Bearer ${state.key}`, "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body),
    });
  } catch {
    return api("POST", "/requests", { body, headers });
  }
  if (res.status === 401) { signOut("Your API key was not accepted. Sign in again."); throw new Error("unauthorized"); }
  const type = res.headers.get("content-type") || "";
  if (!res.ok || !res.body || !type.startsWith("text/event-stream")) {
    let data = null;
    try { data = await res.json(); } catch { /* not JSON */ }
    return { ok: res.ok, status: res.status, data };
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "", requestId = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message", data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (!data) continue;  // keep-alive comment
      const payload = JSON.parse(data);
      if (event === "accepted") requestId = payload.request_id;
      else if (event === "progress") onProgress(payload);
      else if (event === "record") return { ok: payload.status < 400, status: payload.status, data: payload.record };
      else if (event === "error") return { ok: false, status: payload.status, data: { error: payload.error } };
    }
  }
  // The stream ended early (a proxy timeout, say); the request kept running, so fetch its record.
  if (requestId) return api("GET", `/requests/${encodeURIComponent(requestId)}`);
  throw new Error("stream ended");
}

const STEP_MARK = { succeeded: "✓", failed: "✗", timed_out: "⏱", denied: "⛔", skipped: "–", cancelled: "–",
                    awaiting_approval: "⏸" };

function progressView() {
  const phase = el("span", { class: "phase", text: "Planning" });
  const clock = el("span", { class: "muted clock" });
  const steps = el("ol", { class: "steps" });
  const root = el("div", { class: "progress" }, el("div", { class: "thinking-line" }, phase, " ", clock), steps);
  let rows = new Map();
  const started = Date.now();
  const timer = setInterval(() => {
    const s = Math.round((Date.now() - started) / 1000);
    if (s >= 2) clock.textContent = `${s}s`;
  }, 1000);

  function row(taskId) {
    if (!rows.has(taskId)) {
      const mark = el("span", { class: "mark", text: "○" });
      const name = el("span", { class: "step-name", text: taskId });
      const info = el("span", { class: "muted step-info" });
      const li = el("li", { class: "pending" }, mark, name, info);
      steps.append(li);
      rows.set(taskId, { li, mark, info });
    }
    return rows.get(taskId);
  }

  function update(msg) {
    switch (msg.type) {
      case "planner_retry": phase.textContent = "Planning (asking again: the first plan was unreadable)"; break;
      case "planned":
        phase.textContent = msg.tasks.length ? `Running ${msg.tasks.length} step${msg.tasks.length === 1 ? "" : "s"}` : "Planning";
        msg.tasks.forEach(row);
        break;
      case "executing": msg.tasks.forEach(row); break;
      case "task_started": {
        const r = row(msg.task_id);
        r.li.className = "running"; r.mark.textContent = "◌";
        r.info.textContent = msg.tool ? `${msg.tool}.${msg.operation}` : "";
        break;
      }
      case "task_retry":
        row(msg.task_id).info.textContent = msg.attempt ? `retrying (attempt ${msg.attempt + 1})` : "retrying";
        break;
      case "task_finished": {
        const r = row(msg.task_id);
        r.li.className = msg.status;
        r.mark.textContent = STEP_MARK[msg.status] || "•";
        const ms = msg.duration_ms != null ? `${Math.round(msg.duration_ms)} ms` : "";
        r.info.textContent = [msg.tool, msg.deduplicated ? "reused" : ms, msg.error].filter(Boolean).join(" · ");
        break;
      }
      case "approval_needed": row(msg.task_id).info.textContent = "waiting for approval"; break;
      case "replanning":
        phase.textContent = `Re-planning after ${msg.failed.join(", ")} failed`;
        steps.append(el("li", { class: "divider", text: "Revised plan" }));
        rows = new Map();
        break;
      case "answering": phase.textContent = "Writing the answer from the results"; break;
      case "finished": phase.textContent = "Done"; break;
    }
  }

  return { root, update, stop: () => clearInterval(timer) };
}

async function send(text) {
  // Attachments go with this message only, so later, unrelated messages don't carry them.
  const files = [...state.attached];
  state.attached = [];
  $("chips").replaceChildren();
  addMessage("user", text, files.length ? el("div", { class: "files", text: `Files: ${files.join(", ")}` }) : null);
  const view = progressView();
  const bubble = addMessage("agent", view.root);
  $("send").disabled = true;
  try {
    const context = state.history.slice(-MAX_CONTEXT);
    const { status, data } = await streamRequest(
      { text, files, allow_partial: $("partial").checked, context }, { "Idempotency-Key": newId() },
      (msg) => { view.update(msg); bubble.scrollIntoView({ block: "end" }); });
    view.stop();
    if (context.length && (status === 404 || status === 409) && !(data && data.state)) {
      // An earlier message can no longer be used (the server restarted without durable state, say).
      state.history = [];
      bubble.replaceChildren(el("p", { class: "error", text: `${errorText(data, status)} ` +
        "Earlier messages are no longer available, so this conversation starts fresh: send it again." }));
      return;
    }
    await renderRecord(bubble, data, status);
    if (data && data.state === "completed" && data.request_id) state.history.push(data.request_id);
  } catch (e) {
    bubble.replaceChildren(el("p", { class: "error", text: e.message === "unauthorized" ? "Signed out." : "Could not reach the server." }));
  } finally {
    view.stop();
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
    const key = $("key").value.trim();
    const provider = providerOf(key);
    let message = null;
    if (provider) {
      $("key").value = "";
      message = `That looks like a ${provider} key. It was not sent anywhere. The server already has its own ` +
        `${provider} key; sign in with your orchestrator API key instead (dev server: config/dev/api-key.txt).`;
    } else if (!(await signIn(key).catch(() => false))) {
      message = "That key was not accepted. Use the orchestrator API key issued for your user " +
        "(dev server: config/dev/api-key.txt), not an LLM provider key.";
    }
    if (message) { $("signin-error").textContent = message; $("signin-error").hidden = false; }
  });
  $("signout").addEventListener("click", () => signOut());
  $("new-conversation").addEventListener("click", () => { resetConversation(); $("text").focus(); });
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
