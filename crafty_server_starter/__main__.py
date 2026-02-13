"""Entry point for Crafty Server Starter.

Run with:  python -m crafty_server_starter [--config /path/to/config.yaml]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
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
_reload_event: asyncio.Event | None = None
_config_path: str = DEFAULT_CONFIG_PATH


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="crafty-server-starter",
        description="Auto-hibernate and wake Minecraft servers via Crafty API v2.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to YAML config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args(argv)


async def _run(config_path: str) -> None:
    """Main async entry point — load config, build components, run the event loop."""
    global _shutdown_event, _reload_event, _config_path

    _config_path = config_path

    # -- Load config ----------------------------------------------------------
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        log.critical("Configuration error: %s", exc)
        sys.exit(1)

    setup_logging(cfg.logging)
    log.info("Crafty Server Starter v%s starting", __version__)
    log.info("Managing %d server(s)", len(cfg.servers))

    # -- Events ---------------------------------------------------------------
    _shutdown_event = asyncio.Event()
    _reload_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _request_shutdown, sig)
        loop.add_signal_handler(signal.SIGHUP, _request_reload)
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
                name,
                sm.cfg.crafty_server_id,
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

    # -- Reload watcher -------------------------------------------------------
    async def _reload_watcher() -> None:
        """Watch for SIGHUP reload events and apply config changes."""
        while not _shutdown_event.is_set():
            _reload_event.clear()
            # Wait for either a reload signal or shutdown.
            reload_task = asyncio.create_task(_reload_event.wait())
            shutdown_task = asyncio.create_task(_shutdown_event.wait())
            _done, pending = await asyncio.wait(
                {reload_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()

            if _shutdown_event.is_set():
                break

            # SIGHUP received — reload config.
            log.info("Reloading configuration from %s", _config_path)
            try:
                new_cfg = load_config(_config_path)
            except ConfigError as exc:
                log.error("Config reload failed — keeping current config: %s", exc)
                continue

            # Apply per-server config changes (timeouts, MOTDs, kick message).
            for name, sm in state_machines.items():
                if name in new_cfg.servers:
                    new_srv = new_cfg.servers[name]
                    sm.cfg.idle_timeout_minutes = new_srv.idle_timeout_minutes
                    sm.cfg.start_timeout_seconds = new_srv.start_timeout_seconds
                    sm.cfg.motd_hibernating = new_srv.motd_hibernating
                    sm.cfg.kick_message = new_srv.kick_message
                    log.info(
                        "Server '%s': config updated (idle=%dm, motd='%s')",
                        name,
                        new_srv.idle_timeout_minutes,
                        new_srv.motd_hibernating,
                    )

            # Apply cooldown changes.
            for _name, sm in state_machines.items():
                sm.cooldowns = new_cfg.cooldowns

            # Apply polling interval.
            idle_mon._poll_cfg = new_cfg.polling

            log.info("Configuration reloaded successfully.")

    # -- Run ------------------------------------------------------------------
    log.info("Starting idle monitor and proxy manager…")
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(
            idle_mon.run(_shutdown_event),
            proxy_mgr.run(_shutdown_event),
            _reload_watcher(),
        )

    log.info("Shutdown complete.")


def _request_shutdown(sig: signal.Signals) -> None:
    """Signal handler — set the shutdown event."""
    log.info("Received %s, shutting down…", sig.name)
    if _shutdown_event is not None:
        _shutdown_event.set()


def _request_reload() -> None:
    """Signal handler — set the reload event."""
    log.info("Received SIGHUP, scheduling config reload…")
    if _reload_event is not None:
        _reload_event.set()


def main() -> None:
    """Synchronous wrapper that sets up minimal logging, then runs the async loop."""
    # Minimal logging before config is loaded so early errors are visible.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        stream=sys.stderr,
    )
    args = _parse_args()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args.config))


if __name__ == "__main__":
    main()
