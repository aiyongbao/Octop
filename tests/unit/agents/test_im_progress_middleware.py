"""The progress convention is per-call, Feishu-only and public-task oriented."""

from langchain.agents.middleware import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from octop.infra.agents.middleware import im_progress


def request(content="agent rules", tools=True):
    return ModelRequest(
        model=FakeMessagesListChatModel(responses=[]),
        messages=[HumanMessage("把三个平台独立分析")],
        system_message=SystemMessage(content=content),
        tools=[{"name": "write_todos"}] if tools else [],
    )


def test_feishu_instruction_does_not_mutate_prompt_or_user_history(monkeypatch):
    monkeypatch.setattr(
        im_progress, "get_config", lambda: {"configurable": {"source": "feishu/test"}}
    )
    original = request()
    changed = im_progress._with_progress_instruction(original)
    assert changed.system_message is not original.system_message
    assert original.system_message.content == "agent rules"
    assert changed.messages == original.messages
    assert "write_todos" in changed.system_message.content
    assert "not private reasoning" in changed.system_message.content
    assert im_progress._with_progress_instruction(changed) is changed


def test_other_channels_and_missing_plan_tool_are_untouched(monkeypatch):
    original = request()
    monkeypatch.setattr(
        im_progress, "get_config", lambda: {"configurable": {"source": "dashboard/test"}}
    )
    assert im_progress._with_progress_instruction(original) is original
    monkeypatch.setattr(
        im_progress, "get_config", lambda: {"configurable": {"source": "feishu/test"}}
    )
    without_tool = request(tools=False)
    assert im_progress._with_progress_instruction(without_tool) is without_tool


def test_block_system_prompt_is_copied_not_appended_in_place(monkeypatch):
    monkeypatch.setattr(
        im_progress, "get_config", lambda: {"configurable": {"source": "feishu/test"}}
    )
    original = request(
        content=[{"type": "text", "text": "original", "cache_control": {"type": "ephemeral"}}]
    )
    changed = im_progress._with_progress_instruction(original)
    assert len(original.system_message.content) == 1
    assert len(changed.system_message.content) == 2
    assert changed.system_message.content[0]["cache_control"] == {"type": "ephemeral"}


def test_progress_reminder_uses_confirmed_plan_and_counts_real_returns_without_advancing():
    original = request()
    original.messages.extend(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "p1",
                        "name": "write_todos",
                        "args": {
                            "todos": [
                                {"content": "核对数据", "status": "in_progress"},
                                {"content": "对比分析", "status": "pending"},
                            ]
                        },
                    }
                ],
            ),
            ToolMessage(content="saved", tool_call_id="p1"),
            AIMessage(content="", tool_calls=[{"id": "exec", "name": "execute", "args": {}}]),
            ToolMessage(content="private output must not be copied", tool_call_id="exec"),
        ]
    )
    text = im_progress._progress_context(original)
    assert '"tool_results_since_update": 1' in text
    assert '"status": "in_progress"' in text and '"status": "pending"' in text
    assert "private output must not be copied" not in text
    assert "completed" not in text


def test_failed_plan_write_does_not_replace_current_plan_and_new_user_turn_resets_reminder():
    original = request()
    original.messages.extend(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "p",
                        "name": "write_todos",
                        "args": {"todos": [{"content": "核对数据", "status": "in_progress"}]},
                    }
                ],
            ),
            ToolMessage(content="saved", tool_call_id="p"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "bad",
                        "name": "write_todos",
                        "args": {"todos": [{"content": "wrong completion", "status": "completed"}]},
                    }
                ],
            ),
            ToolMessage(content="write failed", tool_call_id="bad", status="error"),
            AIMessage(content="", tool_calls=[{"id": "exec", "name": "execute", "args": {}}]),
            ToolMessage(content="returned", tool_call_id="exec"),
        ]
    )
    text = im_progress._progress_context(original)
    assert '"status": "in_progress"' in text and "wrong completion" not in text
    original.messages.append(HumanMessage("开始新的任务"))
    assert im_progress._progress_context(original) == ""
