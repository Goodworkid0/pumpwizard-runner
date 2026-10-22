"""Offline source desktop evaluation acceptance; no live sockets or wallet."""

import copy
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
from unittest.mock import patch

from pumpwizard_runner import evaluation
from pumpwizard_runner.desktop import RunnerWindow
from pumpwizard_runner.evaluation_panel import EvaluationPanel
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.strategies import parse_package


def main():
    with tempfile.TemporaryDirectory(prefix="wizard-evaluation-ui-") as directory, patch.object(
        socket.socket, "connect", side_effect=AssertionError("Evaluation tried live network")
    ):
        directory = Path(directory)
        library = StrategyLibrary(directory)
        package = {"schema": "pumpwizard.strategy-handoff/v1",
                   "strategy": {"id": "core-frequency", "label": "Core evaluation"},
                   "configuration": copy.deepcopy(CONFIG)}
        strategy = parse_package(package)
        library.import_strategy(strategy)
        agent = library.create_instance(strategy.digest)
        before = list(library._connection.iterdump())
        root = tk.Tk()
        RunnerWindow._configure_style(SimpleNamespace(root=root))
        root.withdraw()
        errors, navigation = [], []
        root.report_callback_exception = lambda *args: errors.append(args)
        panel = EvaluationPanel(root, directory, library.instance(agent), lambda *args: navigation.append(args))

        def wait(predicate):
            deadline = time.monotonic() + 15
            while not predicate():
                root.update()
                assert not errors
                if time.monotonic() > deadline:
                    raise AssertionError("Evaluation UI timed out: " + panel.status.cget("text"))
                time.sleep(.02)
            root.update()
            assert not errors

        try:
            assert not evaluation.reports(directory, strategy.digest)
            panel.example_button.invoke()
            wait(lambda: not panel.busy)
            assert panel.selected['summary']['event_counts']['fill_partial_exit'] == 1
            assert "bundled synthetic example" in panel.detail.get("1.0", "end")
            assert strategy.digest in panel.detail.get("1.0", "end")
            if os.environ.get("WIZARD_UI_SCREENSHOTS"):
                from PIL import ImageGrab

                panel.window.lift()
                root.update()
                ImageGrab.grab().save("/tmp/pumpwizard-dev41-evaluation.png")
            destination = directory / "export.json"
            with patch("pumpwizard_runner.evaluation_panel.filedialog.asksaveasfilename", return_value=str(destination)):
                panel.export_button.invoke()
            exported = json.loads(destination.read_text())
            assert exported == panel.selected
            ident = exported['id']
            panel.close()
            panel = EvaluationPanel(root, directory, library.instance(agent), lambda *args: navigation.append(args))
            assert panel.selected['id'] == ident
            path = directory / "recording.json"
            path.write_text(json.dumps(exported['recording']))
            with patch("pumpwizard_runner.evaluation_panel.filedialog.askopenfilename", return_value=str(path)):
                panel.load_button.invoke()
            wait(lambda: not panel.busy)
            assert len(panel.report_rows) == 1 and panel.selected['id'] == ident
            path.write_text('{"schema":"unsupported"}')
            with patch("pumpwizard_runner.evaluation_panel.filedialog.askopenfilename", return_value=str(path)):
                panel.load_button.invoke()
            wait(lambda: not panel.busy)
            assert "not completed" in panel.status.cget('text') and len(panel.report_rows) == 1
            panel.shadow_button.invoke()
            panel.worker_button.invoke()
            assert navigation == [(agent, 2), (agent, 7)]
            assert list(library._connection.iterdump()) == before
            assert not list(directory.glob('*wallet*'))
            panel.close()
            unsupported = library.instance(agent)
            unsupported['package']['configuration']['order_usd'] += 1
            panel = EvaluationPanel(root, directory, unsupported, lambda *args: navigation.append(args))
            assert str(panel.load_button.cget('state')) == 'disabled'
            assert str(panel.shadow_button.cget('state')) == 'disabled'
            assert panel.selected is None
            print("PASS: offline example, report/export identity, reopened history, duplicate replay, "
                  "invalid recording, unsigned navigation, unsupported configuration; no wallet/network changes")
        finally:
            panel.close()
            root.destroy()
            library.close()


if __name__ == '__main__':
    main()
