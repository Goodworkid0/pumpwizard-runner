import json

import pytest

from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.transfers import TransferError
from pumpwizard_runner.web_api import command, payload
from pumpwizard_runner import __version__


class Authorizer:
    def __init__(self):
        self.calls = []

    def require(self, sender, network):
        if (sender, network) != ("wallet", "devnet"):
            raise TransferError("Wallet ownership proof is required")
        self.calls.append((sender, network))
        return sender, network


class Service:
    def __init__(self):
        self.received = []
        self.allowances = []

    def status(self):
        return {
            "service": {
                "schema": "pumpwizard.background-service-status/v1",
                "instance_id": "service-1", "instance_revision": 1,
                "data_directory_id": "a" * 64, "version": "test",
                "state": "running", "health": "healthy",
                "production_trading": "blocked",
            },
            "jobs": [], "coverage": [],
            "trading": {
                "schema": "pumpwizard.service-trading-state/v1",
                "signer_state": "locked", "authorization_status": "none",
                "production_activation": "blocked",
            },
        }

    def signing_overview(self, sender, network):
        self.received.append(("overview", sender, network))
        return {
            "trading": self.status()["trading"],
            "enrollment": None,
            "allowances": self.allowances,
        }

    def enroll_signer(self, body):
        self.received.append(("enroll", body))
        return {"schema": "pumpwizard.service-wallet-vault/v1", "address": body["sender"]}

    def review_signing(self, library, body):
        assert isinstance(library, StrategyLibrary)
        self.received.append(("review", body))
        return {"schema": "pumpwizard.main-wallet-allowance-review/v2", "id": "review"}

    def authorize_signing(self, library, body):
        assert isinstance(library, StrategyLibrary)
        self.received.append(("authorize", body))
        return {"schema": "pumpwizard.main-wallet-allowance/v2", "id": "allowance"}

    def start_signing(self, body):
        self.received.append(("start", body))
        return self.status()["trading"]

    def review_signing_acceptance(self, body):
        self.received.append(("acceptance-request", body))
        return {"schema": "pumpwizard.service-acceptance-request/v1", "message": "signed"}

    def install_signing_acceptance(self, body):
        self.received.append(("acceptance-install", body))
        return self.status()["trading"]

    def signing_acceptance_evidence(self, sender, network, approval_id):
        self.received.append(("acceptance-evidence", sender, network, approval_id))
        return {"schema": "pumpwizard.service-acceptance-evidence/v1", "status": "reconciled"}


def test_service_status_is_separate_from_wallet_authenticated_signing_control(tmp_path):
    library = StrategyLibrary(tmp_path)
    service, authorizer = Service(), Authorizer()
    try:
        status = payload(library, "/api/v1/service/status", {}, background_service=service)
        assert status["service"]["instance_id"] == "service-1"
        assert status["build_identity"] == {
            "schema": "pumpwizard.build-identity/v1",
            "build_id": "development",
            "version": __version__,
            "source_checkpoint": "development",
            "packaged_at": "development",
            "package_kind": "source",
        }

        with pytest.raises(TransferError, match="ownership proof"):
            payload(
                library, "/api/v1/service/signing",
                {"sender": ["attacker"], "network": ["devnet"]},
                authorizer, background_service=service,
            )
        signing = payload(
            library, "/api/v1/service/signing",
            {"sender": ["wallet"], "network": ["devnet"]},
            authorizer, background_service=service,
        )
        assert signing["allowances"] == []
        assert signing["order_controls"] == {
            "schema": "pumpwizard.service-order-controls/v1",
            "cancelled_reviews": [],
            "unresolved_submissions": [],
            "authority": {"orders": False, "signer": False, "submission": False},
        }
        assert authorizer.calls == [("wallet", "devnet")]
    finally:
        library.close()


def test_service_signing_exposes_revoked_reviews_and_unresolved_submissions(tmp_path):
    from pumpwizard_runner.agent_execution import SCHEMA as AGENT_SCHEMA
    from pumpwizard_runner.main_wallet_autopilot import SCHEMA as ALLOWANCE_SCHEMA
    from pumpwizard_runner.manual_trades import TradeJournal
    from pumpwizard_runner.transfers import GENESIS

    library = StrategyLibrary(tmp_path)
    service, authorizer = Service(), Authorizer()
    service.allowances = [{"id": "allowance-1", "session_id": "session-1"}]
    journal = TradeJournal(tmp_path)
    try:
        journal.db.executescript(AGENT_SCHEMA)
        journal.db.executescript(ALLOWANCE_SCHEMA)
        preview = json.dumps({"sender": "wallet", "genesis": GENESIS["devnet"]})
        with journal.db:
            journal.db.execute(
                "INSERT INTO agent_orders VALUES (?,?,?,?,?,?,?,?,?)",
                ("cancelled-1", "session-1", "mint", "buy", "cash-1", None,
                 "cancelled", "{}", 10.0),
            )
            journal.db.execute(
                "INSERT INTO trade_previews VALUES (?,?)", ("cancelled-1", preview),
            )
            journal.db.execute(
                "INSERT INTO main_wallet_allowance_events"
                "(allowance_id,at,kind,detail,payload) VALUES (?,?,?,?,?)",
                ("allowance-1", 20.0, "unsigned-reviews-cancelled", "Entries paused",
                 json.dumps({"order_ids": ["cancelled-1"]})),
            )
            journal.db.execute(
                "INSERT INTO agent_orders VALUES (?,?,?,?,?,?,?,?,?)",
                ("submitted-1", "session-1", "mint", "sell", "cash-2", "tokens-1",
                 "submitted", "{}", 30.0),
            )
            journal.db.execute(
                "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,NULL)",
                ("submitted-1", GENESIS["devnet"], "wallet", "signature-1", "unknown",
                 30.0, "{}", "Awaiting reconciliation"),
            )

        signing = payload(
            library, "/api/v1/service/signing",
            {"sender": ["wallet"], "network": ["devnet"]},
            authorizer, background_service=service,
        )
        controls = signing["order_controls"]
        assert controls["authority"] == {
            "orders": False, "signer": False, "submission": False,
        }
        assert controls["cancelled_reviews"] == [{
            "order_id": "cancelled-1", "session_id": "session-1", "side": "buy",
            "status": "cancelled", "created_at": 10.0, "cancelled_at": 20.0,
            "detail": "Entries paused",
        }]
        assert controls["unresolved_submissions"] == [{
            "order_id": "submitted-1", "session_id": "session-1", "side": "sell",
            "status": "submitted", "submission_state": "unknown",
            "signature": "signature-1", "created_at": 30.0,
        }]
    finally:
        journal.close()
        library.close()


def test_first_use_capabilities_separate_local_auth_from_wallet_authority(tmp_path):
    library = StrategyLibrary(tmp_path)
    service = Service()
    try:
        result = payload(
            library, "/api/v1/capabilities", {}, background_service=service,
        )["capabilities"]
        assert result["schema"] == "pumpwizard.first-use-capabilities/v1"
        assert result["authentication"] == {
            "state": "authenticated",
            "method": "local_launch_token",
            "scope": "local_data_directory",
            "wallet_identity": False,
        }
        assert result["research"] == {
            "state": "available",
            "wallet_required": False,
            "deposit_required": False,
            "signer_required": False,
            "ai_provider_required": False,
            "collection_start": "explicit",
        }
        assert result["wallet_actions"]["state"] == "connection_required"
        assert result["wallet_actions"]["address"] is None
        assert result["wallet_actions"]["network_scan"] == "not_started"
        assert result["service"] == {
            "lifecycle": "background_service",
            "state": "running",
            "signer_state": "locked",
            "browser_close_stops_service": False,
        }
        assert result["production_execution"]["state"] == "blocked"
        assert result["production_execution"]["automatic_execution"] is False
        assert not any(result["read_effects"].values())
        browser_owned = payload(library, "/api/v1/capabilities", {})["capabilities"]
        assert browser_owned["service"]["lifecycle"] == "browser_owned"
        assert browser_owned["service"]["browser_close_stops_service"] is False
    finally:
        library.close()


def test_service_enrollment_secret_is_consumed_only_by_wallet_authenticated_route(tmp_path):
    library = StrategyLibrary(tmp_path)
    service, authorizer = Service(), Authorizer()
    body = {
        "sender": "wallet", "network": "devnet",
        "envelope": {"schema": "pumpwizard.service-wallet-vault/v1", "ciphertext": "opaque"},
        "service_unlock_secret": "never-echo-this",
    }
    try:
        result = command(
            library, "/api/v1/service/signing/enroll", body,
            wallet_authorizer=authorizer, background_service=service,
        )
        assert result["enrollment"]["address"] == "wallet"
        assert "never-echo-this" not in json.dumps(result)
        assert service.received[-1] == ("enroll", body)
        with pytest.raises(TransferError, match="ownership proof"):
            command(
                library, "/api/v1/service/signing/enroll",
                {**body, "sender": "attacker"},
                wallet_authorizer=authorizer, background_service=service,
            )
    finally:
        library.close()


@pytest.mark.parametrize(("suffix", "extra", "key"), (
    ("review", {"session_id": "session", "plan": {}}, "review"),
    ("authorize", {
        "session_id": "session", "review_id": "review", "confirmation": "ALLOW SERVICE SESSION",
    }, "allowance"),
    ("start", {
        "allowance_id": "allowance", "service_unlock_secret": "transient",
        "confirmation": "START SESSION",
    }, "trading"),
))
def test_service_signing_lifecycle_routes_preserve_typed_operations(
    tmp_path, suffix, extra, key,
):
    library = StrategyLibrary(tmp_path)
    service, authorizer = Service(), Authorizer()
    try:
        result = command(
            library, f"/api/v1/service/signing/{suffix}",
            {"sender": "wallet", "network": "devnet", **extra},
            wallet_authorizer=authorizer, background_service=service,
        )
        assert key in result
    finally:
        library.close()


@pytest.mark.parametrize(("suffix", "extra", "key"), (
    ("acceptance/request", {
        "allowance_id": "allowance", "routes": ["route"], "duration_seconds": 300,
    }, "acceptance"),
    ("acceptance/install", {
        "payload": {"allowance_id": "allowance"}, "signature": "signature",
    }, "trading"),
))
def test_service_acceptance_commands_require_wallet_ownership(
    tmp_path, suffix, extra, key,
):
    library = StrategyLibrary(tmp_path)
    service, authorizer = Service(), Authorizer()
    body = {"sender": "wallet", "network": "devnet", **extra}
    try:
        result = command(
            library, f"/api/v1/service/signing/{suffix}", body,
            wallet_authorizer=authorizer, background_service=service,
        )
        assert key in result
        with pytest.raises(TransferError, match="ownership proof"):
            command(
                library, f"/api/v1/service/signing/{suffix}",
                {**body, "sender": "attacker"},
                wallet_authorizer=authorizer, background_service=service,
            )
    finally:
        library.close()


def test_service_acceptance_evidence_requires_wallet_ownership(tmp_path):
    library = StrategyLibrary(tmp_path)
    service, authorizer = Service(), Authorizer()
    query = {
        "sender": ["wallet"], "network": ["devnet"], "approval_id": ["approval"],
    }
    try:
        result = payload(
            library, "/api/v1/service/signing/acceptance/evidence", query,
            authorizer, background_service=service,
        )
        assert result["evidence"]["status"] == "reconciled"
        with pytest.raises(TransferError, match="ownership proof"):
            payload(
                library, "/api/v1/service/signing/acceptance/evidence",
                {**query, "sender": ["attacker"]},
                authorizer, background_service=service,
            )
    finally:
        library.close()
