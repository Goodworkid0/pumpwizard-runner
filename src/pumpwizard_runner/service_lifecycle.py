"""Linux lifecycle helpers for the authenticated local background service."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from . import __version__
from .background_service import BackgroundServiceEngine, SERVICE_PORT


TOKEN_FILE = "background-service-access-token"
UNIT_NAME = "pumpwizard-runner.service"
UNIT_MARKER = "# Managed by PumpWizard Runner"


def access_token(data_dir: Path, *, create: bool) -> str:
    """Read or create the user-only dashboard token; never return it in status."""
    root = Path(data_dir).resolve()
    path = root / TOKEN_FILE
    if path.exists():
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise ValueError("Background service access token must be a private regular file")
        mode = stat.S_IMODE(details.st_mode)
        if mode & 0o077:
            raise ValueError("Background service access token permissions are not private")
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 24:
            raise ValueError("Background service access token is invalid")
        return token
    if not create:
        raise ValueError("Background service has not been initialized")
    created_root = not root.exists()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if created_root and os.name != "nt":
        # ``mkdir`` still honors process umask; explicitly retain a private
        # first-use data directory without changing permissions on existing data.
        root.chmod(0o700)
    token = secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, (token + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return token


def data_directory_id(data_dir: Path) -> str:
    return hashlib.sha256(str(Path(data_dir).resolve()).encode()).hexdigest()


def probe(data_dir: Path, *, port: int = SERVICE_PORT,
          timeout: float = 1.0) -> dict[str, object] | None:
    payload = _authenticated_probe(data_dir, port=port, timeout=timeout)
    if payload is None or payload["service"].get("version") != __version__:
        return None
    return payload


def _authenticated_probe(data_dir: Path, *, port: int = SERVICE_PORT,
                         timeout: float = 1.0) -> dict[str, object] | None:
    """Read only the service bound to this data directory, regardless of version."""
    try:
        token = access_token(data_dir, create=False)
    except ValueError:
        return None
    request = urllib.request.Request(
        f"http://127.0.0.1:{_port(port)}/api/v1/service/status",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "X-PumpWizard-Browser-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError):
        return None
    service = payload.get("service") if isinstance(payload, dict) else None
    if not isinstance(service, dict):
        return None
    if service.get("data_directory_id") != data_directory_id(data_dir):
        return None
    if service.get("schema") != "pumpwizard.background-service-status/v1":
        return None
    return payload


def start(data_dir: Path, *, port: int = SERVICE_PORT,
          web_root: Path | None = None, wait_seconds: float = 8.0) -> dict[str, object]:
    selected_port = _port(port)
    running = probe(data_dir, port=selected_port)
    if running is not None:
        return {**running, "reused": True}
    if _port_occupied(selected_port):
        authenticated = _authenticated_probe(data_dir, port=selected_port)
        if (authenticated is not None
                and authenticated["service"].get("version") != __version__):
            raise ValueError(
                "The verified service uses a different Runner version. Stop it with its "
                "current executable, then start this candidate explicitly"
            )
        raise ValueError(
            f"Port {selected_port} is occupied by an unverified service; refusing to reuse it"
        )
    access_token(data_dir, create=True)
    command = [*_runtime_command(),
        "--data-dir", str(Path(data_dir).resolve()), "service", "run",
        "--port", str(selected_port), "--expected-version", __version__,
    ]
    if web_root is not None:
        command.extend(("--web-root", str(Path(web_root).resolve())))
    log_path = Path(data_dir).resolve() / "background-service.log"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(log_path, flags, 0o600)
    try:
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=descriptor, stderr=descriptor,
            start_new_session=True, close_fds=True,
        )
    finally:
        os.close(descriptor)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        running = probe(data_dir, port=selected_port, timeout=0.5)
        if running is not None:
            return {**running, "reused": False}
        time.sleep(0.1)
    raise ValueError(f"Background service did not become healthy; inspect {log_path}")


def stop(data_dir: Path, *, port: int = SERVICE_PORT,
         timeout: float = 5.0) -> dict[str, object]:
    status = probe(data_dir, port=port)
    if status is None:
        if _port_occupied(_port(port)):
            raise ValueError("The occupied service port could not be authenticated; refusing to stop it")
        return {"stopped": True, "detail": "No verified background service is running"}
    response = _post(data_dir, port, "/api/v1/service/shutdown", {})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_occupied(_port(port)):
            return {**response, "stopped": True}
        time.sleep(0.1)
    return {**response, "stopped": False,
            "detail": "Shutdown was requested but the service port is still occupied"}


def configure_job(data_dir: Path, body: dict[str, object], *,
                  port: int = SERVICE_PORT) -> dict[str, object]:
    if probe(data_dir, port=port) is None:
        raise ValueError("A verified background service is not running")
    return _post(data_dir, port, "/api/v1/service/jobs/strategy-monitor", body)


def run_foreground(data_dir: Path, *, port: int = SERVICE_PORT,
                   web_root: Path | None = None,
                   expected_version: str | None = None) -> int:
    if expected_version is not None and expected_version != __version__:
        raise ValueError(
            f"Service unit expects Runner {expected_version}, but this executable is {__version__}"
        )
    from .web_api import serve

    token = access_token(data_dir, create=True)
    engine = BackgroundServiceEngine(Path(data_dir), port=_port(port))
    server = None
    previous_handlers = {}
    try:
        # Bind before taking service ownership so a foreign listener cannot
        # leave a misleading running instance record.
        server = serve(
            Path(data_dir), token, "127.0.0.1", _port(port), web_root,
            background_service=engine,
        )
        engine.start()
        if threading.current_thread() is threading.main_thread():
            def stop_for_signal(signum, _frame):
                engine.request_shutdown(f"Operating-system signal {signum} requested shutdown")
                threading.Thread(
                    target=server.shutdown, name="pumpwizard-signal-shutdown", daemon=True
                ).start()

            for selected_signal in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[selected_signal] = signal.getsignal(selected_signal)
                signal.signal(selected_signal, stop_for_signal)
        server.serve_forever()
        return 0
    finally:
        for selected_signal, handler in previous_handlers.items():
            signal.signal(selected_signal, handler)
        if server is not None:
            server.server_close()


def dashboard_url(data_dir: Path, *, port: int = SERVICE_PORT) -> str:
    if probe(data_dir, port=port) is None:
        if _port_occupied(_port(port)):
            raise ValueError("The service port is occupied by an unverified process")
        raise ValueError("Start the background service before opening its dashboard")
    return f"http://127.0.0.1:{_port(port)}/"


def render_user_unit(*, executable: Path, data_dir: Path, port: int = SERVICE_PORT) -> str:
    selected_executable = Path(executable)
    selected_data = Path(data_dir)
    if not selected_executable.is_absolute() or not selected_data.is_absolute():
        raise ValueError("Service executable and data directory must be absolute paths")
    command = " ".join(_systemd_quote(part) for part in (
        str(selected_executable), "--data-dir", str(selected_data), "service", "run",
        "--port", str(_port(port)), "--expected-version", __version__,
    ))
    return (
        f"{UNIT_MARKER} {__version__}\n"
        "[Unit]\n"
        "Description=PumpWizard local background service\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "NoNewPrivileges=true\n"
        "PrivateTmp=true\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install_user_unit(*, executable: Path, data_dir: Path, port: int = SERVICE_PORT,
                      unit_dir: Path | None = None) -> Path:
    destination_dir = (Path(unit_dir) if unit_dir is not None else
                       Path.home() / ".config" / "systemd" / "user")
    destination = destination_dir / UNIT_NAME
    if destination.exists():
        existing = destination.read_text(encoding="utf-8")
        if not existing.startswith(UNIT_MARKER):
            raise ValueError("Refusing to replace a user service not managed by PumpWizard")
    content = render_user_unit(executable=Path(executable).absolute(),
                               data_dir=Path(data_dir).resolve(), port=port)
    destination_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=UNIT_NAME + ".", dir=destination_dir)
    try:
        os.fchmod(descriptor, 0o644)
        os.write(descriptor, content.encode())
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def remove_user_unit(*, unit_dir: Path | None = None) -> bool:
    destination_dir = (Path(unit_dir) if unit_dir is not None else
                       Path.home() / ".config" / "systemd" / "user")
    destination = destination_dir / UNIT_NAME
    if not destination.exists():
        return False
    if not destination.read_text(encoding="utf-8").startswith(UNIT_MARKER):
        raise ValueError("Refusing to remove a user service not managed by PumpWizard")
    destination.unlink()
    return True


def _post(data_dir: Path, port: int, path: str,
          body: dict[str, object]) -> dict[str, object]:
    token = access_token(data_dir, create=False)
    encoded = json.dumps(body, allow_nan=False).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{_port(port)}{path}", data=encoded, method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:5173",
            "X-PumpWizard-Browser-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("error")
        except (ValueError, json.JSONDecodeError):
            message = None
        raise ValueError(message or "Background service rejected the request") from None


def _runtime_command() -> list[str]:
    """Return the current Runner invocation without resolving versioned symlinks."""
    executable = str(Path(sys.executable).absolute())
    if getattr(sys, "frozen", False):
        return [executable]
    return [executable, "-m", "pumpwizard_runner"]


def _port(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("Service port must be between 1 and 65535")
    return value


def _port_occupied(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.25)
        return client.connect_ex(("127.0.0.1", port)) == 0


def _systemd_quote(value: str) -> str:
    if any(character in value for character in "\n\r\x00"):
        raise ValueError("Service paths cannot contain control characters")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("$", "$$").replace("%", "%%")
    return f'"{escaped}"'
