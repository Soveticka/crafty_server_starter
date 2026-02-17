"""Prometheus-compatible metrics collector.

Generates metrics in Prometheus text exposition format (text/plain;
version=0.0.4) from the shared ServerStateMachine instances. No external
dependencies — everything is built from format strings.

Designed to be served by the HealthServer on ``GET /metrics``.
"""

from __future__ import annotations

from .server_state import ServerStateMachine

# Prefix for all metrics
_NS = "crafty_watcher"


def _gauge(name: str, help_text: str, labels: dict[str, str], value: float | int) -> str:
    """Format a single Prometheus gauge sample."""
    label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
    return f"{name}{{{label_str}}} {value}"


def generate_metrics(
    state_machines: dict[str, ServerStateMachine],
    uptime_seconds: float,
    start_count: dict[str, int],
    stop_count: dict[str, int],
) -> str:
    """Return a complete Prometheus text exposition payload."""
    lines: list[str] = []

    # ── Uptime ──────────────────────────────────────────────────
    lines.append(f"# HELP {_NS}_uptime_seconds Time since the service started")
    lines.append(f"# TYPE {_NS}_uptime_seconds gauge")
    lines.append(f"{_NS}_uptime_seconds {uptime_seconds:.1f}")
    lines.append("")

    # ── Per-server metrics ──────────────────────────────────────
    lines.append(f"# HELP {_NS}_server_state Current server state (1=active)")
    lines.append(f"# TYPE {_NS}_server_state gauge")
    for name, sm in state_machines.items():
        labels = {"server": name, "state": sm.state.value}
        lines.append(_gauge(f"{_NS}_server_state", "", labels, 1))
    lines.append("")

    lines.append(f"# HELP {_NS}_players_online Current online player count")
    lines.append(f"# TYPE {_NS}_players_online gauge")
    for name, sm in state_machines.items():
        lines.append(_gauge(f"{_NS}_players_online", "", {"server": name}, sm.last_known_online))
    lines.append("")

    lines.append(f"# HELP {_NS}_players_max Max player slots")
    lines.append(f"# TYPE {_NS}_players_max gauge")
    for name, sm in state_machines.items():
        lines.append(_gauge(f"{_NS}_players_max", "", {"server": name}, sm.last_known_max))
    lines.append("")

    lines.append(f"# HELP {_NS}_idle_seconds Seconds the server has been idle (0 if not idle)")
    lines.append(f"# TYPE {_NS}_idle_seconds gauge")
    for name, sm in state_machines.items():
        idle = round(sm.idle_elapsed(), 1) if sm.idle_since else 0
        lines.append(_gauge(f"{_NS}_idle_seconds", "", {"server": name}, idle))
    lines.append("")

    lines.append(f"# HELP {_NS}_starts_total Total times this server was started")
    lines.append(f"# TYPE {_NS}_starts_total counter")
    for name in state_machines:
        lines.append(_gauge(f"{_NS}_starts_total", "", {"server": name}, start_count.get(name, 0)))
    lines.append("")

    lines.append(f"# HELP {_NS}_stops_total Total times this server was stopped")
    lines.append(f"# TYPE {_NS}_stops_total counter")
    for name in state_machines:
        lines.append(_gauge(f"{_NS}_stops_total", "", {"server": name}, stop_count.get(name, 0)))
    lines.append("")

    return "\n".join(lines) + "\n"
