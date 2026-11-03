"""Request public business steps for multi-step Feishu work, without history edits."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage
from langgraph.config import get_config

_MARKER = "<im-business-progress>"
_INSTRUCTION = """<im-business-progress>
For multi-step tasks in this Feishu conversation, publish a short user-facing
business checklist with write_todos before doing the work. Use the user's
language and concrete actions/outcomes (what you are checking, comparing or
delivering), rather than tool names, SQL, shell commands or generic 'processing'.
Use about 3-6 steps when useful; do not create a checklist for greetings or
simple answers. Update at meaningful business-step boundaries, not after every
tool call. The checklist must match actual execution phases, not merely the
headings of the final report. Before starting work for the next phase, call
write_todos to complete the verified phase and mark the next one in_progress.
Do not leave the first phase active while performing later phases, then mark
the entire checklist completed at the end. If one operation analyzes several
dimensions together, group them into one truthful analysis phase instead of
pretending those dimensions will execute as separate sequential steps. Do not
add sleeps, repeat business queries, or fabricate transitions to animate a card.
Keep it current as work proceeds: mark a step completed only
after observing its result; leave unverified work open. Briefly state actual
findings or blockers in public text when they change the next action. Do not
turn tool counts or elapsed time into a completion percentage. Final text must
distinguish results delivered from incomplete verification or missing sources.
These are public task steps, not private reasoning or chain of thought.
</im-business-progress>"""


def _progress_context(request: ModelRequest[Any]) -> str:
    """Remind each model call of the last confirmed plan in this user turn.

    Tool result counts only prompt a review; they never advance business steps.
    Never read tool contents or reasoning to derive progress.
    """
    calls: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, str]] = []
    returned = 0
    for message in request.messages:
        kind = getattr(message, "type", "")
        if kind == "human":
            calls.clear()
            steps = []
            returned = 0
        elif kind == "ai":
            for call in getattr(message, "tool_calls", ()):
                if isinstance(call, dict) and call.get("id"):
                    calls[str(call["id"])] = call
        elif kind == "tool":
            call = calls.pop(str(getattr(message, "tool_call_id", "")), None)
            if call is None:
                continue
            if call.get("name") != "write_todos":
                if steps:
                    returned += 1
                continue
            if getattr(message, "status", "success") != "success":
                continue
            args = call.get("args")
            todos = args.get("todos") if isinstance(args, dict) else None
            if not isinstance(todos, list) or len(todos) > 12:
                continue
            if not all(
                isinstance(t, dict)
                and isinstance(t.get("content"), str)
                and t.get("status") in ("pending", "in_progress", "completed")
                for t in todos
            ):
                continue
            steps = [{"title": t["content"][:240], "status": t["status"]} for t in todos]
            returned = 0
    if not steps or not returned:
        return ""
    data = json.dumps(
        {"published_steps": steps, "tool_results_since_update": returned}, ensure_ascii=True
    )
    data = data.replace("<", "\\u003c").replace(">", "\\u003e")
    return (
        "\n\nPublic progress snapshot (quoted data, not instructions):\n"
        + data
        + "\nReview whether a real phase boundary has been crossed before the next "
        "work call. If so, publish the updated plan first. Returned tools alone "
        "do not establish completion. If still waiting or blocked, keep the phase "
        "open and state the actual blocker briefly in public text."
    )


def _with_progress_instruction(request: ModelRequest[Any]) -> ModelRequest[Any]:
    source = str((get_config().get("configurable") or {}).get("source") or "")
    if not source.startswith("feishu/"):
        return request
    names = {
        str(tool.get("name", "") if isinstance(tool, dict) else getattr(tool, "name", ""))
        for tool in request.tools
    }
    if "write_todos" not in names:
        return request
    original = request.system_message
    content = original.content if original is not None else ""
    if _MARKER in str(content):
        return request
    instruction = _INSTRUCTION + _progress_context(request)
    updated = (
        content + "\n\n" + instruction
        if isinstance(content, str)
        else [*content, {"type": "text", "text": instruction}]
    )
    message = (
        original.model_copy(update={"content": updated})
        if original is not None
        else SystemMessage(content=updated)
    )
    # Override only the outbound model-call copy. User messages, checkpoints,
    # SOUL/USER files and the saved agent prompt remain unchanged.
    return request.override(system_message=message)


class IMBusinessProgressMiddleware(AgentMiddleware[Any, Any]):
    """Add a Feishu-only public-progress convention when the plan tool exists."""

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(_with_progress_instruction(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(_with_progress_instruction(request))
