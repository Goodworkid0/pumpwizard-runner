"""Offline onboarding preview: no wallet, network, signing or order creation."""

import copy
import os
from pathlib import Path
import socket
import tempfile
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch
from types import SimpleNamespace

from pumpwizard_runner.desktop import RunnerWindow
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.strategies import parse_package
from pumpwizard_runner.strategy_browser import StrategyBrowser
from pumpwizard_runner.strategy_onboarding import show_import
from pumpwizard_runner.strategy_support import import_comparison


def buttons(widget):
    for child in widget.winfo_children():
        if isinstance(child, ttk.Button):
            yield child
        yield from buttons(child)


def main():
    with tempfile.TemporaryDirectory(prefix="wizard-onboarding-preview-") as directory, patch.object(
        socket.socket, "connect", side_effect=AssertionError("Onboarding tried live network")
    ):
        library = StrategyLibrary(Path(directory))
        root = tk.Tk()
        RunnerWindow._configure_style(SimpleNamespace(root=root))
        root.geometry("1100x800")
        errors, opened = [], []
        root.report_callback_exception = lambda *args: errors.append(args)
        try:
            browser = StrategyBrowser(root, library, Path(directory), lambda: None,
                                      opened.append, lambda: None)
            browser.pack(fill="both", expand=True)
            browser.tree.selection_set("core-frequency")
            browser.select()
            browser.show_detail()
            root.update()
            text = browser.support_text.get("1.0", "end")
            assert "Reviewed eligible" in text and "Package SHA-256" in text
            assert "Funded agent lifecycle acceptance: pending" in text
            if os.environ.get("WIZARD_UI_SCREENSHOTS"):
                from PIL import ImageGrab

                ImageGrab.grab().save("/tmp/pumpwizard-onboarding-catalog.png")
            package = {"schema": "pumpwizard.strategy-handoff/v1",
                       "strategy": {"id": "core-frequency", "label": "Imported core"},
                       "configuration": copy.deepcopy(CONFIG)}
            strategy = parse_package(package)
            comparison = import_comparison(library, strategy)
            library.import_strategy(strategy)
            window = show_import(root, library, strategy, comparison, opened.append)
            root.update()
            assert library.instances() == []
            if os.environ.get("WIZARD_UI_SCREENSHOTS"):
                ImageGrab.grab().save("/tmp/pumpwizard-onboarding-import.png")
            next(b for b in buttons(window) if b.cget("text").startswith("Create session")).invoke()
            root.update()
            agent = opened[-1]
            assert library.instance(agent)["entry_mode"] == "disabled"
            assert not library.tracking_settings(agent)["enabled"]
            window = show_import(root, library, strategy, import_comparison(library, strategy), opened.append)
            next(b for b in buttons(window) if b.cget("text") == "Open existing session").invoke()
            assert opened[-1] == agent and len(library.instances()) == 1
            package["trial"] = {"version": 3}
            trial = parse_package(package)
            library.import_strategy(trial)
            window = show_import(root, library, trial, import_comparison(library, trial), opened.append)
            root.update()
            assert [b.cget("text") for b in buttons(window)] == ["Close"]
            assert len(library.instances()) == 1
            assert not list(Path(directory).glob("*wallet*")) and not errors
            print("PASS: catalog capabilities, inspectable import, disabled agent creation, "
                  "duplicate reuse and inspection-only overlay; no network or wallet")
        finally:
            root.destroy()
            library.close()


if __name__ == "__main__":
    main()
