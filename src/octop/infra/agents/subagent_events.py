"""Observe native task runs without changing prompts or executing a second graph.

The sink belongs to the current stream consumer, not the saved callback instance:
HITL may retain callbacks and resume them under a new stream later.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langgraph.errors import GraphInterrupt

_Sink = Callable[[dict[str, Any]], Awaitable[None]]
_sink: ContextVar[_Sink | None] = ContextVar("subagent_event_sink", default=None)


@dataclass
class _Run:
    name: str
    call_id: str | None
    parent: str | None
    started: float
    started_at: float = field(default_factory=time.time)
    terminal: bool = False


def _error_phase(error: BaseException) -> str:
    if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
        return "cancelled"
    if isinstance(error, GraphInterrupt):
        return "interrupted"
    return "failed"


class SubagentEventObserver(AsyncCallbackHandler):
    """Record identities, names and timings only; never capture inputs or outputs."""

    def __init__(self) -> None:
        self._parents: dict[str, str | None] = {}
        self._agents: dict[str, _Run] = {}
        self._tools: dict[str, _Run] = {}
        self._models: dict[str, _Run] = {}
        self._execution: str | None = None

    def _owner(self, run_id: str | None) -> str | None:
        while run_id is not None:
            if run_id in self._agents:
                return run_id
            run_id = self._parents.get(run_id)
        return None

    async def _emit(self, owner: str, phase: str, **fields: Any) -> None:
        sink = _sink.get()
        if sink is None:
            return
        run = self._agents[owner]
        await sink(
            {
                "type": "subagent_event",
                "ts": time.time(),
                "execution_id": self._execution,
                "run_id": owner,
                "parent_run_id": run.parent,
                "parent_call_id": run.call_id,
                "name": run.name,
                "started_at": run.started_at,
                "phase": phase,
                **fields,
            }
        )

    async def _finish(self, run_id: str, phase: str) -> None:
        run = self._agents[run_id]
        if run.terminal:
            return
        run.terminal = True
        await self._emit(run_id, phase, duration_ms=(time.monotonic() - run.started) * 1000)

    async def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._parents[str(run_id)] = str(parent_run_id) if parent_run_id else None
        if parent_run_id is None:
            self._execution = str(run_id)

    async def on_chain_end(
        self, outputs: Any, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any
    ) -> None:
        if parent_run_id is not None:
            return
        # A checkpoint interruption is a pause, never proof that a task returned.
        phase = (
            "interrupted"
            if isinstance(outputs, dict) and outputs.get("__interrupt__")
            else "unknown"
        )
        await self._finish_all(phase)

    async def _finish_all(self, phase: str) -> None:
        # CancelledError may skip BaseTool.on_tool_error. Seal open operations
        # before their owner so the UI never leaves an abandoned tool running.
        for identity in list(self._tools):
            await self._tool_terminal(identity, phase)
        for identity in list(self._models):
            await self._model_terminal(identity, phase)
        for owner in self._agents:
            await self._finish(owner, phase)
        self._parents.clear()
        self._agents.clear()
        self._tools.clear()
        self._models.clear()

    async def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        if parent_run_id is None:
            await self._finish_all(_error_phase(error))

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        owner = self._owner(str(parent_run_id) if parent_run_id else None)
        if owner is not None:
            identity = str(run_id)
            self._models[identity] = _Run("model", None, owner, time.monotonic())
            await self._emit(owner, "model_started", model_run_id=identity)

    async def _model_terminal(self, identity: str, phase: str) -> None:
        model = self._models.pop(identity, None)
        if model is not None and model.parent is not None:
            await self._emit(
                model.parent,
                "model_" + phase,
                model_run_id=identity,
                duration_ms=(time.monotonic() - model.started) * 1000,
            )

    async def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        await self._model_terminal(str(run_id), "completed")

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        await self._model_terminal(str(run_id), _error_phase(error))

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        identity = str(run_id)
        parent = str(parent_run_id) if parent_run_id else None
        self._parents[identity] = parent
        name = str(serialized.get("name") or "tool")[:120]
        call_id = kwargs.get("tool_call_id")
        call_id = call_id if isinstance(call_id, str) else None
        owner = self._owner(parent)
        if name == "task":
            agent_name = (inputs or {}).get("subagent_type")
            self._agents[identity] = _Run(
                name=agent_name[:120] if isinstance(agent_name, str) else "task",
                call_id=call_id,
                parent=owner,
                started=time.monotonic(),
            )
            await self._emit(identity, "started")
        elif owner is not None:
            self._tools[identity] = _Run(name, call_id, owner, time.monotonic())
            await self._emit(
                owner, "tool_started", tool_run_id=identity, tool_call_id=call_id, tool_name=name
            )

    async def _tool_terminal(self, identity: str, phase: str) -> None:
        if identity in self._agents:
            await self._finish(identity, phase)
        elif identity in self._tools:
            tool = self._tools.pop(identity)
            assert tool.parent is not None
            await self._emit(
                tool.parent,
                "tool_" + phase,
                tool_run_id=identity,
                tool_call_id=tool.call_id,
                tool_name=tool.name,
                duration_ms=(time.monotonic() - tool.started) * 1000,
            )

    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        status = (
            output.get("status") if isinstance(output, dict) else getattr(output, "status", None)
        )
        await self._tool_terminal(str(run_id), "failed" if status == "error" else "completed")

    async def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        await self._tool_terminal(str(run_id), _error_phase(error))


async def merge_subagent_events(
    stream: AsyncGenerator[dict[str, Any], None],
) -> AsyncGenerator[dict[str, Any], None]:
    """Consume the original stream exactly once and merge live callback metadata.

    Task completion is the end signal. An awaited END put in a producer's finally
    would deadlock if the consumer closes while the bounded queue is full.
    """
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
    closed = False

    async def publish(event: dict[str, Any]) -> None:
        if not closed:
            await queue.put(event)

    async def drive() -> None:
        token = _sink.set(publish)
        try:
            async with aclosing(stream) as chunks:
                async for chunk in chunks:
                    await queue.put(chunk)
        finally:
            _sink.reset(token)

    driver = asyncio.create_task(drive(), name="subagent-event-observer")
    pending: asyncio.Task[dict[str, Any]] | None = None
    try:
        while True:
            if driver.done() and queue.empty():
                await driver  # Propagate cancellation/errors after queued terminal events.
                return
            pending = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait((pending, driver), return_when=asyncio.FIRST_COMPLETED)
            if pending in done:
                chunk = pending.result()
                pending = None
                yield chunk
            else:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                pending = None
    finally:
        closed = True
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if not driver.done():
            driver.cancel()
        # Release any callback already waiting for capacity during shutdown.
        while not queue.empty():
            queue.get_nowait()
        await asyncio.gather(driver, return_exceptions=True)
