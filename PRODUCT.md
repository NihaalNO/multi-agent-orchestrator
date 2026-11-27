# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

**Primary: analysts at customer organizations.** Business and data analysts who ask questions in plain
language over their own uploaded files (CSV, text, JSON) and the web, and need an answer they can
trust and check before acting on it or passing it on. They are not engineers; plan ids, tool
operation names and JSON are not their vocabulary.

Secondary roles in the same interface:
- **Approvers** decide sensitive steps (a write, a delete) that a request is waiting on.
- **Admins** manage tools and read the receipt chain for audits.

## Product Purpose

Multi-Tool Orchestrator turns a natural-language request into a plan of tool steps (file reading,
sandboxed Python, web search, and tools registered at runtime), runs the steps in parallel where it
can, reconciles conflicting results, and answers. Success for the user: a correct answer they can
see the basis of — which steps ran, what each produced, which source backs each claim — without
having to trust the model.

## Positioning

**Provable answers.** The LLM only proposes; deterministic code decides. Every step is
permission-checked against the user's grants; every claim in a written answer cites numbered
evidence and is checked by code (numbers must appear in the cited evidence, unconfirmed claims are
marked, not hidden); every finished request gets a signed, hash-chained receipt that anyone with the
public key can verify offline. A typical AI chat or agent framework cannot truthfully claim this.

## Operating Context

- Offered to outside organizations as a product; first impressions, onboarding and plain-language
  explanations matter.
- Users sign in with an orchestrator API key issued per user (never an LLM provider key) and work in
  a browser chat; requests run for seconds to tens of seconds (planning, tools, then writing the
  answer), streamed as live progress.
- A conversation is held by the browser tab: follow-ups build on the last five completed replies by
  reference; reloading or "New conversation" starts fresh.
- Files are uploaded to the user's own folder and referenced by path.
- Sensitive steps pause the request until someone with the approver role approves or denies them;
  the requester then continues it.

## Capabilities and Constraints

- Serving: a FastAPI app serves the chat as three static files (`src/orchestrator/api/static/`:
  `chat.html`, `chat.css`, `chat.js`), plain JavaScript with no framework or build step.
- Strict Content-Security-Policy on the page: scripts, styles and fonts load only from the same
  origin (`default-src 'none'`, `script-src 'self'`, `style-src 'self'`), so no CDNs, inline scripts
  or third-party fonts; any font or icon must be shipped as a local file.
- All server text is rendered as text, never as HTML; only `http(s)` links are made clickable.
- Data the UI receives per request: plan (tasks, tools, inputs), live progress events, per-step
  results and errors, conflicts and caveats, a grounded answer (text with `[E#]` citations, claims
  with status supported / unverified / uncited, gaps, evidence items with kind, source, date, step),
  and a receipt (sequence, signature, key id, hashes).
- Grounded answers exist only with Groq/Ollama planners today; otherwise the response is a
  deterministic summary. Answer writing can fail (for example a provider rate limit) while the tool
  results still succeed.
- Python runs in a WebAssembly sandbox with the standard library only.
- Terminology the product uses: request, plan, step, tool, evidence (E1, E2…), claim, checked /
  unverified / no evidence, receipt, approval, conversation.

## Evidence on Hand

- Real behavior verified live: CSV average via sandboxed Python, Tavily web search with dated
  sources, grounded answers with checked claims, follow-ups by reference ("now double it" →
  258,650), signed receipts verified offline.
- A design critique of the current chat UI: `.impeccable/critique/` (21/40; trace discarded on
  completion, misleading status, verification styled as fine print, accessibility gaps, dead ends).
- No customers, testimonials, logos, benchmarks, pricing or certifications exist. Do not fabricate
  any.

## Product Principles

1. **Show the work, keep it.** The trace of what ran is the product, not a debug view: it stays
   readable after the work is done.
2. **Never overstate.** Status, badges and copy must never claim more certainty or success than the
   record supports; doubt is surfaced, confidence can be collapsed.
3. **Analyst language first.** Plain words up front; ids, hashes, operation names and JSON one level
   deeper for those who want them.
4. **Every dead end has a next step.** Errors and rejections say what the user can do instead.
5. **Proof travels.** Evidence and receipts are first-class, shareable artifacts, not footnotes.
