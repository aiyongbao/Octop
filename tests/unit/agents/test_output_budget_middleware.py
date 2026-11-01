from typing import Any

import pytest
from langchain.agents.middleware import ModelRequest
from octop_harness.config import ModelConfig, ProviderConfig
from octop_harness.llm.factory import build_chat_model
from octop_harness.middleware import model_settings

from octop.infra.agents.middleware.output_budget import _with_output_budget


def _anthropic_model(**limits: Any) -> Any:
    provider = ProviderConfig(
        id="relay",
        name="relay",
        base_url="https://relay.test",
        protocol="anthropic",
        api_key="test",
        models=[ModelConfig(id="claude-opus-5-5", **limits)],
    )
    return build_chat_model(provider, provider.models[0])


@pytest.fixture(autouse=True)
def _graph_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_settings, "runtime_config", lambda _request: {"configurable": {"thread_id": "t"}}
    )


def test_defaults_max_tokens_to_model_max_output_tokens() -> None:
    model = _anthropic_model(max_output_tokens=128_000)
    assert model.max_tokens == 4096  # ChatAnthropic fallback for unknown ids

    configured = _with_output_budget(ModelRequest(model=model, messages=[]))

    assert configured.model_settings["max_tokens"] == 128_000


def test_explicit_agent_max_tokens_wins() -> None:
    model = _anthropic_model(max_output_tokens=128_000)
    request = ModelRequest(model=model, messages=[], model_settings={"max_tokens": 2048})

    assert _with_output_budget(request) is request


def test_unknown_output_cap_leaves_request_untouched() -> None:
    model = _anthropic_model(max_input_tokens=200_000)
    request = ModelRequest(model=model, messages=[])

    assert _with_output_budget(request) is request


def test_reserves_output_inside_context_window() -> None:
    model = _anthropic_model(
        context_window=200_000, max_input_tokens=200_000, max_output_tokens=64_000
    )

    _with_output_budget(ModelRequest(model=model, messages=[]))

    assert model.profile["max_input_tokens"] == 136_000
