"""Bounded, rule-redacted tool details shared by live and persisted events."""

from __future__ import annotations

import json
import math
import re
from itertools import islice
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import Command

DETAIL_LIMIT = 64_000
_REDACTED = "[redacted]"
_PRIVATE = re.compile(
    r"(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)"
)
_HEADERS = re.compile(
    r"(?im)((?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+"
)
_QUERY = re.compile(
    r"(?i)([?&](?:key|sig|signature|token|access_token|api_key|password|secret)=)[^&#\s]+"
)
_ASSIGN = re.compile(
    r"""(?ix)(\b[\w-]{0,80}(?:token|password|passwd|secret|api[_-]?key|private[_-]?key|authorization|cookie|credential)[\w-]{0,80}["']?\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;\]}&]+)"""
)
_BEARER = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/-]+=*")
_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]{0,20}://)[^\s/@]+:[^\s/@]+@")
_KNOWN_TOKEN = re.compile(
    r"\b(?:sk-[\w-]+|gh[pousr]_[\w]+|github_pat_[\w]+|eyJ[\w-]+\.[\w-]+\.[\w-]+)"
)
_SENSITIVE = re.compile(
    r"(?i)(password|passwd|secret|token|apikey|privatekey|authorization|cookie|credential|signature|reasoning|thinking)"
)
_HIDDEN_KEYS = {
    "env",
    "environment",
    "config",
    "configuration",
    "artifact",
    "additionalkwargs",
    "responsemetadata",
}
_SENSITIVE_SOURCE = re.compile(
    r"(?i)(?:\bprintenv\b|\bos\.environ\b|\bprocess\.env\b|(?:^|[;&|]\s*)env\s*(?:$|[;&|])|(?:^|[/\\\s'\"])(?:\.env(?:\.[\w-]+)?|credentials(?:\.\w+)?|secrets?(?:\.\w+)?|id_rsa|id_ed25519|config\.(?:json|ya?ml|toml)|settings\.json)(?:$|[\s'\"]))"
)


_THINK_MARKER = re.compile(r"(?i)<(/?)(?:think|thinking)\b")


def _without_reasoning(raw: str) -> str:
    # Linear marker scanning: repeated unclosed tags must not cause regex
    # backtracking to stall the agent's event loop.
    parts: list[str] = []
    depth = 0
    cursor = 0
    for marker in _THINK_MARKER.finditer(raw):
        if marker.start() < cursor:
            continue
        if depth == 0:
            parts.append(raw[cursor : marker.start()])
        if marker[1]:
            if depth:
                depth -= 1
            else:
                parts.clear()  # Orphan closing tag separates a hidden prefix.
        else:
            depth += 1
        end = raw.find(">", marker.end(), marker.end() + 256)
        cursor = end + 1 if end >= 0 else marker.end()
    if depth == 0:
        parts.append(raw[cursor:])
    return "".join(parts)


def sensitive_tool_source(value: Any) -> bool:
    """Unknown/unscanned source tails conservatively hide the corresponding reply."""
    budget = 2000
    scan_left = 256_000
    seen: set[int] = set()

    def check(item: Any, depth: int) -> bool:
        nonlocal budget, scan_left
        budget -= 1
        if budget < 0 or depth > 8:
            return True
        if isinstance(item, str):
            scan_left -= len(item)
            return scan_left < 0 or bool(_SENSITIVE_SOURCE.search(item))
        if not isinstance(item, (dict, list, tuple)) or id(item) in seen:
            return False
        seen.add(id(item))
        children = item.values() if isinstance(item, dict) else item
        return any(check(v, depth + 1) for v in children)

    return check(value, 0)


def tool_reply(value: Any, call_id: str | None) -> Any:
    """A Command may carry state: only select this invocation's explicit reply."""
    if isinstance(value, Command):
        update = value.update
        messages = update.get("messages", []) if isinstance(update, dict) else []
        if isinstance(messages, (list, tuple)):
            candidates = [
                m
                for m in islice(reversed(messages), 1000)
                if isinstance(m, ToolMessage) and (call_id is None or m.tool_call_id == call_id)
            ]
            if candidates and (call_id is not None or len(candidates) == 1):
                return candidates[0]
        return "[tool reply not available]"
    if isinstance(value, ToolMessage) and call_id is not None and value.tool_call_id != call_id:
        return "[tool reply not available]"
    return value


def tool_detail(value: Any, *, hidden: bool = False) -> dict[str, Any]:
    """Extract supported text/JSON only. Never stringify arbitrary object reprs."""
    redacted = hidden
    truncated = False
    budget = 2000
    seen: set[int] = set()
    chars_left = DETAIL_LIMIT
    scan_left = 256_000

    def text(raw: str) -> str:
        nonlocal redacted, truncated, chars_left, scan_left
        if len(raw) > scan_left:
            raw = raw[:scan_left]
            truncated = True
        scan_left -= len(raw)
        clean = _without_reasoning(raw)
        clean = _PRIVATE.sub(_REDACTED, clean)
        clean = _HEADERS.sub(lambda m: m[1] + _REDACTED, clean)
        clean = _BEARER.sub(lambda m: m[1] + _REDACTED, clean)
        clean = _ASSIGN.sub(lambda m: m[1] + _REDACTED, clean)
        clean = _QUERY.sub(lambda m: m[1] + _REDACTED, clean)
        clean = _USERINFO.sub(lambda m: m[1] + _REDACTED + "@", clean)
        clean = _KNOWN_TOKEN.sub(_REDACTED, clean)
        redacted |= clean != raw
        if len(clean) > chars_left:
            clean = clean[:chars_left]
            truncated = True
        chars_left -= len(clean)
        return clean

    def visit(item: Any, depth: int = 0) -> Any:
        nonlocal budget, truncated, redacted, scan_left
        budget -= 1
        if budget < 0 or depth > 8 or chars_left <= 0 or scan_left <= 0:
            truncated = True
            return "[truncated]"
        if isinstance(item, str):
            # JSON returned inside ToolMessage.content still needs key redaction.
            if item.lstrip().startswith(("{", "[")):
                if len(item) > scan_left:
                    truncated = True
                    return "[truncated]"
                scan_left -= len(item)
                try:
                    return visit(json.loads(item), depth + 1)
                except (ValueError, RecursionError):
                    pass
            return text(item)
        if isinstance(item, int) and item.bit_length() > 13000:
            truncated = True
            return "[integer exceeds display limit]"
        if isinstance(item, float) and not math.isfinite(item):
            return "[non-finite number]"
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if not isinstance(item, (dict, list, tuple)):
            return "[unsupported value]"
        if id(item) in seen:
            truncated = True
            return "[circular value]"
        seen.add(id(item))
        try:
            if isinstance(item, dict):
                block_type = item.get("type")
                if isinstance(block_type, str) and block_type in {
                    "image",
                    "image_url",
                    "audio",
                    "video",
                    "file",
                    "image_file",
                    "input_audio",
                    "input_image",
                    "output_image",
                    "input_file",
                    "output_file",
                    "output_audio",
                    "thinking",
                    "reasoning",
                    "redacted_thinking",
                }:
                    redacted = True
                    return "[non-text content omitted]"
                result: dict[str, Any] = {}
                for key, child in item.items():
                    budget -= 1
                    if budget < 0 or chars_left <= 0 or scan_left <= 0:
                        truncated = True
                        break
                    if not isinstance(key, str):
                        continue
                    if len(key) > 1000:
                        result[text(key[:1000])] = _REDACTED
                        truncated = True
                        redacted = True
                        continue
                    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                    if _SENSITIVE.search(normalized) or normalized in _HIDDEN_KEYS:
                        result[text(key)] = _REDACTED
                        redacted = True
                    else:
                        result[text(key)] = visit(child, depth + 1)
                return result
            result_list = []
            for child in item:
                if budget < 0 or chars_left <= 0 or scan_left <= 0:
                    truncated = True
                    break
                result_list.append(visit(child, depth + 1))
            return result_list
        finally:
            seen.discard(id(item))

    if hidden:
        safe: Any = _REDACTED
    elif isinstance(value, ToolMessage):
        content = value.content
        if isinstance(content, list):
            # Do not serialize message metadata, artifacts, images or reasoning blocks.
            truncated |= len(content) > 1000
            blocks = content
            content = [
                block if isinstance(block, str) else block["text"]
                for block in islice(content, 1000)
                if isinstance(block, str)
                or (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                )
            ]
            if len(content) != len(blocks):
                redacted = True
            if blocks and not content:
                content = "[non-text content omitted]"
        safe = visit(content)
    else:
        safe = visit(value)
    rendered = safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=False, indent=2)
    original_chars = len(rendered)
    if original_chars > DETAIL_LIMIT:
        rendered = rendered[:DETAIL_LIMIT]
        truncated = True
    return {"text": rendered, "truncated": truncated, "redacted": redacted}


def tool_detail_fields(name: str, value: Any, *, hidden: bool = False) -> dict[str, Any]:
    detail = tool_detail(value, hidden=hidden)
    return {
        name: detail["text"],
        f"{name}_truncated": detail["truncated"],
        f"{name}_redacted": detail["redacted"],
    }
