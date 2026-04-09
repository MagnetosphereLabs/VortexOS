#!/usr/bin/env bash
set -Eeuo pipefail

REPO_OWNER="${REPO_OWNER:-MagnetosphereLabs}"
REPO_NAME="${REPO_NAME:-VortexOS}"
REPO_BRANCH="${REPO_BRANCH:-main}"

INSTALL_DIR="${INSTALL_DIR:-/opt/vortex-node}"
VENV_DIR="${INSTALL_DIR}/.venv"

APP_FILE="vortex_node.py"
UI_FILE="vortex_os.html"

RAW_BASE="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${REPO_BRANCH}"
APP_URL="${RAW_BASE}/${APP_FILE}"
UI_URL="${RAW_BASE}/${UI_FILE}"

log() {
  printf '\n==> %s\n' "$*"
}

die() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

cleanup_tmp() {
  rm -f "${INSTALL_DIR}/.${APP_FILE}.tmp" "${INSTALL_DIR}/.${UI_FILE}.tmp" 2>/dev/null || true
}

trap cleanup_tmp EXIT
trap 'printf "\nInstaller failed on line %s.\n" "$LINENO" >&2' ERR

if [[ "${EUID}" -ne 0 ]]; then
  die "Run this installer with sudo or as root."
fi

if ! command -v apt-get >/dev/null 2>&1; then
  die "This installer currently supports Ubuntu/Debian-style systems with apt-get."
fi

export DEBIAN_FRONTEND=noninteractive

log "Installing minimal bootstrap packages"
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  python3 \
  python3-venv \
  python3-pip

log "Preparing install directory at ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
cd "${INSTALL_DIR}"

download_file() {
  local url="$1"
  local out="$2"
  local tmp="${INSTALL_DIR}/.${out}.tmp"

  curl --fail --location --show-error --silent "$url" -o "$tmp"
  mv "$tmp" "${INSTALL_DIR}/${out}"
}

log "Downloading Vortex files from GitHub"
download_file "$APP_URL" "$APP_FILE"
download_file "$UI_URL" "$UI_FILE"
chmod 755 "${INSTALL_DIR}/${APP_FILE}"

log "Creating Python virtual environment"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  python3 -m venv "${VENV_DIR}"
fi

log "Upgrading pip tooling in the virtual environment"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip setuptools wheel

log "Launching the built-in Vortex installer"
printf '\n'
printf 'The next step is Vortex itself running its install flow.\n'
printf 'That Python installer handles the heavier setup from here.\n'
printf '\n'

if [[ -r /dev/tty ]]; then
  "${VENV_DIR}/bin/python" "${INSTALL_DIR}/${APP_FILE}" install </dev/tty
else
  die "No interactive TTY available for the Python installer."
fi

SERVICE_PATH="/etc/systemd/system/vortex-node.service"

if command -v systemctl >/dev/null 2>&1; then
  if [[ -f "${SERVICE_PATH}" ]]; then
    log "Systemd service file found; ensuring vortex-node is running"
    systemctl daemon-reload
    systemctl enable --now vortex-node.service

    for _ in 1 2 3 4 5 6 7 8 9 10; do
      if systemctl is-active --quiet vortex-node.service; then
        log "Vortex is running as the vortex-node systemd service"
        printf '\n'
        printf 'Check status with:\n'
        printf '  sudo systemctl status vortex-node --no-pager -l\n'
        printf '\n'
        printf 'Local health check:\n'
        printf '  curl http://127.0.0.1:8787/healthz\n'
        printf '\n'
        exit 0
      fi
      sleep 1
    done

    printf '\nThe vortex-node service file exists but the service did not become active.\n' >&2
    printf 'Check logs with:\n' >&2
    printf '  sudo journalctl -u vortex-node -n 100 --no-pager\n' >&2
    exit 1
  fi
fi

log "No vortex-node systemd service file found; starting Vortex directly"
exec "${VENV_DIR}/bin/python" "${INSTALL_DIR}/${APP_FILE}" run

log "No vortex-node service detected; starting Vortex directly"
exec "${VENV_DIR}/bin/python" "${INSTALL_DIR}/${APP_FILE}" run
