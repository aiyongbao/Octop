"""Default the per-call output budget to the routed model's ``max_output_tokens``.

Without an explicit agent ``max_tokens`` the LLM client falls back to its own
default (``ChatAnthropic`` uses 4096 for model ids it does not know), which
truncates long answers and tool-call arguments. Runs after harness model routing
so the cap always matches the model actually being called.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from octop_harness.middleware.model_settings import apply_configurable_model_settings


def _with_output_budget(request: ModelRequest[Any]) -> ModelRequest[Any]:
    settings = dict(request.model_settings or {})
    if settings.get("max_tokens") is not None:
        return request
    cap = getattr(request.model, "_harness_max_output_tokens", 0)
    if not isinstance(cap, int) or cap <= 0:
        return request
    settings["max_tokens"] = cap
    # Re-run the harness reservation so prompt + output stays inside the context window.
    return apply_configurable_model_settings(request.override(model_settings=settings))


class OutputBudgetMiddleware(AgentMiddleware[Any, Any]):
    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(_with_output_budget(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(_with_output_budget(request))


__all__ = ["OutputBudgetMiddleware"]
