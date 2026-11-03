"""Tool correlation and explicit business-plan projection, without LLM calls."""

import json

from langchain_core.messages import ToolMessage

from octop.infra.gateway.process.tool_progress import ToolProgressTracker


def test_parallel_calls_and_reused_indexes_do_not_concatenate_names():
    tracker = ToolProgressTracker()
    grep = tracker.feed({"id": "g1", "index": 0, "name": "grep", "args": "{}"})
    execute = tracker.feed({"id": "e1", "index": 1, "name": "execute", "args": "{}"})
    results = tracker.results(
        {"messages": [ToolMessage("ok", tool_call_id="e1"), ToolMessage("ok", tool_call_id="g1")]}
    )
    assert [c.name for c, _ in results] == ["execute", "grep"]
    assert grep.ended and execute.ended
    next_call = tracker.feed({"id": "e2", "index": 0, "name": "execute", "args": "{}"})
    assert next_call.name == "execute" and not next_call.ended


def test_replayed_results_cannot_end_live_call_or_publish_old_plan():
    tracker = ToolProgressTracker()
    live = tracker.feed({"id": "live", "name": "execute", "args": "{}"})
    assert tracker.results({"messages": [{"tool_call_id": "old", "name": "write_todos"}]}) == []
    assert not live.ended
    assert len(tracker.results({"messages": [{"tool_call_id": "live"}]})) == 1
    assert tracker.results({"messages": [{"tool_call_id": "live"}]}) == []


def test_successful_write_todos_publishes_business_steps_only():
    tracker = ToolProgressTracker()
    todos = [
        {"content": "核验千川数据", "status": "completed"},
        {"content": "核验 ADQ 数据", "status": "in_progress"},
    ]
    payload = json.dumps({"todos": todos}, ensure_ascii=False)
    tracker.feed({"id": "plan", "name": "write_todos", "args": payload[:20]})
    tracker.feed({"index": 0, "args": payload[20:]})
    call, status = tracker.results(
        {"messages": [ToolMessage("updated", tool_call_id="plan", status="success")]}
    )[0]
    assert call.progress(status) == {
        "version": 1,
        "source": "write_todos",
        "steps": [
            {"title": "核验千川数据", "status": "completed"},
            {"title": "核验 ADQ 数据", "status": "in_progress"},
        ],
    }
    assert call.progress("error") is None


def test_partial_or_invalid_plan_never_replaces_valid_progress():
    tracker = ToolProgressTracker()
    call = tracker.feed({"id": "plan", "name": "write_todos", "args": '{"todos": ['})
    assert call.progress("success") is None
    call.arguments = json.dumps({"todos": [{"content": "查数据", "status": "guessed"}]})
    assert call.progress("success") is None


def test_late_call_id_retains_original_event_identity():
    tracker = ToolProgressTracker()
    first = tracker.feed({"index": 0, "name": "execute", "args": "{"})
    second = tracker.feed({"index": 0, "id": "real-id", "args": "}"})
    assert first is second
    assert (
        tracker.results({"messages": [{"tool_call_id": "real-id"}]})[0][0].call_id == first.call_id
    )
