"""Desktop lifecycle smoke with throwaway keys and synthetic RPC; no network."""

import os
from pathlib import Path
import socket
import tempfile
import time
import tkinter as tk
from unittest.mock import patch

from pumpwizard_runner.desktop import RunnerWindow
from pumpwizard_runner.manual_trades import TradeJournal
from pumpwizard_runner.transfers import GENESIS
from pumpwizard_runner.wallet_vault import WalletVault
from test_manual_trades import MINT
from test_pump_swap_route import SwapRpc
from test_token2022_route import Token2022Rpc


def main(token2022=False):
    directory = Path(tempfile.mkdtemp(prefix="wizard-manual-ui-"))
    password = "synthetic ui test password"
    wallet = WalletVault(directory).create(password)
    rpc = Token2022Rpc(wallet) if token2022 else SwapRpc(wallet)
    rpc.network = "devnet"
    rpc.genesis = lambda: GENESIS["devnet"]
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    root = tk.Tk()
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)

    def wait(predicate):
        deadline = time.monotonic() + 10
        while not predicate():
            root.update()
            if errors:
                raise AssertionError(errors)
            if time.monotonic() > deadline:
                raise AssertionError("UI timed out: " + panel.status.cget("text"))
            time.sleep(0.02)
        root.update()
        assert not errors

    with (
        patch.object(socket.socket, "connect", side_effect=AssertionError("UI tried live network")),
        patch.object(RunnerWindow, "_unlock_local_wallet", return_value=wallet),
        patch.object(RunnerWindow, "show_patch_notes"),
        patch.object(RunnerWindow, "refresh_market_watch"),
        patch.object(RunnerWindow, "sync_wallet_balance"),
        patch.object(RunnerWindow, "recover_manual_trades"),
        patch("pumpwizard_runner.desktop.MonitorScheduler.start"),
        patch("pumpwizard_runner.trade_panel.rpc_for", return_value=rpc),
        patch("pumpwizard_runner.trade_panel.simpledialog.askstring", return_value=password),
        patch("pumpwizard_runner.trade_panel.messagebox.askyesno", return_value=False) as approve,
    ):
        app = RunnerWindow(root, directory)
        app.open_manual_trades()
        panel = app.trade_panel
        wait(lambda: not panel.busy)
        panel.network.set("devnet")
        panel.mint.set(MINT)
        panel.amount.set("0.1")
        panel.review()
        wait(lambda: not panel.busy and "cancelled" in panel.status.cget("text"))
        assert not rpc.sent
        approve.return_value = True
        panel.review()
        wait(lambda: len(panel.position_rows) == 1 and not panel.busy)
        ident = next(iter(panel.position_rows))
        rpc.graduated = True
        for quantity in ("400", "600"):
            panel.positions.selection_set(ident)
            panel.sell_selected()
            panel.amount.set(quantity)
            before = len(rpc.sent)
            panel.review()
            wait(
                lambda: len(rpc.sent) == before + 1
                and not panel.busy
                and len(panel.order_rows) == before + 1
            )
            assert "PumpSwap migration exit" in approve.call_args.args[1]
        assert panel.position_rows[ident]["status"] == "closed"
        assert not app.position_rail.get_children()
        assert len(rpc.sent) == 3
        panel.close()
        app.open_manual_trades()
        panel = app.trade_panel
        wait(lambda: len(panel.order_rows) == 3 and not panel.busy)
        assert panel.position_rows[ident]["status"] == "closed"
        root.update()
        if os.environ.get("WIZARD_UI_SCREENSHOT"):
            from PIL import ImageGrab

            ImageGrab.grab().save(os.environ["WIZARD_UI_SCREENSHOT"])
        journal = TradeJournal(directory)
        assert not journal.pending(wallet.address, rpc.genesis())
        journal.close()
        panel.close()
        app.close()
    print(
        f"PASS ({'Token-2022' if token2022 else 'SPL'}): cancel, reviewed buy, graduation, partial/full PumpSwap exit, sidebar, reopen/history; synthetic RPC only"
    )


if __name__ == "__main__":
    main()
    main(token2022=True)
