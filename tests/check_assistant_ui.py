"""Offline full-desktop chat acceptance using a synthetic provider and no wallet."""
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
from unittest.mock import patch

from pumpwizard_runner import assistant_chat as chat, __version__
from pumpwizard_runner.release_notes import note_url
from pumpwizard_runner.agent_hub import AgentHub, AgentSettings
from pumpwizard_runner.ai_keys import AIKeys
from pumpwizard_runner.desktop import RunnerWindow


def main():
    with tempfile.TemporaryDirectory(prefix='wizard-assistant-ui-') as temporary:
        directory = Path(temporary)
        hub = AgentHub(directory)
        settings = hub.add(AgentSettings(), label='Research provider',
                          endpoint='https://provider.invalid/v1/chat/completions', model='configured-model')
        hub.save(settings)
        keys = AIKeys(directory)
        keys.set(settings.agents[0], 'synthetic-test-key', 'test AI password value')
        requests, errors = [], []
        proposal = {'content': 'I can inspect the local core-frequency rules. Please review the data request below.',
                    'tool_calls': [{'id': 'read_1', 'type': 'function', 'function': {
                        'name': 'request_context', 'arguments': json.dumps(dict(kind='strategy',
                        query='core-frequency', limit=1, reason='Check the exact local configuration and evidence status.'))}}]}
        replies = [proposal, proposal, {'content': 'These rules are eligible for reviewed execution only after local setup. Funded acceptance remains pending.'}]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, _limit):
                return json.dumps({'choices': [{'message': replies.pop(0)}],
                                   'usage': {'prompt_tokens': 400, 'completion_tokens': 50, 'total_tokens': 450}}).encode()

        def send(request, timeout):
            requests.append(request)
            return Response()

        with (
            patch.object(socket.socket, 'connect', side_effect=AssertionError('Unexpected live connection')),
            patch.object(RunnerWindow, '_unlock_local_wallet', return_value=SimpleNamespace(address='11111111111111111111111111111111')),
            patch('pumpwizard_runner.desktop.webbrowser.open_new_tab', return_value=True) as browser,
            patch.object(RunnerWindow, 'refresh_market_watch'),
            patch.object(RunnerWindow, 'sync_wallet_balance'),
            patch.object(RunnerWindow, 'recover_manual_trades'),
            patch('pumpwizard_runner.desktop.MonitorScheduler.start'),
            patch.object(chat.urllib.request, 'build_opener', return_value=SimpleNamespace(open=send)),
            patch('pumpwizard_runner.assistant_panel.simpledialog.askstring', return_value='test AI password value'),
        ):
            root = tk.Tk()
            root.report_callback_exception = lambda *args: errors.append(args)
            app = RunnerWindow(root, directory)
            panel = app.assistant

            def wait(predicate):
                deadline = time.monotonic() + 10
                while not predicate():
                    root.update()
                    assert not errors, errors
                    assert time.monotonic() < deadline, panel.status.cget('text')
                    time.sleep(.02)
                root.update()
                assert not errors, errors

            def capture(name):
                if os.environ.get('WIZARD_UI_SCREENSHOTS'):
                    from PIL import ImageGrab
                    root.update()
                    ImageGrab.grab().save('/tmp/pumpwizard-dev45-' + name + '.png')

            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)

            try:
                root.update()
                assert app.nav_buttons['workspace'].cget('text') == 'Market'
                assert app.agent_tree.heading('agent')['text'] == 'SESSION'
                assert app.version_badge.cget('text').startswith('v' + __version__)
                assert root.title().startswith('PumpWizard Runner')
                browser.assert_not_called()
                app.version_badge.invoke()
                browser.assert_called_once_with(note_url(__version__))
                plus = next(w for w in descendants(app.sidebar) if w.winfo_class() == 'TButton' and w.cget('text') == '+')
                plus.invoke()
                root.update()
                chooser = app.strategy_chooser
                next(w for w in descendants(chooser) if w.winfo_class() == 'TButton' and w.cget('text') == 'Create strategy').invoke()
                root.update()
                creator = app.strategy_creator
                assert creator.title().startswith('Create strategy')
                next(w for w in descendants(creator) if w.winfo_class() == 'TButton' and w.cget('text') == 'Cancel').invoke()
                before_choice = list(app.library._connection.iterdump())
                app.nav_buttons['strategies'].invoke()
                root.update()
                chooser = app.strategy_chooser
                assert chooser.grab_current() == chooser
                capture('strategy-choice')
                next(w for w in descendants(chooser) if w.winfo_class() == 'TButton' and w.cget('text') == 'Cancel').invoke()
                assert list(app.library._connection.iterdump()) == before_choice
                app.open_strategies()
                chooser = app.strategy_chooser
                app.open_strategies()
                assert app.strategy_chooser is chooser
                next(w for w in descendants(chooser) if w.winfo_class() == 'TButton' and w.cget('text') == 'Browse marketplace').invoke()
                root.update()
                assert app.active_section == 'strategies' and root.grab_current() is None
                capture('strategies')
                from pumpwizard_runner.portfolio import CONFIG
                package = directory / 'import.json'
                package.write_text(json.dumps({'schema': 'pumpwizard.strategy-handoff/v1',
                    'strategy': {'id': 'core-frequency', 'label': 'Imported core'}, 'configuration': CONFIG}))
                with patch('pumpwizard_runner.desktop.filedialog.askopenfilename', return_value=str(package)):
                    app.open_strategies()
                    next(w for w in descendants(app.strategy_chooser) if w.winfo_class() == 'TButton' and w.cget('text') == 'Choose config file').invoke()
                root.update()
                assert len(app.library.rows()) == 1 and not app.library.instances()
                imported = next(w for w in descendants(root) if isinstance(w, tk.Toplevel) and w.title().startswith('Imported configuration'))
                imported.destroy()
                app.nav_buttons['workspace'].invoke()
                root.update()
                assert app.welcome.winfo_ismapped() and app.selected_agent_id is None
                app.show_section('strategies')
                root.update()
                assert 'assistant' not in app.nav_buttons
                assert panel.master != app.workspace
                assert panel.winfo_ismapped() and app.strategy_browser.winfo_ismapped()
                before = list(app.library._connection.iterdump())
                panel.input.insert('1.0', 'Explain core-frequency and its evidence status.')
                panel.send()
                panel.send()  # Busy guard prevents duplicate billing.
                wait(lambda: not panel.busy and panel.pending)
                assert len(requests) == 1
                assert str(panel.send_button.cget('state')) == 'disabled'
                capture('assistant')
                panel.review()
                root.update()
                capture('permission')
                for child in list(descendants(root)):
                    if isinstance(child, tk.Toplevel):
                        child.destroy()
                panel.decline()
                assert len(requests) == 1 and not panel.pending
                panel.input.insert('1.0', 'Please propose that read again.')
                panel.send()
                wait(lambda: not panel.busy and panel.pending)
                assert len(requests) == 2
                panel.review()
                root.update()
                approve = next(w for w in descendants(root) if w.winfo_class() == 'TButton' and w.cget('text') == 'Approve & send once')
                assert panel.get_profile() == panel.pending[0], (panel.get_profile(), panel.pending[0])
                assert panel.conversation == panel.pending[3], (panel.conversation, panel.pending[3])
                approve.invoke()
                wait(lambda: not panel.busy)
                assert len(requests) == 3 and not panel.pending, (len(requests), panel.status.cget('text'), bool(panel.pending))
                sent = json.loads(requests[-1].data)
                assert sent['messages'][-1]['role'] == 'tool'
                assert 'core-frequency' in sent['messages'][-1]['content']
                assert 'Funded acceptance remains pending' in panel.transcript.get('1.0', 'end')
                assert list(app.library._connection.iterdump()) == before
                assert not list(directory.glob('*wallet*'))
                conversation = panel.conversation
                reopened = chat.ChatStore(directory)
                assert reopened.messages(conversation)[-1]['metadata']['usage']['total_tokens'] == 450
                reopened.close()
                root.update()
                assert panel.choice.winfo_ismapped()
                panel.new()
                assert panel.conversation != conversation
                panel.choice.set(next(k for k,v in panel.choices.items() if v == conversation))
                panel.select()
                assert panel.conversation == conversation
                for section in ('workspace', 'settings', 'strategies'):
                    app.show_section(section)
                    root.update()
                    assert panel.winfo_ismapped() and panel.conversation == conversation
                # Ensure essential chat controls remain on screen at the supported minimum.
                root.geometry('1100x680')
                root.update()
                for widget in (panel.send_button, panel.status):
                    assert widget.winfo_rootx() + widget.winfo_width() <= root.winfo_rootx() + root.winfo_width()
                    assert widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + root.winfo_height()
                assert panel.transcript.dlineinfo('end-1c') is not None
                capture('assistant-small')
                app.open_ai_settings()
                root.update()
                form = next(w for w in descendants(root) if isinstance(w, tk.Toplevel) and w.title() == 'Connect Strategy Assistant')
                assert [app.settings_tabs.tab(tab, 'text') for tab in app.settings_tabs.tabs()] == ['Marketplace']
                choices = {int(info['row']): w for w in descendants(form) if w.winfo_class() == 'TCombobox'
                           for info in (w.grid_info(),) if 'row' in info}
                provider = choices[2]
                assert 'Anthropic Claude' in provider.cget('values')
                assert 'DeepSeek API' in provider.cget('values')
                provider.set('Anthropic Claude')
                provider.event_generate('<<ComboboxSelected>>')
                assert 'claude-sonnet-5' in choices[3].cget('values')
                provider.set('DeepSeek API')
                provider.event_generate('<<ComboboxSelected>>')
                assert 'deepseek-flash' in choices[3].cget('values')
                provider.set('OpenAI API')
                provider.event_generate('<<ComboboxSelected>>')
                entries = {int(info['row']): w for w in descendants(form) if w.winfo_class() == 'TEntry'
                           for info in (w.grid_info(),) if 'row' in info}
                assert entries[5].cget('show') == '•'
                assert 'gpt-5.6-terra' in choices[3].cget('values')
                choices[3].set('gpt-5.2')
                entries[5].insert(0, 'synthetic-second-key')
                next(w for w in descendants(form) if w.winfo_class() == 'TButton' and w.cget('text') == 'Save connection').invoke()
                root.update()
                assert len(requests) == 3 and len(app.agent_settings.agents) == 1
                assert app.ai_keys.available(app.active_ai_profile())
                assert 'synthetic-second-key' not in hub.path.read_text()
                assert panel.profile_id == app.active_ai_profile().id and panel.conversation is None
                print('PASS: desktop chat, encrypted-key unlock, bounded tool approval/decline, duplicate guard, history restore, minimum-size layout; no real provider or wallet')
            finally:
                app.close()


if __name__ == '__main__':
    main()
