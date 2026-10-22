import base64
import json
import sqlite3
import time

import pytest

from pumpwizard_runner import main_wallet_autopilot, service_acceptance
from pumpwizard_runner.service_acceptance import (
    acceptance_approval_request,
    acceptance_evidence,
    verify_acceptance_approval,
)
from pumpwizard_runner.service_wallet_vault import SERVICE_CUSTODY
from pumpwizard_runner.transfers import GENESIS, TransferError
from pumpwizard_runner.wallet_vault import WalletVault


PACKAGED_BUILD = {
    "schema": "pumpwizard.build-identity/v1",
    "build_id": "fixture-build",
    "version": "0.2.0.dev65",
    "source_checkpoint": "fixture-checkpoint",
    "packaged_at": "2026-09-29T00:00:00Z",
    "package_kind": "linux-x86_64",
}


def allowance_row(wallet, *, now: float) -> dict:
    scope = {
        "network": "mainnet-beta",
        "custody": SERVICE_CUSTODY,
        "custody_identity": "55" * 16,
        "cash_hold": "66" * 16,
        "routes": ["pump_swap_amm_buy_exact_quote_in", "pump_swap_amm_sell"],
        "budget_lamports": 200_000_000,
        "max_order_debit_lamports": 100_000_000,
        "max_daily_debit_lamports": 180_000_000,
        "max_orders": 4,
        "max_trades_per_day": 4,
        "max_open_positions": 2,
        "slippage_bps": 100,
    }
    requested = {"custody": SERVICE_CUSTODY, "automatic_entries": True}
    granted = {**requested, "automatic_protective_exits": True}
    return {
        "id": "11" * 16,
        "review_id": "22" * 16,
        "session_id": "33" * 16,
        "genesis": GENESIS["mainnet-beta"],
        "sender": wallet.address,
        "strategy_digest": "44" * 32,
        "policy_revision": 7,
        "authorization_revision": 1,
        "requested_permissions": json.dumps(requested, sort_keys=True),
        "granted_permissions": json.dumps(granted, sort_keys=True),
        "scope": json.dumps(scope, sort_keys=True),
        "created_at": now - 10,
        "expires_at": now + 7_200,
        "status": "authorized",
        "entries_paused": 0,
        "detail": "Authorized fixture allowance",
    }


def sign_payload(wallet, payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return base64.b64encode(wallet.sign(service_acceptance.DOMAIN + encoded.encode())).decode()


@pytest.fixture
def approved(tmp_path, monkeypatch):
    wallet = WalletVault(tmp_path / "wallet").create("fixture wallet password")
    now = time.time()
    row = allowance_row(wallet, now=now)
    monkeypatch.setattr(service_acceptance, "current_build_identity", lambda: PACKAGED_BUILD)
    request = acceptance_approval_request(
        row,
        ["pump_swap_amm_sell", "pump_swap_amm_buy_exact_quote_in"],
        3_600,
        now=now,
    )
    signature = base64.b64encode(wallet.sign(base64.b64decode(request["message"]))).decode()
    capability = verify_acceptance_approval(request["payload"], signature, now=now)
    return wallet, row, request["payload"], signature, capability, now


def test_wallet_signature_cannot_extend_effective_acceptance_past_one_hour(approved):
    wallet, _, payload, _, _, now = approved
    shifted = {
        **payload,
        "approval_id": "77" * 16,
        "approved_at": now + 300,
        "expires_at": now + 3_900,
    }
    signature = sign_payload(wallet, shifted)
    with pytest.raises(TransferError, match="cannot extend beyond 60 minutes"):
        verify_acceptance_approval(shifted, signature, now=now)


def test_capability_rejects_allowance_vault_policy_and_expiry_mutation(approved):
    _, row, _, _, capability, now = approved
    capability.validate_allowance(row, vault_id="55" * 16, now=now)
    for name, changed in (
        ("vault", {**json.loads(row["scope"]), "custody_identity": "88" * 16}),
        ("policy", None),
        ("expiry", None),
    ):
        mutated = dict(row)
        if name == "vault":
            mutated["scope"] = json.dumps(changed, sort_keys=True)
        elif name == "policy":
            mutated["policy_revision"] += 1
        else:
            mutated["expires_at"] -= 1
        with pytest.raises(TransferError, match="exact allowance|another service vault|outlives"):
            capability.validate_allowance(mutated, vault_id="55" * 16, now=now)


def test_archived_evidence_fails_closed_if_allowance_authority_is_mutated(approved):
    _, row, payload, signature, _, _ = approved
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(main_wallet_autopilot.SCHEMA)
    db.executescript("""
        CREATE TABLE service_acceptance_approvals (
            approval_id TEXT PRIMARY KEY, allowance_id TEXT NOT NULL UNIQUE,
            activated_at REAL NOT NULL, message_sha256 TEXT NOT NULL,
            payload TEXT NOT NULL, signature TEXT NOT NULL
        );
        CREATE TABLE trades (
            id TEXT PRIMARY KEY, signature TEXT, status TEXT, detail TEXT,
            payload TEXT, receipt TEXT
        );
    """)
    values = tuple(row[name] for name in (
        "id", "review_id", "session_id", "genesis", "sender", "strategy_digest",
        "policy_revision", "authorization_revision", "requested_permissions",
        "granted_permissions", "scope", "created_at", "expires_at", "status",
        "entries_paused", "detail",
    ))
    db.execute(
        "INSERT INTO main_wallet_allowances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        values,
    )
    message = service_acceptance.DOMAIN + json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()
    db.execute(
        "INSERT INTO service_acceptance_approvals VALUES (?,?,?,?,?,?)",
        (
            payload["approval_id"], row["id"], time.time(),
            service_acceptance.hashlib.sha256(message).hexdigest(),
            json.dumps(payload, sort_keys=True, separators=(",", ":")), signature,
        ),
    )

    class Journal:
        pass

    journal = Journal()
    journal.db = db
    assert acceptance_evidence(journal, payload["approval_id"])["status"] == "empty"
    scope = json.loads(row["scope"])
    scope["budget_lamports"] += 1
    db.execute(
        "UPDATE main_wallet_allowances SET scope=? WHERE id=?",
        (json.dumps(scope, sort_keys=True), row["id"]),
    )
    with pytest.raises(TransferError, match="binding does not match"):
        acceptance_evidence(journal, payload["approval_id"])
    db.close()
