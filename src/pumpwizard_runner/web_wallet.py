"""Read-only, public wallet-control summaries for the browser client."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3


def portfolio_valuations(data_dir: Path) -> list[dict]:
    """Return saved valuation summaries without refreshing RPC or quote data."""
    path = Path(data_dir) / "portfolio-risk.sqlite"
    if not path.exists():
        return []
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    db.row_factory = sqlite3.Row
    try:
        rows = []
        for row in db.execute("SELECT genesis,sender,payload FROM portfolios ORDER BY genesis,sender LIMIT 100"):
            report = json.loads(row["payload"])
            # Quotes contain the detailed local route/observation evidence used
            # by Python. The browser only needs a retained portfolio headline.
            rows.append({
                "genesis": row["genesis"],
                "sender": row["sender"],
                "status": report.get("status", "unknown"),
                "started_at": report.get("started_at"),
                "net_liquidation": report.get("net_liquidation"),
                "total_pnl": report.get("total_pnl"),
                "unrealized_pnl": report.get("unrealized_pnl"),
                "drawdown": report.get("drawdown"),
            })
        return rows
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()


def state(data_dir: Path) -> dict:
    path = Path(data_dir) / "wallet-transfers.sqlite"
    if not path.exists():
        return {"wallets": [], "policies": [], "reservations": [], "transactions": [], "signing_sessions": [],
                "portfolio_valuations": portfolio_valuations(data_dir)}
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    db.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        wallets = []
        if "trades" in tables:
            wallets = [dict(sender=row["sender"], genesis=row["genesis"])
                       for row in db.execute("SELECT DISTINCT sender,genesis FROM trades ORDER BY sender,genesis")]
        policies = []
        if "wallet_risk_policies" in tables:
            policies = [dict(sender=row["sender"], genesis=row["genesis"], policy=json.loads(row["payload"]))
                        for row in db.execute("SELECT * FROM wallet_risk_policies ORDER BY sender,genesis")]
        reservations = []
        if "agent_reservations" in tables:
            reservations = [dict(id=row["id"], agent_id=row["agent_id"], sender=row["sender"], genesis=row["genesis"],
                                 status=row["status"], created_at=row["created_at"], released_at=row["released_at"])
                            for row in db.execute("SELECT * FROM agent_reservations ORDER BY created_at DESC LIMIT 100")]
        transactions = []
        if "trades" in tables:
            transactions = [dict(id=row["id"], sender=row["sender"], genesis=row["genesis"], status=row["status"],
                                 signature=row["signature"], created_at=row["created_at"])
                            for row in db.execute("SELECT * FROM trades ORDER BY created_at DESC LIMIT 100")]
        sessions = []
        if "signing_sessions" in tables:
            sessions = [dict(id=row["id"], sender=row["sender"], genesis=row["genesis"], status=row["status"],
                                 expires_at=row["expires_at"], created_at=row["created_at"])
                        for row in db.execute("SELECT * FROM signing_sessions ORDER BY created_at DESC LIMIT 100")]
        return {"wallets": wallets, "policies": policies, "reservations": reservations,
                "transactions": transactions, "signing_sessions": sessions,
                "portfolio_valuations": portfolio_valuations(data_dir)}
    except sqlite3.OperationalError:
        return {"wallets": [], "policies": [], "reservations": [], "transactions": [], "signing_sessions": [],
                "portfolio_valuations": portfolio_valuations(data_dir)}
    finally:
        db.close()
