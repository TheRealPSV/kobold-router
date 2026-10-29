from __future__ import annotations

import pytest

from app.config import AppConfig
from app.errors import RouterError
from app.paths import path_matches
from app.routing import Incoming, ModelRouter

CONFIG = AppConfig.model_validate(
    {
        "models": {
            "rp": {
                "filename": "gemma-rp.kcpps",
                "force_on": ["/api/v1/generate", "/api/extra/generate/stream"],
            },
            "assistant": {"filename": "gemma-assistant.kcpps", "expect": "assistant"},
        },
        "default_model": "rp",
        "mappings": {"my-rp-model": "rp", "my-assistant": "assistant"},
    }
)


def decide(**kwargs: str | None):
    incoming = Incoming(
        method=kwargs.pop("method", "POST"),
        path=kwargs.pop("path"),
        header_model=kwargs.pop("header_model", None),
        query_model=kwargs.pop("query_model", None),
        body_model=kwargs.pop("body_model", None),
    )
    assert not kwargs
    return ModelRouter(CONFIG).decide(incoming)


def test_prefix_and_slash() -> None:
    assert path_matches("/api/v1/generate", "/api/v1/generate/")
    assert path_matches("/v1/*", "/v1/chat/completions")
    assert not path_matches("/v1/models", "/v1/chat/completions")


def test_force_on_beats_body_model() -> None:
    decision = decide(path="/api/v1/generate", body_model="my-assistant")
    assert decision is not None
    assert decision.reason == "force_on"
    assert decision.model_key == "rp"
    assert decision.rewrite_body is False


def test_header_beats_force_on() -> None:
    decision = decide(path="/api/extra/generate/stream", header_model="assistant")
    assert decision is not None
    assert decision.reason == "header"
    assert decision.filename == "gemma-assistant.kcpps"
    assert decision.expect == "assistant"


def test_empty_header_uses_default() -> None:
    decision = decide(path="/v1/chat/completions", header_model="")
    assert decision is not None
    assert decision.model_key == "rp"
    assert decision.rewrite_body is True


def test_openai_alias() -> None:
    decision = decide(path="/v1/chat/completions", body_model="my-assistant")
    assert decision is not None
    assert decision.reason == "body_model"
    assert decision.filename == "gemma-assistant.kcpps"
    assert decision.rewrite_body is True


def test_generate_without_model_uses_default_when_not_forced() -> None:
    decision = decide(path="/api/latest/generate")
    assert decision is not None
    assert decision.reason == "default_model"
    assert decision.model_key == "rp"


def test_unknown_openai_model() -> None:
    with pytest.raises(RouterError) as caught:
        decide(path="/v1/completions", body_model="nope")
    assert caught.value.status == 400
    assert caught.value.code == "model_not_found"


def test_version_is_not_pinned() -> None:
    assert decide(method="GET", path="/api/extra/version") is None


def test_filename_and_special_names() -> None:
    by_file = decide(path="/v1/completions", body_model="gemma-rp.kcpps")
    assert by_file is not None and by_file.model_key == "rp"
    initial = decide(path="/v1/completions", header_model="initial_model")
    assert initial is not None and initial.filename == "initial_model"
