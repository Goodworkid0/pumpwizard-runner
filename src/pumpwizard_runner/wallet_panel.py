"""Wallet management and user-confirmed SOL withdrawals; network work stays off Tk."""
from __future__ import annotations

from pathlib import Path
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from .transfers import ENDPOINTS, TransferError, TransferJournal, TransferRpc, WithdrawalService, sol_text
from .wallet_vault import WalletVault


class WalletPanel:
    def __init__(self, root: tk.Tk, data_dir: Path, address: str, on_restore):
        self.data_dir, self.address, self.on_restore = data_dir, address, on_restore
        self.busy = False
        self.prompting = False
        self.closed = False
        self.result = None
        self.timer = None
        self.window = tk.Toplevel(root)
        self.window.title("Wallet · SolanaMarket Wizard")
        self.window.geometry("820x640")
        self.window.minsize(720, 580)
        self.window.configure(background="#121a2d")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(self.window, style="Panel.TFrame", padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(8, weight=1)
        ttk.Label(body, text="Wallet & withdrawals", style="Hero.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(body, text=f"Deposit address: {address}", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=8)
        actions = ttk.Frame(body, style="Panel.TFrame")
        actions.grid(row=2, column=0, sticky="ew", pady=8)
        self.controls = []
        for label, action in (("Copy address", self.copy_address), ("Encrypted backup", self.backup),
                              ("Restore backup", self.restore)):
            button = ttk.Button(actions, text=label, command=lambda fn=action: self._prompt(fn), style="Subtle.TButton")
            button.pack(side="left", padx=(0, 8))
            self.controls.append(button)
        form = ttk.Frame(body, style="Card.TFrame", padding=14)
        form.grid(row=3, column=0, sticky="ew", pady=10)
        form.columnconfigure(1, weight=1)
        self.network = tk.StringVar(value="mainnet-beta")
        self.recipient = tk.StringVar()
        self.amount = tk.StringVar()
        ttk.Label(form, text="Network", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.network_choice = ttk.Combobox(form, textvariable=self.network, values=tuple(ENDPOINTS),
                                            state="readonly", style="Agent.TCombobox")
        self.network_choice.grid(row=0, column=1, sticky="ew")
        self.network_choice.bind("<<ComboboxSelected>>", self.change_network)
        for row, (label, variable) in enumerate((("Recipient address", self.recipient), ("Amount (SOL)", self.amount)), 1):
            ttk.Label(form, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=8)
            entry = ttk.Entry(form, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew", pady=8)
            self.controls.append(entry)
        self.preview_button = ttk.Button(form, text="Review withdrawal", style="Accent.TButton", command=self.review)
        self.preview_button.grid(row=3, column=1, sticky="e", pady=(6, 0))
        self.controls.append(self.preview_button)
        self.status = ttk.Label(body, text="Loading wallet history…", style="Muted.TLabel", wraplength=740)
        self.status.grid(row=4, column=0, sticky="w", pady=8)
        ttk.Label(body, text="WITHDRAWAL HISTORY · double-click to copy a transaction signature", style="Section.TLabel").grid(
            row=6, column=0, sticky="w", pady=(8, 6))
        self.history = ttk.Treeview(body, columns=("network", "amount", "to", "status"), show="headings", style="App.Treeview")
        for name, title, width in (("network", "NETWORK", 100), ("amount", "SOL", 100),
                                   ("to", "RECIPIENT", 250), ("status", "STATUS", 110)):
            self.history.heading(name, text=title)
            self.history.column(name, width=width)
        self.history.grid(row=8, column=0, sticky="nsew")
        self.history.bind("<Double-Button-1>", self.history_selected)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.history.yview)
        scrollbar.grid(row=8, column=1, sticky="ns")
        self.history.configure(yscrollcommand=scrollbar.set)
        self.history_rows = {}
        self.refresh_history()

    def close(self):
        if self.busy:
            self.status.configure(text="Wait for the current wallet operation to finish.")
            return
        self.closed = True
        if self.timer:
            self.window.after_cancel(self.timer)
        self.window.destroy()

    def _run(self, operation, complete):
        if self.busy or self.closed:
            return
        self.busy = True
        self.result = None
        for control in self.controls:
            control.configure(state="disabled")
        self.network_choice.configure(state="disabled")

        def worker():
            try:
                self.result = (True, operation())
            except Exception as exc:
                self.result = (False, str(exc)[:240] or "Wallet operation failed")

        # Allow an authorized submission to journal its outcome even if the app closes.
        thread = threading.Thread(target=worker, name="wallet-operation", daemon=False)
        thread.start()

        def poll():
            if self.closed:
                return
            if thread.is_alive():
                self.window.after(100, poll)
                return
            self.busy = False
            for control in self.controls:
                control.configure(state="normal")
            self.network_choice.configure(state="readonly")
            success, result = self.result
            self.result = None
            if success:
                self._prompt(lambda: complete(result))
            else:
                self.status.configure(text=result)
        self.window.after(100, poll)

    def _prompt(self, action):
        self.prompting = True
        try:
            action()
        finally:
            self.prompting = False

    def _service(self, network, action):
        journal = TransferJournal(self.data_dir)
        try:
            return action(WithdrawalService(TransferRpc(network), journal))
        finally:
            journal.close()

    def review(self):
        network, recipient, amount = self.network.get(), self.recipient.get().strip(), self.amount.get().strip()
        self.status.configure(text="Checking network, balance and transfer fee…")
        self._run(lambda: self._service(network, lambda service: service.preview(self.address, recipient, amount)), self.confirm)

    def confirm(self, preview):
        if not messagebox.askyesno("Confirm SOL withdrawal", (
            f"Network: {preview.network}\n\nFrom: {preview.sender}\nTo: {preview.recipient}\n\n"
            f"Amount: {sol_text(preview.lamports)} SOL\nNetwork fee: {sol_text(preview.fee)} SOL\n"
            f"Total: {sol_text(preview.lamports + preview.fee)} SOL\n\nSend this transfer?"
        ), parent=self.window):
            self.status.configure(text="Withdrawal cancelled. Nothing was submitted.")
            return
        password = simpledialog.askstring("Authorize withdrawal", "Enter your wallet password to sign this transfer:",
                                           show="•", parent=self.window)
        if password is None:
            self.status.configure(text="Withdrawal cancelled. Nothing was submitted.")
            return
        self.status.configure(text="Validating and submitting the approved transfer…")

        def submit():
            wallet = WalletVault(self.data_dir).unlock(password)
            return self._service(preview.network, lambda service: service.submit(preview, wallet))

        def submitted(signature):
            self.status.configure(text="Transfer recorded. Waiting for network confirmation.")
            self.refresh_history()
        self._run(submit, submitted)

    def refresh_history(self):
        if self.closed:
            return
        if self.timer:
            self.window.after_cancel(self.timer)
        self.timer = self.window.after(5_000, self.refresh_history)
        if self.busy or self.prompting:
            return
        network = self.network.get()

        def read():
            journal = TransferJournal(self.data_dir)
            try:
                # Reconcile all recorded networks, so reopening on mainnet still recovers devnet work.
                networks = {r["network"] for r in journal.rows(self.address)
                            if r["status"] in {"pending", "unknown", "confirmed"}}
                error = ""
                for selected in networks:
                    try:
                        WithdrawalService(TransferRpc(selected), journal).reconcile(self.address)
                    except TransferError:
                        error = "Some transfers await a connection to their network."
                rpc = TransferRpc(network)
                try:
                    rpc.genesis()
                    balance = rpc.call("getBalance", [self.address, {"commitment": "confirmed"}])["value"]
                    message = f"{network} · Balance {sol_text(balance)} SOL. {error}"
                except (TransferError, KeyError, TypeError):
                    message = "Connection unavailable. Saved withdrawal history is shown."
                return journal.rows(self.address), message
            finally:
                journal.close()

        def render(result):
            rows, message = result
            selected = self.history.selection()
            self.history_rows = {row["id"]: row for row in rows}
            for item in self.history.get_children():
                self.history.delete(item)
            for row in rows:
                self.history.insert("", "end", iid=row["id"], values=(
                    row["network"], sol_text(int(row["lamports"])), row["recipient"], row["status"]))
            if selected and selected[0] in self.history_rows:
                self.history.selection_set(selected[0])
            self.status.configure(text=message)
        self._run(read, render)

    def change_network(self, _event):
        self.refresh_history()

    def history_selected(self, _event):
        selected = self.history.selection()
        if selected and selected[0] in self.history_rows:
            row = self.history_rows[selected[0]]
            self.window.clipboard_clear()
            self.window.clipboard_append(row["signature"])
            self.status.configure(text=f"{row['detail']} · Signature copied: {row['signature']}")

    def copy_address(self):
        self.window.clipboard_clear()
        self.window.clipboard_append(self.address)
        self.status.configure(text="Deposit address copied. Use the selected Solana network.")

    def backup(self):
        filename = filedialog.asksaveasfilename(parent=self.window, title="Save encrypted wallet backup",
                                               initialfile="solanamarket-wallet-backup.json", defaultextension=".json")
        if not filename:
            return
        password = simpledialog.askstring("Verify wallet password", "Your current wallet password also protects this backup:",
                                           show="•", parent=self.window)
        if password is not None:
            self._run(lambda: WalletVault(self.data_dir).export_backup(Path(filename), password),
                      lambda _: self.status.configure(text="Encrypted backup saved. Keep the password separately; both are needed to restore."))

    def restore(self):
        journal = TransferJournal(self.data_dir)
        try:
            if journal.db.execute("SELECT 1 FROM wallet_intents WHERE sender=?", (self.address,)).fetchone():
                self.status.configure(text="Resolve pending trades and withdrawals before switching wallets.")
                return
        finally:
            journal.close()
        filename = filedialog.askopenfilename(parent=self.window, title="Restore encrypted wallet backup", filetypes=[("JSON backup", "*.json")])
        if not filename:
            return
        password = simpledialog.askstring("Backup password", "Enter the password for this encrypted backup:", show="•", parent=self.window)
        if password is None:
            return

        def validate():
            vault = WalletVault(Path(filename).parent)
            vault.path = Path(filename)
            return vault.unlock(password).address

        def validated(address):
            if not messagebox.askyesno("Restore wallet", f"Switch to wallet:\n{address}\n\nThe current encrypted vault will be preserved locally. The app will close; reopen it with the backup password.", parent=self.window):
                return
            self._run(lambda: WalletVault(self.data_dir).restore_backup(Path(filename), password, address),
                      lambda _: self.on_restore())
        self._run(validate, validated)
