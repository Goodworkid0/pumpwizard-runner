"""Guided desktop trading: setup, unsigned assessment, explicit review and recovery."""

import json
import os
import sqlite3
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

from . import trading_workspace as workspace
from .manual_trades import PREVIEW_TTL
from .pump_route import MAX_ACCOUNT_FUNDING
from .route_check import RouteCheckRpc
from .trade_panel import rpc_for, token_text
from .transfers import TransferError, sol_text
from .wallet_vault import WalletVault
from .wallet_risk import FIELDS
from .transfers import parse_sol
from .signing_sessions import SessionManager
from .execution_worker import ExecutionWorker, WorkerConfig, history as worker_history
from .strategy_browser import StrategyBrowser
from .strategies import parse_package
from .strategy_support import support_text


def review_text(p):
    ctx = p["agent_order"]
    sell = p["side"] == "sell"
    amount = token_text(p["amount"]) + " tokens" if sell else sol_text(p["amount"]) + " SOL"
    minimum = (
        sol_text(p["minimum_output"]) + " SOL"
        if sell
        else token_text(p["minimum_output"]) + " tokens"
    )
    bound = (
        f"Conservative net floor: {sol_text(p['minimum_output'] - p['fee'] - MAX_ACCOUNT_FUNDING)} SOL"
        if sell
        else f"Maximum wallet debit: {sol_text(p['max_wallet_debit'])} SOL"
    )
    return (
        f"{p['network']} · {'REAL SOL' if p['network'] == 'mainnet-beta' else 'TEST SOL'}\n"
        f"Wallet: {p['sender']}\nToken: {p['mint']}\n\n"
        f"{p['side'].upper()} {amount}\nReason: {ctx['reason'].replace('_', ' ')}\n"
        f"Venue: {'PumpSwap' if p['route'].startswith('pumpswap') else 'Pump curve'}\n"
        f"Minimum route output: {minimum}\nNetwork fee: {sol_text(p['fee'])} SOL\n"
        f"Account funding allowance: {sol_text(MAX_ACCOUNT_FUNDING)} SOL\n"
        f"Estimated wallet change: {sol_text(p['expected_wallet_delta'])} SOL\n{bound}\n"
        f"Slippage: {p['slippage_bps'] / 100:g}%\n\n"
        "This authorizes one trade. The app rechecks the quote before signing."
    )


class TradingPanel:
    def __init__(self, root, data_dir, address, agent_id, refresh, open_wallet, manual_exit):
        self.data_dir, self.address, self.agent_id = data_dir, address, agent_id
        self.refresh_app, self.open_wallet, self.manual_exit = refresh, open_wallet, manual_exit
        self.busy, self.closed, self.result = False, False, None
        self.timer, self.poll_timer = None, None
        self.clock_timer = None
        self.data = None
        self.risk_network = None
        self.sessions = SessionManager()
        self.session_id = None
        self.session_rows = {}
        self.session_cancellation = None
        self.pending_revocation = None
        self.execution_worker = None
        self.worker_history = dict(run=None, cycles=[])
        self.worker_close_timer = None
        (
            self.candidate_rows,
            self.order_rows,
            self.position_rows,
            self.hold_rows,
            self.report_rows,
        ) = {}, {}, {}, {}, {}
        self.cash_choices = {}
        self.window = tk.Toplevel(root)
        self.window.title("Agent trading · SolanaMarket Wizard")
        self.window.geometry("1120x860")
        self.window.minsize(980, 700)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(self.window, style="Panel.TFrame", padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)
        header = ttk.Frame(body, style="Panel.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        self.title = ttk.Label(header, text="Agent trading", style="Hero.TLabel")
        self.title.pack(side="left")
        ttk.Label(header, text="APPROVAL FOR EACH TRADE", style="PanelAccent.TLabel").pack(
            side="right"
        )
        self.summary = ttk.Label(
            body, text="Loading setup and local history…", style="Muted.TLabel", wraplength=1030
        )
        self.summary.grid(row=1, column=0, sticky="w", pady=(6, 12))
        form = ttk.Frame(body, style="Card.TFrame", padding=12)
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(3, weight=1)
        prefs = workspace.preferences(data_dir, agent_id, address)
        self.network = tk.StringVar(value=prefs["network"])
        self.allocation = tk.StringVar(value=prefs["allocation"])
        self.slippage = tk.StringVar(value=str(prefs["slippage_bps"]))
        self.cash = tk.StringVar()
        self.controls = []
        for column, (label, variable, width) in enumerate(
            (
                ("Network", self.network, 15),
                ("Allocation (SOL)", self.allocation, 16),
                ("Slippage (bps)", self.slippage, 14),
            )
        ):
            ttk.Label(form, text=label, style="MetricCaption.TLabel").grid(
                row=0, column=column, sticky="w", padx=(0, 16)
            )
            widget = (
                ttk.Combobox(
                    form,
                    textvariable=variable,
                    values=("mainnet-beta", "devnet"),
                    state="readonly",
                    width=width,
                    style="Agent.TCombobox",
                )
                if column == 0
                else ttk.Entry(form, textvariable=variable, width=width)
            )
            widget.grid(row=1, column=column, sticky="ew", padx=(0, 16), pady=(4, 0))
            self.controls.append(widget)
        self.controls[0].bind("<<ComboboxSelected>>", lambda _: self.change_network())
        ttk.Label(form, text="Active cash allocation", style="MetricCaption.TLabel").grid(
            row=0, column=3, sticky="w"
        )
        self.cash_choice = ttk.Combobox(
            form, textvariable=self.cash, state="readonly", style="Agent.TCombobox"
        )
        self.cash_choice.grid(row=1, column=3, sticky="ew", pady=(4, 0))
        self.controls.append(self.cash_choice)
        toolbar = ttk.Frame(body, style="Panel.TFrame")
        toolbar.grid(row=3, column=0, sticky="ew", pady=10)
        self.button(toolbar, "Save setup", self.save_setup)
        self.button(toolbar, "Refresh & recover", self.refresh_history)
        self.button(toolbar, "Copy deposit address", self.copy_address)
        self.button(toolbar, "Wallet & backup", open_wallet)
        ttk.Style().configure("Trading.App.TNotebook.Tab", padding=(6, 10))
        self.tabs = ttk.Notebook(body, style="Trading.App.TNotebook")
        self.tabs.grid(row=4, column=0, sticky="nsew")
        setup = self.tab("Setup")
        self.checks = self.table(
            setup,
            [
                ("check", "CHECK", 155),
                ("state", "STATE", 150),
                ("detail", "NEXT STEP / DETAILS", 670),
            ],
            row=0,
            height=7,
        )
        setup_actions = ttk.Frame(setup, style="Panel.TFrame")
        setup_actions.grid(row=1, column=0, sticky="ew", pady=8)
        self.enable_button = self.button(setup_actions, "Enable reviewed entries", self.enable)
        self.track_button = self.button(setup_actions, "Start tracking", self.toggle_tracking)
        self.allocate_button = self.button(setup_actions, "Allocate SOL", self.allocate)
        self.button(setup_actions, "Release selected allocation", self.release)
        self.holds = self.table(
            setup,
            [
                ("kind", "ALLOCATION", 120),
                ("amount", "AMOUNT", 170),
                ("position", "POSITION", 270),
                ("id", "ID", 350),
            ],
            row=2,
            height=3,
        )
        setup.rowconfigure(0, weight=2)
        setup.rowconfigure(2, weight=1)
        ttk.Label(
            setup,
            text="Save setup before depositing. Allocate SOL after funding; this reserves funds locally. Slippage: 100 bps = 1%.",
            style="Muted.TLabel",
            wraplength=1000,
        ).grid(row=3, column=0, sticky="w", pady=(8, 0))
        limits_tab = self.tab("Wallet limits")
        risk_canvas = tk.Canvas(
            limits_tab, highlightthickness=0,
            background=ttk.Style().lookup("Panel.TFrame", "background") or "#111a2e",
        )
        risk_canvas.grid(row=0, column=0, sticky="nsew")
        risk_scroll = ttk.Scrollbar(limits_tab, orient="vertical", command=risk_canvas.yview)
        risk_scroll.grid(row=0, column=1, sticky="ns")
        risk_canvas.configure(yscrollcommand=risk_scroll.set)
        limits = ttk.Frame(risk_canvas, style="Panel.TFrame")
        limits.columnconfigure(0, weight=1)
        risk_content = risk_canvas.create_window((0, 0), window=limits, anchor="nw")
        limits.bind("<Configure>", lambda _event: risk_canvas.configure(scrollregion=risk_canvas.bbox("all")))
        risk_canvas.bind("<Configure>", lambda event: risk_canvas.itemconfigure(risk_content, width=event.width))
        self.risk_summary = ttk.Label(
            limits, text="Loading wallet limits…", style="Muted.TLabel", wraplength=850
        )
        self.risk_summary.grid(row=0, column=0, sticky="w", pady=(0, 14))
        limits_form = ttk.Frame(limits, style="Panel.TFrame")
        limits_form.grid(row=1, column=0, sticky="nw")
        self.risk_fields = {}
        for row, (name, label) in enumerate(FIELDS.items()):
            ttk.Label(limits_form, text=label + " (SOL)", style="Muted.TLabel").grid(
                row=row, column=0, sticky="w", pady=4
            )
            variable = tk.StringVar()
            self.risk_fields[name] = variable
            entry = ttk.Entry(limits_form, textvariable=variable, width=22)
            entry.grid(row=row, column=1, padx=20, pady=4)
            self.controls.append(entry)
        self.risk_paused = tk.BooleanVar(value=False)
        pause = ttk.Checkbutton(
            limits_form,
            text="Pause new buys across this wallet; keep exits available",
            variable=self.risk_paused,
            style="Panel.TCheckbutton",
        )
        pause.grid(row=len(FIELDS), column=0, columnspan=2, sticky="w", pady=12)
        self.controls.append(pause)
        actions = ttk.Frame(limits, style="Panel.TFrame")
        actions.grid(row=2, column=0, sticky="w", pady=12)
        self.button(actions, "Review & save wallet limits", self.save_risk_limits, accent=True)
        self.button(actions, "Reload saved limits", self.reload_risk_limits)
        self.button(actions, "Refresh portfolio value", self.refresh_portfolio_value)
        ttk.Label(
            limits,
            style="Muted.TLabel",
            wraplength=1000,
            text="Limits apply to every agent and manual buy on the selected network. Buy cost includes fees and account funding. "
            "Open cost includes remaining position cost and committed buys. Daily loss counts losing fills and failed-trade fees, "
            "without offsetting profits, and resets at midnight UTC. Drawdown measures cumulative realized trade P/L from its high. "
            "Portfolio drawdown includes open positions at conservative simulated sell values. Missing or stale valuation blocks new buys. "
            "External positions and SOL/USD changes are outside these metrics. Saving limits does not authorize trading.",
        ).grid(row=3, column=0, sticky="nw", pady=12)
        candidates = self.tab("Candidates")
        self.candidates = self.table(
            candidates,
            [
                ("side", "ACTION", 80),
                ("mint", "TOKEN", 360),
                ("amount", "QUANTITY", 160),
                ("fresh", "QUOTE AGE", 125),
                ("reason", "SIGNAL", 250),
            ],
        )
        actions = ttk.Frame(candidates, style="Panel.TFrame")
        actions.grid(row=1, column=0, sticky="w", pady=10)
        self.button(actions, "Assess unsigned", self.assess)
        self.button(actions, "Prepare order review", self.prepare, accent=True)
        ttk.Label(
            candidates,
            text="Candidates are signals, not orders. Assessments never sign. Each candidate must pass a fresh quote and balance check.",
            style="Muted.TLabel",
            wraplength=1000,
        ).grid(row=2, column=0, sticky="w")
        orders = self.tab("Orders")
        self.orders = self.table(
            orders,
            [
                ("side", "SIDE", 70),
                ("mint", "TOKEN", 330),
                ("state", "STATUS", 155),
                ("amount", "AMOUNT", 150),
                ("reason", "REASON", 250),
            ],
        )
        self.orders.bind("<<TreeviewSelect>>", self.show_order)
        actions = ttk.Frame(orders, style="Panel.TFrame")
        actions.grid(row=1, column=0, sticky="w", pady=10)
        self.authorize_button = self.button(
            actions, "Review & authorize", self.authorize, accent=True
        )
        self.cancel_button = self.button(actions, "Cancel unsigned review", self.cancel)
        self.button(actions, "Copy signature", self.copy_signature)
        self.order_detail = ttk.Label(
            orders,
            text="Select a review or submitted order. Expired reviews must be cancelled and prepared again.",
            style="Muted.TLabel",
            wraplength=1000,
        )
        self.order_detail.grid(row=2, column=0, sticky="w")
        positions = self.tab("Positions")
        self.positions = self.table(
            positions,
            [
                ("mint", "TOKEN", 350),
                ("quantity", "REMAINING TOKENS", 170),
                ("cost", "COST (SOL)", 130),
                ("realized", "REALIZED P/L (SOL)", 160),
                ("state", "STATUS", 100),
            ],
        )
        actions = ttk.Frame(positions, style="Panel.TFrame")
        actions.grid(row=1, column=0, sticky="w", pady=10)
        self.button(actions, "Assess exit", self.assess_position)
        self.button(actions, "Manual exit…", self.exit_manually)
        ttk.Label(
            positions,
            text="Positions and realized P/L come from finalized receipts. Pause entries keeps held-position monitoring active.",
            style="Muted.TLabel",
            wraplength=1000,
        ).grid(row=2, column=0, sticky="w")
        reports = self.tab("Shadow reports")
        self.reports = self.table(
            reports,
            [
                ("time", "CHECKED", 170),
                ("mint", "TOKEN", 330),
                ("status", "RESULT", 110),
                ("reason", "DETAIL", 340),
            ],
        )
        self.reports.bind("<<TreeviewSelect>>", self.show_report)
        actions = ttk.Frame(reports, style="Panel.TFrame")
        actions.grid(row=1, column=0, sticky="w", pady=8)
        self.button(actions, "Export selected report", self.export_report)
        self.report_detail = ttk.Label(
            reports,
            text="Unsigned reports will appear here. A ready result is not authorization to trade.",
            style="Muted.TLabel",
            wraplength=1000,
        )
        self.report_detail.grid(row=2, column=0, sticky="w")
        session = self.tab("Signing session")
        self.session_summary = ttk.Label(
            session, text="No authorized session", style="Muted.TLabel", wraplength=1000
        )
        self.session_summary.grid(row=0, column=0, sticky="nw", pady=12)
        session_form = ttk.Frame(session, style="Panel.TFrame")
        session_form.grid(row=1, column=0, sticky="nw")
        self.session_fields = {}
        for row, (name, label) in enumerate(
            (
                ("budget", "Total spending cap (SOL)"),
                ("minutes", "Duration (1–60 minutes)"),
                ("orders", "Maximum orders (1–100)"),
                ("slippage", "Maximum slippage (bps)"),
            )
        ):
            ttk.Label(session_form, text=label, style="Muted.TLabel").grid(
                row=row, column=0, sticky="w", pady=8
            )
            variable = tk.StringVar()
            self.session_fields[name] = variable
            entry = ttk.Entry(session_form, textvariable=variable, width=22)
            entry.grid(row=row, column=1, padx=20, pady=8)
            self.controls.append(entry)
        self.use_session = tk.BooleanVar(value=False)
        use = ttk.Checkbutton(
            session_form,
            text="Use this session for individually reviewed agent orders",
            variable=self.use_session,
            style="Panel.TCheckbutton",
        )
        use.grid(row=4, column=0, columnspan=2, sticky="w", pady=12)
        self.controls.append(use)
        actions = ttk.Frame(session, style="Panel.TFrame")
        actions.grid(row=2, column=0, sticky="w", pady=12)
        self.button(actions, "Review & authorize session", self.authorize_session, accent=True)
        # Revocation stays available while an order is being revalidated.
        self.revoke_button = ttk.Button(
            actions, text="Revoke session", command=self.revoke_session, style="Danger.TButton"
        )
        self.revoke_button.pack(side="left", padx=(0, 8))
        ttk.Label(
            session,
            style="Muted.TLabel",
            wraplength=1000,
            text="One wallet, network, agent and selected cash allocation. Every order still needs review and a typed confirmation. "
            "The session replaces repeated password prompts; it does not run an automatic trader. "
            "Each submitted order permanently consumes its maximum debit allowance, including fees and account funding. "
            "Proceeds never refill this cap. Closing this workspace, changing network or restarting locks the session. "
            "Uncheck the session option to authorize an exit separately if the session expires or runs out of budget. "
            "Revocation stops future signatures; already signed/submitted transactions must still be recovered.",
        ).grid(row=3, column=0, sticky="nw", pady=12)
        worker_tab = self.tab("Worker rehearsal")
        self.worker_summary = ttk.Label(
            worker_tab, text="Worker stopped · unsigned only", style="Muted.TLabel", wraplength=1000
        )
        self.worker_summary.grid(row=0, column=0, sticky="w", pady=10)
        worker_form = ttk.Frame(worker_tab, style="Panel.TFrame")
        worker_form.grid(row=1, column=0, sticky="w", pady=10)
        self.worker_interval = tk.StringVar(value="15")
        self.worker_limit = tk.StringVar(value="3")
        for col, (label, variable) in enumerate(
            (
                ("Interval (seconds)", self.worker_interval),
                ("Candidates per cycle", self.worker_limit),
            )
        ):
            ttk.Label(worker_form, text=label, style="Muted.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0, 20)
            )
            entry = ttk.Entry(worker_form, textvariable=variable, width=20)
            entry.grid(row=1, column=col, sticky="w", padx=(0, 20), pady=5)
            self.controls.append(entry)
        worker_actions = ttk.Frame(worker_tab, style="Panel.TFrame")
        worker_actions.grid(row=2, column=0, sticky="w", pady=10)
        self.start_worker_button = self.button(
            worker_actions, "Start unsigned worker", self.start_worker, accent=True
        )
        ttk.Button(
            worker_actions, text="Stop worker", command=self.stop_worker, style="Danger.TButton"
        ).pack(side="left", padx=8)
        self.button(worker_actions, "Export latest cycle", self.export_worker)
        self.worker_results = self.table(
            worker_tab,
            [
                ("side", "ACTION", 75),
                ("mint", "TOKEN", 320),
                ("status", "RESULT", 110),
                ("reason", "DETAIL", 500),
            ],
            row=3,
            height=7,
        )
        worker_tab.rowconfigure(0, weight=0)
        worker_tab.rowconfigure(3, weight=1)
        ttk.Label(
            worker_tab,
            style="Muted.TLabel",
            wraplength=1000,
            text="Runs while this workspace is open. Start market tracking for fresh signals. Managed exits are assessed first, "
            "including while entries are paused. Existing submitted signatures are recovered; no new orders, allocations or signatures are created. "
            "A READY result is historical simulation evidence: prepare and approve a fresh order separately. "
            "Closing this workspace or changing networks stops the worker. Restart is always manual.",
        ).grid(row=4, column=0, sticky="w", pady=10)
        operations = self.tab("State & recovery")
        self.operations = StrategyBrowser.textbox(self, operations, 0)
        self.status = ttk.Label(
            body, text="Checking readiness…", style="Muted.TLabel", wraplength=1030
        )
        self.status.grid(row=5, column=0, sticky="w", pady=(12, 0))
        self.refresh_history()
        self.tick()

    def tick(self):
        if self.closed:
            return
        if not self.busy:
            self.show_order()
            if self.pending_revocation:
                self.flush_session_revocation()
        self.render_session()
        self.render_worker()
        self.clock_timer = self.window.after(1000, self.tick)

    def tab(self, title):
        frame = ttk.Frame(self.tabs, style="Panel.TFrame", padding=(0, 12, 0, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tabs.add(frame, text=title)
        return frame

    def table(self, parent, columns, row=0, height=8):
        frame = ttk.Frame(parent, style="Panel.TFrame")
        frame.grid(row=row, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            frame,
            columns=[c[0] for c in columns],
            show="headings",
            selectmode="browse",
            style="App.Treeview",
            height=height,
        )
        for name, label, width in columns:
            tree.heading(name, text=label)
            tree.column(name, width=width, minwidth=60)
        tree.grid(row=0, column=0, sticky="nsew")
        for orient, target, grid in [
            ("vertical", tree.yview, dict(row=0, column=1, sticky="ns")),
            ("horizontal", tree.xview, dict(row=1, column=0, sticky="ew")),
        ]:
            bar = ttk.Scrollbar(frame, orient=orient, command=target)
            bar.grid(**grid)
            tree.configure(
                **{"yscrollcommand" if orient == "vertical" else "xscrollcommand": bar.set}
            )
        return tree

    def button(self, parent, label, action, accent=False):
        button = ttk.Button(
            parent,
            text=label,
            command=action,
            style="Accent.TButton" if accent else "Subtle.TButton",
        )
        button.pack(side="left", padx=(0, 8))
        self.controls.append(button)
        return button

    def close(self):
        if self.closed:
            return
        self.sessions.lock()
        if self.session_cancellation:
            self.session_cancellation.set()
        if self.execution_worker:
            self.execution_worker.stop()
            if self.execution_worker.running:
                self.status.configure(
                    text="Stopping unsigned worker; waiting for the current public RPC read to return…"
                )
                if self.worker_close_timer is None:

                    def retry():
                        self.worker_close_timer = None
                        self.close()

                    self.worker_close_timer = self.window.after(100, retry)
                return
        if self.busy:
            self.status.configure(text="Wait for the current operation to finish.")
            return
        self.closed = True
        for timer in (self.timer, self.poll_timer, self.clock_timer):
            if timer:
                self.window.after_cancel(timer)
        self.window.destroy()

    def _run(self, work, done):
        if self.busy or self.closed:
            return
        self.busy, self.result = True, None
        for control in self.controls:
            control.configure(state="disabled")

        def worker():
            try:
                self.result = (True, work())
            except Exception as exc:
                self.result = (False, str(exc)[:350] or "Operation failed; retry the check.")

        thread = threading.Thread(target=worker, daemon=False, name="reviewed-agent-operation")
        thread.start()

        def poll():
            if self.closed:
                return
            if thread.is_alive():
                self.poll_timer = self.window.after(100, poll)
                return
            self.poll_timer = None
            self.busy = False
            for control in self.controls:
                control.configure(
                    state="readonly" if isinstance(control, ttk.Combobox) else "normal"
                )
            success, result = self.result
            self.result = None
            if success:
                done(result)
            else:
                self.status.configure(text=result)

        self.poll_timer = self.window.after(100, poll)

    def _inputs(self):
        slippage = int(self.slippage.get())
        if not 1 <= slippage <= 500:
            raise ValueError("Use slippage from 1 to 500 bps (0.01–5%)")
        return slippage

    def _cash(self):
        ident = self.cash_choices.get(self.cash.get())
        if not ident:
            raise TransferError("Allocate SOL, then select an available cash allocation.")
        return ident

    def _selected(self, tree, rows):
        selection = tree.selection()
        if not selection or selection[0] not in rows:
            raise TransferError("Select a row first.")
        return rows[selection[0]]

    def change_network(self):
        if self.busy:
            return
        self.sessions.lock()
        if self.execution_worker:
            self.execution_worker.stop()
        self.session_id = None
        self.use_session.set(False)
        self.cash.set("")
        self.cash_choices = {}
        self.risk_network = None
        for variable in self.risk_fields.values():
            variable.set("")
        self.risk_paused.set(False)
        self.refresh_history()

    def start_worker(self):
        if self.busy or (self.execution_worker and self.execution_worker.running):
            return
        try:
            network = self.network.get()
            config = WorkerConfig(
                self.agent_id,
                self.address,
                network,
                cash_hold=self.cash_choices.get(self.cash.get()),
                interval=int(self.worker_interval.get()),
                limit=int(self.worker_limit.get()),
                slippage_bps=self._inputs(),
            )
            self.execution_worker = ExecutionWorker(self.data_dir, config, lambda: rpc_for(network))
            self.execution_worker.start()
            self.status.configure(
                text="Unsigned worker started. It cannot create or submit orders."
            )
        except (ValueError, TransferError) as exc:
            self.status.configure(text=str(exc))

    def stop_worker(self):
        if self.execution_worker:
            self.execution_worker.stop()
        self.status.configure(
            text="Worker stop requested. Existing transactions still require recovery."
        )

    def render_worker(self):
        try:
            config = WorkerConfig(self.agent_id, self.address, self.network.get())
            self.worker_history = worker_history(self.data_dir, config)
        except (sqlite3.Error, TransferError):
            self.worker_summary.configure(text="Worker history temporarily unavailable; retrying")
            return
        run = self.worker_history["run"]
        current = self.execution_worker
        if run:
            state = run["status"] + (" · running" if run["active"] else " · stopped")
            delay = (
                max(0, int(run["next_at"] - time.time()))
                if run["next_at"] and run["active"]
                else None
            )
            self.worker_summary.configure(
                text=f"UNSIGNED ONLY · {state} · {run['cycles']} completed cycles"
                + (f" · next check in {delay}s" if delay is not None else "")
                + f"\n{run['detail']} · consecutive RPC errors: {run['failures']}"
                + (f"\n{current.error}" if current and current.error else "")
            )
        else:
            self.worker_summary.configure(
                text="Worker stopped · unsigned only"
                + (f"\n{current.error}" if current and current.error else "")
            )
        results = (
            self.worker_history["cycles"][0]["report"]["results"]
            if self.worker_history["cycles"]
            else []
        )
        self._fill(
            self.worker_results,
            [
                (str(i), (r["side"].upper(), r["mint"], r["status"], r["reason"]))
                for i, r in enumerate(results)
            ],
        )
        self.start_worker_button.configure(
            state="disabled" if self.busy or (current and current.running) else "normal"
        )

    def export_worker(self):
        if self.busy or not self.worker_history["cycles"]:
            return
        self.busy = True
        try:
            filename = filedialog.asksaveasfilename(
                parent=self.window,
                defaultextension=".json",
                initialfile="pump-worker-cycle.json",
                filetypes=[("JSON report", "*.json")],
            )
            if filename:
                with open(filename, "w", encoding="utf-8") as output:
                    json.dump(self.worker_history["cycles"][0]["report"], output, indent=2)
                self.status.configure(text="Unsigned worker cycle exported.")
        except OSError:
            self.status.configure(text="Could not export worker cycle; choose a writable location.")
        finally:
            self.busy = False

    def render_session(self):
        record = self.session_rows.get(self.session_id)
        if record is None:
            previous = next(iter(self.session_rows.values()), None)
            text = "No authorized session in this workspace."
            if previous:
                text += (
                    " Previous session: "
                    + previous["status"]
                    + "; fresh authorization is required."
                )
        else:
            scope = record["scope"]
            remaining = max(0, int(record["expires_at"] - time.time()))
            active = (
                self.sessions.available(self.session_id)
                and record["status"] == "active"
                and remaining > 0
            )
            if not active:
                self.sessions.lock(self.session_id)
            text = (
                f"Session {record['id'][:8]} · {'Authorized' if active else 'Locked / expired / revoked'} · {remaining}s remaining"
                f"\nSpending allowance used: {sol_text(record['used_lamports'])} / {sol_text(scope['budget_lamports'])} SOL"
                f"\nOrders used: {record['used_orders']} / {scope['max_orders']} · slippage cap: {scope['slippage_bps']} bps"
                f"\nAllocation: {scope['cash_hold'][:8]} · every order still requires approval"
            )
        self.session_summary.configure(text=text)

    def authorize_session(self):
        if self.busy:
            return
        try:
            cash = self._cash()
            options = dict(
                budget_lamports=parse_sol(self.session_fields["budget"].get()),
                duration_seconds=int(self.session_fields["minutes"].get()) * 60,
                max_orders=int(self.session_fields["orders"].get()),
                slippage_bps=int(self.session_fields["slippage"].get()),
            )
            if not self.data or not self.data["wallet_risk"]["policy"]:
                raise TransferError("Configure and save Wallet limits first.")
            options["reviewed_limits"] = dict(self.data["wallet_risk"]["policy"])
            rpc = rpc_for(self.network.get())
        except (ValueError, TransferError) as exc:
            self.status.configure(text=str(exc) or "Enter whole-number session settings.")
            return
        text = (
            f"Wallet: {self.address}\nNetwork: {rpc.network}\nAgent: {self.agent_id}\nAllocation: {cash}\n\n"
            f"Total cap: {sol_text(options['budget_lamports'])} SOL\nDuration: {options['duration_seconds'] // 60} minutes\n"
            f"Maximum orders: {options['max_orders']}\nSlippage cap: {options['slippage_bps']} bps\n\n"
            "Pump curve and supported PumpSwap exits only. Saved wallet limits remain ceilings for this session. "
            "Each order still needs review and typed approval. This replaces any previous session for this wallet/network. "
            "No automatic trading starts."
        )
        self.busy = True
        try:
            if not messagebox.askyesno(
                "Authorize bounded signing session", text, parent=self.window
            ):
                return
            answer = simpledialog.askstring(
                "Confirm signing session", f"Type SESSION {self.agent_id[:8]}:", parent=self.window
            )
            if answer != "SESSION " + self.agent_id[:8]:
                self.status.configure(text="Session authorization cancelled.")
                return
            password = simpledialog.askstring(
                "Session wallet authorization",
                "Enter the wallet password for this bounded session:",
                show="•",
                parent=self.window,
            )
            if password is None:
                return
        finally:
            self.busy = False
        self.session_cancellation = threading.Event()
        options["cancellation"] = self.session_cancellation

        def authorize():
            wallet = WalletVault(self.data_dir).unlock(password)
            return workspace.authorize_session(
                self.data_dir,
                rpc,
                self.agent_id,
                self.address,
                cash,
                self.sessions,
                wallet,
                options,
            )

        def done(ident):
            self.session_id = ident
            self.use_session.set(self.sessions.available(ident))
            self.changed("Bounded signing session authorized. Each trade still needs review.")

        self._run(authorize, done)

    def revoke_session(self):
        self.sessions.lock()
        if self.session_cancellation:
            self.session_cancellation.set()
        self.use_session.set(False)
        if self.session_id:
            self.pending_revocation = (self.session_id, self.network.get())
        self.status.configure(
            text="Session locked locally. Already submitted orders still require recovery."
        )
        self.render_session()
        if not self.busy:
            self.flush_session_revocation()

    def flush_session_revocation(self):
        if self.busy or not self.pending_revocation:
            return
        ident, network = self.pending_revocation

        def done(_):
            self.pending_revocation = None
            self.changed("Session revoked. No further session signing is available.")

        self._run(
            lambda: workspace.revoke_session(self.data_dir, self.address, network, ident), done
        )

    def reload_risk_limits(self):
        if self.busy:
            return
        network = self.network.get()
        self._run(
            lambda: workspace.risk_settings(self.data_dir, network, self.address),
            lambda result: self.render_risk(result, network, reload=True),
        )

    def refresh_portfolio_value(self):
        if self.busy:
            return
        network = self.network.get()
        rpc = rpc_for(network)
        self._run(
            lambda: workspace.refresh_risk(self.data_dir, rpc, self.address),
            lambda result: self.render_risk(result, network),
        )

    def render_risk(self, result, network, reload=False):
        limits, values = result["policy"], result["metrics"]
        if reload or self.risk_network != network:
            for name, variable in self.risk_fields.items():
                variable.set(sol_text(limits[name]) if limits and name in limits else "")
            self.risk_paused.set(limits["entries_paused"] if limits else False)
            self.risk_network = network
        self.risk_summary.configure(
            text=(
                f"{network} · "
                + ("Limits configured" if limits else "No wallet limits configured")
                + f"\nOpen cost: {sol_text(values['open_cost'])} SOL · committed buys: {sol_text(values['committed_cost'])} SOL"
                + f"\n{values['utc_day']} UTC loss: {sol_text(values['daily_loss'])} SOL · realized drawdown: {sol_text(values['realized_drawdown'])} SOL"
                + (f"\nLast portfolio estimate: {sol_text(result['portfolio']['drawdown'])} SOL drawdown · {sol_text(result['portfolio']['unrealized_pnl'])} SOL unrealized P/L"
                   if (result.get('portfolio') or {}).get('status') == 'ready' else "\nPortfolio estimate unavailable")
                + ("\n" + "; ".join(result["entry_blocks"]) if result["entry_blocks"] else "")
            )
        )

    def save_risk_limits(self):
        if self.busy:
            return
        try:
            values = {
                name: 0
                if name == "reserve_lamports" and variable.get().strip() == "0"
                else parse_sol(variable.get().strip())
                for name, variable in self.risk_fields.items()
            }
            values["entries_paused"] = self.risk_paused.get()
        except (ValueError, TransferError) as exc:
            self.status.configure(text=str(exc))
            return
        network = self.network.get()
        text = f"Wallet: {self.address}\nNetwork: {network}\n\n"
        text += "\n".join(
            f"{label}: {sol_text(values[name])} SOL" for name, label in FIELDS.items()
        )
        text += f"\nNew buys paused: {'Yes' if values['entries_paused'] else 'No'}\n\nApplies to all local agents and manual buys. Historical losses are retained. Exits remain available."
        self.busy = True
        try:
            accepted = messagebox.askyesno("Save wallet limits", text, parent=self.window)
        finally:
            self.busy = False
        if accepted:
            self._run(
                lambda: workspace.risk_settings(self.data_dir, network, self.address, values),
                lambda result: self.risk_saved(result, network),
            )

    def risk_saved(self, result, network):
        self.render_risk(result, network, reload=True)
        self.changed("Wallet limits saved. No trade authorized.")

    def save_setup(self):
        if self.busy:
            return
        try:
            values = dict(
                network=self.network.get(),
                allocation=self.allocation.get(),
                slippage_bps=self._inputs(),
            )
            workspace.preferences(self.data_dir, self.agent_id, self.address, values)
            self.status.configure(
                text="Setup saved for this agent and wallet. No funds allocated or trades authorized."
            )
        except (ValueError, TransferError) as exc:
            self.status.configure(text=str(exc))

    def copy_address(self):
        self.window.clipboard_clear()
        self.window.clipboard_append(self.address)
        self.status.configure(
            text=f"Deposit address copied · selected network: {self.network.get()}"
        )

    def enable(self):
        if self.busy:
            return
        rpc = rpc_for(self.network.get())
        self._run(
            lambda: workspace.enable_reviewed_entries(self.data_dir, rpc, self.agent_id),
            lambda _: self.changed("Reviewed entries enabled. Start tracking to collect signals."),
        )

    def toggle_tracking(self):
        if self.busy or not self.data:
            return
        enabled = not (
            self.data["tracking"]["enabled"] and self.data["instance"]["status"] == "monitoring"
        )
        rpc = rpc_for(self.network.get())
        self._run(
            lambda: workspace.tracking(self.data_dir, rpc, self.agent_id, enabled),
            lambda _: self.changed(
                "Tracking started. Waiting for fresh signals."
                if enabled
                else "Entries paused. Held positions remain monitored."
            ),
        )

    def allocate(self):
        if self.busy:
            return
        rpc, amount = rpc_for(self.network.get()), self.allocation.get().strip()
        self.status.configure(text="Checking available balance and allocating SOL locally…")
        self._run(
            lambda: workspace.allocate(self.data_dir, rpc, self.agent_id, self.address, amount),
            lambda _: self.changed("SOL allocated. No transaction was signed."),
        )

    def release(self):
        if self.busy:
            return
        try:
            row = self._selected(self.holds, self.hold_rows)
        except TransferError as exc:
            self.status.configure(text=str(exc))
            return
        self.busy = True
        try:
            approved = messagebox.askyesno(
                "Release allocation",
                "Release this local allocation?\n\nIts funds become available to manual trades. Releasing position tokens disables reviewed agent exits for that position; use Manual exit to finish selling it.",
                parent=self.window,
            )
        finally:
            self.busy = False
        if approved:
            rpc = rpc_for(self.network.get())
            self._run(
                lambda: workspace.release_allocation(
                    self.data_dir, rpc, self.agent_id, self.address, row["id"]
                ),
                lambda _: self.changed("Allocation released. No funds moved."),
            )

    def _candidate(self):
        return self._selected(self.candidates, self.candidate_rows)

    def assess(self, candidate=None):
        if self.busy:
            return
        try:
            candidate = candidate or self._candidate()
            cash, slippage, network = self._cash(), self._inputs(), self.network.get()
        except (TransferError, ValueError) as exc:
            self.status.configure(text=str(exc))
            return
        rpc = RouteCheckRpc(
            network,
            os.environ.get(
                "SOLANA_RPC_URL" if network == "mainnet-beta" else "SOLANA_DEVNET_RPC_URL"
            ),
        )
        self.status.configure(text="Simulating unsigned candidate; no signing or order creation…")

        def complete(report):
            self.tabs.select(4)
            self.changed(
                "Assessment complete: "
                + report["results"][0]["status"]
                + " · "
                + report["results"][0]["reason"]
            )

        self._run(
            lambda: workspace.shadow(
                self.data_dir, rpc, self.agent_id, self.address, cash, candidate, slippage
            ),
            complete,
        )

    def assess_position(self):
        try:
            p = self._selected(self.positions, self.position_rows)
            if p["status"] != "open":
                raise TransferError("Select an open position.")
        except TransferError as exc:
            self.status.configure(text=str(exc))
            return
        self.assess(dict(mint=p["mint"], position_id=p["id"]))

    def prepare(self):
        if self.busy:
            return
        try:
            candidate, cash, slippage = self._candidate(), self._cash(), self._inputs()
        except (TransferError, ValueError) as exc:
            self.status.configure(text=str(exc))
            return
        rpc = rpc_for(self.network.get())
        self.status.configure(
            text="Preparing a fresh unsigned review; the selected allocation will be bound…"
        )

        def complete(p):
            self.tabs.select(2)
            self.changed(
                "Review prepared. Select it under Orders to authorize or cancel.",
                select_order=p["id"],
            )

        self._run(
            lambda: workspace.prepare(
                self.data_dir,
                rpc,
                self.agent_id,
                self.address,
                cash,
                candidate["mint"],
                candidate.get("position_id"),
                slippage,
            ),
            complete,
        )

    def authorize(self):
        if self.busy:
            return
        try:
            row = self._selected(self.orders, self.order_rows)
        except TransferError as exc:
            self.status.configure(text=str(exc))
            return
        rpc = rpc_for(self.network.get())
        self._run(
            lambda: workspace.order(self.data_dir, rpc, self.agent_id, self.address, row["id"]),
            self.confirm,
        )

    def confirm(self, p):
        if not 0 <= time.time() - p["created_at"] <= PREVIEW_TTL:
            self.status.configure(text="Review expired. Cancel it and prepare a fresh quote.")
            return
        session_id = self.session_id if self.use_session.get() else None
        if self.use_session.get() and not self.sessions.available(session_id):
            self.status.configure(
                text="Session unavailable. Authorize a new session or uncheck Use this session for a separate password approval."
            )
            return
        password = None
        self.busy = True
        try:
            if not messagebox.askyesno("Review agent trade", review_text(p), parent=self.window):
                self.status.configure(
                    text="Nothing signed. The unsigned review remains available to cancel."
                )
                return
            confirmation = simpledialog.askstring(
                "Confirm this order",
                f"Type SIGN {p['id'][:8]} to authorize this one order:",
                parent=self.window,
            )
            if confirmation != "SIGN " + p["id"][:8]:
                self.status.configure(text="Authorization cancelled. Nothing was signed.")
                return
            password = (
                None
                if session_id
                else simpledialog.askstring(
                    "Wallet authorization",
                    "Enter the wallet password for this trade:",
                    show="•",
                    parent=self.window,
                )
            )
            if password is None and not session_id:
                self.status.configure(text="Authorization cancelled. Nothing was signed.")
                return
        finally:
            self.busy = False
        rpc = rpc_for(p["network"])

        def submit():
            if session_id:
                return workspace.order(
                    self.data_dir,
                    rpc,
                    self.agent_id,
                    self.address,
                    p["id"],
                    action="submit",
                    session_manager=self.sessions,
                    session_id=session_id,
                )
            wallet = WalletVault(self.data_dir).unlock(password)
            try:
                return workspace.order(
                    self.data_dir,
                    rpc,
                    self.agent_id,
                    self.address,
                    p["id"],
                    action="submit",
                    wallet=wallet,
                )
            finally:
                del wallet

        self.status.configure(text="Revalidating the reviewed order before signing…")
        self._run(
            submit,
            lambda _: self.changed(
                "Submission recorded. Current receipt status is shown under Orders."
            ),
        )

    def cancel(self):
        if self.busy:
            return
        try:
            row = self._selected(self.orders, self.order_rows)
        except TransferError as exc:
            self.status.configure(text=str(exc))
            return
        rpc = rpc_for(self.network.get())
        self._run(
            lambda: workspace.order(
                self.data_dir, rpc, self.agent_id, self.address, row["id"], action="cancel"
            ),
            lambda _: self.changed("Unsigned review cancelled. The SOL allocation is retained."),
        )

    def copy_signature(self):
        try:
            row = self._selected(self.orders, self.order_rows)
            if not row["trade"]:
                raise TransferError("Unsigned reviews have no transaction signature.")
            self.window.clipboard_clear()
            self.window.clipboard_append(row["trade"]["signature"])
            self.status.configure(text="Transaction signature copied.")
        except TransferError as exc:
            self.status.configure(text=str(exc))

    def exit_manually(self):
        if self.busy:
            return
        try:
            p = self._selected(self.positions, self.position_rows)
            if p["status"] != "open":
                raise TransferError("Select an open position.")
        except TransferError as exc:
            self.status.configure(text=str(exc))
            return
        self.busy = True
        try:
            approved = messagebox.askyesno(
                "Switch this position to manual exits",
                "Release this position’s token allocation and open the manual sell form?\n\n"
                "You can choose a partial or full quantity there. Agent exits for the remaining tokens will be disabled. "
                "Manual proceeds are credited to your wallet, outside the agent’s cash allocation. No sale occurs until you review and authorize it.",
                parent=self.window,
            )
        finally:
            self.busy = False
        if approved:
            rpc = rpc_for(self.network.get())

            def complete(_):
                self.manual_exit(p)
                self.changed("Position switched to manual exits. Review the sale in Manual trades.")

            self._run(
                lambda: workspace.release_allocation(
                    self.data_dir, rpc, self.agent_id, self.address, p["token_hold"]
                ),
                complete,
            )

    def show_order(self, _event=None):
        self.authorize_button.configure(state="disabled")
        self.cancel_button.configure(state="disabled")
        try:
            row = self._selected(self.orders, self.order_rows)
        except TransferError:
            return
        if row["status"] == "review":
            remaining = max(0, int(PREVIEW_TTL - (time.time() - row["preview"]["created_at"])))
            if not self.busy:
                self.cancel_button.configure(state="normal")
                self.authorize_button.configure(state="normal" if remaining > 0 else "disabled")
            detail = f"Review {row['id'][:8]} · expires in {remaining}s. " + (
                "Cancel and prepare again."
                if remaining == 0
                else "Authorization applies only to this order."
            )
        else:
            trade = row["trade"] or {}
            detail = trade.get("detail") or row["display_status"]
            if trade.get("receipt"):
                detail += " · Wallet change " + sol_text(trade["receipt"]["wallet_delta"]) + " SOL"
        self.order_detail.configure(text=detail)

    def show_report(self, _event=None):
        try:
            record = self._selected(self.reports, self.report_rows)
        except TransferError:
            return
        item = record["report"]["results"][0]
        risk = item.get("risk") or {}
        text = item["status"].upper() + " · " + item["reason"]
        identity = record['report'].get('identity')
        text += (f"\nPackage: {identity['package_sha256']} · engine: {identity['decision_engine']}"
                 if identity else "\nLegacy report: configuration identity was not recorded.")
        if risk:
            text += f"\nExpected net {sol_text(risk['expected_net_lamports'])} SOL · conservative floor {sol_text(risk['net_floor_lamports'])} SOL · slot {risk['slot']}"
        text += "\nHistorical unsigned assessment. Prepare a fresh review before authorization."
        self.report_detail.configure(text=text)

    def export_report(self):
        if self.busy:
            return
        try:
            report = self._selected(self.reports, self.report_rows)
        except TransferError as exc:
            self.status.configure(text=str(exc))
            return
        self.busy = True
        try:
            filename = filedialog.asksaveasfilename(
                parent=self.window,
                title="Export unsigned assessment",
                defaultextension=".json",
                filetypes=[("JSON report", "*.json")],
            )
            if filename:
                with open(filename, "w", encoding="utf-8") as output:
                    json.dump(report, output, indent=2)
                self.status.configure(
                    text="Unsigned assessment exported. No signing material is included."
                )
        except OSError:
            self.status.configure(text="Could not write the report. Choose a writable location.")
        finally:
            self.busy = False

    def changed(self, message, select_order=None):
        self.refresh_app()
        self.refresh_history(message=message, select_order=select_order)

    def refresh_history(self, message=None, select_order=None):
        if self.closed:
            return
        if self.timer:
            self.window.after_cancel(self.timer)
        self.timer = self.window.after(15_000, self.refresh_history)
        if self.busy:
            return
        rpc = rpc_for(self.network.get())
        self._run(
            lambda: workspace.snapshot(self.data_dir, rpc, self.agent_id, self.address),
            lambda data: self.render(data, message, select_order),
        )

    def _fill(self, tree, rows):
        selected = tree.selection()
        tree.delete(*tree.get_children())
        for ident, values in rows:
            tree.insert("", "end", iid=ident, values=values)
        if selected and tree.exists(selected[0]):
            tree.selection_set(selected[0])

    def render(self, data, message=None, select_order=None):
        def financial_state(value):
            return [(r["id"], r["display_status"]) for r in value["orders"]] + [
                (r["id"], r["quantity"], r["status"]) for r in value["positions"]
            ]

        financial_changed = self.data is None or financial_state(data) != financial_state(self.data)
        self.data = data
        self.session_rows = {
            r["id"]: r for r in data["signing_sessions"] if r["agent_id"] == self.agent_id
        }
        self.render_session()
        self.render_risk(data["wallet_risk"], data["network"])
        self.title.configure(text=data["instance"]["label"] + " · trading")
        balance = (
            "Balance unavailable"
            if data["balance"] is None
            else f"{sol_text(data['balance'])} SOL total · {sol_text(data['available'])} SOL unallocated"
        )
        self.summary.configure(
            text=f"{data['network']} · {balance} · checked {time.strftime('%H:%M:%S', time.localtime(data['checked_at']))}\n"
            "Automatic execution disabled · Funded acceptance pending · See State & recovery for blocks and next steps"
        )
        StrategyBrowser.write(self.operations, workspace.operations_text(data) + "\n\n" +
                              support_text(parse_package(data["instance"]["package"])))
        self._fill(
            self.checks,
            [(str(i), (r["name"], r["state"], r["detail"])) for i, r in enumerate(data["checks"])],
        )
        self.hold_rows = {r["id"]: r for r in data["holds"]}
        self._fill(
            self.holds,
            [
                (
                    r["id"],
                    (
                        "Tokens" if r["position_id"] else "SOL",
                        token_text(r["token_atoms"]) + " tokens"
                        if r["position_id"]
                        else sol_text(int(r["lamports"])) + " SOL",
                        r["position_id"] or "Cash allocation",
                        r["id"],
                    ),
                )
                for r in data["holds"]
            ],
        )
        selected = self.cash_choices.get(self.cash.get())
        self.cash_choices = {
            sol_text(int(r["lamports"])) + " SOL · " + r["id"][:8]: r["id"]
            for r in data["free_cash"]
        }
        self.cash_choice.configure(values=tuple(self.cash_choices))
        self.cash.set(
            next(
                (label for label, ident in self.cash_choices.items() if ident == selected),
                next(iter(self.cash_choices), "No available allocation"),
            )
        )
        self.candidate_rows = {r["id"]: r for r in data["candidates"]}
        self._fill(
            self.candidates,
            [
                (
                    r["id"],
                    (
                        r["side"].upper(),
                        r["mint"],
                        token_text(r["quantity"]) if r["quantity"] else "Strategy budget",
                        ("Fresh · " if r["fresh"] else "Stale · ") + str(r["quote_age"]) + "s"
                        if r["quote_age"] is not None
                        else "No quote",
                        r["reason"].replace("_", " "),
                    ),
                )
                for r in data["candidates"]
            ],
        )
        self.order_rows = {r["id"]: r for r in data["orders"]}
        self._fill(
            self.orders,
            [
                (
                    r["id"],
                    (
                        r["side"].upper(),
                        r["mint"],
                        r["display_status"],
                        sol_text(r["preview"]["amount"]) + " SOL"
                        if r["side"] == "buy"
                        else token_text(r["preview"]["amount"]) + " tokens",
                        r["preview"]["agent_order"]["reason"].replace("_", " "),
                    ),
                )
                for r in data["orders"]
            ],
        )
        if select_order and self.orders.exists(select_order):
            self.orders.selection_set(select_order)
        self.position_rows = {r["id"]: r for r in data["positions"]}
        self._fill(
            self.positions,
            [
                (
                    r["id"],
                    (
                        r["mint"],
                        token_text(r["quantity"]),
                        sol_text(int(r["remaining_cost"])),
                        sol_text(int(r["realized_proceeds"]) - int(r["realized_cost"])),
                        "manual exit"
                        if r["status"] == "open" and not r["managed"]
                        else r["status"],
                    ),
                )
                for r in data["positions"]
            ],
        )
        self.report_rows = {str(r["id"]): r for r in data["reports"] if r["report"]["results"]}
        self._fill(
            self.reports,
            [
                (
                    str(r["id"]),
                    (
                        time.strftime("%m-%d %H:%M:%S", time.localtime(r["at"])),
                        r["report"]["results"][0]["mint"],
                        r["report"]["results"][0]["status"],
                        r["report"]["results"][0]["reason"],
                    ),
                )
                for r in self.report_rows.values()
            ],
        )
        self.enable_button.configure(state="normal" if data["supported"] else "disabled")
        self.track_button.configure(
            text="Pause entries"
            if data["tracking"]["enabled"] and data["instance"]["status"] == "monitoring"
            else "Start tracking"
        )
        self.allocate_button.configure(
            state="normal"
            if data["supported"]
            and data["balance"] is not None
            and data["balance"] > 0
            and not data["pending"]
            else "disabled"
        )
        self.show_order()
        self.status.configure(
            text=message
            or data["rpc_error"]
            or data["recovery_error"]
            or (
                "Unresolved wallet operation. Use Refresh & recover; keep holds and the original signature."
                if data["pending"]
                else
                "Awaiting deposit. Save setup and export an encrypted wallet backup before testing."
                if data["balance"] == 0
                else "Select a candidate for an unsigned assessment. Every trade requires fresh approval; use a password or an authorized session."
            )
        )
        if financial_changed:
            self.refresh_app()
