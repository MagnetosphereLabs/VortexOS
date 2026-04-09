#!/usr/bin/env python3
"""
Vortex Network
Single-file self-hosted node for Vortex OS.

Modes:
  python vortex_network.py install
  python vortex_network.py run
  python vortex_network.py service-install
  python vortex_network.py doctor
  python vortex_network.py print-nginx

This script intentionally keeps everything in one file:
- interactive installer / config wizard
- FastAPI web server
- auth + rate limiting
- remote browser sessions via Playwright
- translated local-render pages + asset proxy
- fallback screenshot stream mode
- Tor / proxy / system-VPN aware routing
- tmux helpers / service-install helper

"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import dataclasses
import hashlib
import hmac
import html as html_lib
import io
import json
import mimetypes
import os
import pathlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
import typing as t
import urllib.parse
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

# Third-party runtime deps
try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
except ImportError:  # pragma: no cover
    FastAPI = None

try:
    import uvicorn
except ImportError:  # pragma: no cover
    uvicorn = None

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright
except ImportError:  # pragma: no cover
    async_playwright = None
    Browser = BrowserContext = Page = Playwright = t.Any

try:
    import pyotp
except ImportError:  # pragma: no cover
    pyotp = None

try:
    import qrcode
except ImportError:  # pragma: no cover
    qrcode = None

try:
    import av
    from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer, RTCSessionDescription
    from aiortc.contrib.media import MediaPlayer
except ImportError:  # pragma: no cover
    av = None
    RTCPeerConnection = RTCConfiguration = RTCIceServer = RTCSessionDescription = None
    MediaPlayer = None

APP_NAME = "Vortex Node"
APP_VERSION = "0.3.0"

SCRIPT_PATH = pathlib.Path(__file__).resolve()
SCRIPT_DIR = SCRIPT_PATH.parent

DEFAULT_DATA_DIR = pathlib.Path(
    os.environ.get("VORTEX_NETWORK_DIR") or str(SCRIPT_DIR / "vortex_network_state")
).expanduser().resolve()

CONFIG_PATH = DEFAULT_DATA_DIR / "config.json"
BROWSER_STATE_DIR = DEFAULT_DATA_DIR / "browser"
LOG_DIR = DEFAULT_DATA_DIR / "logs"
SESSION_DIR = DEFAULT_DATA_DIR / "sessions"
RUNTIME_DIR = DEFAULT_DATA_DIR / "runtime"
WRAPPER_DIR = RUNTIME_DIR / "wrappers"
WIREGUARD_DIR = DEFAULT_DATA_DIR / "wireguard"

MAX_BODY_PREVIEW = 1024 * 1024

DEFAULT_UI_HTML_CANDIDATES = [
    SCRIPT_DIR / "vortex_os.html",
    SCRIPT_DIR / "vortexos.html",
]
DEFAULT_UI_HTML = next((p for p in DEFAULT_UI_HTML_CANDIDATES if p.exists()), DEFAULT_UI_HTML_CANDIDATES[0])

DEFAULT_UPDATE_OWNER = "MagnetosphereLabs"
DEFAULT_UPDATE_REPO = "VortexOS"
DEFAULT_UPDATE_BRANCH = "main"
DEFAULT_REMOTE_BACKEND_PATH = SCRIPT_PATH.name
DEFAULT_REMOTE_FRONTEND_PATH = "vortex_os.html"
DEFAULT_WG_INTERFACE = "vortexwg0"
DEFAULT_WG_NAMESPACE = "vortexnode-worker"
DEFAULT_TOR_SOCKS = "socks5://127.0.0.1:9050"

PAIR_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 180  # 180 days

SYNCED_BLOB_DIR = DEFAULT_DATA_DIR / "synced_os_profiles"
TERMINAL_STATE_DIR = DEFAULT_DATA_DIR / "terminal"
MAX_TABS_PER_SESSION = 10
DEFAULT_REMOTE_WIDTH_CAP = 1280
DEFAULT_REMOTE_HEIGHT_CAP = 720
MAX_REMOTE_FPS = 60
DEFAULT_XVFB_START_DISPLAY = 110
DEFAULT_REMOTE_STUN_SERVERS = ["stun:stun.l.google.com:19302"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs() -> None:
    DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    BROWSER_STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    WRAPPER_DIR.mkdir(parents=True, exist_ok=True)
    WIREGUARD_DIR.mkdir(parents=True, exist_ok=True)
    SYNCED_BLOB_DIR.mkdir(parents=True, exist_ok=True)
    TERMINAL_STATE_DIR.mkdir(parents=True, exist_ok=True)


def read_json(path: pathlib.Path, default: t.Any) -> t.Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def write_json(path: pathlib.Path, payload: t.Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False), "utf-8")
    tmp.replace(path)


def prompt(text: str, default: t.Optional[str] = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    label = f"{text}{suffix}: "
    if secret:
        import getpass
        while True:
            value = getpass.getpass(label)
            if value:
                return value
            if default is not None:
                return default
    while True:
        value = input(label).strip()
        if value:
            return value
        if default is not None:
            return default


def prompt_yes_no(text: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        value = input(f"{text} [{suffix}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def prompt_choice(text: str, choices: list[tuple[str, str]], default_key: str) -> str:
    print(text)
    for key, label in choices:
        marker = "*" if key == default_key else " "
        print(f"  {marker} {key}: {label}")
    valid = {k for k, _ in choices}
    while True:
        value = input(f"Choose [{default_key}]: ").strip().lower()
        if not value:
            return default_key
        if value in valid:
            return value
        print(f"Choose one of: {', '.join(sorted(valid))}")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def hash_password(password: str, *, n: int = 2**15, r: int = 8, p: int = 1) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=64)
    return f"scrypt${n}${r}${p}${b64url(salt)}${b64url(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$", 5)
        if algo != "scrypt":
            return False
        salt = b64url_decode(salt_b64)
        expected = b64url_decode(dk_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def sign_token(secret_key: str, payload: dict[str, t.Any]) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body_b64 = b64url(body)
    sig = hmac.new(secret_key.encode("utf-8"), body_b64.encode("utf-8"), hashlib.sha256).digest()
    return f"{body_b64}.{b64url(sig)}"


def verify_token(secret_key: str, token: str) -> dict[str, t.Any]:
    try:
        body_b64, sig_b64 = token.split(".", 1)
        expected_sig = hmac.new(secret_key.encode("utf-8"), body_b64.encode("utf-8"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected_sig, b64url_decode(sig_b64)):
            raise ValueError("bad signature")
        payload = json.loads(b64url_decode(body_b64).decode("utf-8"))
        exp = int(payload.get("exp", 0))
        if exp and time.time() > exp:
            raise ValueError("expired")
        return payload
    except Exception as exc:
        raise ValueError("invalid token") from exc


def shell_join(cmd: list[str]) -> str:
    return " ".join(shlex_quote(x) for x in cmd)


def shlex_quote(value: str) -> str:
    import shlex
    return shlex.quote(value)


def which(name: str) -> str | None:
    return shutil.which(name)


def local_ip_guess() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def parse_origin_list(raw: str) -> list[str]:
    values = [x.strip() for x in raw.split(",") if x.strip()]
    clean: list[str] = []
    for value in values:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            clean.append(f"{parsed.scheme}://{parsed.netloc}")
    return sorted(set(clean))

@dataclasses.dataclass
class SimpleUpstreamResponse:
    status_code: int
    content: bytes
    headers: dict[str, str]


def prompt_multiline(text: str, end_marker: str = "EOF") -> str:
    print(f"{text} (finish with a line containing only {end_marker})")
    lines: list[str] = []
    while True:
        line = input()
        if line.strip() == end_marker:
            break
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def run_command(
    cmd: list[str],
    *,
    check: bool = True,
    capture_output: bool = True,
    text: bool = True,
    input_data: str | bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture_output,
        text=text,
        input=input_data,
        env=env,
    )


def write_private_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, "utf-8")
    os.chmod(path, 0o600)


def apt_install(packages: list[str]) -> None:
    if sys.platform != "linux":
        raise RuntimeError("Automatic package installation in this script is only implemented for Linux.")
    if os.geteuid() != 0:
        raise RuntimeError("Run the installer with sudo/root so packages can be installed automatically.")
    run_command(["apt-get", "update"], check=False)
    run_command(["apt-get", "install", "-y", *packages], check=True)

def restart_current_process(reason: str) -> None:
    print(reason)
    os.execv(sys.executable, [sys.executable, str(SCRIPT_PATH), *sys.argv[1:]])


def read_text_if_exists(path: pathlib.Path) -> str:
    try:
        return path.read_text("utf-8")
    except Exception:
        return ""


def write_text_atomic(path: pathlib.Path, content: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, "utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def migrate_config(raw: dict[str, t.Any]) -> dict[str, t.Any]:
    raw = dict(raw or {})
    if not raw:
        return raw

    raw.setdefault("version", 4)

    raw.setdefault("frontend", {})
    raw["frontend"].setdefault("serve_ui", False)
    raw["frontend"].setdefault("ui_html_path", str(DEFAULT_UI_HTML))

    raw.setdefault("ops", {})

    legacy_updates = raw["ops"].get("updates")
    if isinstance(legacy_updates, dict):
        raw.setdefault("updates", {})
        for key, value in legacy_updates.items():
            raw["updates"].setdefault(key, value)
        raw["ops"].pop("updates", None)

    raw.setdefault("updates", {})
    raw["updates"].setdefault("enabled", True)
    raw["updates"].setdefault("owner", DEFAULT_UPDATE_OWNER)
    raw["updates"].setdefault("repo", DEFAULT_UPDATE_REPO)
    raw["updates"].setdefault("branch", DEFAULT_UPDATE_BRANCH)
    raw["updates"].setdefault("backend_path", DEFAULT_REMOTE_BACKEND_PATH)
    raw["updates"].setdefault("frontend_path", DEFAULT_REMOTE_FRONTEND_PATH)

    raw.setdefault("server", {})
    raw["server"].setdefault("host", "127.0.0.1")
    raw["server"].setdefault("port", 8787)
    raw["server"].setdefault("public_base_url", "https://node.example.com")
    raw["server"].setdefault("allowed_origins", [])
    raw["server"].setdefault("frame_ancestors", [])
    raw["server"].setdefault("max_clients", 12)
    raw["server"].setdefault("exposure_mode", "lan")

    raw.setdefault("browser", {})
    raw["browser"].setdefault("mode", "hybrid")
    raw["browser"].setdefault("max_sessions", 4)
    raw["browser"].setdefault("max_tabs_per_session", MAX_TABS_PER_SESSION)
    raw["browser"].setdefault("viewport", {"width": 1366, "height": 900})
    raw["browser"].setdefault("user_agent", "")
    raw["browser"].setdefault("block_aggressive_popups", True)
    raw["browser"].setdefault("strip_common_junk", True)
    raw["browser"].setdefault("allow_media_proxy", True)
    raw["browser"].setdefault("screenshot_quality", 85)
    raw["browser"].setdefault("screenshot_fps", 30)
    raw["browser"].setdefault("remote_width_cap", DEFAULT_REMOTE_WIDTH_CAP)
    raw["browser"].setdefault("remote_height_cap", DEFAULT_REMOTE_HEIGHT_CAP)
    raw["browser"].setdefault("detection", {})
    raw["browser"]["detection"].setdefault("heavy_dom_threshold", 5000)
    raw["browser"]["detection"].setdefault("heavy_script_threshold", 32)
    raw["browser"]["detection"].setdefault("canvas_threshold", 2)

    raw["ops"].setdefault("use_tmux", True)
    raw["ops"].setdefault("run_on_boot", True)
    raw["ops"].setdefault("auto_https", False)
    raw["ops"].setdefault("certbot_email", "")

    return raw


def ensure_system_packages() -> None:
    missing: list[str] = []

    if which("curl") is None:
        missing.append("curl")
    if which("tmux") is None:
        missing.append("tmux")
    if which("ip") is None:
        missing.append("iproute2")
    if which("wg") is None:
        missing.extend(["wireguard", "wireguard-tools"])
    if which("tor") is None:
        missing.append("tor")
    if which("ffmpeg") is None:
        missing.append("ffmpeg")
    if which("Xvfb") is None:
        missing.append("xvfb")
    if which("xrandr") is None:
        missing.append("x11-xserver-utils")
    if which("pulseaudio") is None:
        missing.append("pulseaudio")
    if which("pactl") is None:
        missing.append("pulseaudio-utils")
    if which("dbus-launch") is None:
        missing.append("dbus-x11")

    missing = sorted(set(missing))
    if missing:
        print(f"Installing Ubuntu packages: {', '.join(missing)}")
        apt_install(missing)


def ensure_python_packages() -> bool:
    missing: list[str] = []
    if FastAPI is None:
        missing.append("fastapi")
    if uvicorn is None:
        missing.append("uvicorn[standard]")
    if httpx is None:
        missing.append("httpx[socks]")
    if BeautifulSoup is None:
        missing.extend(["beautifulsoup4", "lxml"])
    if async_playwright is None:
        missing.append("playwright")
    if pyotp is None:
        missing.append("pyotp")
    if qrcode is None:
        missing.append("qrcode[pil]")
    if RTCPeerConnection is None:
        missing.append("aiortc")
    if av is None:
        missing.append("av")

    missing = sorted(set(missing))
    if not missing:
        return False

    print(f"Installing Python packages: {', '.join(missing)}")
    run_command([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], check=False)
    run_command([sys.executable, "-m", "pip", "install", *missing], check=True)
    return True


def playwright_browser_ready() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return pathlib.Path(p.chromium.executable_path).exists()
    except Exception:
        return False


def ensure_playwright_browser() -> None:
    if playwright_browser_ready():
        return

    print("Installing Playwright Chromium...")
    run_command([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    if sys.platform == "linux" and os.geteuid() == 0:
        run_command([sys.executable, "-m", "playwright", "install-deps", "chromium"], check=True)


def bootstrap_runtime() -> None:
    ensure_dirs()
    ensure_system_packages()
    if ensure_python_packages():
        restart_current_process("Python dependencies installed. Restarting Vortex Node...")
    ensure_playwright_browser()


def current_frontend_path(cfg_raw: dict[str, t.Any] | None = None) -> pathlib.Path:
    cfg_raw = cfg_raw or {}
    ui_path = str((cfg_raw.get("frontend") or {}).get("ui_html_path") or DEFAULT_UI_HTML)
    return pathlib.Path(ui_path).expanduser().resolve()


def github_fetch_text(owner: str, repo: str, branch: str, remote_path: str) -> str:
    import urllib.request

    raw_url = (
        f"https://raw.githubusercontent.com/"
        f"{urllib.parse.quote(owner, safe='')}/"
        f"{urllib.parse.quote(repo, safe='')}/"
        f"{urllib.parse.quote(branch, safe='')}/"
        f"{urllib.parse.quote(remote_path, safe='/')}"
    )

    request = urllib.request.Request(
        raw_url,
        headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"}
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def apply_startup_updates(
    cfg_raw: dict[str, t.Any] | None = None,
    *,
    restart_after_backend_update: bool = True,
) -> dict[str, t.Any]:
    cfg_raw = migrate_config(cfg_raw or {})
    updates = cfg_raw.get("updates") or {}
    if not updates.get("enabled", True):
        return {"checked": False, "updated": []}

    owner = str(updates.get("owner") or DEFAULT_UPDATE_OWNER)
    repo = str(updates.get("repo") or DEFAULT_UPDATE_REPO)
    branch = str(updates.get("branch") or DEFAULT_UPDATE_BRANCH)
    backend_remote = str(updates.get("backend_path") or DEFAULT_REMOTE_BACKEND_PATH)
    frontend_remote = str(updates.get("frontend_path") or DEFAULT_REMOTE_FRONTEND_PATH)

    changed: list[str] = []

    try:
        remote_backend = github_fetch_text(owner, repo, branch, backend_remote)
        if remote_backend != read_text_if_exists(SCRIPT_PATH):
            print(f"Update detected for {SCRIPT_PATH.name}. Applying...")
            write_text_atomic(SCRIPT_PATH, remote_backend, mode=0o755)
            changed.append("backend")

        frontend_path = current_frontend_path(cfg_raw)
        remote_frontend = github_fetch_text(owner, repo, branch, frontend_remote)
        if remote_frontend != read_text_if_exists(frontend_path):
            print(f"Update detected for {frontend_path.name}. Applying...")
            write_text_atomic(frontend_path, remote_frontend, mode=0o644)
            changed.append("frontend")
    except Exception as exc:
        print(f"Startup update check skipped: {exc}")
        return {"checked": False, "updated": [], "error": str(exc)}

    if "backend" in changed and restart_after_backend_update:
        restart_current_process("Backend update applied. Restarting Vortex Node...")

    return {"checked": True, "updated": changed}


def factory_reset_flow(*, reinstall: bool = False) -> int:
    print(f"{APP_NAME} factory reset\n")
    if not prompt_yes_no("Erase ALL node state, config, pairings, logs, and runtime files?", default=False):
        print("Cancelled.")
        return 1

    shutil.rmtree(DEFAULT_DATA_DIR, ignore_errors=True)
    ensure_dirs()
    print(f"Wiped {DEFAULT_DATA_DIR}")

    if reinstall:
        return install_flow()

    print("Node state erased. Run `python vortex_network.py install` to configure it again.")
    return 0

def free_display_number(start: int = DEFAULT_XVFB_START_DISPLAY, stop: int = DEFAULT_XVFB_START_DISPLAY + 60) -> int:
    for number in range(start, stop):
        if not pathlib.Path(f"/tmp/.X11-unix/X{number}").exists():
            return number
    raise RuntimeError("No free Xvfb display numbers were found.")


def ensure_xvfb(display_number: int, width: int, height: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        ["Xvfb", f":{display_number}", "-screen", "0", f"{width}x{height}x24", "-ac", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 5
    socket_path = pathlib.Path(f"/tmp/.X11-unix/X{display_number}")
    while time.time() < deadline:
        if socket_path.exists():
            return proc
        time.sleep(0.1)

    with contextlib.suppress(Exception):
        proc.terminate()
    raise RuntimeError(f"Xvfb did not start on :{display_number}")


def ensure_pulse_server() -> dict[str, str]:
    runtime_dir = (RUNTIME_DIR / "pulse-runtime").resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime_dir, 0o700)

    socket_path = runtime_dir / "native"

    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = str(runtime_dir)
    env["PULSE_SERVER"] = f"unix:{socket_path}"

    if not socket_path.exists():
        subprocess.run(
            [
                "pulseaudio",
                "--daemonize=yes",
                "--exit-idle-time=-1",
                "--disable-shm=yes",
                "--log-target=stderr",
                "--load", f"module-native-protocol-unix auth-anonymous=1 socket={socket_path}",
            ],
            env=env,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.time() + 5
        while time.time() < deadline:
            if socket_path.exists():
                break
            time.sleep(0.1)

    if not socket_path.exists():
        raise RuntimeError("PulseAudio did not start correctly.")

    return env


def pulse_load_module(pulse_env: dict[str, str], *args: str) -> str:
    result = run_command(["pactl", "load-module", *args], env=pulse_env, check=True)
    return (result.stdout or "").strip()


def pulse_unload_module(pulse_env: dict[str, str], module_id: str | None) -> None:
    if not module_id:
        return
    with contextlib.suppress(Exception):
        run_command(["pactl", "unload-module", module_id], env=pulse_env, check=False)


def create_session_audio_devices(session_id: str, pulse_env: dict[str, str]) -> dict[str, t.Any]:
    safe_id = re.sub(r"[^a-zA-Z0-9]", "", session_id)[:12]

    sink_name = f"vortex_{safe_id}_sink"
    sink_module_id = pulse_load_module(
        pulse_env,
        "module-null-sink",
        f"sink_name={sink_name}",
        f"sink_properties=device.description=Vortex-{safe_id}",
    )

    mic_fifo = RUNTIME_DIR / f"{safe_id}.mic.pcm"
    with contextlib.suppress(FileNotFoundError):
        mic_fifo.unlink()
    os.mkfifo(mic_fifo, 0o600)

    mic_source_name = f"vortex_{safe_id}_mic"
    mic_module_id = pulse_load_module(
        pulse_env,
        "module-pipe-source",
        f"file={mic_fifo}",
        f"source_name={mic_source_name}",
        "format=s16le",
        "rate=48000",
        "channels=2",
    )

    return {
        "sink_name": sink_name,
        "sink_module_id": sink_module_id,
        "mic_source_name": mic_source_name,
        "mic_module_id": mic_module_id,
        "mic_fifo": mic_fifo,
    }

def parse_wireguard_config(raw: str) -> dict[str, t.Any]:
    section = ""
    addresses: list[str] = []
    dns_servers: list[str] = []
    mtu: int | None = None
    stripped_lines: list[str] = []

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            stripped_lines.append(raw_line)
            continue

        section_match = re.match(r"^\[(.+?)\]\s*$", line)
        if section_match:
            section = section_match.group(1).strip().lower()
            stripped_lines.append(raw_line)
            continue

        if "=" not in raw_line:
            stripped_lines.append(raw_line)
            continue

        key, value = [x.strip() for x in raw_line.split("=", 1)]
        key_lower = key.lower()

        if section == "interface":
            if key_lower == "address":
                addresses.extend([x.strip() for x in value.split(",") if x.strip()])
                continue
            if key_lower == "dns":
                dns_servers.extend([x.strip() for x in value.split(",") if x.strip()])
                continue
            if key_lower == "mtu":
                try:
                    mtu = int(value)
                except Exception:
                    mtu = None
                continue
            if key_lower in {"table", "preup", "postup", "predown", "postdown", "saveconfig"}:
                continue

        stripped_lines.append(raw_line)

    stripped = "\n".join(stripped_lines).strip() + "\n"
    return {
        "addresses": addresses,
        "dns_servers": dns_servers,
        "mtu": mtu,
        "stripped_config": stripped,
    }


def ensure_tor_service_running(cfg: "NodeConfig") -> None:
    tor_cfg = cfg.egress.get("tor", {})
    socks_url = tor_cfg.get("socks_url", DEFAULT_TOR_SOCKS)
    parsed = urllib.parse.urlparse(socks_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9050

    if which("tor") is None and tor_cfg.get("install_if_missing", True):
        apt_install(["tor"])

    if sys.platform == "linux" and which("systemctl"):
        run_command(["systemctl", "enable", "--now", "tor"], check=False)
        run_command(["systemctl", "enable", "--now", "tor@default"], check=False)

    try:
        with socket.create_connection((host, port), timeout=2):
            return
    except Exception as exc:
        raise RuntimeError(f"Tor SOCKS endpoint is not reachable at {host}:{port}") from exc


def ensure_wireguard_namespace(cfg: "NodeConfig") -> None:
    if sys.platform != "linux":
        raise RuntimeError("WireGuard namespace mode in this implementation is Linux-only.")
    if os.geteuid() != 0:
        raise RuntimeError("WireGuard namespace setup requires sudo/root.")

    if which("ip") is None or which("wg") is None or which("curl") is None:
        apt_install(["iproute2", "wireguard", "wireguard-tools", "curl"])

    wg_cfg = cfg.egress.get("wireguard", {})
    conf_path = pathlib.Path(str(wg_cfg.get("config_path", ""))).expanduser().resolve()
    if not conf_path.exists():
        raise RuntimeError(f"WireGuard config file not found: {conf_path}")

    ns_name = str(wg_cfg.get("namespace_name") or DEFAULT_WG_NAMESPACE)
    iface = str(wg_cfg.get("interface_name") or DEFAULT_WG_INTERFACE)

    parsed = parse_wireguard_config(conf_path.read_text("utf-8"))
    stripped_conf_path = WIREGUARD_DIR / f"{iface}.setconf.conf"
    write_private_text(stripped_conf_path, parsed["stripped_config"])

    run_command(["ip", "netns", "add", ns_name], check=False)
    run_command(["ip", "-n", ns_name, "link", "set", "lo", "up"], check=False)

    run_command(["ip", "link", "del", iface], check=False)
    run_command(["ip", "-n", ns_name, "link", "del", iface], check=False)

    run_command(["ip", "link", "add", iface, "type", "wireguard"], check=True)
    run_command(["ip", "link", "set", iface, "netns", ns_name], check=True)
    run_command(["ip", "netns", "exec", ns_name, "wg", "setconf", iface, str(stripped_conf_path)], check=True)

    for address in parsed["addresses"]:
        run_command(["ip", "-n", ns_name, "address", "add", address, "dev", iface], check=False)

    if parsed["mtu"]:
        run_command(["ip", "-n", ns_name, "link", "set", "mtu", str(parsed["mtu"]), "dev", iface], check=False)

    run_command(["ip", "-n", ns_name, "link", "set", iface, "up"], check=True)
    run_command(["ip", "-n", ns_name, "route", "replace", "default", "dev", iface], check=True)

    if parsed["dns_servers"]:
        resolv_dir = pathlib.Path("/etc/netns") / ns_name
        resolv_dir.mkdir(parents=True, exist_ok=True)
        (resolv_dir / "resolv.conf").write_text(
            "".join(f"nameserver {server}\n" for server in parsed["dns_servers"]),
            "utf-8",
        )


def build_netns_browser_wrapper(namespace_name: str, browser_path: str) -> str:
    WRAPPER_DIR.mkdir(parents=True, exist_ok=True)
    wrapper_path = WRAPPER_DIR / f"chromium-{namespace_name}"
    wrapper_path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"exec ip netns exec {shlex_quote(namespace_name)} {shlex_quote(browser_path)} \"$@\"\n",
        "utf-8",
    )
    os.chmod(wrapper_path, 0o700)
    return str(wrapper_path)


def parse_header_dump(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    blocks = [b for b in re.split(r"\r?\n\r?\n", raw) if b.strip()]
    if not blocks:
        return {}
    last = blocks[-1]
    headers: dict[str, str] = {}
    for line in last.splitlines()[1:]:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        headers[k.strip().lower()] = v.strip()
    return headers


def curl_fetch_via_namespace(
    namespace_name: str,
    *,
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    follow_redirects: bool = True,
    timeout: float = 30.0,
) -> SimpleUpstreamResponse:
    headers = headers or {}
    with tempfile.TemporaryDirectory(prefix="vortex-node-curl-") as tmp_dir:
        tmp = pathlib.Path(tmp_dir)
        header_path = tmp / "headers.txt"
        body_path = tmp / "body.bin"
        upload_path = tmp / "upload.bin"

        cmd = [
            "ip", "netns", "exec", namespace_name,
            "curl",
            "--silent",
            "--show-error",
            "--request", method.upper(),
            "--dump-header", str(header_path),
            "--output", str(body_path),
            "--write-out", "%{http_code}",
            "--max-time", str(int(timeout)),
        ]

        if follow_redirects:
            cmd.append("--location")

        for key, value in headers.items():
            cmd.extend(["-H", f"{key}: {value}"])

        if body is not None:
            upload_path.write_bytes(body)
            cmd.extend(["--data-binary", f"@{upload_path}"])

        cmd.append(url)

        result = run_command(cmd, check=False, capture_output=True, text=True)
        status_text = (result.stdout or "").strip()
        try:
            status_code = int(status_text[-3:])
        except Exception:
            status_code = 599

        content = body_path.read_bytes() if body_path.exists() else b""
        response_headers = parse_header_dump(header_path.read_text("utf-8", errors="ignore")) if header_path.exists() else {}

        return SimpleUpstreamResponse(status_code=status_code, content=content, headers=response_headers)


def build_totp_qr_data_url(otpauth_uri: str) -> str | None:
    if qrcode is None:
        return None
    try:
        image = qrcode.make(otpauth_uri)
        out = io.BytesIO()
        image.save(out, format="PNG")
        return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")
    except Exception:
        return None


def sanitize_username_for_path(username: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(username or "").strip())
    return clean or "owner"

def synced_blob_path(username: str) -> pathlib.Path:
    return SYNCED_BLOB_DIR / f"{sanitize_username_for_path(username)}.json"

def strip_ansi_codes(text: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text or "")

def configure_public_https(cfg: "NodeConfig", certbot_email: str) -> None:
    if sys.platform != "linux":
        raise RuntimeError("Automatic Nginx/Certbot setup is only implemented for Linux.")
    if os.geteuid() != 0:
        raise RuntimeError("Run the installer with sudo/root to configure Nginx and HTTPS automatically.")

    apt_install(["nginx", "certbot", "python3-certbot-nginx"])

    public_base = str(cfg.server.get("public_base_url", "")).strip()
    parsed = urllib.parse.urlparse(public_base)
    domain = parsed.hostname or ""
    if not domain or domain in {"localhost", "127.0.0.1"}:
        raise RuntimeError("A real public domain is required for automatic HTTPS setup.")

    upstream = f"http://{cfg.server.get('host', '127.0.0.1')}:{int(cfg.server.get('port', 8787))}"
    site_path = pathlib.Path("/etc/nginx/sites-available/vortex-node")
    enabled_path = pathlib.Path("/etc/nginx/sites-enabled/vortex-node")

    nginx_conf = textwrap.dedent(f"""
    map $http_upgrade $connection_upgrade {{
        default upgrade;
        ''      close;
    }}

    server {{
        listen 80;
        server_name {domain};

        client_max_body_size 64m;

        location / {{
            proxy_pass {upstream};
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
        }}
    }}
    """).strip() + "\n"

    site_path.write_text(nginx_conf, "utf-8")
    if enabled_path.exists() or enabled_path.is_symlink():
        enabled_path.unlink()
    enabled_path.symlink_to(site_path)

    default_site = pathlib.Path("/etc/nginx/sites-enabled/default")
    if default_site.exists() or default_site.is_symlink():
        with contextlib.suppress(Exception):
            default_site.unlink()

    run_command(["nginx", "-t"], check=True)
    run_command(["systemctl", "enable", "--now", "nginx"], check=False)
    run_command(["systemctl", "reload", "nginx"], check=False)

    email = str(certbot_email or "").strip()
    if not email:
        email = f"admin@{domain}"

    run_command([
        "certbot",
        "--nginx",
        "--non-interactive",
        "--agree-tos",
        "--redirect",
        "-m", email,
        "-d", domain,
    ], check=False)

@dataclasses.dataclass
class TerminalSessionState:
    session_id: str
    username: str
    cwd: str
    created_at: str
    updated_at: str

class TerminalRuntime:
    def __init__(self) -> None:
        self.sessions: dict[str, TerminalSessionState] = {}
        self._lock = asyncio.Lock()

    async def create_session(self, username: str) -> TerminalSessionState:
        session = TerminalSessionState(
            session_id=secrets.token_urlsafe(12),
            username=str(username or "owner"),
            cwd=str(pathlib.Path.home()),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        async with self._lock:
            self.sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> TerminalSessionState:
        session = self.sessions.get(session_id)
        if not session:
            raise HTTPException(404, "Unknown terminal session")
        return session

    async def close_session(self, session_id: str) -> None:
        async with self._lock:
            self.sessions.pop(session_id, None)

    async def exec(self, session_id: str, command: str) -> dict[str, t.Any]:
        session = self.get(session_id)
        command = str(command or "").rstrip()
        if not command:
            return {
                "ok": True,
                "cwd": session.cwd,
                "output": "",
                "exit_code": 0,
                "session_id": session.session_id,
            }

        script = (
            f"cd {shlex_quote(session.cwd)}\n"
            f"{command}\n"
            "printf '\\n__VORTEX_CWD__=%s\\n' \"$PWD\"\n"
        )

        proc = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-lc",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        stdout, stderr = await proc.communicate()

        combined = (stdout or b"").decode("utf-8", "replace") + (stderr or b"").decode("utf-8", "replace")
        combined = strip_ansi_codes(combined)

        marker = "__VORTEX_CWD__="
        new_cwd = session.cwd
        idx = combined.rfind(marker)
        if idx != -1:
            tail = combined[idx + len(marker):].strip()
            tail_line = tail.splitlines()[0].strip() if tail else ""
            if tail_line:
                new_cwd = tail_line
            combined = combined[:idx].rstrip() + ("\n" if combined[:idx].strip() else "")

        session.cwd = new_cwd
        session.updated_at = utc_now()

        return {
            "ok": proc.returncode == 0,
            "cwd": session.cwd,
            "output": combined,
            "exit_code": int(proc.returncode or 0),
            "session_id": session.session_id,
        }

class FrameSocketHub:
    def __init__(self) -> None:
        self._sockets: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._sockets[session_id].add(websocket)

    async def disconnect(self, session_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._sockets[session_id].discard(websocket)
            if not self._sockets[session_id]:
                self._sockets.pop(session_id, None)

    async def broadcast(self, session_id: str, payload: bytes) -> None:
        async with self._lock:
            sockets = list(self._sockets.get(session_id, set()))
        dead: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_bytes(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._sockets[session_id].discard(ws)

def paired_devices(cfg_raw: dict[str, t.Any]) -> list[dict[str, t.Any]]:
    auth_cfg = cfg_raw.setdefault("auth", {})
    return auth_cfg.setdefault("paired_devices", [])


def find_paired_device(cfg_raw: dict[str, t.Any], device_id: str) -> dict[str, t.Any] | None:
    for item in paired_devices(cfg_raw):
        if item.get("device_id") == device_id:
            return item
    return None


def touch_paired_device(cfg_raw: dict[str, t.Any], device_id: str, label: str) -> dict[str, t.Any]:
    item = find_paired_device(cfg_raw, device_id)
    if item is None:
        item = {
            "device_id": device_id,
            "label": label,
            "created_at": utc_now(),
            "last_seen_at": utc_now(),
        }
        paired_devices(cfg_raw).append(item)
    else:
        item["label"] = label or item.get("label", "")
        item["last_seen_at"] = utc_now()
    return item


def revoke_paired_device(cfg_raw: dict[str, t.Any], device_id: str) -> bool:
    devices = paired_devices(cfg_raw)
    before = len(devices)
    devices[:] = [d for d in devices if d.get("device_id") != device_id]
    return len(devices) != before

def ask_install_config() -> dict[str, t.Any]:
    ensure_dirs()
    print(f"\n== {APP_NAME} installer ==\n")
    print("This installer configures the node, frontend serving, routing mode, browser behavior, updates, and optional HTTPS automation.")
    print("Run it with sudo on Ubuntu if you want automatic WireGuard/Tor/systemd/Nginx/Certbot setup.\n")

    deployment_mode = prompt_choice(
        "How should this node be reached?",
        [
            ("lan", "LAN only / local network"),
            ("public", "Public domain over HTTPS"),
        ],
        default_key="lan",
    )

    bind_mode = prompt_choice(
        "Where should the Python server bind?",
        [
            ("localhost", "127.0.0.1 only (recommended behind Nginx/Certbot)"),
            ("lan", "0.0.0.0 for LAN / direct access"),
        ],
        default_key="localhost" if deployment_mode == "public" else "lan",
    )
    if deployment_mode == "public":
        bind_mode = "localhost"

    host = "127.0.0.1" if bind_mode == "localhost" else "0.0.0.0"
    port = int(prompt("Node port", default="8787"))

    public_base_default = "https://node.example.com" if deployment_mode == "public" else f"http://{local_ip_guess()}:{port}"
    public_base_url = prompt(
        "Public base URL (example: https://node.example.com or http://192.168.1.20:8787)",
        default=public_base_default,
    ).rstrip("/")

    serve_ui = prompt_yes_no("Serve the Vortex OS HTML file from this node root (/)?", default=True)
    ui_html_path = ""
    if serve_ui:
        ui_html_path = prompt(
            "Path to the Vortex OS HTML file to serve",
            default=str(DEFAULT_UI_HTML.resolve()),
        )

    auto_https = False
    certbot_email = ""
    if deployment_mode == "public":
        auto_https = prompt_yes_no("Automatically configure Nginx and HTTPS with Certbot?", default=True)
        if auto_https:
            suggested_email = f"admin@{urllib.parse.urlparse(public_base_url).hostname or 'example.com'}"
            certbot_email = prompt("Email for Certbot / Let's Encrypt", default=suggested_email)

    print("\nCreate the first node owner account.")
    while True:
        username = prompt("Node username", default=os.environ.get("SUDO_USER") or os.environ.get("USER") or "owner")
        if username.strip().lower() == "admin":
            print("Choose a non-default username. Do not use 'admin'.\n")
            continue
        if len(username.strip()) < 3:
            print("Use a longer username.\n")
            continue
        break

    while True:
        password = prompt("Node password", secret=True)
        password2 = prompt("Confirm node password", secret=True)
        if password != password2:
            print("Passwords do not match. Try again.\n")
            continue
        if len(password) < 12:
            print("Use a stronger password (12+ characters).\n")
            continue
        break

    route_mode = prompt_choice(
        "Default worker route mode",
        [
            ("direct", "Direct: use the host's normal network"),
            ("wireguard", "WireGuard: only node browser/fetch traffic goes through a WireGuard tunnel"),
            ("tor", "Tor: only node browser/fetch traffic goes through Tor"),
        ],
        default_key="direct",
    )

    wireguard_cfg: dict[str, t.Any] = {
        "enabled": False,
        "config_path": "",
        "interface_name": DEFAULT_WG_INTERFACE,
        "namespace_name": DEFAULT_WG_NAMESPACE,
        "auto_configure": False,
    }
    tor_cfg: dict[str, t.Any] = {
        "enabled": False,
        "socks_url": DEFAULT_TOR_SOCKS,
        "install_if_missing": True,
    }

    if route_mode == "wireguard":
        wireguard_cfg["enabled"] = True
        wireguard_cfg["config_path"] = prompt("Path to WireGuard .conf file", default="/etc/wireguard/wg0.conf")
        wireguard_cfg["interface_name"] = prompt("Worker WireGuard interface name", default=DEFAULT_WG_INTERFACE)
        wireguard_cfg["namespace_name"] = prompt("Worker network namespace name", default=DEFAULT_WG_NAMESPACE)
        wireguard_cfg["auto_configure"] = prompt_yes_no("Configure the worker namespace + WireGuard automatically now?", default=True)
    elif route_mode == "tor":
        tor_cfg["enabled"] = True
        tor_cfg["socks_url"] = prompt("Tor SOCKS URL", default=DEFAULT_TOR_SOCKS)
        tor_cfg["install_if_missing"] = prompt_yes_no("Install/start Tor automatically if needed?", default=True)

    browser_mode = prompt_choice(
        "Default browser virtualization mode",
        [
            ("hybrid", "Translation first, automatic live remote fallback"),
            ("translate", "Always prefer translated/local render"),
            ("stream", "Always prefer live remote mode"),
        ],
        default_key="hybrid",
    )

    max_sessions = int(prompt("Maximum simultaneous browser windows", default="4"))
    max_tabs_per_session = int(prompt("Maximum tabs per browser window", default=str(MAX_TABS_PER_SESSION)))
    max_clients = int(prompt("Maximum simultaneous API/browser clients", default="12"))

    frame_ancestors = parse_origin_list(prompt(
        "Allowed Vortex OS origins (comma-separated)",
        default=f"{public_base_url},http://localhost:8080,http://127.0.0.1:8080",
    ))
    cors_origins = frame_ancestors[:] if frame_ancestors else [public_base_url]

    run_on_boot = prompt_yes_no("Install a systemd service (Linux only, requires sudo)?", default=True)
    use_tmux = prompt_yes_no("Use tmux helpers for foreground/background management?", default=True)

    updates_cfg = {
        "enabled": prompt_yes_no("Check GitHub for backend/frontend updates on startup?", default=True),
        "owner": DEFAULT_UPDATE_OWNER,
        "repo": DEFAULT_UPDATE_REPO,
        "branch": DEFAULT_UPDATE_BRANCH,
        "backend_path": DEFAULT_REMOTE_BACKEND_PATH,
        "frontend_path": DEFAULT_REMOTE_FRONTEND_PATH,
    }

    cfg = {
        "version": 4,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "server": {
            "host": host,
            "port": port,
            "public_base_url": public_base_url,
            "allowed_origins": cors_origins,
            "frame_ancestors": frame_ancestors,
            "max_clients": max_clients,
            "exposure_mode": deployment_mode,
        },
        "frontend": {
            "serve_ui": serve_ui,
            "ui_html_path": ui_html_path,
        },
        "auth": {
            "username": username,
            "password_hash": hash_password(password),
            "secret_key": secrets.token_urlsafe(48),
            "cookie_secret": secrets.token_urlsafe(48),
            "access_token_ttl_seconds": 60 * 60 * 12,
            "pair_token_ttl_seconds": PAIR_TOKEN_TTL_SECONDS,
            "embed_ticket_ttl_seconds": 60 * 10,
            "login_rate_limit_per_15m": 20,
            "api_rate_limit_per_minute": 240,
            "totp": {
                "enabled": False,
                "secret": "",
                "issuer": APP_NAME,
            },
            "paired_devices": [],
        },
        "egress": {
            "route_mode": route_mode,
            "wireguard": wireguard_cfg,
            "tor": tor_cfg,
        },
        "browser": {
            "mode": browser_mode,
            "default_route_mode": route_mode,
            "max_sessions": max_sessions,
            "max_tabs_per_session": max_tabs_per_session,
            "viewport": {"width": 1366, "height": 900},
            "user_agent": "",
            "block_aggressive_popups": True,
            "strip_common_junk": True,
            "allow_media_proxy": True,
            "screenshot_quality": 85,
            "screenshot_fps": 30,
            "remote_width_cap": DEFAULT_REMOTE_WIDTH_CAP,
            "remote_height_cap": DEFAULT_REMOTE_HEIGHT_CAP,
            "detection": {
                "heavy_dom_threshold": 5000,
                "heavy_script_threshold": 32,
                "canvas_threshold": 2,
            },
        },
        "features": {
            "translated_render": True,
            "stream_fallback": True,
            "media_proxy": True,
            "tor_mode": True,
            "wireguard_mode": True,
            "basic_sanitization": True,
            "pair_tokens": True,
            "totp": True,
            "frontend_serving": True,
            "terminal": True,
            "node_blob_sync": True,
        },
        "updates": updates_cfg,
        "ops": {
            "use_tmux": use_tmux,
            "run_on_boot": run_on_boot,
            "auto_https": auto_https,
            "certbot_email": certbot_email,
        },
    }

    write_json(CONFIG_PATH, cfg)

    if route_mode == "wireguard" and wireguard_cfg.get("auto_configure"):
        print("\nConfiguring worker-only WireGuard namespace now...")
        ensure_wireguard_namespace(NodeConfig(cfg))
    elif route_mode == "tor" and tor_cfg.get("install_if_missing", True):
        print("\nEnsuring Tor is available for node-only traffic now...")
        ensure_tor_service_running(NodeConfig(cfg))

    return cfg

class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.time()
        async with self._lock:
            bucket = self._buckets[key]
            while bucket and bucket[0] <= now - window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(window_seconds - (now - bucket[0])))
                return False, retry_after
            bucket.append(now)
            return True, 0


@dataclasses.dataclass
class NodeConfig:
    raw: dict[str, t.Any]

    @property
    def server(self) -> dict[str, t.Any]:
        return self.raw["server"]

    @property
    def auth(self) -> dict[str, t.Any]:
        return self.raw["auth"]

    @property
    def egress(self) -> dict[str, t.Any]:
        return self.raw["egress"]

    @property
    def browser(self) -> dict[str, t.Any]:
        return self.raw["browser"]


class AuthManager:
    def __init__(self, cfg: NodeConfig) -> None:
        self.cfg = cfg

    def issue_access_token(self, subject: str, *, device_id: str = "") -> str:
        ttl = int(self.cfg.auth.get("access_token_ttl_seconds", 3600))
        payload = {
            "sub": subject,
            "kind": "access",
            "device_id": device_id,
            "iat": int(time.time()),
            "exp": int(time.time()) + ttl,
            "jti": secrets.token_urlsafe(16),
        }
        return sign_token(self.cfg.auth["secret_key"], payload)

    def issue_pair_token(self, subject: str, *, device_id: str) -> str:
        ttl = int(self.cfg.auth.get("pair_token_ttl_seconds", PAIR_TOKEN_TTL_SECONDS))
        payload = {
            "sub": subject,
            "kind": "pair",
            "device_id": device_id,
            "iat": int(time.time()),
            "exp": int(time.time()) + ttl,
            "jti": secrets.token_urlsafe(16),
        }
        return sign_token(self.cfg.auth["secret_key"], payload)

    def issue_embed_ticket(self, subject: str, *, session_id: str) -> str:
        ttl = int(self.cfg.auth.get("embed_ticket_ttl_seconds", 600))
        payload = {
            "sub": subject,
            "kind": "embed",
            "session_id": session_id,
            "iat": int(time.time()),
            "exp": int(time.time()) + ttl,
            "jti": secrets.token_urlsafe(16),
        }
        return sign_token(self.cfg.auth["secret_key"], payload)

    def require_access_token(self, token: str) -> dict[str, t.Any]:
        payload = verify_token(self.cfg.auth["secret_key"], token)
        if payload.get("kind") != "access":
            raise ValueError("wrong token kind")
        return payload

    def require_pair_token(self, token: str) -> dict[str, t.Any]:
        payload = verify_token(self.cfg.auth["secret_key"], token)
        if payload.get("kind") != "pair":
            raise ValueError("wrong token kind")
        return payload

    def require_embed_ticket(self, ticket: str, *, session_id: str) -> dict[str, t.Any]:
        payload = verify_token(self.cfg.auth["secret_key"], ticket)
        if payload.get("kind") != "embed":
            raise ValueError("wrong ticket kind")
        if payload.get("session_id") != session_id:
            raise ValueError("wrong session")
        return payload

    def totp_enabled(self) -> bool:
        return bool(self.cfg.auth.get("totp", {}).get("enabled"))

    def verify_totp(self, otp: str) -> bool:
        if not self.totp_enabled():
            return True
        if pyotp is None:
            return False
        secret = str(self.cfg.auth.get("totp", {}).get("secret", "")).strip()
        if not secret:
            return False
        return pyotp.TOTP(secret).verify(str(otp or "").strip(), valid_window=1)

    def build_totp_setup(self) -> dict[str, t.Any]:
        if pyotp is None:
            raise RuntimeError("Install pyotp to use TOTP 2FA.")
        totp_cfg = self.cfg.auth.setdefault("totp", {})
        secret = str(totp_cfg.get("secret") or "").strip()
        if not secret:
            secret = pyotp.random_base32()
            totp_cfg["secret"] = secret
        issuer = str(totp_cfg.get("issuer") or APP_NAME)
        username = str(self.cfg.auth.get("username", "owner"))
        uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)
        return {
            "secret": secret,
            "issuer": issuer,
            "username": username,
            "provisioning_uri": uri,
            "qr_data_url": build_totp_qr_data_url(uri),
        }


class EgressManager:
    def __init__(self, cfg: NodeConfig):
        self.cfg = cfg

    def default_mode(self) -> str:
        mode = str(self.cfg.egress.get("route_mode", "direct")).lower()
        return mode if mode in {"direct", "wireguard", "tor"} else "direct"

    def normalize_mode(self, route_mode: str | None) -> str:
        mode = str(route_mode or self.default_mode()).lower()
        if mode == "default":
            return self.default_mode()
        return mode if mode in {"direct", "wireguard", "tor"} else self.default_mode()

    def tor_socks(self) -> str:
        return str(self.cfg.egress.get("tor", {}).get("socks_url", DEFAULT_TOR_SOCKS))

    async def ensure_ready(self, route_mode: str | None = None) -> None:
        mode = self.normalize_mode(route_mode)
        if mode == "wireguard":
            await asyncio.to_thread(ensure_wireguard_namespace, self.cfg)
        elif mode == "tor":
            await asyncio.to_thread(ensure_tor_service_running, self.cfg)

    def describe(self, route_mode: str | None = None) -> dict[str, t.Any]:
        mode = self.normalize_mode(route_mode)
        data = {"mode": mode}
        if mode == "wireguard":
            wg_cfg = self.cfg.egress.get("wireguard", {})
            data.update({
                "namespace_name": wg_cfg.get("namespace_name", DEFAULT_WG_NAMESPACE),
                "interface_name": wg_cfg.get("interface_name", DEFAULT_WG_INTERFACE),
                "config_path": wg_cfg.get("config_path", ""),
            })
        elif mode == "tor":
            data.update({
                "socks_url": self.tor_socks(),
            })
        return data

    def playwright_launch_options(self, route_mode: str | None, browser_type: t.Any) -> dict[str, t.Any]:
        mode = self.normalize_mode(route_mode)
        if mode == "tor":
            return {"proxy": {"server": self.tor_socks()}}
        if mode == "wireguard":
            namespace_name = str(self.cfg.egress.get("wireguard", {}).get("namespace_name", DEFAULT_WG_NAMESPACE))
            wrapper_path = build_netns_browser_wrapper(namespace_name, browser_type.executable_path)
            return {"executable_path": wrapper_path}
        return {}

    async def fetch(
        self,
        route_mode: str | None,
        *,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        follow_redirects: bool = True,
        timeout: float = 30.0,
    ) -> SimpleUpstreamResponse:
        mode = self.normalize_mode(route_mode)
        headers = headers or {}
        if mode == "wireguard":
            namespace_name = str(self.cfg.egress.get("wireguard", {}).get("namespace_name", DEFAULT_WG_NAMESPACE))
            return await asyncio.to_thread(
                curl_fetch_via_namespace,
                namespace_name,
                url=url,
                method=method,
                headers=headers,
                body=body,
                follow_redirects=follow_redirects,
                timeout=timeout,
            )

        proxy = self.tor_socks() if mode == "tor" else None
        return await httpx_fetch(
            url=url,
            method=method,
            headers=headers,
            body=body,
            proxy=proxy,
            follow_redirects=follow_redirects,
            timeout=timeout,
        )


async def httpx_fetch(
    *,
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    proxy: str | None = None,
    follow_redirects: bool = True,
    timeout: float = 30.0,
) -> SimpleUpstreamResponse:
    if httpx is None:
        raise RuntimeError("httpx is required")
    async with httpx.AsyncClient(proxy=proxy, follow_redirects=follow_redirects, timeout=timeout, verify=True) as client:
        response = await client.request(method, url, headers=headers, content=body)
        return SimpleUpstreamResponse(
            status_code=response.status_code,
            content=response.content,
            headers={str(k).lower(): str(v) for k, v in response.headers.items()},
        )


class SessionWebSocketHub:
    def __init__(self) -> None:
        self._sockets: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._sockets[session_id].add(websocket)

    async def disconnect(self, session_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._sockets[session_id].discard(websocket)
            if not self._sockets[session_id]:
                self._sockets.pop(session_id, None)

    async def broadcast(self, session_id: str, message: dict[str, t.Any]) -> None:
        async with self._lock:
            sockets = list(self._sockets.get(session_id, set()))
        dead: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._sockets[session_id].discard(ws)

async def analyze_page_profile(page: Page, cfg: NodeConfig) -> dict[str, t.Any]:
    url = page.url if getattr(page, "url", None) else ""
    host = urllib.parse.urlparse(url).hostname or ""

    metrics = await page.evaluate(
        """async () => {
            const iframes = Array.from(document.querySelectorAll('iframe'));
            let crossOriginIframes = 0;
            for (const frame of iframes) {
                try { void frame.contentWindow.location.href; } catch { crossOriginIframes += 1; }
            }

            let serviceWorkerCount = 0;
            if (navigator.serviceWorker && navigator.serviceWorker.getRegistrations) {
                try {
                    const regs = await navigator.serviceWorker.getRegistrations();
                    serviceWorkerCount = regs.length;
                } catch {}
            }

            const videos = document.querySelectorAll('video').length;
            const audios = document.querySelectorAll('audio').length;
            const canvases = document.querySelectorAll('canvas').length;
            const scripts = document.scripts.length;
            const nodes = document.getElementsByTagName('*').length;
            const hasMediaSource = typeof window.MediaSource !== 'undefined';
            const hasWebRTC = !!(window.RTCPeerConnection || window.webkitRTCPeerConnection || window.mozRTCPeerConnection);
            const hasGetUserMedia = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
            const webglContexts = Array.from(document.querySelectorAll('canvas')).length;

            return {
                videos,
                audios,
                canvases,
                scripts,
                nodes,
                iframe_count: iframes.length,
                cross_origin_iframes: crossOriginIframes,
                service_worker_count: serviceWorkerCount,
                has_media_source: hasMediaSource,
                has_webrtc: hasWebRTC,
                has_getusermedia: hasGetUserMedia,
                webgl_contexts: webglContexts,
            };
        }"""
    )

    heavy_dom_threshold = int(cfg.browser.get("detection", {}).get("heavy_dom_threshold", 5000))
    heavy_script_threshold = int(cfg.browser.get("detection", {}).get("heavy_script_threshold", 32))
    canvas_threshold = int(cfg.browser.get("detection", {}).get("canvas_threshold", 2))

    stream_hosts = {
        "youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be",
        "discord.com", "www.discord.com",
        "open.spotify.com",
        "www.netflix.com", "netflix.com",
        "www.twitch.tv", "twitch.tv",
        "meet.google.com",
        "web.whatsapp.com",
    }

    recommended_mode = "translate"
    remote_reason = ""

    if any(host == h or host.endswith(f".{h}") for h in stream_hosts):
        recommended_mode = "stream"
        remote_reason = "known-media-host"
    elif metrics["videos"] > 0 or metrics["audios"] > 0 or metrics["has_media_source"]:
        recommended_mode = "stream"
        remote_reason = "media-runtime"
    elif metrics["has_webrtc"] or metrics["has_getusermedia"]:
        recommended_mode = "stream"
        remote_reason = "realtime-media"
    elif metrics["cross_origin_iframes"] > 0 or metrics["service_worker_count"] > 0:
        recommended_mode = "hybrid"
        remote_reason = "complex-app-shell"
    elif metrics["nodes"] >= heavy_dom_threshold or metrics["scripts"] >= heavy_script_threshold or metrics["canvases"] >= canvas_threshold:
        recommended_mode = "hybrid"
        remote_reason = "heavy-dom"

    metrics["host"] = host
    metrics["url"] = url
    metrics["recommended_mode"] = recommended_mode
    metrics["remote_reason"] = remote_reason
    return metrics

@dataclasses.dataclass
class BrowserTab:
    tab_id: str
    page: Page
    title: str = "New Tab"
    url: str = "about:blank"


class BrowserRuntime:
    def __init__(self, cfg: NodeConfig) -> None:
        self.cfg = cfg
        self.playwright: Playwright | None = None
        self.hub = SessionWebSocketHub()
        self.frame_hub = FrameSocketHub()
        self.sessions: dict[str, "BrowserSession"] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if async_playwright is None:
            raise RuntimeError("playwright is required")
        self.playwright = await async_playwright().start()

    async def stop(self) -> None:
        sessions = list(self.sessions.values())
        for session in sessions:
            with contextlib.suppress(Exception):
                await session.close()
        self.sessions.clear()
        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None

    async def create_session(
        self,
        *,
        url: str,
        mode: str,
        route_mode: str,
        egress: EgressManager,
    ) -> "BrowserSession":
        async with self._lock:
            max_sessions = int(self.cfg.browser.get("max_sessions", 4))
            if len(self.sessions) >= max_sessions:
                raise HTTPException(429, f"Maximum browser windows reached ({max_sessions})")
            session_id = secrets.token_urlsafe(12)
            session = BrowserSession(
                runtime=self,
                session_id=session_id,
                target_url=url,
                requested_mode=mode,
                route_mode=route_mode,
                egress=egress,
            )
            self.sessions[session_id] = session
        await session.start()
        return session

    def get(self, session_id: str) -> "BrowserSession":
        session = self.sessions.get(session_id)
        if not session:
            raise HTTPException(404, "Unknown session")
        return session

    async def remove(self, session_id: str) -> None:
        async with self._lock:
            self.sessions.pop(session_id, None)


class BrowserSession:
    def __init__(
        self,
        *,
        runtime: BrowserRuntime,
        session_id: str,
        target_url: str,
        requested_mode: str,
        route_mode: str,
        egress: EgressManager,
    ) -> None:
        self.runtime = runtime
        self.cfg = runtime.cfg
        self.session_id = session_id
        self.target_url = target_url
        self.requested_mode = requested_mode
        self.route_mode = route_mode
        self.effective_mode = requested_mode
        self.egress = egress

        self.browser: Browser | None = None
        self.context: BrowserContext | None = None

        self.tabs: dict[str, Page] = {}
        self.tab_order: list[str] = []
        self.tab_meta: dict[str, dict[str, str]] = {}
        self.active_tab_id: str = ""

        self.created_at = utc_now()
        self.updated_at = utc_now()
        self.last_render_html = ""
        self.last_title = ""
        self.last_url = target_url
        self.last_status = "creating"
        self.viewer_token = secrets.token_urlsafe(24)
        self.closed = False
        self.analysis: dict[str, t.Any] = {}
        self.translation_event_counts: dict[str, int] = defaultdict(int)
        self.force_stream_reason = ""

        self._html_lock = asyncio.Lock()
        self._frame_bytes: bytes | None = None
        self._frame_event = asyncio.Event()
        self._stream_cdp: t.Any = None
        self._stream_active_tab_id = ""

    @property
    def active_page(self) -> Page:
        page = self.tabs.get(self.active_tab_id)
        if page is None:
            raise HTTPException(404, "No active tab")
        return page

    def tabs_payload(self) -> list[dict[str, t.Any]]:
        payload: list[dict[str, t.Any]] = []
        for tab_id in self.tab_order:
            meta = self.tab_meta.get(tab_id, {})
            payload.append({
                "tab_id": tab_id,
                "title": meta.get("title") or "New Tab",
                "url": meta.get("url") or "about:blank",
                "active": tab_id == self.active_tab_id,
            })
        return payload

    async def _refresh_tab_meta(self, tab_id: str) -> None:
        page = self.tabs.get(tab_id)
        if page is None:
            return
        try:
            title = await page.title()
        except Exception:
            title = self.tab_meta.get(tab_id, {}).get("title", "New Tab")
        try:
            url = page.url or "about:blank"
        except Exception:
            url = self.tab_meta.get(tab_id, {}).get("url", "about:blank")
        self.tab_meta[tab_id] = {
            "title": title or "New Tab",
            "url": url or "about:blank",
        }

    async def _wire_page_events(self, page: Page, tab_id: str) -> None:
        async def on_console(msg):
            text = msg.text[:400]
            if getattr(msg, "type", "") == "error":
                self.translation_event_counts["console_error"] += 1
                if self.translation_event_counts["console_error"] >= 3:
                    self.force_stream_reason = "console-error-threshold"
                    if tab_id == self.active_tab_id:
                        await self.refresh_render(reason="console-error")
            await self.runtime.hub.broadcast(self.session_id, {
                "type": "console",
                "tab_id": tab_id,
                "text": text,
            })

        async def on_load():
            await self._refresh_tab_meta(tab_id)
            if tab_id == self.active_tab_id:
                await self.refresh_render(reason="load")

        async def on_domcontentloaded():
            await self._refresh_tab_meta(tab_id)
            if tab_id == self.active_tab_id:
                await self.refresh_render(reason="domcontentloaded")

        async def on_popup(popup_page):
            if len(self.tabs) >= int(self.cfg.browser.get("max_tabs_per_session", MAX_TABS_PER_SESSION)):
                with contextlib.suppress(Exception):
                    await popup_page.close()
                return
            await self._create_tab_page(page=popup_page, url=popup_page.url or "about:blank", make_active=True)
            await self.refresh_render(reason="popup")

        page.on("console", lambda msg: asyncio.create_task(on_console(msg)))
        page.on("load", lambda: asyncio.create_task(on_load()))
        page.on("domcontentloaded", lambda: asyncio.create_task(on_domcontentloaded()))
        page.on("popup", lambda popup: asyncio.create_task(on_popup(popup)))

    async def _create_tab_page(self, *, url: str = "about:blank", make_active: bool = True, page: Page | None = None) -> str:
        if len(self.tabs) >= int(self.cfg.browser.get("max_tabs_per_session", MAX_TABS_PER_SESSION)):
            raise HTTPException(429, f"Maximum tabs reached ({int(self.cfg.browser.get('max_tabs_per_session', MAX_TABS_PER_SESSION))})")

        assert self.context is not None
        page = page or await self.context.new_page()
        tab_id = secrets.token_urlsafe(8)
        self.tabs[tab_id] = page
        self.tab_order.append(tab_id)
        self.tab_meta[tab_id] = {"title": "New Tab", "url": "about:blank"}
        await self._wire_page_events(page, tab_id)

        if make_active:
            self.active_tab_id = tab_id

        if url and url != "about:blank":
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("load", timeout=15000)

        await self._refresh_tab_meta(tab_id)
        return tab_id

    async def start(self) -> None:
        assert self.runtime.playwright is not None
        await self.egress.ensure_ready(self.route_mode)

        chromium = self.runtime.playwright.chromium
        viewport = self.cfg.browser.get("viewport", {"width": 1366, "height": 900})

        launch_kwargs: dict[str, t.Any] = {
            "headless": True,
            "chromium_sandbox": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-infobars",
            ],
        }
        launch_kwargs.update(self.egress.playwright_launch_options(self.route_mode, chromium))

        self.browser = await chromium.launch(**launch_kwargs)
        self.context = await self.browser.new_context(
            viewport=viewport,
            ignore_https_errors=False,
            java_script_enabled=True,
            bypass_csp=False,
            accept_downloads=False,
            user_agent=str(self.cfg.browser.get("user_agent") or "") or None,
        )

        await self._create_tab_page(url=self.target_url, make_active=True)
        self.last_status = "starting"
        await self.refresh_render(reason="initial")

    async def create_tab(self, url: str = "about:blank") -> str:
        tab_id = await self._create_tab_page(url=url, make_active=True)
        await self.refresh_render(reason="new_tab")
        return tab_id

    async def activate_tab(self, tab_id: str) -> None:
        if tab_id not in self.tabs:
            raise HTTPException(404, "Unknown tab")
        self.active_tab_id = tab_id
        await self.refresh_render(reason="activate_tab")

    async def close_tab(self, tab_id: str) -> None:
        page = self.tabs.get(tab_id)
        if page is None:
            raise HTTPException(404, "Unknown tab")

        if len(self.tabs) == 1:
            await page.goto("about:blank", wait_until="domcontentloaded", timeout=60000)
            self.force_stream_reason = ""
            await self._refresh_tab_meta(tab_id)
            await self.refresh_render(reason="close_last_tab")
            return

        with contextlib.suppress(Exception):
            await page.close()

        self.tabs.pop(tab_id, None)
        self.tab_meta.pop(tab_id, None)
        self.tab_order = [x for x in self.tab_order if x != tab_id]

        if self.active_tab_id == tab_id:
            self.active_tab_id = self.tab_order[-1]

        await self.refresh_render(reason="close_tab")

    async def note_translation_event(self, event_type: str, detail: dict[str, t.Any] | None = None) -> None:
        event_type = str(event_type or "").strip().lower()
        if not event_type:
            return

        self.translation_event_counts[event_type] += 1
        if event_type in {"media_capture_requested", "camera_capture_requested", "microphone_capture_requested"}:
            self.force_stream_reason = event_type
        elif event_type in {"client_error", "promise_rejection"} and self.translation_event_counts[event_type] >= 4:
            self.force_stream_reason = "translation-error-threshold"
        elif event_type == "media_runtime":
            self.force_stream_reason = "media-runtime"

        await self.runtime.hub.broadcast(self.session_id, {
            "type": "translation_event",
            "event_type": event_type,
            "detail": detail or {},
        })

        if self.active_tab_id:
            await self.refresh_render(reason=f"translation-event:{event_type}")

    async def _stop_screencast(self) -> None:
        if self._stream_cdp is not None:
            with contextlib.suppress(Exception):
                await self._stream_cdp.send("Page.stopScreencast")
        self._stream_cdp = None
        self._stream_active_tab_id = ""

    async def _on_screencast_frame(self, payload: dict[str, t.Any]) -> None:
        data_b64 = str(payload.get("data") or "")
        if not data_b64:
            return
        try:
            self._frame_bytes = base64.b64decode(data_b64)
            self._frame_event.set()
            await self.runtime.frame_hub.broadcast(self.session_id, self._frame_bytes)
        finally:
            if self._stream_cdp is not None:
                with contextlib.suppress(Exception):
                    await self._stream_cdp.send("Page.screencastFrameAck", {"sessionId": payload.get("sessionId")})

    async def _ensure_screencast(self) -> None:
        if self._stream_cdp is not None and self._stream_active_tab_id == self.active_tab_id:
            return

        await self._stop_screencast()

        page = self.active_page
        assert self.context is not None
        cdp = await self.context.new_cdp_session(page)
        self._stream_cdp = cdp
        self._stream_active_tab_id = self.active_tab_id

        cdp.on("Page.screencastFrame", lambda payload: asyncio.create_task(self._on_screencast_frame(payload)))
        await cdp.send("Page.enable")

        viewport = page.viewport_size or self.cfg.browser.get("viewport", {"width": 1366, "height": 900})
        max_width = min(int(viewport.get("width", 1366)), int(self.cfg.browser.get("remote_width_cap", DEFAULT_REMOTE_WIDTH_CAP)))
        max_height = min(int(viewport.get("height", 900)), int(self.cfg.browser.get("remote_height_cap", DEFAULT_REMOTE_HEIGHT_CAP)))

        await cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": int(self.cfg.browser.get("screenshot_quality", 85)),
            "maxWidth": max_width,
            "maxHeight": max_height,
            "everyNthFrame": 1,
        })

    async def latest_frame(self) -> bytes:
        if self._frame_bytes is not None:
            return self._frame_bytes
        await self._frame_event.wait()
        return self._frame_bytes or b""

    async def refresh_render(self, *, reason: str) -> None:
        async with self._html_lock:
            page = self.active_page

            await self._refresh_tab_meta(self.active_tab_id)

            try:
                self.last_title = (self.tab_meta.get(self.active_tab_id) or {}).get("title") or await page.title()
            except Exception:
                self.last_title = self.last_title or "Untitled"

            try:
                self.last_url = (self.tab_meta.get(self.active_tab_id) or {}).get("url") or page.url
            except Exception:
                pass

            try:
                self.analysis = await analyze_page_profile(page, self.cfg)
            except Exception:
                pass

            if self.force_stream_reason:
                new_effective = "stream"
            elif self.requested_mode == "hybrid":
                new_effective = self.analysis.get("recommended_mode", self.effective_mode or "translate")
            else:
                new_effective = self.requested_mode

            self.effective_mode = new_effective

            if self.effective_mode == "stream":
                await self._ensure_screencast()
            else:
                await self._stop_screencast()

            try:
                raw_html = await page.content()
            except Exception:
                raw_html = "<!doctype html><html><body><pre>Unable to capture page HTML.</pre></body></html>"

            self.last_render_html = rewrite_document_for_vortex(self, raw_html, self.last_url)
            self.updated_at = utc_now()
            self.last_status = f"ready:{reason}:{self.effective_mode}"

        await self.runtime.hub.broadcast(
            self.session_id,
            {
                "type": "session_update",
                "reason": reason,
                "url": self.last_url,
                "title": self.last_title,
                "status": self.last_status,
                "render_mode": self.effective_mode,
                "route_mode": self.route_mode,
                "tabs": self.tabs_payload(),
                "active_tab_id": self.active_tab_id,
                "stream_reason": self.force_stream_reason,
            },
        )

    async def navigate(self, url: str) -> None:
        page = self.active_page
        if url == "about:blank":
            await page.goto("about:blank", wait_until="domcontentloaded", timeout=60000)
        else:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("load", timeout=15000)
        self.force_stream_reason = ""
        await self.refresh_render(reason="navigate")

    async def reload(self) -> None:
        page = self.active_page
        await page.reload(wait_until="domcontentloaded", timeout=60000)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("load", timeout=15000)
        await self.refresh_render(reason="reload")

    async def go_back(self) -> None:
        page = self.active_page
        await page.go_back(wait_until="domcontentloaded", timeout=60000)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("load", timeout=15000)
        await self.refresh_render(reason="back")

    async def go_forward(self) -> None:
        page = self.active_page
        await page.go_forward(wait_until="domcontentloaded", timeout=60000)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("load", timeout=15000)
        await self.refresh_render(reason="forward")

    async def resize(self, width: int, height: int) -> None:
        page = self.active_page
        width = max(320, min(3840, int(width)))
        height = max(240, min(2160, int(height)))
        await page.set_viewport_size({"width": width, "height": height})
        await asyncio.sleep(0.02)
        if self.effective_mode == "stream":
            await self._ensure_screencast()
        await self.refresh_render(reason="resize")

    async def click(self, x: float, y: float, *, button: str = "left") -> None:
        page = self.active_page
        await page.mouse.click(x, y, button=button)
        await asyncio.sleep(0.05)
        await self.refresh_render(reason="click")

    async def mouse_move(self, x: float, y: float) -> None:
        page = self.active_page
        await page.mouse.move(x, y)

    async def mouse_down(self, x: float, y: float, *, button: str = "left") -> None:
        page = self.active_page
        await page.mouse.move(x, y)
        await page.mouse.down(button=button)

    async def mouse_up(self, x: float, y: float, *, button: str = "left") -> None:
        page = self.active_page
        await page.mouse.move(x, y)
        await page.mouse.up(button=button)
        await asyncio.sleep(0.02)
        await self.refresh_render(reason="mouse_up")

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        page = self.active_page
        await page.mouse.wheel(delta_x, delta_y)

    async def type_text(self, text: str) -> None:
        page = self.active_page
        await page.keyboard.type(text)
        await asyncio.sleep(0.02)
        await self.refresh_render(reason="type")

    async def key_press(self, key: str) -> None:
        page = self.active_page
        await page.keyboard.press(key)
        await asyncio.sleep(0.02)
        await self.refresh_render(reason="key")

    async def key_down(self, key: str) -> None:
        page = self.active_page
        await page.keyboard.down(key)

    async def key_up(self, key: str) -> None:
        page = self.active_page
        await page.keyboard.up(key)

    async def fill_selector(self, selector: str, value: str) -> None:
        page = self.active_page
        await page.fill(selector, value)
        await asyncio.sleep(0.02)
        await self.refresh_render(reason="fill")

    async def proxy_fetch(self, url: str, method: str, headers: dict[str, str], body: bytes | None) -> SimpleUpstreamResponse:
        return await self.egress.fetch(
            self.route_mode,
            url=url,
            method=method,
            headers=headers,
            body=body,
            follow_redirects=True,
        )

    async def close(self) -> None:
        self.closed = True
        await self._stop_screencast()

        for page in list(self.tabs.values()):
            with contextlib.suppress(Exception):
                await page.close()
        self.tabs.clear()
        self.tab_meta.clear()
        self.tab_order.clear()

        if self.context is not None:
            with contextlib.suppress(Exception):
                await self.context.close()
            self.context = None

        if self.browser is not None:
            with contextlib.suppress(Exception):
                await self.browser.close()
            self.browser = None

        await self.runtime.remove(self.session_id)


VORTEX_BRIDGE_JS = r"""
(() => {
  const sessionId = %SESSION_ID_JSON%;
  const originBase = %ORIGIN_JSON%;
  const viewerToken = %VIEWER_TOKEN_JSON%;
  const proxyBase = `/api/browser/sessions/${sessionId}`;

  function abs(url) {
    try { return new URL(url, originBase).toString(); } catch { return url; }
  }

  async function report(type, detail = {}) {
    try {
      await fetch(`${proxyBase}/event?viewer=${encodeURIComponent(viewerToken)}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        keepalive: true,
        body: JSON.stringify({ type, detail })
      });
    } catch {}
  }

  function proxyAsset(url) {
    const u = encodeURIComponent(abs(url));
    return `${proxyBase}/asset?viewer=${encodeURIComponent(viewerToken)}&u=${u}`;
  }

  function nav(url) {
    window.location.href = `${proxyBase}/render?viewer=${encodeURIComponent(viewerToken)}&u=${encodeURIComponent(abs(url))}`;
  }

  window.addEventListener('error', (ev) => {
    report('client_error', { message: String(ev.message || 'error') });
  });

  window.addEventListener('unhandledrejection', (ev) => {
    report('promise_rejection', { message: String(ev.reason || 'rejection') });
  });

  if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    const originalGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async function(constraints) {
      await report('media_capture_requested', { constraints: constraints || {} });
      return originalGetUserMedia(constraints);
    };
  }

  const OriginalWebSocket = window.WebSocket;
  window.WebSocket = function(url, protocols) {
    report('websocket_runtime', { url: String(url || '') });
    return new OriginalWebSocket(url, protocols);
  };
  window.WebSocket.prototype = OriginalWebSocket.prototype;

  document.addEventListener('click', (ev) => {
    const a = ev.target && ev.target.closest ? ev.target.closest('a[href]') : null;
    if (!a) return;
    const href = a.getAttribute('href');
    if (!href || href.startsWith('#') || href.startsWith('javascript:')) return;
    ev.preventDefault();
    nav(href);
  }, true);

  document.addEventListener('submit', async (ev) => {
    const form = ev.target;
    if (!(form instanceof HTMLFormElement)) return;
    ev.preventDefault();
    const action = form.getAttribute('action') || window.location.href;
    const method = (form.getAttribute('method') || 'GET').toUpperCase();
    const target = abs(action);
    const data = new FormData(form);
    if (method === 'GET') {
      const url = new URL(target);
      for (const [k, v] of data.entries()) url.searchParams.set(k, String(v));
      nav(url.toString());
      return;
    }
    const body = new URLSearchParams();
    for (const [k, v] of data.entries()) body.append(k, String(v));
    const res = await fetch(`${proxyBase}/proxy?viewer=${encodeURIComponent(viewerToken)}`, {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({
        url: target,
        method,
        headers: {'content-type': 'application/x-www-form-urlencoded'},
        body_b64: btoa(body.toString())
      })
    });
    if (res.status >= 400) {
      report('upstream_error', { status: res.status, url: target });
    }
    if (res.redirected) {
      window.location.href = res.url;
      return;
    }
    window.location.reload();
  }, true);

  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const requestUrl = typeof input === 'string' ? input : (input && input.url ? input.url : String(input));
    const method = String((init && init.method) || 'GET').toUpperCase();
    let headers = {};
    if (init && init.headers) {
      if (init.headers instanceof Headers) {
        init.headers.forEach((v, k) => headers[k] = v);
      } else if (Array.isArray(init.headers)) {
        for (const [k, v] of init.headers) headers[k] = v;
      } else {
        headers = Object.assign({}, init.headers);
      }
    }
    let body_b64 = null;
    if (init && typeof init.body === 'string') {
      body_b64 = btoa(unescape(encodeURIComponent(init.body)));
    }
    const res = await originalFetch(`${proxyBase}/proxy?viewer=${encodeURIComponent(viewerToken)}`, {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({ url: abs(requestUrl), method, headers, body_b64 })
    });
    if (res.status >= 400) {
      report('upstream_error', { status: res.status, url: abs(requestUrl) });
    }
    return res;
  };

  const xhrOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__vortex_method = method;
    this.__vortex_url = abs(url);
    return xhrOpen.apply(this, arguments);
  };

  const xhrSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function(body) {
    const xhr = this;
    const method = String(xhr.__vortex_method || 'GET').toUpperCase();
    const url = xhr.__vortex_url || window.location.href;
    const payload = {
      url,
      method,
      headers: {'x-vortex-xhr': '1'},
      body_b64: typeof body === 'string' ? btoa(unescape(encodeURIComponent(body))) : null
    };
    originalFetch(`${proxyBase}/proxy?viewer=${encodeURIComponent(viewerToken)}`, {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify(payload)
    }).then(async (resp) => {
      const text = await resp.text();
      if (resp.status >= 400) {
        report('upstream_error', { status: resp.status, url });
      }
      Object.defineProperty(xhr, 'readyState', {value: 4, configurable: true});
      Object.defineProperty(xhr, 'status', {value: resp.status, configurable: true});
      Object.defineProperty(xhr, 'responseText', {value: text, configurable: true});
      if (xhr.onreadystatechange) xhr.onreadystatechange();
      if (xhr.onload) xhr.onload();
    }).catch((err) => {
      report('client_error', { message: String(err || 'xhr failed') });
      if (xhr.onerror) xhr.onerror(err);
    });
  };

  document.querySelectorAll('img[src],script[src],link[href],video[src],audio[src],source[src],iframe[src]').forEach((el) => {
    const attr = el.hasAttribute('src') ? 'src' : 'href';
    const value = el.getAttribute(attr);
    if (!value) return;
    if (value.startsWith('data:') || value.startsWith('blob:')) return;
    el.setAttribute(attr, proxyAsset(value));
  });

  const mediaNodes = document.querySelectorAll('video, audio');
  if (mediaNodes.length > 0) {
    report('media_runtime', { count: mediaNodes.length });
  }
})();
"""


def safe_html_text(value: str) -> str:
    return html_lib.escape(value, quote=True)


def wrap_translated_html(title: str, body_html: str, *, session_id: str, origin_url: str, viewer_token: str) -> str:
    bridge = (VORTEX_BRIDGE_JS
        .replace("%SESSION_ID_JSON%", json.dumps(session_id))
        .replace("%ORIGIN_JSON%", json.dumps(origin_url))
        .replace("%VIEWER_TOKEN_JSON%", json.dumps(viewer_token))
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_html_text(title)}</title>
  <style>
    html, body {{ margin: 0; padding: 0; min-height: 100%; background: #fff; color: #111; }}
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; }}
    img, video {{ max-width: 100%; }}
    iframe {{ border: 0; max-width: 100%; }}
    .vortex-banner {{ position: sticky; top: 0; z-index: 2147483647; background: #111827; color: #e5e7eb; font: 12px/1.4 system-ui, sans-serif; padding: 6px 10px; }}
    .vortex-banner strong {{ color: #fff; }}
  </style>
</head>
<body>
  <div class="vortex-banner"><strong>Vortex Network</strong> translated session · {safe_html_text(origin_url)}</div>
  {body_html}
  <script>{bridge}</script>
</body>
</html>
"""


def rewrite_document_for_vortex(session: BrowserSession, raw_html: str, base_url: str) -> str:
    if BeautifulSoup is None:
        safe = f"<pre>{safe_html_text(raw_html[:MAX_BODY_PREVIEW])}</pre>"
        return wrap_translated_html(session.last_title or "Translated", safe, session_id=session.session_id, origin_url=base_url, viewer_token=session.viewer_token)

    soup = BeautifulSoup(raw_html, "lxml")

    # Remove or soften some of the worst junk without trying to be perfect.
    for tag in soup.find_all(["script", "noscript"]):
        if tag.name == "script":
            # Keep external/site scripts; drop obvious ad and tracking tags when possible.
            src = (tag.get("src") or "").lower()
            text = (tag.string or "")[:512].lower() if tag.string else ""
            junk_markers = ["doubleclick", "googletagmanager", "adservice", "quantserve", "taboola", "outbrain", "adsystem"]
            if any(marker in src or marker in text for marker in junk_markers):
                tag.decompose()
                continue
        if tag.name == "noscript":
            tag.decompose()

    for selector in [
        "[aria-label*=cookie i]",
        "[class*=cookie i]",
        "[id*=cookie i]",
        "[class*=popup i]",
        "[id*=popup i]",
        "[class*=overlay i]",
        "[id*=overlay i]",
    ]:
        for node in soup.select(selector):
            if getattr(node, "decompose", None):
                with contextlib.suppress(Exception):
                    node.decompose()

    # Rewrite resource URLs.
    url_attrs = {
        "a": ["href"],
        "img": ["src", "srcset"],
        "script": ["src"],
        "link": ["href"],
        "video": ["src", "poster"],
        "audio": ["src"],
        "source": ["src", "srcset"],
        "iframe": ["src"],
        "form": ["action"],
    }

    viewer = urllib.parse.quote(session.viewer_token, safe="")

    def asset_url(original: str) -> str:
        absolute = urllib.parse.urljoin(base_url, original)
        return (
            f"/api/browser/sessions/{session.session_id}/asset"
            f"?viewer={viewer}&u={urllib.parse.quote(absolute, safe='')}"
        )

    def nav_url(original: str) -> str:
        absolute = urllib.parse.urljoin(base_url, original)
        return (
            f"/api/browser/sessions/{session.session_id}/render"
            f"?viewer={viewer}&u={urllib.parse.quote(absolute, safe='')}"
        )

    for tag_name, attrs in url_attrs.items():
        for node in soup.find_all(tag_name):
            for attr in attrs:
                value = node.get(attr)
                if not value:
                    continue
                if attr == "srcset":
                    parts = []
                    for chunk in value.split(","):
                        chunk = chunk.strip()
                        if not chunk:
                            continue
                        bits = chunk.split()
                        bits[0] = asset_url(bits[0])
                        parts.append(" ".join(bits))
                    node[attr] = ", ".join(parts)
                    continue
                if attr in {"href", "action"} and tag_name in {"a", "form"}:
                    if value.startswith("#") or value.startswith("javascript:"):
                        continue
                    node[attr] = nav_url(value)
                else:
                    if value.startswith("data:") or value.startswith("blob:"):
                        continue
                    node[attr] = asset_url(value)

    body = soup.body or soup
    body_html = str(body)
    return wrap_translated_html(session.last_title or "Translated", body_html, session_id=session.session_id, origin_url=base_url, viewer_token=session.viewer_token)




def ensure_http_https_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, "Only http/https URLs are supported")
    return url


def make_remote_shell(session_id: str, title: str, render_mode: str, ticket: str, viewer_token: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_html_text(title)}</title>
  <style>
    html, body {{ margin: 0; padding: 0; height: 100%; background: #070a16; overflow: hidden; }}
    #content {{ position: fixed; inset: 0; }}
    iframe, canvas {{ width: 100%; height: 100%; border: 0; display: block; background: #000; }}
    canvas {{ touch-action: none; outline: none; cursor: default; }}
  </style>
</head>
<body>
  <div id="content"></div>
  <script>
    const sessionId = {json.dumps(session_id)};
    const ticket = {json.dumps(ticket)};
    const viewerToken = {json.dumps(viewer_token)};
    const content = document.getElementById('content');
    let renderMode = {json.dumps(render_mode)};
    let frameWs = null;
    let canvas = null;
    let ctx = null;

    function pageUrl() {{
      return `/api/browser/sessions/${{sessionId}}/page?viewer=${{encodeURIComponent(viewerToken)}}&t=${{Date.now()}}`;
    }}

    function frameWsUrl() {{
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      return `${{proto}}://${{location.host}}/ws/frame/${{sessionId}}?ticket=${{encodeURIComponent(ticket)}}`;
    }}

    async function sendAction(payload) {{
      return fetch(`/api/browser/sessions/${{sessionId}}/action?viewer=${{encodeURIComponent(viewerToken)}}`, {{
        method: 'POST',
        headers: {{ 'content-type': 'application/json' }},
        body: JSON.stringify(payload)
      }});
    }}

    async function pushResize() {{
      await sendAction({{
        type: 'resize',
        width: Math.max(320, Math.floor(window.innerWidth)),
        height: Math.max(240, Math.floor(window.innerHeight))
      }});
    }}

    function closeFrameSocket() {{
      if (frameWs) {{
        try {{ frameWs.close(); }} catch {{}}
        frameWs = null;
      }}
    }}

    function mountTranslate() {{
      closeFrameSocket();
      const f = document.createElement('iframe');
      f.referrerPolicy = 'no-referrer';
      f.allow = 'autoplay; encrypted-media; microphone; camera';
      f.sandbox = 'allow-scripts allow-forms allow-same-origin allow-downloads';
      f.src = pageUrl();
      content.replaceChildren(f);
      pushResize().catch(() => {{}});
    }}

    function openFrameSocket() {{
      closeFrameSocket();
      frameWs = new WebSocket(frameWsUrl());
      frameWs.binaryType = 'arraybuffer';
      frameWs.onmessage = async (ev) => {{
        if (!canvas || !ctx) return;
        const blob = new Blob([ev.data], {{ type: 'image/jpeg' }});
        const bitmap = await createImageBitmap(blob);
        if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {{
          canvas.width = bitmap.width;
          canvas.height = bitmap.height;
        }}
        ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
        bitmap.close();
      }};
    }}

    function canvasPoint(ev) {{
      const rect = canvas.getBoundingClientRect();
      const scaleX = canvas.width > 0 ? (canvas.width / rect.width) : 1;
      const scaleY = canvas.height > 0 ? (canvas.height / rect.height) : 1;
      return {{
        x: (ev.clientX - rect.left) * scaleX,
        y: (ev.clientY - rect.top) * scaleY
      }};
    }}

    function mountStream() {{
      canvas = document.createElement('canvas');
      canvas.tabIndex = 0;
      ctx = canvas.getContext('2d', {{ alpha: false, desynchronized: true }});
      content.replaceChildren(canvas);
      openFrameSocket();

      canvas.addEventListener('pointermove', async (ev) => {{
        const p = canvasPoint(ev);
        await sendAction({{ type: 'move', x: p.x, y: p.y }});
      }});

      canvas.addEventListener('pointerdown', async (ev) => {{
        canvas.focus();
        const p = canvasPoint(ev);
        await sendAction({{ type: 'mouse_down', x: p.x, y: p.y, button: ev.button === 2 ? 'right' : 'left' }});
      }});

      canvas.addEventListener('pointerup', async (ev) => {{
        const p = canvasPoint(ev);
        await sendAction({{ type: 'mouse_up', x: p.x, y: p.y, button: ev.button === 2 ? 'right' : 'left' }});
      }});

      canvas.addEventListener('wheel', async (ev) => {{
        ev.preventDefault();
        await sendAction({{ type: 'wheel', delta_x: ev.deltaX, delta_y: ev.deltaY }});
      }}, {{ passive: false }});

      window.addEventListener('keydown', async (ev) => {{
        if (!canvas) return;
        await sendAction({{ type: 'key_down', key: ev.key }});
      }});

      window.addEventListener('keyup', async (ev) => {{
        if (!canvas) return;
        await sendAction({{ type: 'key_up', key: ev.key }});
      }});

      window.addEventListener('paste', async (ev) => {{
        const text = ev.clipboardData ? ev.clipboardData.getData('text/plain') : '';
        if (!text) return;
        await sendAction({{ type: 'type', text }});
      }});

      pushResize().catch(() => {{}});
    }}

    function mountCurrentMode() {{
      if (renderMode === 'stream') mountStream();
      else mountTranslate();
    }}

    window.addEventListener('resize', () => {{
      pushResize().catch(() => {{}});
    }});

    mountCurrentMode();

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${{proto}}://${{location.host}}/ws/session/${{sessionId}}?ticket=${{encodeURIComponent(ticket)}}`);

    ws.onmessage = (ev) => {{
      try {{
        const msg = JSON.parse(ev.data);
        if (msg.type !== 'session_update') return;

        if (msg.render_mode && msg.render_mode !== renderMode) {{
          renderMode = msg.render_mode;
          mountCurrentMode();
          return;
        }}

        if (renderMode === 'translate') {{
          const f = content.querySelector('iframe');
          if (f) f.src = pageUrl();
        }}
      }} catch {{}}
    }};
  </script>
</body>
</html>
"""


def create_app(cfg: NodeConfig) -> FastAPI:
    if FastAPI is None:
        raise RuntimeError("fastapi and uvicorn are required")

    ensure_dirs()
    auth = AuthManager(cfg)
    egress = EgressManager(cfg)
    runtime = BrowserRuntime(cfg)
    terminals = TerminalRuntime()
    login_limiter = SlidingWindowRateLimiter()
    api_limiter = SlidingWindowRateLimiter()

    app = FastAPI(title=APP_NAME, version=APP_VERSION, docs_url="/docs", redoc_url="/redoc")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.server.get("allowed_origins", ["*"]),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    async def persist_cfg() -> None:
        cfg.raw["updated_at"] = utc_now()
        await asyncio.to_thread(write_json, CONFIG_PATH, cfg.raw)

    def node_settings_payload() -> dict[str, t.Any]:
        totp_cfg = cfg.auth.setdefault("totp", {"enabled": False, "secret": "", "issuer": APP_NAME})
        return {
            "route_mode": cfg.egress.get("route_mode", "direct"),
            "browser_mode": cfg.browser.get("mode", "hybrid"),
            "strip_common_junk": bool(cfg.browser.get("strip_common_junk", True)),
            "block_aggressive_popups": bool(cfg.browser.get("block_aggressive_popups", True)),
            "screenshot_fps": int(cfg.browser.get("screenshot_fps", 30)),
            "screenshot_quality": int(cfg.browser.get("screenshot_quality", 85)),
            "max_tabs_per_session": int(cfg.browser.get("max_tabs_per_session", MAX_TABS_PER_SESSION)),
            "remote_width_cap": int(cfg.browser.get("remote_width_cap", DEFAULT_REMOTE_WIDTH_CAP)),
            "remote_height_cap": int(cfg.browser.get("remote_height_cap", DEFAULT_REMOTE_HEIGHT_CAP)),
            "serve_ui": bool(cfg.raw.get("frontend", {}).get("serve_ui", False)),
            "ui_html_path": str(cfg.raw.get("frontend", {}).get("ui_html_path", "")),
            "totp_enabled": bool(totp_cfg.get("enabled")),
            "paired_devices": [
                {
                    "device_id": d.get("device_id"),
                    "label": d.get("label", ""),
                    "created_at": d.get("created_at", ""),
                    "last_seen_at": d.get("last_seen_at", ""),
                }
                for d in cfg.auth.get("paired_devices", [])
            ],
            "available_routes": ["direct", "wireguard", "tor"],
            "blob_sync": True,
            "terminal": True,
        }

    @app.on_event("startup")
    async def _startup() -> None:
        await egress.ensure_ready(egress.default_mode())
        await runtime.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await runtime.stop()

    def client_key(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        return forwarded or (request.client.host if request.client else "unknown")

    async def require_api_rate_limit(request: Request) -> None:
        key = f"api:{client_key(request)}"
        ok, retry = await api_limiter.check(key, int(cfg.auth.get("api_rate_limit_per_minute", 240)), 60)
        if not ok:
            raise HTTPException(429, f"Rate limited; retry in {retry}s")

    def extract_bearer(request: Request) -> str:
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            return authz[7:].strip()
        raise HTTPException(401, "Missing bearer token")

    async def require_access(request: Request) -> dict[str, t.Any]:
        await require_api_rate_limit(request)
        token = extract_bearer(request)
        try:
            return auth.require_access_token(token)
        except Exception as exc:
            raise HTTPException(401, str(exc)) from exc

    def require_embed(ticket: str, session_id: str) -> dict[str, t.Any]:
        try:
            return auth.require_embed_ticket(ticket, session_id=session_id)
        except Exception as exc:
            raise HTTPException(401, str(exc)) from exc

    @app.get("/", include_in_schema=False)
    async def root() -> Response:
        frontend_cfg = cfg.raw.get("frontend", {})
        ui_path = pathlib.Path(str(frontend_cfg.get("ui_html_path", ""))).expanduser()
        if frontend_cfg.get("serve_ui") and ui_path.exists():
            return FileResponse(ui_path, media_type="text/html", headers={"cache-control": "no-store"})
        return JSONResponse(
            {
                "name": APP_NAME,
                "version": APP_VERSION,
                "status": "ok",
                "public_base_url": cfg.server.get("public_base_url"),
                "egress": egress.describe(),
                "browser_mode": cfg.browser.get("mode", "hybrid"),
                "features": {
                    "translation": True,
                    "remote_fallback": True,
                    "terminal": True,
                    "blob_sync": True,
                },
            },
            headers={
                "content-security-policy": f"default-src 'self'; frame-ancestors {' '.join(cfg.server.get('frame_ancestors') or ['*'])}; img-src 'self' data: blob:; media-src 'self' blob:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss: https: http:;",
            },
        )

    @app.get("/api/info")
    async def api_info() -> JSONResponse:
        return JSONResponse(
            {
                "name": APP_NAME,
                "version": APP_VERSION,
                "status": "ok",
                "public_base_url": cfg.server.get("public_base_url"),
                "egress": egress.describe(),
                "browser_mode": cfg.browser.get("mode", "hybrid"),
                "features": {
                    "translation": True,
                    "remote_fallback": True,
                    "terminal": True,
                    "blob_sync": True,
                },
            }
        )

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({
            "ok": True,
            "time": utc_now(),
            "sessions": len(runtime.sessions),
            "terminal_sessions": len(terminals.sessions),
            "egress": egress.describe(),
        })

    @app.post("/api/auth/login")
    async def login(request: Request) -> JSONResponse:
        key = f"login:{client_key(request)}"
        ok, retry = await login_limiter.check(key, int(cfg.auth.get("login_rate_limit_per_15m", 20)), 15 * 60)
        if not ok:
            raise HTTPException(429, f"Too many attempts; retry in {retry}s")

        payload = await request.json()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        otp = str(payload.get("otp", "")).strip()
        remember_device = bool(payload.get("remember_device", True))
        device_id = str(payload.get("device_id", "")).strip()
        device_label = str(payload.get("device_label", "")).strip() or request.headers.get("user-agent", "Vortex OS device")

        if username != cfg.auth.get("username") or not verify_password(password, cfg.auth.get("password_hash", "")):
            raise HTTPException(401, "Invalid credentials")

        if auth.totp_enabled() and not auth.verify_totp(otp):
            raise HTTPException(401, "A valid authenticator code is required")

        pairing_token = None
        if remember_device and device_id:
            touch_paired_device(cfg.raw, device_id, device_label)
            await persist_cfg()
            pairing_token = auth.issue_pair_token(username, device_id=device_id)

        token = auth.issue_access_token(username, device_id=device_id)
        return JSONResponse({
            "access_token": token,
            "pairing_token": pairing_token,
            "token_type": "bearer",
            "expires_in": int(cfg.auth.get("access_token_ttl_seconds", 0)),
            "username": username,
            "egress": egress.describe(),
            "node_settings": node_settings_payload(),
        })

    @app.post("/api/auth/refresh")
    async def refresh_access(request: Request) -> JSONResponse:
        payload = await request.json()
        pair_token = str(payload.get("pairing_token", "")).strip()
        if not pair_token:
            raise HTTPException(400, "pairing_token is required")
        try:
            claims = auth.require_pair_token(pair_token)
        except Exception as exc:
            raise HTTPException(401, str(exc)) from exc

        device_id = str(claims.get("device_id", "")).strip()
        device = find_paired_device(cfg.raw, device_id)
        if device is None:
            raise HTTPException(401, "This paired device is no longer trusted")

        touch_paired_device(cfg.raw, device_id, device.get("label", ""))
        await persist_cfg()

        token = auth.issue_access_token(str(claims.get("sub", cfg.auth.get("username"))), device_id=device_id)
        return JSONResponse({
            "access_token": token,
            "token_type": "bearer",
            "expires_in": int(cfg.auth.get("access_token_ttl_seconds", 0)),
            "node_settings": node_settings_payload(),
        })

    @app.post("/api/auth/totp/setup")
    async def totp_setup(request: Request) -> JSONResponse:
        await require_access(request)
        data = auth.build_totp_setup()
        await persist_cfg()
        return JSONResponse(data)

    @app.post("/api/auth/totp/confirm")
    async def totp_confirm(request: Request) -> JSONResponse:
        await require_access(request)
        payload = await request.json()
        otp = str(payload.get("otp", "")).strip()
        if not otp:
            raise HTTPException(400, "otp is required")
        if pyotp is None:
            raise HTTPException(500, "pyotp is not installed on the node")

        secret = str(cfg.auth.get("totp", {}).get("secret", "")).strip()
        if not secret:
            raise HTTPException(400, "2FA setup has not been started")

        if not pyotp.TOTP(secret).verify(otp, valid_window=1):
            raise HTTPException(401, "Invalid authenticator code")

        cfg.auth.setdefault("totp", {})["enabled"] = True
        await persist_cfg()
        return JSONResponse({"ok": True, "totp_enabled": True})

    @app.post("/api/auth/totp/disable")
    async def totp_disable(request: Request) -> JSONResponse:
        await require_access(request)
        cfg.auth.setdefault("totp", {})["enabled"] = False
        cfg.auth.setdefault("totp", {})["secret"] = ""
        await persist_cfg()
        return JSONResponse({"ok": True, "totp_enabled": False})

    @app.get("/api/auth/paired-devices")
    async def list_paired_devices(request: Request) -> JSONResponse:
        await require_access(request)
        return JSONResponse({"devices": node_settings_payload()["paired_devices"]})

    @app.delete("/api/auth/paired-devices/{device_id}")
    async def delete_paired_device(device_id: str, request: Request) -> JSONResponse:
        await require_access(request)
        ok = revoke_paired_device(cfg.raw, device_id)
        await persist_cfg()
        return JSONResponse({"ok": ok})

    @app.get("/api/node/settings")
    async def get_node_settings(request: Request) -> JSONResponse:
        await require_access(request)
        return JSONResponse(node_settings_payload())

    @app.patch("/api/node/settings")
    async def patch_node_settings(request: Request) -> JSONResponse:
        await require_access(request)
        payload = await request.json()

        if "route_mode" in payload:
            new_route = str(payload["route_mode"]).lower()
            if new_route not in {"direct", "wireguard", "tor"}:
                raise HTTPException(400, "Invalid route_mode")
            cfg.egress["route_mode"] = new_route
            await egress.ensure_ready(new_route)

        if "browser_mode" in payload:
            new_browser_mode = str(payload["browser_mode"]).lower()
            if new_browser_mode not in {"translate", "stream", "hybrid"}:
                raise HTTPException(400, "Invalid browser_mode")
            cfg.browser["mode"] = new_browser_mode

        if "strip_common_junk" in payload:
            cfg.browser["strip_common_junk"] = bool(payload["strip_common_junk"])

        if "block_aggressive_popups" in payload:
            cfg.browser["block_aggressive_popups"] = bool(payload["block_aggressive_popups"])

        if "screenshot_fps" in payload:
            cfg.browser["screenshot_fps"] = max(1, min(MAX_REMOTE_FPS, int(payload["screenshot_fps"])))

        if "screenshot_quality" in payload:
            cfg.browser["screenshot_quality"] = max(40, min(95, int(payload["screenshot_quality"])))

        if "max_tabs_per_session" in payload:
            cfg.browser["max_tabs_per_session"] = max(1, min(32, int(payload["max_tabs_per_session"])))

        await persist_cfg()
        return JSONResponse(node_settings_payload())

    @app.post("/api/node/update")
    async def node_update(request: Request) -> JSONResponse:
        await require_access(request)
        result = apply_startup_updates(cfg.raw, restart_after_backend_update=False)
        updated = result.get("updated", []) or []
        if "backend" in updated:
            loop = asyncio.get_running_loop()
            loop.call_later(0.75, lambda: restart_current_process("Backend update applied. Restarting Vortex Node..."))
        return JSONResponse({
            "ok": True,
            "checked": bool(result.get("checked")),
            "updated": updated,
            "error": result.get("error"),
            "will_restart": "backend" in updated,
        })

    @app.post("/api/node/reset")
    async def node_reset(request: Request) -> JSONResponse:
        await require_access(request)
        payload = await request.json()
        preserve_config = bool(payload.get("preserve_config", True))
        clear_synced_profiles = bool(payload.get("clear_synced_profiles", True))

        for session in list(runtime.sessions.values()):
            with contextlib.suppress(Exception):
                await session.close()

        for path in [BROWSER_STATE_DIR, LOG_DIR, SESSION_DIR, RUNTIME_DIR, WIREGUARD_DIR]:
            shutil.rmtree(path, ignore_errors=True)
            path.mkdir(parents=True, exist_ok=True)

        if clear_synced_profiles:
            shutil.rmtree(SYNCED_BLOB_DIR, ignore_errors=True)
            SYNCED_BLOB_DIR.mkdir(parents=True, exist_ok=True)

        cfg.auth["paired_devices"] = []
        cfg.auth.setdefault("totp", {})["enabled"] = False
        cfg.auth.setdefault("totp", {})["secret"] = ""

        if preserve_config:
            await persist_cfg()

        return JSONResponse({
            "ok": True,
            "preserve_config": preserve_config,
            "clear_synced_profiles": clear_synced_profiles,
        })

    @app.get("/api/me")
    async def me(request: Request) -> JSONResponse:
        claims = await require_access(request)
        return JSONResponse({
            "username": claims.get("sub"),
            "server": {
                "public_base_url": cfg.server.get("public_base_url"),
                "mode": cfg.browser.get("mode", "hybrid"),
                "egress": egress.describe(),
            },
            "node_settings": node_settings_payload(),
        })

    @app.get("/api/profile/blob")
    async def get_profile_blob(request: Request) -> Response:
        claims = await require_access(request)
        path = synced_blob_path(str(claims.get("sub", cfg.auth.get("username", "owner"))))
        if not path.exists():
            raise HTTPException(404, "No synced Vortex OS blob exists for this user")
        return Response(path.read_bytes(), media_type="application/json", headers={"cache-control": "no-store"})

    @app.put("/api/profile/blob")
    async def put_profile_blob(request: Request) -> JSONResponse:
        claims = await require_access(request)
        body = await request.body()
        if len(body) > MAX_BODY_PREVIEW * 8:
            raise HTTPException(413, "Blob is too large")
        try:
            json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise HTTPException(400, "Profile blob must be valid JSON") from exc
        path = synced_blob_path(str(claims.get("sub", cfg.auth.get("username", "owner"))))
        write_private_text(path, body.decode("utf-8"))
        return JSONResponse({"ok": True, "path": str(path)})

    @app.post("/api/terminal/sessions")
    async def create_terminal_session(request: Request) -> JSONResponse:
        claims = await require_access(request)
        session = await terminals.create_session(str(claims.get("sub", cfg.auth.get("username", "owner"))))
        return JSONResponse({
            "ok": True,
            "session_id": session.session_id,
            "cwd": session.cwd,
            "created_at": session.created_at,
        })

    @app.post("/api/terminal/sessions/{session_id}/exec")
    async def exec_terminal_session(session_id: str, request: Request) -> JSONResponse:
        await require_access(request)
        payload = await request.json()
        result = await terminals.exec(session_id, str(payload.get("command", "")))
        return JSONResponse(result)

    @app.delete("/api/terminal/sessions/{session_id}")
    async def delete_terminal_session(session_id: str, request: Request) -> JSONResponse:
        await require_access(request)
        await terminals.close_session(session_id)
        return JSONResponse({"ok": True})

    @app.post("/api/browser/sessions")
    async def create_browser_session(request: Request) -> JSONResponse:
        claims = await require_access(request)
        payload = await request.json()

        url = str(payload.get("url", "")).strip()
        if not url:
            raise HTTPException(400, "url is required")
        if url != "about:blank":
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                raise HTTPException(400, "Only http/https URLs are supported")

        mode = str(payload.get("mode") or cfg.browser.get("mode", "hybrid")).lower()
        if mode not in {"translate", "stream", "hybrid"}:
            mode = "hybrid"

        requested_route = str(payload.get("route_mode_override") or "default").lower()
        route_mode = egress.normalize_mode(requested_route)
        await egress.ensure_ready(route_mode)

        session = await runtime.create_session(
            url=url,
            mode=mode,
            route_mode=route_mode,
            egress=egress,
        )

        ticket = auth.issue_embed_ticket(str(claims.get("sub")), session_id=session.session_id)
        public_base = str(cfg.server.get("public_base_url", "")).rstrip("/")
        ws_base = public_base.replace("https://", "wss://").replace("http://", "ws://")

        return JSONResponse({
            "session_id": session.session_id,
            "url": session.last_url,
            "title": session.last_title,
            "mode": session.requested_mode,
            "effective_mode": session.effective_mode,
            "route_mode": session.route_mode,
            "analysis": session.analysis,
            "embed_url": f"{public_base}/embed/{session.session_id}?ticket={urllib.parse.quote(ticket, safe='')}&viewer={urllib.parse.quote(session.viewer_token, safe='')}&mode={session.effective_mode}",
            "page_url": f"{public_base}/api/browser/sessions/{session.session_id}/page?viewer={urllib.parse.quote(session.viewer_token, safe='')}",
            "viewer_token": session.viewer_token,
            "tabs": session.tabs_payload(),
            "active_tab_id": session.active_tab_id,
            "ws_url": f"{ws_base}/ws/session/{session.session_id}?ticket={urllib.parse.quote(ticket, safe='')}",
        })

    @app.get("/api/browser/sessions")
    async def list_browser_sessions(request: Request) -> JSONResponse:
        await require_access(request)
        data = []
        for session in runtime.sessions.values():
            data.append({
                "session_id": session.session_id,
                "url": session.last_url,
                "title": session.last_title,
                "status": session.last_status,
                "requested_mode": session.requested_mode,
                "effective_mode": session.effective_mode,
                "route_mode": session.route_mode,
                "analysis": session.analysis,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "tabs": session.tabs_payload(),
                "active_tab_id": session.active_tab_id,
            })
        return JSONResponse({"sessions": data})

    @app.get("/embed/{session_id}")
    async def embed_shell(session_id: str, request: Request, ticket: str, viewer: str | None = None, mode: str = "translate") -> HTMLResponse:
        require_embed(ticket, session_id)
        session = runtime.get(session_id)
        if viewer != session.viewer_token:
            raise HTTPException(401, "invalid viewer token")
        html = make_remote_shell(session.session_id, session.last_title or "Remote Web", session.effective_mode, ticket, session.viewer_token)
        return HTMLResponse(html, headers={
            "content-security-policy": f"default-src 'self' blob: data:; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss: https: http:; frame-ancestors {' '.join(cfg.server.get('frame_ancestors') or ['*'])};",
            "x-frame-options": "ALLOWALL",
        })

    @app.get("/api/browser/sessions/{session_id}/page")
    async def browser_page(session_id: str, ticket: str | None = None, viewer: str | None = None) -> HTMLResponse:
        if ticket:
            require_embed(ticket, session_id)
        else:
            session_for_auth = runtime.get(session_id)
            if viewer != session_for_auth.viewer_token:
                raise HTTPException(401, "invalid viewer token")
        session = runtime.get(session_id)
        return HTMLResponse(session.last_render_html, headers={
            "content-security-policy": f"default-src 'self' data: blob:; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss: https: http:; frame-ancestors {' '.join(cfg.server.get('frame_ancestors') or ['*'])};",
        })

    @app.get("/api/browser/sessions/{session_id}/render")
    async def browser_render(session_id: str, u: str, viewer: str | None = None) -> RedirectResponse:
        session = runtime.get(session_id)
        if viewer != session.viewer_token:
            raise HTTPException(401, "invalid viewer token")
        target = ensure_http_https_url(urllib.parse.unquote(u))
        await session.navigate(target)
        return RedirectResponse(url=f"/api/browser/sessions/{session_id}/page?viewer={urllib.parse.quote(session.viewer_token, safe='')}")

    @app.post("/api/browser/sessions/{session_id}/proxy")
    async def browser_proxy(session_id: str, request: Request, viewer: str | None = None) -> Response:
        session = runtime.get(session_id)
        if viewer != session.viewer_token:
            raise HTTPException(401, "invalid viewer token")

        payload = await request.json()
        target_url = ensure_http_https_url(str(payload.get("url", "")).strip())
        method = str(payload.get("method", "GET")).upper()
        headers = {str(k): str(v) for k, v in dict(payload.get("headers") or {}).items()}
        body_b64 = payload.get("body_b64")
        body = None
        if body_b64:
            try:
                body = base64.b64decode(body_b64)
            except Exception:
                body = None

        upstream = await session.proxy_fetch(target_url, method, headers, body)
        content_type = upstream.headers.get("content-type", "application/octet-stream")
        passthrough_headers = {}
        for header in ["content-type", "cache-control", "content-language", "etag", "last-modified"]:
            if header in upstream.headers:
                passthrough_headers[header] = upstream.headers[header]
        return Response(content=upstream.content, status_code=upstream.status_code, headers=passthrough_headers, media_type=content_type)

    @app.get("/api/browser/sessions/{session_id}/asset")
    async def browser_asset(session_id: str, request: Request, u: str, viewer: str | None = None) -> Response:
        session = runtime.get(session_id)
        if viewer != session.viewer_token:
            raise HTTPException(401, "invalid viewer token")

        target_url = ensure_http_https_url(urllib.parse.unquote(u))
        headers: dict[str, str] = {}
        if request.headers.get("range"):
            headers["range"] = request.headers["range"]

        upstream = await session.proxy_fetch(target_url, "GET", headers, None)
        passthrough_headers = {}
        for header in [
            "content-type",
            "content-length",
            "accept-ranges",
            "content-range",
            "cache-control",
            "etag",
            "last-modified",
            "content-disposition",
        ]:
            if header in upstream.headers:
                passthrough_headers[header] = upstream.headers[header]
        return Response(content=upstream.content, status_code=upstream.status_code, headers=passthrough_headers)

    @app.post("/api/browser/sessions/{session_id}/event")
    async def browser_event(session_id: str, request: Request, viewer: str | None = None) -> JSONResponse:
        session = runtime.get(session_id)
        if viewer != session.viewer_token:
            raise HTTPException(401, "invalid viewer token")
        payload = await request.json()
        await session.note_translation_event(str(payload.get("type", "")), dict(payload.get("detail") or {}))
        return JSONResponse({"ok": True, "render_mode": session.effective_mode})

    @app.get("/api/browser/sessions/{session_id}/stream.jpg")
    async def browser_stream_jpg(session_id: str, ticket: str | None = None, viewer: str | None = None) -> Response:
        if ticket:
            require_embed(ticket, session_id)
            session = runtime.get(session_id)
        else:
            session = runtime.get(session_id)
            if viewer != session.viewer_token:
                raise HTTPException(401, "invalid viewer token")
        frame = await session.latest_frame()
        return Response(content=frame, media_type="image/jpeg", headers={"cache-control": "no-store"})

    @app.post("/api/browser/sessions/{session_id}/action")
    async def browser_action(session_id: str, request: Request, ticket: str | None = None, viewer: str | None = None) -> JSONResponse:
        if ticket:
            require_embed(ticket, session_id)
            session = runtime.get(session_id)
        elif viewer:
            session = runtime.get(session_id)
            if viewer != session.viewer_token:
                raise HTTPException(401, "invalid viewer token")
        else:
            await require_access(request)
            session = runtime.get(session_id)

        payload = await request.json()
        action_type = str(payload.get("type", "")).lower()

        if action_type == "click":
            await session.click(float(payload.get("x", 0)), float(payload.get("y", 0)), button=str(payload.get("button", "left")))
        elif action_type == "move":
            await session.mouse_move(float(payload.get("x", 0)), float(payload.get("y", 0)))
        elif action_type == "mouse_down":
            await session.mouse_down(float(payload.get("x", 0)), float(payload.get("y", 0)), button=str(payload.get("button", "left")))
        elif action_type == "mouse_up":
            await session.mouse_up(float(payload.get("x", 0)), float(payload.get("y", 0)), button=str(payload.get("button", "left")))
        elif action_type == "wheel":
            await session.wheel(float(payload.get("delta_x", 0)), float(payload.get("delta_y", 0)))
        elif action_type == "type":
            await session.type_text(str(payload.get("text", "")))
        elif action_type == "key":
            await session.key_press(str(payload.get("key", "Enter")))
        elif action_type == "key_down":
            await session.key_down(str(payload.get("key", "Enter")))
        elif action_type == "key_up":
            await session.key_up(str(payload.get("key", "Enter")))
        elif action_type == "fill":
            await session.fill_selector(str(payload.get("selector", "")), str(payload.get("value", "")))
        elif action_type == "navigate":
            url = str(payload.get("url", "")).strip()
            if url != "about:blank":
                url = ensure_http_https_url(url)
            await session.navigate(url or "about:blank")
        elif action_type == "back":
            await session.go_back()
        elif action_type == "forward":
            await session.go_forward()
        elif action_type == "reload":
            await session.reload()
        elif action_type == "resize":
            await session.resize(int(payload.get("width", 1366)), int(payload.get("height", 900)))
        elif action_type == "new_tab":
            url = str(payload.get("url", "about:blank")).strip() or "about:blank"
            if url != "about:blank":
                url = ensure_http_https_url(url)
            await session.create_tab(url)
        elif action_type == "activate_tab":
            await session.activate_tab(str(payload.get("tab_id") or payload.get("tabId") or ""))
        elif action_type == "close_tab":
            await session.close_tab(str(payload.get("tab_id") or payload.get("tabId") or ""))
        else:
            raise HTTPException(400, "Unsupported action")

        return JSONResponse({
            "ok": True,
            "url": session.last_url,
            "title": session.last_title,
            "status": session.last_status,
            "render_mode": session.effective_mode,
            "route_mode": session.route_mode,
            "tabs": session.tabs_payload(),
            "active_tab_id": session.active_tab_id,
        })

    @app.delete("/api/browser/sessions/{session_id}")
    async def close_browser_session(session_id: str, request: Request) -> JSONResponse:
        await require_access(request)
        session = runtime.get(session_id)
        await session.close()
        return JSONResponse({"ok": True})

    @app.websocket("/ws/session/{session_id}")
    async def session_ws(websocket: WebSocket, session_id: str, ticket: str) -> None:
        try:
            auth.require_embed_ticket(ticket, session_id=session_id)
        except Exception:
            await websocket.close(code=1008)
            return

        session = runtime.get(session_id)
        await runtime.hub.connect(session_id, websocket)
        try:
            await websocket.send_json({
                "type": "session_update",
                "url": session.last_url,
                "title": session.last_title,
                "status": session.last_status,
                "render_mode": session.effective_mode,
                "route_mode": session.route_mode,
                "tabs": session.tabs_payload(),
                "active_tab_id": session.active_tab_id,
                "stream_reason": session.force_stream_reason,
            })
            while True:
                data = await websocket.receive_json()
                if not isinstance(data, dict):
                    continue
                cmd = str(data.get("type", "")).lower()
                if cmd == "ping":
                    await websocket.send_json({"type": "pong", "time": utc_now()})
                elif cmd == "navigate":
                    await session.navigate(str(data.get("url", "")))
                elif cmd == "reload":
                    await session.reload()
                elif cmd == "new_tab":
                    await session.create_tab(str(data.get("url", "about:blank")))
                elif cmd == "activate_tab":
                    await session.activate_tab(str(data.get("tab_id", "")))
                elif cmd == "close_tab":
                    await session.close_tab(str(data.get("tab_id", "")))
        except WebSocketDisconnect:
            pass
        finally:
            await runtime.hub.disconnect(session_id, websocket)

    @app.websocket("/ws/frame/{session_id}")
    async def frame_ws(websocket: WebSocket, session_id: str, ticket: str) -> None:
        try:
            auth.require_embed_ticket(ticket, session_id=session_id)
        except Exception:
            await websocket.close(code=1008)
            return

        session = runtime.get(session_id)
        await runtime.frame_hub.connect(session_id, websocket)
        try:
            frame = await session.latest_frame()
            if frame:
                await websocket.send_bytes(frame)
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await runtime.frame_hub.disconnect(session_id, websocket)

    @app.get("/api/install/guide")
    async def install_guide() -> PlainTextResponse:
        return PlainTextResponse(get_install_guide())

    @app.get("/api/diagnostics")
    async def diagnostics(request: Request) -> JSONResponse:
        await require_access(request)
        return JSONResponse({
            "python": sys.version,
            "cwd": str(pathlib.Path.cwd()),
            "config_path": str(CONFIG_PATH),
            "data_dir": str(DEFAULT_DATA_DIR),
            "tmux": which("tmux") is not None,
            "nginx": which("nginx") is not None,
            "certbot": which("certbot") is not None,
            "curl": which("curl") is not None,
            "ip": which("ip") is not None,
            "wg": which("wg") is not None,
            "tor": which("tor") is not None,
            "pyotp": pyotp is not None,
            "qrcode": qrcode is not None,
            "playwright_installed": async_playwright is not None,
            "httpx_installed": httpx is not None,
            "bs4_installed": BeautifulSoup is not None,
            "egress": egress.describe(),
            "node_settings": node_settings_payload(),
            "terminal_sessions": len(terminals.sessions),
            "browser_sessions": len(runtime.sessions),
        })

    return app



def build_tmux_start_command(python_executable: str | None = None) -> str:
    python_executable = python_executable or sys.executable
    return (
        f"tmux new-session -d -s vortex-network "
        f"{shlex_quote(python_executable)} {shlex_quote(str(pathlib.Path(__file__).resolve()))} run"
    )


def print_tmux_hint() -> None:
    print("\nTmux tips:")
    print("  detach: Ctrl+b then d")
    print("  attach: tmux attach -t vortex-network")
    print("  list:   tmux ls")
    print()


def install_systemd_service() -> None:
    if sys.platform != "linux":
        print("Systemd install is only supported automatically on Linux.")
        return

    service_path = pathlib.Path("/etc/systemd/system/vortex-node.service")
    python_exe = sys.executable
    exec_cmd = f"{shlex_quote(python_exe)} {shlex_quote(str(SCRIPT_PATH))} run"

    content = f"""[Unit]
Description={APP_NAME}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={SCRIPT_DIR}
EnvironmentFile=-/etc/default/vortex-node
ExecStart={exec_cmd}
Restart=on-failure
RestartSec=3
User=root
Group=root
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=false
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_SYS_ADMIN
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_SYS_ADMIN

[Install]
WantedBy=multi-user.target
"""

    if os.geteuid() != 0:
        print("Run with sudo to install the systemd service automatically.")
        print("Copy this into /etc/systemd/system/vortex-node.service:\n")
        print(content)
        print("Then run:\n  sudo systemctl daemon-reload\n  sudo systemctl enable --now vortex-node")
        return

    service_path.write_text(content, "utf-8")
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "enable", "--now", "vortex-node"], check=False)
    print(f"Installed and started {service_path}")


def get_install_guide() -> str:
    current_port = read_json(CONFIG_PATH, {"server": {"port": 8787}}).get("server", {}).get("port", 8787)
    return textwrap.dedent(
        f"""
        {APP_NAME} {APP_VERSION}
        ======================

        Ubuntu 24 install
        ------------------------------------
        1) Copy vortex_network.py and your Vortex OS HTML file (for example: vortex_os.html) into the same folder.

        2) Install system packages you are likely to need:
           sudo apt update
           sudo apt install -y tmux nginx certbot python3-certbot-nginx curl iproute2 wireguard wireguard-tools tor ffmpeg xvfb x11-xserver-utils pulseaudio pulseaudio-utils dbus-x11

        3) Create and activate a conda environment:
           conda create -n vortex-node python=3.11 -y
           conda activate vortex-node

        4) Install Python packages:
           pip install fastapi uvicorn[standard] httpx[socks] beautifulsoup4 lxml playwright pyotp qrcode[pil] aiortc av
           playwright install chromium
           sudo $(which python) -m playwright install-deps chromium

        5) Run the installer with sudo so route modes / systemd can be configured automatically:
           sudo $(which python) vortex_network.py install

        6) Start the node:
           sudo $(which python) vortex_network.py run

        7) Optional tmux background start:
           sudo {build_tmux_start_command()}

        8) Put Nginx in front of the node and terminate TLS there.
           The Python service can stay on 127.0.0.1:{current_port}

        9) Open your node URL in a browser. If frontend serving is enabled, the node will serve the Vortex OS UI at /.

        10) Unlock your Vortex OS profile, then connect Vortex OS to the node from Settings > Network.

        11) To use microphone passthrough from the browser, serve the node over HTTPS. Browser microphone capture requires a secure context.
        """
    ).strip() + "\n"


def print_nginx_snippet(cfg: NodeConfig | None = None) -> None:
    cfg = cfg or NodeConfig(read_json(CONFIG_PATH, {}))
    public_base = cfg.server.get("public_base_url", "https://node.example.com")
    parsed = urllib.parse.urlparse(public_base)
    server_name = parsed.netloc or "node.example.com"
    upstream = f"http://{cfg.server.get('host', '127.0.0.1')}:{cfg.server.get('port', 8787)}"

    print(textwrap.dedent(f"""
    Example Nginx reverse proxy
    ---------------------------
    map $http_upgrade $connection_upgrade {{
        default upgrade;
        ''      close;
    }}

    server {{
        listen 80;
        server_name {server_name};

        location / {{
            proxy_pass {upstream};
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
        }}
    }}

    Then obtain TLS with Certbot:
      sudo certbot --nginx
      sudo certbot renew --dry-run
    """).strip())


def doctor() -> int:
    problems = []
    print(f"{APP_NAME} doctor\n")

    for name in ["tmux", "nginx", "certbot", "curl", "ip", "wg", "tor", "ffmpeg", "Xvfb", "xrandr", "pulseaudio", "pactl"]:
        print(f"{name}: {which(name) or 'not found'}")

    if FastAPI is None:
        problems.append("fastapi is not installed")
    if uvicorn is None:
        problems.append("uvicorn is not installed")
    if httpx is None:
        problems.append("httpx is not installed")
    if BeautifulSoup is None:
        problems.append("beautifulsoup4 / lxml are not installed")
    if async_playwright is None:
        problems.append("playwright is not installed")
    if pyotp is None:
        problems.append("pyotp is not installed")
    if qrcode is None:
        problems.append("qrcode[pil] is not installed")
    if RTCPeerConnection is None:
        problems.append("aiortc is not installed")
    if av is None:
        problems.append("av is not installed")

    if not CONFIG_PATH.exists():
        problems.append(f"missing config: {CONFIG_PATH}")
    else:
        print(f"config: {CONFIG_PATH}")

    if problems:
        print("\nProblems:")
        for p in problems:
            print(f" - {p}")
        return 1

    print("\nEverything required by the script looks present.")
    return 0


def install_flow() -> int:
    bootstrap_runtime()
    cfg = ask_install_config()
    print(f"\nSaved config to {CONFIG_PATH}\n")

    if cfg["ops"].get("run_on_boot"):
        install_systemd_service()

    if cfg["server"].get("exposure_mode") == "public" and cfg["ops"].get("auto_https"):
        print("Configuring Nginx and HTTPS...")
        configure_public_https(NodeConfig(cfg), str(cfg["ops"].get("certbot_email", "")))

    if cfg["ops"].get("use_tmux"):
        if which("tmux") is None:
            print("tmux is not installed.")
            if sys.platform == "linux":
                print("Install it with: sudo apt install tmux  (or your distro equivalent)")
            elif sys.platform == "darwin":
                print("Install it with: brew install tmux")
            elif sys.platform == "win32":
                print("tmux is not available natively on Windows. Skip tmux or use WSL.")
        else:
            print("tmux is available.")
            print(f"Background start command:\n  {build_tmux_start_command()}\n")
            print_tmux_hint()

    print("Nginx/Certbot snippet:\n")
    print_nginx_snippet(NodeConfig(cfg))
    print("\nInstaller finished. Start the node with:\n  python vortex_network.py run\n")
    return 0


def run_server() -> int:
    cfg_raw = migrate_config(read_json(CONFIG_PATH, {})) if CONFIG_PATH.exists() else {}
    apply_startup_updates(cfg_raw, restart_after_backend_update=True)
    bootstrap_runtime()

    if not CONFIG_PATH.exists():
        print(f"Missing config: {CONFIG_PATH}. Run: python vortex_network.py install")
        return 1

    cfg_raw = migrate_config(read_json(CONFIG_PATH, {}))
    write_json(CONFIG_PATH, cfg_raw)
    cfg = NodeConfig(cfg_raw)
    app = create_app(cfg)
    host = cfg.server.get("host", "127.0.0.1")
    port = int(cfg.server.get("port", 8787))
    print(f"\n{APP_NAME} {APP_VERSION}")
    print(f"Binding on {host}:{port}")
    print(f"Public base URL: {cfg.server.get('public_base_url')}")
    print(f"Egress mode: {cfg.egress.get('route_mode')}")
    print(f"Browser mode: {cfg.browser.get('mode')}")
    if cfg.raw.get("ops", {}).get("use_tmux"):
        print_tmux_hint()
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install", help="interactive install/config wizard")
    sub.add_parser("run", help="run the node")
    sub.add_parser("doctor", help="check dependencies and config")
    sub.add_parser("bootstrap", help="install or repair runtime dependencies")
    sub.add_parser("update", help="check GitHub for vortex_node.py and vortex_os.html updates")
    sub.add_parser("factory-reset", help="wipe all node state and optionally reinstall")
    sub.add_parser("service-install", help="install systemd service (Linux)")
    sub.add_parser("print-nginx", help="print example Nginx/Certbot instructions")
    args = parser.parse_args(argv)

    if args.command == "install":
        return install_flow()
    if args.command == "run":
        return run_server()
    if args.command == "doctor":
        return doctor()
    if args.command == "service-install":
        install_systemd_service()
        return 0
    if args.command == "print-nginx":
        cfg = NodeConfig(read_json(CONFIG_PATH, {"server": {"host": "127.0.0.1", "port": 8787, "public_base_url": "https://node.example.com"}}))
        print_nginx_snippet(cfg)
        return 0
    if args.command == "bootstrap":
        bootstrap_runtime()
        print("Runtime dependencies are ready.")
        return 0
    if args.command == "update":
        result = apply_startup_updates(read_json(CONFIG_PATH, {}) if CONFIG_PATH.exists() else {}, restart_after_backend_update=False)
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "factory-reset":
        reinstall = prompt_yes_no("Run the installer again after wiping the node?", default=True)
        return factory_reset_flow(reinstall=reinstall)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
