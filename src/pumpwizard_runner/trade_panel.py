"""Manual trade review and position history. Tk never performs network I/O."""

from __future__ import annotations

from decimal import Decimal
import os
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from .manual_trades import ACTIVE, ManualTradeService, TradeJournal, TradeRpc
from .pump_route import MAX_ACCOUNT_FUNDING
from .pump_swap_route import ROUTES as SWAP_ROUTES
from .transfers import GENESIS, sol_text
from .wallet_vault import WalletVault


def token_text(atoms):
    return format(Decimal(atoms) / 1_000_000, "f")


def rpc_for(network):
    variable = "SOLANA_RPC_URL" if network == "mainnet-beta" else "SOLANA_DEVNET_RPC_URL"
    return TradeRpc(network, os.environ.get(variable) if network != "localnet" else None)


def reconcile_wallet(data_dir, address):
    """Read-only recovery, including networks other than the currently selected one."""
    journal = TradeJournal(data_dir)
    try:
        networks = {
            r["payload"]["network"] for r in journal.trades(address) if r["status"] in ACTIVE
        }
        errors = []
        for network in networks:
            try:
                ManualTradeService(rpc_for(network), journal).reconcile(address)
            except Exception:
                errors.append(network)
        return journal.trades(address), journal.positions(address), errors
    finally:
        journal.close()


class TradePanel:
    def __init__(self, root, data_dir, address, refresh, mint=""):
        self.data_dir, self.address, self.refresh = data_dir, address, refresh
        self.busy, self.closed, self.result, self.timer = False, False, None, None
        self.position_id = None
        self.requested_position = None
        self.window = tk.Toplevel(root)
        self.window.title("Manual trades · SolanaMarket Wizard")
        self.window.geometry("1000x740")
        self.window.minsize(860, 650)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(self.window, style="Panel.TFrame", padding=18)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(9, weight=1)
        ttk.Label(body, text="Manual Pump trades", style="Hero.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(
            body,
            text="SPL and supported Token-2022 tokens · curve trades and canonical PumpSwap exits · manual only",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=8)
        self.network = tk.StringVar(value="mainnet-beta")
        self.mint, self.amount = tk.StringVar(value=mint), tk.StringVar()
        self.slippage = tk.StringVar(value="100")
        self.side = "buy"
        self.controls = []
        for index, (label, variable) in enumerate(
            (
                ("Network", self.network),
                ("Token mint", self.mint),
                ("Buy amount (SOL)", self.amount),
                ("Slippage (basis points; 100 = 1%)", self.slippage),
            ),
            2,
        ):
            caption = ttk.Label(body, text=label, style="Panel.TLabel")
            caption.grid(row=index, column=0, sticky="w", padx=(0, 12), pady=5)
            if index == 4:
                self.amount_caption = caption
            control = (
                ttk.Combobox(
                    body, textvariable=variable, values=("mainnet-beta", "devnet"), state="readonly"
                )
                if index == 2
                else ttk.Entry(body, textvariable=variable)
            )
            control.grid(row=index, column=1, sticky="ew", pady=5)
            self.controls.append(control)
        self.controls[0].bind("<<ComboboxSelected>>", lambda _: self.new_buy())
        actions = ttk.Frame(body, style="Panel.TFrame")
        actions.grid(row=6, column=0, columnspan=2, sticky="w", pady=8)
        for label, action in [
            ("New buy", self.new_buy),
            ("Sell selected position", self.sell_selected),
            ("Review trade", self.review),
            ("Refresh receipts", self.refresh_history),
        ]:
            button = ttk.Button(actions, text=label, command=action, style="Subtle.TButton")
            button.pack(side="left", padx=(0, 8))
            self.controls.append(button)
        self.status = ttk.Label(
            body, text="Loading local history…", style="Muted.TLabel", wraplength=900
        )
        self.status.grid(row=7, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Label(
            body,
            text="Positions use finalized balances. SOL cash costs include fees and account funding.",
            style="Muted.TLabel",
        ).grid(row=8, column=0, columnspan=2, sticky="w")
        tabs = ttk.Notebook(body)
        tabs.grid(row=9, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        self.positions = self._table(
            tabs,
            "Positions",
            [
                ("network", 100),
                ("mint", 240),
                ("tokens", 140),
                ("cost_SOL", 140),
                ("realized_SOL", 140),
                ("status", 100),
            ],
        )
        self.history = self._table(
            tabs,
            "Orders",
            [("side", 70), ("mint", 270), ("network", 100), ("status", 110), ("signature", 300)],
        )
        self.history.bind("<<TreeviewSelect>>", self.show_order)
        self.history.bind("<Double-Button-1>", self.copy_signature)
        self.position_rows, self.order_rows = {}, {}
        self.refresh_history()

    def _table(self, tabs, title, columns):
        frame = ttk.Frame(tabs)
        tabs.add(frame, text=title)
        table = ttk.Treeview(
            frame,
            columns=[x[0] for x in columns],
            show="headings",
            selectmode="browse",
            style="App.Treeview",
        )
        for name, width in columns:
            table.heading(name, text=name.replace("_", " ").upper())
            table.column(name, width=width)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=table.yview)
        scrollbar.pack(side="right", fill="y")
        table.configure(yscrollcommand=scrollbar.set)
        table.pack(fill="both", expand=True)
        return table

    def close(self):
        if self.busy:
            self.status.configure(text="Wait for the current operation to finish.")
            return
        self.closed = True
        if self.timer:
            self.window.after_cancel(self.timer)
        self.window.destroy()

    def _run(self, work, done):
        if self.busy or self.closed:
            return
        self.busy = True
        for control in self.controls:
            control.configure(state="disabled")
        self.result = None

        def worker():
            try:
                self.result = (True, work())
            except Exception as exc:
                self.result = (False, str(exc)[:300])

        threading.Thread(target=worker, daemon=False, name="manual-trade-operation").start()

        def poll():
            if self.closed:
                return
            if self.result is None:
                self.window.after(100, poll)
                return
            success, value = self.result
            self.busy = False
            for control in self.controls:
                control.configure(state="normal")
            self.controls[0].configure(state="readonly")
            if success:
                done(value)
            else:
                self.status.configure(text=value)

        self.window.after(100, poll)

    def _service(self, network, fn):
        journal = TradeJournal(self.data_dir)
        try:
            return fn(ManualTradeService(rpc_for(network), journal))
        finally:
            journal.close()

    def new_buy(self):
        if self.busy:
            return
        self.side, self.position_id = "buy", None
        self.amount.set("")
        self.amount_caption.configure(text="Buy amount (SOL)")
        self.status.configure(
            text="Enter a mint and exact SOL spend. Review does not sign or submit."
        )

    def sell_selected(self):
        selected = self.positions.selection()
        if self.busy or not selected:
            return
        row = self.position_rows[selected[0]]
        if row["status"] != "open":
            self.status.configure(text="Select an open position.")
            return
        network = next(
            (name for name, genesis in GENESIS.items() if genesis == row["genesis"]), None
        )
        if network is None:
            self.status.configure(text="This position belongs to an unsupported desktop network.")
            return
        self.network.set(network)
        self.position_id, self.side = row["id"], "sell"
        self.mint.set(row["mint"])
        self.amount.set(token_text(row["quantity"]))
        self.amount_caption.configure(text="Sell quantity (tokens)")
        self.status.configure(
            text="Full quantity selected. Enter a smaller quantity for a partial sell."
        )

    def select_position(self, ident):
        self.requested_position = ident
        if not self.busy and ident in self.position_rows:
            self.positions.selection_set(ident)
            self.sell_selected()
            self.requested_position = None

    def review(self):
        if self.busy:
            return
        network, side, mint, amount, position = (
            self.network.get(),
            self.side,
            self.mint.get().strip(),
            self.amount.get().strip(),
            self.position_id,
        )
        try:
            slippage = int(self.slippage.get())
        except ValueError:
            self.status.configure(text="Enter slippage as an integer number of basis points.")
            return
        self.status.configure(text="Reading on-chain accounts and simulating an unsigned quote…")
        self._run(
            lambda: self._service(
                network,
                lambda service: service.preview(
                    self.address, mint, side, amount, slippage, position
                ),
            ),
            self.confirm,
        )

    def confirm(self, p):
        # Keep refresh timers and form actions disabled while modal dialogs run.
        self.busy = True
        try:
            unit = "tokens" if p["side"] == "buy" else "SOL before fees/account funding"
            output = (
                token_text(p["minimum_output"])
                if p["side"] == "buy"
                else sol_text(p["minimum_output"])
            )
            amount = (
                sol_text(p["amount"]) + " SOL"
                if p["side"] == "buy"
                else token_text(p["amount"]) + " tokens"
            )
            debit = (
                f"Maximum SOL cash debit: {sol_text(p['max_wallet_debit'])}"
                if p["side"] == "buy"
                else f"Conservative net SOL floor: {sol_text(p['minimum_output'] - p['fee'] - MAX_ACCOUNT_FUNDING)}"
            )
            text = (
                f"Network: {p['network']} {'(REAL SOL)' if p['network'] == 'mainnet-beta' else '(test SOL)'}\n"
                f"Wallet: {p['sender']}\nToken: {p['mint']}\n\n{p['side'].upper()}: {amount}\n"
                f"Venue: {'PumpSwap migration exit' if p['route'] in SWAP_ROUTES else 'Pump bonding curve'}\n"
                f"Minimum route output: {output} {unit}\n"
                f"Network fee: {sol_text(p['fee'])} SOL\n"
                f"Account funding allowance: {sol_text(MAX_ACCOUNT_FUNDING)} SOL\n"
                f"Estimated wallet change: {sol_text(p['expected_wallet_delta'])} SOL\n{debit}\n\n"
                "Graduation exits require a supported canonical SOL pool and a fresh review. "
                "Automatic exits are unavailable.\n\nSign and submit this one trade?"
            )
            if not messagebox.askyesno("Review manual trade", text, parent=self.window):
                self.status.configure(text="Trade cancelled. Nothing was signed or submitted.")
                return
            password = simpledialog.askstring(
                "Authorize trade",
                "Enter the wallet password to sign this trade:",
                show="•",
                parent=self.window,
            )
            if password is None:
                self.status.configure(text="Trade cancelled. Nothing was signed or submitted.")
                return
        finally:
            self.busy = False
        self.status.configure(text="Revalidating the reviewed quote and submitting…")
        self._run(
            lambda: self._service(
                p["network"],
                lambda service: service.submit(
                    p["id"], WalletVault(self.data_dir).unlock(password)
                ),
            ),
            lambda _: self.refresh_history(),
        )

    def refresh_history(self):
        if self.closed:
            return
        if self.timer:
            self.window.after_cancel(self.timer)
        self.timer = self.window.after(10_000, self.refresh_history)
        if self.busy:
            return
        self._run(lambda: reconcile_wallet(self.data_dir, self.address), self.render)

    def render(self, result):
        orders, positions, errors = result
        self.order_rows, self.position_rows = (
            {r["id"]: r for r in orders},
            {r["id"]: r for r in positions},
        )
        for table in (self.positions, self.history):
            selected = table.selection()
            for item in table.get_children():
                table.delete(item)
            rows = positions if table is self.positions else orders
            for row in rows:
                if table is self.positions:
                    values = (
                        next(
                            (
                                name
                                for name, genesis in GENESIS.items()
                                if genesis == row["genesis"]
                            ),
                            "localnet",
                        ),
                        row["mint"],
                        token_text(row["quantity"]),
                        sol_text(int(row["remaining_cost"])),
                        sol_text(int(row["realized_proceeds"]) - int(row["realized_cost"])),
                        row["status"],
                    )
                else:
                    p = row["payload"]
                    values = (p["side"], p["mint"], p["network"], row["status"], row["signature"])
                table.insert("", "end", iid=row["id"], values=values)
            if selected and table.exists(selected[0]):
                table.selection_set(selected[0])
        self.status.configure(
            text="Some receipts await RPC access; wallet reservations remain active."
            if errors
            else "History refreshed. Select an order for its recovery state, or a position to sell."
        )
        self.refresh()
        if self.requested_position:
            self.select_position(self.requested_position)

    def show_order(self, _event=None):
        selection = self.history.selection()
        if selection and selection[0] in self.order_rows:
            row = self.order_rows[selection[0]]
            receipt = row["receipt"]
            suffix = (
                f" · Fee {sol_text(receipt['fee_lamports'])} SOL · Wallet change {sol_text(receipt['wallet_delta'])} SOL"
                if receipt
                else ""
            )
            self.status.configure(text=row["detail"] + suffix)

    def copy_signature(self, _event=None):
        selection = self.history.selection()
        if selection and selection[0] in self.order_rows:
            self.window.clipboard_clear()
            self.window.clipboard_append(self.order_rows[selection[0]]["signature"])
            self.status.configure(text="Transaction signature copied.")
