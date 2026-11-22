"""Error hierarchy.

Every orchestrator error carries:
  * ``code``        – stable machine-readable identifier (safe to expose over the API)
  * ``recoverable`` – whether retrying the *same* operation could succeed.

The execution engine's retry policy reads only ``recoverable``. Validation, permission and
lookup errors are never recoverable, which keeps retries from looping on errors that will
deterministically repeat.
"""

from __future__ import annotations

from typing import Any


class OrchestratorError(Exception):
    code: str = "orchestrator_error"
    recoverable: bool = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "recoverable": self.recoverable,
            "details": self.details,
        }


class ConfigurationError(OrchestratorError):
    code = "configuration_error"


# --- Registry ---------------------------------------------------------------------------


class RegistryError(OrchestratorError):
    code = "registry_error"


class ToolNotFoundError(RegistryError):
    code = "tool_not_found"


class OperationNotFoundError(RegistryError):
    code = "operation_not_found"


class DuplicateToolError(RegistryError):
    code = "duplicate_tool"


class InvalidToolSpecError(RegistryError):
    code = "invalid_tool_spec"


class ToolUnavailableError(RegistryError):
    code = "tool_unavailable"


# --- Validation -------------------------------------------------------------------------


class SchemaValidationError(OrchestratorError):
    code = "schema_validation_error"


class InputValidationError(SchemaValidationError):
    code = "input_validation_error"


class OutputValidationError(SchemaValidationError):
    code = "output_validation_error"


# --- Planning ---------------------------------------------------------------------------


class PlanError(OrchestratorError):
    code = "plan_error"


class PlanOutputError(PlanError):
    """The planner's output could not be read as a plan (empty, off-schema, bad inputs_json).
    Same API error code as PlanError; routing retries it once with the error as feedback."""


class PlanCycleError(PlanError):
    code = "plan_cycle"


class UnknownDependencyError(PlanError):
    code = "unknown_dependency"


class NoMatchingToolError(PlanError):
    code = "no_matching_tool"


class InputResolutionError(PlanError):
    """A ``$ref`` could not be resolved against an upstream task's output."""

    code = "input_resolution_error"


class PlannerUnavailableError(PlanError):
    """The planning backend (e.g. the LLM API) is temporarily unreachable or rate-limited."""

    code = "planner_unavailable"
    recoverable = True


# --- Authorization ----------------------------------------------------------------------


class PermissionDeniedError(OrchestratorError):
    code = "permission_denied"


class ApprovalRequiredError(OrchestratorError):
    code = "approval_required"


# --- Execution --------------------------------------------------------------------------


class ToolExecutionError(OrchestratorError):
    """A tool failed while running. Adapters choose whether the failure is recoverable."""

    code = "tool_execution_error"

    def __init__(
        self,
        message: str,
        *,
        recoverable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.recoverable = recoverable


class TransientToolError(ToolExecutionError):
    """Convenience for adapters: a failure worth retrying (rate limit, network blip)."""

    code = "transient_tool_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, recoverable=True, details=details)


class ToolTimeoutError(ToolExecutionError):
    code = "tool_timeout"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, recoverable=True, details=details)


class ExecutionCancelledError(OrchestratorError):
    code = "execution_cancelled"


# --- Requests ---------------------------------------------------------------------------


class RequestNotFoundError(OrchestratorError):
    code = "request_not_found"


class InvalidRequestStateError(OrchestratorError):
    """The operation does not apply to the request in its current state (e.g. resuming a
    request that is not awaiting approval, or one that is already running)."""

    code = "invalid_request_state"
