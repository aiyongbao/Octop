"""Reject incomplete model output before it can end the graph successfully."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)
INCOMPLETE_RESPONSE = "octop_incomplete_model_response"


def response_problem(response: ModelResponse[Any]) -> str:
    if not response.result:
        return "empty_final_response"
    for message in response.result:
        if not isinstance(message, AIMessage):
            continue
        stop = message.response_metadata.get("stop_reason") or message.response_metadata.get(
            "finish_reason"
        )
        if stop in {"max_tokens", "length", "model_length"}:
            return "output_limit"
        if message.invalid_tool_calls:
            return "invalid_tool_arguments"
        if message.tool_calls:
            continue
        content = message.content
        if isinstance(content, str):
            visible = bool(content.strip())
        else:
            visible = any(
                bool(block.strip())
                if isinstance(block, str)
                else (
                    block.get("type") in {"image", "image_url", "audio", "video", "file"}
                    or (
                        block.get("type") in {"text", "refusal"}
                        and bool(str(block.get("text") or block.get("refusal") or "").strip())
                    )
                )
                for block in content
            )
        if not visible:
            return "empty_final_response"
    return ""


class ResponseIntegrityMiddleware(AgentMiddleware[Any, Any]):
    """Retry an empty inference once; tools are never re-executed by this retry.

    A second empty result is an error, leaving the checkpoint and todos open.
    Existing model retry policy still owns provider failures and truncation.
    """

    @staticmethod
    def _check(response: ModelResponse[Any], attempt: int) -> bool:
        problem = response_problem(response)
        if not problem:
            return True
        # Log only the reason: model text, tool arguments and signatures are private.
        logger.warning("Model response rejected: reason=%s attempt=%s", problem, attempt + 1)
        if problem != "empty_final_response" or attempt:
            raise ValueError(f"{INCOMPLETE_RESPONSE}: {problem}")
        return False

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        response = handler(request)
        if not self._check(response, 0):
            response = handler(request)
            self._check(response, 1)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        response = await handler(request)
        if not self._check(response, 0):
            response = await handler(request)
            self._check(response, 1)
        return response
