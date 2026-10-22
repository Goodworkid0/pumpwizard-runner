"""Wallet-approved capability for one bounded service-signing allowance.

The capability is deliberately narrower than production promotion.  It binds a
single already-authorized allowance to an exact packaged build and route set,
then requires the selected wallet to sign that immutable description.  Request
data alone is therefore never authority, and a capability cannot be moved to a
different session, wallet, strategy, policy, allowance, build, or expiry.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import math
import re
import secrets
import time
from typing import Mapping

from solders.signature import Signature

from .build_identity import current_build_identity
from .pump_route import key
from .service_wallet_vault import SERVICE_CUSTODY
from .transfers import GENESIS, TransferError, public_key


SCHEMA = "pumpwizard.service-acceptance-approval/v1"
DOMAIN = b"PumpWizard bounded service acceptance v1\n"
MAX_ACCEPTANCE_SECONDS = 3600
_BUILD_FIELDS = {
    "schema", "build_id", "version", "source_checkpoint", "packaged_at", "package_kind",
}
_FIELDS = {
    "schema", "approval_id", "build_identity", "custody", "sender", "network",
    "genesis", "session_id", "strategy_digest", "allowance_id",
    "authorization_revision", "allowance_binding_sha256", "routes", "approved_at",
    "expires_at",
}
_HEX_32 = re.compile(r"[0-9a-f]{32}")
_HEX_64 = re.compile(r"[0-9a-f]{64}")


def wallet_approval_available() -> bool:
    """Live authority still requires the wallet's signature for this packaged build."""
    kind = current_build_identity().get("package_kind")
    return bool(kind and kind != "source")


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(value: object) -> str:
    return hashlib.sha256(_encode(value).encode()).hexdigest()


def _json_field(value: object, name: str) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            raise TransferError(f"Allowance {name} is not valid JSON") from None
    return value


def allowance_binding_sha256(row: Mapping[str, object]) -> str:
    """Hash every immutable authority field of one durable allowance."""
    fields = (
        "id", "review_id", "session_id", "genesis", "sender", "strategy_digest",
        "policy_revision", "authorization_revision", "requested_permissions",
        "granted_permissions", "scope", "created_at", "expires_at",
    )
    try:
        bound = {name: row[name] for name in fields}
    except (KeyError, IndexError, TypeError):
        raise TransferError("Acceptance requires a complete authorized allowance") from None
    for name in ("requested_permissions", "granted_permissions", "scope"):
        bound[name] = _json_field(bound[name], name)
    return _sha256(bound)


def _validate_build(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _BUILD_FIELDS:
        raise TransferError("Acceptance approval requires the exact packaged build identity")
    if not all(isinstance(value[name], str) and value[name] for name in _BUILD_FIELDS):
        raise TransferError("Acceptance approval contains an invalid build identity")
    if value["package_kind"] == "source":
        raise TransferError("Acceptance mode requires an inspectable packaged candidate")
    current = current_build_identity()
    if value != current:
        raise TransferError("Acceptance approval belongs to another Runner build")
    return dict(value)


def _validate_payload(payload: object, *, now: float | None = None) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise TransferError("Acceptance approval fields are incomplete or unexpected")
    value = dict(payload)
    if value.get("schema") != SCHEMA:
        raise TransferError("Acceptance approval schema is unsupported")
    if not isinstance(value.get("approval_id"), str) or not _HEX_32.fullmatch(
        value["approval_id"]
    ):
        raise TransferError("Acceptance approval ID must be 32 lowercase hexadecimal characters")
    value["build_identity"] = _validate_build(value.get("build_identity"))
    if value.get("custody") != SERVICE_CUSTODY:
        raise TransferError("Acceptance approval is not for service custody")
    sender = value.get("sender")
    public_key(sender)
    network = value.get("network")
    if not isinstance(network, str) or network not in GENESIS:
        raise TransferError("Acceptance approval has an unsupported network")
    if value.get("genesis") != GENESIS[network]:
        raise TransferError("Acceptance approval network identity does not match")
    for name in ("session_id", "allowance_id"):
        if not isinstance(value.get(name), str) or not _HEX_32.fullmatch(value[name]):
            raise TransferError(f"Acceptance {name.replace('_', ' ')} is invalid")
    for name in ("strategy_digest", "allowance_binding_sha256"):
        if not isinstance(value.get(name), str) or not _HEX_64.fullmatch(value[name]):
            raise TransferError(f"Acceptance {name.replace('_', ' ')} is invalid")
    if type(value.get("authorization_revision")) is not int or value[
        "authorization_revision"
    ] < 1:
        raise TransferError("Acceptance authorization revision is invalid")
    routes = value.get("routes")
    if (
        not isinstance(routes, list)
        or not 1 <= len(routes) <= 4
        or any(not isinstance(route, str) or not route for route in routes)
        or len(set(routes)) != len(routes)
    ):
        raise TransferError("Acceptance approval requires one to four exact routes")
    if routes != sorted(routes):
        raise TransferError("Acceptance approval routes must be unique and sorted")
    for name in ("approved_at", "expires_at"):
        field = value.get(name)
        if isinstance(field, bool) or not isinstance(field, (int, float)) or not math.isfinite(field):
            raise TransferError(f"Acceptance {name.replace('_', ' ')} is invalid")
    checked_at = time.time() if now is None else now
    if value["approved_at"] > checked_at + 300:
        raise TransferError("Acceptance approval time is in the future")
    if checked_at >= value["expires_at"]:
        raise TransferError("Acceptance approval expired")
    if not 0 < value["expires_at"] - value["approved_at"] <= MAX_ACCEPTANCE_SECONDS:
        raise TransferError("Acceptance approval duration must be at most 60 minutes")
    if value["expires_at"] - checked_at > MAX_ACCEPTANCE_SECONDS:
        # A wallet-signed payload may tolerate a small forward approval-time
        # skew, but that must never extend effective authority beyond one hour
        # from the service's current clock.
        raise TransferError("Acceptance approval cannot extend beyond 60 minutes")
    return value


def acceptance_approval_message(payload: object, *, now: float | None = None) -> bytes:
    """Return the exact non-transaction message the selected wallet must sign."""
    return DOMAIN + _encode(_validate_payload(payload, now=now)).encode()


def acceptance_approval_request(
    row: Mapping[str, object],
    routes: object,
    duration_seconds: object,
    *,
    now: float | None = None,
) -> dict[str, object]:
    """Create an exact, unsigned approval request for the selected wallet."""
    if row["status"] != "authorized":
        raise TransferError("Acceptance requires a fresh authorized allowance")
    if type(duration_seconds) is not int or not 60 <= duration_seconds <= 3600:
        raise TransferError("Acceptance duration must be 1–60 minutes")
    if (
        not isinstance(routes, list)
        or not routes
        or any(not isinstance(route, str) or not route for route in routes)
    ):
        raise TransferError("Choose the exact acceptance routes")
    selected_routes = sorted(set(routes))
    if len(selected_routes) != len(routes) or len(selected_routes) > 4:
        raise TransferError("Choose one to four unique acceptance routes")
    scope = _json_field(row["scope"], "scope")
    permissions = _json_field(row["granted_permissions"], "granted_permissions")
    if not isinstance(scope, dict) or not isinstance(permissions, dict):
        raise TransferError("Acceptance allowance scope is invalid")
    if permissions.get("custody") != SERVICE_CUSTODY:
        raise TransferError("Acceptance allowance is not service custody")
    if not set(selected_routes).issubset(set(scope.get("routes", []))):
        raise TransferError("Acceptance routes are outside the authorized allowance")
    approved_at = time.time() if now is None else now
    expires_at = min(approved_at + duration_seconds, float(row["expires_at"]))
    if expires_at - approved_at < 60:
        raise TransferError("Allowance expires too soon for an acceptance run")
    payload = {
        "schema": SCHEMA,
        "approval_id": secrets.token_hex(16),
        "build_identity": current_build_identity(),
        "custody": SERVICE_CUSTODY,
        "sender": row["sender"],
        "network": scope.get("network"),
        "genesis": row["genesis"],
        "session_id": row["session_id"],
        "strategy_digest": row["strategy_digest"],
        "allowance_id": row["id"],
        "authorization_revision": row["authorization_revision"],
        "allowance_binding_sha256": allowance_binding_sha256(row),
        "routes": selected_routes,
        "approved_at": approved_at,
        "expires_at": expires_at,
    }
    message = acceptance_approval_message(payload, now=approved_at)
    return {
        "schema": "pumpwizard.service-acceptance-request/v1",
        "payload": payload,
        "message": base64.b64encode(message).decode(),
        "expires_at": expires_at,
    }


@dataclass(frozen=True)
class ServiceAcceptanceCapability:
    """Verified process-local authority for one acceptance allowance."""

    _payload_json: str
    signature: str
    message_sha256: str

    def __post_init__(self) -> None:
        try:
            payload = json.loads(self._payload_json)
        except (TypeError, json.JSONDecodeError):
            raise TransferError("Acceptance capability payload is invalid") from None
        message = acceptance_approval_message(payload)
        try:
            parsed = Signature.from_bytes(base64.b64decode(self.signature, validate=True))
        except Exception:
            raise TransferError("Acceptance approval signature is invalid") from None
        if not parsed.verify(key(str(payload["sender"])), message):
            raise TransferError(
                "Acceptance approval signature does not match the selected wallet"
            )
        if self.message_sha256 != hashlib.sha256(message).hexdigest():
            raise TransferError("Acceptance capability message identity is invalid")

    @property
    def payload(self) -> dict[str, object]:
        return json.loads(self._payload_json)

    @property
    def approval_id(self) -> str:
        return str(self.payload["approval_id"])

    @property
    def allowance_id(self) -> str:
        return str(self.payload["allowance_id"])

    @property
    def routes(self) -> tuple[str, ...]:
        return tuple(self.payload["routes"])

    def public(self) -> dict[str, object]:
        value = self.payload
        return {
            "schema": value["schema"],
            "approval_id": value["approval_id"],
            "allowance_id": value["allowance_id"],
            "session_id": value["session_id"],
            "sender": value["sender"],
            "network": value["network"],
            "routes": value["routes"],
            "expires_at": value["expires_at"],
            "build_id": value["build_identity"]["build_id"],
            "message_sha256": self.message_sha256,
        }

    def audit_record(self) -> dict[str, object]:
        return {
            "payload": self.payload,
            "signature": self.signature,
            "message_sha256": self.message_sha256,
        }

    def validate_current(self, *, now: float | None = None) -> None:
        """Recheck build identity and expiry without exposing authority fields."""
        _validate_payload(self.payload, now=now)

    def validate_allowance(
        self,
        row: Mapping[str, object],
        *,
        vault_id: str,
        now: float | None = None,
    ) -> None:
        value = _validate_payload(self.payload, now=now)
        scope = _json_field(row["scope"], "scope")
        permissions = _json_field(row["granted_permissions"], "granted_permissions")
        if not isinstance(scope, dict) or not isinstance(permissions, dict):
            raise TransferError("Acceptance allowance scope is invalid")
        expected = (
            row["id"], row["session_id"], row["sender"], row["genesis"],
            row["strategy_digest"], row["authorization_revision"],
            allowance_binding_sha256(row), scope.get("network"),
        )
        actual = (
            value["allowance_id"], value["session_id"], value["sender"], value["genesis"],
            value["strategy_digest"], value["authorization_revision"],
            value["allowance_binding_sha256"], value["network"],
        )
        if actual != expected:
            raise TransferError("Acceptance approval does not match this exact allowance")
        if permissions.get("custody") != SERVICE_CUSTODY:
            raise TransferError("Acceptance allowance is not service custody")
        if scope.get("custody_identity") != vault_id:
            raise TransferError("Acceptance approval belongs to another service vault")
        if not set(value["routes"]).issubset(set(scope.get("routes", []))):
            raise TransferError("Acceptance routes are outside the authorized allowance")
        if value["expires_at"] > row["expires_at"]:
            raise TransferError("Acceptance approval outlives its allowance")


def verify_acceptance_approval(
    payload: object,
    signature: object,
    *,
    now: float | None = None,
) -> ServiceAcceptanceCapability:
    """Verify a wallet signature and return non-serializable runtime authority."""
    message = acceptance_approval_message(payload, now=now)
    if not isinstance(signature, str):
        raise TransferError("Acceptance approval signature is missing")
    try:
        raw = base64.b64decode(signature, validate=True)
        parsed = Signature.from_bytes(raw)
    except Exception:
        raise TransferError("Acceptance approval signature is invalid") from None
    sender = str(payload["sender"])
    if not parsed.verify(key(sender), message):
        raise TransferError("Acceptance approval signature does not match the selected wallet")
    return ServiceAcceptanceCapability(
        _payload_json=_encode(payload),
        signature=signature,
        message_sha256=hashlib.sha256(message).hexdigest(),
    )


def acceptance_evidence(journal, approval_id: object) -> dict[str, object]:
    """Correlate one consumed approval with exact allowance and receipt evidence."""
    if not isinstance(approval_id, str) or not _HEX_32.fullmatch(approval_id):
        raise TransferError("Choose an exact service acceptance approval")
    approval = journal.db.execute(
        "SELECT * FROM service_acceptance_approvals WHERE approval_id=?",
        (approval_id,),
    ).fetchone()
    if approval is None:
        raise TransferError("Service acceptance approval was not found")
    allowance = journal.db.execute(
        "SELECT * FROM main_wallet_allowances WHERE id=?",
        (approval["allowance_id"],),
    ).fetchone()
    if allowance is None:
        raise TransferError("Acceptance allowance evidence is missing")
    payload = _json_field(approval["payload"], "approval payload")
    scope = _json_field(allowance["scope"], "scope")
    if not isinstance(payload, dict) or not isinstance(scope, dict):
        raise TransferError("Acceptance audit evidence is invalid")
    if set(payload) != _FIELDS or payload.get("schema") != SCHEMA:
        raise TransferError("Acceptance audit payload is invalid")
    archived_message = DOMAIN + _encode(payload).encode()
    try:
        archived_signature_bytes = base64.b64decode(approval["signature"], validate=True)
        archived_signature = Signature.from_bytes(archived_signature_bytes)
    except Exception:
        raise TransferError("Acceptance audit signature is invalid") from None
    if (
        not archived_signature.verify(key(str(payload.get("sender"))), archived_message)
        or approval["message_sha256"] != hashlib.sha256(archived_message).hexdigest()
    ):
        raise TransferError("Acceptance audit signature or message digest does not match")
    permissions = _json_field(allowance["granted_permissions"], "granted_permissions")
    if not isinstance(permissions, dict):
        raise TransferError("Acceptance audit allowance permissions are invalid")
    expected = (
        approval["approval_id"],
        approval["allowance_id"],
        allowance["id"],
        allowance["session_id"],
        allowance["sender"],
        allowance["genesis"],
        allowance["strategy_digest"],
        allowance["authorization_revision"],
        allowance_binding_sha256(allowance),
        scope.get("network"),
        SERVICE_CUSTODY,
    )
    actual = (
        payload.get("approval_id"),
        payload.get("allowance_id"),
        payload.get("allowance_id"),
        payload.get("session_id"),
        payload.get("sender"),
        payload.get("genesis"),
        payload.get("strategy_digest"),
        payload.get("authorization_revision"),
        payload.get("allowance_binding_sha256"),
        payload.get("network"),
        payload.get("custody"),
    )
    if actual != expected:
        raise TransferError("Acceptance audit allowance binding does not match")
    routes = payload.get("routes")
    if (
        permissions.get("custody") != SERVICE_CUSTODY
        or not isinstance(routes, list)
        or not set(routes).issubset(set(scope.get("routes", [])))
        or payload.get("expires_at") > allowance["expires_at"]
    ):
        raise TransferError("Acceptance audit scope no longer matches its allowance")
    orders = []
    for row in journal.db.execute(
        "SELECT o.*,t.signature,t.status AS trade_status,t.detail AS trade_detail,"
        "t.payload AS trade_payload,t.receipt FROM main_wallet_allowance_orders o "
        "LEFT JOIN trades t ON t.id=o.order_id WHERE o.allowance_id=? "
        "ORDER BY o.authorized_at,o.order_id",
        (allowance["id"],),
    ):
        review = _json_field(row["trade_payload"], "trade payload") if row[
            "trade_payload"
        ] is not None else None
        receipt = _json_field(row["receipt"], "trade receipt") if row[
            "receipt"
        ] is not None else None
        orders.append({
            "order_id": row["order_id"],
            "ticket_id": row["ticket_id"],
            "side": row["side"],
            "debit_lamports": row["debit"],
            "authorized_at": row["authorized_at"],
            "authorization_revision": row["authorization_revision"],
            "route": review.get("route") if isinstance(review, dict) else None,
            "mint": review.get("mint") if isinstance(review, dict) else None,
            "review": review,
            "signature": row["signature"],
            "status": row["trade_status"] or "reserved",
            "detail": row["trade_detail"] or "Allowance order has no trade record",
            "receipt": receipt,
        })
    unsettled = {"pending", "unknown", "confirmed", "reserved"}
    recovery_required = any(order["status"] in unsettled for order in orders)
    reconciled = bool(orders) and all(
        order["status"] in {"finalized", "failed"} and order["receipt"] is not None
        for order in orders
    )
    return {
        "schema": "pumpwizard.service-acceptance-evidence/v1",
        "generated_at": time.time(),
        "approval": {
            "payload": payload,
            "signature_sha256": hashlib.sha256(archived_signature_bytes).hexdigest(),
            "message_sha256": approval["message_sha256"],
            "activated_at": approval["activated_at"],
            "signature_verified": True,
        },
        "allowance": {
            "id": allowance["id"],
            "session_id": allowance["session_id"],
            "sender": allowance["sender"],
            "genesis": allowance["genesis"],
            "strategy_digest": allowance["strategy_digest"],
            "authorization_revision": allowance["authorization_revision"],
            "binding_sha256": allowance_binding_sha256(allowance),
            "binding_matches_approval": (
                payload.get("allowance_binding_sha256")
                == allowance_binding_sha256(allowance)
            ),
            "status": allowance["status"],
            "detail": allowance["detail"],
            "expires_at": allowance["expires_at"],
            "routes": scope.get("routes", []),
            "budget_lamports": scope.get("budget_lamports"),
            "max_order_debit_lamports": scope.get("max_order_debit_lamports"),
            "max_daily_debit_lamports": scope.get("max_daily_debit_lamports"),
            "max_orders": scope.get("max_orders"),
            "max_trades_per_day": scope.get("max_trades_per_day"),
            "max_open_positions": scope.get("max_open_positions"),
            "slippage_bps": scope.get("slippage_bps"),
            "used_orders": len(orders),
            "used_lamports": sum(int(order["debit_lamports"]) for order in orders),
        },
        "orders": orders,
        "recovery_required": recovery_required,
        "status": (
            "recovery-required"
            if recovery_required
            else "reconciled"
            if reconciled
            else "empty"
            if not orders
            else "incomplete"
        ),
    }
