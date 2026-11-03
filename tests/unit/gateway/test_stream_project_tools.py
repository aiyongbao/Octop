"""tests/unit/gateway/test_stream_project_tools.py"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from octop.infra.gateway.process.stream_project import enrich_tool_stream_chunk


def test_enrich_tool_stream_chunk_adds_display_name():
    chunk = {"type": "tool_call_chunk", "name": "read_file", "args": "{"}
    out = enrich_tool_stream_chunk(chunk, "zh")
    assert out["display_name"] == "读取文件"
    assert out["name"] == "read_file"


def test_enrich_tool_stream_chunk_ignores_non_tool():
    chunk = {"type": "token", "content": "hi"}
    assert enrich_tool_stream_chunk(chunk, "zh") is chunk


def test_tool_start_event_carries_localized_hint():
    from octop_gateway.models import MessageEvent

    from octop.i18n import channel_tool_hint_start

    label = "读取文件"
    event = MessageEvent.tool_start(
        label,
        tool_hint_text=channel_tool_hint_start(label, "zh"),
    )
    assert event.metadata["tool_hint_text"] == channel_tool_hint_start(label, "zh")
    assert "正在调用工具" in event.metadata["tool_hint_text"]


async def projected(monkeypatch, chunks):
    from octop.infra.gateway.process import stream_project

    monkeypatch.setattr(stream_project, "harness_workspace_for_agent", lambda *_args: None)

    async def stream():
        for chunk in chunks:
            yield chunk

    return [
        event
        async for event in stream_project._project_chunks(
            stream(),
            agent_manager=None,
            agent_id="test",
            locale="zh",
            usage_tracker=None,
            history_tracker=None,
            projection_state=None,
            hitl_coordinator=None,
            hitl_ctx=None,
        )
    ]


@pytest.mark.asyncio
async def test_projector_emits_explicit_task_steps_without_raw_tool_arguments(monkeypatch):
    todos = [{"content": "核验 ADQ 数据", "status": "in_progress"}]
    events = await projected(
        monkeypatch,
        [
            {
                "type": "tool_call_chunk",
                "id": "plan",
                "name": "write_todos",
                "args": json.dumps({"todos": todos}),
            },
            {
                "type": "tool_result",
                "messages": [ToolMessage("updated", tool_call_id="plan", status="success")],
            },
            {
                "type": "tool_call_chunk",
                "id": "exec",
                "name": "execute",
                "args": '{"password":"never-forward-this"}',
            },
            {
                "type": "tool_result",
                "messages": [
                    ToolMessage("private tool result", tool_call_id="exec", status="error")
                ],
            },
        ],
    )
    progress = [e.metadata["task_progress"] for e in events if "task_progress" in e.metadata]
    assert progress == [
        {
            "version": 1,
            "source": "write_todos",
            "steps": [{"title": "核验 ADQ 数据", "status": "in_progress"}],
        }
    ]
    assert [e.metadata["tool_call_id"] for e in events if e.type == "tool_end"] == ["plan", "exec"]
    wire = json.dumps([e.model_dump() for e in events])
    assert "never-forward-this" not in wire and "private tool result" not in wire


@pytest.mark.asyncio
async def test_projector_parallel_tools_do_not_leave_phantom_active_calls(monkeypatch):
    events = await projected(
        monkeypatch,
        [
            {"type": "tool_call_chunk", "id": "a", "index": 0, "name": "grep", "args": "{}"},
            {"type": "tool_call_chunk", "id": "b", "index": 1, "name": "execute", "args": "{}"},
            {
                "type": "tool_result",
                "messages": [
                    ToolMessage("ok", tool_call_id="a"),
                    ToolMessage("ok", tool_call_id="b"),
                ],
            },
            {"type": "tool_call_chunk", "id": "c", "index": 0, "name": "execute", "args": "{}"},
            {
                "type": "tool_result",
                "messages": [
                    ToolMessage("old", tool_call_id="a"),
                    ToolMessage("ok", tool_call_id="c"),
                ],
            },
        ],
    )
    started = [e.metadata["tool_call_id"] for e in events if e.type == "tool_start"]
    ended = [e.metadata["tool_call_id"] for e in events if e.type == "tool_end"]
    assert started == ["a", "b", "c"] and ended == started
    assert "grepexecute" not in json.dumps([e.model_dump() for e in events])


@pytest.mark.asyncio
async def test_failed_plan_write_does_not_emit_business_progress(monkeypatch):
    events = await projected(
        monkeypatch,
        [
            {
                "type": "tool_call_chunk",
                "id": "plan",
                "name": "write_todos",
                "args": '{"todos":[{"content":"未执行","status":"completed"}]}',
            },
            {
                "type": "tool_result",
                "messages": [ToolMessage("failed", tool_call_id="plan", status="error")],
            },
        ],
    )
    assert not any("task_progress" in e.metadata for e in events)


@pytest.mark.asyncio
async def test_nonstreaming_provider_plan_is_projected_from_latest_assistant_update(monkeypatch):
    events = await projected(
        monkeypatch,
        [
            {
                "type": "state_update",
                "node": "model",
                "data": {
                    "messages": [
                        AIMessage(
                            "",
                            tool_calls=[
                                {
                                    "id": "plan",
                                    "name": "write_todos",
                                    "args": {
                                        "todos": [{"content": "核验数据", "status": "in_progress"}]
                                    },
                                }
                            ],
                        )
                    ]
                },
            },
            {
                "type": "tool_result",
                "messages": [ToolMessage("updated", tool_call_id="plan", status="success")],
            },
        ],
    )
    assert [
        e.metadata["task_progress"]["steps"] for e in events if "task_progress" in e.metadata
    ] == [[{"title": "核验数据", "status": "in_progress"}]]
