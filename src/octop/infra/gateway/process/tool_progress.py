"""Correlate live tool calls and publish their explicit task-list updates.

Tool indexes are reused on every model round. Retain call IDs instead of one
"active" index so parallel results and history replay cannot end the wrong call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

_ARGS_LIMIT = 65536
_PLAN_LIMIT = 12
_STATUSES = frozenset({"pending", "in_progress", "completed"})


def _value(message: Any, key: str) -> Any:
    if isinstance(message, dict):
        data = message.get("data")
        return (data if isinstance(data, dict) else message).get(key)
    return getattr(message, key, None)


@dataclass
class LiveToolCall:
    """One invocation, including its original event identity."""

    call_id: str
    external_id: str = ""
    name: str = ""
    arguments: str = ""
    started: bool = False
    ended: bool = False

    def progress(self, status: str) -> dict[str, Any] | None:
        """Expose only successful, explicit write_todos plans, never inferred work."""
        if self.name.rsplit("/", 1)[-1] != "write_todos" or status != "success":
            return None
        try:
            data = json.loads(self.arguments)
        except (ValueError, TypeError):
            return None
        todos = data.get("todos") if isinstance(data, dict) else None
        if not isinstance(todos, list) or len(todos) > _PLAN_LIMIT:
            return None
        steps = []
        for item in todos:
            if not isinstance(item, dict):
                return None
            content, state = item.get("content"), item.get("status")
            if (
                not isinstance(content, str)
                or not content.strip()
                or not isinstance(state, str)
                or state not in _STATUSES
            ):
                return None
            steps.append({"title": content.strip()[:240], "status": state})
        return {"version": 1, "source": "write_todos", "steps": steps}


@dataclass
class ToolProgressTracker:
    """Turn-local call correlation; raw arguments are never forwarded or logged."""

    calls: dict[str, LiveToolCall] = field(default_factory=dict)
    indexes: dict[tuple[str, str], str] = field(default_factory=dict)
    external: dict[str, str] = field(default_factory=dict)
    serial: int = 0

    def feed(self, chunk: dict[str, Any]) -> LiveToolCall:
        """Accumulate one argument/name fragment, isolating index reuse by ID."""
        index = (str(chunk.get("node") or ""), str(chunk.get("index", 0)))
        external_id = str(chunk.get("id") or "")
        call = self.calls.get(self.external.get(external_id, "")) if external_id else None
        if call is None:
            previous = self.calls.get(self.indexes.get(index, ""))
            if previous and not previous.ended and (not external_id or not previous.external_id):
                call = previous
            else:
                self.serial += 1
                key = external_id or f"im-tool-{self.serial}"
                call = LiveToolCall(call_id=key)
                self.calls[key] = call
        if external_id:
            call.external_id = external_id
            self.external[external_id] = call.call_id
        self.indexes[index] = call.call_id
        name = chunk.get("name")
        if isinstance(name, str):
            call.name += name
        args = chunk.get("args")
        if isinstance(args, dict):
            call.arguments = json.dumps(args, ensure_ascii=False)[:_ARGS_LIMIT]
        elif isinstance(args, str):
            call.arguments = (call.arguments + args)[:_ARGS_LIMIT]
        return call

    def results(self, chunk: dict[str, Any]) -> list[tuple[LiveToolCall, str]]:
        """End matching live calls once; ignore historical/unrelated result IDs."""
        messages = chunk.get("messages")
        records = messages if isinstance(messages, list) else [chunk]
        found = []
        for message in records:
            result_id = str(_value(message, "tool_call_id") or _value(message, "id") or "")
            call = self.calls.get(self.external.get(result_id, "")) if result_id else None
            # Older protocols emit a bare result without messages/IDs. Only a
            # unique live call can be safely paired in that legacy shape.
            if call is None and not result_id and not isinstance(messages, list):
                pending = [c for c in self.calls.values() if not c.ended]
                call = pending[0] if len(pending) == 1 else None
            if call is None or call.ended:
                continue
            call.ended = True
            name = _value(message, "name")
            if isinstance(name, str) and name:
                call.name = name
            found.append((call, str(_value(message, "status") or "")))
        return found

    def complete_calls(self, data: Any) -> list[LiveToolCall]:
        """Read the latest assistant update for non-streaming tool providers.

        Full assistant updates also repair partial argument buffers. A history
        dump ending in a human/tool message must not create old live calls.
        """
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list) or not messages:
            return []
        message = messages[-1]
        role = _value(message, "type") or _value(message, "role")
        if role not in ("ai", "assistant"):
            return []
        calls = _value(message, "tool_calls")
        if not isinstance(calls, list):
            return []
        found = []
        for index, raw in enumerate(calls):
            if not isinstance(raw, dict) or not raw.get("id") or not raw.get("name"):
                continue
            external_id = str(raw["id"])
            call = self.calls.get(self.external.get(external_id, ""))
            if call is None:
                call = self.feed({**raw, "index": index})
            elif not call.ended:
                call.name = str(raw["name"])
                if isinstance(raw.get("args"), dict):
                    call.arguments = json.dumps(raw["args"], ensure_ascii=False)[:_ARGS_LIMIT]
            found.append(call)
        return found
