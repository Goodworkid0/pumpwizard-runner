"""Exact-unit, durable agent holds in the shared wallet journal.

Holds are allocations, not authorization to sign. They never expire automatically.
Reviewed agent orders bind them until finalized receipt accounting; ordinary
manual execution can use their resources only after explicit release.
"""
from __future__ import annotations

import time
import uuid

from .transfers import TransferError, _uint, public_key


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_reservations (
    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, genesis TEXT NOT NULL,
    sender TEXT NOT NULL, lamports TEXT NOT NULL, position_id TEXT,
    mint TEXT, token_atoms TEXT NOT NULL, created_at REAL NOT NULL,
    released_at REAL, status TEXT NOT NULL CHECK(status IN ('held','released'))
);
CREATE INDEX IF NOT EXISTS held_agent_resources
    ON agent_reservations(genesis,sender,status);
CREATE TABLE IF NOT EXISTS wallet_revisions (
    genesis TEXT NOT NULL, sender TEXT NOT NULL, revision INTEGER NOT NULL,
    PRIMARY KEY(genesis,sender)
);
CREATE TRIGGER IF NOT EXISTS wallet_intent_added AFTER INSERT ON wallet_intents
BEGIN
    INSERT INTO wallet_revisions VALUES (NEW.genesis,NEW.sender,1)
    ON CONFLICT(genesis,sender) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER IF NOT EXISTS wallet_intent_removed AFTER DELETE ON wallet_intents
BEGIN
    INSERT INTO wallet_revisions VALUES (OLD.genesis,OLD.sender,1)
    ON CONFLICT(genesis,sender) DO UPDATE SET revision=revision+1;
END;
"""


def wallet_revision(journal, genesis, sender):
    row = journal.db.execute("SELECT revision FROM wallet_revisions WHERE genesis=? AND sender=?",
                             (genesis, sender)).fetchone()
    return row[0] if row else 0


def require_revision(journal, genesis, sender, expected):
    if expected is not None and wallet_revision(journal, genesis, sender) != expected:
        raise TransferError("Another wallet operation changed the balance snapshot; review again")


def rows(journal, sender=None):
    query = "SELECT * FROM agent_reservations"
    args = ()
    if sender is not None:
        query += " WHERE sender=?"
        args = (sender,)
    return [dict(row) for row in journal.db.execute(query + " ORDER BY created_at,id", args)]


def require_available(journal, genesis, sender, balance, debit, *, mint=None,
                      tokens=0, sell=0, position_id=None, position_quantity=0):
    """Caller takes BEGIN IMMEDIATE when this check guards a journal mutation."""
    for value in (balance, debit, tokens, sell, position_quantity):
        _uint(value, "wallet allocation amount")
    held = [r for r in rows(journal, sender) if r['genesis'] == genesis and r['status'] == 'held']
    if debit + sum(int(r['lamports']) for r in held) > balance:
        raise TransferError("SOL is reserved for agents or the wallet balance is below its reservations")
    if mint is not None and sell + sum(int(r['token_atoms']) for r in held if r['mint'] == mint) > tokens:
        raise TransferError("Tokens are reserved for agents or the wallet balance is below its reservations")
    if position_id is not None and sell + sum(
        int(r['token_atoms']) for r in held if r['position_id'] == position_id
    ) > position_quantity:
        raise TransferError("Position quantity is reserved for agents")


def _idle(journal, genesis, sender):
    if journal.db.execute("SELECT 1 FROM wallet_intents WHERE genesis=? AND sender=?",
                          (genesis, sender)).fetchone():
        raise TransferError("Resolve the pending wallet operation before changing agent reservations")


def reserve(journal, rpc, agent_id, sender, *, lamports=0, position_id=None, token_atoms=0):
    """Read current balances under the same SQLite lock as all wallet writers.

    No cached snapshot can be reused after another local wallet operation. The
    on-chain balance is still revalidated by execution; external wallets are
    outside this process's lock.
    """
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise TransferError("Select an agent instance")
    public_key(sender)
    _uint(lamports, "reserved lamports")
    _uint(token_atoms, "reserved token atoms")
    if (not lamports and not token_atoms) or bool(position_id) != bool(token_atoms):
        raise TransferError("Reserve positive SOL or tokens from a confirmed position")
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        genesis = rpc.genesis()
        _idle(journal, genesis, sender)
        mint, tokens, quantity = None, 0, 0
        if position_id:
            from .pump_route import read_state

            position = journal.position(position_id)
            if (position['sender'], position['genesis'], position['status']) != (sender, genesis, 'open'):
                raise TransferError("Position does not belong to this wallet and network or is closed")
            mint, quantity = position['mint'], int(position['quantity'])
            state = read_state(rpc, sender, mint, 'sell')
            balance, tokens = state['wallet_lamports'], state['token_atoms']
            recorded = sum(int(p['quantity']) for p in journal.positions(sender, genesis)
                           if p['mint'] == mint and p['status'] == 'open')
            if recorded > tokens:
                raise TransferError("Wallet tokens are below recorded holdings")
        else:
            balance = _uint(rpc.call('getBalance', [sender, {'commitment': 'confirmed'}])['value'], 'balance')
        require_available(journal, genesis, sender, balance, lamports, mint=mint, tokens=tokens,
                          sell=token_atoms, position_id=position_id, position_quantity=quantity)
        ident = uuid.uuid4().hex
        journal.db.execute("INSERT INTO agent_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (ident, agent_id, genesis, sender, str(lamports), position_id, mint,
                            str(token_atoms), time.time(), None, 'held'))
    return next(r for r in rows(journal, sender) if r['id'] == ident)


def release(journal, ident):
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        row = journal.db.execute("SELECT * FROM agent_reservations WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise TransferError("Unknown agent reservation")
        if row['status'] == 'held':
            _idle(journal, row['genesis'], row['sender'])
            if journal.db.execute("SELECT 1 FROM agent_orders WHERE (cash_hold=? OR token_hold=?) AND status IN ('review','submitted')",
                                  (ident,ident)).fetchone():
                raise TransferError('Cancel the unsigned agent review before releasing its reservation')
            journal.db.execute("UPDATE agent_reservations SET status='released',released_at=? WHERE id=?",
                               (time.time(), ident))
    return dict(journal.db.execute("SELECT * FROM agent_reservations WHERE id=?", (ident,)).fetchone())
