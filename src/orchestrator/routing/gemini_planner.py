"""Gemini-backed planner (Google Gen AI SDK, Interactions API).

Same contract as the Claude planner — shared prompt, schema and parser (``llm_common``) — so a
Gemini plan is exactly as untrusted and exactly as re-verified as a Claude plan.

* Structured output: ``response_format`` with ``PLAN_SCHEMA`` (JSON mime type).
* ``store=False``: requests (which contain user data) are not kept on Google's servers. The SDK's
  automatic continuation still works; it resends the input on each hop.
* Credentials: ``GEMINI_API_KEY`` or ``GOOGLE_API_KEY`` (the SDK reads both; ``GOOGLE_API_KEY``
  wins), or ``ORCH_LLM_API_KEY``. Unlike Anthropic there is no sign-in fallback, so a missing key
  is a configuration error at startup.
* Gemini's JSON-Schema support strips keywords it does not enforce; nothing is lost because the
  output is validated again by ``parse_plan_output`` and the router.
"""

from __future__ import annotations

from typing import Any, Literal

from orchestrator.contracts import PlanningRequest, PlanProposal
from orchestrator.errors import ConfigurationError, PlanError, PlannerUnavailableError
from orchestrator.observability.audit import AuditLog
from orchestrator.routing.llm_common import PLAN_SCHEMA, SYSTEM_PROMPT, no_tools_proposal, parse_plan_output, user_message
from orchestrator.routing.planner import ToolCatalog

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
INSTALL_HINT = "install the Gemini extra: pip install 'multi-tool-orchestrator[gemini]'"

ErrorKind = Literal["config", "unavailable", "failure"]


def _network_errors() -> tuple[type[BaseException], ...]:
    """Transport errors from whichever HTTP stack the SDK uses (httpx and/or httpx2)."""
    found: list[type[BaseException]] = []
    for module in ("httpx", "httpx2"):
        try:
            found.append(__import__(module).TransportError)
        except (ImportError, AttributeError):
            pass
    return tuple(found)


def classify_gemini_error(exc: BaseException) -> tuple[ErrorKind, str] | None:
    """Map a Google Gen AI SDK / transport exception to (kind, message), or None if it is not one.

    config      — credentials or project setup (retrying will not help; an operator must act)
    unavailable — rate limits, quota, outages, timeouts, network (worth retrying later)
    failure     — the request itself was rejected
    """
    from google.genai import errors

    if isinstance(exc, errors.APIError):
        code, status = exc.code or 0, (exc.status or "").upper()
        message = f"Gemini API error {code} {status}: {exc.message}".strip()
        text = (exc.message or "").lower()
        if code in (401, 403) or status in ("UNAUTHENTICATED", "PERMISSION_DENIED") or "api key" in text:
            return "config", f"Gemini credentials rejected: {exc.message}"
        if code == 429 or status == "RESOURCE_EXHAUSTED":
            return "unavailable", f"Gemini rate limit or quota reached: {exc.message}"
        if code >= 500 or isinstance(exc, errors.ServerError):
            return "unavailable", message
        return "failure", message
    if isinstance(exc, errors.ContinuationInterruptedError):
        return "unavailable", "Gemini request ran out of time"
    if isinstance(exc, (TimeoutError, *_network_errors())):
        return "unavailable", "could not reach the Gemini API"
    return None


def make_gemini_client(api_key: str | None = None) -> Any:
    try:
        from google import genai
    except ImportError as exc:
        raise ConfigurationError(f"Gemini support is not installed; {INSTALL_HINT}") from exc
    try:
        return genai.Client(api_key=api_key) if api_key else genai.Client()
    except ValueError as exc:  # raised when no key is configured
        raise ConfigurationError(
            "no Gemini API key: set GEMINI_API_KEY (or GOOGLE_API_KEY, or ORCH_LLM_API_KEY)"
        ) from exc


class GeminiPlanner:
    name = "gemini"
    supports_feedback = True

    def __init__(
        self,
        client: Any = None,
        *,
        model: str = DEFAULT_GEMINI_MODEL,
        timeout_s: float = 120.0,
        audit: AuditLog | None = None,
    ) -> None:
        self._client = client if client is not None else make_gemini_client()
        self._model = model
        self._timeout = timeout_s
        self._audit = audit or AuditLog()

    async def plan(self, request: PlanningRequest, catalog: ToolCatalog, feedback: str | None = None) -> PlanProposal:
        if not catalog.entries:
            return no_tools_proposal(request, self.name)
        try:
            interaction = await self._client.aio.interactions.create(
                model=self._model,
                input=user_message(request, catalog, feedback),
                system_instruction=SYSTEM_PROMPT,
                response_format={"type": "text", "mime_type": "application/json", "schema": PLAN_SCHEMA},
                store=False,
                timeout=self._timeout,
            )
        except Exception as exc:
            classified = classify_gemini_error(exc)
            if classified is None:
                raise
            kind, message = classified
            if kind == "config":
                raise ConfigurationError(message) from exc
            if kind == "unavailable":
                raise PlannerUnavailableError(message) from exc
            raise PlanError(f"planner request rejected: {message}") from exc

        status = getattr(interaction, "status", None)
        self._audit.record(
            "planner.response", request_id=request.request_id, provider="gemini",
            model=self._model, status=status, interaction_id=getattr(interaction, "id", None),
        )
        if status != "completed":
            problems = "; ".join(
                f"{e.code}: {e.message}" for e in (getattr(interaction, "errors", None) or []) if e
            )
            if status == "incomplete":
                raise PlanError("planner output was cut off (incomplete)")
            raise PlanError(f"planner interaction ended {status}" + (f" ({problems})" if problems else ""))
        return parse_plan_output(getattr(interaction, "output_text", None), request, self.name)
