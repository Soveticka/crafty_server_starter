"""Lightweight HTTP health-check, status, and metrics server.

Exposes three endpoints:
- GET /health   → 200 OK (for Docker HEALTHCHECK / Uptime Kuma)
- GET /status   → 200 JSON with per-server state details
- GET /metrics  → 200 Prometheus text exposition format
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from http import HTTPStatus
from typing import Any

from .metrics import generate_metrics
from .server_state import ServerStateMachine

log = logging.getLogger(__name__)


class HealthServer:
    """Minimal async HTTP server using stdlib asyncio streams.

    Parameters
    ----------
    state_machines:
        Mapping of server name → state machine (shared with IdleMonitor).
    host:
        Address to bind on.
    port:
        TCP port for the HTTP server.
    """

    def __init__(
        self,
        state_machines: dict[str, ServerStateMachine],
        host: str = "127.0.0.1",
        port: int = 8095,
    ):
        self._sms = state_machines
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None
        self._start_time = time.monotonic()

    async def run(self, shutdown: asyncio.Event) -> None:
        """Start the server, wait for shutdown, then close."""
        self._start_time = time.monotonic()
        self._server = await asyncio.start_server(
            self._handle_request,
            self._host,
            self._port,
        )
        log.info(f"Health server listening on {self._host}:{self._port}")

        await shutdown.wait()

        self._server.close()
        await self._server.wait_closed()
        log.info("Health server stopped")

    async def _handle_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Parse a minimal HTTP request and route to /health or /status."""
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            if not request_line:
                return

            parts = request_line.decode("utf-8", errors="replace").strip().split()
            if len(parts) < 2:
                self._send_response(writer, HTTPStatus.BAD_REQUEST, "Bad Request")
                return

            method, path = parts[0], parts[1]

            # Drain remaining headers (we don't need them).
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break

            if method != "GET":
                self._send_response(writer, HTTPStatus.METHOD_NOT_ALLOWED, "Method Not Allowed")
            elif path == "/health":
                self._send_response(writer, HTTPStatus.OK, "OK")
            elif path == "/status":
                body = self._build_status_json()
                self._send_json(writer, HTTPStatus.OK, body)
            elif path == "/metrics":
                body = self._build_metrics()
                self._send_plain(writer, HTTPStatus.OK, body)
            else:
                self._send_response(writer, HTTPStatus.NOT_FOUND, "Not Found")

        except (TimeoutError, ConnectionResetError, EOFError):
            pass
        except Exception:
            log.exception("Health server request error")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _build_status_json(self) -> dict[str, Any]:
        uptime = time.monotonic() - self._start_time
        servers: dict[str, Any] = {}
        for name, sm in self._sms.items():
            servers[name] = {
                "state": sm.state.value,
                "port": sm.cfg.listen_port,
                "players_online": sm.last_known_online,
                "players_max": sm.last_known_max,
                "idle_seconds": round(sm.idle_elapsed(), 1) if sm.idle_since else None,
                "crafty_server_id": sm.cfg.crafty_server_id,
            }
        return {
            "status": "ok",
            "uptime_seconds": round(uptime, 1),
            "servers": servers,
        }

    def _build_metrics(self) -> str:
        """Generate Prometheus text exposition payload."""
        uptime = time.monotonic() - self._start_time
        start_counts = {name: sm.start_count for name, sm in self._sms.items()}
        stop_counts = {name: sm.stop_count for name, sm in self._sms.items()}
        return generate_metrics(
            state_machines=self._sms,
            uptime_seconds=uptime,
            start_count=start_counts,
            stop_count=stop_counts,
        )

    @staticmethod
    def _send_response(writer: asyncio.StreamWriter, status: HTTPStatus, body: str) -> None:
        response = (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{body}"
        )
        writer.write(response.encode("utf-8"))

    @staticmethod
    def _send_json(writer: asyncio.StreamWriter, status: HTTPStatus, data: dict) -> None:
        body = json.dumps(data, indent=2)
        response = (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{body}"
        )
        writer.write(response.encode("utf-8"))

    @staticmethod
    def _send_plain(writer: asyncio.StreamWriter, status: HTTPStatus, body: str) -> None:
        encoded = body.encode("utf-8")
        header = (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            f"Content-Type: text/plain; version=0.0.4; charset=utf-8\r\n"
            f"Content-Length: {len(encoded)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        writer.write(header.encode("utf-8") + encoded)
