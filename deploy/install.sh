#!/usr/bin/env bash
# install.sh — idempotent installer for BirdHeatmap on Debian/Ubuntu.
#
# Usage:
#   sudo ./install.sh          # fresh install or upgrade
#
# What it does:
#   1. Checks for required system packages (python3, python3-venv).
#   2. Creates the `birdheatmap` system user (if missing).
#   3. Installs/updates code in /opt/birdheatmap (Python venv).
#   4. Creates /var/lib/birdheatmap (state) and /etc/birdheatmap (config).
#   5. Copies the example env file on first install; never overwrites existing.
#   6. Installs and enables the systemd service unit.
#   7. On upgrade: restarts the service.
#
# Safe to re-run for upgrades.  Config and database are never touched.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
APP_USER="birdheatmap"
INSTALL_DIR="/opt/birdheatmap"
STATE_DIR="/var/lib/birdheatmap"
CONFIG_DIR="/etc/birdheatmap"
SERVICE_FILE="/etc/systemd/system/birdheatmap.service"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo "[install] $*"; }
warn()  { echo "[install] WARNING: $*" >&2; }
error() { echo "[install] ERROR: $*" >&2; exit 1; }

require_root() {
    [[ $EUID -eq 0 ]] || error "This script must be run as root (sudo ./install.sh)."
}

# ---------------------------------------------------------------------------
# 0. System prerequisites
# ---------------------------------------------------------------------------
check_prerequisites() {
    info "Checking prerequisites …"

    # python3 must be >= 3.11
    if ! command -v python3 &>/dev/null; then
        error "python3 not found. Install it with: apt install python3"
    fi
    PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PY_MAJOR=${PY_VER%%.*}
    PY_MINOR=${PY_VER##*.}
    if [[ $PY_MAJOR -lt 3 || ($PY_MAJOR -eq 3 && $PY_MINOR -lt 11) ]]; then
        error "Python 3.11+ required (found ${PY_VER}). Install: apt install python3.11"
    fi
    info "Python ${PY_VER} — OK"

    # python3-venv is a separate package on Debian
    if ! python3 -m venv --help &>/dev/null 2>&1; then
        error "python3-venv not found. Install it with: apt install python3-venv"
    fi
}

# ---------------------------------------------------------------------------
# 1. System user
# ---------------------------------------------------------------------------
create_user() {
    if id "$APP_USER" &>/dev/null; then
        info "User '$APP_USER' already exists — skipping."
    else
        info "Creating system user '$APP_USER' …"
        useradd \
            --system \
            --no-create-home \
            --shell /usr/sbin/nologin \
            --comment "BirdHeatmap service account" \
            "$APP_USER"
    fi
}

# ---------------------------------------------------------------------------
# 2. Python venv + package
# ---------------------------------------------------------------------------
install_code() {
    info "Installing/updating code in $INSTALL_DIR …"
    mkdir -p "$INSTALL_DIR"

    if [[ ! -d "$INSTALL_DIR/venv" ]]; then
        info "Creating Python virtual environment …"
        python3 -m venv "$INSTALL_DIR/venv"
    fi

    info "Upgrading pip …"
    "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip

    info "Installing pinned dependencies from lockfile …"
    "$INSTALL_DIR/venv/bin/pip" install --quiet --require-hashes -r "$REPO_ROOT/requirements.lock"

    info "Installing birdheatmap package (deps already locked above) …"
    "$INSTALL_DIR/venv/bin/pip" install --quiet --no-deps "$REPO_ROOT"

    # Pre-compile .pyc files so the service (running under ProtectSystem=strict,
    # which makes the code directory read-only) never needs to write at runtime.
    info "Pre-compiling Python bytecode …"
    "$INSTALL_DIR/venv/bin/python" -m compileall -q "$INSTALL_DIR/venv/lib"

    chown -R "$APP_USER:$APP_USER" "$INSTALL_DIR"
}

# ---------------------------------------------------------------------------
# 3. State and config directories
# ---------------------------------------------------------------------------
create_dirs() {
    info "Creating state directory $STATE_DIR …"
    mkdir -p "$STATE_DIR/cache" "$STATE_DIR/.matplotlib"
    chown -R "$APP_USER:$APP_USER" "$STATE_DIR"
    chmod 750 "$STATE_DIR"

    info "Creating config directory $CONFIG_DIR …"
    mkdir -p "$CONFIG_DIR"
    chmod 750 "$CONFIG_DIR"
}

# ---------------------------------------------------------------------------
# 4. Example env file (first install only)
# ---------------------------------------------------------------------------
install_env_example() {
    local dest="$CONFIG_DIR/birdheatmap.env"
    if [[ -f "$dest" ]]; then
        info "Config $dest already exists — not overwriting."
    else
        info "Installing example config to $dest …"
        cp "$SCRIPT_DIR/birdheatmap.env.example" "$dest"
        chown root:"$APP_USER" "$dest"
        chmod 640 "$dest"
        echo ""
        echo "  ======================================================"
        echo "  ACTION REQUIRED before starting the service:"
        echo "  Edit $dest"
        echo "  At minimum, confirm STATION_ID=5114 is correct."
        echo "  ======================================================"
        echo ""
    fi
}

# ---------------------------------------------------------------------------
# 5. systemd service
# ---------------------------------------------------------------------------
install_service() {
    info "Installing systemd unit …"
    cp "$SCRIPT_DIR/birdheatmap.service" "$SERVICE_FILE"
    systemctl daemon-reload
    systemctl enable birdheatmap.service
}

# ---------------------------------------------------------------------------
# 6. Start / restart
# ---------------------------------------------------------------------------
start_service() {
    if systemctl is-active --quiet birdheatmap.service; then
        info "Restarting birdheatmap.service …"
        systemctl restart birdheatmap.service
    else
        info "Starting birdheatmap.service …"
        systemctl start birdheatmap.service
    fi
    echo ""
    systemctl status birdheatmap.service --no-pager --lines=5 || true
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
require_root
check_prerequisites
create_user
install_code
create_dirs
install_env_example
install_service
start_service

echo ""
info "Done."
info "View logs:        journalctl -u birdheatmap -f"
info "Manual sync:      sudo -u birdheatmap $INSTALL_DIR/venv/bin/python -m birdheatmap sync"
info "Web UI (LAN):     http://$(hostname -I | awk '{print $1}'):8765"
