from typing import Any

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import HumanMessage
from octop_harness.config import ModelConfig, ProviderConfig
from octop_harness.llm.factory import build_chat_model
from octop_harness.middleware import model_settings

from octop.infra.agents.middleware.output_budget import _with_output_budget
from octop.infra.agents.providers.store import ProviderStore


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


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        ({"max_tokens": 32_768}, 32_768),
        ({"max_tokens": 32_768, "max_output_tokens": 64_000}, 64_000),
        ({"max_tokens": 32_768, "max_output_tokens": 0}, 4096),
        ({"max_tokens": 32_768, "max_output_tokens": None}, 32_768),
    ],
)
def test_stored_model_output_cap_reaches_anthropic_payload(
    limits: dict[str, Any], expected: int
) -> None:
    config = ProviderStore._model_config_from_row({"id": "claude-opus-5-5", **limits})
    provider = ProviderConfig(
        id="relay",
        base_url="https://relay.test",
        api_key="test",
        protocol="anthropic",
        models=[config],
    )
    model = build_chat_model(provider, config)
    request = _with_output_budget(ModelRequest(model=model, messages=[HumanMessage("check")]))
    payload = model._get_request_payload(request.messages, **request.model_settings)

    assert payload["max_tokens"] == expected
    # An output-only value must not become a prompt/context limit.
    assert config.context_window == config.max_input_tokens == 0
