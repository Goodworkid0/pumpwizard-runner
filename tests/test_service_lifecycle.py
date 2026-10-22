import json
import os
from pathlib import Path
import socket
import stat
from threading import Thread
from urllib.request import Request, urlopen

import pytest

from pumpwizard_runner import __version__, cli
from pumpwizard_runner import service_lifecycle
from pumpwizard_runner.background_service import BackgroundServiceEngine
from pumpwizard_runner.service_lifecycle import (
    UNIT_MARKER,
    access_token,
    install_user_unit,
    remove_user_unit,
    render_user_unit,
    start,
)
from pumpwizard_runner.web_api import serve


def test_private_access_token_and_version_pinned_user_unit(tmp_path: Path):
    data_dir = (tmp_path / "data").resolve()
    first = access_token(data_dir, create=True)
    assert first == access_token(data_dir, create=False)
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((data_dir / "background-service-access-token").stat().st_mode) == 0o600

    executable = (tmp_path / f"PumpWizard-{__version__}" / "pumpwizard").resolve()
    unit = render_user_unit(executable=executable, data_dir=data_dir)
    assert unit.startswith(UNIT_MARKER)
    assert str(executable) in unit and str(data_dir) in unit
    assert f'"--expected-version" "{__version__}"' in unit
    assert "Restart=on-failure" in unit
    assert "systemctl" not in unit and "linger" not in unit

    unit_dir = tmp_path / "units"
    destination = install_user_unit(
        executable=executable, data_dir=data_dir, unit_dir=unit_dir
    )
    assert destination.read_text() == unit
    assert remove_user_unit(unit_dir=unit_dir) is True
    assert remove_user_unit(unit_dir=unit_dir) is False


def test_access_token_rejects_symlinks_and_public_permissions(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = tmp_path / "not-a-service-token"
    target.write_text("x" * 32)
    token_path = data_dir / "background-service-access-token"
    token_path.symlink_to(target)
    with pytest.raises(ValueError, match="regular file"):
        access_token(data_dir, create=False)
    token_path.unlink()
    token_path.write_text("x" * 32)
    token_path.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        access_token(data_dir, create=False)


def test_user_unit_never_replaces_or_removes_a_foreign_file(tmp_path: Path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    destination = unit_dir / "pumpwizard-runner.service"
    destination.write_text("[Service]\nExecStart=/foreign\n")
    with pytest.raises(ValueError, match="replace"):
        install_user_unit(
            executable=(tmp_path / "runner").resolve(), data_dir=(tmp_path / "data").resolve(),
            unit_dir=unit_dir,
        )
    with pytest.raises(ValueError, match="remove"):
        remove_user_unit(unit_dir=unit_dir)
    assert destination.read_text() == "[Service]\nExecStart=/foreign\n"


def test_user_unit_preserves_an_absolute_versioned_interpreter_symlink(tmp_path: Path):
    executable = tmp_path / "runner-venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to("/usr/bin/python3")
    destination = install_user_unit(
        executable=executable,
        data_dir=(tmp_path / "data").resolve(),
        unit_dir=tmp_path / "units",
    )
    assert str(executable) in destination.read_text()
    assert 'ExecStart="/usr/bin/python3"' not in destination.read_text()


def test_foreign_occupied_port_fails_closed_before_process_spawn(tmp_path: Path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        with pytest.raises(ValueError, match="unverified service"):
            start(tmp_path, port=listener.getsockname()[1], wait_seconds=.1)
    finally:
        listener.close()


def test_background_start_preserves_the_selected_python_symlink(tmp_path: Path, monkeypatch):
    executable = tmp_path / "runtime-venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to("/usr/bin/python3")
    expected = {
        "service": {
            "schema": "pumpwizard.background-service-status/v1",
            "data_directory_id": service_lifecycle.data_directory_id(tmp_path),
        }
    }
    probes = iter((None, expected))
    captured = []
    monkeypatch.setattr(service_lifecycle, "probe", lambda *_args, **_kwargs: next(probes))
    monkeypatch.setattr(service_lifecycle, "_port_occupied", lambda _port: False)
    monkeypatch.setattr(service_lifecycle.sys, "executable", str(executable))
    monkeypatch.setattr(
        service_lifecycle.subprocess, "Popen",
        lambda command, **kwargs: captured.append((command, kwargs)),
    )
    result = service_lifecycle.start(tmp_path, port=8781, wait_seconds=.1)
    assert result["reused"] is False
    assert captured[0][0][0] == str(executable)
    assert captured[0][0][0] != "/usr/bin/python3"


def test_background_start_uses_frozen_executable_without_python_module_flags(
    tmp_path: Path, monkeypatch,
):
    executable = tmp_path / "candidate" / "SolanaMarketWizard"
    executable.parent.mkdir(parents=True)
    executable.write_text("candidate")
    expected = {
        "service": {
            "schema": "pumpwizard.background-service-status/v1",
            "data_directory_id": service_lifecycle.data_directory_id(tmp_path),
            "version": __version__,
        }
    }
    probes = iter((None, expected))
    captured = []
    monkeypatch.setattr(service_lifecycle, "probe", lambda *_args, **_kwargs: next(probes))
    monkeypatch.setattr(service_lifecycle, "_port_occupied", lambda _port: False)
    monkeypatch.setattr(service_lifecycle.sys, "executable", str(executable))
    monkeypatch.setattr(service_lifecycle.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        service_lifecycle.subprocess, "Popen",
        lambda command, **kwargs: captured.append((command, kwargs)),
    )

    result = service_lifecycle.start(tmp_path, port=8782, wait_seconds=.1)

    assert result["reused"] is False
    assert captured[0][0][:3] == [str(executable), "--data-dir", str(tmp_path.resolve())]
    assert "-m" not in captured[0][0]


def test_background_start_explains_authenticated_version_conflict(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service_lifecycle, "probe", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service_lifecycle, "_port_occupied", lambda _port: True)
    monkeypatch.setattr(service_lifecycle, "_authenticated_probe", lambda *_args, **_kwargs: {
        "service": {
            "schema": "pumpwizard.background-service-status/v1",
            "data_directory_id": service_lifecycle.data_directory_id(tmp_path),
            "version": "0.1.0-old",
        }
    })

    with pytest.raises(ValueError, match="different Runner version"):
        service_lifecycle.start(tmp_path, port=8783, wait_seconds=.1)


def test_authenticated_service_routes_and_shutdown_lock_the_engine(tmp_path: Path):
    token = "service-test-token-which-is-long-enough"
    engine = BackgroundServiceEngine(tmp_path, port=0)
    server = serve(tmp_path, token, port=0, background_service=engine)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {
        "Origin": "http://127.0.0.1:5173",
        "X-PumpWizard-Browser-Token": token,
        "Content-Type": "application/json",
    }
    try:
        with urlopen(Request(base + "/api/v1/service/status", headers=headers)) as response:
            status = json.loads(response.read())
        assert status["service"]["state"] == "running"
        assert status["jobs"][0]["enabled"] is False
        assert status["trading"]["signer_state"] == "locked"

        body = json.dumps({"enabled": False, "interval_seconds": 20}).encode()
        request = Request(
            base + "/api/v1/service/jobs/strategy-monitor",
            data=body, headers=headers, method="POST",
        )
        with urlopen(request) as response:
            configured = json.loads(response.read())
        assert configured["job"]["interval_seconds"] == 20

        shutdown = Request(
            base + "/api/v1/service/shutdown", data=b"{}", headers=headers, method="POST"
        )
        with urlopen(shutdown) as response:
            result = json.loads(response.read())
        assert result["requested"] is True
        assert "not cancelled" in result["notice"]
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_service_cli_parses_lifecycle_without_installing_or_starting(tmp_path: Path, monkeypatch, capsys):
    unit_dir = tmp_path / "units"
    executable = tmp_path / "versioned" / "pumpwizard"
    result = cli.main([
        "--data-dir", str(tmp_path / "data"), "service", "install-user",
        "--executable", str(executable), "--unit-dir", str(unit_dir),
    ])
    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["enabled"] is False and output["started"] is False
    assert output["linger_changed"] is False
    assert os.path.isabs(output["installed"])


def test_service_cli_launch_dashboard_reuses_service_without_wallet_authority(
    tmp_path: Path, monkeypatch, capsys,
):
    data_dir = tmp_path / "data"
    web_root = tmp_path / "web"
    web_root.mkdir()
    (web_root / "index.html").write_text("<!doctype html><title>Runner</title>")
    expected = {
        "service": {
            "schema": "pumpwizard.background-service-status/v1",
            "instance_id": "service-1",
            "instance_revision": 1,
            "data_directory_id": "a" * 64,
            "version": __version__,
            "state": "running",
            "health": "healthy",
            "production_trading": "blocked",
        },
        "reused": True,
    }
    starts = []
    monkeypatch.setattr(
        service_lifecycle, "start",
        lambda selected, **options: starts.append((selected, options)) or expected,
    )
    monkeypatch.setattr(
        service_lifecycle, "dashboard_url",
        lambda selected, **options: "http://127.0.0.1:8845/",
    )
    monkeypatch.setattr(cli.webbrowser, "open_new_tab", lambda location: location.endswith(":8845/"))

    assert cli.main([
        "--data-dir", str(data_dir), "service", "launch-dashboard",
        "--port", "8845", "--web-root", str(web_root),
    ]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["reused"] is True
    assert result["opened"] is True
    assert result["service"]["production_trading"] == "blocked"
    assert "did not unlock a wallet" in result["notice"]
    assert starts == [(
        data_dir.resolve(), {"port": 8845, "web_root": web_root.resolve()},
    )]
