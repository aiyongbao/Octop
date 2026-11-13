"""Keep historical checkpoint tool results out of the current delivery stream."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from functools import wraps
from typing import Any, ParamSpec

P = ParamSpec("P")


def live_tool_stream(
    *, resume: bool = False
) -> Callable[
    [Callable[P, AsyncIterator[dict[str, Any]]]], Callable[P, AsyncIterator[dict[str, Any]]]
]:
    """Wrap the public stream boundary independently of its internal pipeline."""

    def decorate(
        stream: Callable[P, AsyncIterator[dict[str, Any]]],
    ) -> Callable[P, AsyncIterator[dict[str, Any]]]:
        @wraps(stream)
        async def guarded(*args: P.args, **kwargs: P.kwargs) -> AsyncIterator[dict[str, Any]]:
            source = stream(*args, **kwargs)
            try:
                async for chunk in isolate_live_tool_results(source, resume=resume):
                    yield chunk
            finally:
                # Release the underlying thread lock promptly on stop/disconnect.
                close = getattr(source, "aclose", None)
                if close is not None:
                    await close()

        return guarded

    return decorate


def _attr(message: Any, name: str) -> Any:
    return message.get(name) if isinstance(message, dict) else getattr(message, name, None)


def _calls(messages: list[Any]) -> set[str]:
    return {
        call["id"]
        for message in messages
        for call in (_attr(message, "tool_calls") or [])
        if isinstance(call, dict) and isinstance(call.get("id"), str) and call["id"]
    }


async def isolate_live_tool_results(
    chunks: AsyncIterator[dict[str, Any]], *, resume: bool = False
) -> AsyncIterator[dict[str, Any]]:
    """Authorize results by live call ID, not by whether *any* tool ran this turn.

    Harness history repair may emit a full checkpoint after a live tool call.
    On approval resume only, seed unanswered calls from the first checkpoint;
    those calls were issued before the pause and are intentionally executing now.
    """
    live_ids: set[str] = set()
    first_snapshot = True
    async for chunk in chunks:
        kind = chunk.get("type")
        if kind == "tool_call_chunk":
            call_id = chunk.get("id")
            if isinstance(call_id, str) and call_id:
                live_ids.add(call_id)
        elif kind == "state_update":
            data = chunk.get("data")
            if isinstance(data, dict) and isinstance(data.get("messages"), list):
                # Complete model updates also cover non-streaming providers.
                live_ids.update(_calls(data["messages"]))
        elif kind == "state_snapshot" and first_snapshot:
            first_snapshot = False
            data = chunk.get("data")
            if resume and isinstance(data, dict) and isinstance(data.get("messages"), list):
                messages = data["messages"]
                answered = {_attr(m, "tool_call_id") for m in messages}
                start = max(
                    (
                        i
                        for i, m in enumerate(messages)
                        if _attr(m, "type") in {"human", "user"} or _attr(m, "role") == "user"
                    ),
                    default=0,
                )
                live_ids.update(_calls(messages[start:]) - answered)
        elif kind == "tool_result":
            messages = chunk.get("messages")
            if isinstance(messages, list):
                current = [m for m in messages if _attr(m, "tool_call_id") in live_ids]
                if not current:
                    continue
                chunk = {**chunk, "messages": current}
            elif chunk.get("id", chunk.get("tool_call_id")) not in live_ids:
                continue
        yield chunk
