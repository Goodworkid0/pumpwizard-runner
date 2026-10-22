"""One durable wallet allocation shared by the strategies in a session workspace."""

from __future__ import annotations

import time
import uuid

from .transfers import TransferError, _uint


SCHEMA = """
CREATE TABLE IF NOT EXISTS session_wallet_funding (
    genesis TEXT NOT NULL, sender TEXT NOT NULL,
    state TEXT NOT NULL, balance_lamports TEXT,
    required_lamports TEXT NOT NULL, shortfall_lamports TEXT NOT NULL,
    checked_at REAL NOT NULL, paused_at REAL, detail TEXT NOT NULL,
    PRIMARY KEY(genesis,sender)
);
CREATE TABLE IF NOT EXISTS session_workspace_allocations (
    workspace_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL UNIQUE,
    genesis TEXT NOT NULL,
    sender TEXT NOT NULL,
    cash_hold TEXT NOT NULL UNIQUE,
    allocation_lamports TEXT NOT NULL,
    created_at REAL NOT NULL,
    released_at REAL,
    status TEXT NOT NULL CHECK(status IN ('held','released'))
);
CREATE TABLE IF NOT EXISTS strategy_workspace_allocations (
    agent_id TEXT NOT NULL,
    genesis TEXT NOT NULL,
    sender TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    cash_hold TEXT NOT NULL,
    bound_at REAL NOT NULL,
    PRIMARY KEY(agent_id,genesis,sender)
);
CREATE INDEX IF NOT EXISTS strategy_workspace_allocation_owner
    ON strategy_workspace_allocations(workspace_id,genesis,sender);
CREATE TABLE IF NOT EXISTS session_workspace_allocation_changes (
    id INTEGER PRIMARY KEY, workspace_id TEXT NOT NULL,
    old_allocation_lamports TEXT NOT NULL, new_allocation_lamports TEXT NOT NULL,
    old_cash_lamports TEXT NOT NULL, new_cash_lamports TEXT NOT NULL,
    changed_at REAL NOT NULL
);
"""


def install(journal) -> None:
    journal.db.executescript(SCHEMA)


def _owner_id(workspace_id: str) -> str:
    return f"workspace:{workspace_id}"


def allocation(journal, workspace_id: str) -> dict | None:
    row = journal.db.execute(
        "SELECT * FROM session_workspace_allocations WHERE workspace_id=?", (workspace_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def summary(journal, workspace_id: str) -> dict:
    current = allocation(journal, workspace_id)
    if current is None:
        return {
            "available_lamports": "0",
            "committed_lamports": "0",
            "attributed_open_cost_lamports": "0",
            "allocation_status": "none",
        }
    hold = journal.db.execute(
        "SELECT lamports,status FROM agent_reservations WHERE id=?", (current["cash_hold"],)
    ).fetchone()
    committed = journal.db.execute(
        "SELECT COALESCE(SUM(CAST(position.remaining_cost AS INTEGER)),0) "
        "FROM agent_positions AS owned "
        "JOIN trade_positions AS position ON position.id=owned.position_id "
        "JOIN agent_reservations AS tokens ON tokens.id=owned.token_hold "
        "JOIN strategy_workspace_allocations AS binding "
        "ON binding.agent_id=owned.agent_id AND binding.genesis=position.genesis "
        "AND binding.sender=position.sender "
        "WHERE binding.workspace_id=? AND position.status='open' AND tokens.status='held'",
        (workspace_id,),
    ).fetchone()[0]
    attributed = journal.db.execute(
        "SELECT COALESCE(SUM(CAST(position.remaining_cost AS INTEGER)),0) "
        "FROM agent_positions AS owned "
        "JOIN trade_positions AS position ON position.id=owned.position_id "
        "JOIN strategy_workspace_allocations AS binding "
        "ON binding.agent_id=owned.agent_id AND binding.genesis=position.genesis "
        "AND binding.sender=position.sender "
        "WHERE binding.workspace_id=? AND position.status='open'",
        (workspace_id,),
    ).fetchone()[0]
    available = int(hold["lamports"]) if hold is not None and hold["status"] == "held" else 0
    return {
        "available_lamports": str(available),
        "committed_lamports": str(int(committed)),
        "attributed_open_cost_lamports": str(int(attributed)),
        "allocation_status": str(current["status"]),
    }


def binding(journal, agent_id: str, genesis: str, sender: str) -> dict | None:
    row = journal.db.execute(
        "SELECT * FROM strategy_workspace_allocations "
        "WHERE agent_id=? AND genesis=? AND sender=?",
        (agent_id, genesis, sender),
    ).fetchone()
    return dict(row) if row is not None else None


def cash_owner(journal, agent_id: str, genesis: str, sender: str) -> str:
    current = binding(journal, agent_id, genesis, sender)
    return str(current["owner_id"]) if current is not None else agent_id


def cash_holds(journal, agent_id: str, genesis: str, sender: str) -> list:
    owner = cash_owner(journal, agent_id, genesis, sender)
    return list(journal.db.execute(
        "SELECT * FROM agent_reservations WHERE agent_id=? AND genesis=? AND sender=? "
        "AND status='held' AND position_id IS NULL ORDER BY created_at,id",
        (owner, genesis, sender),
    ))


def reserve_workspace(
    journal,
    workspace_id: str,
    genesis: str,
    sender: str,
    lamports: int,
    balance: int,
) -> dict:
    """Reserve a workspace pool once under the same lock as all wallet writers."""
    from .reservations import require_available

    if not isinstance(workspace_id, str) or not workspace_id:
        raise TransferError("Choose a trading session")
    _uint(lamports, "session allocation")
    _uint(balance, "wallet balance")
    if lamports <= 0:
        raise TransferError("Trading session allocation must be greater than zero")
    owner = _owner_id(workspace_id)
    now = time.time()
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        if journal.pending(sender, genesis):
            raise TransferError("Resolve the pending wallet operation before allocating this session")
        if allocation(journal, workspace_id) is not None:
            raise TransferError("This trading session already has a wallet allocation")
        require_available(journal, genesis, sender, balance, lamports)
        hold_id = uuid.uuid4().hex
        journal.db.execute(
            "INSERT INTO agent_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (hold_id, owner, genesis, sender, str(lamports), None, None, "0", now, None, "held"),
        )
        journal.db.execute(
            "INSERT INTO session_workspace_allocations VALUES (?,?,?,?,?,?,?,NULL,'held')",
            (workspace_id, owner, genesis, sender, hold_id, str(lamports), now),
        )
        journal.db.execute(
            "INSERT INTO wallet_revisions VALUES (?,?,1) ON CONFLICT(genesis,sender) "
            "DO UPDATE SET revision=revision+1",
            (genesis, sender),
        )
    return dict(allocation(journal, workspace_id) or {})


def bind_strategy(
    journal,
    workspace_id: str,
    agent_id: str,
    genesis: str,
    sender: str,
) -> dict:
    """Bind a strategy policy to its workspace cash pool without another reservation."""
    current = allocation(journal, workspace_id)
    if current is None or current["status"] != "held":
        raise TransferError("The trading session wallet allocation is missing or released")
    if (current["genesis"], current["sender"]) != (genesis, sender):
        raise TransferError("The strategy wallet and network must match its trading session")
    hold = journal.db.execute(
        "SELECT * FROM agent_reservations WHERE id=?", (current["cash_hold"],)
    ).fetchone()
    if hold is None or (
        hold["agent_id"], hold["genesis"], hold["sender"], hold["status"], hold["position_id"]
    ) != (current["owner_id"], genesis, sender, "held", None):
        raise TransferError("The trading session cash allocation changed or is unavailable")
    previous = binding(journal, agent_id, genesis, sender)
    if previous is not None and (
        previous["workspace_id"], previous["owner_id"], previous["cash_hold"]
    ) != (workspace_id, current["owner_id"], current["cash_hold"]):
        raise TransferError("This strategy is already bound to another session allocation")
    journal.db.execute(
        "INSERT OR REPLACE INTO strategy_workspace_allocations VALUES (?,?,?,?,?,?,?)",
        (
            agent_id, genesis, sender, workspace_id, current["owner_id"],
            current["cash_hold"], time.time(),
        ),
    )
    return current


def unbind_strategy(journal, agent_id: str) -> bool:
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        rows = list(journal.db.execute(
            "SELECT * FROM strategy_workspace_allocations WHERE agent_id=?", (agent_id,)
        ))
        if not rows:
            return False
        if journal.db.execute(
            "SELECT 1 FROM agent_orders WHERE agent_id=? AND status IN ('review','submitted') LIMIT 1",
            (agent_id,),
        ).fetchone():
            raise TransferError("Finish or cancel this strategy's active order before removing it")
        if journal.db.execute(
            "SELECT 1 FROM agent_positions AS owned "
            "JOIN trade_positions AS position ON position.id=owned.position_id "
            "JOIN agent_reservations AS tokens ON tokens.id=owned.token_hold "
            "WHERE owned.agent_id=? AND position.status='open' "
            "AND tokens.status='held' LIMIT 1",
            (agent_id,),
        ).fetchone():
            raise TransferError(
                "Close or explicitly hand back this strategy's managed position before removing it"
            )
        if journal.db.execute(
            "SELECT 1 FROM signing_sessions WHERE agent_id=? AND status='active' LIMIT 1",
            (agent_id,),
        ).fetchone():
            raise TransferError("Revoke this strategy's signing session before removing it")
        has_allowances = journal.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='main_wallet_allowances'"
        ).fetchone()
        if has_allowances and journal.db.execute(
            "SELECT 1 FROM main_wallet_allowances WHERE session_id=? "
            "AND status IN ('authorized','active','entry-paused') LIMIT 1",
            (agent_id,),
        ).fetchone():
            raise TransferError("Revoke this strategy's main-wallet allowance before removing it")
        journal.db.execute(
            "DELETE FROM strategy_workspace_allocations WHERE agent_id=?", (agent_id,)
        )
        for genesis, sender in {(row["genesis"], row["sender"]) for row in rows}:
            journal.db.execute(
                "INSERT INTO wallet_revisions VALUES (?,?,1) ON CONFLICT(genesis,sender) "
                "DO UPDATE SET revision=revision+1",
                (genesis, sender),
            )
    return True


def release_workspace(journal, workspace_id: str) -> int:
    """Release an unused workspace allocation; assigned sessions are checked by the caller."""
    current = allocation(journal, workspace_id)
    if current is None or current["status"] == "released":
        return 0
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        current = allocation(journal, workspace_id)
        if current is None or current["status"] == "released":
            return 0
        if journal.pending(current["sender"], current["genesis"]):
            raise TransferError("Resolve the pending wallet operation before releasing this session")
        if journal.db.execute(
            "SELECT 1 FROM strategy_workspace_allocations WHERE workspace_id=? LIMIT 1",
            (workspace_id,),
        ).fetchone():
            raise TransferError("Remove this session's strategies before releasing its allocation")
        if journal.db.execute(
            "SELECT 1 FROM agent_orders WHERE cash_hold=? AND status IN ('review','submitted') LIMIT 1",
            (current["cash_hold"],),
        ).fetchone():
            raise TransferError("Finish or cancel this session's active order before releasing it")
        has_allowances = journal.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='main_wallet_allowances'"
        ).fetchone()
        if has_allowances and journal.db.execute(
            "SELECT 1 FROM main_wallet_allowances "
            "WHERE json_extract(scope,'$.cash_hold')=? "
            "AND status IN ('authorized','active','entry-paused') LIMIT 1",
            (current["cash_hold"],),
        ).fetchone():
            raise TransferError("Revoke this session's main-wallet allowance before releasing it")
        hold = journal.db.execute(
            "SELECT agent_id,genesis,sender,status,position_id "
            "FROM agent_reservations WHERE id=?",
            (current["cash_hold"],),
        ).fetchone()
        if hold is None or (
            hold["agent_id"], hold["genesis"], hold["sender"],
            hold["status"], hold["position_id"],
        ) != (
            current["owner_id"], current["genesis"], current["sender"],
            "held", None,
        ):
            raise TransferError("The trading session cash allocation changed or is unavailable")
        now = time.time()
        journal.db.execute(
            "UPDATE agent_reservations SET status='released',released_at=? "
            "WHERE id=? AND status='held'",
            (now, current["cash_hold"]),
        )
        journal.db.execute(
            "UPDATE session_workspace_allocations SET status='released',released_at=? "
            "WHERE workspace_id=?",
            (now, workspace_id),
        )
        journal.db.execute(
            "INSERT INTO wallet_revisions VALUES (?,?,1) ON CONFLICT(genesis,sender) "
            "DO UPDATE SET revision=revision+1",
            (current["genesis"], current["sender"]),
        )
    return int(current["allocation_lamports"])
