"""YAML config plus a few environment overrides for Docker."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.paths import normalize_path

DEFAULT_GENERATE_PATHS = [
    "/api/v1/generate",
    "/api/latest/generate",
    "/api/extra/generate/stream",
    "/request",
]

DEFAULT_OPENAI_PATHS = [
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
    "/v1/responses",
]


class ConfigError(Exception):
    """The config file is missing or invalid."""


class ModelSpec(BaseModel):
    """One switchable KoboldCPP config or GGUF file."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    force_on: list[str] = Field(default_factory=list)
    # Sent as reload_config `overrideconfig` when switching to a bare GGUF.
    base_config: str | None = None
    # After reload, GET /api/v1/model must contain this substring.
    expect: str | None = None

    @field_validator("filename", "base_config", "expect")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("force_on")
    @classmethod
    def clean_paths(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            item = item.strip()
            if not item.startswith("/"):
                raise ValueError(f"force_on path must start with '/': {item!r}")
            cleaned.append(item)
        return cleaned


class AppConfig(BaseModel):
    """Runtime settings. Unknown keys are rejected so typos fail at startup."""

    model_config = ConfigDict(extra="forbid")

    backend: str = "http://kobold:5001"
    admin_password: str | None = None
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080

    reload_timeout: float = 300
    poll_interval: float = 1.0
    # reload_config returns before KoboldCPP has restarted. Wait until a probe
    # fails (the process actually dropped) or this window passes, then wait
    # until a probe succeeds.
    wait_for_restart: bool = True
    restart_detect_seconds: float = 15
    post_ready_delay: float = 0.5
    ready_path: str = "/api/extra/version"
    model_path: str = "/api/v1/model"

    # 0 means no read timeout. Generations and SSE streams can run for minutes.
    request_timeout: float = 0
    connect_timeout: float = 10
    # How long to wait for the admin POST itself. A dropped connection after
    # this is treated as "reload started" and we move on to polling.
    reload_request_timeout: float = 30

    models: dict[str, ModelSpec]
    default_model: str
    mappings: dict[str, str] = Field(default_factory=dict)
    # Skip the first reload when you know KoboldCPP already loaded this key.
    initial_model: str | None = None
    allow_raw_filenames: bool = False

    generate_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_GENERATE_PATHS))
    openai_model_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_OPENAI_PATHS))

    cors: bool = True
    log_level: str = "info"

    @field_validator("backend")
    @classmethod
    def clean_backend(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("backend must be an http or https URL")
        return value

    @field_validator("admin_password", "initial_model")
    @classmethod
    def empty_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("listen_port")
    @classmethod
    def port_range(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("listen_port must be between 1 and 65535")
        return value

    @field_validator("log_level")
    @classmethod
    def clean_level(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"debug", "info", "warning", "error"}:
            raise ValueError("log_level must be debug, info, warning, or error")
        return value

    @model_validator(mode="after")
    def check_references(self) -> AppConfig:
        if self.reload_timeout <= 0:
            raise ValueError("reload_timeout must be positive")
        if self.poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if self.default_model not in self.models:
            raise ValueError(f"default_model {self.default_model!r} is not defined in models")
        if self.initial_model is not None and self.initial_model not in self.models:
            raise ValueError(f"initial_model {self.initial_model!r} is not defined in models")
        for alias, target in self.mappings.items():
            if not alias.strip():
                raise ValueError("mapping alias must not be empty")
            if target not in self.models:
                raise ValueError(f"mapping {alias!r} points at unknown model {target!r}")
        claimed: dict[str, str] = {}
        for key, spec in self.models.items():
            for pattern in spec.force_on:
                identity = pattern if pattern.endswith("*") else normalize_path(pattern)
                owner = claimed.get(identity)
                if owner is not None:
                    raise ValueError(
                        f"{pattern} is listed in force_on for both {owner!r} and {key!r}"
                    )
                claimed[identity] = key
        for label, paths in (
            ("generate_paths", self.generate_paths),
            ("openai_model_paths", self.openai_model_paths),
            ("ready_path", [self.ready_path]),
            ("model_path", [self.model_path]),
        ):
            for path in paths:
                if not path.startswith("/"):
                    raise ValueError(f"{label} entry must start with '/': {path!r}")
        # Touch normalize so a trailing-slash-only path is still a real route.
        normalize_path(self.ready_path)
        return self


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load YAML from `path` or CONFIG_PATH (default `config.yaml`)."""
    raw_path = path or os.environ.get("CONFIG_PATH", "config.yaml")
    config_path = Path(raw_path)
    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {config_path}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping")
    try:
        config = AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid config {config_path}:\n{exc}") from exc
    return _apply_env(config)


def _apply_env(config: AppConfig) -> AppConfig:
    """Non-empty environment variables win over the file.

    Compose always interpolates unset variables to an empty string. Empty
    values are ignored so they do not wipe `admin_password: null`.
    """
    updates: dict = {}
    backend = os.environ.get("KOBOLD_BACKEND", "").strip()
    if backend:
        updates["backend"] = backend
    password = os.environ.get("KOBOLD_ADMIN_PASSWORD", "").strip()
    if password:
        updates["admin_password"] = password
    host = os.environ.get("KOBOLD_LISTEN_HOST", "").strip()
    if host:
        updates["listen_host"] = host
    port = os.environ.get("KOBOLD_LISTEN_PORT", "").strip()
    if port:
        try:
            updates["listen_port"] = int(port)
        except ValueError as exc:
            raise ConfigError(f"KOBOLD_LISTEN_PORT is not an integer: {port}") from exc
    level = os.environ.get("KOBOLD_LOG_LEVEL", "").strip()
    if level:
        updates["log_level"] = level
    if not updates:
        return config
    try:
        return config.model_copy(update=updates)
    except ValidationError as exc:
        raise ConfigError(f"Invalid environment override:\n{exc}") from exc
