"""kobold-router process entrypoint."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import AppConfig, ConfigError, load_config
from app.errors import RouterError
from app.proxy import install_routes
from app.routing import ModelRouter
from app.switcher import ModelCoordinator, ModelSwitcher

logger = logging.getLogger("kobold_router")


def create_app(
    config: AppConfig,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the ASGI app. `transport` is for tests that mock KoboldCPP."""
    _configure_logging(config.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        timeout = httpx.Timeout(
            connect=config.connect_timeout,
            # Generations and SSE have no natural upper bound unless configured.
            read=None if config.request_timeout == 0 else config.request_timeout,
            write=config.connect_timeout,
            pool=config.connect_timeout,
        )
        # Keepalive is off on purpose. A reload restarts KoboldCPP and the
        # pooled socket would still point at the dead process.
        client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=0, max_connections=100),
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )
        app.state.http = client
        app.state.coordinator = ModelCoordinator(config, ModelSwitcher(config, client))
        logger.info(
            "kobold-router ready, backend %s, default model %s (%s)",
            config.backend,
            config.default_model,
            config.models[config.default_model].filename,
        )
        try:
            yield
        finally:
            await client.aclose()

    # Built-in docs would shadow KoboldCPP's own /docs if a client asks for it.
    app = FastAPI(
        title="kobold-router",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = config
    app.state.router = ModelRouter(config)
    if config.cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[
                "x-kobold-router-model",
                "x-kobold-router-reload-seconds",
            ],
        )

    @app.exception_handler(RouterError)
    async def handle_router_error(_request: Request, exc: RouterError) -> JSONResponse:
        logger.warning("%s", exc.message)
        return JSONResponse(status_code=exc.status, content=exc.body())

    install_routes(app)
    return app


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("kobold_router").setLevel(level.upper())


def main() -> None:
    try:
        config = load_config()
    except (ConfigError, ValueError) as exc:
        print(f"kobold-router: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    # log_config=None keeps uvicorn from installing a second handler on top
    # of the one basicConfig already attached, which would print every line twice.
    uvicorn.run(
        create_app(config),
        host=config.listen_host,
        port=config.listen_port,
        log_level=config.log_level,
        log_config=None,
    )


if __name__ == "__main__":
    main()
