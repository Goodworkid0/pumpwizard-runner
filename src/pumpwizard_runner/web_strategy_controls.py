"""Browser API adapter for main-wallet to strategy allocation controls."""

from __future__ import annotations

from pathlib import Path

from .manual_trades import TradeJournal
from .strategy_risk import parse_browser_policy, save_policy, status
from .transfers import GENESIS, TransferError, _uint, public_key
from . import web_trading


def _identity(sender: object, network: object) -> tuple[str, str]:
    if not isinstance(sender, str) or not isinstance(network, str):
        raise TransferError("Choose a connected wallet and Solana network")
    public_key(sender)
    if network not in GENESIS:
        raise TransferError("Choose mainnet-beta or devnet")
    return sender, network


def _balance(sender: str, network: str, directory: Path) -> tuple[str, int]:
    rpc = web_trading.rpc_for(network, directory)
    genesis = rpc.genesis()
    if genesis != GENESIS[network]:
        raise TransferError("RPC network does not match the selected strategy network")
    balance = _uint(
        rpc.call("getBalance", [sender, {"commitment": "confirmed"}])["value"],
        "wallet balance",
    )
    return genesis, balance


def _entry_blocks(instance: dict, current: dict) -> list[str]:
    blocks = list(current["entry_blocks"])
    if instance["entry_mode"] == "disabled":
        blocks.append("Session entry mode is disabled")
    if instance["status"] != "monitoring":
        blocks.append("Session is not monitoring")
    return blocks


def overview(library, directory: Path, sender: object, network: object) -> dict:
    sender, network = _identity(sender, network)
    genesis, balance = _balance(sender, network, directory)
    journal = TradeJournal(Path(directory))
    try:
        controls = []
        from .workspace_scope import scoped_instance_ids
        visible = scoped_instance_ids(library, (sender, network))
        for instance in library.instances():
            if instance['id'] not in visible:
                continue
            agent_id = str(instance["id"])
            current = status(journal, agent_id, genesis, sender)
            controls.append({
                "instance": {
                    "id": agent_id,
                    "label": instance["label"],
                    "status": instance["status"],
                    "entry_mode": instance["entry_mode"],
                    "exit_mode": instance["exit_mode"],
                },
                **current,
                "entry_blocks": _entry_blocks(instance, current),
            })
        allocated = sum(int(row[0]) for row in journal.db.execute(
            "SELECT lamports FROM agent_reservations WHERE genesis=? AND sender=? "
            "AND status='held' AND position_id IS NULL",
            (genesis, sender),
        ))
        return {
            "sender": sender,
            "network": network,
            "genesis": genesis,
            "balance_lamports": str(balance),
            "allocated_lamports": str(allocated),
            "unallocated_lamports": str(max(0, balance - allocated)),
            "controls": controls,
        }
    finally:
        journal.close()


def save(library, directory: Path, agent_id: str, body: object) -> dict:
    if not isinstance(body, dict):
        raise TransferError("Strategy controls must be a JSON object")
    values = dict(body)
    sender, network = _identity(values.pop("sender", None), values.pop("network", None))
    instance = library.instance(agent_id)
    genesis, balance = _balance(sender, network, directory)
    parsed = parse_browser_policy(values)
    workspace_id = instance.get("workspace_id")
    if workspace_id is not None:
        workspace = library.session_workspace(str(workspace_id))
        if workspace["mode"] != "trade":
            raise TransferError("Monitoring sessions cannot bind wallet risk controls")
        if (workspace.get("wallet_sender"), workspace.get("wallet_network")) != (sender, network):
            raise TransferError("The strategy wallet and network must match its trading session")
    journal = TradeJournal(Path(directory))
    try:
        saved = save_policy(
            journal, agent_id, genesis, sender, parsed, balance,
            workspace_id=str(workspace_id) if workspace_id is not None else None,
        )
        current = status(journal, agent_id, genesis, sender)
    finally:
        journal.close()
    return {
        "instance": {
            "id": agent_id,
            "label": instance["label"],
            "status": instance["status"],
            "entry_mode": instance["entry_mode"],
            "exit_mode": instance["exit_mode"],
        },
        **saved,
        "metrics": current["metrics"],
        "entry_blocks": _entry_blocks(instance, current),
    }
