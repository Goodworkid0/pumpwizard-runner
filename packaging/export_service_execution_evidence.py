#!/usr/bin/env python3
"""Export a redacted service approval/order/receipt/accounting record.

The exporter is read-only and never opens a wallet vault. Transaction signatures
and wallet addresses are public chain identities; private keys, passwords,
runtime tokens, ciphertext, and signed transaction wires are not read or emitted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from pumpwizard_runner.service_acceptance import acceptance_evidence


SCHEMA = "pumpwizard.service-execution-evidence/v1"


class ReadOnlyJournal:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15
        )
        self.db.row_factory = sqlite3.Row

    def close(self) -> None:
        self.db.close()


def _json(value: object) -> object:
    if value is None:
        return None
    return json.loads(str(value))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(directory: Path, approval_id: str | None = None) -> dict[str, object]:
    database = Path(directory).resolve() / "wallet-transfers.sqlite"
    if not database.is_file():
        raise ValueError("Service execution journal was not found")
    journal = ReadOnlyJournal(database)
    try:
        approvals = [
            str(row[0])
            for row in journal.db.execute(
                "SELECT approval_id FROM service_acceptance_approvals ORDER BY activated_at"
            )
        ]
        if approval_id is None:
            if len(approvals) != 1:
                raise ValueError("Select one approval ID from this service journal")
            approval_id = approvals[0]
        if approval_id not in approvals:
            raise ValueError("Service acceptance approval was not found")
        trail = acceptance_evidence(journal, approval_id)
        allowance = trail["allowance"]
        session_id = allowance["session_id"]
        sender = allowance["sender"]
        genesis = allowance["genesis"]
        positions = [
            dict(row)
            for row in journal.db.execute(
                "SELECT p.*,a.agent_id,a.token_hold,a.sold_stages,a.entry_fx "
                "FROM trade_positions p JOIN agent_positions a ON a.position_id=p.id "
                "WHERE a.agent_id=? AND p.sender=? AND p.genesis=? "
                "ORDER BY p.opened_at,p.id",
                (session_id, sender, genesis),
            )
        ]
        reservations = [
            dict(row)
            for row in journal.db.execute(
                "SELECT id,agent_id,genesis,sender,lamports,position_id,mint,"
                "token_atoms,created_at,released_at,status FROM agent_reservations "
                "WHERE genesis=? AND sender=? AND (agent_id=? OR position_id IN "
                "(SELECT position_id FROM agent_positions WHERE agent_id=?)) "
                "ORDER BY created_at,id",
                (genesis, sender, session_id, session_id),
            )
        ]
        events = [
            {
                **dict(row),
                "payload": _json(row["payload"]),
            }
            for row in journal.db.execute(
                "SELECT at,kind,detail,payload FROM main_wallet_allowance_events "
                "WHERE allowance_id=? ORDER BY id",
                (allowance["id"],),
            )
        ]
        unresolved = [
            dict(row)
            for row in journal.db.execute(
                "SELECT id,kind FROM wallet_intents WHERE genesis=? AND sender=? ORDER BY id",
                (genesis, sender),
            )
        ]
        return {
            "schema": SCHEMA,
            "generated_at": time.time(),
            "source": {
                "journal": database.name,
                "sha256": _sha256(database),
                "read_only": True,
            },
            "acceptance": trail,
            "positions": positions,
            "reservations": reservations,
            "allowance_events": events,
            "unresolved_wallet_intents": unresolved,
            "secrets_included": False,
        }
    finally:
        journal.close()


def write_new(path: Path, payload: dict[str, object]) -> None:
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        content = (
            json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode()
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--approval-id")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build(args.directory, args.approval_id)
    write_new(args.output, payload)
    print(json.dumps({
        "schema": SCHEMA,
        "status": payload["acceptance"]["status"],
        "orders": len(payload["acceptance"]["orders"]),
        "output": str(args.output.resolve()),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
