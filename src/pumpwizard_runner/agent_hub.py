"""User-configured AI-agent profiles and bounded provider chat."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


SETTINGS_FILENAME = "solanamarket-agent-settings.json"
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_MAX_MESSAGE_CHARS = 8_000


class AgentHubError(RuntimeError):
    """An AI-agent profile or response could not be used safely."""


@dataclass(frozen=True)
class AgentProfile:
    id: str
    label: str
    endpoint: str
    model: str
    api_key_env: str = ""


@dataclass(frozen=True)
class AgentSettings:
    marketplace: str = "pump.fun"
    active_agent_id: str | None = None
    agents: tuple[AgentProfile, ...] = ()


class AgentHub:
    """Local, non-secret preferences for marketplace and AI-agent selection."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / SETTINGS_FILENAME

    def load(self) -> AgentSettings:
        if not self.path.exists():
            return AgentSettings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentHubError("AI-agent settings could not be read") from exc
        if not isinstance(raw, dict) or raw.get("schema") != "solanamarket-wizard.agent-settings/v1":
            raise AgentHubError("AI-agent settings use an unsupported format")
        marketplace = raw.get("marketplace")
        if marketplace != "pump.fun":
            raise AgentHubError("AI-agent settings contain an unsupported marketplace")
        entries = raw.get("agents")
        if not isinstance(entries, list):
            raise AgentHubError("AI-agent settings contain invalid profiles")
        agents = tuple(_profile_from_raw(entry) for entry in entries)
        active = raw.get("active_agent_id")
        if active is not None and active not in {agent.id for agent in agents}:
            raise AgentHubError("AI-agent settings select a missing profile")
        return AgentSettings(marketplace=marketplace, active_agent_id=active, agents=agents)

    def save(self, settings: AgentSettings) -> None:
        if settings.marketplace != "pump.fun":
            raise AgentHubError("Only Pump.fun is available in this build")
        if settings.active_agent_id is not None and settings.active_agent_id not in {agent.id for agent in settings.agents}:
            raise AgentHubError("Active AI agent must be in the local profile list")
        record = {
            "schema": "solanamarket-wizard.agent-settings/v1",
            "marketplace": settings.marketplace,
            "active_agent_id": settings.active_agent_id,
            "agents": [asdict(agent) for agent in settings.agents],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".solanamarket-agent-", suffix=".tmp", dir=self.path.parent)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                json.dump(record, destination, sort_keys=True, separators=(",", ":"))
                destination.write("\n")
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                self.path.chmod(0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def add(self, settings: AgentSettings, *, label: str, endpoint: str, model: str,
            api_key_env: str = "") -> AgentSettings:
        profile = AgentProfile(id=str(uuid.uuid4()), label=label.strip(), endpoint=endpoint.strip(),
                               model=model.strip(), api_key_env=api_key_env.strip())
        _validate_profile(profile)
        if any(agent.label.casefold() == profile.label.casefold() for agent in settings.agents):
            raise AgentHubError("An AI agent with that name already exists")
        return AgentSettings(marketplace=settings.marketplace, active_agent_id=profile.id,
                             agents=(*settings.agents, profile))

    def replace_active(self, settings: AgentSettings, *, label: str, endpoint: str, model: str,
                       api_key_env: str = "") -> AgentSettings:
        """Update the selected connection without changing its credential identity."""
        active = next((agent for agent in settings.agents if agent.id == settings.active_agent_id), None)
        if active is None:
            return self.add(settings, label=label, endpoint=endpoint, model=model, api_key_env=api_key_env)
        profile = AgentProfile(id=active.id, label=label.strip(), endpoint=endpoint.strip(),
                               model=model.strip(), api_key_env=api_key_env.strip())
        _validate_profile(profile)
        if any(agent.id != profile.id and agent.label.casefold() == profile.label.casefold()
               for agent in settings.agents):
            raise AgentHubError("An AI agent with that name already exists")
        return AgentSettings(marketplace=settings.marketplace, active_agent_id=profile.id,
                             agents=tuple(profile if agent.id == profile.id else agent for agent in settings.agents))


def chat(profile: AgentProfile, messages: list[dict[str, str]], workspace_context: str) -> str:
    """Send a bounded user-initiated chat request without wallet signing material."""
    _validate_profile(profile)
    if not messages or any(message.get("role") not in {"user", "assistant"} or
                           not isinstance(message.get("content"), str) for message in messages):
        raise AgentHubError("AI chat history is invalid")
    system = (
        "You are a user-configured research assistant inside SolanaMarket Wizard. "
        "You can discuss the supplied local workspace context, but cannot sign, submit, "
        "or claim to execute a transaction. Never request a seed phrase or private key.\n\n"
        + workspace_context[:_MAX_MESSAGE_CHARS]
    )
    history = [{"role": message["role"], "content": message["content"][:_MAX_MESSAGE_CHARS]}
               for message in messages[-12:]]
    anthropic = _profile_protocol(profile) == "anthropic"
    values = ({"model": profile.model, "system": system, "messages": history, "max_tokens": 1200,
               "temperature": 0.2} if anthropic else
              {"model": profile.model, "messages": [{"role": "system", "content": system}, *history],
               "temperature": 0.2})
    payload = json.dumps(values, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "User-Agent": "SolanaMarketWizard/0.2-agent"}
    if profile.api_key_env:
        token = os.environ.get(profile.api_key_env)
        if not token:
            raise AgentHubError(f"Set {profile.api_key_env} before using this AI agent")
        if anthropic:
            headers["X-Api-Key"] = token
            headers["Anthropic-Version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(profile.endpoint, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            if urllib.parse.urlsplit(response.geturl()).hostname != urllib.parse.urlsplit(profile.endpoint).hostname:
                raise AgentHubError("AI agent redirected to a different host")
            body = response.read(2_000_001)
    except urllib.error.HTTPError as exc:
        raise AgentHubError(f"AI agent returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AgentHubError("AI agent could not be reached") from exc
    if len(body) > 2_000_000:
        raise AgentHubError("AI agent response exceeded the local size limit")
    try:
        response_data = json.loads(body)
        if anthropic:
            content = "\n".join(block["text"] for block in response_data["content"]
                                if isinstance(block, dict) and block.get("type") == "text")
        else:
            content = response_data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise AgentHubError("AI agent returned an unsupported chat response") from exc
    if not isinstance(content, str) or not content.strip():
        raise AgentHubError("AI agent returned an empty chat response")
    return content.strip()


def _profile_from_raw(raw: Any) -> AgentProfile:
    if not isinstance(raw, dict) or set(raw) != {"id", "label", "endpoint", "model", "api_key_env"}:
        raise AgentHubError("AI-agent profile is invalid")
    profile = AgentProfile(**raw)
    _validate_profile(profile)
    return profile


def _validate_profile(profile: AgentProfile) -> None:
    try:
        uuid.UUID(profile.id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise AgentHubError("AI-agent profile identifier is invalid") from exc
    if not 1 <= len(profile.label) <= 60 or not 1 <= len(profile.model) <= 120:
        raise AgentHubError("AI-agent name and model are required")
    parsed = urllib.parse.urlsplit(profile.endpoint)
    local_http = parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS
    remote_https = parsed.scheme == "https" and bool(parsed.hostname)
    protocol = _profile_protocol(profile)
    if (not (local_http or remote_https) or parsed.username or parsed.password or parsed.query or
            parsed.fragment or protocol is None):
        raise AgentHubError(
            "AI endpoint must be local HTTP or HTTPS and end in /chat/completions, "
            "or use Anthropic's official HTTPS /v1/messages endpoint"
        )
    if profile.api_key_env and not _ENV_NAME.fullmatch(profile.api_key_env):
        raise AgentHubError("AI API-key environment variable name is invalid")


def _profile_protocol(profile: AgentProfile) -> str | None:
    """Return the supported wire protocol without trusting a display label."""
    parsed = urllib.parse.urlsplit(profile.endpoint)
    if parsed.path.endswith("/chat/completions"):
        return "chat_completions"
    if (parsed.scheme == "https" and (parsed.hostname or "").casefold() == "api.anthropic.com"
            and parsed.path.rstrip("/") == "/v1/messages"):
        return "anthropic"
    return None
