// Chat UI for the orchestrator API. Plain JS, same origin, no dependencies.
// Everything the server returns is rendered with textContent, never as HTML.
"use strict";

const KEY_STORE = "orch.apiKey";
const $ = (id) => document.getElementById(id);
// history: ids of this conversation's completed requests, oldest first. Follow-ups send the last
// MAX_CONTEXT of them so the agent can use their results ("now double it").
const MAX_CONTEXT = 5;
const state = { key: null, me: null, attached: [], history: [], turns: 0, can: [] };

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

function formatNumber(v) {
  return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function formatValue(v) {
  if (typeof v === "number") return formatNumber(v);
  if (typeof v === "string") return v;
  if (v && typeof v === "object" && !Array.isArray(v)) {
    const entries = Object.entries(v);
    if (entries.length && entries.length <= 8 && entries.every(([, x]) => x === null || typeof x !== "object")) {
      return entries.map(([k, x]) => `${k}: ${formatValue(x)}`).join("  ·  ");
    }
  }
  return short(v, 600);
}

function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }
function seconds(ms) { return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`; }
function humanize(id) { return (id || "").replace(/[_-]+/g, " ").replace(/^\w/, (c) => c.toUpperCase()); }

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

// Plain-language reasons, so a dead end always says what happened and what to do instead.
const PLAIN_ERRORS = {
  no_matching_tool: "None of the tools you can use can do this.",
  permission_denied: "You don't have permission for part of this request.",
  plan_error: "The planner couldn't produce a usable plan. Try rephrasing the request.",
  planner_unavailable: "The planning service is unavailable right now. Try again in a minute.",
  configuration_error: "The server isn't set up for this yet. Ask your administrator.",
  request_not_found: "An earlier message this builds on is no longer available.",
};

function plainError(error) {
  if (!error) return "Something went wrong.";
  return PLAIN_ERRORS[error.code] || error.message || error.code;
}

function plainAnswerNote(note) {
  if (/RateLimit/i.test(note)) return "the writing model is busy right now (rate limit). Your results are below.";
  return note;
}

function announce(text) { $("status").textContent = text; }

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

// What this user's tools can do, in plain words (from GET /capabilities).
const CAN = [
  [/^(file\.|data\.tabular)/, "read your uploaded files"],
  [/^code\.python/, "run Python on your data"],
  [/^web\.search/, "search the web"],
];

async function loadCapabilities() {
  try {
    const { ok, data } = await api("GET", "/capabilities");
    const names = ok && data ? Object.keys(data) : [];
    state.can = CAN.filter(([re]) => names.some((n) => re.test(n))).map(([, words]) => words);
  } catch { state.can = []; }
}

function canSentence() {
  if (!state.can.length) return "";
  const list = state.can.length > 1 ? `${state.can.slice(0, -1).join(", ")} and ${state.can.at(-1)}` : state.can[0];
  return `Here I can ${list}.`;
}

async function signIn(key) {
  state.key = key;
  const res = await fetch("/me", { headers: { Authorization: `Bearer ${key}` } });
  if (!res.ok) { state.key = null; return false; }
  state.me = await res.json();
  storeSet(key);
  $("who").replaceChildren(el("strong", { text: state.me.user_id }), state.me.roles.map(humanize).join(" · ") || "No roles");
  $("who").hidden = $("signout").hidden = $("new-conversation-top").hidden = false;
  $("signin").hidden = true;
  await loadCapabilities();
  resetConversation();
  $("workspace").hidden = false;
  autosize();
  $("text").focus();
  return true;
}

// A fresh, empty conversation. Called on every sign-in and sign-out, so nothing one user saw or
// attached is left on the page for the next person using this tab.
function resetConversation() {
  state.attached = [];
  state.history = [];
  state.turns = 0;
  $("chips").replaceChildren();
  $("text").value = "";
  $("turns").replaceChildren();
  $("rail-empty").hidden = false;
  const empty = $("empty-template").content.cloneNode(true);
  for (const node of empty.querySelectorAll("[data-user]")) node.textContent = state.me ? state.me.user_id : "your";
  for (const button of empty.querySelectorAll("[data-prompt]")) {
    button.addEventListener("click", () => {
      $("text").value = button.dataset.prompt.replace("{user}", state.me ? state.me.user_id : "you");
      autosize();
      $("text").focus();
    });
  }
  const can = canSentence();
  if (can) empty.querySelector("#can").textContent = `${can} Attach a CSV, text or JSON file with “Attach file”; it goes with your next message.`;
  $("log").replaceChildren(empty);
}

function signOut(message) {
  state.key = state.me = null;
  storeSet(null);
  resetConversation();
  $("who").hidden = $("signout").hidden = $("new-conversation-top").hidden = true;
  $("workspace").hidden = true;
  $("signin").hidden = false;
  $("key").value = "";
  $("signin-error").hidden = !message;
  $("signin-error").textContent = message || "";
  $("key").focus();
}

// ------------------------------------------------------------------ files

async function attach(file) {
  const path = `users/${state.me.user_id}/${file.name}`;
  const chip = el("span", { class: "chip", text: `Uploading ${file.name}…` });
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

// ------------------------------------------------------------------ turns and the rail

function addTurn(text, files) {
  $("empty")?.remove();
  $("rail-empty").hidden = true;
  const n = ++state.turns;
  const id = `turn-${n}`;
  const question = el("header", { class: "question" },
    el("span", { class: "n", text: String(n).padStart(2, "0") }),
    el("p", { text }),
    files.length ? el("span", { class: "files", text: `With ${files.join(", ")}` }) : null);
  const reply = el("div", { class: "reply" });
  const article = el("article", { class: "turn", id, "aria-label": `Question ${n}` }, question, reply);
  $("log").append(article);

  const mark = el("span", { class: "mark run", text: "◌", "aria-label": "running" });
  const link = el("a", { href: `#${id}`, onclick: (ev) => { ev.preventDefault(); article.scrollIntoView({ behavior: "smooth", block: "start" }); } },
    el("span", { class: "n", text: String(n).padStart(2, "0") }), el("span", { text: text.length > 70 ? `${text.slice(0, 68)}…` : text }), mark);
  $("turns").append(el("li", {}, link));
  article.scrollIntoView({ block: "start", behavior: "smooth" });
  return { article, reply, mark };
}

const RAIL = { ok: ["✓", "answered"], warn: ["!", "needs attention"], bad: ["✗", "did not run"], run: ["◌", "running"] };
function setRail(turn, tone) {
  const [glyph, label] = RAIL[tone] || RAIL.run;
  turn.mark.className = `mark ${tone}`;
  turn.mark.textContent = glyph;
  turn.mark.setAttribute("aria-label", label);
}

// ------------------------------------------------------------------ the reply

// One honest status: never green when what the user asked for did not arrive.
function outcome(record) {
  const partial = record.report && record.report.status === "partial";
  switch (record.state) {
    case "completed":
      if (record.answer_note) return ["warn", "Results ready", "no written answer"];
      if (partial) return ["warn", "Partly completed", "some steps did not finish"];
      return ["ok", record.answer ? "Answered" : "Completed", ""];
    case "awaiting_approval": return ["warn", "Waiting for approval", ""];
    case "failed": return ["bad", "Failed", "no step succeeded"];
    case "rejected": return ["bad", "Couldn't run this", ""];
    default: return ["run", humanize(record.state), ""];
  }
}

function stateLine(record) {
  const [tone, label, sub] = outcome(record);
  const used = (record.context || []).length;
  const bits = [sub, used ? `builds on ${plural(used, "earlier message")}` : ""].filter(Boolean).join(" · ");
  return el("span", { class: `state ${tone}` }, label, bits ? el("span", { class: "sub", text: ` · ${bits}` }) : null);
}

// The signature: the headline figure with its Source line, like a business-paper chart.
function headlineFigure(record, anchor) {
  const results = (record.report && record.report.task_results) || [];
  // A computed number: a bare result, or an object holding exactly one number ({"average": 129325}).
  const numberIn = (v) => {
    if (typeof v === "number") return v;
    if (v && typeof v === "object" && !Array.isArray(v)) {
      const nums = Object.values(v).filter((x) => typeof x === "number");
      return nums.length === 1 ? nums[0] : null;
    }
    return null;
  };
  const hit = results.find((r) => r.status === "succeeded" && r.output && numberIn(r.output.result) != null);
  if (!hit) return null;
  const value = numberIn(hit.output.result);
  const task = record.plan && record.plan.tasks.find((t) => t.task_id === hit.task_id);
  const label = (task && task.description) || humanize(hit.task_id);
  const ev = record.answer && (record.answer.evidence || []).find((e) => e.task_id === hit.task_id && e.kind === "computed");
  const checked = ev && (record.answer.claims || []).some((c) => c.status === "supported" && (c.evidence || []).includes(ev.id));
  const source = el("div", { class: "source" }, el("b", { text: "Source" }),
    ev ? el("a", { class: "cite", href: `#${anchor}-${ev.id}`, text: ev.id, onclick: (e) => { e.preventDefault(); showEvidence(anchor, ev.id); } }) : null,
    el("span", { text: `Python · step ${hit.task_id}` }),
    checked ? el("span", { class: "checked", text: "✓ checked by code" }) : null);
  return el("div", { class: "figure" }, el("div", { class: "label", text: label }),
    el("div", { class: "value", text: formatNumber(value) }), source);
}

const CLAIM_STATUS = {
  supported: ["ok", "✓ checked", "Every number matches the evidence it cites."],
  unverified: ["warn", "! unverified", "Code could not confirm this against the cited evidence."],
  uncited: ["bad", "✗ no evidence", "This statement cites no existing evidence."],
};

// Answer text with [E1] citations turned into chips that jump to the evidence item.
function citedText(text, anchor) {
  const p = el("p", { class: "answer-text" });
  let last = 0;
  for (const m of text.matchAll(/\[(E\d+)(\?)?\]/g)) {
    p.append(text.slice(last, m.index));
    if (m[2]) p.append(el("span", { class: "cite bad", title: "This citation points to no evidence", text: `${m[1]}?` }));
    else p.append(el("a", { class: "cite", href: `#${anchor}-${m[1]}`, text: m[1], "aria-label": `Evidence ${m[1]}`,
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
  const parts = [citedText(answer.text, anchor)];

  const claims = answer.claims || [];
  const ok = claims.filter((c) => c.status === "supported").length;
  const allOk = ok === claims.length && answer.cites_inline !== false;
  const why = el("details", { class: "block" }, el("summary", {},
    "How this answer is supported", el("span", { class: "count", text: ` · ${plural(claims.length, "claim")}` })));
  const list = el("ul", { class: "claims" });
  for (const c of claims) {
    const [tone, label, help] = CLAIM_STATUS[c.status] || CLAIM_STATUS.uncited;
    const refs = (c.evidence || []).map((id) => el("a", { class: "cite", href: `#${anchor}-${id}`, text: id,
      onclick: (ev) => { ev.preventDefault(); showEvidence(anchor, id); } }));
    list.append(el("li", {}, el("span", { class: `tag ${tone}`, title: help, text: label }),
      el("span", {}, c.statement, " ", ...refs, c.note ? el("span", { class: "note", text: c.note }) : null)));
  }
  why.append(list);
  // Anything unconfirmed, or an answer whose text links no sources, is shown, not tucked away.
  if (!allOk) why.open = true;

  const verify = el("div", { class: `verify ${allOk ? "" : "warn"}` },
    allOk ? `✓ ${ok} of ${plural(claims.length, "claim")} checked against the evidence`
          : `${ok} of ${plural(claims.length, "claim")} confirmed; the rest are marked below`,
    answer.model ? el("span", { class: "by", text: `Written by ${answer.model}, checked by code` }) : null);
  parts.push(verify);
  if (answer.cites_inline === false) {
    parts.push(el("p", { class: "note-line", text: "The answer text cites no evidence inline; each claim's sources are listed below." }));
  }
  if ((answer.gaps || []).length) {
    parts.push(el("div", { class: "callout" }, el("strong", { text: "Not answered by the evidence" }),
      el("ul", {}, ...answer.gaps.map((g) => el("li", { text: g })))));
  }
  parts.push(why);

  const evidence = el("details", { class: "block" }, el("summary", {},
    "Evidence", el("span", { class: "count", text: ` · ${(answer.evidence || []).length} items from the steps that ran` })));
  const ol = el("ol", { class: "evidence" });
  for (const e of answer.evidence || []) {
    const href = safeUrl(e.url || "");
    const head = href ? el("a", { class: "title", href, target: "_blank", rel: "noopener noreferrer", text: e.label })
                      : el("span", { class: "title", text: e.label });
    const date = e.published_at ? new Date(e.published_at).toLocaleDateString() : null;
    const kind = e.kind === "web" ? "Web" : e.kind === "computed" ? "Computed with Python" : e.kind === "earlier" ? "Earlier result" : humanize(e.kind);
    const meta = [kind, `step ${e.task_id}`, date].filter(Boolean).join(" · ");
    ol.append(el("li", { id: `${anchor}-${e.id}` }, el("span", { class: "key", text: e.id }),
      el("div", {}, head, el("span", { class: "meta", text: meta }), el("q", { text: e.text }))));
  }
  evidence.append(ol);
  parts.push(evidence);
  return parts;
}

// Only web addresses become links; anything else (javascript:, data:, ...) stays plain text.
function safeUrl(url) {
  try { const u = new URL(url); return u.protocol === "https:" || u.protocol === "http:" ? u.href : null; } catch { return null; }
}

function sources(results) {
  const list = el("ol", { class: "evidence" });
  results.slice(0, 10).forEach((s, i) => {
    const href = safeUrl(s.url);
    const title = href
      ? el("a", { class: "title", href, target: "_blank", rel: "noopener noreferrer", text: s.title || s.url })
      : el("span", { class: "title", text: s.title || s.url || "" });
    const meta = [s.source, s.published_at ? new Date(s.published_at).toLocaleDateString() : null].filter(Boolean).join(" · ");
    list.append(el("li", {}, el("span", { class: "key", text: String(i + 1) }),
      el("div", {}, title, meta ? el("span", { class: "meta", text: meta }) : null, s.snippet ? el("q", { text: s.snippet }) : null)));
  });
  return list;
}

// The headline figure shows a bare number or an object holding exactly one number; anything else
// is listed with the results below.
function shownAsFigure(v) {
  if (typeof v === "number") return true;
  return !!v && typeof v === "object" && !Array.isArray(v) && Object.values(v).filter((x) => typeof x === "number").length === 1;
}

// Results without a written answer: the computed values and the sources, structured.
function highlights(record) {
  const box = el("div", { class: "highlights" });
  for (const r of (record.report && record.report.task_results) || []) {
    if (r.status !== "succeeded" || !r.output) continue;
    const o = r.output;
    if (Array.isArray(o.results) && o.results.length) {
      const d = el("details", { class: "block" }, el("summary", {}, "Web results",
        el("span", { class: "count", text: ` · ${o.results.length} from step ${r.task_id}` })));
      d.append(sources(o.results));
      box.append(d);
    } else if ("result" in o && !shownAsFigure(o.result)) {
      box.append(el("p", { class: "answer-text" }, el("b", { text: `${humanize(r.task_id)}: ` }), formatValue(o.result)));
    }
  }
  return box.childElementCount ? box : null;
}

// ------------------------------------------------------------------ the trace (kept after the run)

const TOOL_WORDS = {
  "file_processing.read_csv": "Read CSV", "file_processing.read_text": "Read text file",
  "file_processing.list_files": "List files", "python_exec.run": "Run Python", "web_search.search": "Web search",
};
const STEP_MARK = { succeeded: "✓", failed: "✗", timed_out: "⏱", denied: "⛔", skipped: "–", cancelled: "–",
                    awaiting_approval: "⏸", running: "◌", pending: "○" };

function toolWords(toolId, op) { return TOOL_WORDS[`${toolId}.${op}`] || (toolId ? `${toolId}.${op}` : ""); }

// A step's time comes from its start and finish (the record has no duration field).
function stepMs(result) {
  if (result.duration_ms != null) return result.duration_ms;
  const a = Date.parse(result.started_at), b = Date.parse(result.finished_at);
  return Number.isFinite(a) && Number.isFinite(b) && b >= a ? b - a : null;
}

function traceRow(result, task, maxMs) {
  const ms = stepMs(result);
  const what = el("span", { class: "what" }, el("b", { text: (task && task.description) || humanize(result.task_id) }),
    el("span", { text: [toolWords(result.tool_id, result.operation), result.deduplicated ? "reused" : "", result.task_id].filter(Boolean).join(" · ") }));
  const head = [el("span", { class: "mark", text: STEP_MARK[result.status] || "•", "aria-label": result.status }), what,
    el("span", { class: "ms", text: ms != null ? seconds(ms) : "" })];
  let bar = null;
  if (ms != null && maxMs) {
    // Width set through the CSSOM: the page's CSP forbids inline style attributes.
    const fill = el("span", { class: "bar-fill" });
    fill.style.width = `${Math.max(0.5, (ms / maxMs) * 100).toFixed(1)}%`;
    bar = el("span", { class: "bar-track", "aria-hidden": "true" }, fill);
  }

  const io = el("div", { class: "io" });
  const inputs = task && task.inputs ? { ...task.inputs } : null;
  if (inputs && typeof inputs.code === "string") { io.append(el("h4", { text: "Code" }), el("pre", { text: inputs.code })); delete inputs.code; }
  if (inputs && Object.keys(inputs).length) io.append(el("h4", { text: "Inputs" }), el("pre", { text: short(inputs, 1500) }));
  if (result.output) io.append(el("h4", { text: "Output" }), el("pre", { text: short(result.output, 2500) }));
  if (result.error) io.append(el("h4", { text: "What went wrong" }), el("pre", { text: result.error.message || result.error.code }));
  const details = el("details", {}, el("summary", { "aria-label": `Show inputs and output of ${result.task_id}` }, ...head, bar), io);
  return el("li", { class: result.status }, details);
}

function tracePanel(record) {
  const results = (record.report && record.report.task_results) || [];
  const tasks = new Map(((record.plan && record.plan.tasks) || []).map((t) => [t.task_id, t]));
  const maxMs = Math.max(0, ...results.map((r) => stepMs(r) || 0));
  const total = (record.stages || []).find((s) => s.stage === "total");
  const panel = el("section", { class: "panel", "aria-label": "Steps that ran" },
    el("h3", {}, "Steps", el("span", { class: "count", text: plural(results.length, "step") })));
  const list = el("ol", { class: "trace" }, ...results.map((r) => traceRow(r, tasks.get(r.task_id), maxMs)));
  panel.append(list);
  if (total) panel.append(el("div", { class: "total" }, el("span", { text: "Total, planning and answer included" }), el("span", { text: seconds(total.duration_ms) })));
  if (record.receipt) panel.append(receiptBlock(record.receipt));
  if (record.plan && record.report && (record.state === "completed" || record.state === "failed")) {
    panel.append(replayControl(record.request_id));
  }
  return panel;
}

async function approvalCard(record, taskId, approvalId, onDecided) {
  const card = el("div", { class: "approval" }, el("strong", { text: `Approval needed: ${humanize(taskId)}` }));
  const { ok, data } = await api("GET", `/approvals/${encodeURIComponent(approvalId)}`);
  if (ok) {
    card.append(el("span", { text: `${toolWords(data.tool_id, data.operation)}${data.resource ? " on " + data.resource : ""}` }));
    card.append(el("pre", { text: data.args_preview }));
  }
  const canApprove = state.me.roles.includes("approver");
  if (!canApprove) {
    card.append(el("span", { class: "note-line", text: "Waiting for someone with the approver role. Ask them to approve it, then press Continue." }));
    return card;
  }
  const actions = el("div", { class: "actions" });
  const decide = async (approve) => {
    for (const b of actions.querySelectorAll("button")) b.disabled = true;
    const res = await api("POST", `/approvals/${encodeURIComponent(approvalId)}/${approve ? "approve" : "deny"}`, { body: {} });
    if (!res.ok) { actions.append(el("span", { class: "error", text: errorText(res.data, res.status) })); return; }
    actions.replaceChildren(el("span", { class: `tag ${approve ? "ok" : "bad"}`, text: approve ? "Approved" : "Denied" }));
    onDecided();
  };
  actions.append(
    el("button", { type: "button", class: "btn primary", text: "Approve", onclick: () => decide(true) }),
    el("button", { type: "button", class: "btn", text: "Deny", onclick: () => decide(false) }));
  card.append(actions);
  return card;
}

async function renderRecord(turn, record, status) {
  const reply = turn.reply;
  reply.replaceChildren();
  reply.className = "reply";
  if (!record || !record.state) {
    reply.classList.add("single");
    reply.append(el("div", { class: "main-col" }, el("div", { class: "callout bad" },
      el("strong", { text: "This request didn't go through" }), el("p", { text: errorText(record, status) }))));
    setRail(turn, "bad");
    announce("The request didn't go through.");
    return;
  }
  const anchor = `ev-${record.request_id}`;
  const [tone, label] = outcome(record);
  setRail(turn, tone === "run" ? "run" : tone);
  announce(`${label}.`);
  const main = el("div", { class: "main-col" }, stateLine(record));

  if (record.state === "rejected") {
    const can = canSentence();
    main.append(el("div", { class: "callout bad" }, el("strong", { text: plainError(record.error) }),
      el("p", { text: [can || "", "Try rephrasing, or ask about an uploaded file or the web."].filter(Boolean).join(" ") })));
  }
  const figure = headlineFigure(record, anchor);
  if (figure) main.append(figure);
  if (record.answer) {
    main.append(...groundedAnswer(record.answer, record.request_id));
  } else {
    if (record.answer_note) {
      main.append(el("div", { class: "callout" }, el("strong", { text: "No written answer this time" }),
        el("p", { text: `The steps finished, but ${plainAnswerNote(record.answer_note.replace(/^the answer could not be written \(([^)]+)\)\.?$/, "$1"))}` })));
    }
    const h = highlights(record);
    if (h) main.append(h);
  }

  const caveats = [...((record.report && record.report.uncertainty) || []), ...(record.unsupported || []).map((u) => `Not supported by any available tool: ${u}`)];
  if (caveats.length && record.state !== "rejected") {
    main.append(el("div", { class: "callout" }, el("strong", { text: "Worth knowing" }), el("ul", {}, ...caveats.map((c) => el("li", { text: c })))));
  }

  const pending = Object.entries(record.pending_approvals || {});
  if (pending.length) {
    let left = pending.length;
    const cont = el("button", { type: "button", class: "btn primary", text: "Continue", hidden: "" });
    const decided = () => { if (--left === 0) cont.hidden = false; };
    for (const [taskId, approvalId] of pending) main.append(await approvalCard(record, taskId, approvalId, decided));
    if (!state.me.roles.includes("approver")) cont.hidden = false;
    cont.addEventListener("click", () => resume(record.request_id, cont, turn));
    main.append(el("div", {}, cont));
  }

  reply.append(main);
  if (record.report && (record.report.task_results || []).length) reply.append(el("div", { class: "side-col" }, tracePanel(record)));
  else {
    reply.classList.add("single");
    if (record.receipt) main.append(el("div", { class: "panel" }, receiptBlock(record.receipt)));
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
  const box = el("div", { class: "receipt" });
  const button = el("button", { type: "button", class: "btn small", text: "Replay steps",
    title: "Run the same steps again now and compare each result with this one",
    onclick: async () => {
      button.disabled = true;
      const status = el("span", { class: "note-line", text: "Replaying the steps…" });
      box.replaceChildren(button, status);
      try {
        const { ok, status: code, data } = await api("POST", `/requests/${encodeURIComponent(requestId)}/replay`);
        status.remove();
        box.append(ok ? replayResult(data) : el("p", { class: "error note-line", text: errorText(data, code) }));
        if (ok) announce(data.verdict === "identical" ? "Replay: every step gave the same result." : "Replay finished with differences.");
      } catch (e) {
        status.remove();
        box.append(el("p", { class: "error note-line", text: e.message === "unauthorized" ? "Signed out." : "Could not reach the server." }));
      } finally {
        button.disabled = false;
        button.textContent = "Replay again";
      }
    } });
  box.append(el("span", { class: "line", text: "Would this still give the same result today?" }), button);
  return box;
}

function replayResult(r) {
  const headline = {
    identical: "Replayed now: every re-run step gave the same result.",
    differs: "Replayed now: some results changed since the original run.",
    incomplete: "Replayed now: some steps could not run again.",
  }[r.verdict];
  const out = el("div", { class: "replay-result" }, el("p", { text: headline }));
  const list = el("ul", { class: "replay-steps" });
  for (const s of r.steps) {
    const [tone, label] = REPLAY_OUTCOME[s.outcome] || ["", s.outcome];
    const timing = s.original_ms != null && s.replay_ms != null ? ` · ${seconds(s.original_ms)} → ${seconds(s.replay_ms)}` : "";
    const detail = [s.note, s.changed && s.changed.length ? `changed: ${s.changed.join(", ")}` : ""].filter(Boolean).join(" · ");
    list.append(el("li", {}, el("span", { class: `tag ${tone}`, text: label }), el("span", { text: `${s.task_id}${timing}` }),
      detail ? el("span", { class: "note", text: detail }) : null));
  }
  out.append(list);
  const facts = [];
  if (r.policy_changed === true) facts.push("Your permissions policy has changed since the original run.");
  if (r.receipt_matches_record === false) facts.push("Warning: the original record no longer matches its receipt.");
  else if (r.receipt_matches_record === true) facts.push("The original record still matches its signed receipt.");
  facts.push(r.note);
  out.append(el("p", { class: "note-line", text: facts.join(" ") }));
  return out;
}

// ------------------------------------------------------------------ receipt: copy to clipboard

// The request's receipt: hashes and ids only, signed when the server has a key. Copied as JSON for
// offline checks with `python -m orchestrator verify-receipt`.
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* fall through to the selection fallback */ }
  const area = el("textarea", { class: "sr", "aria-hidden": "true" });
  area.value = text;
  document.body.append(area);
  area.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  area.remove();
  return ok;
}

function receiptBlock(r) {
  const signed = r.signature ? `signed with key ${r.key_id}` : "not signed (this server has no signing key)";
  const done = el("span", { class: "copied", role: "status" });
  const copy = el("button", { type: "button", class: "btn small", text: "Copy receipt",
    title: "Copy this receipt as JSON: hashes of the plan, every step's arguments and output, the answer and the policy, chained to the previous receipt",
    onclick: async () => {
      const ok = await copyText(JSON.stringify(r, null, 2));
      done.textContent = ok ? "Copied to clipboard" : "Copy failed; select the receipt from the record instead";
      done.className = ok ? "copied" : "error note-line";
      setTimeout(() => { done.textContent = ""; }, 2500);
    } });
  return el("div", { class: "receipt" },
    el("span", { class: "line" }, el("b", { text: `Receipt #${r.sequence}` }), ` · ${signed}`),
    el("div", { class: "actions" }, copy, done));
}

async function resume(requestId, button, turn) {
  button.disabled = true;
  setRail(turn, "run");
  announce("Continuing.");
  try {
    const { status, data } = await api("POST", `/requests/${encodeURIComponent(requestId)}/resume`);
    await renderRecord(turn, data, status);
  } catch (e) {
    turn.reply.replaceChildren(el("div", { class: "callout bad" }, el("p", { text: e.message === "unauthorized" ? "Signed out." : "Could not reach the server." })));
  }
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

// The same trace panel, filling in while the request runs; the final record replaces it with the
// complete one (inputs, outputs, receipt), so the trace never disappears.
function progressView(turn) {
  const phase = el("span", { class: "state run", text: "Planning" });
  const clock = el("span", { class: "sub" });
  phase.append(clock);
  const list = el("ol", { class: "trace" });
  const panel = el("section", { class: "panel", "aria-label": "Steps running" }, el("h3", { text: "Steps" }), list);
  turn.reply.replaceChildren(el("div", { class: "main-col" }, phase), el("div", { class: "side-col" }, panel));
  let rows = new Map();
  const started = Date.now();
  const timer = setInterval(() => {
    const s = Math.round((Date.now() - started) / 1000);
    if (s >= 2) clock.textContent = ` · ${s}s`;
  }, 1000);
  const setPhase = (text) => { phase.firstChild.textContent = text; announce(text); };

  function row(taskId) {
    if (!rows.has(taskId)) {
      const mark = el("span", { class: "mark", text: STEP_MARK.pending });
      const info = el("span", {});
      const li = el("li", { class: "pending" }, el("div", { class: "row" }, mark,
        el("span", { class: "what" }, el("b", { text: humanize(taskId) }), info), el("span", { class: "ms" })));
      list.append(li);
      rows.set(taskId, { li, mark, info, ms: li.querySelector(".ms") });
    }
    return rows.get(taskId);
  }

  function update(msg) {
    switch (msg.type) {
      case "planner_retry": setPhase("Planning (asking again: the first plan was unreadable)"); break;
      case "planned":
        if (msg.tasks.length) setPhase(`Running ${plural(msg.tasks.length, "step")}`);
        msg.tasks.forEach(row);
        break;
      case "executing": msg.tasks.forEach(row); break;
      case "task_started": {
        const r = row(msg.task_id);
        r.li.className = "running"; r.mark.textContent = STEP_MARK.running;
        r.info.textContent = toolWords(msg.tool, msg.operation);
        break;
      }
      case "task_retry":
        row(msg.task_id).info.textContent = msg.attempt ? `retrying (attempt ${msg.attempt + 1})` : "retrying";
        break;
      case "task_finished": {
        const r = row(msg.task_id);
        r.li.className = msg.status;
        r.mark.textContent = STEP_MARK[msg.status] || "•";
        r.ms.textContent = msg.deduplicated ? "reused" : msg.duration_ms != null ? seconds(msg.duration_ms) : "";
        if (msg.error) r.info.textContent = msg.error;
        break;
      }
      case "approval_needed": row(msg.task_id).info.textContent = "waiting for approval"; break;
      case "replanning":
        setPhase(`Re-planning after ${msg.failed.join(", ")} failed`);
        list.append(el("li", { class: "divider", text: "Revised plan" }));
        rows = new Map();
        break;
      case "answering": setPhase("Writing the answer from the results"); break;
      case "finished": setPhase("Done"); break;
    }
  }

  return { update, stop: () => clearInterval(timer) };
}

async function send(text) {
  // Attachments go with this message only, so later, unrelated messages don't carry them.
  const files = [...state.attached];
  state.attached = [];
  $("chips").replaceChildren();
  const turn = addTurn(text, files);
  const view = progressView(turn);
  $("send").disabled = true;
  try {
    const context = state.history.slice(-MAX_CONTEXT);
    const { status, data } = await streamRequest(
      { text, files, allow_partial: $("partial").checked, context }, { "Idempotency-Key": newId() },
      (msg) => view.update(msg));
    view.stop();
    if (context.length && (status === 404 || status === 409) && !(data && data.state)) {
      // An earlier message can no longer be used (the server restarted without durable state, say).
      state.history = [];
      turn.reply.replaceChildren(el("div", { class: "callout bad" }, el("strong", { text: errorText(data, status) }),
        el("p", { text: "Earlier messages are no longer available, so this conversation starts fresh: send it again." })));
      setRail(turn, "bad");
      return;
    }
    await renderRecord(turn, data, status);
    if (data && data.state === "completed" && data.request_id) state.history.push(data.request_id);
  } catch (e) {
    turn.reply.replaceChildren(el("div", { class: "callout bad" }, el("p", { text: e.message === "unauthorized" ? "Signed out." : "Could not reach the server." })));
    setRail(turn, "bad");
  } finally {
    view.stop();
    $("send").disabled = false;
  }
}

// ------------------------------------------------------------------ wiring

function autosize() {
  const t = $("text");
  t.style.height = "auto";
  t.style.height = `${Math.max(44, t.scrollHeight + 2)}px`;
  t.style.overflowY = t.scrollHeight > 200 ? "auto" : "hidden";
}

function newConversation() { resetConversation(); $("text").focus(); }

document.addEventListener("DOMContentLoaded", async () => {
  $("signin-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const key = $("key").value.trim();
    const provider = providerOf(key);
    let message = null;
    $("signin-button").disabled = true;
    if (provider) {
      $("key").value = "";
      message = `That looks like a ${provider} key. It was not sent anywhere. The server already has its own ` +
        `${provider} key; sign in with your orchestrator API key instead (dev server: config/dev/api-key.txt).`;
    } else if (!(await signIn(key).catch(() => false))) {
      message = "That key was not accepted. Use the orchestrator API key issued for your user " +
        "(dev server: config/dev/api-key.txt), not an LLM provider key.";
    }
    $("signin-button").disabled = false;
    if (message) { $("signin-error").textContent = message; $("signin-error").hidden = false; }
  });
  $("signout").addEventListener("click", () => signOut());
  $("new-conversation").addEventListener("click", newConversation);
  $("new-conversation-top").addEventListener("click", newConversation);
  $("attach").addEventListener("click", () => $("file").click());
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
