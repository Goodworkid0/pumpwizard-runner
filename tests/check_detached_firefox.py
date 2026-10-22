"""Exercise the real Firefox popup lifecycle using disposable browser/API data.

Requires local Firefox and geckodriver. Uses W3C WebDriver directly so this
check neither installs a browser nor requires Selenium or a user's profile.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
ELEMENT = "element-6066-11e4-a52e-4f735466cecf"


def free_port() -> int:
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def wait_until(task, description: str, timeout: float = 20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = task()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError(description)


def port_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-root", type=Path, default=ROOT / "web-client/dist")
    parser.add_argument("--python", default=os.environ.get("PUMPWIZARD_E2E_PYTHON", "python3"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    firefox, geckodriver = shutil.which("firefox"), shutil.which("geckodriver")
    if not firefox or not geckodriver:
        raise SystemExit("Install Firefox and geckodriver locally before running this check.")
    web_port, driver_port = free_port(), free_port()
    driver_origin = f"http://127.0.0.1:{driver_port}"

    def request(path: str, body=None, method: str | None = None):
        payload = json.dumps(body).encode() if body is not None else None
        query = urllib.request.Request(
            driver_origin + path, data=payload, method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(query, timeout=30) as response:
            return json.load(response)["value"]

    with tempfile.TemporaryDirectory(prefix="pumpwizard-firefox-popup-") as directory:
        folder = Path(directory)
        # Exercise the exact supplied client assets with the synthetic source API.
        # Its development identity cannot match a packaged executable's identity;
        # omit only that metadata in the disposable copy, never the real bundle.
        client_root = folder / "web-client"
        shutil.copytree(args.web_root.resolve(), client_root,
                        ignore=shutil.ignore_patterns("build-identity.json"))
        with (folder / "api.log").open("w") as api_log, (folder / "driver.log").open("w") as driver_log:
            server = subprocess.Popen(
                [args.python, str(ROOT / "tests/browser_e2e_server.py"),
                 "--web-root", str(client_root), "--port", str(web_port),
                 "--data-dir", str(folder / "state")],
                cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                stdout=api_log, stderr=subprocess.STDOUT,
            )
            driver = subprocess.Popen(
                [geckodriver, "--host", "127.0.0.1", "--port", str(driver_port)],
                stdout=driver_log, stderr=subprocess.STDOUT,
            )
            session = None
            try:
                wait_until(lambda: port_ready(web_port), "Synthetic API did not start")
                wait_until(lambda: port_ready(driver_port), "WebDriver did not start")
                created = request("/session", {"capabilities": {"alwaysMatch": {
                    "browserName": "firefox",
                    "moz:firefoxOptions": {"binary": firefox, "args": ["-headless"]},
                }}})
                session = created["sessionId"]
                prefix = "/session/" + session

                def execute(script: str):
                    return request(prefix + "/execute/sync", {"script": script, "args": []})

                def click(selector: str):
                    element = request(prefix + "/element", {
                        "using": "css selector", "value": selector,
                    })[ELEMENT]
                    request(prefix + "/element/" + element + "/click", {})

                def wait_script(script: str):
                    return wait_until(lambda: execute(script), "Browser condition failed: " + script)

                request(prefix + "/url", {"url": f"http://127.0.0.1:{web_port}/"})
                wait_script('return !!document.querySelector(".research-entry-choice")')
                click(".research-entry-choice")
                wait_script('return !!document.querySelector(".command-board-detach-button")')
                main_window = request(prefix + "/window")
                execute("""
                    const open = window.open;
                    window.open = function(...args) {
                        return window.__popup = open.apply(this, args);
                    };
                """)
                click(".command-board-detach-button")
                # The old Firefox race closed the window within milliseconds.
                # A timer in the popup proves its initial document survives
                # startup and multiple lifecycle polling intervals.
                execute("window.__popup.setTimeout(() => window.__popup.__startupReady = true, 1500)")
                wait_script("return window.__popup?.__startupReady === true")
                persistence = execute("""
                    const popup = window.__popup;
                    const panel = popup?.closed ? null :
                        popup.document.querySelector('.command-board-popout');
                    return {
                        remains_open: !!popup && !popup.closed,
                        portal_connected: !!panel?.isConnected,
                        styles_loaded: !!panel && popup.getComputedStyle(
                            panel.querySelector('.agent-terminal')).display === 'flex',
                        status_history: !!panel?.querySelector('[role=log]')?.textContent
                            .includes('RPC sync test'),
                        startup_timer_completed: popup.__startupReady === true
                    };
                """)
                assert all(persistence[name] for name in (
                    "remains_open", "portal_connected", "styles_loaded", "status_history",
                )), persistence
                windows = request(prefix + "/window/handles")
                popup_window = next(item for item in windows if item != main_window)
                request(prefix + "/window", {"handle": popup_window})
                click(".command-connect button")
                wait_script('return !!document.querySelector("[role=dialog]")')
                wait_script('return document.activeElement === document.querySelector("button.close")')
                click("button.close")
                request(prefix + "/window", {"handle": main_window})
                click(".command-board-detached-placeholder button")
                wait_script('return !!document.querySelector(".command-board-stream")')
                wait_until(lambda: len(request(prefix + "/window/handles")) == 1,
                           "Return here did not close the companion")

                click(".command-board-details > summary")
                click(".command-board-detach-button")
                wait_script('return !!window.__popup?.document.querySelector(".command-board-popout")')
                popup_window = next(item for item in request(prefix + "/window/handles")
                                    if item != main_window)
                request(prefix + "/window", {"handle": popup_window})
                request(prefix + "/window", method="DELETE")
                request(prefix + "/window", {"handle": main_window})
                wait_script('return document.querySelector(".command-board-detach-button")'
                            '.textContent.includes("Open in new window")')
                click(".command-board-detach-button")
                wait_script('return !!window.__popup?.document.querySelector(".command-board-popout")')
                request(prefix + "/refresh", {})
                wait_until(lambda: len(request(prefix + "/window/handles")) == 1,
                           "Reloading the Dashboard did not close the companion")
                result = {
                    "browser": created["capabilities"]["browserVersion"],
                    "web_root": str(args.web_root.resolve()),
                    "persistence": persistence,
                    "checks": ["Initial popup document survives startup and lifecycle checks",
                               "Portal, styles and status history remain available",
                               "AI settings and keyboard focus work in the companion",
                               "Return here restores the inline view",
                               "Collapsed-board detach and native closure work",
                               "Dashboard reload closes the companion"],
                    "limits": "Isolated walletless synthetic API and new Firefox profile; "
                              "client assets copied unchanged except build-identity metadata; "
                              "no user data, wallet signature or provider request.",
                }
                if args.output:
                    args.output.write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps(result, indent=2))
            finally:
                try:
                    if session:
                        request("/session/" + session, method="DELETE")
                finally:
                    driver.terminate()
                    server.terminate()
                    driver.wait(timeout=10)
                    server.wait(timeout=10)


if __name__ == "__main__":
    main()
