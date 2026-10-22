"""Guided desktop acceptance using synthetic funds, with all live sockets blocked."""

import os
from pathlib import Path
import socket
import tempfile
import time
import tkinter as tk
from unittest.mock import patch

from pumpwizard_runner import trading_workspace as workspace
from pumpwizard_runner.desktop import RunnerWindow
from pumpwizard_runner.wallet_vault import WalletVault
from test_agent_execution import MINT, observe
from test_trading_workspace import setup


def main(manual_override=False, session_mode=False):
    directory = Path(tempfile.mkdtemp(prefix="wizard-guided-ui-"))
    fixture = setup.__wrapped__(directory)
    wallet, rpc, journal, library, agent, cash, _, _ = next(fixture)
    workspace.release_allocation(directory, rpc, agent, wallet.address, cash["id"])
    workspace.preferences(
        directory, agent, wallet.address, dict(network="devnet", allocation="0.2", slippage_bps=100)
    )
    rpc.balance = 0
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    root = tk.Tk()
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    panel = None

    def wait(predicate):
        deadline = time.monotonic() + 15
        while not predicate():
            root.update()
            if errors:
                raise AssertionError(errors)
            if time.monotonic() > deadline:
                raise AssertionError("UI timed out: " + panel.status.cget("text"))
            time.sleep(0.02)
        root.update()
        assert not errors

    def screenshot(name):
        if os.environ.get("WIZARD_UI_SCREENSHOTS"):
            from PIL import ImageGrab

            panel.window.lift()
            root.update()
            ImageGrab.grab().save("/tmp/pumpwizard-dev36-" + name + ".png")

    with (
        patch.object(socket.socket, "connect", side_effect=AssertionError("UI tried live network")),
        patch.object(RunnerWindow, "_unlock_local_wallet", return_value=wallet),
        patch.object(RunnerWindow, "show_patch_notes"),
        patch.object(RunnerWindow, "refresh_market_watch"),
        patch.object(RunnerWindow, "sync_wallet_balance"),
        patch.object(RunnerWindow, "recover_manual_trades"),
        patch("pumpwizard_runner.desktop.MonitorScheduler.start"),
        patch("pumpwizard_runner.trading_panel.rpc_for", return_value=rpc),
        patch("pumpwizard_runner.trading_panel.RouteCheckRpc", return_value=rpc),
        patch("pumpwizard_runner.trade_panel.rpc_for", return_value=rpc),
        patch(
            "pumpwizard_runner.trading_panel.WalletVault",
            side_effect=lambda _: WalletVault(directory / "vault"),
        ),
        patch(
            "pumpwizard_runner.trade_panel.WalletVault",
            side_effect=lambda _: WalletVault(directory / "vault"),
        ),
        patch("pumpwizard_runner.trading_panel.messagebox.askyesno", return_value=False) as approve,
        patch("pumpwizard_runner.trading_panel.simpledialog.askstring") as answer,
    ):
        app = RunnerWindow(root, directory)
        app._show_agent(agent)
        app.open_agent_trading()
        panel = app.trading_panel
        wait(lambda: not panel.busy and panel.data is not None)
        assert panel.data["balance"] == 0 and not panel.hold_rows
        assert "Automatic execution disabled" in panel.summary.cget("text")
        assert "Funded acceptance pending" in panel.summary.cget("text")
        assert "Recent local decisions" in panel.operations.get("1.0", "end")
        assert "strategy_entry_signal" in panel.operations.get("1.0", "end")
        panel.tabs.select(8)
        screenshot("state-recovery")
        panel.tabs.select(0)
        assert panel.data['wallet_risk']['policy'] is None
        for name, value in dict(max_buy_lamports='0.2', max_exposure_lamports='0.5',
                                reserve_lamports='0.1', daily_loss_lamports='0.1',
                                drawdown_lamports='0.1', portfolio_drawdown_lamports='0.5').items():
            panel.risk_fields[name].set(value)
        panel.save_risk_limits()  # Declined policy review does not save anything.
        assert workspace.risk_settings(directory, 'devnet', wallet.address)['policy'] is None
        approve.return_value = True
        panel.save_risk_limits()
        wait(lambda: not panel.busy and panel.data['wallet_risk']['policy'] is not None)
        assert panel.data['wallet_risk']['policy']['max_buy_lamports'] == 200_000_000
        assert not rpc.sent
        panel.reload_risk_limits()
        wait(lambda: not panel.busy)
        assert panel.risk_fields['max_buy_lamports'].get() == '0.2'
        panel.refresh_portfolio_value()
        wait(lambda: not panel.busy)
        assert 'Last portfolio estimate' in panel.risk_summary.cget('text')
        panel.tabs.select(1)
        root.update()
        assert panel.risk_summary.winfo_height() >= 60
        screenshot('wallet-limits')
        panel.tabs.select(0)
        approve.return_value = False
        panel.save_setup()
        assert "saved" in panel.status.cget("text")
        screenshot("setup")
        rpc.balance = 3_000_000_000
        panel.refresh_history()
        wait(lambda: not panel.busy and panel.data["balance"] > 0)
        panel.enable()
        wait(lambda: not panel.busy)
        panel.allocate()
        wait(lambda: not panel.busy and len(panel.cash_choices) == 1)
        if not manual_override and not session_mode:
            before = list(journal.db.iterdump())
            panel.tabs.select(7)
            panel.start_worker()
            wait(lambda: bool(panel.worker_history['cycles']))
            assert panel.worker_history['cycles'][0]['report']['results'][0]['status'] == 'ready'
            assert not rpc.sent and list(journal.db.iterdump()) == before
            screenshot('worker-rehearsal')
            panel.stop_worker()
            wait(lambda: not panel.execution_worker.running)
            panel.render_worker()
            assert panel.worker_history['run']['status'] == 'stopped'
            panel.tabs.select(0)
        session_ident = None
        if session_mode:
            for name, value in dict(budget='0.2', minutes='15', orders='5', slippage='100').items():
                panel.session_fields[name].set(value)
            approve.return_value = True
            answer.return_value = 'WRONG'
            panel.authorize_session()
            assert panel.session_id is None and not panel.sessions._leases
            answer.side_effect = lambda title, *args, **kwargs: (
                'SESSION ' + agent[:8] if title == 'Confirm signing session' else 'test wallet password'
            )
            panel.authorize_session()
            wait(lambda: not panel.busy and panel.session_id in panel.session_rows)
            session_ident = panel.session_id
            assert panel.sessions.available(session_ident) and panel.use_session.get() and not rpc.sent
            panel.tabs.select(6)
            screenshot('signing-session')
            panel.tabs.select(0)
            approve.return_value = False
            answer.side_effect = None
        panel.candidates.selection_set("entry:" + MINT)
        before = list(journal.db.iterdump())
        panel.assess()
        wait(lambda: not panel.busy and len(panel.report_rows) == 1)
        assert list(journal.db.iterdump()) == before and not rpc.sent
        assert next(iter(panel.report_rows.values()))["report"]["results"][0]["status"] == "ready"
        panel.prepare()
        wait(lambda: not panel.busy and len(panel.order_rows) == 1)
        ident = next(iter(panel.order_rows))
        panel.orders.selection_set(ident)
        panel.authorize()
        wait(lambda: not panel.busy and "Nothing signed" in panel.status.cget("text"))
        assert not rpc.sent
        panel.cancel()
        wait(lambda: not panel.busy and panel.order_rows[ident]["status"] == "cancelled")
        assert len(panel.cash_choices) == 1
        panel.prepare()
        wait(lambda: not panel.busy and len(panel.order_rows) == 2)
        ident = next(r["id"] for r in panel.order_rows.values() if r["status"] == "review")
        panel.orders.selection_set(ident)
        approve.return_value = True
        answer.return_value = "WRONG"
        panel.authorize()
        wait(lambda: not panel.busy and "Authorization cancelled" in panel.status.cget("text"))
        assert not rpc.sent
        answer.side_effect = lambda title, *args, **kwargs: (
            "SIGN " + panel.orders.selection()[0][:8]
            if title == "Confirm this order"
            else "test wallet password"
        )
        panel.authorize()
        wait(lambda: not panel.busy and len(panel.position_rows) == 1)
        assert len(rpc.sent) == 1
        if session_mode:
            assert panel.session_rows[session_ident]['used_orders'] == 1
            assert not any(call.args[0] == 'Wallet authorization' for call in answer.call_args_list)
        panel.risk_paused.set(True)
        panel.save_risk_limits()
        wait(lambda: not panel.busy and panel.data['wallet_risk']['policy']['entries_paused'])
        assert panel.data['wallet_risk']['entry_blocks']
        position = next(iter(panel.position_rows))
        assert app.position_rail.item("manual:" + position)["values"][1] == "Core"
        if manual_override:
            panel.positions.selection_set(position)
            panel.exit_manually()
            wait(
                lambda: not panel.busy and app.trade_panel is not None and not app.trade_panel.busy
            )
            manual = app.trade_panel
            assert manual.position_id == position and manual.side == "sell" and len(rpc.sent) == 1
            panel.refresh_history()
            wait(lambda: not panel.busy)
            assert not panel.position_rows[position]["managed"]
            assert position not in panel.candidate_rows
            for quantity in ("300", "499"):
                before = len(rpc.sent)
                manual.amount.set(quantity)
                manual.review()
                wait(
                    lambda: not manual.busy
                    and len(rpc.sent) == before + 1
                    and len(manual.order_rows) == before + 1
                )
            assert manual.position_rows[position]["status"] == "closed"
            assert journal.db.execute("SELECT COUNT(*) FROM agent_fills").fetchone()[0] == 3
            assert not app.position_rail.get_children()
            manual.close()
            panel.close()
            app.close()
            next(fixture, None)
            print(
                "PASS: explicit manual-exit handoff, partial/full manual sells, owner attribution and allocation protection"
            )
            return
        library.set_tracking(agent, False)
        observe(library, agent, time.time(), 320)
        panel.refresh_history()
        wait(lambda: not panel.busy)
        panel.candidates.selection_set(position)
        panel.prepare()
        wait(
            lambda: not panel.busy
            and any(r["status"] == "review" for r in panel.order_rows.values())
        )
        panel.orders.selection_set(
            next(r["id"] for r in panel.order_rows.values() if r["status"] == "review")
        )
        panel.authorize()
        wait(lambda: not panel.busy and len(rpc.sent) == 2)
        assert panel.position_rows[position]["quantity"] == "479400000"
        if session_mode:
            assert panel.session_rows[session_ident]['used_orders'] == 2
            panel.revoke_session()
            wait(lambda: not panel.busy and panel.session_rows[session_ident]['status'] == 'revoked')
            assert not panel.sessions.available(session_ident) and not panel.use_session.get()
        if not session_mode:
            panel.start_worker()
            wait(lambda: panel.execution_worker.running)
        panel.close()
        wait(lambda: not panel.window.winfo_exists())
        app.open_agent_trading()
        panel = app.trading_panel
        wait(lambda: not panel.busy and position in panel.position_rows)
        assert panel.allocation.get() == "0.2" and panel.network.get() == "devnet"
        assert panel.execution_worker is None
        if not session_mode:
            panel.render_worker()
            assert panel.worker_history['run']['status'] == 'stopped'
        if session_mode:
            assert not panel.sessions.available(session_ident) and not panel.use_session.get()
        observe(library, agent, time.time(), 170)
        panel.refresh_history()
        wait(lambda: not panel.busy)
        panel.candidates.selection_set(position)
        panel.prepare()
        wait(
            lambda: not panel.busy
            and any(r["status"] == "review" for r in panel.order_rows.values())
        )
        panel.orders.selection_set(
            next(r["id"] for r in panel.order_rows.values() if r["status"] == "review")
        )
        panel.authorize()
        wait(lambda: not panel.busy and panel.position_rows[position]["status"] == "closed")
        assert len(rpc.sent) == 3 and not app.position_rail.get_children()
        screenshot("orders")
        assert not errors
        panel.close()
        app.close()
    next(fixture, None)
    print(
        "PASS: pre-deposit setup, allocation, unsigned assessment, declined/wrong authorization, reviewed buy, paused partial exit, reopen, full exit, ownership sidebar; synthetic funds only"
    )
    if session_mode:
        print('PASS: typed session authorization, reviewed buy/partial exit without per-order password, revoke, reopen locked, explicit password full exit')
    else:
        print('PASS: unsigned worker start, ready assessment, unchanged wallet journal, stop, close while active and reopen without automatic restart')


if __name__ == "__main__":
    import subprocess
    import sys

    # Each lifecycle owns one Tcl interpreter. Repeated destroyed Tk roots in
    # one process can be garbage-collected by a later RPC worker thread.
    if len(sys.argv) == 1:
        for mode in ('guided', 'manual', 'session'):
            subprocess.run([sys.executable, __file__, mode], check=True)
    else:
        main(manual_override=sys.argv[1] == 'manual', session_mode=sys.argv[1] == 'session')
