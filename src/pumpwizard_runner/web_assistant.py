"""Browser adapter for the local, consented Strategy Assistant.

Provider credentials are accepted only by the loopback API and remain in
``AIKeys``. The browser receives public profile metadata, locally stored chat
history, and a frozen request preview. Every provider request needs a separate
approval; tool-requested strategy or coin context becomes another preview.
"""
from __future__ import annotations

import json
from pathlib import Path
import threading
import uuid

from .agent_hub import AgentHub, AgentHubError
from .ai_keys import AIKeys
from .assistant_chat import (
    ChatStore,
    context_continuation,
    execute_request,
    prepare_evidence_request,
    prepare_request,
)
from .evidence_assistant import (
    EvidenceAssistant,
    explain_context,
    validate_ai_candidate,
    validate_selector,
)
from .library import StrategyLibrary


_PROVIDERS = {
    "openai": ("OpenAI", "https://api.openai.com/v1/chat/completions"),
    "anthropic": ("Anthropic Claude", "https://api.anthropic.com/v1/messages"),
    "deepseek": ("DeepSeek", "https://api.deepseek.com/chat/completions"),
    "ollama": ("Local Ollama", "http://127.0.0.1:11434/v1/chat/completions"),
    "compatible": ("Compatible API", None),
}


class BrowserAssistant:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.hub = AgentHub(self.data_dir)
        self.keys = AIKeys(self.data_dir)
        self.pending: dict[str, tuple[object, object, list[dict[str, str]], str]] = {}
        self.pending_evidence: dict[str, dict] = {}
        self.lock = threading.RLock()

    def _active(self):
        settings = self.hub.load()
        return settings, next((item for item in settings.agents if item.id == settings.active_agent_id), None)

    def _conversation(self, store: ChatStore, profile_id: str, conversation: object, scope: str = "") -> str:
        if conversation is None:
            return store.create(profile_id, scope)
        if not isinstance(conversation, str) or conversation not in {row["id"] for row in store.conversations(profile_id, scope)}:
            raise AgentHubError("Unknown local conversation")
        return conversation

    def configure(self, values: object) -> dict:
        if not isinstance(values, dict) or set(values) != {"provider", "model", "api_key", "endpoint"}:
            raise AgentHubError("Assistant setup requires provider, model, API key, and endpoint fields")
        provider, model, api_key, endpoint = (values[name] for name in ("provider", "model", "api_key", "endpoint"))
        if provider not in _PROVIDERS or not all(isinstance(value, str) for value in (model, api_key, endpoint)):
            raise AgentHubError("Assistant setup fields are invalid")
        label, fixed_endpoint = _PROVIDERS[provider]
        selected_endpoint = fixed_endpoint or endpoint.strip()
        with self.lock:
            settings, previous = self._active()
            same_connection = previous is not None and previous.endpoint == selected_endpoint
            if (provider != "ollama" and not api_key.strip()
                    and not (same_connection and self.keys.available(previous))):
                raise AgentHubError("Enter an API key for this provider")
            updated = self.hub.replace_active(settings, label=label, endpoint=selected_endpoint, model=model, api_key_env="")
            profile = next(item for item in updated.agents if item.id == updated.active_agent_id)
            if api_key.strip():
                self.keys.set(profile, api_key, password=None)
            elif previous is None or not self.keys.available(previous):
                raise AgentHubError("Enter an API key or configure Local Ollama without one")
            self.hub.save(updated)
            return self._public(profile)

    def _public(self, profile) -> dict:
        return {"id": profile.id, "label": profile.label, "endpoint": profile.endpoint, "model": profile.model,
                "key_available": self.keys.available(profile), "key_persisted": self.keys.exists(profile)}

    def overview(self, scope: str = "") -> dict:
        with self.lock:
            _settings, profile = self._active()
            if profile is None:
                return {"configured": False, "profile": None, "conversations": []}
            store = ChatStore(self.data_dir)
            try:
                conversations = store.conversations(profile.id, scope)
            finally:
                store.close()
            return {"configured": True, "profile": self._public(profile), "conversations": conversations}

    def messages(self, conversation: str, scope: str = "") -> dict:
        with self.lock:
            _settings, profile = self._active()
            if profile is None:
                raise AgentHubError("Configure the Strategy Assistant first")
            store = ChatStore(self.data_dir)
            try:
                self._conversation(store, profile.id, conversation, scope)
                return {"conversation": conversation, "messages": store.messages(conversation)}
            finally:
                store.close()

    def create(self, scope: str = "") -> dict:
        with self.lock:
            _settings, profile = self._active()
            if profile is None:
                raise AgentHubError("Configure the Strategy Assistant first")
            store = ChatStore(self.data_dir)
            try:
                conversation = store.create(profile.id, scope)
                return {"conversation": conversation}
            finally:
                store.close()

    def delete(self, conversation: str, scope: str = "") -> dict:
        with self.lock:
            _settings, profile = self._active()
            if profile is None:
                raise AgentHubError("Configure the Strategy Assistant first")
            store = ChatStore(self.data_dir)
            try:
                self._conversation(store, profile.id, conversation, scope)
                store.delete(conversation)
            finally:
                store.close()
            return {"deleted": conversation}

    def prepare(self, values: object, scope: str = "") -> dict:
        if not isinstance(values, dict) or set(values) != {"conversation", "message", "max_output"}:
            raise AgentHubError("Assistant message requires conversation, message, and output cap")
        message, max_output = values["message"], values["max_output"]
        if not isinstance(message, str) or not message.strip() or len(message) > 8000 or type(max_output) is not int:
            raise AgentHubError("Assistant message or output cap is invalid")
        with self.lock:
            _settings, profile = self._active()
            if profile is None or not self.keys.available(profile):
                raise AgentHubError("Configure an active assistant API key for this browser session")
            store = ChatStore(self.data_dir)
            try:
                conversation = self._conversation(store, profile.id, values["conversation"], scope)
                store.add(conversation, "user", message.strip())
                history = store.history(conversation)
            finally:
                store.close()
            plan = prepare_request(profile, history, max_output=max_output, tools_enabled=True)
            request_id = uuid.uuid4().hex
            self.pending[request_id] = (profile, plan, history, conversation)
            payload = json.loads(plan.payload)
            return {"request_id": request_id, "conversation": conversation, "estimated_input": plan.estimated_input,
                    "output_cap": payload.get("max_completion_tokens", payload.get("max_tokens")),
                    "preview": "This sends the bounded recent conversation to the selected provider. No local strategy or coin data is attached yet.",
                    "expires_in_seconds": 300}

    def evidence(self, values: object) -> dict:
        """Return a deterministic explanation without contacting any provider."""
        if not isinstance(values, dict) or set(values) != {"selector", "question"}:
            raise AgentHubError("Evidence read requires selector and question fields")
        with self.lock:
            adapter = EvidenceAssistant(self.data_dir)
            context = adapter.context(values["selector"])
            explanation = explain_context(context, values["question"])
            _settings, profile = self._active()
            ai_available = profile is not None and self.keys.available(profile)
            return {
                "schema": "pumpwizard.evidence-assistant-view/v1",
                "context": context,
                "explanation": explanation,
                "ai": {
                    "available": ai_available,
                    "profile": self._public(profile) if profile is not None else None,
                    "unavailable_reason": None if ai_available else "Configure an assistant provider to request an optional AI explanation.",
                },
            }

    def prepare_evidence(self, values: object, scope: str = "") -> dict:
        required = {"conversation", "message", "max_output", "selector", "question", "context_id"}
        if not isinstance(values, dict) or set(values) != required:
            raise AgentHubError("Evidence AI request fields do not match the v1 contract")
        message, max_output = values["message"], values["max_output"]
        if (not isinstance(message, str) or not message.strip() or len(message) > 2_000
                or type(max_output) is not int or not isinstance(values["context_id"], str)):
            raise AgentHubError("Evidence AI message, context identity, or output cap is invalid")
        with self.lock:
            _settings, profile = self._active()
            if profile is None or not self.keys.available(profile):
                raise AgentHubError("Configure an active assistant API key before preparing optional AI output")
            adapter = EvidenceAssistant(self.data_dir)
            context = adapter.context(values["selector"])
            if context["id"] != values["context_id"]:
                raise AgentHubError("Selected evidence changed; inspect it and prepare a new request")
            explanation = explain_context(context, values["question"])
            store = ChatStore(self.data_dir)
            try:
                conversation = self._conversation(store, profile.id, values["conversation"], scope)
                store.add(conversation, "user", message.strip(), {
                    "evidence_context_id": context["id"],
                    "local_explanation_id": explanation["id"],
                })
                history = store.history(conversation)
            finally:
                store.close()
            plan = prepare_evidence_request(
                profile, history, context, explanation, message.strip(), max_output=max_output,
            )
            request_id = uuid.uuid4().hex
            self.pending[request_id] = (profile, plan, history, conversation)
            self.pending_evidence[request_id] = {
                # Keep the validated lookup selector locally. Public decision
                # contexts expose only its digest because legacy keys embed JSON.
                "selector": validate_selector(values["selector"]),
                "context_id": context["id"],
                "context": context,
                "explanation": explanation,
            }
            payload = json.loads(plan.payload)
            return {
                "schema": "pumpwizard.evidence-ai-preview/v1",
                "request_id": request_id,
                "conversation": conversation,
                "provider": {"profile_id": profile.id, "endpoint": profile.endpoint,
                             "model": profile.model},
                "context_id": context["id"],
                "request_sha256": plan.digest,
                "estimated_input": plan.estimated_input,
                "output_cap": payload.get("max_completion_tokens", payload.get("max_tokens")),
                "estimated_cost_usd": None,
                "actual_usage": None,
                "actual_cost_usd": None,
                "preview": json.loads(plan.preview),
                "expires_in_seconds": 300,
            }

    def _check_pending_scope(self, request_id: str, scope: str) -> None:
        pending = self.pending.get(request_id)
        if pending is None:
            raise AgentHubError("Request expired, was already used, or was declined")
        profile, _plan, _history, conversation = pending
        store = ChatStore(self.data_dir)
        try:
            self._conversation(store, profile.id, conversation, scope)
        finally:
            store.close()

    def approve(self, request_id: str, scope: str = "") -> dict:
        with self.lock:
            self._check_pending_scope(request_id, scope)
            pending = self.pending.pop(request_id, None)
            evidence = self.pending_evidence.pop(request_id, None)
            if pending is None:
                raise AgentHubError("Request expired, was already used, or was declined")
            profile, plan, history, conversation = pending
            _settings, active = self._active()
            if active != profile:
                raise AgentHubError("Assistant profile changed; prepare a new request")
            key = self.keys.get(profile)
            if not key and profile.label != "Local Ollama":
                raise AgentHubError("Assistant key is unavailable in this local browser session")
            store = ChatStore(self.data_dir)
            try:
                if evidence is not None:
                    current = EvidenceAssistant(self.data_dir).context(evidence["selector"])
                    if current["id"] != evidence["context_id"]:
                        raise AgentHubError(
                            "Selected evidence changed after preview; prepare and approve a new request"
                        )
                store.add(conversation, "notice", "Approved one provider request.",
                          {"request_sha256": plan.digest, "estimated_input": plan.estimated_input})
                result = execute_request(profile, plan, api_key=key, approved=True)
                message = result["message"]
                if evidence is not None:
                    if message.get("tool_calls"):
                        raise AgentHubError("Evidence AI returned an unsupported capability request")
                    reviewed = validate_ai_candidate(message.get("content"), evidence["context"])
                    store.add(
                        conversation,
                        "assistant",
                        json.dumps(reviewed, ensure_ascii=False, sort_keys=True),
                        {
                            "actual_usage": result["usage"],
                            "actual_cost_usd": None,
                            "estimated_input": plan.estimated_input,
                            "evidence_context_id": evidence["context_id"],
                            "candidate_sha256": reviewed["candidate_sha256"],
                        },
                    )
                    return {
                        "conversation": conversation,
                        "actual_usage": result["usage"],
                        "actual_cost_usd": None,
                        "reviewed_response": reviewed,
                        "context_request": None,
                    }
                if message.get("content"):
                    store.add(conversation, "assistant", message["content"], {"usage": result["usage"]})
                if message.get("tool_calls"):
                    library = StrategyLibrary(self.data_dir)
                    try:
                        next_plan = context_continuation(profile, history, message, library,
                                                         self.data_dir / "strategy-catalog.json")
                    finally:
                        library.close()
                    next_id = uuid.uuid4().hex
                    self.pending[next_id] = (profile, next_plan, history, conversation)
                    store.add(conversation, "notice", "Assistant requested bounded local context; review before sending again.",
                              {"usage": result["usage"]})
                    return {"conversation": conversation, "usage": result["usage"], "context_request": {
                        "request_id": next_id, "estimated_input": next_plan.estimated_input, "preview": next_plan.preview,
                        "expires_in_seconds": 300}}
                return {"conversation": conversation, "usage": result["usage"], "context_request": None}
            except Exception as exc:
                store.add(conversation, "notice", str(exc) if isinstance(exc, AgentHubError) else "AI request did not complete; no automatic retry.")
                raise
            finally:
                store.close()

    def decline(self, request_id: str, scope: str = "") -> dict:
        with self.lock:
            self._check_pending_scope(request_id, scope)
            pending = self.pending.pop(request_id, None)
            self.pending_evidence.pop(request_id, None)
            if pending is None:
                raise AgentHubError("Request expired or was already handled")
            _profile, _plan, _history, conversation = pending
            store = ChatStore(self.data_dir)
            try:
                store.add(conversation, "notice", "Declined provider/context request. No provider request was sent.")
            finally:
                store.close()
            return {"conversation": conversation, "declined": True}
