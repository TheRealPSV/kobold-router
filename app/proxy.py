"""Forward requests to KoboldCPP, including SSE streams.

Kobold generate bodies are forwarded as the original bytes. OpenAI JSON is
the exception: the `model` field is replaced with the mapped filename so a
backend that has router mode on loads the same file this proxy just loaded.
"""

from __future__ import annotations

import json
import logging
from html import escape
from urllib.parse import parse_qsl, urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from app.config import AppConfig
from app.errors import RouterError
from app.routing import Decision, Incoming, ModelRouter, split_model_prefix
from app.switcher import ModelCoordinator, ReserveInfo, StatusSnapshot, Target

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
    # The browser address for this process. KoboldAI Lite stays on the
    # KoboldCPP port; proxying GET / made the router look like that UI.
    app.add_api_route("/", home, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route("/favicon.ico", favicon, methods=["GET", "HEAD"], include_in_schema=False)
    # Separate wrappers so the catch-all path parameter is not also a query
    # parameter on the root route (FastAPI would treat an undeclared path
    # name as a query field and could confuse clients).
    app.add_api_route(
        "/",
        proxy_root,
        methods=["POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    app.add_api_route("/{full_path:path}", proxy_path, methods=PROXY_METHODS, include_in_schema=False)


async def health() -> dict:
    """Process liveness. Docker uses this, so it does not call KoboldCPP."""
    return {"status": "ok"}


async def status(request: Request) -> dict:
    config: AppConfig = request.app.state.config
    coordinator: ModelCoordinator = request.app.state.coordinator
    return status_payload(config, await coordinator.snapshot())


def status_payload(config: AppConfig, snap: StatusSnapshot) -> dict:
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


async def home(request: Request) -> Response:
    """Router page for the listen URL.

    Browsers send ``text/html``. Other clients get the same JSON as ``/status``.
    """
    config: AppConfig = request.app.state.config
    coordinator: ModelCoordinator = request.app.state.coordinator
    payload = status_payload(config, await coordinator.snapshot())
    if _wants_html(request):
        return HTMLResponse(_home_html(request, payload))
    return JSONResponse(payload)


async def favicon() -> Response:
    """Keep the browser tab from requesting KoboldCPP's icon through the proxy."""
    return Response(status_code=204)


def _wants_html(request: Request) -> bool:
    for part in request.headers.get("accept", "").split(","):
        media = part.split(";", 1)[0].strip().lower()
        if media == "text/html":
            return True
        if media == "application/json":
            return False
    return False


def _home_html(request: Request, payload: dict) -> str:
    origin = str(request.base_url).rstrip("/")
    aliases_for: dict[str, list[str]] = {}
    for alias, key in payload["mappings"].items():
        aliases_for.setdefault(key, []).append(alias)

    blocks: list[str] = []
    for key, spec in payload["models"].items():
        public = [key, *aliases_for.get(key, [])]
        openai_ids = list(public)
        if spec["filename"] not in openai_ids:
            openai_ids.append(spec["filename"])
        pinned = ", ".join(spec["force_on"]) if spec["force_on"] else "—"
        marker = " (default)" if key == payload["default_model"] else ""
        links = "".join(
            f'<li><a href="{_esc(origin)}/{_esc(name)}">{_esc(origin)}/{_esc(name)}</a></li>'
            for name in public
        )
        ids = ", ".join(f"<code>{_esc(name)}</code>" for name in openai_ids)
        blocks.append(
            "<section>"
            f"<h3><code>{_esc(key)}</code>{_esc(marker)}</h3>"
            "<dl>"
            f"<dt>File</dt><dd><code>{_esc(spec['filename'])}</code></dd>"
            f"<dt>Pinned paths</dt><dd>{_esc(pinned)}</dd>"
            f"<dt>SillyTavern URL</dt><dd><ul>{links}</ul></dd>"
            f"<dt>OpenAI model id</dt><dd>{ids}</dd>"
            "</dl>"
            "</section>"
        )

    loaded = payload["loaded_model"] or "none yet"
    reported = payload["backend_model"] or "—"
    error = payload["last_error"]
    error_row = (
        f"<tr><th>Last error</th><td>{_esc(error)}</td></tr>" if error else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>kobold-router</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 52rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.45; background: #fafafa; color: #1a1a1a; }}
  code {{ font-family: ui-monospace, monospace; font-size: 0.92em; word-break: break-all; }}
  table, dl {{ border-collapse: collapse; width: 100%; }}
  th, td, dt, dd {{ text-align: left; vertical-align: top; }}
  th, td {{ padding: 0.35rem 0.8rem 0.35rem 0; }}
  dt {{ font-weight: 600; margin-top: 0.45rem; }}
  dd {{ margin: 0.1rem 0 0; }}
  section {{ margin-top: 1.25rem; }}
  ul {{ margin: 0.15rem 0 0; padding-left: 1.1rem; }}
  h1 {{ font-size: 1.4rem; }}
  a {{ color: inherit; }}
</style>
</head>
<body>
<h1>kobold-router</h1>
<p>This address switches models and forwards the Kobold and OpenAI APIs. KoboldAI Lite is served by KoboldCPP at <code>{_esc(payload["backend"])}</code>.</p>
<h2>Loaded</h2>
<table>
<tr><th>Config</th><td><code>{_esc(loaded)}</code></td></tr>
<tr><th>File</th><td><code>{_esc(payload["loaded_filename"] or "—")}</code></td></tr>
<tr><th>KoboldCPP reports</th><td><code>{_esc(reported)}</code></td></tr>
<tr><th>Switching</th><td>{"yes" if payload["switching"] else "no"}</td></tr>
<tr><th>In flight</th><td>{_esc(payload["inflight"])}</td></tr>
{error_row}
</table>
<h2>Models</h2>
{"".join(blocks)}
<p><a href="{_esc(origin)}/status">/status</a> · <a href="{_esc(origin)}/health">/health</a> · <a href="{_esc(origin)}/v1/models">/v1/models</a></p>
</body>
</html>
"""


def _esc(value: object) -> str:
    return escape(str(value), quote=True)


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
    # `/assistant/api/v1/generate` is the same generate call with the model
    # chosen by the base URL SillyTavern was given.
    url_model, backend_path = split_model_prefix(config, request.url.path)
    incoming = Incoming(
        method=request.method,
        path=backend_path,
        header_model=_optional_header(request, "x-force-model"),
        query_model=_optional_query(request, "force_model"),
        body_model=body_model,
        url_model=url_model,
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
            _upstream_request(request, config, backend_path, body, rewritten),
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
    backend_path: str,
    body: bytes,
    rewritten: bool,
) -> httpx.Request:
    query = _forwarded_query(request.url.query)
    url = config.backend + backend_path
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
