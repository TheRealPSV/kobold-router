"""Forward requests to KoboldCPP, including SSE streams.

Kobold generate bodies are forwarded as the original bytes. OpenAI JSON is
the exception: the `model` field is replaced with the mapped filename so a
backend that has router mode on loads the same file this proxy just loaded.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qsl, urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from app.config import AppConfig
from app.errors import RouterError
from app.routing import Decision, Incoming, ModelRouter
from app.switcher import ModelCoordinator, ReserveInfo, Target

logger = logging.getLogger("kobold_router.proxy")

# Hop-by-hop and headers this proxy consumes itself. content-length is
# rebuilt by httpx from the bytes we actually send (the OpenAI rewrite
# changes it).
DROP_REQUEST_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "x-force-model",
    }
)

# content-encoding and content-length are dropped because the body iterator
# yields decoded bytes, not the on-wire encoding.
DROP_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "content-encoding",
    }
)

PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def install_routes(app: FastAPI) -> None:
    app.add_api_route("/health", health, methods=["GET"])
    app.add_api_route("/status", status, methods=["GET"])
    app.add_api_route("/v1/models", list_models, methods=["GET"])
    # Separate wrappers so the catch-all path parameter is not also a query
    # parameter on the root route (FastAPI would treat an undeclared path
    # name as a query field and could confuse clients).
    app.add_api_route("/", proxy_root, methods=PROXY_METHODS, include_in_schema=False)
    app.add_api_route("/{full_path:path}", proxy_path, methods=PROXY_METHODS, include_in_schema=False)


async def health() -> dict:
    """Process liveness. Docker uses this, so it does not call KoboldCPP."""
    return {"status": "ok"}


async def status(request: Request) -> dict:
    config: AppConfig = request.app.state.config
    coordinator: ModelCoordinator = request.app.state.coordinator
    snap = await coordinator.snapshot()
    return {
        "proxy": "kobold-router",
        "backend": config.backend,
        "loaded_model": snap.loaded_key,
        "loaded_filename": snap.loaded_filename,
        "backend_model": snap.backend_model,
        "switching": snap.switching,
        "inflight": snap.inflight,
        "last_switch_seconds": snap.last_switch_seconds,
        "last_switch_at": snap.last_switch_at,
        "last_switch_reason": snap.last_switch_reason,
        "last_error": snap.last_error,
        "default_model": config.default_model,
        "models": {
            key: {"filename": spec.filename, "force_on": list(spec.force_on)}
            for key, spec in config.models.items()
        },
        "mappings": dict(config.mappings),
    }


async def list_models(request: Request) -> dict:
    """OpenAI model list built from config, so aliases show up in Open WebUI.

    This does not reload anything and does not need the backend to be up.
    """
    config: AppConfig = request.app.state.config
    return {
        "object": "list",
        "data": [
            {"id": model_id, "object": "model", "created": 0, "owned_by": "kobold-router"}
            for model_id in catalog_ids(config)
        ],
    }


def catalog_ids(config: AppConfig) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value not in seen:
            seen.add(value)
            ordered.append(value)

    for alias in config.mappings:
        add(alias)
    for key, spec in config.models.items():
        add(key)
        add(spec.filename)
    return ordered


async def proxy_root(request: Request) -> Response:
    return await forward(request)


async def proxy_path(request: Request, full_path: str) -> Response:
    del full_path
    return await forward(request)


async def forward(request: Request) -> Response:
    config: AppConfig = request.app.state.config
    router: ModelRouter = request.app.state.router
    coordinator: ModelCoordinator = request.app.state.coordinator
    client: httpx.AsyncClient = request.app.state.http

    raw = await request.body()
    parsed, body_model = inspect_body(raw, request.headers.get("content-type", ""))
    incoming = Incoming(
        method=request.method,
        path=request.url.path,
        header_model=_optional_header(request, "x-force-model"),
        query_model=_optional_query(request, "force_model"),
        body_model=body_model,
    )
    decision = router.decide(incoming)
    body, rewritten = apply_model_override(raw, parsed, decision)
    target = _target(decision)

    # Do not wrap this in `async with`. Returning a StreamingResponse exits
    # the context before the client has read the body, which would drop the
    # in-flight count while KoboldCPP is still generating and let another
    # client swap the model mid-stream.
    reservation = coordinator.reserve(target)
    info = await reservation.__aenter__()
    try:
        upstream = await client.send(
            _upstream_request(request, config, body, rewritten),
            stream=True,
        )
    except httpx.ConnectError as exc:
        await reservation.__aexit__(None, None, None)
        raise _unreachable(config, exc) from exc
    except httpx.TimeoutException as exc:
        await reservation.__aexit__(None, None, None)
        raise RouterError(
            504,
            f"Timed out contacting KoboldCPP at {config.backend}.",
            "backend_timeout",
        ) from exc
    except httpx.HTTPError as exc:
        await reservation.__aexit__(None, None, None)
        raise RouterError(
            502,
            f"Error contacting KoboldCPP at {config.backend}: {exc}",
            "backend_error",
        ) from exc

    _log_forward(request, decision, info)
    return _GuardedStream(
        _chunks(upstream),
        status_code=upstream.status_code,
        headers=_response_headers(upstream, info),
        upstream=upstream,
        reservation=reservation,
    )


class _GuardedStream(StreamingResponse):
    """Release the model slot and the upstream connection when the body ends."""

    def __init__(self, content, upstream: httpx.Response, reservation, **kwargs) -> None:
        super().__init__(content, **kwargs)
        self._upstream = upstream
        self._reservation = reservation

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self._upstream.aclose()
            finally:
                await self._reservation.__aexit__(None, None, None)


async def _chunks(upstream: httpx.Response):
    async for chunk in upstream.aiter_bytes():
        yield chunk


def _target(decision: Decision | None) -> Target | None:
    if decision is None:
        return None
    return Target(
        key=decision.model_key,
        filename=decision.filename,
        base_config=decision.base_config,
        expect=decision.expect,
        reason=decision.reason,
    )


def _upstream_request(
    request: Request,
    config: AppConfig,
    body: bytes,
    rewritten: bool,
) -> httpx.Request:
    query = _forwarded_query(request.url.query)
    url = config.backend + request.url.path
    if query:
        url = f"{url}?{query}"
    headers = {}
    for key, value in request.headers.items():
        if key.lower() in DROP_REQUEST_HEADERS:
            continue
        if rewritten and key.lower() in {"content-encoding"}:
            continue
        headers[key] = value
    if rewritten:
        headers["content-type"] = "application/json"
    return httpx.Request(request.method, url, headers=headers, content=body)


def _response_headers(upstream: httpx.Response, info: ReserveInfo) -> dict[str, str]:
    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in DROP_RESPONSE_HEADERS
    }
    if info.target is not None:
        headers["x-kobold-router-model"] = info.target.key
    if info.elapsed is not None:
        headers["x-kobold-router-reload-seconds"] = f"{info.elapsed:.3f}"
    return headers


def _forwarded_query(raw: str) -> str:
    pairs = [
        (key, value)
        for key, value in parse_qsl(raw, keep_blank_values=True)
        if key.lower() != "force_model"
    ]
    return urlencode(pairs)


def _optional_header(request: Request, name: str) -> str | None:
    if name not in request.headers:
        return None
    return request.headers[name].strip()


def _optional_query(request: Request, name: str) -> str | None:
    if name not in request.query_params:
        return None
    return request.query_params[name].strip()


def inspect_body(raw: bytes, content_type: str) -> tuple[dict | None, str | None]:
    """Return (object, model string) without modifying `raw`."""
    if not raw:
        return None, None
    looks_json = "json" in content_type.lower() or raw.lstrip()[:1] in (b"{", b"[")
    if not looks_json:
        return None, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    model = parsed.get("model")
    if not isinstance(model, str):
        return parsed, None
    return parsed, model.strip()


def apply_model_override(
    raw: bytes,
    parsed: dict | None,
    decision: Decision | None,
) -> tuple[bytes, bool]:
    """Replace the OpenAI model field. Kobold generate bodies are returned as-is."""
    if decision is None or not decision.rewrite_body:
        return raw, False
    if parsed is None:
        logger.warning(
            "loaded %s but left the body unchanged because it is not a JSON object",
            decision.filename,
        )
        return raw, False
    updated = dict(parsed)
    updated["model"] = decision.filename
    return json.dumps(updated, ensure_ascii=False).encode("utf-8"), True


def _log_forward(request: Request, decision: Decision | None, info: ReserveInfo) -> None:
    if decision is None:
        logger.debug("%s %s proxied with the model already loaded", request.method, request.url.path)
        return
    logger.info(
        "%s %s -> %s (%s) reason=%s switched=%s",
        request.method,
        request.url.path,
        decision.model_key,
        decision.filename,
        decision.reason,
        info.switched,
    )


def _unreachable(config: AppConfig, exc: Exception) -> RouterError:
    return RouterError(
        502,
        f"Could not reach KoboldCPP at {config.backend}: {exc}",
        "backend_unreachable",
    )
