"""Read-only normalized evidence from already retained trade/transfer journals."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from .strategies import canonical_json
from .transfers import GENESIS


SCHEMA = "pumpwizard.recorded-receipt-evidence/v1"
MAX_RECEIPTS = 500
REPORT_SCHEMA = "pumpwizard.receipt-evidence-report/v1"
COMPARISON_SCHEMA = "pumpwizard.receipt-expectation-comparison/v1"
ADAPTER_VERSION = "pumpwizard-read-only-receipt-adapter/1"


class ReceiptEvidenceError(ValueError):
    """Retained receipt evidence is ambiguous, conflicting, or unsupported."""


def retained_receipts(data_dir: str | Path, *, sender: str | None = None,
                      limit: int = 100) -> dict[str, Any]:
    """Read retained rows only; never queries a wallet, RPC or provider."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RECEIPTS:
        raise ValueError(f"Receipt evidence limit must be between 1 and {MAX_RECEIPTS}")
    path = Path(data_dir) / "wallet-transfers.sqlite"
    if not path.exists():
        return _result([], [])
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        trade_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trades'"
        ).fetchone()
        transfer_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transfers'"
        ).fetchone()
        trades = []
        if trade_exists:
            where = " WHERE sender=?" if sender is not None else ""
            parameters: list[Any] = [sender] if sender is not None else []
            parameters.append(limit)
            rows = db.execute(
                "SELECT * FROM trades" + where + " ORDER BY created_at DESC,id DESC LIMIT ?",
                parameters,
            ).fetchall()
            trades = [_trade(row) for row in rows]
        transfers = []
        if transfer_exists:
            where = " WHERE sender=?" if sender is not None else ""
            parameters = [sender] if sender is not None else []
            parameters.append(limit)
            rows = db.execute(
                "SELECT * FROM transfers" + where
                + " ORDER BY created_at DESC,id DESC LIMIT ?",
                parameters,
            ).fetchall()
            transfers = [_transfer(row) for row in rows]
    finally:
        db.close()
    return _result(trades, transfers)


def normalize_receipts(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate synthetic/adapter rows by documented receipt identity."""
    selected: dict[str, dict[str, Any]] = {}
    for raw in rows:
        value = json.loads(canonical_json(raw))
        network = str(value.get("network") or "unknown")
        signature = str(value.get("signature") or "")
        instruction = int(value.get("instruction_index", 0))
        if not signature:
            raise ValueError("Receipt evidence requires a retained signature")
        identity = _digest([network, signature, instruction])
        candidate = {**value, "receipt_identity": identity}
        prior = selected.get(identity)
        if prior is not None and prior != candidate:
            raise ValueError("Duplicate receipt identity has conflicting retained content")
        selected[identity] = candidate
    return [selected[key] for key in sorted(selected)]


def _trade(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload"])
    receipt = json.loads(row["receipt"]) if row["receipt"] else None
    status = str(row["status"])
    if status == "finalized" and receipt is not None and receipt.get("err") is None:
        evidence_state = "finalized_success"
    elif (status == "failed" and receipt is not None and receipt.get("err") is not None
          and receipt.get("token_delta") == 0
          and receipt.get("wallet_delta") == -receipt.get("fee_lamports", -1)):
        evidence_state = "fee_only_failure"
    elif status in {"pending", "confirmed"}:
        evidence_state = "pending"
    else:
        evidence_state = "unresolved"
    ownership = "manual"
    owner_id = None
    context = payload.get("agent_order")
    if isinstance(context, dict):
        ownership = "strategy" if isinstance(context.get("agent_id"), str) else "unattributed"
        owner_id = context.get("agent_id")
    network = str(payload.get("network") or _network(str(row["genesis"])))
    record = {
        "schema": SCHEMA, "kind": "trade", "journal_id": row["id"],
        "network": network, "signature": row["signature"], "instruction_index": 0,
        "sender": row["sender"], "created_at": row["created_at"],
        "state": evidence_state, "ownership": ownership, "owner_id": owner_id,
        "mint": payload.get("mint"), "side": payload.get("side"),
        "route": payload.get("route"),
        "receipt": receipt,
        "units": {
            "wallet_delta_lamports": receipt.get("wallet_delta") if receipt else None,
            "token_delta_atoms": receipt.get("token_delta") if receipt else None,
            "fee_lamports": receipt.get("fee_lamports") if receipt else None,
        },
        "paper_fill": False,
        "limitations": ([] if receipt else ["Final receipt is not retained; outcome is unresolved."]),
    }
    return normalize_receipts([record])[0]


def _transfer(row: sqlite3.Row) -> dict[str, Any]:
    state = str(row["status"])
    evidence_state = (
        "finalized_success" if state == "finalized"
        else "pending" if state in {"pending", "confirmed"}
        else "unresolved"
    )
    record = {
        "schema": SCHEMA, "kind": "withdrawal", "journal_id": row["id"],
        "network": row["network"], "signature": row["signature"],
        "instruction_index": 0, "sender": row["sender"],
        "created_at": row["created_at"], "state": evidence_state,
        "ownership": "external_cash_flow", "owner_id": None,
        "units": {"lamports": row["lamports"], "fee_lamports": row["fee"]},
        "paper_fill": False,
        "return_treatment": "excluded_from_strategy_return",
        "limitations": ["Transfer journal state is not a strategy trade receipt."],
    }
    return normalize_receipts([record])[0]


def _result(trades: list[dict[str, Any]], transfers: list[dict[str, Any]]) -> dict[str, Any]:
    records = normalize_receipts([*trades, *transfers])
    counts = {key: 0 for key in (
        "finalized_success", "fee_only_failure", "pending", "unresolved",
    )}
    for row in records:
        counts[row["state"]] += 1
    return {
        "schema": "pumpwizard.recorded-receipt-collection/v1",
        "records": records, "counts": counts,
        "source": "local retained journals only",
        "read_only": True, "rpc_queried": False,
        "paper_fill_match_default": False,
        "limitations": [
            "No arbitrary wallet history is queried.",
            "Unretained deposits, transfers and external trades remain unattributed.",
            "A modeled paper fill is never presented as an on-chain receipt.",
        ],
    }


def _network(genesis: str) -> str:
    return next((network for network, value in GENESIS.items() if value == genesis), "unknown")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _object(value: object, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ReceiptEvidenceError(f"{field} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ReceiptEvidenceError(f"{field} must be an object")
    return json.loads(canonical_json(value))


def _int(value: object, field: str, *, nonnegative: bool = False) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        result = int(value)
        if str(result) != str(value) and not isinstance(value, int):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ReceiptEvidenceError(f"{field} must be an integer") from exc
    if nonnegative and result < 0:
        raise ReceiptEvidenceError(f"{field} must be nonnegative")
    return result


def _receipt_identity(genesis: str, network: str, signature: str, effect: str) -> tuple[str, dict]:
    identity = {
        "genesis": str(genesis), "network": str(network),
        "signature": str(signature), "instruction_identity": effect,
    }
    if not all(identity.values()):
        raise ReceiptEvidenceError("Receipt identity is incomplete")
    return "receipt-" + _digest(identity), identity


def normalize_retained_receipts(
    trades: Iterable[dict[str, Any]],
    transfers: Iterable[dict[str, Any]] = (),
    *,
    as_of: int | float,
    source_label: str = "retained-local-trade-journal",
    agent_fill_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Produce the strict v1 report while preserving the legacy collection API."""
    if isinstance(as_of, bool) or not isinstance(as_of, (int, float)) or not math.isfinite(as_of):
        raise ReceiptEvidenceError("as_of must be finite")
    linked = {str(value) for value in agent_fill_ids}
    records = []
    for raw in trades:
        row = dict(raw)
        payload = _object(row.get("payload"), "trade.payload")
        receipt = _object(row["receipt"], "trade.receipt") if row.get("receipt") else None
        network = str(payload.get("network") or _network(str(row.get("genesis"))))
        ident, identity = _receipt_identity(
            str(row.get("genesis")), network, str(row.get("signature")),
            "authorized-swap-transaction-effects",
        )
        status = str(row.get("status"))
        amounts = {
            "fee_lamports": None, "cash_delta_lamports": None,
            "token_delta_atoms": None,
        }
        receipt_status = "not-retained"
        classification = "unresolved"
        finalized_at = None
        if receipt is not None:
            fee = _int(receipt.get("fee_lamports"), "receipt fee", nonnegative=True)
            cash = _int(receipt.get("wallet_delta"), "receipt wallet delta")
            tokens = _int(receipt.get("token_delta"), "receipt token delta")
            amounts = {
                "fee_lamports": fee, "cash_delta_lamports": cash,
                "token_delta_atoms": tokens,
            }
            finalized_at = receipt.get("block_time")
            if status == "finalized" and receipt.get("err") is None:
                classification = "finalized_success"
                receipt_status = "validated-finalized-transaction"
            elif status == "failed" and receipt.get("err") is not None \
                    and tokens == 0 and cash == -fee:
                classification = "fee_only_failure"
                receipt_status = "validated-finalized-fee-only-failure"
            else:
                receipt_status = "retained-receipt-state-or-effects-conflict"
        elif status in {"pending", "confirmed"}:
            classification = "pending"
            receipt_status = "awaiting-finalized-transaction"
        elif status == "unknown":
            receipt_status = "outcome-unverified"
        context = payload.get("agent_order")
        if isinstance(context, dict) and isinstance(context.get("agent_id"), str):
            ownership = {
                "kind": "strategy_instance", "agent_id": context["agent_id"],
                "evidence": "retained authorized trade payload",
                "agent_fill_link_retained": str(row.get("id")) in linked,
            }
        else:
            ownership = {
                "kind": "manual",
                "evidence": "retained trade has no agent-order binding",
            }
        records.append({
            "schema": SCHEMA, "id": ident, "identity": identity,
            "journal_record_id": str(row.get("id")), "record_kind": "trade",
            "evidence_class": "recorded_execution", "source_label": source_label,
            "classification": classification, "receipt_status": receipt_status,
            "created_at": row.get("created_at"), "finalized_at": finalized_at,
            "side": payload.get("side"),
            "asset": {
                "mint": payload.get("mint"), "decimals": payload.get("decimals"),
                "token_unit": "atoms",
            },
            "amounts": amounts,
            "units": {
                "cash": "lamports", "cash_asset": "SOL", "fee": "lamports",
                "token": "atoms", "implicit_fx_conversion": False,
            },
            "ownership": ownership, "paper_fill": False,
            "writes_or_rpc_queries_performed": False,
        })
    for raw in transfers:
        row = dict(raw)
        ident, identity = _receipt_identity(
            str(row.get("genesis")), str(row.get("network")), str(row.get("signature")),
            "system-transfer-instruction:0",
        )
        status = str(row.get("status"))
        lamports = _int(row.get("lamports"), "transfer lamports", nonnegative=True)
        fee = _int(row.get("fee"), "transfer fee", nonnegative=True)
        classification = (
            "finalized_success" if status == "finalized"
            else "pending" if status in {"pending", "confirmed"}
            else "unresolved"
        )
        records.append({
            "schema": SCHEMA, "id": ident, "identity": identity,
            "journal_record_id": str(row.get("id")), "record_kind": "withdrawal",
            "evidence_class": "recorded_execution", "source_label": source_label,
            "classification": classification,
            "receipt_status": (
                "finality-status-with-submission-validated-fee"
                if status == "finalized" else "transaction-receipt-not-retained"
            ),
            "created_at": row.get("created_at"), "finalized_at": None,
            "side": "withdrawal",
            "asset": {"mint": None, "decimals": 9, "token_unit": "lamports"},
            "amounts": {
                "fee_lamports": fee if status == "finalized" else None,
                "cash_delta_lamports": -(lamports + fee) if status == "finalized" else None,
                "token_delta_atoms": None, "withdrawal_lamports": lamports,
            },
            "units": {
                "cash": "lamports", "cash_asset": "SOL", "fee": "lamports",
                "implicit_fx_conversion": False,
            },
            "ownership": {
                "kind": "manual", "evidence": "retained explicit withdrawal journal",
            },
            "paper_fill": False, "writes_or_rpc_queries_performed": False,
        })
    if len(records) > 10_000:
        raise ReceiptEvidenceError("Receipt report exceeds the 10000 row bound")
    selected = {}
    duplicates = 0
    for row in records:
        prior = selected.get(row["id"])
        if prior is None:
            selected[row["id"]] = row
        elif prior != row:
            raise ReceiptEvidenceError("Receipt identity was reused with conflicting evidence")
        else:
            duplicates += 1
    normalized = sorted(
        selected.values(), key=lambda row: (row.get("created_at") or -1, row["id"])
    )
    counts = {
        state: sum(row["classification"] == state for row in normalized)
        for state in ("pending", "finalized_success", "fee_only_failure", "unresolved")
    }
    core = {
        "schema": REPORT_SCHEMA, "adapter_version": ADAPTER_VERSION, "as_of": as_of,
        "source": {
            "kind": "existing-trade-journal", "label": source_label,
            "read_only": True, "arbitrary_wallet_history_queried": False,
        },
        "identity_policy": {
            "trade": "genesis + network + signature + authorized transaction effects",
            "withdrawal": "genesis + network + signature + system-transfer instruction 0",
        },
        "receipts": normalized,
        "counts": {**counts, "deduplicated_rows": duplicates, "retained": len(normalized)},
        "limitations": [
            "Only locally retained Runner journal records are covered.",
            "Withdrawal rows retain finality status, not finalized balance receipts.",
            "No arbitrary external wallet history, deposits, or unrecorded transfers are discovered.",
            "No native-asset/quote-currency conversion is inferred.",
        ],
        "submission_enabled": False,
    }
    ident = _digest(core)
    return {**core, "id": ident, "reproducibility_sha256": ident}


def read_trade_journal_receipts(
    data_dir: str | Path,
    *,
    as_of: int | float,
    sender: str | None = None,
    source_label: str = "retained-local-trade-journal",
) -> dict[str, Any]:
    """Read the existing SQLite file in query-only mode; never initialize it."""
    path = Path(data_dir) / "wallet-transfers.sqlite"
    if not path.is_file():
        raise ReceiptEvidenceError("The retained trade journal does not exist")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        table_names = {
            str(row[0]) for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"trades", "transfers"} <= table_names:
            raise ReceiptEvidenceError("The retained journal is missing receipt tables")
        suffix = " WHERE sender=?" if sender is not None else ""
        params = (sender,) if sender is not None else ()
        trades = [dict(row) for row in db.execute(
            "SELECT * FROM trades" + suffix + " ORDER BY created_at,id", params
        )]
        transfers = [dict(row) for row in db.execute(
            "SELECT * FROM transfers" + suffix + " ORDER BY created_at,id", params
        )]
        linked = (
            [str(row[0]) for row in db.execute("SELECT trade_id FROM agent_fills")]
            if "agent_fills" in table_names else []
        )
    except sqlite3.Error as exc:
        raise ReceiptEvidenceError("The retained journal could not be read") from exc
    finally:
        db.close()
    return normalize_retained_receipts(
        trades, transfers, as_of=as_of, source_label=source_label, agent_fill_ids=linked
    )


def compare_receipt_expectations(
    receipt_report: dict[str, Any], expectations: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Compare exact links without converting modeled evidence to execution."""
    report = _object(receipt_report, "receipt_report")
    if report.get("schema") != REPORT_SCHEMA:
        raise ReceiptEvidenceError(f"receipt_report.schema must be {REPORT_SCHEMA}")
    receipts = {row["id"]: row for row in report.get("receipts", [])}
    comparisons = []
    seen = set()
    for raw in expectations:
        row = _object(raw, "expectation")
        ident = str(row.get("id") or "")
        if not ident or ident in seen:
            raise ReceiptEvidenceError("Expectation IDs must be non-empty and unique")
        seen.add(ident)
        evidence_class = row.get("evidence_class")
        if evidence_class not in {"observed_paper", "retrospective_scenario", "recorded_execution"}:
            raise ReceiptEvidenceError("Expectation evidence_class is unsupported")
        receipt = receipts.get(row.get("receipt_id"))
        differences = []
        if receipt is not None and receipt["classification"] in {
            "finalized_success", "fee_only_failure",
        }:
            for field in ("cash_delta_lamports", "token_delta_atoms", "fee_lamports"):
                if field in row and receipt["amounts"].get(field) != _int(row[field], field):
                    differences.append({
                        "field": field, "expected": _int(row[field], field),
                        "observed": receipt["amounts"].get(field),
                    })
            status = "matched" if not differences else "mismatch"
        elif receipt is None:
            status = "unlinked"
        else:
            status = "receipt_not_final"
        comparisons.append({
            "expectation_id": ident, "evidence_class": evidence_class,
            "receipt_id": row.get("receipt_id"), "status": status,
            "differences": differences, "comparison_only": True,
            "evidence_class_changed": False,
        })
    core = {
        "schema": COMPARISON_SCHEMA, "receipt_report_id": report.get("id"),
        "comparisons": comparisons,
        "matched": sum(row["status"] == "matched" for row in comparisons),
        "paper_fills_reclassified_as_execution": False,
    }
    ident = _digest(core)
    return {**core, "id": ident, "reproducibility_sha256": ident}
