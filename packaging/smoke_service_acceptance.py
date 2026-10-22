"""Exercise wallet-signed acceptance in an exact package without network or funds."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from pumpwizard_runner import main_wallet_autopilot as autopilot
from pumpwizard_runner import strategy_risk, wallet_risk
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.manual_trades import TradeJournal
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.pump_swap_route import ROUTE as SWAP_ROUTE
from pumpwizard_runner.service_wallet_vault import (
    KDF_ITERATIONS,
    SERVICE_CUSTODY,
    SERVICE_VAULT_SCHEMA,
)
from pumpwizard_runner.strategies import parse_package
from pumpwizard_runner.transfers import GENESIS
from pumpwizard_runner.wallet_vault import WalletVault


PASSWORD = "disposable package acceptance password"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _service_envelope(wallet) -> dict[str, object]:
    identity = {
        "schema": SERVICE_VAULT_SCHEMA,
        "vault_id": os.urandom(16).hex(),
        "custody": SERVICE_CUSTODY,
        "address": wallet.address,
        "network": "mainnet-beta",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    salt, nonce = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt, KDF_ITERATIONS, 32)
    aad = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ciphertext = AESGCM(key).encrypt(nonce, wallet._seed, aad)
    return {
        **identity,
        "kdf": {
            "name": "PBKDF2",
            "hash": "SHA-256",
            "iterations": KDF_ITERATIONS,
            "salt": base64.b64encode(salt).decode(),
        },
        "cipher": {
            "name": "AES-256-GCM",
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        },
    }


def _seed(directory: Path):
    wallet = WalletVault(directory / "disposable-browser-wallet").create(PASSWORD)
    library = StrategyLibrary(directory)
    strategy = parse_package(
        {
            "schema": "pumpwizard.strategy-handoff/v1",
            "strategy": {"id": "package-acceptance", "label": "Package acceptance"},
            "configuration": CONFIG,
        }
    )
    library.import_strategy(strategy)
    session_id = library.create_instance(strategy.digest, entry_mode="manual")
    permissions = {
        **autopilot.requested_permissions("entries-and-exits", SERVICE_CUSTODY),
        "automation_wallet_required": False,
        "generated_wallet_required": True,
    }
    brief = library.session_brief(session_id)
    brief.update(
        schema="pumpwizard.session-brief/v4",
        mode="trade-autopilot",
        session_mode="trade",
        execution_style="autopilot",
        automation_scope="entries-and-exits",
        profit_management="strategy-managed",
        permissions=permissions,
    )
    with library._connection:
        library._connection.execute(
            "UPDATE session_briefs SET schema_name=?,payload_json=? WHERE instance_id=?",
            (
                brief["schema"],
                json.dumps(brief, sort_keys=True, separators=(",", ":")),
                session_id,
            ),
        )
    journal = TradeJournal(directory)
    try:
        wallet_risk.save_policy(
            journal,
            GENESIS["mainnet-beta"],
            wallet.address,
            {
                "max_buy_lamports": 200_000_000,
                "max_exposure_lamports": 500_000_000,
                "reserve_lamports": 100_000_000,
                "daily_loss_lamports": 100_000_000,
                "drawdown_lamports": 100_000_000,
                "portfolio_drawdown_lamports": 500_000_000,
                "entries_paused": False,
            },
        )
        strategy_risk.save_policy(
            journal,
            session_id,
            GENESIS["mainnet-beta"],
            wallet.address,
            {
                "allocation_lamports": 200_000_000,
                "buys_enabled": True,
                "sells_enabled": True,
                "min_trade_lamports": 1,
                "max_trade_lamports": 150_000_000,
                "max_daily_buy_lamports": 180_000_000,
                "max_trades_per_day": 4,
                "max_daily_loss_lamports": 40_000_000,
                "max_daily_loss_bps": 2_000,
                "max_trade_loss_bps": 4_000,
                "max_open_positions": 2,
            },
            3_000_000_000,
        )
    finally:
        journal.close()
        library.close()
    return wallet, session_id


class Client:
    def __init__(self, origin: str, token: str):
        self.origin = origin
        self.headers = {
            "Origin": origin,
            "Sec-Fetch-Site": "same-origin",
            "X-PumpWizard-Browser-Token": token,
        }

    def get(self, path: str) -> dict:
        request = urllib.request.Request(self.origin + path, headers=self.headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def post(self, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            self.origin + path,
            headers={**self.headers, "Content-Type": "application/json"},
            data=json.dumps(body).encode(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())


def _sign_in(client: Client, wallet) -> None:
    challenge = client.post(
        "/api/v1/wallet/challenge",
        {"sender": wallet.address, "network": "mainnet-beta"},
    )
    signature = base64.b64encode(
        wallet.sign(base64.b64decode(challenge["message"], validate=True))
    ).decode()
    client.post(
        "/api/v1/wallet/verify",
        {
            "sender": wallet.address,
            "network": "mainnet-beta",
            "challenge_id": challenge["challenge_id"],
            "signature": signature,
        },
    )


def _exercise(client: Client, wallet, session_id: str) -> dict[str, object]:
    _sign_in(client, wallet)
    client.post(
        "/api/v1/service/signing/enroll",
        {
            "sender": wallet.address,
            "network": "mainnet-beta",
            "envelope": _service_envelope(wallet),
            "service_unlock_secret": PASSWORD,
        },
    )
    review = client.post(
        "/api/v1/service/signing/review",
        {
            "sender": wallet.address,
            "network": "mainnet-beta",
            "session_id": session_id,
            "plan": {
                "duration_seconds": 900,
                "budget_lamports": 200_000_000,
                "max_order_debit_lamports": 120_000_000,
                "max_daily_debit_lamports": 180_000_000,
                "max_orders": 4,
                "max_trades_per_day": 4,
                "max_open_positions": 2,
                "slippage_bps": 100,
            },
        },
    )["review"]
    assert review["ready"] is True
    allowance = client.post(
        "/api/v1/service/signing/authorize",
        {
            "sender": wallet.address,
            "network": "mainnet-beta",
            "session_id": session_id,
            "review_id": review["id"],
            "confirmation": f"ALLOW SERVICE {session_id[:8].upper()}",
        },
    )["allowance"]
    acceptance = client.post(
        "/api/v1/service/signing/acceptance/request",
        {
            "sender": wallet.address,
            "network": "mainnet-beta",
            "allowance_id": allowance["id"],
            "routes": [SWAP_ROUTE],
            "duration_seconds": 300,
        },
    )["acceptance"]
    initial_status = client.get("/api/v1/service/status")
    assert not any(job["enabled"] for job in initial_status["jobs"])
    runtime_identity = initial_status["build_identity"]
    assert acceptance["payload"]["build_identity"] == runtime_identity
    approval_signature = base64.b64encode(
        wallet.sign(base64.b64decode(acceptance["message"], validate=True))
    ).decode()
    available = client.post(
        "/api/v1/service/signing/acceptance/install",
        {
            "sender": wallet.address,
            "network": "mainnet-beta",
            "payload": acceptance["payload"],
            "signature": approval_signature,
        },
    )["trading"]
    assert available["production_activation"] == "blocked"
    assert available["acceptance_activation"] == "available"
    active = client.post(
        "/api/v1/service/signing/start",
        {
            "sender": wallet.address,
            "network": "mainnet-beta",
            "allowance_id": allowance["id"],
            "service_unlock_secret": PASSWORD,
            "confirmation": f"START {session_id[:8].upper()}",
        },
    )["trading"]
    assert active["production_activation"] == "blocked"
    assert active["acceptance_activation"] == "active"
    assert active["signer_state"] == "unlocked"
    approval_id = acceptance["payload"]["approval_id"]
    query = urllib.parse.urlencode(
        {
            "sender": wallet.address,
            "network": "mainnet-beta",
            "approval_id": approval_id,
        }
    )
    evidence = client.get(
        f"/api/v1/service/signing/acceptance/evidence?{query}"
    )["evidence"]
    assert evidence["status"] == "empty"
    assert evidence["approval"]["signature_verified"] is True
    assert evidence["allowance"]["binding_matches_approval"] is True
    assert evidence["allowance"]["used_orders"] == 0
    return {
        "schema": "pumpwizard.packaged-service-acceptance-smoke/v1",
        "checked_at": datetime.fromtimestamp(time.time(), timezone.utc).isoformat(),
        "build_identity": runtime_identity,
        "wallet": {
            "kind": "disposable_generated",
            "sender": wallet.address,
            "network": "mainnet-beta",
        },
        "route": SWAP_ROUTE,
        "approval_id": approval_id,
        "funded_acceptance": False,
        "submission_enabled": False,
        "service_job_enabled": False,
        "installed": {
            "production_activation": available["production_activation"],
            "acceptance_activation": available["acceptance_activation"],
        },
        "activated": {
            "production_activation": active["production_activation"],
            "acceptance_activation": active["acceptance_activation"],
            "signer_state": active["signer_state"],
        },
        "audit": {
            "status": evidence["status"],
            "signature_verified": evidence["approval"]["signature_verified"],
            "binding_matches_approval": evidence["allowance"][
                "binding_matches_approval"
            ],
            "used_orders": evidence["allowance"]["used_orders"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    executable = str(args.executable.resolve())
    with tempfile.TemporaryDirectory(
        prefix="pumpwizard-package-acceptance-"
    ) as raw_directory:
        directory = Path(raw_directory)
        wallet, session_id = _seed(directory)
        port = _port()
        lifecycle = [executable, "--data-dir", str(directory), "service"]
        try:
            subprocess.run(
                [*lifecycle, "start", "--port", str(port)],
                check=True,
                capture_output=True,
                text=True,
                timeout=45,
            )
            token = (directory / "background-service-access-token").read_text().strip()
            client = Client(f"http://127.0.0.1:{port}", token)
            result = _exercise(client, wallet, session_id)
        finally:
            subprocess.run(
                [*lifecycle, "stop", "--port", str(port)],
                capture_output=True,
                text=True,
                timeout=15,
            )
        subprocess.run(
            [*lifecycle, "start", "--port", str(port)],
            check=True,
            capture_output=True,
            text=True,
            timeout=45,
        )
        try:
            token = (directory / "background-service-access-token").read_text().strip()
            restarted = Client(f"http://127.0.0.1:{port}", token).get(
                "/api/v1/service/status"
            )["trading"]
            assert restarted["production_activation"] == "blocked"
            assert restarted["acceptance_activation"] == "blocked"
            assert restarted["signer_state"] == "locked"
            result["restart"] = {
                "production_activation": restarted["production_activation"],
                "acceptance_activation": restarted["acceptance_activation"],
                "signer_state": restarted["signer_state"],
            }
        finally:
            subprocess.run(
                [*lifecycle, "stop", "--port", str(port)],
                capture_output=True,
                text=True,
                timeout=15,
            )
        encoded = json.dumps(result, indent=2) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
        print(encoded, end="")


if __name__ == "__main__":
    main()
