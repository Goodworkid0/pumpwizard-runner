import json
import time
from dataclasses import replace
from types import SimpleNamespace
import urllib.request

import pytest

from pumpwizard_runner import assistant_chat as chat
from pumpwizard_runner.agent_hub import AgentHub, AgentHubError, AgentSettings
from pumpwizard_runner.ai_keys import AIKeys
from pumpwizard_runner.library import StrategyLibrary


@pytest.fixture
def profile(tmp_path):
    return AgentHub(tmp_path).add(AgentSettings(), label='Research',
        endpoint='https://api.openai.com/v1/chat/completions', model='user-selected-model').agents[0]


def response(monkeypatch, value):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return json.dumps(value).encode()[:limit]

    def send(request, timeout):
        calls.append(request)
        return Response()

    monkeypatch.setattr(chat.urllib.request, 'build_opener', lambda *args: SimpleNamespace(open=send))
    return calls


def proposed(kind='strategy', query='core-frequency', **changes):
    arguments = dict(kind=kind, query=query, limit=3, reason='Inspect exact rules before comparing results')
    arguments.update(changes)
    return dict(role='assistant', content='', tool_calls=[dict(id='call_1', type='function',
        function=dict(name='request_context', arguments=json.dumps(arguments)))])


def test_context_never_calls_provider_until_approval(tmp_path, profile, monkeypatch):
    calls = response(monkeypatch, {'choices': [{'message': {'content': 'Evidence explained'}}],
                                  'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}})
    library = StrategyLibrary(tmp_path)
    try:
        before = list(library._connection.iterdump())
        history = [{'role': 'user', 'content': 'Explain core-frequency'}]
        plan = chat.context_continuation(profile, history, proposed(), library, tmp_path / 'catalog.json')
        assert plan.additional_context and 'core-frequency' in plan.preview and not calls
        with pytest.raises(AgentHubError, match='approval'):
            chat.execute_request(profile, plan, api_key='test-api-credential')
        assert not calls and not plan.consumed
        result = chat.execute_request(profile, plan, api_key='test-api-credential', approved=True)
        assert len(calls) == 1 and result['usage']['total_tokens'] == 120
        payload = json.loads(calls[0].data)
        assert payload['messages'][-1]['role'] == 'tool'
        assert payload['messages'][-1]['tool_call_id'] == 'call_1'
        assert 'test-api-credential' not in calls[0].data.decode()
        assert calls[0].get_header('Authorization') == 'Bearer test-api-credential'
        assert list(library._connection.iterdump()) == before
        with pytest.raises(AgentHubError, match='already sent'):
            chat.execute_request(profile, plan, approved=True)
        assert len(calls) == 1
    finally:
        library.close()


@pytest.mark.parametrize('arguments', [
    dict(kind='shell', query='whoami', limit=1, reason='x'),
    dict(kind='coin_history', query='', limit=1, reason='x'),
    dict(kind='strategy', query='core', limit=1000, reason='x'),
    dict(kind='strategy', query='core', limit=True, reason='x'),
    dict(kind='strategy', query='core', limit=1, reason='x', path='/etc/passwd'),
])
def test_retrieval_rejects_arbitrary_capabilities(tmp_path, arguments):
    library = StrategyLibrary(tmp_path)
    try:
        with pytest.raises(AgentHubError):
            chat.local_context(library, tmp_path / 'catalog.json', arguments)
    finally:
        library.close()


def test_coin_history_is_scoped_bounded_and_timestamped(tmp_path):
    library = StrategyLibrary(tmp_path)
    try:
        with library._connection:
            for i in range(6):
                library._connection.execute('''INSERT INTO market_watch_samples
                    (mint,symbol,observed_at,price_usd,market_cap_usd,liquidity_usd) VALUES (?,?,?,?,?,?)''',
                    ('mint-A', 'COIN', 100+i, '0.1', '10000', '1000'))
            library._connection.execute('''INSERT INTO market_watch_samples
                    (mint,symbol,observed_at,price_usd,market_cap_usd,liquidity_usd) VALUES (?,?,?,?,?,?)''',
                    ('mint-B', 'OTHER', 100, '0.2', '20000', '2000'))
        args = dict(kind='coin_history', query='mint-A', limit=3, reason='Review history')
        result = chat.local_context(library, tmp_path / 'catalog.json', args)
        assert [r['observed_at'] for r in result['rows']] == [105, 104, 103]
        assert all(r['mint'] == 'mint-A' for r in result['rows'])
        result = chat.local_context(library, tmp_path / 'catalog.json', dict(args, query="' OR 1=1 --"))
        assert result['rows'] == []
    finally:
        library.close()


def test_long_history_requires_approval_and_has_output_cap(profile, monkeypatch):
    calls = response(monkeypatch, {'choices': [{'message': {'content': 'Answer'}}]})
    plan = chat.prepare_request(profile, [{'role': 'user', 'content': 'a'*12000}], max_output=512)
    assert plan.estimated_input > 3000
    with pytest.raises(AgentHubError, match='approval'):
        chat.execute_request(profile, plan)
    assert not calls
    assert json.loads(plan.payload)['max_completion_tokens'] == 512
    local = replace(profile, endpoint='http://127.0.0.1:11434/v1/chat/completions')
    assert json.loads(chat.prepare_request(local, [{'role': 'user', 'content': 'Hi'}], max_output=512).payload)['max_tokens'] == 512


def test_expiry_and_profile_change_reject_pending_approval(profile, monkeypatch):
    calls = response(monkeypatch, {})
    plan = chat.prepare_request(profile, [{'role': 'user', 'content': 'Hi'}])
    with pytest.raises(AgentHubError, match='profile changed'):
        chat.execute_request(replace(profile, endpoint='https://other.test/v1/chat/completions'), plan, approved=True)
    plan.created_at = time.time() - 301
    with pytest.raises(AgentHubError, match='expired'):
        chat.execute_request(profile, plan, approved=True)
    assert not calls


def test_redirect_rejected_before_key_forwarding():
    request = urllib.request.Request('https://provider.test/v1/chat/completions', headers={'Authorization': 'Bearer test'})
    with pytest.raises(AgentHubError, match='redirected'):
        chat.NoRedirect().redirect_request(request, None, 307, 'redirect', {}, 'https://elsewhere.test')


def test_key_echo_redacted_and_usage_not_invented(profile, monkeypatch):
    response(monkeypatch, {'choices': [{'message': {'content': 'echo test-api-secret'}}]})
    result = chat.execute_request(profile, chat.prepare_request(profile, [{'role': 'user', 'content': 'Hi'}]), api_key='test-api-secret')
    assert 'test-api-secret' not in json.dumps(result) and result['usage'] == {}


def test_conversations_restore_and_isolate_profiles(tmp_path):
    store = chat.ChatStore(tmp_path)
    first, other = store.create('profile-one'), store.create('profile-two')
    store.add(first, 'user', 'Hello')
    store.add(first, 'notice', 'Approved read: private snippet')
    store.add(first, 'assistant', 'Research answer', {'usage': {'total_tokens': 42}})
    store.add(other, 'user', 'Other provider history')
    store.close()
    store = chat.ChatStore(tmp_path)
    try:
        assert len(store.conversations('profile-one')) == 1
        assert store.history(first) == [{'role': 'user', 'content': 'Hello'}, {'role': 'assistant', 'content': 'Research answer'}]
        assert store.messages(first)[2]['metadata']['usage']['total_tokens'] == 42
        store.delete(first)
        assert not store.messages(first) and store.messages(other)
    finally:
        store.close()


def test_keys_encrypted_endpoint_bound_and_locked_on_restart(tmp_path, profile):
    keys = AIKeys(tmp_path)
    keys.set(profile, 'private-test-api-key', 'separate AI password')
    assert 'private-test-api-key' not in keys.path(profile).read_text()
    restored = AIKeys(tmp_path)
    assert not restored.available(profile)
    assert not keys.available(replace(profile, endpoint='https://different.test/v1/chat/completions'))
    with pytest.raises(AgentHubError):
        restored.unlock(profile, 'wrong password value')
    assert restored.unlock(profile, 'separate AI password') == 'private-test-api-key'
    with pytest.raises(AgentHubError):
        restored.unlock(replace(profile, endpoint='https://different.test/v1/chat/completions'), 'separate AI password')
    restored.set(profile, 'new-session-only-key')
    assert not restored.exists(profile)
    restored.lock()
    assert not restored.get(profile)


def test_multibyte_history_keeps_latest_question_within_byte_budget(tmp_path):
    store = chat.ChatStore(tmp_path)
    try:
        conversation = store.create('profile')
        store.add(conversation, 'user', 'old question')
        store.add(conversation, 'user', '🪙' * 8000)
        history = store.history(conversation)
        assert history and history[-1]['content'].startswith('🪙')
        assert sum(len(row['content'].encode()) for row in history) <= 24000
    finally:
        store.close()


def test_uncertain_transport_failure_is_consumed_without_retry(profile, monkeypatch):
    calls = []

    def fail(request, timeout):
        calls.append(request)
        raise TimeoutError('transport details must not be exposed')

    monkeypatch.setattr(chat.urllib.request, 'build_opener', lambda *_: SimpleNamespace(open=fail))
    plan = chat.prepare_request(profile, [{'role': 'user', 'content': 'Hello'}])
    with pytest.raises(AgentHubError, match='may have been billed'):
        chat.execute_request(profile, plan)
    with pytest.raises(AgentHubError, match='already sent'):
        chat.execute_request(profile, plan)
    assert len(calls) == 1


def test_pending_data_is_frozen_until_explicit_send(tmp_path, profile, monkeypatch):
    calls = response(monkeypatch, {'choices': [{'message': {'content': 'No matching retained data'}}]})
    library = StrategyLibrary(tmp_path)
    try:
        plan = chat.context_continuation(profile, [{'role': 'user', 'content': 'Inspect TEST'}],
            proposed('coin_history', 'TEST', limit=1), library, tmp_path / 'catalog.json')
        payload = plan.payload
        with library._connection:
            library._connection.execute('''INSERT INTO market_watch_samples
                (mint,symbol,observed_at,price_usd,market_cap_usd,liquidity_usd) VALUES (?,?,?,?,?,?)''',
                ('mint-A', 'TEST', 999, '0.1', '10000', '1000'))
        chat.execute_request(profile, plan, approved=True)
        assert calls[0].data == payload
        assert json.loads(json.loads(payload)['messages'][-1]['content'])['rows'] == []
    finally:
        library.close()


def test_anthropic_messages_transport_and_tool_round_trip(tmp_path, profile, monkeypatch):
    claude = replace(profile, label='Anthropic Claude', endpoint='https://api.anthropic.com/v1/messages',
                     model='claude-sonnet-5')
    calls = response(monkeypatch, {
        'content': [
            {'type': 'text', 'text': 'I need the exact strategy.'},
            {'type': 'tool_use', 'id': 'toolu_1', 'name': 'request_context', 'input': {
                'kind': 'strategy', 'query': 'core-frequency', 'limit': 1,
                'reason': 'Inspect exact rules before comparing results',
            }},
        ],
        'usage': {'input_tokens': 90, 'output_tokens': 30},
    })
    history = [{'role': 'user', 'content': 'Explain core-frequency'}]
    plan = chat.prepare_request(claude, history, max_output=512)
    payload = json.loads(plan.payload)
    assert payload['system'] == chat.SYSTEM
    assert payload['messages'] == history
    assert payload['max_tokens'] == 512
    assert payload['tools'][0]['input_schema'] == chat.TOOL['function']['parameters']
    assert payload['tool_choice'] == {'type': 'auto', 'disable_parallel_tool_use': True}

    result = chat.execute_request(claude, plan, api_key='anthropic-test-key')
    request = calls[0]
    assert request.get_header('X-api-key') == 'anthropic-test-key'
    assert request.get_header('Anthropic-version') == '2023-06-01'
    assert request.get_header('Authorization') is None
    assert result['message']['content'] == 'I need the exact strategy.'
    assert result['message']['tool_calls'][0]['function']['name'] == 'request_context'
    assert json.loads(result['message']['tool_calls'][0]['function']['arguments'])['query'] == 'core-frequency'
    assert result['usage'] == {'prompt_tokens': 90, 'completion_tokens': 30, 'total_tokens': 120}

    library = StrategyLibrary(tmp_path)
    try:
        continuation = chat.context_continuation(
            claude, history, result['message'], library, tmp_path / 'catalog.json', max_output=512,
        )
        followup = json.loads(continuation.payload)
        assert followup['messages'][-2]['content'][-1] == {
            'type': 'tool_use', 'id': 'toolu_1', 'name': 'request_context',
            'input': {
                'kind': 'strategy', 'query': 'core-frequency', 'limit': 1,
                'reason': 'Inspect exact rules before comparing results',
            },
        }
        assert followup['messages'][-1]['role'] == 'user'
        assert followup['messages'][-1]['content'][0]['type'] == 'tool_result'
        assert followup['messages'][-1]['content'][0]['tool_use_id'] == 'toolu_1'
    finally:
        library.close()


def test_anthropic_response_rejects_empty_or_malformed_blocks(profile, monkeypatch):
    claude = replace(profile, endpoint='https://api.anthropic.com/v1/messages', model='claude-sonnet-5')
    response(monkeypatch, {'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'request_context', 'input': 'bad'}]})
    with pytest.raises(AgentHubError, match='unsupported response'):
        chat.execute_request(claude, chat.prepare_request(claude, [{'role': 'user', 'content': 'Hi'}]), api_key='test')


def test_deepseek_uses_chat_completions_with_non_thinking_tool_flow(profile):
    deepseek = replace(profile, label='DeepSeek', endpoint='https://api.deepseek.com/chat/completions',
                       model='deepseek-flash')
    payload = json.loads(chat.prepare_request(
        deepseek, [{'role': 'user', 'content': 'Compare these rules'}], max_output=512,
    ).payload)
    assert payload['model'] == 'deepseek-flash'
    assert payload['max_tokens'] == 512
    assert payload['thinking'] == {'type': 'disabled'}
    assert payload['tools'] == [chat.TOOL]
