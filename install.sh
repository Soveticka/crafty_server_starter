#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# Crafty Server Watcher — installation script
# ═══════════════════════════════════════════════════════════════════
# Run as root.  Installs the Python package, creates the system user,
# sets up directories, and enables the systemd service.
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

INSTALL_DIR="/opt/crafty-server-watcher"
CONFIG_DIR="/etc/crafty-server-watcher"
LOG_DIR="/var/log/crafty-server-watcher"
SERVICE_USER="crafty-watcher"
SERVICE_GROUP="crafty-watcher"

# ── Pre-flight checks ──────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root." >&2
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 is required." >&2
    exit 1
fi

echo "=== Crafty Server Watcher — Installer ==="

# ── Create service user ────────────────────────────────────────
if ! id -u "$SERVICE_USER" &>/dev/null; then
    echo "Creating system user '$SERVICE_USER'…"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# ── Create directories ─────────────────────────────────────────
echo "Creating directories…"
mkdir -p "$INSTALL_DIR"
mkdir -p "$CONFIG_DIR"
mkdir -p "$LOG_DIR"

# ── Copy source code ──────────────────────────────────────────
echo "Installing source code to $INSTALL_DIR…"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp -r "$SCRIPT_DIR/crafty_server_watcher" "$INSTALL_DIR/"

# ── Python virtual environment ─────────────────────────────────
echo "Setting up Python virtual environment…"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install pyyaml

# ── Config ─────────────────────────────────────────────────────
if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
    echo "Installing example config…"
    cp "$SCRIPT_DIR/config.example.yaml" "$CONFIG_DIR/config.yaml"
    echo "  → Edit $CONFIG_DIR/config.yaml with your Crafty server IDs."
fi

if [[ ! -f "$CONFIG_DIR/env" ]]; then
    echo "Installing example env file…"
    cp "$SCRIPT_DIR/systemd/env.example" "$CONFIG_DIR/env"
    echo "  → Edit $CONFIG_DIR/env and set your CRAFTY_API_TOKEN."
fi

# ── Permissions ────────────────────────────────────────────────
echo "Setting permissions…"
chown -R root:root "$INSTALL_DIR"
chmod -R 755 "$INSTALL_DIR"

chown root:"$SERVICE_GROUP" "$CONFIG_DIR/config.yaml"
chmod 640 "$CONFIG_DIR/config.yaml"

chown root:"$SERVICE_GROUP" "$CONFIG_DIR/env"
chmod 640 "$CONFIG_DIR/env"

chown "$SERVICE_USER":"$SERVICE_GROUP" "$LOG_DIR"
chmod 750 "$LOG_DIR"

# ── systemd ────────────────────────────────────────────────────
echo "Installing systemd service…"
cp "$SCRIPT_DIR/systemd/crafty-server-watcher.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable crafty-server-watcher.service

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit $CONFIG_DIR/config.yaml with your server details."
echo "  2. Edit $CONFIG_DIR/env with your Crafty API token."
echo "  3. Start the service:  systemctl start crafty-server-watcher"
echo "  4. Check status:       systemctl status crafty-server-watcher"
echo "  5. View logs:          journalctl -u crafty-server-watcher -f"
echo "                         tail -f $LOG_DIR/service.log"
