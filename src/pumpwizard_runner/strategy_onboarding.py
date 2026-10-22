"""Inspectable local import result with an explicit, disabled-agent next step."""

import json
import tkinter as tk
from tkinter import ttk

from .strategy_browser import StrategyBrowser
from .strategy_support import support_text


def show_import(root, library, strategy, comparison, on_open):
    window = tk.Toplevel(root)
    window.title("Imported configuration · " + strategy.label)
    window.geometry("900x720")
    window.minsize(640, 480)
    window.configure(background=ttk.Style().lookup("Panel.TFrame", "background") or "#121a2d")
    tabs = ttk.Notebook(window, style="App.TNotebook")
    tabs.pack(fill="both", expand=True, padx=16, pady=16)
    for title, text in (
        ("Compatibility & evidence", support_text(strategy) + "\n\n" + comparison),
        ("Exact imported configuration", json.dumps(strategy.package, indent=2, ensure_ascii=False)),
    ):
        page = ttk.Frame(tabs, style="Panel.TFrame")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=1)
        box = StrategyBrowser.textbox(None, page, 0, mono=title.startswith("Exact"))
        StrategyBrowser.write(box, text)
        tabs.add(page, text=title)
    actions = ttk.Frame(window, style="Panel.TFrame", padding=(16, 0, 16, 16))
    actions.pack(fill="x")

    def open_agent():
        agent = next((row["id"] for row in library.instances()
                      if row["strategy_digest"] == strategy.digest), None)
        if agent is None:
            agent = library.create_instance(strategy.digest, entry_mode="disabled", exit_mode="manual")
        window.destroy()
        on_open(agent)

    ttk.Button(actions, text="Close", style="Subtle.TButton", command=window.destroy).pack(side="right")
    if strategy.compatibility == "monitoring-ready":
        existing = any(row["strategy_digest"] == strategy.digest for row in library.instances())
        ttk.Button(actions, text="Open existing session" if existing else "Create session · entries disabled",
                   style="Accent.TButton", command=open_agent).pack(side="left")
    return window
