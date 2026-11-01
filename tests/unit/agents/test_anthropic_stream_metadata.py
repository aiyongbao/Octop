"""Replay the SDK stream that failed after tools completed on 2026-11-01."""

from __future__ import annotations

import inspect
import json
from copy import deepcopy
from typing import Any

import httpx
import pytest
from anthropic import Anthropic, AsyncAnthropic
from anthropic.types import RawMessageDeltaEvent
from anthropic.types.beta import BetaRawMessageDeltaEvent
from langchain_anthropic import ChatAnthropic

from octop.infra.agents.providers.anthropic_stream import (
    install_anthropic_stream_metadata_compat,
)


@pytest.fixture(autouse=True)
def isolated_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ChatAnthropic,
        "_make_message_chunk_from_anthropic_event",
        inspect.unwrap(ChatAnthropic._make_message_chunk_from_anthropic_event),
    )


def delta(context: Any, event_type: Any = RawMessageDeltaEvent) -> Any:
    return event_type.model_validate(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 7},
            "context_management": context,
        }
    )


@pytest.mark.parametrize("event_type", [RawMessageDeltaEvent, BetaRawMessageDeltaEvent])
@pytest.mark.parametrize(
    "context", [None, {"applied_edits": []}, {"applied_edits": [], "relay_extension": {"a": 1}}]
)
def test_preserves_sdk_and_dictionary_metadata(event_type: Any, context: Any) -> None:
    install_anthropic_stream_metadata_compat()
    model = ChatAnthropic(model="claude-test", api_key="test")
    event = delta(context, event_type)
    before = deepcopy(event.model_dump())

    chunk, _ = model._make_message_chunk_from_anthropic_event(
        event, stream_usage=True, coerce_content_to_string=False
    )

    assert chunk is not None
    assert chunk.response_metadata.get("context_management") == context
    assert chunk.response_metadata["stop_reason"] == "end_turn"
    assert chunk.usage_metadata is not None
    assert chunk.usage_metadata["output_tokens"] == 7
    assert event.model_dump() == before


def test_install_is_idempotent() -> None:
    install_anthropic_stream_metadata_compat()
    first = ChatAnthropic._make_message_chunk_from_anthropic_event
    install_anthropic_stream_metadata_compat()
    assert ChatAnthropic._make_message_chunk_from_anthropic_event is first


def test_invalid_metadata_is_not_silently_dropped() -> None:
    install_anthropic_stream_metadata_compat()
    model = ChatAnthropic(model="claude-test", api_key="test")
    with pytest.raises(AttributeError):
        model._make_message_chunk_from_anthropic_event(
            delta("invalid"), stream_usage=True, coerce_content_to_string=False
        )


def sse(tool_call: bool = False) -> str:
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "fixture-message",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "claude-test",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 4, "output_tokens": 0},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 7},
            "context_management": {"applied_edits": []},
        },
        {"type": "message_stop"},
    ]
    if tool_call:
        events[1]["content_block"] = {
            "type": "tool_use",
            "id": "fixture-call",
            "name": "fixture_tool",
            "input": {},
        }
        events[2]["delta"] = {"type": "input_json_delta", "partial_json": '{"x":1}'}
        events[4]["delta"]["stop_reason"] = "tool_use"
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


def response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse())


def test_sync_stream_finishes_with_metadata() -> None:
    install_anthropic_stream_metadata_compat()
    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        sdk = Anthropic(api_key="test", http_client=http)
        model = ChatAnthropic(model="claude-test", api_key="test")
        model.__dict__["_client"] = sdk
        chunks = list(model.stream("synthetic fixture"))

    assert "".join(chunk.text for chunk in chunks) == "ok"
    assert chunks[-1].response_metadata["context_management"] == {"applied_edits": []}
    assert chunks[-1].usage_metadata is not None
    assert chunks[-1].usage_metadata["output_tokens"] == 7


async def test_async_stream_finishes_with_metadata() -> None:
    install_anthropic_stream_metadata_compat()
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        sdk = AsyncAnthropic(api_key="test", http_client=http)
        model = ChatAnthropic(model="claude-test", api_key="test")
        model.__dict__["_async_client"] = sdk
        chunks = [chunk async for chunk in model.astream("synthetic fixture")]

    assert "".join(chunk.text for chunk in chunks) == "ok"
    assert chunks[-1].response_metadata["context_management"] == {"applied_edits": []}
    assert chunks[-1].usage_metadata is not None
    assert chunks[-1].usage_metadata["output_tokens"] == 7


async def test_tool_arguments_and_usage_survive_final_metadata() -> None:
    install_anthropic_stream_metadata_compat()

    def tool_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse(True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(tool_response)) as http:
        sdk = AsyncAnthropic(api_key="test", http_client=http)
        model = ChatAnthropic(model="claude-test", api_key="test")
        model.__dict__["_async_client"] = sdk
        chunks = [chunk async for chunk in model.astream("synthetic fixture")]
    combined = chunks[0]
    for chunk in chunks[1:]:
        combined += chunk
    assert combined.tool_calls == [
        {"name": "fixture_tool", "args": {"x": 1}, "id": "fixture-call", "type": "tool_call"}
    ]
    assert combined.response_metadata["stop_reason"] == "tool_use"
    assert combined.usage_metadata is not None
    assert combined.usage_metadata["output_tokens"] == 7
