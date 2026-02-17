"""Async client for the Crafty Controller API v2.

Uses stdlib ``http.client`` wrapped in ``asyncio.to_thread()`` so we
never block the event loop.  No external HTTP library needed.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import ssl
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)


class CraftyApiError(Exception):
    """Raised when the Crafty API returns an unexpected response."""

    def __init__(self, status: int, body: str, url: str):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"Crafty API {status} for {url}: {body[:200]}")


class CraftyApiClient:
    """Thin async wrapper around Crafty API v2.

    Parameters
    ----------
    base_url:
        Full URL including scheme and port, e.g. ``https://localhost:8443``.
    token:
        Long-lived bearer token (API key).
    verify_tls:
        Whether to verify the server TLS certificate.
    """

    def __init__(self, base_url: str, token: str, verify_tls: bool = True):
        parsed = urlparse(base_url)
        self._scheme = parsed.scheme
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or (443 if self._scheme == "https" else 80)
        self._token = token
        self._verify_tls = verify_tls

        # Pre-build an SSL context.
        if self._scheme == "https":
            self._ssl_ctx = ssl.create_default_context()
            if not verify_tls:
                self._ssl_ctx.check_hostname = False
                self._ssl_ctx.verify_mode = ssl.CERT_NONE
        else:
            self._ssl_ctx = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Low-level HTTP (runs in a thread)
    # ------------------------------------------------------------------

    def _request_sync(
        self,
        method: str,
        path: str,
        body: str | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict[str, Any]]:
        """Perform a synchronous HTTP(S) request.  Returns (status, parsed_json)."""
        if self._scheme == "https":
            conn = http.client.HTTPSConnection(
                self._host,
                self._port,
                context=self._ssl_ctx,
                timeout=15,
            )
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=15)

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = content_type

        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
        finally:
            conn.close()

        # The Crafty API always returns JSON for /api/ endpoints.
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw": raw}

        return status, data

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Async wrapper — offloads the sync HTTP call to a thread.

        Raises
        ------
        CraftyApiError
            On HTTP 4xx / 5xx.
        ConnectionError
            If the connection fails entirely.
        """
        body_str = json.dumps(body) if body else None
        log.debug(f"{method} {path}")

        try:
            status, data = await asyncio.to_thread(
                self._request_sync,
                method,
                path,
                body_str,
            )
        except (OSError, http.client.HTTPException) as exc:
            raise ConnectionError(
                f"Crafty API connection failed for {method} {path}: {exc}"
            ) from exc

        if status >= 400:
            raise CraftyApiError(status, json.dumps(data), path)

        log.debug(f"{method} {path} → {status}")
        return data

    # ------------------------------------------------------------------
    # High-level methods
    # ------------------------------------------------------------------

    async def check_health(self) -> bool:
        """``GET /api/v2/crafty/check`` → True if Crafty is alive."""
        try:
            data = await self._request("GET", "/api/v2/crafty/check")
            return data.get("status") == "ok"
        except (ConnectionError, CraftyApiError):
            return False

    async def list_servers(self) -> list[dict[str, Any]]:
        """``GET /api/v2/servers`` → list of server dicts the token has access to."""
        data = await self._request("GET", "/api/v2/servers")
        return data.get("data", [])

    async def get_server_stats(self, server_id: str) -> dict[str, Any]:
        """``GET /api/v2/servers/{serverID}/stats`` → full stats dict.

        Important keys: ``running``, ``online``, ``max``, ``players``,
        ``crashed``, ``waiting_start``, ``server_port``, ``version``,
        ``icon``, ``int_ping_results``.
        """
        data = await self._request("GET", f"/api/v2/servers/{server_id}/stats")
        return data.get("data", data)

    async def start_server(self, server_id: str) -> bool:
        """``POST /api/v2/servers/{serverID}/action/start_server``."""
        log.info(f"API → start_server {server_id}")
        data = await self._request(
            "POST",
            f"/api/v2/servers/{server_id}/action/start_server",
        )
        return data.get("status") == "ok"

    async def stop_server(self, server_id: str) -> bool:
        """``POST /api/v2/servers/{serverID}/action/stop_server``."""
        log.info(f"API → stop_server {server_id}")
        data = await self._request(
            "POST",
            f"/api/v2/servers/{server_id}/action/stop_server",
        )
        return data.get("status") == "ok"

    async def send_stdin(self, server_id: str, command: str) -> bool:
        """``POST /api/v2/servers/{serverID}/stdin`` — send a console command.

        Useful for future enhancements (e.g., broadcasting shutdown warnings).
        """
        log.info(f"API → stdin {server_id}: {command}")
        # The stdin endpoint expects text/plain body.
        body_str = command
        log.debug(f"POST /api/v2/servers/{server_id}/stdin")
        try:
            status, data = await asyncio.to_thread(
                self._request_sync,
                "POST",
                f"/api/v2/servers/{server_id}/stdin",
                body_str,
                "text/plain",
            )
        except (OSError, http.client.HTTPException) as exc:
            raise ConnectionError(f"stdin command failed: {exc}") from exc
        if status >= 400:
            raise CraftyApiError(status, json.dumps(data), f"/api/v2/servers/{server_id}/stdin")
        return data.get("status") == "ok"
