"""Bounded public-holder discovery and unsigned Token-2022 PumpSwap verification.

Input is a saved public Pump coin-list JSON, never a wallet or key file.
No financial journal, wallet vault, or submission method is accessible.
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import time

from pumpwizard_runner.pump_route import TOKEN_2022_PROGRAM, key, read_state
from pumpwizard_runner.pump_swap_route import TOKEN_2022_ROUTE, canonical_pool
from pumpwizard_runner.route_check import RouteCheckRpc, check_routes
from pumpwizard_runner.transfers import TransferError


class PacedRpc(RouteCheckRpc):
    def call(self, method, params):
        time.sleep(0.4)
        return super().call(method, params)


class DiscoveryRpc(PacedRpc):
    allowed_methods = RouteCheckRpc.allowed_methods | {"getSignaturesForAddress", "getTransaction"}


def verify(coins, target):
    rpc = DiscoveryRpc("mainnet-beta")
    evidence = dict(
        checked_at=datetime.now(timezone.utc).isoformat(),
        network="mainnet-beta",
        genesis=rpc.genesis(),
        funded_acceptance=False,
        submission_enabled=False,
        status="incomplete",
        attempts=[],
    )

    def save():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(evidence, indent=2) + "\n")

    for coin in coins[:50]:
        mint = coin["mint"]
        attempt = dict(mint=mint)
        evidence["attempts"].append(attempt)
        try:
            state = read_state(rpc, None, mint, "sell")
            if state["route"] != TOKEN_2022_ROUTE or state["token_program"] != TOKEN_2022_PROGRAM:
                attempt["detail"] = "Outside Token-2022 canonical PumpSwap scope"
                save()
                continue
            attempt["route"] = state["route"]
            pool = canonical_pool(mint)[0]
            owners = {
                coin["creator"]: dict(kind="public_pump_creator", wallet_control_claimed=False)
            }
            signatures = rpc.call(
                "getSignaturesForAddress", [pool, {"limit": 4, "commitment": "finalized"}]
            )
            for signature in signatures:
                if signature["err"]:
                    continue
                tx = rpc.call(
                    "getTransaction",
                    [
                        signature["signature"],
                        {
                            "encoding": "json",
                            "maxSupportedTransactionVersion": 0,
                            "commitment": "finalized",
                        },
                    ],
                )
                if not tx:
                    continue
                for balance in tx["meta"]["postTokenBalances"]:
                    if (
                        balance["mint"] == mint
                        and int(balance["uiTokenAmount"]["amount"]) > 0
                        and key(balance["owner"]).is_on_curve()
                    ):
                        owners[balance["owner"]] = dict(
                            kind="public_finalized_transaction",
                            signature=signature["signature"],
                            slot=signature["slot"],
                            wallet_control_claimed=False,
                        )
            attempt["probes"] = []
            for owner, provenance in list(owners.items())[:6]:
                held = read_state(rpc, owner, mint, "sell")
                if held["token_atoms"] < 1_000_000 or held["wallet_lamports"] < 20_100_000:
                    continue
                quantity = min(held["token_atoms"], 1_000_000_000_000)
                report = check_routes(
                    PacedRpc("mainnet-beta"),
                    [mint],
                    sender=owner,
                    side="sell",
                    amount=str(Decimal(quantity) / 10**6),
                )
                report["public_sender_source"] = provenance
                attempt["probes"].append(report)
                if report["status"] == "passed":
                    result = report["results"][0]
                    result["net_floor_lamports"] = (
                        result["minimum_output"] - result["fee_lamports"] - 20_000_000
                    )
                    if result["net_floor_lamports"] > 0:
                        evidence["status"] = "passed"
                        save()
                        print("PASS", mint, owner, flush=True)
                        return
        except (TransferError, ValueError, KeyError, TypeError) as exc:
            attempt["detail"] = str(exc)
        for snapshot in reversed(rpc.snapshots):
            found = next((a for a in snapshot["accounts"] if a["address"] == mint), None)
            if found:
                attempt["mint_program"] = found["owner"]
                break
        print(mint, attempt.get("detail", "No supported funded public holder"), flush=True)
        save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coins", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    verify(json.loads(args.coins.read_text()), args.output)
