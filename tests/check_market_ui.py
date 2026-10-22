"""Offline session/market/coin acceptance, with a fake AI provider and no wallet."""
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
from unittest.mock import patch

from pumpwizard_runner import assistant_chat as chat
from pumpwizard_runner.agent_hub import AgentHub, AgentSettings
from pumpwizard_runner.desktop import RunnerWindow
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.strategies import parse_package


def children(widget):
    for child in widget.winfo_children():
        yield child
        yield from children(child)


def main():
    with tempfile.TemporaryDirectory(prefix='wizard-market-ui-') as temporary:
        directory = Path(temporary)
        library = StrategyLibrary(directory)
        strategy = parse_package({'schema': 'pumpwizard.strategy-handoff/v1', 'strategy': {'id': 'core-frequency', 'label': 'Core'}, 'configuration': CONFIG})
        library.import_strategy(strategy)
        active, paused, blocked = [library.create_instance(strategy.digest, label=label) for label in ('Core monitoring', 'Paused research', 'Needs setup')]
        library.set_tracking(active, True)
        library.set_tracking(paused, False)
        library.set_tracking(blocked, True)
        now = time.time()
        with library._connection:
            library._connection.execute('UPDATE monitor_settings SET fault=? WHERE instance_id=?', ('Invalid configuration; inspect imported rules', blocked))
            library._connection.execute('INSERT INTO monitor_runs(instance_id,started_at,finished_at,status,observations,decisions,detail) VALUES (?,?,?,?,?,?,?)', (active, now-20, now-10, 'completed', 3, 1, ''))
            for i in range(125):
                library._connection.execute('INSERT INTO market_watch_samples(mint,symbol,observed_at,created_at,price_usd,market_cap_usd,liquidity_usd) VALUES (?,?,?,?,?,?,?)',
                    ('mint-one', 'ALPHA', now-i*15, now-3600, str(.00000123+i*.00000001), '12500', '4200'))
            for mint, symbol, age in [('mint-two', 'ALPHA', 700), ('mint-three', 'BETA', 5)]:
                library._connection.execute('INSERT INTO market_watch_samples(mint,symbol,observed_at,price_usd,market_cap_usd,liquidity_usd) VALUES (?,?,?,?,?,?)',
                    (mint, symbol, now-age, '.0003', '25000', '3000'))
        library.record_decisions(active, [dict(at=now-10, mint='mint-one', kind='entry_blocked', reason='Recorded signal awaits reviewed setup')])
        library.close()
        hub = AgentHub(directory)
        hub.save(hub.add(AgentSettings(), label='Test AI', endpoint='https://provider.invalid/v1/chat/completions', model='synthetic-model'))
        requests, errors = [], []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, _limit):
                return json.dumps({'choices': [{'message': {'content': 'This is a bounded local sample, not complete coin history.'}}]}).encode()

        def send(request, timeout):
            requests.append(request)
            return Response()

        with (
            patch.object(socket.socket, 'connect', side_effect=AssertionError('Unexpected network')),
            patch.object(RunnerWindow, '_unlock_local_wallet', return_value=SimpleNamespace(address='11111111111111111111111111111111')),
            patch.object(RunnerWindow, 'refresh_market_watch'),
            patch.object(RunnerWindow, 'sync_wallet_balance'),
            patch.object(RunnerWindow, 'recover_manual_trades'),
            patch('pumpwizard_runner.desktop.MonitorScheduler.start'),
            patch.object(chat.urllib.request, 'build_opener', return_value=SimpleNamespace(open=send)),
        ):
            root = tk.Tk()
            root.report_callback_exception = lambda *args: errors.append(args)
            app = RunnerWindow(root, directory)

            def update():
                root.update()
                assert not errors, errors

            def capture(name, window=root):
                if os.environ.get('WIZARD_UI_SCREENSHOTS'):
                    from PIL import ImageGrab
                    window.lift()
                    update()
                    ImageGrab.grab().save('/tmp/pumpwizard-dev46-' + name + '.png')

            try:
                update()
                before = list(app.library._connection.iterdump())
                assert app.agent_tree.item(active)['values'][1] == 'Monitoring'
                assert app.agent_tree.item(paused)['values'][1] == 'Paused'
                assert app.agent_tree.item(blocked)['values'][1] == 'Blocked'
                app._show_agent(blocked)
                update()
                assert 'Invalid configuration' in app.tracking_status.cget('text')
                app.recovery_button.invoke()
                assert app.notebook.index(app.notebook.select()) == 3
                capture('session-blocked')
                app._show_agent(active)
                assert 'entry blocked' in app.tracking_status.cget('text')
                app.recovery_button.invoke()
                assert app.notebook.select() == str(app.activity_tab)
                app._show_welcome()
                update()
                assert len(app.market_watch_tree.get_children()) == 3
                app.market_watch_tree.selection_set('mint-one')
                app._render_market_watch_rows(app.library.market_watch_rows())
                assert app.market_watch_tree.selection() == ('mint-one',)
                assert '$0.000001230' in str(app.market_watch_tree.item('mint-one')['values'])
                app.market_fresh_only.set(True)
                app._on_market_filter_changed()
                assert 'mint-two' not in app.market_watch_tree.get_children()
                app.market_query.set('missing')
                update()
                assert app.market_empty.winfo_ismapped()
                assert str(app.coin_details_button.cget('state')) == 'disabled'
                app.reset_market_filters()
                app._market_watch_result = (False, 'Synthetic provider outage')
                app._await_market_watch()
                assert 'cached samples retained' in app.market_watch_status.cget('text').lower()
                assert len(app.market_watch_tree.get_children()) == 3
                capture('market')
                app.market_watch_tree.selection_set('mint-one')
                app.open_coin_details()
                coin = app.coin_panel
                update()
                assert len(coin.samples) == 100 and all(r['mint'] == 'mint-one' for r in coin.samples)
                assert 'latest 100' in coin.coverage.cget('text')
                assert coin.events[0]['instance_id'] == active
                capture('coin-details', coin.window)
                coin.activity.selection_set('0')
                coin.open_selected_session()
                assert coin.closed and app.selected_agent_id == active
                app._show_welcome()
                app.market_watch_tree.selection_set('mint-one')
                app.open_coin_details()
                coin = app.coin_panel
                app.assistant.input.insert('1.0', 'Keep my draft')
                coin.ask_assistant()
                assert not app.assistant.pending and not requests and not coin.closed
                assert app.assistant.input.get('1.0', 'end-1c') == 'Keep my draft'
                app.assistant.input.delete('1.0', 'end')
                coin.ask_assistant()
                update()
                assert coin.closed and app.assistant.pending and not requests
                first = app.assistant.pending[1]
                assert first.additional_context and 'mint-one' in first.preview and 'mint-two' not in first.preview
                assert first.payload.count(b'coin_history') > 0
                # Declining must not spend tokens, and stale review buttons cannot approve another request.
                app.assistant.decline()
                for child in list(children(root)):
                    if isinstance(child, tk.Toplevel):
                        child.destroy()
                assert not requests
                assert app.ask_about_coin('mint-one', 'ALPHA')
                update()
                approve = next(w for w in children(root) if w.winfo_class() == 'TButton' and w.cget('text') == 'Approve & send once')
                approve.invoke()
                deadline = time.monotonic()+5
                while app.assistant.busy:
                    update()
                    assert time.monotonic() < deadline
                    time.sleep(.02)
                assert len(requests) == 1
                payload = json.loads(requests[0].data)
                assert 'mint-one' in payload['messages'][-1]['content'] and 'mint-two' not in payload['messages'][-1]['content']
                assert list(app.library._connection.iterdump()) == before
                root.geometry('1100x680')
                update()
                for widget in (app.coin_details_button, app.market_refresh_button, app.assistant.send_button):
                    assert widget.winfo_rootx()+widget.winfo_width() <= root.winfo_rootx()+root.winfo_width()
                    assert widget.winfo_rooty()+widget.winfo_height() <= root.winfo_rooty()+root.winfo_height()
                capture('market-small')
                app.scheduler.events.put((None, 'Scheduler stopped: synthetic fault'))
                root.after_cancel(app._scheduler_after)
                app._poll_scheduler()
                app.update_session_status(active)
                assert 'Restart the app' in app.tracking_status.cget('text')
                assert 'Scheduler stopped' in app.session_summary.cget('text')
                app.scheduler.events.put((active, 'recovered'))
                root.after_cancel(app._scheduler_after)
                app._poll_scheduler()
                assert not app._scheduler_warning
                print('PASS: session states/recovery, market filters/freshness/selection/cache, bounded coin history/activity and approved-only AI handoff; unchanged library, no real provider or wallet')
            finally:
                app.close()


if __name__ == '__main__':
    main()
