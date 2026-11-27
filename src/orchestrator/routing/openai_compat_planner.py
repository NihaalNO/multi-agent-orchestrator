"""Planner for providers with an OpenAI-compatible Chat Completions API: Groq and Ollama.

One implementation, configured by a ``ProviderProfile`` per provider, held to the same contract as
the Claude and Gemini planners (shared prompt, schema and parser in ``llm_common``).

Structured output — the part that differs by provider and model:

* ``json_schema``: the provider constrains output to ``PLAN_SCHEMA`` (``response_format`` with a
  JSON Schema; ``strict`` where the provider enforces it).
* ``json_object``: JSON mode only — valid JSON, but the schema is not enforced, so the schema is
  put in the instructions instead. Used for models without schema support (e.g. Llama models on
  Groq) and Ollama's cloud.

Either way the output is parsed and validated by ``parse_plan_output`` and then re-verified by the
router, so a weaker output mode can cause a rejected plan but never an unchecked one.
``ORCH_LLM_JSON_MODE`` overrides the automatic choice.

Uses the official ``openai`` SDK purely as an HTTP client for these providers' compatible
endpoints (install the ``openai-compat`` extra). No request goes to OpenAI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from orchestrator.contracts import PlanningRequest, PlanProposal
from orchestrator.errors import ConfigurationError, PlanError, PlannerUnavailableError
from orchestrator.observability.audit import AuditLog
from orchestrator.routing.llm_common import PLAN_SCHEMA, SYSTEM_PROMPT, no_tools_proposal, parse_plan_output, user_message
from orchestrator.routing.planner import ToolCatalog

JsonMode = Literal["json_schema", "json_object"]
INSTALL_HINT = "install the extra: pip install 'multi-tool-orchestrator[openai-compat]'"


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    label: str
    base_url: str
    default_model: str
    key_env: tuple[str, ...] = ()
    key_required: bool = True
    placeholder_key: str | None = None  # sent when no key is needed but the SDK wants one
    # Models whose output the provider constrains to a JSON Schema. None = all models.
    schema_models: frozenset[str] | None = None
    strict: bool = False
    # Base-URL prefixes (e.g. a hosted variant) that only support JSON mode.
    json_object_only_urls: tuple[str, ...] = ()
    # Name of the output-limit parameter. Reasoning models (e.g. gpt-oss) spend part of it on
    # hidden reasoning, so provider defaults can cut a plan off before the JSON is finished.
    max_tokens_param: str = "max_completion_tokens"
    notes: Mapping[str, str] = field(default_factory=dict)
    # Generic endpoints: base URL from these env vars when ORCH_LLM_BASE_URL is unset.
    base_url_env: tuple[str, ...] = ()
    # Only these base-URL prefixes are known to enforce JSON Schemas and to take
    # ``max_tokens_param``; anything else gets JSON mode and ``max_tokens``. None = no such split.
    native_urls: tuple[str, ...] | None = None

    def is_native(self, base_url: str) -> bool:
        return self.native_urls is None or any(base_url.startswith(u) for u in self.native_urls)

    def json_mode(self, model: str, base_url: str) -> JsonMode:
        if any(base_url.startswith(u) for u in self.json_object_only_urls) or not self.is_native(base_url):
            return "json_object"
        if self.schema_models is None or model in self.schema_models:
            return "json_schema"
        return "json_object"

    def tokens_param(self, base_url: str) -> str:
        return self.max_tokens_param if self.is_native(base_url) else "max_tokens"

    def resolve_base_url(self, explicit: str | None, environ: Mapping[str, str] | None = None) -> str:
        env = os.environ if environ is None else environ
        if explicit:
            return explicit
        for var in self.base_url_env:
            if env.get(var):
                return env[var]
        return self.base_url

    def key_needed(self, base_url: str) -> bool:
        """A local server (localhost, 127.0.0.1, ::1) usually takes no key; hosted ones do."""
        from urllib.parse import urlsplit

        host = (urlsplit(base_url).hostname or "").lower()
        return self.key_required and host not in ("localhost", "127.0.0.1", "::1") and not host.endswith(".localhost")

    def api_key(self, explicit: str | None, environ: Mapping[str, str] | None = None) -> str | None:
        env = os.environ if environ is None else environ
        if explicit:
            return explicit
        for var in self.key_env:
            if env.get(var):
                return env[var]
        return self.placeholder_key


# Sources: console.groq.com/docs (openai, structured-outputs, models) and docs.ollama.com
# (openai-compatibility, structured-outputs), read 2026-11-22.
PROVIDERS: dict[str, ProviderProfile] = {
    "groq": ProviderProfile(
        name="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        default_model="openai/gpt-oss-120b",
        key_env=("GROQ_API_KEY",),
        # Groq enforces strict schemas only on these; everything else (incl. Llama) gets JSON mode.
        schema_models=frozenset({"openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b",
                                 "meta-models/muse-spark-open"}),
        strict=True,
    ),
    "ollama": ProviderProfile(
        name="ollama",
        label="Ollama",
        base_url="http://localhost:11434/v1",
        default_model="gpt-oss:20b",
        key_env=("OLLAMA_API_KEY",),  # only Ollama's cloud needs one
        key_required=False,
        placeholder_key="ollama",     # local Ollama: "required but ignored"
        max_tokens_param="max_tokens",  # what Ollama's OpenAI-compatible endpoint documents
        schema_models=None,           # local Ollama constrains output for any model
        json_object_only_urls=("https://ollama.com",),  # Ollama's cloud: no structured outputs
    ),
    # Any server that speaks the OpenAI Chat Completions API: OpenAI itself, or a compatible
    # gateway or local server (vLLM, LM Studio, OpenRouter, ...). There is no common model name,
    # so ORCH_LLM_MODEL is required. Strict JSON Schemas and max_completion_tokens only on
    # api.openai.com; other servers get JSON mode and max_tokens unless ORCH_LLM_JSON_MODE says otherwise.
    "openai": ProviderProfile(
        name="openai",
        label="OpenAI-compatible",
        base_url="https://api.openai.com/v1",
        default_model="",
        key_env=("OPENAI_API_KEY",),
        base_url_env=("OPENAI_BASE_URL",),
        placeholder_key=None,
        schema_models=None,
        strict=True,
        native_urls=("https://api.openai.com",),
    ),
}


def openai_compat_error(exc: BaseException) -> tuple[Literal["config", "unavailable", "failure"], str] | None:
    import openai

    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return "config", f"credentials rejected: {exc.message}"
    if isinstance(exc, openai.NotFoundError):
        return "config", f"model or endpoint not found (is the model pulled / the name right?): {exc.message}"
    if isinstance(exc, openai.APIStatusError) and exc.status_code == 410:
        # Gateways retire models but may keep listing them: say so, and how to fix it.
        return "config", f"this model is no longer available (retired); set ORCH_LLM_MODEL to another: {exc.message}"
    if isinstance(exc, openai.APIStatusError) and exc.status_code == 413:
        # e.g. Groq: prompt + output budget exceed the account's tokens-per-minute limit. Retrying
        # cannot help; lower ORCH_LLM_MAX_OUTPUT_TOKENS or use a higher tier.
        return "config", f"request too large for this account's limits (lower ORCH_LLM_MAX_OUTPUT_TOKENS): {exc.message}"
    if isinstance(exc, openai.RateLimitError):
        return "unavailable", "rate limit reached"
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500:
            return "unavailable", f"server error {exc.status_code}"
        return "failure", f"request rejected ({exc.status_code}): {exc.message}"
    if isinstance(exc, openai.APIConnectionError):  # includes APITimeoutError
        return "unavailable", "could not reach the provider"
    return None


def make_openai_compat_client(profile: ProviderProfile, *, api_key: str | None = None,
                              base_url: str | None = None, environ: Mapping[str, str] | None = None) -> Any:
    try:
        import openai
    except ImportError as exc:
        raise ConfigurationError(f"{profile.label} support is not installed; {INSTALL_HINT}") from exc
    url = profile.resolve_base_url(base_url, environ)
    key = profile.api_key(api_key, environ)
    if key is None and profile.key_needed(url):
        raise ConfigurationError(f"no {profile.label} API key: set {' or '.join(profile.key_env)} (or ORCH_LLM_API_KEY)")
    return openai.AsyncOpenAI(base_url=url, api_key=key or "unused")


class OpenAICompatPlanner:
    supports_feedback = True

    def __init__(
        self,
        profile: ProviderProfile,
        client: Any,
        *,
        model: str | None = None,
        base_url: str | None = None,
        json_mode: JsonMode | None = None,
        timeout_s: float = 120.0,
        max_output_tokens: int = 4096,
        audit: AuditLog | None = None,
    ) -> None:
        self._profile = profile
        self._client = client
        self._model = model or profile.default_model
        if not self._model:
            raise ConfigurationError(f"{profile.label} planner needs a model name: set ORCH_LLM_MODEL")
        self._base_url = profile.resolve_base_url(base_url)
        self._json_mode: JsonMode = json_mode or profile.json_mode(self._model, self._base_url)
        self._timeout = timeout_s
        self._max_output_tokens = max_output_tokens
        self._audit = audit or AuditLog()

    @property
    def name(self) -> str:
        return self._profile.name

    @property
    def json_mode(self) -> JsonMode:
        return self._json_mode

    def _request(self, request: PlanningRequest, catalog: ToolCatalog, feedback: str | None = None) -> dict[str, Any]:
        system = SYSTEM_PROMPT
        if self._json_mode == "json_schema":
            schema: dict[str, Any] = {"name": "execution_plan", "schema": PLAN_SCHEMA}
            if self._profile.strict:
                schema["strict"] = True
            response_format: dict[str, Any] = {"type": "json_schema", "json_schema": schema}
        else:
            # JSON mode enforces valid JSON only, so state the schema in the instructions.
            system += ("\nRespond with a single JSON object that matches this JSON Schema exactly, and "
                       "nothing else:\n" + json.dumps(PLAN_SCHEMA))
            response_format = {"type": "json_object"}
        return {
            "model": self._model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user_message(request, catalog, feedback)}],
            "response_format": response_format,
            "timeout": self._timeout,
            self._profile.tokens_param(self._base_url): self._max_output_tokens,
        }

    async def _create_with_retry(self, kwargs: dict[str, Any]) -> Any:
        """One retry when the provider rejects its *own* output for not matching the schema (Groq:
        400 "Failed to validate JSON"). Generation is not deterministic, so a second attempt often
        succeeds; other errors are not retried here (the SDK already retries transient ones)."""
        import openai

        try:
            return await self._client.chat.completions.create(**kwargs)
        except openai.BadRequestError as exc:
            if "failed to validate json" not in str(exc.message).lower():
                raise
            self._audit.record("planner.retry", provider=self.name, reason="output failed schema validation")
            return await self._client.chat.completions.create(**kwargs)

    async def plan(self, request: PlanningRequest, catalog: ToolCatalog, feedback: str | None = None) -> PlanProposal:
        if not catalog.entries:
            return no_tools_proposal(request, self.name)
        label = self._profile.label
        try:
            completion = await self._create_with_retry(self._request(request, catalog, feedback))
        except Exception as exc:
            classified = openai_compat_error(exc)
            if classified is None:
                raise
            kind, message = classified
            if kind == "config":
                raise ConfigurationError(f"{label}: {message}") from exc
            if kind == "unavailable":
                raise PlannerUnavailableError(f"{label}: {message}") from exc
            raise PlanError(f"{label}: {message}") from exc

        choice = completion.choices[0] if completion.choices else None
        self._audit.record(
            "planner.response", request_id=request.request_id, provider=self.name, model=self._model,
            json_mode=self._json_mode, finish_reason=getattr(choice, "finish_reason", None),
        )
        if choice is None:
            raise PlanError(f"{label} returned no choices")
        if getattr(choice.message, "refusal", None):
            raise PlanError(f"the model declined to plan this request: {choice.message.refusal}")
        if choice.finish_reason == "length":
            raise PlanError("planner output was cut off (length)")
        if choice.finish_reason == "content_filter":
            raise PlanError("planner output was blocked by the provider's content filter")
        return parse_plan_output(choice.message.content, request, self.name)
