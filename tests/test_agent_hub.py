from pathlib import Path

import pytest

from pumpwizard_runner.agent_hub import AgentHub, AgentHubError, AgentSettings


def test_agent_profiles_persist_without_storing_an_api_key(tmp_path: Path):
    hub = AgentHub(tmp_path)
    settings = hub.add(AgentSettings(), label="Local research", endpoint="http://127.0.0.1:11434/v1/chat/completions",
                       model="llama3.2", api_key_env="LOCAL_AGENT_KEY")
    hub.save(settings)
    restored = hub.load()

    assert restored.active_agent_id == settings.active_agent_id
    assert restored.agents[0].label == "Local research"
    assert "LOCAL_AGENT_KEY" in hub.path.read_text(encoding="utf-8")
    assert "secret" not in hub.path.read_text(encoding="utf-8")


def test_agent_profile_rejects_unbounded_or_incomplete_endpoints(tmp_path: Path):
    hub = AgentHub(tmp_path)
    with pytest.raises(AgentHubError, match="endpoint"):
        hub.add(AgentSettings(), label="Bad", endpoint="http://example.test/v1/chat/completions", model="x")
    with pytest.raises(AgentHubError, match="endpoint"):
        hub.add(AgentSettings(), label="Bad", endpoint="https://example.test/v1/models", model="x")
    with pytest.raises(AgentHubError, match="endpoint"):
        hub.add(AgentSettings(), label="Impersonated Claude", endpoint="https://example.test/v1/messages", model="x")


def test_official_anthropic_messages_endpoint_is_supported(tmp_path: Path):
    profile = AgentHub(tmp_path).add(
        AgentSettings(), label="Anthropic Claude",
        endpoint="https://api.anthropic.com/v1/messages", model="claude-sonnet-5",
    ).agents[0]
    assert profile.endpoint == "https://api.anthropic.com/v1/messages"


def test_active_connection_can_be_replaced_without_adding_profile_plumbing(tmp_path: Path):
    hub = AgentHub(tmp_path)
    settings = hub.add(AgentSettings(), label='OpenAI', endpoint='https://api.openai.com/v1/chat/completions',
                       model='gpt-5.2')
    updated = hub.replace_active(settings, label='OpenAI', endpoint='https://api.openai.com/v1/chat/completions',
                                 model='gpt-5.6-terra')
    assert len(updated.agents) == 1
    assert updated.agents[0].id == settings.agents[0].id
    assert updated.agents[0].model == 'gpt-5.6-terra'
