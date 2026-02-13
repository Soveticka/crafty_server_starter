"""Entry point for Crafty Server Starter.

Run with:  python -m crafty_server_starter [--config /path/to/config.yaml]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from . import __version__
from .config import ConfigError, load_config
from .logger import setup_logging

log = logging.getLogger("crafty_server_starter")

DEFAULT_CONFIG_PATH = "/etc/crafty-server-starter/config.yaml"

# Will be set by main() so signal handlers can request shutdown.
_shutdown_event: asyncio.Event | None = None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="crafty-server-starter",
        description="Auto-hibernate and wake Minecraft servers via Crafty API v2.",
    )
    parser.add_argument(
        "-c", "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to YAML config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args(argv)


async def _run(config_path: str) -> None:
    """Main async entry point — load config, build components, run the event loop."""
    global _shutdown_event

    # -- Load config ----------------------------------------------------------
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        log.critical("Configuration error: %s", exc)
        sys.exit(1)

    setup_logging(cfg.logging)
    log.info("Crafty Server Starter v%s starting", __version__)
    log.info("Managing %d server(s)", len(cfg.servers))

    # -- Shutdown event -------------------------------------------------------
    _shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _request_shutdown, sig)
    else:
        # Windows: add_signal_handler is not supported.
        # KeyboardInterrupt (Ctrl+C) is caught in main() instead.
        pass

    # -- Build components (imported here to avoid circular imports) -----------
    from .crafty_api import CraftyApiClient
    from .idle_monitor import IdleMonitor
    from .proxy_listener import ProxyManager
    from .webhook import WebhookNotifier

    api = CraftyApiClient(
        base_url=cfg.crafty.base_url,
        token=cfg.crafty.api_token,
        verify_tls=cfg.crafty.verify_tls,
    )

    # Validate connectivity and server mappings.
    if not await api.check_health():
        log.critical("Cannot reach Crafty API at %s — aborting", cfg.crafty.base_url)
        sys.exit(1)

    log.info("Crafty API reachable at %s", cfg.crafty.base_url)

    # Build per-server state machines.
    from .server_state import ServerStateMachine

    state_machines: dict[str, ServerStateMachine] = {}
    for name, srv_cfg in cfg.servers.items():
        state_machines[name] = ServerStateMachine(cfg=srv_cfg, cooldowns=cfg.cooldowns)

    # Validate server IDs against Crafty.
    known_servers = await api.list_servers()
    known_ids = {s["server_id"] for s in known_servers}
    for name, sm in state_machines.items():
        if sm.cfg.crafty_server_id not in known_ids:
            log.error(
                "Server '%s': crafty_server_id '%s' not found in Crafty. Skipping.",
                name, sm.cfg.crafty_server_id,
            )

    # -- Webhook (optional) ---------------------------------------------------
    webhook: WebhookNotifier | None = None
    if cfg.webhook.enabled:
        webhook = WebhookNotifier(
            webhook_url=cfg.webhook.url,
            server_name_label=cfg.webhook.label,
        )
        log.info("Webhook notifications enabled")

    proxy_mgr = ProxyManager(state_machines=state_machines, crafty_api=api, webhook=webhook)
    idle_mon = IdleMonitor(
        state_machines=state_machines,
        crafty_api=api,
        proxy_manager=proxy_mgr,
        polling_cfg=cfg.polling,
        cooldown_cfg=cfg.cooldowns,
        webhook=webhook,
    )

    # -- Run ------------------------------------------------------------------
    log.info("Starting idle monitor and proxy manager…")
    try:
        await asyncio.gather(
            idle_mon.run(_shutdown_event),
            proxy_mgr.run(_shutdown_event),
        )
    except asyncio.CancelledError:
        pass

    log.info("Shutdown complete.")


def _request_shutdown(sig: signal.Signals) -> None:
    """Signal handler — set the shutdown event."""
    log.info("Received %s, shutting down…", sig.name)
    if _shutdown_event is not None:
        _shutdown_event.set()


def main() -> None:
    """Synchronous wrapper that sets up minimal logging, then runs the async loop."""
    # Minimal logging before config is loaded so early errors are visible.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        stream=sys.stderr,
    )
    args = _parse_args()

    try:
        asyncio.run(_run(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
