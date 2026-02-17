"""Async TCP proxy listeners for hibernating Minecraft servers.

For every managed server that is in the STOPPED / STARTING / CRASHED
state, a lightweight TCP server binds to the configured port and handles
the Minecraft protocol just enough to:
- Answer Server List Pings with a custom MOTD.
- On Login attempts, send a kick message and trigger a server start.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .crafty_api import CraftyApiClient
from .mc_protocol import (
    Handshake,
    LoginStart,
    build_disconnect,
    build_pong,
    build_status_response,
    read_packet,
)
from .server_state import ServerStateMachine, State
from .webhook import WebhookNotifier

log = logging.getLogger(__name__)


class ProxyManager:
    """Manages per-port asyncio TCP servers for hibernating MC servers.

    The idle monitor calls :meth:`ensure_listeners` after each poll to
    start / stop listeners as the server states change.
    """

    def __init__(
        self,
        state_machines: dict[str, ServerStateMachine],
        crafty_api: CraftyApiClient,
        webhook: WebhookNotifier | None = None,
    ):
        self._sms = state_machines
        self._api = crafty_api
        self._webhook = webhook
        # name → running asyncio.Server (or None)
        self._listeners: dict[str, asyncio.Server | None] = {name: None for name in state_machines}
        # Servers where we triggered a start — NEVER re-bind proxy for these
        # until they go back to STOPPED or CRASHED.
        self._start_lockout: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self, shutdown: asyncio.Event) -> None:
        """Block until the shutdown event is set, then close all listeners."""
        await shutdown.wait()
        await self.stop_all()

    async def ensure_listeners(self) -> None:
        """Start or stop listeners to match the current server states."""
        for name, sm in self._sms.items():
            # If we triggered a start, NEVER re-bind until server is back to
            # STOPPED or CRASHED.
            if name in self._start_lockout:
                if sm.state in (State.STOPPED, State.CRASHED):
                    # Server went back to stopped — clear lockout, allow proxy.
                    self._start_lockout.discard(name)
                    log.info(f"Start lockout cleared for '{name}' (state={sm.state.value})")
                else:
                    # Still starting/online — keep port free.
                    continue

            if sm.is_proxy_needed:
                await self._start_listener(name)
            else:
                await self._stop_listener(name)

    async def stop_all(self) -> None:
        """Shut down every active listener."""
        for name in list(self._listeners):
            await self._stop_listener(name)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _start_listener(self, name: str) -> None:
        """Bind the proxy listener for *name* if it isn't already running."""
        if self._listeners[name] is not None:
            return  # already listening

        sm = self._sms[name]

        async def _client_cb(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._handle_client(name, reader, writer)

        for attempt in range(15):  # retry binding for up to 30s
            try:
                server = await asyncio.start_server(
                    _client_cb,
                    host=sm.cfg.listen_host,
                    port=sm.cfg.listen_port,
                )
                self._listeners[name] = server
                log.info(
                    f"Proxy listener started on {sm.cfg.listen_host}:{sm.cfg.listen_port} for server '{name}'",
                )
                return
            except OSError as exc:
                if attempt < 14:
                    log.debug(
                        f"Port {sm.cfg.listen_port} not free yet (attempt {attempt + 1}): {exc}",
                    )
                    await asyncio.sleep(2)
                else:
                    log.error(
                        f"Cannot bind to port {sm.cfg.listen_port} for server '{name}' after 30s: {exc}",
                    )

    async def _stop_listener(self, name: str) -> None:
        """Close the proxy listener for *name* if it is running."""
        server = self._listeners.get(name)
        if server is None:
            return
        server.close()
        await server.wait_closed()
        self._listeners[name] = None
        sm = self._sms[name]
        log.info(
            f"Proxy listener stopped on port {sm.cfg.listen_port} for server '{name}'",
        )

    async def _handle_client(
        self,
        name: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single incoming MC client connection."""
        sm = self._sms[name]
        peer = writer.get_extra_info("peername", ("?", 0))
        try:
            # 1) Read Handshake (packet id 0x00 in handshake state)
            pkt_id, stream = await asyncio.wait_for(read_packet(reader), timeout=10)
            if pkt_id != 0x00:
                return
            handshake = Handshake.parse(stream)

            if handshake.next_state == 1:
                # ── Status (Server List Ping) ────────────────────────
                await self._handle_status(sm, reader, writer)

            elif handshake.next_state == 2:
                # ── Login ────────────────────────────────────────────
                await self._handle_login(name, sm, reader, writer, peer)

        except (EOFError, TimeoutError, asyncio.IncompleteReadError):
            # Client disconnected or timed out — ignore silently.
            pass
        except Exception:
            log.exception(f"Error handling client from {peer} on port {sm.cfg.listen_port}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_status(
        self,
        sm: ServerStateMachine,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle Server List Ping: send fake MOTD, answer Ping with Pong."""
        # Read Status Request (packet 0x00, empty payload)
        await asyncio.wait_for(read_packet(reader), timeout=5)

        resp = build_status_response(
            motd=sm.cfg.motd_hibernating,
            version_name="Hibernating",
            protocol=-1,
            max_players=sm.last_known_max,
            online_players=0,
            favicon=sm.last_known_icon if sm.last_known_icon else "",
        )
        writer.write(resp)
        await writer.drain()

        # Read Ping → send Pong
        try:
            pkt_id, stream = await asyncio.wait_for(read_packet(reader), timeout=5)
            if pkt_id == 0x01:
                payload_long = stream.read(8)
                writer.write(build_pong(payload_long))
                await writer.drain()
        except (EOFError, TimeoutError, asyncio.IncompleteReadError):
            pass

    async def _handle_login(
        self,
        name: str,
        sm: ServerStateMachine,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: Any,
    ) -> None:
        """Handle Login Start: kick the player, release the port, then trigger a server start."""
        # Read Login Start (packet 0x00 in login state)
        pkt_id, stream = await asyncio.wait_for(read_packet(reader), timeout=5)
        if pkt_id != 0x00:
            return
        login = LoginStart.parse(stream)

        log.info(
            f"Wake-up trigger from player '{login.player_name}' ({peer[0]}) on port {sm.cfg.listen_port} (server '{name}')",
        )

        # Send Disconnect (kick) message
        writer.write(build_disconnect(sm.cfg.kick_message))
        await writer.drain()

        # Close this client connection immediately so the port isn't held.
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

        # Trigger server start if not already starting
        if sm.state in (State.STOPPED, State.CRASHED):
            # ── CRITICAL: release the port BEFORE asking Crafty to start ──
            # Stop the proxy listener so the MC server can bind to the port.
            await self._stop_listener(name)

            # Lock out this server from ensure_listeners re-binding.
            self._start_lockout.add(name)

            # Give the OS a moment to fully release the socket.
            await asyncio.sleep(5)

            try:
                await self._api.start_server(sm.cfg.crafty_server_id)
                sm.transition(State.STARTING)
                log.info(
                    f"Port {sm.cfg.listen_port} released and start_server sent for '{name}' (lockout active)",
                )
                if self._webhook:
                    self._start_notify_task = asyncio.ensure_future(
                        self._webhook.notify_started(name, player_name=login.player_name)
                    )
            except Exception:
                log.exception(f"Failed to start server '{name}' via Crafty API")
                # Clear lockout and re-bind the proxy so players can still see the MOTD.
                self._start_lockout.discard(name)
                await self._start_listener(name)
