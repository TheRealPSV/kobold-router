"""Decide which configured model a request should run on.

Rules are tried in order. The first one that returns a Decision wins.
Append a rule to ``ModelRouter.rules`` to support a new client later.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import AppConfig, ModelSpec
from app.errors import RouterError
from app.paths import path_matches

# KoboldCPP accepts these names in reload_config without a file in admindir.
SPECIAL_FILENAMES = frozenset({"initial_model", "unload_model"})
_RAW_SUFFIXES = (".kcpps", ".kcppt", ".gguf")


@dataclass(frozen=True)
class Incoming:
    """The few fields routing looks at. The raw body is not stored here."""

    method: str
    path: str
    # None means the client did not send the field. "" means it was sent empty.
    header_model: str | None
    query_model: str | None
    body_model: str | None


@dataclass(frozen=True)
class Resolved:
    key: str
    filename: str
    base_config: str | None
    expect: str | None


@dataclass(frozen=True)
class Decision:
    model_key: str
    filename: str
    base_config: str | None
    expect: str | None
    reason: str
    # When true, the OpenAI `model` field is rewritten to `filename` so
    # KoboldCPP router mode (if enabled) loads the same file we just did.
    rewrite_body: bool


def resolve_reference(config: AppConfig, name: str) -> Resolved:
    """Map a client-supplied name to a configured file.

    Accepts `default`, a mappings alias, a models key, or a configured filename.
    """
    raw = name.strip()
    if not raw or raw == "default":
        raw = config.default_model

    key = config.mappings.get(raw, raw)
    spec = config.models.get(key)
    if spec is not None:
        return Resolved(key, spec.filename, spec.base_config, spec.expect)

    for model_key, candidate in config.models.items():
        if candidate.filename == name.strip() or candidate.filename == key:
            return Resolved(model_key, candidate.filename, candidate.base_config, candidate.expect)

    chosen = name.strip()
    if chosen in SPECIAL_FILENAMES:
        return Resolved(chosen, chosen, None, None)

    if config.allow_raw_filenames and chosen.lower().endswith(_RAW_SUFFIXES):
        return Resolved(chosen, chosen, None, None)

    known = ", ".join([*config.models.keys(), *config.mappings.keys()])
    raise RouterError(
        400,
        f"Unknown model {name.strip()!r}. Known names: {known}.",
        "model_not_found",
    )


def _rewrite_for(config: AppConfig, method: str, path: str) -> bool:
    if method.upper() not in {"POST", "PUT", "PATCH"}:
        return False
    return any(path_matches(pattern, path) for pattern in config.openai_model_paths)


def _decision(config: AppConfig, incoming: Incoming, resolved: Resolved, reason: str) -> Decision:
    return Decision(
        model_key=resolved.key,
        filename=resolved.filename,
        base_config=resolved.base_config,
        expect=resolved.expect,
        reason=reason,
        rewrite_body=_rewrite_for(config, incoming.method, incoming.path),
    )


class ForceHeaderRule:
    """`X-Force-Model` pins a model without changing the JSON body.

    An empty header means the configured default model.
    """

    name = "header"

    def evaluate(self, incoming: Incoming, config: AppConfig) -> Decision | None:
        if incoming.header_model is None:
            return None
        return _decision(config, incoming, resolve_reference(config, incoming.header_model), self.name)


class ForceQueryRule:
    """`?force_model=` is the query-string form of X-Force-Model."""

    name = "query"

    def evaluate(self, incoming: Incoming, config: AppConfig) -> Decision | None:
        if incoming.query_model is None:
            return None
        return _decision(config, incoming, resolve_reference(config, incoming.query_model), self.name)


class ForcePathRule:
    """Server-side pins from each model's `force_on` list."""

    name = "force_on"

    def evaluate(self, incoming: Incoming, config: AppConfig) -> Decision | None:
        for key, spec in config.models.items():
            for pattern in spec.force_on:
                if path_matches(pattern, incoming.path):
                    resolved = Resolved(key, spec.filename, spec.base_config, spec.expect)
                    return _decision(config, incoming, resolved, self.name)
        return None


class BodyModelRule:
    """Honor a JSON `model` field on OpenAI and Kobold generate paths."""

    name = "body_model"

    def evaluate(self, incoming: Incoming, config: AppConfig) -> Decision | None:
        if not incoming.body_model:
            return None
        watched = [*config.openai_model_paths, *config.generate_paths]
        if not any(path_matches(pattern, incoming.path) for pattern in watched):
            return None
        return _decision(
            config,
            incoming,
            resolve_reference(config, incoming.body_model),
            self.name,
        )


class DefaultGenerateRule:
    """Kobold generate calls with no model name load `default_model`."""

    name = "default_model"

    def evaluate(self, incoming: Incoming, config: AppConfig) -> Decision | None:
        if incoming.method.upper() not in {"POST", "PUT"}:
            return None
        if not any(path_matches(pattern, incoming.path) for pattern in config.generate_paths):
            return None
        spec: ModelSpec = config.models[config.default_model]
        resolved = Resolved(config.default_model, spec.filename, spec.base_config, spec.expect)
        return _decision(config, incoming, resolved, self.name)


class ModelRouter:
    """Ordered routing rules. First match wins."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.rules: list = [
            ForceHeaderRule(),
            ForceQueryRule(),
            ForcePathRule(),
            BodyModelRule(),
            DefaultGenerateRule(),
        ]

    def decide(self, incoming: Incoming) -> Decision | None:
        for rule in self.rules:
            decision = rule.evaluate(incoming, self.config)
            if decision is not None:
                return decision
        return None
