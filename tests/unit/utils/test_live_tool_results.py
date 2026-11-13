from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from octop.infra.utils.live_tool_results import isolate_live_tool_results, live_tool_stream


async def _chunks(*events: dict[str, Any]):
    for event in events:
        yield event


async def test_history_replay_after_live_query_does_not_resend_old_attachments():
    old = ToolMessage(content="old report", name="send_file_to_user", tool_call_id="old-send")
    live = ToolMessage(content="query done", name="execute", tool_call_id="query")
    result = [
        c
        async for c in isolate_live_tool_results(
            _chunks(
                {"type": "tool_call_chunk", "id": "query", "name": "execute"},
                {"type": "tool_result", "messages": [old, live]},
            )
        )
    ]
    assert result[-1]["messages"] == [live]
    assert old.content == "old report"  # The persisted history is not modified.


async def test_live_send_is_allowed_but_missing_id_and_history_only_are_not():
    current = {"tool_call_id": "new-send", "name": "send_file_to_user", "content": "new"}
    result = [
        c
        async for c in isolate_live_tool_results(
            _chunks(
                {"type": "tool_call_chunk", "id": "new-send", "name": "send_file_to_user"},
                {
                    "type": "tool_result",
                    "messages": [
                        {"tool_call_id": "old", "name": "send_file_to_user", "content": "old"},
                        {"name": "send_file_to_user", "content": "unidentified"},
                        current,
                    ],
                },
                {"type": "tool_result", "messages": [{"tool_call_id": "old"}]},
            )
        )
    ]
    assert len(result) == 2
    assert result[-1]["messages"] == [current]


async def test_nonstreaming_provider_complete_model_update_registers_calls():
    ai = AIMessage(content="", tool_calls=[{"id": "send", "name": "send_file", "args": {}}])
    event = {"type": "tool_result", "messages": [ToolMessage("ok", tool_call_id="send")]}
    result = [
        c
        async for c in isolate_live_tool_results(
            _chunks({"type": "state_update", "data": {"messages": [ai]}}, event)
        )
    ]
    assert result[-1] == event


@pytest.mark.parametrize("resume,expected", [(False, 0), (True, 1)])
async def test_approval_resume_allows_only_unanswered_calls_since_last_user(resume, expected):
    def ai(call_id):
        return AIMessage("", tool_calls=[{"id": call_id, "name": "send_file", "args": {}}])

    messages = [
        ai("stale"),
        HumanMessage("new request"),
        ai("answered"),
        ToolMessage("ok", tool_call_id="answered"),
        ai("approved"),
    ]
    result = [
        c
        async for c in isolate_live_tool_results(
            _chunks(
                {"type": "state_snapshot", "data": {"messages": messages}},
                {
                    "type": "tool_result",
                    "messages": [
                        ToolMessage("ok", tool_call_id=x) for x in ["stale", "answered", "approved"]
                    ],
                },
            ),
            resume=resume,
        )
    ]
    tools = [m for c in result if c["type"] == "tool_result" for m in c["messages"]]
    assert len(tools) == expected
    if tools:
        assert tools[0].tool_call_id == "approved"


async def test_each_invocation_has_independent_eligibility():
    event = {"type": "tool_result", "messages": [{"tool_call_id": "prior"}]}
    first = [
        c
        async for c in isolate_live_tool_results(
            _chunks({"type": "tool_call_chunk", "id": "prior"}, event)
        )
    ]
    second = [c async for c in isolate_live_tool_results(_chunks(event))]
    assert len(first) == 2
    assert second == []


async def test_stream_decorator_preserves_signature_and_filters_delegated_pipeline():
    import inspect

    class Manager:
        @live_tool_stream()
        async def stream(self, agent_id: str, request: dict[str, Any]):
            pipeline = _chunks(
                {"type": "tool_call_chunk", "id": "live"},
                {
                    "type": "tool_result",
                    "messages": [{"tool_call_id": "old"}, {"tool_call_id": "live"}],
                },
            )
            async for chunk in pipeline:
                yield chunk

    rows = [x async for x in Manager().stream("a", {})]
    assert rows[-1]["messages"] == [{"tool_call_id": "live"}]
    assert list(inspect.signature(Manager.stream).parameters) == ["self", "agent_id", "request"]


async def test_closing_decorated_stream_releases_underlying_execution():
    released = []

    @live_tool_stream()
    async def stream():
        try:
            yield {"type": "token", "content": "running"}
        finally:
            released.append(True)

    iterator = stream()
    await anext(iterator)
    await iterator.aclose()
    assert released == [True]
