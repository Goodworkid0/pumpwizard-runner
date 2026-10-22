"""Strategy selection, simulation evidence, and exact local configuration import."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .catalog import PERIODS, add_strategy, load_catalog, package_for, period_performance, refresh_catalog, select_rows
from .strategies import parse_package
from .strategy_support import import_comparison, support_label, support_text


def money(value):
    return "—" if value is None else f"${value:,.2f}"


def stamp(value):
    return "—" if not value else datetime.fromtimestamp(value, timezone.utc).strftime("%d %b %Y %H:%M UTC")


class StrategyBrowser(ttk.Frame):
    def __init__(self, parent, library, data_dir: Path, on_added, on_open_agent, on_close):
        super().__init__(parent, style="Panel.TFrame")
        self.library = library
        self.cache = data_dir / "strategy-catalog.json"
        self.document = load_catalog(self.cache)
        self.on_added, self.on_open_agent = on_added, on_open_agent
        self.selected = None
        self.results = queue.SimpleQueue()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)
        header = ttk.Frame(self, style="Panel.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="Strategy marketplace", style="Hero.TLabel").pack(side="left")
        ttk.Button(header, text="Back to market", style="Subtle.TButton", command=on_close).pack(side="right")
        self.update_button = ttk.Button(header, text="Update results", style="Subtle.TButton", command=self.update_results)
        self.update_button.pack(side="right", padx=8)
        self.browse_button = ttk.Button(header, text="All strategies", style="Subtle.TButton", command=self.show_list)
        self.status = ttk.Label(self, style="Muted.TLabel", wraplength=730)
        self.status.grid(row=1, column=0, sticky="ew", pady=(6, 10))
        filters = ttk.Frame(self, style="Panel.TFrame")
        self.filters = filters
        filters.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        filters.columnconfigure(0, weight=1)
        ttk.Label(filters, text='Search strategies', style='Muted.TLabel').grid(row=0, column=0, sticky='w', pady=(0, 4))
        self.query = tk.StringVar()
        search = ttk.Entry(filters, textvariable=self.query, width=20)
        search.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.query.trace_add("write", lambda *_: self.render_rows())
        self.family = tk.StringVar(value="All families")
        self.version = tk.StringVar(value="All versions")
        self.outcome = tk.StringVar(value="All results")
        self.family_selector = self.selector(filters, self.family,
            ["All families"] + sorted({row["family"] for row in self.document["strategies"]}), 1)
        self.selector(filters, self.version, ["All versions", "Original", "V2", "V3"], 2).grid_configure(row=1)
        self.selector(filters, self.outcome, ["All results", "Simulated", "Profitable", "Untested"], 3).grid_configure(row=1)
        self.family_selector.grid_configure(row=1)
        period_bar = ttk.Frame(self, style="Panel.TFrame")
        period_bar.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(period_bar, text="PERFORMANCE", style="Section.TLabel").pack(side="left", padx=(0, 10))
        self.period = tk.StringVar(value="All time")
        self.period_selector = ttk.Combobox(period_bar, textvariable=self.period, values=list(PERIODS),
                                            state="readonly", width=18, style="Agent.TCombobox")
        self.period_selector.pack(side="left")
        self.period_selector.bind("<<ComboboxSelected>>", lambda _: self.render_rows())
        ttk.Label(period_bar, text="Highest P&L first", style="Muted.TLabel").pack(side="left", padx=12)

        panes = ttk.Panedwindow(self, orient="vertical")
        self.panes = panes
        panes.grid(row=4, column=0, sticky="nsew")
        listing = ttk.Frame(panes, style="Panel.TFrame")
        self.listing = listing
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        panes.add(listing, weight=1)
        self.tree = ttk.Treeview(listing, style="App.Treeview", show="headings", selectmode="browse", height=5,
                                 columns=("name", "pnl", "return", "closed", "state"))
        for key, title, width in (("name", "STRATEGY", 290), ("pnl", "SIM. P&L", 95),
                                  ("return", "RETURN", 75), ("closed", "CLOSED", 80), ("state", "LOCAL USE", 185)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=55, stretch=key == "name")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        horizontal = ttk.Scrollbar(listing, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(xscrollcommand=horizontal.set)
        self.tree.tag_configure("positive", foreground="#edf2ff")
        self.tree.tag_configure("untested", foreground="#98a7c5")
        self.tree.tag_configure('alternate', background='#162035')
        self.tree.bind("<<TreeviewSelect>>", self.select)
        self.tree.bind("<ButtonRelease-1>", self.open_clicked_row)
        self.tree.bind("<Return>", lambda _: self.show_detail())

        detail = ttk.Frame(panes, style="Panel.TFrame", padding=(0, 12, 0, 0))
        self.detail = detail
        detail.columnconfigure(0, weight=1)
        detail.rowconfigure(2, weight=1)
        self.title = ttk.Label(detail, style="Section.TLabel", wraplength=700)
        self.title.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.description = ttk.Label(detail, style="Muted.TLabel", wraplength=700)
        self.description.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        detail.bind("<Configure>", lambda e: (self.title.configure(wraplength=max(200, e.width-20)),
                                             self.description.configure(wraplength=max(200, e.width-20))))
        self.tabs = ttk.Notebook(detail, style="App.TNotebook")
        self.tabs.grid(row=2, column=0, sticky="nsew")
        performance = ttk.Frame(self.tabs, style="Panel.TFrame")
        config = ttk.Frame(self.tabs, style="Panel.TFrame")
        self.tabs.add(performance, text="Simulation performance")
        self.tabs.add(config, text="Configuration")
        performance.columnconfigure(0, weight=1)
        performance.rowconfigure(1, weight=1)
        cards = ttk.Frame(performance, style="Panel.TFrame")
        cards.grid(row=0, column=0, sticky="ew", pady=8)
        self.metrics = []
        self.metric_captions = []
        for index, caption in enumerate(("TOTAL SIMULATED P&L", "RETURN ON STARTING BALANCE", "ALL-TIME CLOSED WIN RATE")):
            cards.columnconfigure(index, weight=1, uniform="metrics")
            card = ttk.Frame(cards, style="Card.TFrame", padding=10)
            card.grid(row=0, column=index, sticky="ew", padx=(0, 6))
            caption_label = ttk.Label(card, text=caption, style="MetricCaption.TLabel", font=("TkDefaultFont", 8))
            caption_label.pack(anchor="w")
            self.metric_captions.append(caption_label)
            value = ttk.Label(card, style="Metric.TLabel")
            value.pack(anchor="w")
            self.metrics.append(value)
        self.evidence = self.textbox(performance, 1)
        config.columnconfigure(0, weight=1)
        config.rowconfigure(0, weight=1)
        self.config_text = self.textbox(config, 0, mono=True)
        support = ttk.Frame(self.tabs, style="Panel.TFrame")
        support.columnconfigure(0, weight=1)
        support.rowconfigure(0, weight=1)
        self.tabs.add(support, text="Compatibility & evidence")
        self.support_text = self.textbox(support, 0)
        self.tabs.select(support)
        actions = ttk.Frame(self, style="Panel.TFrame")
        actions.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        self.add_button = ttk.Button(actions, text="Add strategy", style="Accent.TButton", command=self.add)
        self.add_button.pack(side="right")
        ttk.Button(actions, text="Config", style="Subtle.TButton", command=self.show_config).pack(side="right", padx=8)
        ttk.Button(actions, text="Export config", style="Subtle.TButton", command=self.export).pack(side="right")
        ttk.Button(actions, text="Details", style="Subtle.TButton", command=self.show_detail).pack(side="right", padx=8)
        self.open_button = ttk.Button(actions, text="Open session", style="Subtle.TButton", command=self.open_agent)
        self.open_button.pack(side="left")
        self.note = ttk.Label(self, style="Muted.TLabel", wraplength=730)
        self.note.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        self.render_rows()

    def show_detail(self):
        if not self.selected:
            return
        for pane in self.panes.panes():
            self.panes.forget(pane)
        self.panes.add(self.detail, weight=1)
        self.filters.grid_remove()
        self.browse_button.pack(side="right", padx=8)

    def show_list(self):
        for pane in self.panes.panes():
            self.panes.forget(pane)
        self.panes.add(self.listing, weight=1)
        self.filters.grid()
        self.browse_button.pack_forget()

    def show_config(self):
        self.show_detail()
        self.tabs.select(1)

    def open_clicked_row(self, event):
        if self.tree.identify_row(event.y):
            self.select()
            self.show_detail()

    def selector(self, parent, variable, values, column):
        widget = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=14,
                              style="Agent.TCombobox")
        widget.grid(row=0, column=column, padx=(0, 6))
        widget.bind("<<ComboboxSelected>>", lambda _: self.render_rows())
        return widget

    def textbox(self, parent, row, mono=False):
        text = tk.Text(parent, background="#121a2d", foreground="#edf2ff", insertbackground="#edf2ff",
                       highlightbackground='#2b3852', highlightcolor='#7c6cff',
                       relief="flat", wrap="word", height=6, width=1, padx=10, pady=8,
                       font=("TkFixedFont" if mono else "TkDefaultFont", 10), state="disabled")
        text.grid(row=row, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(parent, orient="vertical", command=text.yview)
        scroll.grid(row=row, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        return text

    @staticmethod
    def write(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def render_rows(self):
        previous = self.selected["id"] if self.selected else None
        period = self.period.get()
        short = PERIODS[period][1]
        rows = select_rows(self.document, self.query.get(), self.family.get(), self.version.get(), self.outcome.get(), period)
        self.tree.heading("pnl", text='SIM. P&L' if short == 'TOTAL' else f'{short} P&L')
        self.tree.heading("return", text='ROI')
        self.metric_captions[0].configure(text=f"{short} SIMULATED P&L")
        self.metric_captions[1].configure(text=f"{short} RETURN / OPENING EQUITY")
        self.visible = {row["id"]: row for row in rows}
        saved = {row["digest"] for row in self.library.rows()}
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(rows):
            report = row.get("report")
            strategy = parse_package(package_for(row))
            state = support_label(strategy)
            if strategy.digest in saved:
                state += " · saved"
            pnl, percent = period_performance(row, period)
            roi = f"{percent:+.2f}%" if percent is not None else "—"
            tag = "untested" if pnl is None else "positive" if pnl > 0 else ""
            self.tree.insert("", "end", iid=row["id"], values=(row["label"], money(pnl), roi,
                             report["closed_count"] if report else "—", state), tags=(tag, 'alternate' if index % 2 else ''))
        available = sum(period_performance(row, period)[0] is not None for row in rows)
        self.status.configure(text=f"{len(rows):,} strategies · {period} at each snapshot · {available:,} with results · Missing data last")
        if rows:
            chosen = previous if previous in self.visible else rows[0]["id"]
            self.tree.selection_set(chosen)
            self.tree.see(chosen)
            self.select()
        else:
            self.selected = None
            self.title.configure(text="No strategies match these filters")
            self.description.configure(text="Change the search, family, version, or results filter.")
            for value in self.metrics:
                value.configure(text="—", foreground="#edf2ff")
            self.write(self.evidence, "")
            self.write(self.config_text, "")
            self.write(self.support_text, "")
            self.note.configure(text="")
            self.add_button.configure(state="disabled")
            self.open_button.configure(state="disabled")

    def select(self, _event=None):
        selection = self.tree.selection()
        if not selection or selection[0] not in self.visible:
            return
        self.selected = row = self.visible[selection[0]]
        strategy = parse_package(package_for(row))
        report, cfg = row.get("report"), row["config"]
        self.title.configure(text=row["label"])
        self.description.configure(text=support_label(strategy) + " · " + row["description"])
        self.write(self.support_text, support_text(strategy) + "\n\n" + import_comparison(self.library, strategy))
        values = ["—", "—", "—"]
        pnl, percent = period_performance(row, self.period.get())
        if report:
            values[:2] = [money(pnl), f"{percent:+.2f}%" if percent is not None else "—"]
            if report.get("closed_win_rate") is not None and report["closed_count"]:
                values[2] = f'{report["closed_win_rate"]*100:.1f}%  / {report["closed_count"]} closed'
        for index, (widget, value) in enumerate(zip(self.metrics, values)):
            color = "#41d19d" if pnl is not None and pnl > 0 and index < 2 else "#edf2ff"
            widget.configure(text=value, foreground=color)
        evidence = [f"Entry cap: {money(cfg.get('entry_cap_min'))}–{money(cfg.get('entry_cap_max'))}   •   Observation: {cfg.get('observation_seconds', 0)/60:g} min",
                    f"Order size: {money(cfg.get('order_usd'))}   •   Max positions: {cfg.get('max_positions', '—')}   •   Hold limit: {cfg.get('max_hold_seconds', 0)/86400:g} days"]
        if report:
            evidence += ["", f"Selected window: {self.period.get()} ending at the source snapshot. Return uses opening equity."]
            if pnl is None:
                evidence.append("This snapshot does not include results for this window. Update results when the source publishes them.")
            age = max(0, time.time() - row["source_as_of"])
            freshness = "SOURCE REPORTED STALE" if row.get("source_stale") else "historical snapshot"
            evidence += ["", f"Public results as of {stamp(row['source_as_of'])} · {age/3600:.1f}h old · {freshness}",
                f"Simulation period: {stamp(report.get('started_at'))} → {stamp(report.get('last_at'))}",
                f"Starting balance {money(report['initial_usd'])} → equity {money(report['equity_usd'])}",
                f"Realized net {money(report.get('realized_net_pnl_usd'))}   •   Unrealized {money(report.get('unrealized_pnl_usd'))}",
                f"All-time entries {report['total_entries']}   •   All-time closed {report['closed_count']}   •   Open {report.get('open_position_count', '—')}",
                f"24h P&L {money(report.get('pnl_24h_usd'))}   •   Modeled fees {money(report.get('fees_usd'))}"]
            if report.get("max_drawdown") is not None:
                evidence.append(f"Maximum observed equity drawdown: {report['max_drawdown']*100:.1f}%")
            evidence += ["", "Total P&L includes open-position marks and modeled costs. Missing sell quotes may mark holdings at $0. These are simulation results, not expected live returns."]
        else:
            evidence += ["", "UNTESTED · Staged research definition. No simulation results were published for this variant. Profitability and win rate are unknown."]
        if row.get("trial"):
            evidence += ["", f"{row['trial'].get('label', 'Trial')} · Parent: {row['trial'].get('parent_id', '—')}",
                         "Trial metadata is included in Configuration and its export. Local trial execution is not implemented."]
        evidence += ["", "Local monitoring uses independent observations. Reviewed trading is limited to the pinned core-frequency configuration; other configurations are not certified for execution.",
                     "Source: pumpwizard.fun · Bundled/cached locally. Update results is optional; local monitoring does not contact PumpWizard."]
        self.write(self.evidence, "\n".join(evidence))
        self.write(self.config_text, json.dumps(package_for(row), indent=2, ensure_ascii=False))
        self.add_button.configure(state="normal", text="Add strategy")
        self.open_button.configure(state="normal" if self.existing_agent() else "disabled")
        self.note.configure(text=("Adds a monitoring session with entries disabled. Open Agent trading to check reviewed-trading support and setup."
                            if strategy.compatibility == "monitoring-ready" else strategy.reason))

    def existing_agent(self):
        if not self.selected:
            return None
        digest = parse_package(package_for(self.selected)).digest
        return next((row["id"] for row in self.library.instances() if row["strategy_digest"] == digest), None)

    def add(self):
        if not self.selected:
            return
        try:
            agent_id, created = add_strategy(self.library, self.selected)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Cannot add strategy", str(exc), parent=self)
            return
        self.on_added()
        self.render_rows()
        self.note.configure(text=("Local session added. Open session to start independent market tracking. Entries remain disabled."
                                  if agent_id and created else "Already added. Open session to view it."
                                  if agent_id else "Configuration saved in your local library, including trial metadata. Local engine support is still required."))

    def open_agent(self):
        if ident := self.existing_agent():
            self.on_open_agent(ident)

    def export(self):
        if not self.selected:
            return
        path = filedialog.asksaveasfilename(parent=self, title="Export exact strategy config", defaultextension=".json",
                                            initialfile=self.selected["id"] + ".json", filetypes=[("JSON", "*.json")])
        if path:
            try:
                Path(path).write_text(json.dumps(package_for(self.selected), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("Cannot export config", str(exc), parent=self)

    def update_results(self):
        self.update_button.configure(state="disabled")
        self.status.configure(text="Reading public simulation results… local agents keep their existing configurations.")
        current = self.document

        def work():
            try:
                self.results.put((refresh_catalog(current, self.cache), None))
            except Exception:
                self.results.put((None, "Results unavailable. Retaining your last snapshot and all local configurations."))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self.await_results)

    def await_results(self):
        try:
            document, error = self.results.get_nowait()
        except queue.Empty:
            self.after(100, self.await_results)
            return
        self.update_button.configure(state="normal")
        if error:
            self.status.configure(text=error)
        else:
            self.document = document
            self.family_selector.configure(values=["All families"] + sorted({row["family"] for row in document["strategies"]}))
            self.render_rows()
