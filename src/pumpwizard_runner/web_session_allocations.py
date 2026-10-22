"""Browser adapter for durable trading-session wallet allocations."""

from __future__ import annotations

from pathlib import Path
import json
import time

from .manual_trades import TradeJournal
from .session_allocations import release_workspace, reserve_workspace
from .transfers import GENESIS, TransferError, _uint, parse_sol, public_key
from . import web_trading


def funds(library, directory: Path, workspace: dict) -> dict:
    """Read the wallet balance and reservation snapshot for an allocation edit."""
    from .reservations import wallet_revision
    from .session_allocations import allocation, summary
    from .web_strategy_controls import _balance
    if workspace['mode'] != 'trade':
        raise TransferError('Monitoring sessions do not hold trading funds')
    sender, network = workspace['wallet_sender'], workspace['wallet_network']
    genesis, balance = _balance(sender, network, directory)
    journal = TradeJournal(directory)
    try:
        with journal.db:
            journal.db.execute('BEGIN IMMEDIATE')
            current = allocation(journal, workspace['id'])
            if not current or current['status'] != 'held':
                raise TransferError('This session has no active fund allocation')
            available = summary(journal, workspace['id'])
            held = sum(int(row[0]) for row in journal.db.execute(
                "SELECT lamports FROM agent_reservations WHERE genesis=? AND sender=? AND status='held'",
                (genesis, sender)))
            free = max(0, balance - held)
            return dict(workspace_id=workspace['id'], allocation_lamports=current['allocation_lamports'],
                        **available, balance_lamports=str(balance), unallocated_lamports=str(free),
                        maximum_allocation_lamports=str(int(current['allocation_lamports']) + free),
                        wallet_revision=wallet_revision(journal, genesis, sender))
    finally:
        journal.close()


def update(library, directory: Path, workspace: dict, body: dict) -> dict:
    """Resize the cash pool and inactive policy caps under the wallet writer lock.

    Library metadata joins this transaction so normal errors roll back both
    stores. Existing orders, receipts, positions and signing scopes stay intact.
    """
    from .reservations import require_revision, wallet_revision
    from .session_allocations import allocation, summary
    from .strategy_risk import validate
    if set(body) != {'allocation_sol', 'expected_allocation_lamports', 'wallet_revision'}:
        raise TransferError('Supply the new allocation and its reviewed wallet snapshot')
    new = parse_sol(body['allocation_sol'])
    expected = body['expected_allocation_lamports']
    if not isinstance(expected, str) or not expected.isdigit() or type(body['wallet_revision']) is not int:
        raise TransferError('Reload the session fund snapshot before saving')
    if workspace['mode'] != 'trade':
        raise TransferError('Monitoring sessions do not hold trading funds')
    sender, network = workspace['wallet_sender'], workspace['wallet_network']
    genesis = GENESIS[network]
    journal = TradeJournal(directory)
    try:
        journal.db.execute('ATTACH DATABASE ? AS library_store', (str(library.path),))
        with journal.db:
            journal.db.execute('BEGIN IMMEDIATE')
            current = allocation(journal, workspace['id'])
            if not current or current['status'] != 'held' or (current['sender'], current['genesis']) != (sender, genesis):
                raise TransferError('This session has no matching active fund allocation')
            if current['allocation_lamports'] != expected:
                raise TransferError('Session funds changed. Refresh before saving')
            require_revision(journal, genesis, sender, body['wallet_revision'])
            if journal.pending(sender, genesis):
                raise TransferError('Wait for the original wallet transaction before changing funds')
            ids = [r[0] for r in journal.db.execute(
                'SELECT a.instance_id FROM library_store.session_workspace_instances a '
                'LEFT JOIN library_store.removed_instances r ON r.instance_id=a.instance_id '
                'WHERE a.workspace_id=? AND r.instance_id IS NULL', (workspace['id'],))]
            for ident in ids:
                row = journal.db.execute(
                    'SELECT i.status,i.entry_mode,i.exit_mode,COALESCE(m.enabled,0) AS enabled '
                    'FROM library_store.instances i LEFT JOIN library_store.monitor_settings m ON m.instance_id=i.id '
                    'WHERE i.id=?', (ident,)).fetchone()
                if row['status'] != 'paused' or row['enabled'] or row['entry_mode'] == 'automatic' or row['exit_mode'] == 'automatic':
                    raise TransferError('Pause all session strategies before changing funds')
                if journal.db.execute("SELECT 1 FROM signing_sessions WHERE agent_id=? AND status='active'", (ident,)).fetchone():
                    raise TransferError('Lock session signing before changing funds')
            if journal.db.execute("SELECT 1 FROM agent_orders WHERE cash_hold=? AND status IN ('review','submitted')", (current['cash_hold'],)).fetchone():
                raise TransferError('Finish or cancel the session order before changing funds')
            has_allowances = journal.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='main_wallet_allowances'").fetchone()
            if has_allowances and journal.db.execute(
                "SELECT 1 FROM main_wallet_allowances WHERE json_extract(scope,'$.cash_hold')=? "
                "AND status IN ('authorized','active','entry-paused')", (current['cash_hold'],)).fetchone():
                raise TransferError('Lock the session trading allowance before changing funds')
            hold = journal.db.execute('SELECT * FROM agent_reservations WHERE id=?', (current['cash_hold'],)).fetchone()
            if not hold or (hold['agent_id'], hold['genesis'], hold['sender'], hold['status'], hold['position_id']) != (current['owner_id'], genesis, sender, 'held', None):
                raise TransferError('The session cash reservation is unavailable')
            delta = new - int(current['allocation_lamports'])
            if delta == 0:
                raise TransferError('Choose a different session allocation')
            cash = int(hold['lamports']) + delta
            if cash < 0:
                raise TransferError('Only available session cash can be released; funds already spent or in positions stay committed')
            rpc = web_trading.rpc_for(network, directory)
            if rpc.genesis() != genesis:
                raise TransferError('RPC network does not match the selected wallet')
            balance = _uint(rpc.call('getBalance', [sender, {'commitment': 'confirmed'}])['value'], 'wallet balance')
            held = sum(int(r[0]) for r in journal.db.execute(
                "SELECT lamports FROM agent_reservations WHERE genesis=? AND sender=? AND status='held'", (genesis, sender)))
            if held + delta > balance:
                raise TransferError('The allocation exceeds unallocated SOL in the current wallet')
            now = time.time()
            for ident in ids:
                row = journal.db.execute('SELECT payload FROM strategy_risk_policies WHERE agent_id=? AND genesis=? AND sender=?', (ident, genesis, sender)).fetchone()
                if row:
                    policy = json.loads(row[0])
                    # Expand only full-session caps; retain any deliberately smaller strategy cap.
                    policy['allocation_lamports'] = new if policy['allocation_lamports'] == int(expected) else min(new, policy['allocation_lamports'])
                    for key in ('max_trade_lamports', 'max_daily_buy_lamports', 'max_daily_loss_lamports'):
                        policy[key] = min(policy[key], policy['allocation_lamports'])
                    encoded = json.dumps(validate(policy), sort_keys=True, separators=(',', ':'))
                    journal.db.execute('UPDATE strategy_risk_policies SET payload=?,updated_at=? WHERE agent_id=? AND genesis=? AND sender=?', (encoded, now, ident, genesis, sender))
                    journal.db.execute('INSERT INTO strategy_risk_changes(agent_id,genesis,sender,payload,changed_at) VALUES (?,?,?,?,?)', (ident, genesis, sender, encoded, now))
            plan_row = journal.db.execute('SELECT plan_json FROM library_store.session_workspaces WHERE id=?', (workspace['id'],)).fetchone()
            plan = json.loads(plan_row[0])
            plan['allocation_sol'] = format_sol(new)
            for key in ('max_trade_sol', 'max_daily_buy_sol', 'max_daily_loss_sol'):
                if key in plan:
                    plan[key] = format_sol(min(parse_sol(plan[key]), new))
            journal.db.execute('UPDATE agent_reservations SET lamports=? WHERE id=?', (str(cash), current['cash_hold']))
            journal.db.execute('UPDATE session_workspace_allocations SET allocation_lamports=? WHERE workspace_id=?', (str(new), workspace['id']))
            journal.db.execute('INSERT INTO session_workspace_allocation_changes(workspace_id,old_allocation_lamports,new_allocation_lamports,old_cash_lamports,new_cash_lamports,changed_at) VALUES (?,?,?,?,?,?)',
                               (workspace['id'], expected, str(new), hold['lamports'], str(cash), now))
            journal.db.execute('UPDATE library_store.session_workspaces SET allocation_lamports=?,plan_json=? WHERE id=?', (str(new), json.dumps(plan), workspace['id']))
            journal.db.execute('INSERT INTO wallet_revisions VALUES (?,?,1) ON CONFLICT(genesis,sender) DO UPDATE SET revision=revision+1', (genesis, sender))
            result = dict(workspace_id=workspace['id'], allocation_lamports=str(new), **summary(journal, workspace['id']),
                          wallet_revision=wallet_revision(journal, genesis, sender))
        return result
    finally:
        journal.close()


def format_sol(lamports: int) -> str:
    whole, fraction = divmod(lamports, 10**9)
    return f'{whole}.{fraction:09d}'.rstrip('0').rstrip('.')


def _funding(value: object) -> tuple[str, str, int]:
    if not isinstance(value, dict) or set(value) != {"sender", "network", "allocation_sol"}:
        raise TransferError("Trading session funding requires wallet, network, and allocation")
    sender, network = value["sender"], value["network"]
    if not isinstance(sender, str) or not isinstance(network, str):
        raise TransferError("Choose a connected wallet and Solana network")
    public_key(sender)
    if network not in GENESIS:
        raise TransferError("Choose mainnet-beta or devnet")
    return sender, network, parse_sol(value["allocation_sol"])


def create(library, directory: Path, *, label: object, funding: object, plan: object) -> dict:
    sender, network, lamports = _funding(funding)
    if label is not None and not isinstance(label, str):
        raise TransferError("Session label must be text")
    if not isinstance(plan, dict):
        raise TransferError("Trading session plan must be an object")
    if plan.get("session_mode") != "trade" or plan.get("allocation_sol") != funding["allocation_sol"]:
        raise TransferError("Trading session plan must match its exact wallet allocation")
    if plan.get("execution_style") not in {"interactive", "autopilot"}:
        raise TransferError("Trading session requires interactive or Autopilot execution")

    rpc = web_trading.rpc_for(network, directory)
    genesis = rpc.genesis()
    if genesis != GENESIS[network]:
        raise TransferError("RPC network does not match the selected strategy network")
    balance = _uint(
        rpc.call("getBalance", [sender, {"commitment": "confirmed"}])["value"],
        "wallet balance",
    )
    workspace = library.create_session_workspace(
        "trade",
        label,
        allocation_lamports=str(lamports),
        wallet_sender=sender,
        wallet_network=network,
        plan=plan,
    )
    journal = TradeJournal(Path(directory))
    try:
        reserved = reserve_workspace(journal, str(workspace["id"]), genesis, sender, lamports, balance)
    except Exception:
        library.remove_empty_session_workspace(str(workspace["id"]))
        raise
    finally:
        journal.close()
    return {
        "workspace": workspace,
        "allocation": reserved,
        "balance_lamports": str(balance),
        "unallocated_lamports": str(balance - lamports),
    }


def release_empty(library, directory: Path, workspace_id: str) -> int:
    workspace = library.session_workspace(workspace_id)
    if workspace["mode"] != "trade":
        return 0
    journal = TradeJournal(Path(directory))
    try:
        return release_workspace(journal, workspace_id)
    finally:
        journal.close()
