from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx

from app.config import AppConfig
from app.main import create_app


@asynccontextmanager
async def lifespan_client(app):
    """Run the ASGI lifespan, then talk to the app over httpx.

    httpx 0.28's ASGITransport does not start lifespan events itself.
    """
    shutdown = asyncio.Event()
    started = asyncio.Event()
    phase = {"value": "startup"}

    async def receive():
        if phase["value"] == "startup":
            phase["value"] = "running"
            return {"type": "lifespan.startup"}
        await shutdown.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message):
        if message["type"] == "lifespan.startup.complete":
            started.set()
        elif message["type"] == "lifespan.startup.failed":
            raise RuntimeError(message.get("message", "lifespan startup failed"))

    task = asyncio.create_task(
        app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router",
        ) as client:
            yield client
    finally:
        shutdown.set()
        await task

GENERATE = (
    b'{"prompt":"caf\xc3\xa9","max_length":32,"banned_tokens":["foo"],'
    b'"dry_multiplier":0.8,"dry_base":1.75,"dry_allowed_length":2,'
    b'"dry_penalty_last_n":256,"xtc_threshold":0.1,"xtc_probability":0.5}'
)


class FakeKobold:
    """Just enough of the admin and generate API for the proxy tests."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.reloads: list[dict] = []
        self.generate_bodies: list[bytes] = []
        self.chat_bodies: list[bytes] = []
        self.queries: list[str] = []
        self.generate_auths: list[str | None] = []
        self.reload_auths: list[str | None] = []
        self.version_failures_left = 0
        self.always_down = False
        self.model_name = "gemma-rp"
        self.reload_mode = "ok"  # ok, reject, unauthorized, missing
        self.generate_delay = 0.0
        self.hold_generate = False
        self.release_generate = asyncio.Event()
        self.generate_entered = asyncio.Event()
        self.current = ""
        self.paths: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if path == "/api/admin/reload_config":
            payload = json.loads(request.content)
            self.reloads.append(payload)
            self.reload_auths.append(request.headers.get("authorization"))
            self.current = payload["filename"]
            self.events.append(("reload-start", self.current))
            self.version_failures_left = 1
            self.events.append(("reload-end", self.current))
            if self.reload_mode == "reject":
                return httpx.Response(200, json={"success": False, "message": "missing file"})
            if self.reload_mode == "unauthorized":
                return httpx.Response(401, json={"error": "no"})
            if self.reload_mode == "missing":
                return httpx.Response(404, json={"error": "no admin"})
            return httpx.Response(200, json={"success": True})
        if path == "/api/extra/version":
            if self.always_down:
                return httpx.Response(503, json={"error": "restarting"})
            if self.version_failures_left > 0:
                self.version_failures_left -= 1
                return httpx.Response(503, json={"error": "restarting"})
            return httpx.Response(200, json={"result": "KoboldCpp", "version": "1"})
        if path == "/api/v1/model":
            return httpx.Response(200, json={"result": self.model_name})
        if path == "/api/v1/generate":
            self.queries.append(request.url.query.decode())
            self.generate_auths.append(request.headers.get("authorization"))
            self.generate_bodies.append(request.content)
            self.events.append(("gen-start", self.current))
            if self.hold_generate and not self.generate_entered.is_set():
                self.generate_entered.set()
                await self.release_generate.wait()
            elif self.generate_delay:
                await asyncio.sleep(self.generate_delay)
            self.events.append(("gen-end", self.current))
            assert "x-force-model" not in request.headers
            return httpx.Response(
                200,
                content=request.content,
                headers={"content-type": "application/json"},
            )
        if path == "/api/extra/generate/stream":
            self.generate_bodies.append(request.content)
            return httpx.Response(
                200,
                content=b"data: {\"token\": \"hi\"}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        if path == "/v1/chat/completions":
            self.chat_bodies.append(bytes(request.content))
            return httpx.Response(
                200,
                content=request.content,
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, json={"ok": True, "path": path})


def config(**overrides: object) -> AppConfig:
    data: dict = {
        "backend": "http://kobold:5001",
        "admin_password": "secret",
        "models": {
            "rp": {
                "filename": "gemma-rp.kcpps",
                "force_on": ["/api/v1/generate", "/api/extra/generate/stream"],
            },
            "assistant": {"filename": "gemma-assistant.kcpps"},
        },
        "default_model": "rp",
        "mappings": {"my-rp-model": "rp", "my-assistant": "assistant"},
        "reload_timeout": 2,
        "poll_interval": 0.01,
        "restart_detect_seconds": 0.5,
        "post_ready_delay": 0,
        "cors": False,
        "log_level": "warning",
    }
    data.update(overrides)
    return AppConfig.model_validate(data)


def test_preserves_kobold_body_and_uses_admin_password() -> None:
    asyncio.run(_preserves_kobold_body_and_uses_admin_password())


async def _preserves_kobold_body_and_uses_admin_password() -> None:
    backend = FakeKobold()
    app = create_app(config(), transport=httpx.MockTransport(backend))
    async with lifespan_client(app) as client:
        response = await client.post(
            "/api/v1/generate",
            content=GENERATE,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer client-key",
            },
        )
        assert response.status_code == 200
        assert response.content == GENERATE
        assert response.headers["x-kobold-router-model"] == "rp"
        assert "x-kobold-router-reload-seconds" in response.headers
        status = await client.get("/status")
        assert status.json()["loaded_filename"] == "gemma-rp.kcpps"
        assert status.json()["inflight"] == 0
        assert status.json()["backend_model"] == "gemma-rp"
    assert backend.generate_bodies == [GENERATE]
    assert backend.reloads == [{"filename": "gemma-rp.kcpps"}]
    assert backend.reload_auths == ["Bearer secret"]
    assert backend.generate_auths == ["Bearer client-key"]


def test_stream_and_force_header() -> None:
    asyncio.run(_stream_and_force_header())


async def _stream_and_force_header() -> None:
    backend = FakeKobold()
    app = create_app(config(), transport=httpx.MockTransport(backend))
    raw = b'{"prompt":"Hello","banned_tokens":["x"]}'
    async with lifespan_client(app) as client:
        response = await client.post(
            "/api/extra/generate/stream?force_model=assistant&sampler=1",
            content=raw,
            headers={"Content-Type": "application/json", "X-Force-Model": "my-assistant"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.text == 'data: {"token": "hi"}\n\n'
        assert response.headers["x-kobold-router-model"] == "assistant"
        # Header outranks the query pin. force_model must not reach KoboldCPP.
        followed = await client.post(
            "/api/v1/generate?force_model=rp&foo=1",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert followed.status_code == 200
    assert backend.reloads[0] == {"filename": "gemma-assistant.kcpps"}
    assert backend.generate_bodies[0] == raw
    assert "force_model" not in backend.queries[-1]
    assert "foo=1" in backend.queries[-1]


def test_openai_alias_rewrites_model_only() -> None:
    asyncio.run(_openai_alias_rewrites_model_only())


async def _openai_alias_rewrites_model_only() -> None:
    backend = FakeKobold()
    app = create_app(config(), transport=httpx.MockTransport(backend))
    raw = (
        b'{"model":"my-assistant","temperature":0.2,"stream":true,'
        b'"messages":[{"role":"user","content":"hi"}]}'
    )
    async with lifespan_client(app) as client:
        listed = await client.get("/v1/models")
        response = await client.post(
            "/v1/chat/completions",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
    ids = [item["id"] for item in listed.json()["data"]]
    assert ids[0] == "my-rp-model"
    assert "my-assistant" in ids
    assert "assistant" in ids
    assert "gemma-assistant.kcpps" not in ids
    forwarded = response.json()
    assert forwarded["model"] == "gemma-assistant.kcpps"
    assert forwarded["temperature"] == 0.2
    assert forwarded["stream"] is True
    assert forwarded["messages"] == [{"role": "user", "content": "hi"}]
    assert backend.reloads == [{"filename": "gemma-assistant.kcpps"}]


def test_model_catalog_is_not_proxied() -> None:
    asyncio.run(_model_catalog_is_not_proxied())


async def _model_catalog_is_not_proxied() -> None:
    backend = FakeKobold()
    app = create_app(config(), transport=httpx.MockTransport(backend))
    async with lifespan_client(app) as client:
        slash = await client.get("/v1/models/")
        bare = await client.get("/models")
        prefixed = await client.get("/assistant/v1/models")
        lcpp = await client.get("/lcpp/v1/models")
        unknown_prefix = await client.get("/not-a-model/v1/models")
        one = await client.get("/v1/models/my-assistant")
        missing = await client.get("/v1/models/no-such-model")
        head = await client.request("HEAD", "/models")
    assert slash.status_code == 200
    ids = [item["id"] for item in slash.json()["data"]]
    assert ids[0] == "my-rp-model"
    assert "my-assistant" in ids
    assert slash.json()["data"][0]["status"]["value"] == "loaded"
    for response in (bare, prefixed, lcpp, unknown_prefix):
        assert [item["id"] for item in response.json()["data"]] == ids
    assert "gemma-assistant.kcpps" not in ids
    assert one.json()["id"] == "my-assistant"
    assert one.json()["owned_by"] == "kobold-router"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "model_not_found"
    assert head.status_code == 200
    assert backend.paths == []
    assert backend.reloads == []


def test_unknown_model_does_not_reload() -> None:
    asyncio.run(_unknown_model_does_not_reload())


async def _unknown_model_does_not_reload() -> None:
    backend = FakeKobold()
    app = create_app(config(), transport=httpx.MockTransport(backend))
    async with lifespan_client(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "no-such-model", "messages": []},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "model_not_found"
        health = await client.get("/health")
        assert health.json() == {"status": "ok"}
        passthrough = await client.get("/api/extra/version")
        assert passthrough.status_code == 200
    assert backend.reloads == []


def test_reload_rejected_and_timeout() -> None:
    asyncio.run(_reload_rejected_and_timeout())


async def _reload_rejected_and_timeout() -> None:
    backend = FakeKobold()
    backend.reload_mode = "reject"
    app = create_app(config(), transport=httpx.MockTransport(backend))
    async with lifespan_client(app) as client:
        rejected = await client.post("/api/v1/generate", content=GENERATE)
        assert rejected.status_code == 502
        assert rejected.json()["error"]["code"] == "reload_rejected"
        assert "missing file" in rejected.json()["error"]["message"]

        backend.reload_mode = "unauthorized"
        denied = await client.post("/api/v1/generate", content=GENERATE)
        assert denied.status_code == 502
        assert denied.json()["error"]["code"] == "admin_unauthorized"

        backend.reload_mode = "missing"
        missing = await client.post("/api/v1/generate", content=GENERATE)
        assert "no /api/admin/reload_config" in missing.json()["error"]["message"]

    down = FakeKobold()
    down.always_down = True
    slow = create_app(
        config(reload_timeout=0.25, restart_detect_seconds=0.05, poll_interval=0.02),
        transport=httpx.MockTransport(down),
    )
    async with lifespan_client(slow) as client:
        timed_out = await client.post("/api/v1/generate", content=GENERATE)
        assert timed_out.status_code == 504
        assert timed_out.json()["error"]["code"] == "reload_timeout"


def test_initial_model_skips_reload() -> None:
    asyncio.run(_initial_model_skips_reload())


async def _initial_model_skips_reload() -> None:
    backend = FakeKobold()
    app = create_app(config(initial_model="rp"), transport=httpx.MockTransport(backend))
    async with lifespan_client(app) as client:
        response = await client.post(
            "/api/v1/generate",
            content=GENERATE,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert "x-kobold-router-reload-seconds" not in response.headers
    assert backend.reloads == []
    assert backend.generate_bodies == [GENERATE]


def test_base_config_is_sent() -> None:
    asyncio.run(_base_config_is_sent())


async def _base_config_is_sent() -> None:
    backend = FakeKobold()
    app = create_app(
        config(
            models={
                "rp": {
                    "filename": "model.gguf",
                    "base_config": "base.kcpps",
                    "force_on": ["/api/v1/generate"],
                },
                "assistant": {"filename": "gemma-assistant.kcpps"},
            }
        ),
        transport=httpx.MockTransport(backend),
    )
    async with lifespan_client(app) as client:
        response = await client.post("/api/v1/generate", content=GENERATE)
        assert response.status_code == 200
    assert backend.reloads == [{"filename": "model.gguf", "overrideconfig": "base.kcpps"}]


def test_concurrent_requests_do_not_thrash() -> None:
    asyncio.run(_concurrent_requests_do_not_thrash())


async def _concurrent_requests_do_not_thrash() -> None:
    backend = FakeKobold()
    backend.hold_generate = True
    app = create_app(config(), transport=httpx.MockTransport(backend))
    async with lifespan_client(app) as client:
        first = asyncio.create_task(
            client.post(
                "/api/v1/generate",
                content=GENERATE,
                headers={"Content-Type": "application/json"},
            )
        )
        await asyncio.wait_for(backend.generate_entered.wait(), timeout=2)
        second = asyncio.create_task(
            client.post(
                "/api/v1/generate",
                content=b'{"prompt":"other"}',
                headers={"Content-Type": "application/json", "X-Force-Model": "assistant"},
            )
        )
        await asyncio.sleep(0.1)
        # The assistant request must wait. It must not reload during the
        # roleplay generation, and it must not generate yet.
        assert [name for name, _file in backend.events if name == "gen-start"] == ["gen-start"]
        assert [name for name, _file in backend.events if name == "reload-start"] == ["reload-start"]
        backend.release_generate.set()
        first_response, second_response = await asyncio.wait_for(
            asyncio.gather(first, second),
            timeout=5,
        )
        assert first_response.status_code == 200
        assert second_response.status_code == 200
    assert _gens_do_not_overlap_reloads(backend.events)
    assert _max_overlap(backend.events, "gen") == 1
    assert [item["filename"] for item in backend.reloads] == [
        "gemma-rp.kcpps",
        "gemma-assistant.kcpps",
    ]

    same = FakeKobold()
    same.hold_generate = True
    app = create_app(config(), transport=httpx.MockTransport(same))
    async with lifespan_client(app) as client:
        first = asyncio.create_task(client.post("/api/v1/generate", content=GENERATE))
        await asyncio.wait_for(same.generate_entered.wait(), timeout=2)
        second = asyncio.create_task(client.post("/api/v1/generate", content=b'{"prompt":"two"}'))
        await asyncio.sleep(0.1)
        # Same file: the second generate is allowed to run beside the first.
        assert [name for name, _file in same.events if name == "gen-start"] == [
            "gen-start",
            "gen-start",
        ]
        assert [name for name, _file in same.events if name == "reload-start"] == ["reload-start"]
        same.release_generate.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert [item["filename"] for item in same.reloads] == ["gemma-rp.kcpps"]
    assert _max_overlap(same.events, "gen") == 2
    assert len(same.generate_bodies) == 2


def test_root_is_router_page() -> None:
    asyncio.run(_root_is_router_page())


async def _root_is_router_page() -> None:
    backend = FakeKobold()
    app = create_app(config(), transport=httpx.MockTransport(backend))
    async with lifespan_client(app) as client:
        page = await client.get("/", headers={"Accept": "text/html"})
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "kobold-router" in page.text
        assert 'href="http://router/assistant"' in page.text
        assert "gemma-assistant.kcpps" in page.text
        assert "KoboldAI Lite" in page.text
        data = await client.get("/")
        assert data.headers["content-type"].startswith("application/json")
        assert data.json()["proxy"] == "kobold-router"
        assert data.json()["models"]["rp"]["filename"] == "gemma-rp.kcpps"
        icon = await client.get("/favicon.ico")
        assert icon.status_code == 204
        # A non-browser method on / is still an API forward.
        posted = await client.post("/", content=b"{}")
        assert posted.status_code == 200
    assert backend.paths == ["/"]


def test_model_in_base_url() -> None:
    asyncio.run(_model_in_base_url())


async def _model_in_base_url() -> None:
    backend = FakeKobold()
    app = create_app(config(), transport=httpx.MockTransport(backend))
    async with lifespan_client(app) as client:
        version = await client.get("/assistant/api/extra/version")
        assert version.status_code == 200
        assert backend.reloads == []
        response = await client.post(
            "/assistant/api/v1/generate",
            content=GENERATE,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert response.content == GENERATE
        assert response.headers["x-kobold-router-model"] == "assistant"
        missing = await client.post("/no-such/api/v1/generate", content=GENERATE)
        assert missing.status_code == 400
        assert missing.json()["error"]["code"] == "model_not_found"
    assert backend.reloads == [{"filename": "gemma-assistant.kcpps"}]
    assert "/api/v1/generate" in backend.paths
    assert "/assistant/api/v1/generate" not in backend.paths
    assert backend.generate_bodies == [GENERATE]


def _gens_do_not_overlap_reloads(events: list[tuple[str, str]]) -> bool:
    open_gen = False
    for name, _file in events:
        if name == "gen-start":
            if open_gen:
                return False
            open_gen = True
        elif name == "gen-end":
            open_gen = False
        elif name == "reload-start" and open_gen:
            return False
    return not open_gen


def _max_overlap(events: list[tuple[str, str]], kind: str) -> int:
    open_count = 0
    peak = 0
    for name, _file in events:
        if name == f"{kind}-start":
            open_count += 1
            peak = max(peak, open_count)
        elif name == f"{kind}-end":
            open_count -= 1
    return peak
