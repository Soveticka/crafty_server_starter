"""Discord / generic webhook notifications.

Sends rich embed messages to a Discord webhook (or plain JSON POST to any
generic webhook URL) when key server lifecycle events occur:
- Server started (wake-up)
- Server stopped (idle shutdown)
- Server crashed

Uses stdlib http.client + asyncio.to_thread() — no external dependencies.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import ssl
import time
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Discord embed colours
_COLOR_GREEN = 0x2ECC71   # online / started
_COLOR_YELLOW = 0xF1C40F  # stopping / idle
_COLOR_RED = 0xE74C3C     # crashed / error
_COLOR_BLUE = 0x3498DB    # info


class WebhookNotifier:
    """Async webhook notifier for server lifecycle events.

    Parameters
    ----------
    webhook_url:
        The full webhook URL (Discord or generic).
    server_name_label:
        Optional name to display in messages (e.g., "My MC Server").
    """

    def __init__(self, webhook_url: str, server_name_label: str = ""):
        self._url = webhook_url
        self._label = server_name_label
        self._is_discord = "discord.com/api/webhooks" in webhook_url or "discordapp.com/api/webhooks" in webhook_url
        parsed = urlparse(webhook_url)
        self._host = parsed.hostname or ""
        self._port = parsed.port
        self._path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self._scheme = parsed.scheme

    async def notify_started(self, server_name: str, player_name: str = "") -> None:
        """Notify that a server was started (wake-up)."""
        desc = f"🚀 **{server_name}** is starting up!"
        if player_name:
            desc += f"\nTriggered by player **{player_name}**"
        await self._send(
            title="Server Starting",
            description=desc,
            color=_COLOR_GREEN,
            server_name=server_name,
        )

    async def notify_stopped(self, server_name: str, idle_seconds: float = 0) -> None:
        """Notify that a server was stopped (idle shutdown)."""
        desc = f"💤 **{server_name}** was shut down due to inactivity."
        if idle_seconds > 0:
            minutes = int(idle_seconds // 60)
            desc += f"\nIdle for {minutes} minute{'s' if minutes != 1 else ''}"
        await self._send(
            title="Server Stopped",
            description=desc,
            color=_COLOR_YELLOW,
            server_name=server_name,
        )

    async def notify_crashed(self, server_name: str) -> None:
        """Notify that a server crashed."""
        await self._send(
            title="Server Crashed",
            description=f"❌ **{server_name}** has crashed!",
            color=_COLOR_RED,
            server_name=server_name,
        )

    async def _send(self, title: str, description: str, color: int, server_name: str) -> None:
        """Send the notification (Discord embed or generic JSON POST)."""
        if self._is_discord:
            payload = self._build_discord_payload(title, description, color)
        else:
            payload = {
                "event": title.lower().replace(" ", "_"),
                "server": server_name,
                "message": description,
                "timestamp": int(time.time()),
            }

        try:
            await asyncio.to_thread(self._post_json, payload)
            log.info("Webhook sent: %s for '%s'", title, server_name)
        except Exception:
            log.exception("Failed to send webhook notification for '%s'", server_name)

    def _build_discord_payload(self, title: str, description: str, color: int) -> dict[str, Any]:
        embed: dict[str, Any] = {
            "title": title,
            "description": description,
            "color": color,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if self._label:
            embed["footer"] = {"text": self._label}

        return {
            "embeds": [embed],
        }

    def _post_json(self, payload: dict) -> None:
        """Synchronous HTTP POST (called via asyncio.to_thread)."""
        body = json.dumps(payload).encode("utf-8")

        if self._scheme == "https":
            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(self._host, self._port, context=ctx, timeout=10)
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=10)

        try:
            conn.request(
                "POST",
                self._path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
            resp = conn.getresponse()
            if resp.status >= 400:
                resp_body = resp.read().decode("utf-8", errors="replace")[:200]
                log.warning("Webhook returned %d: %s", resp.status, resp_body)
        finally:
            conn.close()
