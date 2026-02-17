"""Per-server state tracking and transitions for Crafty Server Watcher."""

from __future__ import annotations

import enum
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from .config import CooldownConfig, ServerConfig

log = logging.getLogger(__name__)


class State(enum.Enum):
    """Server lifecycle states."""

    UNKNOWN = "UNKNOWN"
    ONLINE = "ONLINE"
    IDLE = "IDLE"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    CRASHED = "CRASHED"


# Allowed transitions: from_state → {set of valid to_states}
_VALID_TRANSITIONS: dict[State, set[State]] = {
    State.UNKNOWN: {State.ONLINE, State.IDLE, State.STOPPED, State.CRASHED},
    State.ONLINE: {State.IDLE, State.STOPPED, State.CRASHED},
    State.IDLE: {State.ONLINE, State.STOPPING, State.STOPPED, State.CRASHED},
    State.STOPPING: {State.STOPPED, State.CRASHED},
    State.STOPPED: {State.STARTING, State.ONLINE},
    State.STARTING: {State.ONLINE, State.STOPPED, State.CRASHED},
    State.CRASHED: {State.STOPPED, State.ONLINE},
}


@dataclass
class ServerStateMachine:
    """Tracks the runtime state and timing of a single managed server.

    Attributes
    ----------
    cfg:
        Per-server config (ports, timeouts, MOTD strings).
    cooldowns:
        Global cooldown / anti-flap settings.
    state:
        Current lifecycle state.
    idle_since:
        Timestamp when the player count first dropped to 0 (or None).
    last_stop_time:
        Timestamp of the most recent stop action.
    last_start_time:
        Timestamp of the most recent start action.
    start_stop_history:
        Recent start/stop timestamps for flap detection.
    last_known_online:
        Last observed player count.
    last_known_max:
        Last observed max players.
    last_known_version:
        Last observed MC version string.
    last_known_icon:
        Last observed server icon (base64).
    """

    cfg: ServerConfig
    cooldowns: CooldownConfig
    state: State = State.UNKNOWN
    idle_since: float | None = None
    last_stop_time: float | None = None
    last_start_time: float | None = None
    start_stop_history: deque[float] = field(default_factory=lambda: deque(maxlen=20))
    start_count: int = 0
    stop_count: int = 0
    last_known_online: int = 0
    last_known_max: int = 20
    last_known_version: str = ""
    last_known_icon: str = ""

    # -- Transitions ----------------------------------------------------------

    def transition(self, new_state: State) -> None:
        """Transition to *new_state*, enforcing the valid-transition graph.

        Also updates bookkeeping timestamps where applicable.
        """
        if new_state == self.state:
            return  # no-op for self-transitions

        valid = _VALID_TRANSITIONS.get(self.state, set())
        if new_state not in valid:
            log.warning(
                f"Server '{self.cfg.name}': invalid transition {self.state.value} → {new_state.value} (ignored)",
            )
            return

        old = self.state
        self.state = new_state
        now = time.monotonic()

        if new_state == State.IDLE:
            self.idle_since = now
        elif new_state == State.STOPPING:
            self.idle_since = None
        elif new_state == State.STOPPED:
            self.idle_since = None
            self.last_stop_time = now
            self.stop_count += 1
            self.start_stop_history.append(now)
        elif new_state == State.STARTING:
            self.last_start_time = now
            self.start_count += 1
            self.start_stop_history.append(now)
        elif new_state == State.ONLINE:
            self.idle_since = None

        log.info(
            f"Server '{self.cfg.name}' (port {self.cfg.listen_port}): {old.value} → {new_state.value}",
        )

    # -- Timing queries -------------------------------------------------------

    def idle_elapsed(self) -> float:
        """Seconds since the server became idle, or 0 if not idle."""
        if self.idle_since is None:
            return 0.0
        return time.monotonic() - self.idle_since

    def idle_timeout_reached(self) -> bool:
        """True if the server has been idle long enough to trigger a shutdown."""
        return self.idle_elapsed() >= self.cfg.idle_timeout_minutes * 60

    def in_start_grace(self) -> bool:
        """True if the start-grace period has not yet elapsed."""
        if self.last_start_time is None:
            return False
        return (time.monotonic() - self.last_start_time) < self.cooldowns.start_grace_minutes * 60

    def in_stop_cooldown(self) -> bool:
        """True if the stop-cooldown period has not yet elapsed."""
        if self.last_stop_time is None:
            return False
        return (time.monotonic() - self.last_stop_time) < self.cooldowns.stop_cooldown_minutes * 60

    def is_flapping(self) -> bool:
        """True if the server has cycled start/stop too many times recently."""
        window = self.cooldowns.flap_window_minutes * 60
        cutoff = time.monotonic() - window
        recent = sum(1 for ts in self.start_stop_history if ts > cutoff)
        return recent >= self.cooldowns.flap_max_cycles * 2  # each cycle = 1 start + 1 stop

    # -- Convenience ----------------------------------------------------------

    @property
    def is_proxy_needed(self) -> bool:
        """True if the proxy listener should be active for this server.

        Note: STARTING is excluded because the real MC server needs the
        port during startup.  The proxy re-binds after the server stops
        or if the start times out.
        """
        return self.state in (State.STOPPED, State.CRASHED)

    def update_from_stats(self, stats: dict) -> None:
        """Update cached fields from a Crafty stats API response.

        This does **not** trigger state transitions — the idle monitor
        is responsible for that logic.
        """
        self.last_known_online = int(stats.get("online", 0))
        self.last_known_max = int(stats.get("max", 20))
        self.last_known_version = str(stats.get("version", ""))
        self.last_known_icon = str(stats.get("icon", ""))
