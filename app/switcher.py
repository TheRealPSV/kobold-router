"""Serialize model reloads and wait until KoboldCPP answers again.

KoboldCPP's `/api/admin/reload_config` returns as soon as the reload is
accepted. The API process then restarts, which drops in-flight connections.
Callers have to poll a cheap endpoint until it succeeds. See
https://github.com/LostRuins/koboldcpp/issues/1945.

A single condition variable keeps two clients from swapping the model out
from under each other:

* requests for the loaded file run together (KoboldCPP has its own queue)
* a request for a different file waits until those finish, reloads, then runs
* anything else waits out an in-progress reload, because the port is down
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

from app.config import AppConfig
from app.errors import ReloadFailed, ReloadRejected
from app.paths import normalize_path

logger = logging.getLogger("kobold_router.switcher")


@dataclass(frozen=True)
class Target:
    key: str
    filename: str
    base_config: str | None
    expect: str | None
    reason: str


@dataclass(frozen=True)
class ReserveInfo:
    switched: bool
    elapsed: float | None
    target: Target | None


@dataclass(frozen=True)
class ReloadResult:
    backend_model: str | None
    seconds: float


@dataclass
class StatusSnapshot:
    loaded_key: str | None
    loaded_filename: str | None
    backend_model: str | None
    switching: bool
    inflight: int
    last_switch_seconds: float | None
    last_switch_at: str | None
    last_switch_reason: str | None
    last_error: str | None


class ModelSwitcher:
    """HTTP calls that change the model. Does not decide *when* to switch."""

    def __init__(self, config: AppConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self._client = client

    def _url(self, path: str) -> str:
        return self.config.backend + normalize_path(path)

    def _admin_headers(self) -> dict[str, str]:
        if not self.config.admin_password:
            return {}
        return {"Authorization": f"Bearer {self.config.admin_password}"}

    async def reload(self, target: Target) -> ReloadResult:
        started = time.perf_counter()
        await self._request_reload(target)
        backend_model = await self._wait_until_ready(target)
        return ReloadResult(backend_model=backend_model, seconds=time.perf_counter() - started)

    async def _request_reload(self, target: Target) -> None:
        payload: dict[str, str] = {"filename": target.filename}
        if target.base_config:
            payload["overrideconfig"] = target.base_config
        url = self._url("/api/admin/reload_config")
        timeout = httpx.Timeout(self.config.reload_request_timeout)
        try:
            response = await self._client.post(
                url,
                json=payload,
                headers={**self._admin_headers(), "Content-Type": "application/json"},
                timeout=timeout,
            )
        except httpx.ConnectError as exc:
            raise ReloadRejected(
                502,
                f"Could not reach KoboldCPP at {self.config.backend} to load {target.filename!r}: {exc}",
                "backend_unreachable",
            ) from exc
        except httpx.TimeoutException:
            # The admin handler often dies mid-response because the process
            # restarts. Polling distinguishes "started" from "never accepted".
            logger.warning("reload POST for %s timed out; polling for restart", target.filename)
            return
        except httpx.TransportError as exc:
            logger.warning(
                "reload POST for %s lost the connection (%s); polling for restart",
                target.filename,
                exc,
            )
            return

        if response.status_code in {401, 403}:
            raise ReloadRejected(
                502,
                "KoboldCPP rejected the admin reload (HTTP "
                f"{response.status_code}). Set admin_password to the server's "
                "--adminpassword.",
                "admin_unauthorized",
            )
        if response.status_code == 404:
            raise ReloadRejected(
                502,
                "KoboldCPP has no /api/admin/reload_config. Start it with --admin "
                "and --admindir pointing at the folder that holds the model files.",
                "admin_disabled",
            )
        if response.status_code >= 400:
            raise ReloadRejected(
                502,
                f"KoboldCPP reload of {target.filename!r} failed with HTTP "
                f"{response.status_code}: {_short(response.text)}",
                "reload_rejected",
            )

        body = _json_or_none(response)
        if isinstance(body, dict) and body.get("success") is False:
            detail = body.get("message") or body.get("error") or body
            raise ReloadRejected(
                502,
                f"KoboldCPP refused to load {target.filename!r}: {detail}",
                "reload_rejected",
            )

    async def _wait_until_ready(self, target: Target) -> str | None:
        deadline = time.monotonic() + self.config.reload_timeout
        if self.config.wait_for_restart:
            detect_until = min(deadline, time.monotonic() + self.config.restart_detect_seconds)
            saw_down = False
            while time.monotonic() < detect_until:
                up, _reported = await self._probe()
                if not up:
                    saw_down = True
                    break
                await asyncio.sleep(self.config.poll_interval)
            if not saw_down:
                logger.info(
                    "backend stayed reachable for %.1fs after requesting %s; "
                    "waiting for it to settle",
                    self.config.restart_detect_seconds,
                    target.filename,
                )

        while time.monotonic() < deadline:
            up, reported = await self._probe()
            if up and _expect_met(target.expect, reported):
                if self.config.post_ready_delay > 0:
                    await asyncio.sleep(self.config.post_ready_delay)
                    up_again, reported_again = await self._probe()
                    if not up_again or not _expect_met(target.expect, reported_again):
                        continue
                    reported = reported_again
                return reported
            await asyncio.sleep(self.config.poll_interval)

        raise ReloadFailed(
            504,
            f"Timed out after {self.config.reload_timeout:.0f}s waiting for KoboldCPP "
            f"to finish loading {target.filename!r}.",
            "reload_timeout",
        )

    async def _probe(self) -> tuple[bool, str | None]:
        """Return (up, reported model name). Connection errors count as down."""
        try:
            response = await self._client.get(
                self._url(self.config.ready_path),
                timeout=self._probe_timeout(),
            )
        except httpx.HTTPError:
            return False, None
        if response.status_code != 200:
            return False, None

        reported = _result_string(response) if _same_path(self.config.ready_path, self.config.model_path) else None
        if reported is None:
            reported = await self._fetch_model_name()
        return True, reported

    async def _fetch_model_name(self) -> str | None:
        try:
            response = await self._client.get(
                self._url(self.config.model_path),
                headers=self._admin_headers(),
                timeout=self._probe_timeout(),
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        return _result_string(response)

    def _probe_timeout(self) -> httpx.Timeout:
        # One probe at a time. A router-mode proxy may hold this call open
        # until the real server is back, so allow a decent slice of the budget.
        return httpx.Timeout(min(30.0, max(self.config.poll_interval, 5.0)))


class ModelCoordinator:
    """Gate around ModelSwitcher. See the module docstring."""

    def __init__(self, config: AppConfig, switcher: ModelSwitcher) -> None:
        self._switcher = switcher
        self._cond = asyncio.Condition()
        self.loaded_key: str | None = None
        self.loaded_filename: str | None = None
        self.backend_model: str | None = None
        self.switching = False
        self.inflight = 0
        self.last_switch_seconds: float | None = None
        self.last_switch_at: str | None = None
        self.last_switch_reason: str | None = None
        self.last_error: str | None = None
        if config.initial_model:
            spec = config.models[config.initial_model]
            self.loaded_key = config.initial_model
            self.loaded_filename = spec.filename

    @asynccontextmanager
    async def reserve(self, target: Target | None) -> AsyncIterator[ReserveInfo]:
        switched, elapsed = await self._acquire(target)
        try:
            yield ReserveInfo(switched=switched, elapsed=elapsed, target=target)
        finally:
            async with self._cond:
                self.inflight -= 1
                if self.inflight == 0:
                    self._cond.notify_all()

    async def _acquire(self, target: Target | None) -> tuple[bool, float | None]:
        announced_wait = False
        async with self._cond:
            while True:
                if self.switching:
                    await self._cond.wait()
                    continue
                if target is None or self._already_loaded(target):
                    self.inflight += 1
                    return False, None
                if self.inflight > 0:
                    if not announced_wait:
                        logger.info(
                            "waiting for %s in-flight request(s) to finish before "
                            "switching to %s (%s)",
                            self.inflight,
                            target.key,
                            target.filename,
                        )
                        announced_wait = True
                    await self._cond.wait()
                    continue
                # Idle and the wrong file is loaded: this task performs the reload.
                self.switching = True
                break

        assert target is not None
        logger.info("switching model to %s (%s) reason=%s", target.key, target.filename, target.reason)
        try:
            result = await self._switcher.reload(target)
        except asyncio.CancelledError:
            async with self._cond:
                self.switching = False
                self.last_error = "reload cancelled"
                self._cond.notify_all()
            raise
        except ReloadRejected as exc:
            async with self._cond:
                self.switching = False
                self.last_error = str(exc)
                self._cond.notify_all()
            raise
        except Exception as exc:
            async with self._cond:
                self.switching = False
                self.loaded_key = None
                self.loaded_filename = None
                self.backend_model = None
                self.last_error = str(exc)
                self._cond.notify_all()
            raise

        async with self._cond:
            self.loaded_key = target.key
            self.loaded_filename = target.filename
            self.backend_model = result.backend_model
            self.last_switch_seconds = result.seconds
            self.last_switch_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.last_switch_reason = target.reason
            self.last_error = None
            self.switching = False
            self.inflight += 1
            self._cond.notify_all()
        logger.info(
            "switched model to %s (%s) in %.2fs backend_model=%s",
            target.key,
            target.filename,
            result.seconds,
            result.backend_model,
        )
        return True, result.seconds

    def _already_loaded(self, target: Target) -> bool:
        if self.loaded_filename != target.filename:
            return False
        if target.expect is None:
            return True
        return bool(self.backend_model) and target.expect in self.backend_model

    async def snapshot(self) -> StatusSnapshot:
        async with self._cond:
            return StatusSnapshot(
                loaded_key=self.loaded_key,
                loaded_filename=self.loaded_filename,
                backend_model=self.backend_model,
                switching=self.switching,
                inflight=self.inflight,
                last_switch_seconds=self.last_switch_seconds,
                last_switch_at=self.last_switch_at,
                last_switch_reason=self.last_switch_reason,
                last_error=self.last_error,
            )


def _expect_met(expect: str | None, reported: str | None) -> bool:
    if expect is None:
        return True
    return bool(reported) and expect in reported


def _same_path(left: str, right: str) -> bool:
    return normalize_path(left) == normalize_path(right)


def _json_or_none(response: httpx.Response) -> object | None:
    try:
        return response.json()
    except ValueError:
        return None


def _result_string(response: httpx.Response) -> str | None:
    body = _json_or_none(response)
    if isinstance(body, dict) and isinstance(body.get("result"), str):
        return body["result"]
    return None


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
