"""Run real Deep Agents task dispatch with fake models; no LLM or business I/O."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from octop_harness.protocols.langgraph import LangGraphProtocol

from octop.infra.agents.subagent_events import SubagentEventObserver, merge_subagent_events


class _Model(FakeMessagesListChatModel):
    calls: int = 0
    fail: bool = False

    def bind_tools(self, tools: Any, **kwargs: Any) -> _Model:
        return self

    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.fail and self.calls == 2:
            raise RuntimeError("sample-exception-content")
        return super()._generate(*args, **kwargs)


def _graph(
    *, count: int = 1, fail: bool = False, slow: bool = False, interrupt: bool = False
) -> tuple[Any, list[_Model], asyncio.Event, asyncio.Event]:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    @tool
    async def sample_add(a: int, b: int) -> int:
        """Add sample integers without accessing outside data."""
        if slow:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return a + b

    models = []
    specs = []
    calls = []
    for i in range(count):
        model = _Model(
            fail=fail,
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "same-child-call",
                            "name": "sample_add",
                            "args": {"a": i + 2, "b": 3},
                        }
                    ],
                ),
                AIMessage(content="sample-child-answer"),
            ],
        )
        models.append(model)
        specs.append(
            {
                "name": f"worker-{i}",
                "description": "Sample",
                "system_prompt": "Add the sample.",
                "model": model,
                "tools": [sample_add],
                **({"interrupt_on": {"sample_add": True}} if interrupt else {}),
            }
        )
        calls.append(
            {
                "id": f"dispatch-{i}",
                "name": "task",
                "args": {
                    "description": "sample-task-instructions",
                    "subagent_type": f"worker-{i}",
                },
            }
        )
    parent = _Model(responses=[AIMessage(content="", tool_calls=calls), AIMessage(content="done")])
    models.append(parent)
    graph = create_deep_agent(
        model=parent,
        subagents=specs,
        name="parent",
        checkpointer=InMemorySaver() if interrupt else None,
    )
    return graph, models, started, cancelled


@pytest.mark.asyncio
async def test_native_parallel_tools_are_scoped_and_models_execute_once() -> None:
    graph, models, _, _ = _graph(count=2)
    observer = SubagentEventObserver()
    config = {"callbacks": [observer]}
    events = [
        event
        async for event in merge_subagent_events(
            LangGraphProtocol(graph=graph).stream([HumanMessage(content="sample")], config)
        )
        if event["type"] == "subagent_event"
    ]
    starts = [e for e in events if e["phase"] == "started"]
    assert len(starts) == 2
    assert {e["parent_call_id"] for e in starts} == {"dispatch-0", "dispatch-1"}
    assert len({e["run_id"] for e in starts}) == 2
    tools = [e for e in events if e["phase"] == "tool_started"]
    assert len(tools) == 2
    assert {e["tool_call_id"] for e in tools} == {"same-child-call"}
    assert {e["run_id"] for e in tools} == {e["run_id"] for e in starts}
    assert len([e for e in events if e["phase"] == "completed"]) == 2
    assert len([e for e in events if e["phase"] == "model_started"]) == 4
    assert [model.calls for model in models] == [2, 2, 2]
    assert all(e["started_at"] <= e["ts"] for e in events)
    assert all(e["duration_ms"] >= 0 for e in events if "duration_ms" in e)
    returned = [e for e in events if e["phase"] == "tool_completed"]
    assert sorted(int(e["result"]) for e in returned) == [5, 6]
    assert sorted(json.loads(e["args"])["a"] for e in returned) == [2, 3]
    assert all("sample-child-answer" in e["result"] for e in events if e["phase"] == "completed")


@pytest.mark.asyncio
async def test_failure_is_published_before_original_exception() -> None:
    graph, models, _, _ = _graph(fail=True)
    events = []
    with pytest.raises(RuntimeError, match="sample-exception-content"):
        async for event in merge_subagent_events(
            LangGraphProtocol(graph=graph).stream(
                [HumanMessage(content="sample")], {"callbacks": [SubagentEventObserver()]}
            )
        ):
            if event["type"] == "subagent_event":
                events.append(event)
    assert any(e["phase"] == "failed" for e in events)
    assert models[-1].calls == 1


@pytest.mark.asyncio
async def test_cancellation_seals_open_child_tools_and_stops_original_stream() -> None:
    graph, models, started, cancelled = _graph(slow=True)
    events = []

    async def consume() -> None:
        async for event in merge_subagent_events(
            LangGraphProtocol(graph=graph).stream(
                [HumanMessage(content="sample")], {"callbacks": [SubagentEventObserver()]}
            )
        ):
            events.append(event)

    driver = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 3)
    driver.cancel()
    with pytest.raises(asyncio.CancelledError):
        await driver
    await asyncio.wait_for(cancelled.wait(), 3)
    assert models[-1].calls == 1
    assert not any(
        t.get_name() == "subagent-event-observer" and not t.done() for t in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_full_queue_consumer_close_does_not_deadlock_or_leak_driver() -> None:
    closed = asyncio.Event()

    async def source() -> AsyncIterator[dict[str, Any]]:
        try:
            for _ in range(1000):
                yield {"type": "token", "content": "x"}
        finally:
            closed.set()

    stream = merge_subagent_events(source())
    await anext(stream)
    await asyncio.wait_for(stream.aclose(), 1)
    assert closed.is_set()
    assert not any(
        t.get_name() == "subagent-event-observer" and not t.done() for t in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_observer_survives_real_child_hitl_pause_and_resume() -> None:
    graph, models, _, _ = _graph(interrupt=True)
    observer = SubagentEventObserver()
    config = {"configurable": {"thread_id": "resume-test"}, "callbacks": [observer]}
    protocol = LangGraphProtocol(graph=graph)
    first = [
        e
        async for e in merge_subagent_events(
            protocol.stream([HumanMessage(content="sample")], config)
        )
    ]
    assert any(e.get("phase") == "interrupted" for e in first)
    state = await graph.aget_state(config)
    assert state.next
    resumed = [
        e
        async for e in merge_subagent_events(
            protocol.resume_stream("resume-test", [{"type": "approve"}], config)
        )
    ]
    assert any(e.get("phase") == "started" for e in resumed)
    assert any(e.get("phase") == "completed" for e in resumed)
    assert not (await graph.aget_state(config)).next


@pytest.mark.asyncio
async def test_root_cancel_callback_marks_pending_tool_without_base_tool_error() -> None:
    observer = SubagentEventObserver()
    root, dispatch, nested = uuid4(), uuid4(), uuid4()

    async def source() -> AsyncIterator[dict[str, Any]]:
        await observer.on_chain_start(None, {}, run_id=root)
        await observer.on_tool_start({"name": "task"}, "", run_id=dispatch, parent_run_id=root)
        await observer.on_tool_start({"name": "execute"}, "", run_id=nested, parent_run_id=dispatch)
        await observer.on_chain_error(asyncio.CancelledError(), run_id=root)
        yield {"type": "done"}

    events = [e async for e in merge_subagent_events(source()) if e["type"] == "subagent_event"]
    assert [e["phase"] for e in events] == [
        "started",
        "tool_started",
        "tool_cancelled",
        "cancelled",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_manager_wires_observer_without_replacing_callbacks(
    enabled: bool,
) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from octop.infra.agents.manager import AgentManager

    existing = object()
    requests: list[dict[str, Any]] = []

    @asynccontextmanager
    async def scope(*args: Any) -> AsyncIterator[None]:
        yield

    async def harness_stream(agent_id: str, req: dict[str, Any]) -> AsyncIterator[Any]:
        requests.append(req)
        callbacks = req["callbacks"]
        assert callbacks[0] is existing
        if enabled:
            observer = callbacks[1]
            root, child = uuid4(), uuid4()
            await observer.on_chain_start({}, {}, run_id=root)
            await observer.on_tool_start(
                {"name": "task"},
                "",
                inputs={"subagent_type": "worker"},
                run_id=child,
                parent_run_id=root,
            )
            await observer.on_tool_end("sample-result", run_id=child)
            await observer.on_chain_end({}, run_id=root)
        yield {"type": "token", "content": "parent"}

    manager = SimpleNamespace(
        _harness_manager=SimpleNamespace(stream=harness_stream),
        _thread_execution_lock=scope,
        _track_invocation=scope,
        _apply_pending_bootstrap_graph_refresh=lambda *args: None,
        _prepare_stream_request=lambda aid, req: dict(req),
        get_config=lambda aid: {"enable_trajectory": enabled},
        _stream_model_label=lambda *args: "fake",
    )
    result = [
        chunk
        async for chunk in AgentManager.stream(
            manager,
            "A",
            {"thread_id": "T", "callbacks": [existing]},
        )
    ]
    assert len(requests) == 1
    assert len(requests[0]["callbacks"]) == (2 if enabled else 1)
    assert [chunk for chunk in result if chunk["type"] == "token"] == [
        {"type": "token", "content": "parent"},
    ]
    assert len([chunk for chunk in result if chunk["type"] == "subagent_event"]) == (
        2 if enabled else 0
    )


@pytest.mark.asyncio
async def test_tool_errors_and_command_error_reply_keep_the_actual_submission_and_return() -> None:
    from langchain_core.messages import ToolMessage
    from langgraph.types import Command

    observer = SubagentEventObserver()
    root, dispatch, invalid, error_reply = uuid4(), uuid4(), uuid4(), uuid4()

    async def source() -> AsyncIterator[dict[str, Any]]:
        await observer.on_chain_start({}, {}, run_id=root)
        await observer.on_tool_start({"name": "task"}, "", run_id=dispatch, parent_run_id=root)
        await observer.on_tool_start(
            {"name": "execute"}, "", inputs={}, run_id=invalid, parent_run_id=dispatch
        )
        await observer.on_tool_error(ValueError("command: Field required"), run_id=invalid)
        await observer.on_tool_start(
            {"name": "execute"},
            "",
            inputs={"command": "python3 --version"},
            run_id=error_reply,
            parent_run_id=dispatch,
            tool_call_id="call",
        )
        await observer.on_tool_end(
            Command(
                update={
                    "messages": [
                        ToolMessage(content="actual failure", status="error", tool_call_id="call"),
                        ToolMessage(content="wrong result", tool_call_id="other"),
                    ]
                }
            ),
            run_id=error_reply,
        )
        await observer.on_tool_end("returned", run_id=dispatch)
        await observer.on_chain_end({}, run_id=root)
        yield {"type": "done"}

    events = [event async for event in merge_subagent_events(source())]
    failed = [e for e in events if e.get("phase") == "tool_failed"]
    assert len(failed) == 2
    assert failed[0]["args"] == "{}"
    assert failed[0]["result"] == "command: Field required"
    assert failed[1]["result"] == "actual failure"
    assert "wrong result" not in json.dumps(events)
