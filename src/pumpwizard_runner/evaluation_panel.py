"""Guided offline replay followed by explicit navigation to unsigned assessment."""

import json
import queue
import sqlite3
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from . import evaluation
from .strategies import parse_package
from .strategy_browser import StrategyBrowser
from .strategy_support import reviewed_eligible, support_text


class EvaluationPanel:
    def __init__(self, root, data_dir, instance, open_shadow):
        self.data_dir, self.instance = data_dir, instance
        self.strategy = parse_package(instance["package"])
        self.results = queue.SimpleQueue()
        self.cancelled = threading.Event()
        self.busy, self.closed, self.selected = False, False, None
        self.report_rows = {}
        self.window = tk.Toplevel(root)
        self.window.title("Local evaluation · " + instance["label"])
        self.window.geometry("1080x800")
        self.window.minsize(820, 600)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(self.window, style="Panel.TFrame", padding=18)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(3, weight=1)
        ttk.Label(body, text=instance["label"] + " · local evaluation", style="Hero.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(body, style="Muted.TLabel", wraplength=1000,
                  text="1  Inspect configuration     →     2  Replay locally     →     3  Assess unsigned\n"
                  "Replay uses modeled funds. Shadow uses current observations and actual wallet readiness. "
                  "Neither authorizes execution.").grid(row=1, column=0, sticky="w", pady=10)
        actions = ttk.Frame(body, style="Panel.TFrame")
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.load_button = ttk.Button(actions, text="Run recording…", style="Accent.TButton", command=self.load)
        self.load_button.pack(side="left")
        self.example_button = ttk.Button(actions, text="Run synthetic example", style="Subtle.TButton",
                                         command=lambda: self.start(evaluation.example_recording))
        self.example_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(actions, text="Cancel replay", style="Subtle.TButton",
                                        command=self.cancelled.set, state="disabled")
        self.cancel_button.pack(side="left")
        self.tabs = ttk.Notebook(body, style="App.TNotebook")
        self.tabs.grid(row=3, column=0, sticky="nsew")
        overview = self.tab("Readiness & next steps")
        self.overview = StrategyBrowser.textbox(self, overview, 0)
        StrategyBrowser.write(self.overview, support_text(self.strategy) + "\n\n"
            "Replay accepts the existing pumpwizard.portfolio-recording/v1 format, up to 16 MiB / "
            "20,000 batches. Every run starts at the configured virtual budget. Repeating identical "
            "inputs reuses the saved report. The synthetic example teaches the workflow; its outcome "
            "is not market performance. Export includes the full configuration and input recording.")
        saved = self.tab("Replay reports")
        saved.rowconfigure(0, weight=0)
        saved.rowconfigure(1, weight=1)
        self.history = ttk.Combobox(saved, state="readonly", style="Agent.TCombobox")
        self.history.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.history.bind("<<ComboboxSelected>>", self.show_report)
        self.detail = StrategyBrowser.textbox(self, saved, 1)
        events = self.tab("Events & inputs")
        self.events = StrategyBrowser.textbox(self, events, 0, mono=True)
        footer = ttk.Frame(body, style="Panel.TFrame")
        footer.grid(row=4, column=0, sticky="ew", pady=10)
        self.export_button = ttk.Button(footer, text="Export selected report…", style="Subtle.TButton",
                                        command=self.export, state="disabled")
        self.export_button.pack(side="left")
        self.shadow_button = ttk.Button(footer, text="Open unsigned assessment", style="Subtle.TButton",
                                        command=lambda: open_shadow(instance["id"], 2))
        self.shadow_button.pack(side="right")
        self.worker_button = ttk.Button(footer, text="Open worker rehearsal", style="Subtle.TButton",
                                        command=lambda: open_shadow(instance["id"], 7))
        self.worker_button.pack(side="right", padx=8)
        self.status = ttk.Label(body, style="Muted.TLabel", wraplength=1000)
        self.status.grid(row=5, column=0, sticky="w")
        eligible = reviewed_eligible(self.strategy)
        if not eligible:
            for button in (self.load_button, self.example_button, self.shadow_button, self.worker_button):
                button.configure(state="disabled")
        self.status.configure(text="Choose a local recording or the synthetic example." if eligible else
                              "Research only: replay and shadow support are unavailable for this configuration.")
        self.refresh()
        self.timer = self.window.after(100, self.poll)

    def tab(self, label):
        frame = ttk.Frame(self.tabs, style="Panel.TFrame", padding=(0, 10, 0, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tabs.add(frame, text=label)
        return frame

    def load(self):
        path = filedialog.askopenfilename(parent=self.window, title="Choose an offline portfolio recording",
                                          filetypes=[("JSON recording", "*.json")])
        if path:
            self.start(lambda: evaluation.read_recording(path))

    def start(self, loader):
        if self.busy or self.closed or not reviewed_eligible(self.strategy):
            return
        self.busy = True
        self.cancelled.clear()
        self.load_button.configure(state="disabled")
        self.example_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status.configure(text="Evaluating offline… No wallet, RPC or signing access.")

        def work():
            try:
                report = evaluation.evaluate(self.data_dir, self.strategy.package, loader(),
                                             cancelled=self.cancelled.is_set)
                self.results.put((report, None))
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, OverflowError) as exc:
                self.results.put((None, str(exc)))

        threading.Thread(target=work, name="local-evaluation", daemon=True).start()

    def poll(self):
        if self.closed:
            return
        try:
            report, error = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.load_button.configure(state="normal")
            self.example_button.configure(state="normal")
            self.cancel_button.configure(state="disabled")
            if error:
                self.status.configure(text="Replay not completed: " + error)
            else:
                self.refresh(report["id"])
                self.tabs.select(1)
                self.status.configure(text="Completed modeled replay. Report saved locally; no execution authorized.")
        self.timer = self.window.after(100, self.poll)

    def refresh(self, selected_id=None):
        try:
            rows = evaluation.reports(self.data_dir, self.strategy.digest)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.status.configure(text="Cannot read saved reports: " + str(exc))
            return
        self.report_rows = {f"{row['id'][:12]} · {row['period']['batches']} batches · "
                            f"modeled equity ${row['summary']['final_equity_usd']:.2f}": row for row in rows}
        self.history.configure(values=tuple(self.report_rows))
        self.history.set(next((label for label, row in self.report_rows.items() if row["id"] == selected_id),
                              next(iter(self.report_rows), "")))
        self.show_report()

    def show_report(self, _event=None):
        self.selected = self.report_rows.get(self.history.get())
        self.export_button.configure(state="normal" if self.selected else "disabled")
        if self.selected:
            StrategyBrowser.write(self.detail, evaluation.report_text(self.selected))
            StrategyBrowser.write(self.events, json.dumps(dict(events=self.selected["events"],
                recording=self.selected["recording"]), indent=2, ensure_ascii=False))
        else:
            StrategyBrowser.write(self.detail, "No completed replay for this exact package. Run a recording to begin.")
            StrategyBrowser.write(self.events, "")

    def export(self):
        if self.selected is None:
            return
        path = filedialog.asksaveasfilename(parent=self.window, title="Export local evaluation including inputs",
                                            defaultextension=".json", initialfile="evaluation-" + self.selected["id"][:12] + ".json")
        if path:
            try:
                from pathlib import Path

                Path(path).write_text(json.dumps(self.selected, indent=2, ensure_ascii=False), encoding="utf-8")
                self.status.configure(text="Exported modeled report with full configuration and recording; inspect before sharing.")
            except OSError as exc:
                self.status.configure(text="Export failed: " + str(exc))

    def close(self):
        self.closed = True
        self.cancelled.set()
        self.window.after_cancel(self.timer)
        self.window.destroy()
