"""Bridge stable SDK context metadata to LangChain's model-only stream parser.

The stable Anthropic SDK retains the beta ``context_management`` field as a
plain dict. langchain-anthropic 1.7.2 unconditionally calls ``model_dump`` on it
at the end of a stream. Keep the metadata intact without changing SDK files.
"""

from __future__ import annotations

from functools import wraps
from typing import Any

from pydantic import RootModel


def install_anthropic_stream_metadata_compat() -> None:
    """Install once before model calls; sync and async use the same parser."""
    from langchain_anthropic import ChatAnthropic

    original = ChatAnthropic._make_message_chunk_from_anthropic_event
    if getattr(original, "_octop_context_metadata_compat", False):
        return

    @wraps(original)
    def parse(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(event, "type", None) == "message_delta":
            metadata = getattr(event, "context_management", None)
            if isinstance(metadata, dict) and metadata:
                # Copy the event: never mutate SDK stream state or drop unknown
                # metadata keys by coercing them into a narrower beta schema.
                event = event.model_copy(
                    update={"context_management": RootModel[dict[str, Any]](dict(metadata))}
                )
        return original(self, event, *args, **kwargs)

    parse._octop_context_metadata_compat = True  # type: ignore[attr-defined]
    ChatAnthropic._make_message_chunk_from_anthropic_event = parse  # type: ignore[method-assign]
