import json
from pathlib import Path

import pytest

from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.manual_trades import TradeJournal
from pumpwizard_runner.ownership import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from pumpwizard_runner.transfers import GENESIS, TransferError
from pumpwizard_runner.wallet_portfolio import read
from pumpwizard_runner.wallet_vault import WalletVault


def address(root: Path, name: str) -> str:
    return WalletVault(root / name).create(f"{name} wallet password").address


class Rpc:
    network = "mainnet-beta"

    def __init__(self, sender, owned, stale, token_2022, accounts):
        self.sender, self.owned, self.stale, self.token_2022 = sender, owned, stale, token_2022
        self.accounts = accounts
        self.calls = []

    def genesis(self):
        return GENESIS[self.network]

    def token_account(self, account, mint, amount, decimals, program):
        return {
            "pubkey": account,
            "account": {
                "owner": program,
                "data": {"parsed": {"info": {
                    "mint": mint, "owner": self.sender,
                    "tokenAmount": {"amount": str(amount), "decimals": decimals},
                }}},
            },
        }

    def transaction(self, *, before_sol, after_sol, mint, before_tokens, after_tokens, decimals=6):
        def token(amount):
            return {"mint": mint, "owner": self.sender,
                    "uiTokenAmount": {"amount": str(amount), "decimals": decimals}}

        return {
            "transaction": {"message": {"accountKeys": [
                {"pubkey": self.sender, "signer": True, "writable": True},
            ]}},
            "meta": {
                "err": None, "fee": 5000,
                "preBalances": [before_sol], "postBalances": [after_sol],
                "preTokenBalances": [token(before_tokens)] if before_tokens else [],
                "postTokenBalances": [token(after_tokens)] if after_tokens else [],
            },
        }

    def call(self, method, params):
        self.calls.append(method)
        if method == "getBalance":
            return {"value": 1_000_000_000}
        if method == "getTokenAccountsByOwner":
            assert params[0] == self.sender
            program = params[1]["programId"]
            values = []
            if program == TOKEN_PROGRAM:
                values = [
                    self.token_account(self.accounts[0], self.owned, 100, 6, program),
                    self.token_account(self.accounts[1], self.owned, 200, 6, program),
                    {"pubkey": "bad", "account": {}},
                ]
            elif program == TOKEN_2022_PROGRAM:
                values = [self.token_account(self.accounts[2], self.token_2022, 5, 0, program)]
            return {"context": {"slot": 50}, "value": values}
        if method == "getSignaturesForAddress":
            assert params[1]["limit"] == 2
            return [
                {"signature": "external-sell", "slot": 49, "blockTime": 900,
                 "confirmationStatus": "finalized", "err": None},
                {"signature": "runner-buy", "slot": 48, "blockTime": 800,
                 "confirmationStatus": "finalized", "err": None},
            ]
        if method == "getTransaction" and params[0] == "external-sell":
            return self.transaction(before_sol=10_000, after_sol=20_000, mint=self.stale,
                                    before_tokens=50, after_tokens=0)
        if method == "getTransaction" and params[0] == "runner-buy":
            return self.transaction(before_sol=20_000, after_sol=10_000, mint=self.owned,
                                    before_tokens=0, after_tokens=300)
        raise AssertionError((method, params))


class HighSlotRpc(Rpc):
    def call(self, method, params):
        result = super().call(method, params)
        if method == "getTokenAccountsByOwner":
            result["context"]["slot"] = 6_002
        return result


class PriceSource:
    def sol_usd_price(self):
        return 150.25


class UnavailablePriceSource:
    def sol_usd_price(self):
        raise OSError("public quote unavailable")


class TokenProfileSource(PriceSource):
    def __init__(self, mint):
        self.mint = mint

    def token_profiles(self, mints):
        assert self.mint in mints
        return {self.mint: {
            "price_usd": 0.5, "price_block_id": 40, "liquidity_usd": 3_500,
            "market_cap_usd": 500, "price_change_24h": -12.5,
            "volume_change_24h": -100, "holder_count": 2, "organic_score": 0,
            "organic_score_label": "low", "suspicious": False,
            "top_holders_percent": 100, "created_at": 700,
        }}


class LikelyWorthlessSource(TokenProfileSource):
    def token_profiles(self, mints):
        result = super().token_profiles(mints)
        result[self.mint]["liquidity_usd"] = 100
        return result


@pytest.fixture
def portfolio(tmp_path):
    sender = address(tmp_path, "sender")
    owned = address(tmp_path, "owned-mint")
    stale = address(tmp_path, "stale-mint")
    token_2022 = address(tmp_path, "token-2022-mint")
    accounts = [address(tmp_path, f"account-{index}") for index in range(3)]
    journal = TradeJournal(tmp_path)
    try:
        payload = json.dumps({"network": "mainnet-beta", "side": "buy", "mint": owned,
                              "amount": 1, "decimals": 6, "route": "test"})
        receipt = json.dumps({"slot": 48, "block_time": 800, "cost_basis": "test",
                              "fee_lamports": 5000, "wallet_delta": -10_000,
                              "token_delta": 300})
        with journal.db:
            journal.db.execute(
                "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
                ("position-one", GENESIS["mainnet-beta"], sender, "runner-buy", "finalized",
                 800, payload, "finalized", receipt),
            )
            journal.db.execute(
                "INSERT INTO trade_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("position-one", GENESIS["mainnet-beta"], sender, owned, 800, "300", "300",
                 "10000", "10000", "0", "0", "open", "runner-buy"),
            )
            journal.db.execute(
                "INSERT INTO trade_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("position-stale", GENESIS["mainnet-beta"], sender, stale, 801, "50", "50",
                 "5000", "5000", "0", "0", "open", "external-sell"),
            )
    finally:
        journal.close()
    library = StrategyLibrary(tmp_path)
    library.record_coin_metadata(owned, {
        "symbol": "OWN", "name": "Owned token",
        "website": "https://example.com/owned",
        "image_url": "https://cdn.dexscreener.com/owned.png",
    }, updated_at=900)
    rpc = Rpc(sender, owned, stale, token_2022, accounts)
    yield tmp_path, library, rpc, sender, owned, stale, token_2022
    library.close()


def test_wallet_portfolio_lists_both_token_programs_and_reconciles_positions(portfolio):
    directory, library, rpc, sender, owned, stale, token_2022 = portfolio
    result = read(directory, library, sender, "mainnet-beta", limit=2, refresh=True, rpc=rpc,
                  market_source=PriceSource())
    assert result["balance"] == "1000000000"
    assert result["sol_usd_price"] == 150.25
    assert result["estimated_usd"] == 150.25
    assert [(row["mint"], row["amount"], row["accounts"], row["program"]) for row in result["assets"]] == [
        (owned, "300", 2, "SPL Token"),
        (token_2022, "5", 1, "Token-2022"),
    ]
    reconciled = {row["mint"]: row for row in result["reconciliations"]}
    assert reconciled[owned]["status"] == "matched" and reconciled[owned]["sellable"]
    assert reconciled[stale] == {
        "mint": stale, "runner_atoms": "50", "wallet_atoms": "0",
        "difference_atoms": "-50", "position_ids": ["position-stale"],
        "status": "externally closed", "sellable": False,
    }
    assert result["assets"][1]["untracked_atoms"] == "5"
    assert {key: result["assets"][0][key] for key in ("symbol", "name", "website", "image_url")} == {
        "symbol": "OWN", "name": "Owned token",
        "website": "https://example.com/owned",
        "image_url": "https://cdn.dexscreener.com/owned.png",
    }
    assert result["coverage_gaps"] == ["One SPL Token account could not be decoded and was omitted"]
    assert rpc.calls.count("getTokenAccountsByOwner") == 2


def test_wallet_activity_separates_external_and_runner_transactions(portfolio):
    directory, library, rpc, sender, owned, stale, _ = portfolio
    activity = read(directory, library, sender, "mainnet-beta", limit=2, refresh=True, rpc=rpc)["activity"]
    assert activity[0] == {
        "signature": "external-sell", "slot": 49, "block_time": 900,
        "confirmation_status": "finalized", "error": None,
        "source": "External wallet activity", "runner_record": None,
        "wallet_signer": True, "sol_delta": "10000", "fee": "5000",
        "token_deltas": [{"mint": stale, "decimals": 6, "amount": "-50"}],
        "kind": "likely sell", "detail_available": True,
    }
    assert activity[1]["source"] == "Runner"
    assert activity[1]["runner_record"] == {"kind": "trade", "id": "position-one", "status": "finalized"}
    assert activity[1]["kind"] == "likely buy"
    assert activity[1]["token_deltas"] == [{"mint": owned, "decimals": 6, "amount": "300"}]


def test_wallet_portfolio_rejects_unbounded_history(portfolio):
    directory, library, rpc, sender, *_ = portfolio
    with pytest.raises(TransferError, match="1–50"):
        read(directory, library, sender, "mainnet-beta", limit=51, refresh=True, rpc=rpc)


def test_wallet_portfolio_keeps_holdings_when_sol_quote_is_unavailable(portfolio):
    directory, library, rpc, sender, owned, *_ = portfolio
    result = read(directory, library, sender, "mainnet-beta", limit=2, refresh=True, rpc=rpc,
                  market_source=UnavailablePriceSource())
    assert result["balance"] == "1000000000"
    assert result["sol_usd_price"] is None
    assert result["estimated_usd"] is None
    assert result["assets"][0]["mint"] == owned


def test_wallet_portfolio_values_held_tokens_and_exposes_market_quality(portfolio):
    directory, library, rpc, sender, owned, *_ = portfolio
    result = read(directory, library, sender, "mainnet-beta", limit=2, refresh=True, rpc=rpc,
                  market_source=TokenProfileSource(owned))
    asset = next(row for row in result["assets"] if row["mint"] == owned)
    assert asset["price_usd"] == "0.5"
    assert asset["estimated_value_usd"] == "0.00015"
    assert asset["price_slot_lag"] == 10
    assert asset["price_change_24h"] == -12.5
    assert asset["liquidity_usd"] == 3_500
    assert asset["holder_count"] == 2
    assert asset["organic_score_label"] == "low"
    assert asset["top_holders_percent"] == 100
    assert asset["created_at"] == 700
    assert asset["valuation_state"] == "illiquid"
    assert "reported liquidity" in asset["valuation_detail"]
    assert asset["valuation"]["mark"]["freshness"] == "fresh"
    assert asset["valuation"]["mark"]["kind"] == "retained_estimate"
    assert asset["valuation"]["current_value"]["amount"] == "0.00015"
    assert asset["valuation"]["basis"]["state"] == "unknown"
    assert asset["valuation"]["executable_liquidation"]["state"] == "unavailable"


def test_wallet_portfolio_calls_dust_likely_worthless_only_with_multiple_negative_signals(portfolio):
    directory, library, rpc, sender, owned, *_ = portfolio
    result = read(directory, library, sender, "mainnet-beta", limit=2, refresh=True, rpc=rpc,
                  market_source=LikelyWorthlessSource(owned))
    asset = next(row for row in result["assets"] if row["mint"] == owned)
    assert asset["estimated_value_usd"] == "0.00015"
    assert asset["valuation_state"] == "likely-worthless"
    assert "Under $0.01 held value" in asset["valuation_detail"]


def test_wallet_portfolio_withholds_current_value_when_profile_mark_is_stale(portfolio):
    directory, library, rpc, sender, owned, *_ = portfolio
    stale_rpc = HighSlotRpc(sender, rpc.owned, rpc.stale, rpc.token_2022, rpc.accounts)
    result = read(
        directory, library, sender, "mainnet-beta", limit=2, refresh=True,
        rpc=stale_rpc, market_source=TokenProfileSource(owned),
    )
    asset = next(row for row in result["assets"] if row["mint"] == owned)
    assert asset["price_slot_lag"] == 5_962
    assert asset["estimated_value_usd"] is None
    assert asset["valuation_state"] == "stale"
    assert asset["valuation"]["mark"]["price"] == "0.5"
    assert asset["valuation"]["current_value"]["state"] == "unknown"


def test_public_inventory_contains_balances_without_journal_or_history(portfolio, monkeypatch):
    from pumpwizard_runner.wallet_portfolio import inventory
    root, library, rpc, sender, owned, stale, token_2022 = portfolio
    def forbidden(*args, **kwargs):
        raise AssertionError("Public balances must not open the private trade journal")
    monkeypatch.setattr("pumpwizard_runner.wallet_portfolio.TradeJournal", forbidden)
    result = inventory(root, library, sender, "mainnet-beta", rpc=rpc, market_source=PriceSource())
    assert result["balance"] == "1000000000"
    assert result["sol_usd_price"] == 150.25
    assert {row["mint"]: row["amount"] for row in result["assets"]} == {owned: "300", token_2022: "5"}
    assert result["assets"][0]["symbol"] == "OWN"
    assert result["assets"][1]["program"] == "Token-2022"
    assert result["coverage_gaps"]
    assert "activity" not in result and "reconciliations" not in result
    assert rpc.calls == ["getBalance", "getTokenAccountsByOwner", "getTokenAccountsByOwner"]
    result["assets"].clear()
    assert len(inventory(root, library, sender, "mainnet-beta", rpc=rpc)["assets"]) == 2
    assert len(rpc.calls) == 3
    refreshed = inventory(root, library, sender, "mainnet-beta", refresh=True, rpc=rpc, market_source=UnavailablePriceSource())
    assert refreshed["sol_usd_price"] is None
    assert len(rpc.calls) == 6


def test_inventory_rejects_wrong_network_and_rpc_failure(portfolio):
    from pumpwizard_runner.wallet_portfolio import inventory
    root, library, rpc, sender, *_ = portfolio
    with pytest.raises(TransferError, match="identity"):
        inventory(root, library, sender, "devnet", rpc=rpc)
    assert rpc.calls == []
    def broken(method, params):
        raise TransferError("RPC unavailable")
    rpc.call = broken
    with pytest.raises(TransferError, match="RPC unavailable"):
        inventory(root, library, sender, "mainnet-beta", rpc=rpc)


def test_inventory_reports_confirmed_empty_wallet(portfolio):
    from pumpwizard_runner.wallet_portfolio import inventory
    root, library, rpc, sender, *_ = portfolio
    rpc.call = lambda method, params: {"value": 0} if method == "getBalance" else {"context": {"slot": 20}, "value": []}
    result = inventory(root, library, sender, "mainnet-beta", rpc=rpc)
    assert result["balance"] == "0"
    assert result["assets"] == [] and result["coverage_gaps"] == []
