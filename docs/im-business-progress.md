# Feishu business-step progress

Feishu tasks can reuse the runtime's `write_todos` tool to expose public business
steps. No business skill, platform-name mapping, extra model or sidecar is needed.

`IMBusinessProgressMiddleware` adds a per-model-call convention only when the
request source starts with `feishu/` and the plan tool is available. It asks for
short, user-language business steps for multi-step work, meaningful-boundary
updates, and truthful incomplete states. It does not request private reasoning,
rewrite user messages, persist extra system turns, or modify SOUL/USER files.
It runs before the output-budget middleware so its text participates in request
budgeting. Greetings/simple answers need no plan.

Each model call also receives the last successfully published plan from the
current user turn and the number of tool returns since that update. Tool output
and reasoning are not copied. Failed plan writes do not replace this snapshot;
a new user message resets it. The reminder asks the model to update the plan
before crossing a real execution-phase boundary. Dimensions analyzed together
in one operation belong in one phase, rather than an artificial sequential
checklist. It does not add model calls, repeated queries, sleeps or automatic
completion. This is a model reporting convention, not a deterministic workflow
engine; actual task logs must still verify intermediate updates.

Live validation has shown intermediate plan writes followed by a batch completion
of later phases. The reminder therefore does not yet guarantee phase-by-phase
reporting. Successful plan-tool results also do not establish timely Feishu
display: projection, card update receipts and client behavior require separate
verification. Do not describe this change as resolving that end-to-end issue.

The stream projection correlates tool fragments and results by call ID. Indexes
are reused across model rounds and are insufficient to identify a call. Live
successful `write_todos` results emit an optional `TYPING` event:

```json
{
  "type": "typing",
  "metadata": {
    "task_progress": {
      "version": 1,
      "source": "write_todos",
      "steps": [
        {"title": "Verify the advertising source", "status": "in_progress"},
        {"title": "Deliver the comparison and source gaps", "status": "pending"}
      ]
    }
  }
}
```

Statuses are `pending`, `in_progress`, and `completed`. Failed plan writes,
malformed/partial arguments, and replayed historical results do not publish a
plan. Raw execution arguments and tool results are not forwarded by this change.
Existing adapters may ignore the optional typing metadata. A gateway with the
business-step card renderer consumes it independently of tool-hint visibility.

A completed model response does not complete pending business steps. The card
keeps unfinished work visible. Tool counts, elapsed time, and tool success are
never converted into a business completion percentage. A model-authored checklist
is a progress report, not independent proof that financial or delivery acceptance
has passed; actual acceptance remains the responsibility of the task workflow.

Deploy both the Octop projection/middleware and the gateway renderer. Installing
only the renderer cannot create progress which the producer never published.
Existing running tasks do not acquire the new middleware until the runtime is
restarted; no running task should be interrupted just to preview the display.
