"""YAML configuration loader and validation for Crafty Server Starter."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# PyYAML — the only external dependency. Justified: Python stdlib has no YAML
# parser, and YAML was chosen as the config format.
try:
    import yaml
except ImportError:
    print(
        "FATAL: PyYAML is required.  Install it with:  pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CraftyConfig:
    """Connection settings for the Crafty Controller API."""

    base_url: str = "https://localhost:8443"
    api_token_env: str = "CRAFTY_API_TOKEN"
    verify_tls: bool = True

    # Resolved at runtime — never stored in the YAML.
    api_token: str = field(default="", repr=False)

    def resolve_token(self) -> None:
        """Read the API token from the environment variable."""
        token = os.environ.get(self.api_token_env, "")
        if not token:
            raise ConfigError(
                f"Environment variable '{self.api_token_env}' is not set or empty. "
                "It must contain a valid Crafty API token."
            )
        self.api_token = token


@dataclass
class ServerConfig:
    """Per-Minecraft-server settings."""

    name: str
    crafty_server_id: str
    listen_port: int
    listen_host: str = "0.0.0.0"
    edition: str = "java"  # "java" or "bedrock"
    idle_timeout_minutes: int = 10
    start_timeout_seconds: int = 180
    motd_hibernating: str = "§7⏳ Server is hibernating. Connect to wake it up!"
    kick_message: str = "§eServer is starting up!\n§7Please reconnect in about 60 seconds."


@dataclass
class PollingConfig:
    """Polling intervals and retry behaviour."""

    interval_seconds: int = 30
    api_retry_delay_seconds: int = 10
    api_max_retries: int = 3


@dataclass
class CooldownConfig:
    """Anti-flap / hysteresis settings."""

    stop_cooldown_minutes: int = 5
    start_grace_minutes: int = 3
    flap_window_minutes: int = 30
    flap_max_cycles: int = 3
    flap_backoff_minutes: int = 10


@dataclass
class WebhookConfig:
    """Webhook notification settings."""

    enabled: bool = False
    url: str = ""
    label: str = "Crafty Server Starter"


@dataclass
class LoggingConfig:
    """Logging destination and rotation settings."""

    level: str = "INFO"
    file: str = "/var/log/crafty-server-starter/service.log"
    max_bytes: int = 10_485_760  # 10 MB
    backup_count: int = 5


@dataclass
class HealthConfig:
    """Health/status HTTP endpoint settings."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8095


@dataclass
class AppConfig:
    """Top-level application configuration."""

    crafty: CraftyConfig = field(default_factory=CraftyConfig)
    servers: dict[str, ServerConfig] = field(default_factory=dict)
    polling: PollingConfig = field(default_factory=PollingConfig)
    cooldowns: CooldownConfig = field(default_factory=CooldownConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    health: HealthConfig = field(default_factory=HealthConfig)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when the configuration is invalid or incomplete."""


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _get(data: dict[str, Any], key: str, expected_type: type, default: Any = None) -> Any:
    """Retrieve *key* from *data*, coerce to *expected_type*, fallback to *default*."""
    value = data.get(key, default)
    if value is None:
        return default
    try:
        return expected_type(value)
    except (ValueError, TypeError) as exc:
        raise ConfigError(
            f"Config key '{key}': cannot convert {value!r} to {expected_type.__name__}"
        ) from exc


def _load_crafty(raw: dict[str, Any]) -> CraftyConfig:
    return CraftyConfig(
        base_url=_get(raw, "base_url", str, CraftyConfig.base_url),
        api_token_env=_get(raw, "api_token_env", str, CraftyConfig.api_token_env),
        verify_tls=_get(raw, "verify_tls", bool, CraftyConfig.verify_tls),
    )


def _load_server(name: str, raw: dict[str, Any]) -> ServerConfig:
    cid = raw.get("crafty_server_id")
    if not cid:
        raise ConfigError(f"Server '{name}': 'crafty_server_id' is required.")
    port = raw.get("listen_port")
    if port is None:
        raise ConfigError(f"Server '{name}': 'listen_port' is required.")
    edition = _get(raw, "edition", str, ServerConfig.edition).lower()
    if edition not in ("java", "bedrock"):
        raise ConfigError(f"Server '{name}': edition must be 'java' or 'bedrock', got '{edition}'.")
    return ServerConfig(
        name=name,
        crafty_server_id=str(cid),
        listen_port=int(port),
        listen_host=_get(raw, "listen_host", str, ServerConfig.listen_host),
        edition=edition,
        idle_timeout_minutes=_get(
            raw, "idle_timeout_minutes", int, ServerConfig.idle_timeout_minutes
        ),
        start_timeout_seconds=_get(
            raw, "start_timeout_seconds", int, ServerConfig.start_timeout_seconds
        ),
        motd_hibernating=_get(raw, "motd_hibernating", str, ServerConfig.motd_hibernating),
        kick_message=_get(raw, "kick_message", str, ServerConfig.kick_message),
    )


def _load_polling(raw: dict[str, Any]) -> PollingConfig:
    return PollingConfig(
        interval_seconds=_get(raw, "interval_seconds", int, PollingConfig.interval_seconds),
        api_retry_delay_seconds=_get(
            raw, "api_retry_delay_seconds", int, PollingConfig.api_retry_delay_seconds
        ),
        api_max_retries=_get(raw, "api_max_retries", int, PollingConfig.api_max_retries),
    )


def _load_cooldowns(raw: dict[str, Any]) -> CooldownConfig:
    return CooldownConfig(
        stop_cooldown_minutes=_get(
            raw, "stop_cooldown_minutes", int, CooldownConfig.stop_cooldown_minutes
        ),
        start_grace_minutes=_get(
            raw, "start_grace_minutes", int, CooldownConfig.start_grace_minutes
        ),
        flap_window_minutes=_get(
            raw, "flap_window_minutes", int, CooldownConfig.flap_window_minutes
        ),
        flap_max_cycles=_get(raw, "flap_max_cycles", int, CooldownConfig.flap_max_cycles),
        flap_backoff_minutes=_get(
            raw, "flap_backoff_minutes", int, CooldownConfig.flap_backoff_minutes
        ),
    )


def _load_logging(raw: dict[str, Any]) -> LoggingConfig:
    return LoggingConfig(
        level=_get(raw, "level", str, LoggingConfig.level),
        file=_get(raw, "file", str, LoggingConfig.file),
        max_bytes=_get(raw, "max_bytes", int, LoggingConfig.max_bytes),
        backup_count=_get(raw, "backup_count", int, LoggingConfig.backup_count),
    )


def _load_webhook(raw: dict[str, Any]) -> WebhookConfig:
    cfg = WebhookConfig(
        enabled=_get(raw, "enabled", bool, WebhookConfig.enabled),
        url=_get(raw, "url", str, WebhookConfig.url),
        label=_get(raw, "label", str, WebhookConfig.label),
    )
    if cfg.enabled and not cfg.url:
        raise ConfigError("webhook.enabled is true but webhook.url is not set.")
    return cfg


def _load_health(raw: dict[str, Any]) -> HealthConfig:
    return HealthConfig(
        enabled=_get(raw, "enabled", bool, HealthConfig.enabled),
        host=_get(raw, "host", str, HealthConfig.host),
        port=_get(raw, "port", int, HealthConfig.port),
    )


def load_config(path: str | Path) -> AppConfig:
    """Load and validate configuration from a YAML file.

    Parameters
    ----------
    path:
        Filesystem path to the YAML config file.

    Returns
    -------
    AppConfig
        Fully-validated configuration object.

    Raises
    ------
    ConfigError
        If the file is missing, unparseable, or semantically invalid.
    """
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse YAML config: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a YAML mapping (dict).")

    # -- Crafty --
    crafty = _load_crafty(raw.get("crafty", {}))

    # -- Servers --
    raw_servers = raw.get("servers", {})
    if not raw_servers:
        raise ConfigError("At least one server must be defined under 'servers:'.")
    servers: dict[str, ServerConfig] = {}
    seen_ports: dict[int, str] = {}
    for name, srv_raw in raw_servers.items():
        if not isinstance(srv_raw, dict):
            raise ConfigError(f"Server '{name}' must be a YAML mapping.")
        srv = _load_server(str(name), srv_raw)
        if srv.listen_port in seen_ports:
            raise ConfigError(
                f"Server '{name}' and '{seen_ports[srv.listen_port]}' both use port {srv.listen_port}."
            )
        seen_ports[srv.listen_port] = name
        servers[str(name)] = srv

    # -- Polling --
    polling = _load_polling(raw.get("polling", {}))

    # -- Cooldowns --
    cooldowns = _load_cooldowns(raw.get("cooldowns", {}))

    # -- Logging --
    logging_cfg = _load_logging(raw.get("logging", {}))

    # -- Webhook --
    webhook_cfg = _load_webhook(raw.get("webhook", {}))
    # -- Health --
    health_cfg = _load_health(raw.get("health", {}))

    config = AppConfig(
        crafty=crafty,
        servers=servers,
        polling=polling,
        cooldowns=cooldowns,
        logging=logging_cfg,
        webhook=webhook_cfg,
        health=health_cfg,
    )

    # Resolve the API token from the environment.
    config.crafty.resolve_token()

    return config
