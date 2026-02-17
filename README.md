# Crafty Server Watcher

[![Lint](https://github.com/Soveticka/crafty-server-watcher/actions/workflows/lint.yml/badge.svg)](https://github.com/Soveticka/crafty-server-watcher/actions/workflows/lint.yml)
[![Docker Build](https://github.com/Soveticka/crafty-server-watcher/actions/workflows/docker-build.yml/badge.svg)](https://github.com/Soveticka/crafty-server-watcher/actions/workflows/docker-build.yml)
[![CodeQL](https://github.com/Soveticka/crafty-server-watcher/actions/workflows/codeql.yml/badge.svg)](https://github.com/Soveticka/crafty-server-watcher/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/github/license/Soveticka/crafty-server-watcher)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

Auto-hibernate idle Minecraft servers and wake them on player connect, powered by the [Crafty Controller](https://craftycontrol.com) ([GitHub](https://gitlab.com/crafty-controller/crafty-4)) API v2.

## Features

- **Idle shutdown** — Stops servers via Crafty API when 0 players for a configurable duration
- **Wake-on-connect** — Binds to MC ports while servers are offline; shows a custom MOTD and kicks login attempts with a "starting…" message, then triggers a start via Crafty API
- **Multi-server** — Manage any number of Minecraft Java & Bedrock servers, each on a separate port
- **Bedrock Edition support** — UDP/RakNet proxy for Bedrock servers alongside Java TCP proxies
- **Health & metrics** — `/health`, `/status` (JSON), and `/metrics` (Prometheus) endpoints
- **Discord notifications** — Webhook alerts on server start, stop, and crash events
- **Config hot-reload** — Send SIGHUP to reload timeouts, MOTDs, and cooldowns without restart
- **Anti-flap** — Start grace, stop cooldown, and cycle-count-based flap guard
- **Minimal dependencies** — Python 3.11 + PyYAML only

## Requirements

- Crafty Controller 4.x with API v2 access
- A dedicated Crafty API user/role (see [Crafty API Setup](#crafty-api-setup))

---

## Deployment — Docker (Recommended)

### 1. Create your config

```bash
mkdir crafty-server-watcher && cd crafty-server-watcher
curl -O https://raw.githubusercontent.com/Soveticka/crafty-server-watcher/main/config.example.yaml
cp config.example.yaml config.yaml
nano config.yaml    # set your Crafty server UUIDs and ports
```

### 2. Create a `.env` file for the API token

```bash
echo "CRAFTY_API_TOKEN=your-token-here" > .env
chmod 600 .env
```

### 3. Create `docker-compose.yml`

```yaml
services:
  crafty-server-watcher:
    image: ghcr.io/Soveticka/crafty-server-watcher:latest
    container_name: crafty-server-watcher
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./config.yaml:/config/config.yaml:ro
    environment:
      - CRAFTY_API_TOKEN=${CRAFTY_API_TOKEN}
```

> **Why `network_mode: host`?** The service must bind directly to the MC server ports on your host so that players connect to the proxy when servers are hibernating.

### 4. Start

```bash
docker compose up -d
docker compose logs -f
```

### Updating (Docker)

```bash
docker compose pull
docker compose up -d
```

---

## Deployment — Manual

<details>
<summary>Click to expand manual deployment instructions</summary>

### Requirements

- Linux (tested on Debian 13 / Trixie)
- Python ≥ 3.11
- PyYAML (`pip install pyyaml`)

### Install

```bash
# 1. Clone
sudo git clone https://github.com/Soveticka/crafty-server-watcher.git /opt/crafty-server-watcher
cd /opt/crafty-server-watcher

# 2. Run installer (creates user, venv, directories, systemd service)
sudo bash install.sh

# 3. Configure
sudo nano /etc/crafty-server-watcher/config.yaml   # set server IDs & ports
sudo nano /etc/crafty-server-watcher/env            # set CRAFTY_API_TOKEN

# 4. Start
sudo systemctl start crafty-server-watcher
sudo systemctl status crafty-server-watcher

# 5. Logs
journalctl -u crafty-server-watcher -f
tail -f /var/log/crafty-server-watcher/service.log
```

### Updating (Manual)

```bash
cd /opt/crafty-server-watcher
sudo systemctl stop crafty-server-watcher
sudo git pull
sudo find . -type d -name __pycache__ -exec rm -rf {} +
sudo systemctl start crafty-server-watcher
```

If the systemd service file changed:

```bash
sudo cp systemd/crafty-server-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart crafty-server-watcher
```

</details>

---

## Configuration

See [`config.example.yaml`](config.example.yaml) for all available options.

### Crafty API Setup

1. Create a **dedicated Crafty user** (e.g., `auto-watcher`)
2. Create a **role** with only **Commands** permission on your managed servers
3. Assign the role to the user
4. Generate a long-lived API token for this user
5. Pass the token via the `CRAFTY_API_TOKEN` environment variable

### Server Mapping

Each server entry maps a listening port to a Crafty server UUID:

```yaml
servers:
  vanilla:
    crafty_server_id: "5adcec83-684c-4555-a7e9-9d913203d07e"
    listen_port: 25565
    idle_timeout_minutes: 10
    start_timeout_seconds: 180
```

The `crafty_server_id` can be found in the Crafty dashboard URL or via `GET /api/v2/servers`.

---

## Architecture

Single Python asyncio daemon:

| Module | Purpose |
|---|---|
| `idle_monitor.py` | Polls Crafty API, drives per-server state machine |
| `proxy_listener.py` | Per-port TCP proxy (Java Edition fake MC protocol) |
| `bedrock_proxy.py` | Per-port UDP proxy (Bedrock Edition RakNet protocol) |
| `mc_protocol.py` | Java Edition protocol parsing (handshake, status, login) |
| `bedrock_protocol.py` | RakNet protocol helpers (ping/pong, connection reject) |
| `crafty_api.py` | Async Crafty API v2 client (stdlib `http.client`) |
| `server_state.py` | 7-state machine with timing/cooldown logic |
| `health_server.py` | HTTP server for `/health`, `/status`, `/metrics` |
| `metrics.py` | Prometheus text exposition format generator |
| `webhook.py` | Discord/generic webhook notifications |
| `config.py` | YAML config loader and validation |
| `logger.py` | Rotating file + stderr logging |

### State Machine

```
UNKNOWN → ONLINE / IDLE / STOPPED / CRASHED
ONLINE  → IDLE / STOPPED / CRASHED
IDLE    → ONLINE / STOPPING / STOPPED / CRASHED
STOPPING → STOPPED / CRASHED
STOPPED  → STARTING / ONLINE
STARTING → ONLINE / STOPPED / CRASHED
CRASHED  → STOPPED / ONLINE
```

## Security

- API token passed via environment variable, never in config files or logs
- Dedicated least-privilege Crafty user/role
- systemd sandboxing (manual deploy): `ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`, etc.
- Docker: minimal `python:3.11-slim` image, read-only config mount

## License

MIT
