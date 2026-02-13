"""Async UDP proxy listener for hibernating Minecraft Bedrock servers.

Handles the RakNet "unconnected" protocol layer:
- Unconnected Ping → responds with Unconnected Pong (custom MOTD)
- Open Connection Request 1 → triggers server start via Crafty API,
  then responds with Incompatible Protocol to reject the connection
  gracefully while the real server is starting.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random

from .bedrock_protocol import (
    build_incompatible_protocol,
    build_unconnected_pong,
    is_open_connection_request_1,
    parse_unconnected_ping,
)
from .crafty_api import CraftyApiClient
from .server_state import ServerStateMachine, State

log = logging.getLogger(__name__)


class BedrockProxyProtocol(asyncio.DatagramProtocol):
    """UDP protocol handler for a single Bedrock server proxy."""

    def __init__(
        self,
        name: str,
        sm: ServerStateMachine,
        crafty_api: CraftyApiClient,
        manager: BedrockProxyManager,
    ):
        self._name = name
        self._sm = sm
        self._api = crafty_api
        self._manager = manager
        self._server_guid = random.getrandbits(63)
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle incoming UDP datagrams."""
        if not data:
            return

        # Unconnected Ping — respond with MOTD pong
        parsed = parse_unconnected_ping(data)
        if parsed is not None:
            client_time, _ = parsed

            # Strip Minecraft formatting codes for Bedrock MOTD
            motd = self._sm.cfg.motd_hibernating
            # Remove § color codes (Bedrock uses § too but simpler text is safer)
            clean_motd = ""
            skip_next = False
            for ch in motd:
                if ch == "§":
                    skip_next = True
                    continue
                if skip_next:
                    skip_next = False
                    continue
                clean_motd += ch

            pong = build_unconnected_pong(
                client_time=client_time,
                server_guid=self._server_guid,
                motd=clean_motd or "Server hibernating",
                players_online=0,
                max_players=self._sm.last_known_max,
                port_v4=self._sm.cfg.listen_port,
            )
            self._transport.sendto(pong, addr)
            return

        # Open Connection Request 1 — someone is trying to connect
        if is_open_connection_request_1(data):
            log.info(
                "Bedrock connection attempt on port %d from %s — triggering wake-up for '%s'",
                self._sm.cfg.listen_port,
                addr[0],
                self._name,
            )

            # Reject with Incompatible Protocol (graceful rejection)
            reject = build_incompatible_protocol(self._server_guid)
            self._transport.sendto(reject, addr)

            # Trigger server start
            if self._sm.state in (State.STOPPED, State.CRASHED):
                self._start_task = asyncio.ensure_future(self._manager.trigger_start(self._name))
            return

    def error_received(self, exc: Exception) -> None:
        log.warning("Bedrock proxy UDP error on '%s': %s", self._name, exc)

    def connection_lost(self, exc: Exception | None) -> None:
        pass


class BedrockProxyManager:
    """Manages UDP proxy listeners for Bedrock servers.

    Mirrors the ProxyManager API but uses UDP (DatagramProtocol) instead
    of TCP (start_server).
    """

    def __init__(
        self,
        state_machines: dict[str, ServerStateMachine],
        crafty_api: CraftyApiClient,
    ):
        self._sms = state_machines
        self._api = crafty_api
        self._transports: dict[str, asyncio.DatagramTransport | None] = {
            name: None for name in state_machines
        }
        self._start_lockout: set[str] = set()

    async def run(self, shutdown: asyncio.Event) -> None:
        """Run the Bedrock proxy manager until shutdown."""
        log.info("Bedrock proxy manager starting (%d servers)", len(self._sms))
        while not shutdown.is_set():
            await self.ensure_listeners()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(shutdown.wait(), timeout=5)

        # Cleanup
        for name in list(self._transports):
            await self._stop_listener(name)
        log.info("Bedrock proxy manager stopped")

    async def ensure_listeners(self) -> None:
        """Start/stop UDP listeners based on server state."""
        for name, sm in self._sms.items():
            # Respect start lockout
            if name in self._start_lockout:
                if sm.state in (State.STOPPED, State.CRASHED):
                    self._start_lockout.discard(name)
                    log.info(
                        "Bedrock start lockout cleared for '%s' (state=%s)", name, sm.state.value
                    )
                else:
                    continue

            if sm.is_proxy_needed and self._transports.get(name) is None:
                await self._start_listener(name)
            elif not sm.is_proxy_needed and self._transports.get(name) is not None:
                await self._stop_listener(name)

    async def _start_listener(self, name: str) -> None:
        """Bind a UDP listener for the given server."""
        sm = self._sms[name]
        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: BedrockProxyProtocol(name, sm, self._api, self),
                local_addr=(sm.cfg.listen_host, sm.cfg.listen_port),
            )
            self._transports[name] = transport
            log.info(
                "Bedrock proxy listening on %s:%d for '%s'",
                sm.cfg.listen_host,
                sm.cfg.listen_port,
                name,
            )
        except OSError as exc:
            log.error(
                "Cannot bind Bedrock proxy on %s:%d for '%s': %s",
                sm.cfg.listen_host,
                sm.cfg.listen_port,
                name,
                exc,
            )

    async def _stop_listener(self, name: str) -> None:
        """Close the UDP listener for the given server."""
        transport = self._transports.get(name)
        if transport is not None:
            transport.close()
            self._transports[name] = None
            log.info(
                "Bedrock proxy stopped for '%s' (port %d)",
                name,
                self._sms[name].cfg.listen_port,
            )

    async def trigger_start(self, name: str) -> None:
        """Start a Bedrock server via Crafty API."""
        sm = self._sms.get(name)
        if sm is None:
            return

        if name in self._start_lockout:
            return

        # Stop the UDP listener to free the port
        await self._stop_listener(name)

        # Lock out re-binding
        self._start_lockout.add(name)

        # Give OS time to release the port
        await asyncio.sleep(5)

        try:
            await self._api.start_server(sm.cfg.crafty_server_id)
            sm.transition(State.STARTING)
            log.info(
                "Bedrock port %d released and start_server sent for '%s' (lockout active)",
                sm.cfg.listen_port,
                name,
            )
        except Exception:
            log.exception("Failed to start Bedrock server '%s' via Crafty API", name)
            self._start_lockout.discard(name)
            await self._start_listener(name)
