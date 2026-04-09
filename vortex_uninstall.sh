#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/vortex-node}"
SERVICE_NAME="${SERVICE_NAME:-vortex-node}"
TMUX_SESSION_NAME="${TMUX_SESSION_NAME:-vortex-network}"
NGINX_SITE_NAME="${NGINX_SITE_NAME:-vortex-node}"

APP_PATH="${INSTALL_DIR}/vortex_node.py"
VENV_DIR="${INSTALL_DIR}/.venv"
DEFAULT_STATE_DIR="${INSTALL_DIR}/vortex_network_state"
SYSTEMD_UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE_PATH="/etc/default/${SERVICE_NAME}"
NGINX_SITE_AVAILABLE="/etc/nginx/sites-available/${NGINX_SITE_NAME}"
NGINX_SITE_ENABLED="/etc/nginx/sites-enabled/${NGINX_SITE_NAME}"

ASSUME_YES=0
REMOVE_CERTBOT_LINEAGE=0

log() {
  printf '\n==> %s\n' "$*"
}

warn() {
  printf '\nWARNING: %s\n' "$*" >&2
}

die() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

confirm() {
  local prompt="$1"
  if [[ "${ASSUME_YES}" -eq 1 ]]; then
    return 0
  fi
  read -r -p "${prompt} [y/N]: " reply
  [[ "${reply,,}" == "y" || "${reply,,}" == "yes" ]]
}

safe_rm_rf() {
  local target="$1"

  if [[ -z "${target}" ]]; then
    die "Refusing to delete an empty path."
  fi
  if [[ "${target}" == "/" ]]; then
    die "Refusing to delete /"
  fi
  if [[ "${target}" == "/root" || "${target}" == "/home" || "${target}" == "/etc" || "${target}" == "/usr" || "${target}" == "/var" ]]; then
    die "Refusing to delete unsafe top-level path: ${target}"
  fi

  if [[ -e "${target}" || -L "${target}" ]]; then
    rm -rf -- "${target}"
  fi
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

python_json_get() {
  local json_path="$1"
  local expr="$2"
  python3 - "$json_path" "$expr" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
expr = sys.argv[2]

if not path.exists():
    raise SystemExit(0)

try:
    data = json.loads(path.read_text("utf-8"))
except Exception:
    raise SystemExit(0)

value = data
for part in expr.split("."):
    if isinstance(value, dict):
        value = value.get(part)
    else:
        value = None
        break

if value is None:
    raise SystemExit(0)

if isinstance(value, bool):
    print("true" if value else "false")
else:
    print(str(value))
PY
}

usage() {
  cat <<EOF
Usage: sudo bash $0 [--yes] [--remove-certbot-lineage]

Removes Vortex-owned artifacts only:
- ${INSTALL_DIR}
- ${DEFAULT_STATE_DIR}
- ${SYSTEMD_UNIT_PATH}
- ${ENV_FILE_PATH}
- ${NGINX_SITE_AVAILABLE}
- ${NGINX_SITE_ENABLED}
- tmux session ${TMUX_SESSION_NAME}
- Vortex-owned WireGuard namespace artifacts, if configured

Does NOT uninstall nginx, certbot, tmux, python, or other system packages.

Options:
  --yes                    Skip confirmation prompt
  --remove-certbot-lineage Also remove the Let's Encrypt lineage for the Vortex domain, if found in config
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)
      ASSUME_YES=1
      shift
      ;;
    --remove-certbot-lineage)
      REMOVE_CERTBOT_LINEAGE=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  die "Run this uninstaller with sudo or as root."
fi

CONFIG_PATH="${DEFAULT_STATE_DIR}/config.json"
DOMAIN=""
WG_ENABLED=""
WG_NAMESPACE=""
WG_INTERFACE=""

if [[ -f "${CONFIG_PATH}" ]]; then
  DOMAIN="$(python_json_get "${CONFIG_PATH}" "server.public_base_url" || true)"
  WG_ENABLED="$(python_json_get "${CONFIG_PATH}" "egress.wireguard.enabled" || true)"
  WG_NAMESPACE="$(python_json_get "${CONFIG_PATH}" "egress.wireguard.namespace_name" || true)"
  WG_INTERFACE="$(python_json_get "${CONFIG_PATH}" "egress.wireguard.interface_name" || true)"
fi

if [[ -z "${WG_NAMESPACE}" ]]; then
  WG_NAMESPACE="vortexnode-worker"
fi
if [[ -z "${WG_INTERFACE}" ]]; then
  WG_INTERFACE="vortexwg0"
fi

CERTBOT_DOMAIN=""
if [[ -n "${DOMAIN}" ]]; then
  CERTBOT_DOMAIN="$(python3 - "$DOMAIN" <<'PY'
import sys, urllib.parse
raw = sys.argv[1].strip()
if not raw:
    raise SystemExit(0)
parsed = urllib.parse.urlparse(raw if "://" in raw else "https://" + raw)
print(parsed.hostname or "")
PY
)"
fi

cat <<EOF
This will remove Vortex-related artifacts from this machine:

  Install dir:         ${INSTALL_DIR}
  Virtualenv:          ${VENV_DIR}
  App file:            ${APP_PATH}
  Default state dir:   ${DEFAULT_STATE_DIR}
  Systemd unit:        ${SYSTEMD_UNIT_PATH}
  Env file:            ${ENV_FILE_PATH}
  Nginx site:          ${NGINX_SITE_AVAILABLE}
  Nginx symlink:       ${NGINX_SITE_ENABLED}
  tmux session:        ${TMUX_SESSION_NAME}
  WireGuard namespace: ${WG_NAMESPACE}
  WireGuard iface:     ${WG_INTERFACE}
EOF

if [[ -n "${CERTBOT_DOMAIN}" ]]; then
  cat <<EOF
  Vortex domain seen in config: ${CERTBOT_DOMAIN}
EOF
fi

if [[ "${REMOVE_CERTBOT_LINEAGE}" -eq 1 && -n "${CERTBOT_DOMAIN}" ]]; then
  cat <<EOF
  Certbot lineage removal: enabled
EOF
else
  cat <<EOF
  Certbot lineage removal: disabled
EOF
fi

if ! confirm "Proceed with Vortex uninstall?"; then
  echo "Cancelled."
  exit 1
fi

log "Stopping tmux session if present"
if have_cmd tmux; then
  tmux has-session -t "${TMUX_SESSION_NAME}" 2>/dev/null && tmux kill-session -t "${TMUX_SESSION_NAME}" || true
fi

log "Stopping and disabling systemd service if present"
if have_cmd systemctl; then
  systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
fi

log "Killing leftover Vortex processes from install dir if any remain"
if have_cmd pkill; then
  pkill -f "${APP_PATH}" 2>/dev/null || true
  pkill -f "${VENV_DIR}/bin/python ${APP_PATH}" 2>/dev/null || true
fi

log "Removing Vortex systemd unit"
safe_rm_rf "${SYSTEMD_UNIT_PATH}"
safe_rm_rf "${ENV_FILE_PATH}"

if have_cmd systemctl; then
  systemctl daemon-reload 2>/dev/null || true
  systemctl reset-failed 2>/dev/null || true
fi

log "Removing Vortex-owned nginx site files only"
if [[ -L "${NGINX_SITE_ENABLED}" || -f "${NGINX_SITE_ENABLED}" ]]; then
  safe_rm_rf "${NGINX_SITE_ENABLED}"
fi
if [[ -f "${NGINX_SITE_AVAILABLE}" ]]; then
  safe_rm_rf "${NGINX_SITE_AVAILABLE}"
fi

if have_cmd nginx; then
  if nginx -t >/dev/null 2>&1; then
    if have_cmd systemctl; then
      systemctl reload nginx 2>/dev/null || true
    fi
  else
    warn "nginx -t failed after removing the Vortex site. Inspect nginx config manually."
  fi
fi

log "Removing Vortex WireGuard namespace artifacts if configured"
if [[ "${WG_ENABLED}" == "true" || "${WG_NAMESPACE}" == "vortexnode-worker" || "${WG_INTERFACE}" == "vortexwg0" ]]; then
  if have_cmd ip; then
    ip netns pids "${WG_NAMESPACE}" >/dev/null 2>&1 && ip netns delete "${WG_NAMESPACE}" 2>/dev/null || true
    ip link show "${WG_INTERFACE}" >/dev/null 2>&1 && ip link delete "${WG_INTERFACE}" 2>/dev/null || true
  fi
  safe_rm_rf "/etc/netns/${WG_NAMESPACE}"
fi

log "Optionally removing Vortex Certbot lineage"
if [[ "${REMOVE_CERTBOT_LINEAGE}" -eq 1 ]]; then
  if [[ -n "${CERTBOT_DOMAIN}" ]]; then
    if have_cmd certbot; then
      certbot delete --cert-name "${CERTBOT_DOMAIN}" --non-interactive 2>/dev/null || true
    fi
    safe_rm_rf "/etc/letsencrypt/live/${CERTBOT_DOMAIN}"
    safe_rm_rf "/etc/letsencrypt/archive/${CERTBOT_DOMAIN}"
    safe_rm_rf "/etc/letsencrypt/renewal/${CERTBOT_DOMAIN}.conf"
  else
    warn "No domain found in Vortex config, so no certbot lineage was removed."
  fi
fi

log "Removing Vortex install directory and default state directory"
safe_rm_rf "${INSTALL_DIR}"

# In normal installs the state dir is inside INSTALL_DIR, but remove it separately
# in case it still exists or was bind-mounted/copied elsewhere.
if [[ "${DEFAULT_STATE_DIR}" != "${INSTALL_DIR}" ]]; then
  safe_rm_rf "${DEFAULT_STATE_DIR}"
fi

log "Vortex uninstall complete"
cat <<EOF

Removed:
  - ${INSTALL_DIR}
  - ${SYSTEMD_UNIT_PATH}
  - ${ENV_FILE_PATH}
  - ${NGINX_SITE_AVAILABLE}
  - ${NGINX_SITE_ENABLED}
  - tmux session ${TMUX_SESSION_NAME}
  - Vortex WireGuard namespace artifacts (if present)

Left alone:
  - nginx package
  - certbot package
  - python / system packages
  - any non-Vortex nginx sites
  - any arbitrary files outside the Vortex paths

EOF
