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

"${VENV_DIR}/bin/python" "${INSTALL_DIR}/${APP_FILE}" install

if command -v systemctl >/dev/null 2>&1; then
  if systemctl list-unit-files 2>/dev/null | grep -q '^vortex-node\.service'; then
    log "Systemd service detected; ensuring it is running"
    systemctl daemon-reload || true
    systemctl enable --now vortex-node || true

    if systemctl is-active --quiet vortex-node; then
      log "Vortex is running as the vortex-node systemd service"
      printf '\n'
      printf 'Check status with:\n'
      printf '  sudo systemctl status vortex-node\n'
      printf '\n'
      printf 'Local health check:\n'
      printf '  curl http://127.0.0.1:8787/healthz\n'
      printf '\n'
      exit 0
    fi
  fi
fi

log "No active vortex-node service detected; starting Vortex directly"
exec "${VENV_DIR}/bin/python" "${INSTALL_DIR}/${APP_FILE}" run
