from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import AppConfig, ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]
ENV_KEYS = (
    "KOBOLD_BACKEND",
    "KOBOLD_ADMIN_PASSWORD",
    "KOBOLD_LISTEN_HOST",
    "KOBOLD_LISTEN_PORT",
    "KOBOLD_LOG_LEVEL",
    "CONFIG_PATH",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_example_config(clean_env: None) -> None:
    config = load_config(ROOT / "config.example.yaml")
    assert config.backend == "http://kobold:5001"
    assert config.default_model == "rp"
    assert config.models["rp"].filename == "gemma-rp.kcpps"
    assert config.models["rp"].force_on == ["/api/v1/generate", "/api/extra/generate/stream"]
    assert config.mappings["my-assistant"] == "assistant"
    assert config.admin_password is None
    assert "/v1/chat/completions" in config.openai_model_paths


def test_unknown_default_model() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "models": {"rp": {"filename": "a.kcpps"}},
                "default_model": "missing",
            }
        )


def test_duplicate_force_on() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "models": {
                    "rp": {"filename": "a.kcpps", "force_on": ["/api/v1/generate/"]},
                    "assistant": {"filename": "b.kcpps", "force_on": ["/api/v1/generate"]},
                },
                "default_model": "rp",
            }
        )


def test_env_overrides(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "backend: http://kobold:5001\n"
        "admin_password: fromfile\n"
        "models:\n"
        "  rp:\n"
        "    filename: a.kcpps\n"
        "default_model: rp\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KOBOLD_ADMIN_PASSWORD", "")
    monkeypatch.setenv("KOBOLD_BACKEND", "http://127.0.0.1:5001")
    monkeypatch.setenv("KOBOLD_LISTEN_PORT", "9090")
    config = load_config(path)
    assert config.admin_password == "fromfile"
    assert config.backend == "http://127.0.0.1:5001"
    assert config.listen_port == 9090


def test_bad_port_env(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "models:\n  rp:\n    filename: a.kcpps\ndefault_model: rp\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KOBOLD_LISTEN_PORT", "nope")
    with pytest.raises(ConfigError):
        load_config(path)
