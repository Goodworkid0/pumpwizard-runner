"""Browser-first CLI launch behavior without opening a real browser or socket."""

from pathlib import Path
from unittest.mock import patch
import pytest

from pumpwizard_runner import cli


@pytest.mark.parametrize('mac_app', [False, True])
def test_frozen_executable_finds_folder_or_macos_resources(tmp_path, mac_app):
    executable = tmp_path / ('Terminal.app/Contents/MacOS/Terminal' if mac_app else 'Terminal/Terminal.exe')
    assets = executable.parent.parent/'Resources/web-client' if mac_app else executable.parent/'web-client'
    assets.mkdir(parents=True)
    (assets/'index.html').write_text('<html>Terminal</html>')
    with patch.object(cli.sys, 'frozen', True, create=True), patch.object(cli.sys, 'executable', str(executable)):
        assert cli._browser_root() == assets


class FakeServer:
    server_address = ("127.0.0.1", 43210)

    def __init__(self):
        self.closed = False

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        self.closed = True


def test_no_command_starts_browser_app_with_internal_token(tmp_path: Path):
    web_root = tmp_path / "web"
    web_root.mkdir()
    (web_root / "index.html").write_text("<title>Runner</title>", encoding="utf-8")
    server = FakeServer()
    with (
        patch.object(cli, "_browser_root", return_value=web_root),
        patch("pumpwizard_runner.web_api.serve", return_value=server) as serve,
        patch.object(cli.webbrowser, "open_new_tab", return_value=True) as opened,
    ):
        assert cli.main(["--data-dir", str(tmp_path / "data")]) == 0

    data_dir, token, host, port, selected_root = serve.call_args.args
    assert data_dir == (tmp_path / "data").resolve()
    assert len(token) >= 24
    assert host == "127.0.0.1" and port == 8765 and selected_root == web_root
    opened.assert_called_once_with("http://127.0.0.1:43210/")
    assert server.closed


def test_browser_no_open_keeps_system_browser_closed(tmp_path: Path):
    web_root = tmp_path / "web"
    web_root.mkdir()
    (web_root / "index.html").write_text("<title>Runner</title>", encoding="utf-8")
    server = FakeServer()
    with (
        patch("pumpwizard_runner.web_api.serve", return_value=server),
        patch.object(cli.webbrowser, "open_new_tab") as opened,
    ):
        result = cli.main([
            "--data-dir", str(tmp_path / "data"), "browser", "--web-root", str(web_root),
            "--port", "0", "--no-open",
        ])

    assert result == 0
    opened.assert_not_called()
