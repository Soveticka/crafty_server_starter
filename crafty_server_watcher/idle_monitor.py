"""Idle-shutdown monitor for Crafty-managed Minecraft servers.

Periodically polls the Crafty API for server stats, drives the per-server
state machine, and orchestrates stop/start actions and proxy lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import CooldownConfig, PollingConfig
from .crafty_api import CraftyApiClient, CraftyApiError
from .proxy_listener import ProxyManager
from .server_state import ServerStateMachine, State
from .webhook import WebhookNotifier

log = logging.getLogger(__name__)


class IdleMonitor:
    """Async polling loop that checks Crafty server stats and enforces
    idle-shutdown / auto-start logic.

    Parameters
    ----------
    state_machines:
        Mapping of server name → state machine.
    crafty_api:
        Client for the Crafty Controller API.
    proxy_manager:
        Controls per-port proxy listeners.
    polling_cfg:
        Polling interval and retry settings.
    cooldown_cfg:
        Anti-flap / hysteresis settings.
    """

    def __init__(
        self,
        state_machines: dict[str, ServerStateMachine],
        crafty_api: CraftyApiClient,
        proxy_manager: ProxyManager,
        polling_cfg: PollingConfig,
        cooldown_cfg: CooldownConfig,
        webhook: WebhookNotifier | None = None,
    ):
        self._sms = state_machines
        self._api = crafty_api
        self._proxy = proxy_manager
        self._poll_cfg = polling_cfg
        self._cd_cfg = cooldown_cfg
        self._webhook = webhook
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, shutdown: asyncio.Event) -> None:
        """Run the polling loop until *shutdown* is set."""
        log.info(f"Idle monitor started (poll every {self._poll_cfg.interval_seconds}s)")

        # Initial state discovery
        await self._poll_all()
        await self._proxy.ensure_listeners()

        while not shutdown.is_set():
            try:
                await asyncio.wait_for(
                    shutdown.wait(),
                    timeout=self._poll_cfg.interval_seconds,
                )
                break  # shutdown requested
            except TimeoutError:
                pass  # normal: timeout = time to poll

            await self._poll_all()
            await self._proxy.ensure_listeners()

        log.info("Idle monitor stopped")

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _poll_all(self) -> None:
        """Poll stats for every managed server and process transitions."""
        for name, sm in self._sms.items():
            try:
                await self._poll_one(name, sm)
                self._consecutive_failures = 0
            except ConnectionError as exc:
                self._consecutive_failures += 1
                log.warning(
                    f"Crafty API unreachable (attempt {self._consecutive_failures}/{self._poll_cfg.api_max_retries}): {exc}",
                )
                if self._consecutive_failures >= self._poll_cfg.api_max_retries:
                    log.error(
                        f"Crafty API unreachable after {self._consecutive_failures} attempts — "
                        "holding current state, will keep retrying.",
                    )
                await asyncio.sleep(self._poll_cfg.api_retry_delay_seconds)
            except CraftyApiError as exc:
                if exc.status == 403:
                    log.critical(
                        f"Crafty API returned 403 for server '{name}' — "
                        "token may be invalid. Skipping all API calls until fixed.",
                    )
                    # Stop polling — manual intervention needed.
                    return
                log.error(f"Crafty API error for server '{name}': {exc}")
            except Exception:
                log.exception(f"Unexpected error polling server '{name}'")

    async def _poll_one(self, name: str, sm: ServerStateMachine) -> None:
        """Fetch stats for a single server and drive its state machine."""
        stats = await self._api.get_server_stats(sm.cfg.crafty_server_id)
        sm.update_from_stats(stats)

        running: bool = bool(stats.get("running", False))
        crashed: bool = bool(stats.get("crashed", False))
        online: int = int(stats.get("online", 0))
        int_ping: str = str(stats.get("int_ping_results", ""))

        log.debug(
            f"Poll '{name}': state={sm.state.value} running={running} online={online} crashed={crashed} int_ping={int_ping}",
        )

        # ── Determine desired state ─────────────────────────────────
        if crashed:
            if sm.state != State.CRASHED:
                sm.transition(State.CRASHED)
                if self._webhook:
                    self._crash_notify_task = asyncio.ensure_future(
                        self._webhook.notify_crashed(name)
                    )
            return

        if not running:
            if sm.state == State.STARTING:
                # Still waiting — check if we've exceeded the timeout.
                if sm.last_start_time and (
                    time.monotonic() - sm.last_start_time > sm.cfg.start_timeout_seconds
                ):
                    log.error(
                        f"Server '{name}': start timed out after {sm.cfg.start_timeout_seconds}s — giving up.",
                    )
                    sm.transition(State.STOPPED)
                # else: still starting, keep waiting.
                return

            if sm.state not in (State.STOPPED, State.CRASHED):
                sm.transition(State.STOPPED)
            return

        # Server is running.
        if sm.state == State.STARTING:
            # Check if the server is truly ready (internal ping succeeds).
            if int_ping == "True":
                sm.transition(State.ONLINE)
            # else: running but not yet accepting connections — stay STARTING.
            return

        if sm.state in (State.STOPPED, State.STARTING, State.CRASHED, State.UNKNOWN):
            # Server came online (possibly started externally).
            if online > 0:
                sm.transition(State.ONLINE)
            else:
                sm.transition(State.IDLE)
            return

        if sm.state == State.STOPPING:
            # We asked it to stop, but it's still running — keep waiting.
            return

        # ── Handle ONLINE / IDLE ────────────────────────────────────
        if online > 0:
            if sm.state != State.ONLINE:
                sm.transition(State.ONLINE)
            return

        # online == 0
        if sm.state == State.ONLINE:
            sm.transition(State.IDLE)
            return

        if sm.state == State.IDLE:
            await self._check_idle_shutdown(name, sm)
            return

    # ------------------------------------------------------------------
    # Idle shutdown logic
    # ------------------------------------------------------------------

    async def _check_idle_shutdown(self, name: str, sm: ServerStateMachine) -> None:
        """Evaluate whether an idle server should be shut down."""
        # Don't start counting idle time during the start-grace period.
        if sm.in_start_grace():
            remaining = sm.cooldowns.start_grace_minutes * 60 - (
                time.monotonic() - (sm.last_start_time or 0)
            )
            log.info(
                f"Server '{name}': in start-grace period ({remaining:.0f}s remaining), idle check paused.",
            )
            return

        # Don't stop again during the stop-cooldown period.
        if sm.in_stop_cooldown():
            remaining = sm.cooldowns.stop_cooldown_minutes * 60 - (
                time.monotonic() - (sm.last_stop_time or 0)
            )
            log.info(
                f"Server '{name}': in stop-cooldown ({remaining:.0f}s remaining), idle check paused.",
            )
            return

        # Flap guard.
        if sm.is_flapping():
            log.warning(
                f"Server '{name}': flap guard active — too many start/stop cycles "
                f"in the last {self._cd_cfg.flap_window_minutes} minutes. Waiting {self._cd_cfg.flap_backoff_minutes} minutes before next stop.",
            )
            return

        if not sm.idle_timeout_reached():
            elapsed = sm.idle_elapsed()
            remaining = sm.cfg.idle_timeout_minutes * 60 - elapsed
            log.info(
                f"Server '{name}': idle for {elapsed:.0f}s / {sm.cfg.idle_timeout_minutes * 60}s, shutdown in {remaining:.0f}s.",
            )
            return

        # ── Trigger shutdown ────────────────────────────────────────
        log.info(
            f"Server '{name}' (port {sm.cfg.listen_port}): idle for {sm.idle_elapsed():.0f}s — triggering shutdown.",
        )
        sm.transition(State.STOPPING)
        try:
            await self._api.stop_server(sm.cfg.crafty_server_id)
            if self._webhook:
                await self._webhook.notify_stopped(name, idle_seconds=sm.idle_elapsed())
        except Exception:
            log.exception(f"Failed to stop server '{name}' via Crafty API")
            # Revert to IDLE so we retry on the next poll.
            sm.transition(State.ONLINE)  # STOPPING → … can't revert cleanly
            # The next poll will detect running=true and re-evaluate.
