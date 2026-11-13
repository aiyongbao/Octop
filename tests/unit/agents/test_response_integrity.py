from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from octop.infra.agents.middleware.response_integrity import (
    ResponseIntegrityMiddleware,
    response_problem,
)


def _response(content="", **kwargs):
    return ModelResponse(result=[AIMessage(content=content, **kwargs)])


@pytest.mark.parametrize(
    "response,expected",
    [
        (_response([{"type": "thinking", "thinking": "private"}]), "empty_final_response"),
        (_response("  "), "empty_final_response"),
        (ModelResponse(result=[]), "empty_final_response"),
        (_response("partial", response_metadata={"stop_reason": "max_tokens"}), "output_limit"),
        (
            _response(
                "",
                invalid_tool_calls=[
                    {"name": "execute", "args": "{", "id": "bad", "error": "truncated"}
                ],
            ),
            "invalid_tool_arguments",
        ),
        (_response("blocked: need user input"), ""),
        (_response([{"type": "text", "text": "done"}]), ""),
        (_response([{"type": "image_url", "image_url": {"url": "example"}}]), ""),
        (
            _response(
                "", tool_calls=[{"name": "execute", "args": {"command": "ok"}, "id": "live"}]
            ),
            "",
        ),
    ],
)
def test_response_validation(response, expected):
    assert response_problem(response) == expected


async def test_empty_inference_is_retried_once_without_changing_request():
    request = ModelRequest(model=FakeMessagesListChatModel(responses=[]), messages=[])
    good = _response("done")
    handler = AsyncMock(side_effect=[_response([{"type": "thinking", "thinking": ""}]), good])
    result = await ResponseIntegrityMiddleware().awrap_model_call(request, handler)
    assert result is good
    assert handler.await_count == 2
    assert all(call.args == (request,) for call in handler.await_args_list)


async def test_repeated_empty_response_is_error_instead_of_success():
    request = ModelRequest(model=FakeMessagesListChatModel(responses=[]), messages=[])
    handler = AsyncMock(return_value=_response(""))
    with pytest.raises(ValueError, match="empty_final_response"):
        await ResponseIntegrityMiddleware().awrap_model_call(request, handler)
    assert handler.await_count == 2


def test_sync_truncation_is_not_accepted_or_retried_as_empty():
    request = ModelRequest(model=FakeMessagesListChatModel(responses=[]), messages=[])
    handler = Mock(return_value=_response("partial", response_metadata={"finish_reason": "length"}))
    with pytest.raises(ValueError, match="output_limit"):
        ResponseIntegrityMiddleware().wrap_model_call(request, handler)
    assert handler.call_count == 1


async def test_real_graph_empty_then_tool_then_answer_executes_tool_once():
    from langchain.agents import create_agent

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    executions = []

    def check() -> str:
        """Record a single diagnostic step."""
        executions.append("check")
        return "checked"

    model = Model(
        responses=[
            AIMessage(content=[{"type": "thinking", "thinking": ""}]),
            AIMessage(content="", tool_calls=[{"name": "check", "args": {}, "id": "check-1"}]),
            AIMessage(content="done"),
        ]
    )
    graph = create_agent(model, tools=[check], middleware=[ResponseIntegrityMiddleware()])
    result = await graph.ainvoke({"messages": [{"role": "user", "content": "check"}]})
    assert executions == ["check"]
    assert result["messages"][-1].content == "done"


async def test_empty_response_failure_keeps_unfinished_todos_in_checkpoint():
    from langchain.agents import create_agent
    from langchain.agents.middleware import TodoListMiddleware
    from langgraph.checkpoint.memory import InMemorySaver

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    todos = [{"content": "write and deliver report", "status": "in_progress"}]
    model = Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "write_todos", "args": {"todos": todos}, "id": "plan"}],
            ),
            AIMessage(content=[{"type": "thinking", "thinking": ""}]),
            AIMessage(content=""),
        ]
    )
    graph = create_agent(
        model,
        middleware=[TodoListMiddleware(), ResponseIntegrityMiddleware()],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "incomplete-report"}}
    with pytest.raises(ValueError, match="empty_final_response"):
        await graph.ainvoke({"messages": [{"role": "user", "content": "write report"}]}, config)
    checkpoint = await graph.aget_state(config)
    assert checkpoint.values["todos"] == todos
    assert checkpoint.values["messages"][-1].type == "tool"
