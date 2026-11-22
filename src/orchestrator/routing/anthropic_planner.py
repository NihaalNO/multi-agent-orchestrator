"""Claude-backed planner (Anthropic Messages API).

Design
------
* Prompt, schema and output parsing are shared with every provider (``llm_common``).
* **Structured outputs** (``output_config.format``) constrain the reply to ``PLAN_SCHEMA``, so
  the planner never has to scrape JSON out of prose. Task inputs are free-form per tool, so
  the model returns them as a JSON *string* (``inputs_json``) that we parse and then validate
  against the chosen operation's real schema in the router.
* The model sees only the ``ToolCatalog`` (capabilities, descriptions, input schemas). It gets
  no permissions, no runtime state and no ability to execute anything.
* User text is wrapped in ``<user_request>`` tags and the system prompt says to treat it as
  data, as a defence against prompt injection in requests.
* ``unsupported`` lets the model say "no capability covers this" instead of inventing one.
* Server-side refusal fallbacks are enabled (``fallbacks="default"``) so a safety-classifier
  decline on the primary model is retried on a fallback model inside the same call.
* API errors are mapped onto orchestrator errors with the right ``recoverable`` flag. The SDK
  already retries 408/409/429/5xx and connection errors (``max_retries``, default 2).
"""

from __future__ import annotations

import anthropic

from orchestrator.contracts import PlanningRequest, PlanProposal
from orchestrator.errors import ConfigurationError, PlanError, PlannerUnavailableError
from orchestrator.observability.audit import AuditLog
from orchestrator.routing.llm_common import (  # re-exported: shared by every provider
    PLAN_SCHEMA,
    SYSTEM_PROMPT,
    no_tools_proposal,
    parse_plan_output,
    user_message,
)
from orchestrator.routing.planner import ToolCatalog

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

class AnthropicPlanner:
    name = "anthropic"

    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = "medium",
        max_tokens: int = 16000,
        audit: AuditLog | None = None,
    ) -> None:
        # The zero-arg client resolves credentials from ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN
        # or an `ant auth login` profile.
        self._client = client if client is not None else anthropic.AsyncAnthropic()
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self._audit = audit or AuditLog()

    async def plan(self, request: PlanningRequest, catalog: ToolCatalog) -> PlanProposal:
        if not catalog.entries:
            return no_tools_proposal(request, self.name)
        try:
            response = await self._client.beta.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message(request, catalog)}],
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": PLAN_SCHEMA},
                },
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise ConfigurationError(f"Anthropic credentials rejected: {exc.message}") from exc
        except anthropic.BadRequestError as exc:
            raise PlanError(f"planner request rejected: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise PlannerUnavailableError("Anthropic API rate limit reached") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise PlannerUnavailableError(f"Anthropic API error {exc.status_code}") from exc
            raise PlanError(f"planner request failed ({exc.status_code}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise PlannerUnavailableError("could not reach the Anthropic API") from exc

        self._audit.record(
            "planner.response",
            request_id=request.request_id,
            model=getattr(response, "model", self._model),
            stop_reason=response.stop_reason,
            api_request_id=getattr(response, "_request_id", None),
        )
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise PlanError(
                "the model declined to plan this request",
                details={"category": getattr(details, "category", None)},
            )
        if response.stop_reason == "max_tokens":
            raise PlanError("planner output was cut off (max_tokens)")

        text = next((b.text for b in response.content if b.type == "text"), None)
        return parse_plan_output(text, request, self.name)
