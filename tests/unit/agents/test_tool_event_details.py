from langchain_core.messages import ToolMessage
from langgraph.types import Command

from octop.infra.agents.tool_event_details import (
    DETAIL_LIMIT,
    sensitive_tool_source,
    tool_detail,
    tool_reply,
)


def test_keeps_tool_values_and_redacts_nested_secrets() -> None:
    detail = tool_detail(
        {
            "command": "python3 --version",
            "nested": {"api_key": "dummy_key", "password": "dummy_pwd"},
            "env": {"PUBLIC": "dummy_env"},
        }
    )
    assert "python3 --version" in detail["text"]
    assert all(secret not in detail["text"] for secret in ("dummy_key", "dummy_pwd", "dummy_env"))
    assert detail["redacted"] is True


def test_redacts_json_text_headers_urls_and_private_keys() -> None:
    for value in [
        '{"api_key":"dummy_key"}',
        "password='dummy_pwd'",
        "Authorization: Bearer dummy_bearer",
        "Cookie: dummy_cookie",
        "Cookie: a=dummy_cookie; b=dummy_cookie_b",
        "https://host/?key=dummy_key",
        "dummy_think</think>ok",
        "https://u:dummy_url@host/path?access_token=dummy_query",
        "-----BEGIN PRIVATE KEY-----\ndummy_pem\n-----END PRIVATE KEY-----",
        "<think>dummy_think</think>ok",
    ]:
        detail = tool_detail(value)
        assert "dummy_" not in detail["text"]
        assert detail["redacted"] is True


def test_only_extracts_text_from_tool_messages_and_command_reply() -> None:
    msg = ToolMessage(
        content=[
            {"type": "text", "text": "actual reply"},
            {"type": "thinking", "thinking": "dummy_think"},
            {"type": "image", "data": "dummy_image"},
        ],
        tool_call_id="t",
        artifact={"credential": "dummy_artifact"},
    )
    result = tool_detail(
        tool_reply(Command(update={"messages": [msg], "private_state": "dummy_state"}), "t")
    )
    assert "actual reply" in result["text"]
    assert "dummy_" not in result["text"]


def test_falsey_values_are_recorded_without_inventing_empty_objects() -> None:
    for value, expected in [(None, "null"), (False, "false"), (0, "0"), ("", ""), ({}, "{}")]:
        assert tool_detail(value)["text"] == expected


def test_bounds_large_circular_and_unsupported_values() -> None:
    class Unsafe:
        def __str__(self) -> str:
            raise AssertionError("must not stringify objects")

    cyclic: dict = {}
    cyclic["self"] = cyclic
    assert tool_detail(cyclic)["truncated"] is True
    assert "unsupported" in tool_detail(Unsafe())["text"]
    assert tool_detail(list(range(100000)))["truncated"] is True
    detail = tool_detail("x" * (DETAIL_LIMIT + 100))
    assert len(detail["text"]) == DETAIL_LIMIT
    assert detail["truncated"] is True


def test_environment_and_sensitive_file_reads_hide_output() -> None:
    for value in (
        {"command": "printenv"},
        {"file_path": "/app/.env"},
        {"command": "cat /app/config.yaml"},
    ):
        assert sensitive_tool_source(value)
        assert "dummy_secret" not in tool_detail("dummy_secret", hidden=True)["text"]
    cyclic: dict = {}
    cyclic["self"] = cyclic
    assert not sensitive_tool_source(cyclic)


def test_adversarial_sources_and_multivalue_payloads_are_bounded() -> None:
    import time

    root: dict = {}
    for i in range(30):
        root[str(i)] = root
    started = time.monotonic()
    sensitive_tool_source(root)
    detail = tool_detail(["x" * 256000] * 2000)
    assert len(detail["text"]) <= DETAIL_LIMIT
    assert time.monotonic() - started < 1
    assert sensitive_tool_source({"paths": ["/app/.env"]})
    assert "dummy_binary" not in tool_detail('{"type":"image","data":"dummy_binary"}')["text"]


def test_command_selects_only_matching_tool_reply_and_preserves_status() -> None:
    wrong = ToolMessage(content="wrong", tool_call_id="other")
    right = ToolMessage(content="actual error", tool_call_id="t", status="error")
    command = Command(update={"messages": [right, wrong]})
    assert tool_reply(command, "t") is right
    assert tool_reply(command, "missing") == "[tool reply not available]"


def test_cumulative_scan_budget_and_unsupported_output_are_explicit() -> None:
    import time

    started = time.monotonic()
    assert sensitive_tool_source(["x" * 256000] * 2000)
    assert tool_detail(["<think>" + "x" * 255000 + "</think>"] * 2000)["truncated"]
    assert time.monotonic() - started < 1
    assert tool_detail(10**5000)["truncated"]
    for block in [
        {"type": "input_image", "image": "dummy_binary"},
        {"type": "image", "data": "dummy_binary"},
    ]:
        assert "dummy_binary" not in tool_detail(block)["text"]
    msg = ToolMessage(content=[{"type": "image", "data": "dummy_binary"}], tool_call_id="t")
    assert tool_detail(msg)["redacted"]
    assert tool_detail(msg)["text"] != "[]"
    assert "dummy_credential" not in tool_detail("credentials=dummy_credential")["text"]


def test_long_keys_and_nested_json_share_the_scan_budget(monkeypatch) -> None:
    import json

    import octop.infra.agents.tool_event_details as module

    result = tool_detail({"x" * 1100 + "password": "dummy_sensitive_value"})
    assert "dummy_sensitive_value" not in result["text"]
    original = json.loads
    parsed = []

    def loads(raw):
        parsed.append(len(raw))
        return original(raw)

    monkeypatch.setattr(module.json, "loads", loads)
    result = tool_detail(['{"password":"' + "x" * 240000 + '"}'] * 500)
    assert sum(parsed) <= 256000
    assert result["truncated"] is True


def test_repeated_reasoning_markers_are_processed_linearly() -> None:
    import time

    started = time.monotonic()
    result = tool_detail("<think>" * 36000)
    assert result["redacted"] is True
    assert result["text"] == ""
    assert time.monotonic() - started < 1
    assert tool_detail("hidden</think>actual")["text"] == "actual"
    assert tool_detail("before<think>hidden</think>after")["text"] == "beforeafter"
