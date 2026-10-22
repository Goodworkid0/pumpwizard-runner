import base64
import struct

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.transaction import Transaction

from pumpwizard_runner import cli
from pumpwizard_runner.pump_route import PUMP_PROGRAM, build_message, read_state, unsigned_transaction
from pumpwizard_runner.route_check import ProbeRpcError, RouteCheckRpc, check_routes
from pumpwizard_runner.transfers import TransferError, TransferRpc
from pumpwizard_runner.wallet_vault import WalletVault
from test_manual_trades import MINT, Rpc, account, token
from pumpwizard_runner.transfers import SYSTEM_PROGRAM


@pytest.fixture
def probe(tmp_path, monkeypatch):
    wallet = WalletVault(tmp_path / "throwaway").create("probe fixture password")
    bank = Rpc(wallet)
    calls = []

    def transport(self, method, params):
        calls.append(method)
        if method == "getGenesisHash":
            return "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
        if method == "getMultipleAccounts":
            if params[0][0] == PUMP_PROGRAM:
                return {"context": {"slot": 20}, "value": [
                    {"executable": True, "data": ["", "base64"], "owner": "loader"}
                    for _ in params[0]
                ]}
            if len(params[0]) == 3:
                return {"context": {"slot": 20}, "value": [
                    bank.bank["global"], bank.bank["curve"], bank.bank["mint_account"],
                ]}
        return bank.call(method, params)

    monkeypatch.setattr(TransferRpc, "call", transport)
    return wallet, bank, RouteCheckRpc("mainnet-beta"), calls


def test_read_only_program_and_market_inspection(probe):
    _, bank, rpc, calls = probe
    report = check_routes(rpc, [MINT])
    assert report["status"] == "passed"
    assert report["mode"] == "account_inspection"
    assert report["results"][0]["status"] == "accounts_supported"
    assert not report["funded_acceptance"] and not report["submission_enabled"]
    assert not bank.sent and "simulateTransaction" not in calls


def test_probe_reuses_unsigned_quote_checks_without_positions_or_signing(probe):
    wallet, bank, rpc, calls = probe
    report = check_routes(rpc, [MINT], sender=wallet.address, amount="0.1")
    assert report["status"] == "passed"
    result = report["results"][0]
    assert result["status"] == "unsigned_simulation_passed"
    assert result["minimum_output"] == 990_000_000 and result["fee_lamports"] == 5000
    assert len(report["simulations"]) == 2
    assert not bank.sent and "sendTransaction" not in calls


def test_simulation_failure_is_never_a_success(probe):
    wallet, bank, rpc, _ = probe
    bank.bad_sim = True
    report = check_routes(rpc, [MINT], sender=wallet.address, amount="0.1")
    assert report["status"] == "incomplete"
    assert report["results"][0]["status"] == "simulation_failed"
    assert report["results"][0]["sender"] == wallet.address
    assert report["results"][0]["amount"] == 100_000_000
    assert report["simulations"][0]["err"] == "slippage"


def test_unsupported_token_is_reported_without_simulating(probe):
    wallet, bank, rpc, calls = probe
    bank.bank["mint_account"]["owner"] = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    report = check_routes(rpc, [MINT], sender=wallet.address, amount="0.1")
    assert report["results"][0]["status"] == "account_check_failed"
    assert "Token-2022" in report["results"][0]["detail"]
    assert "simulateTransaction" not in calls


def test_network_mismatch_is_not_route_evidence(monkeypatch):
    monkeypatch.setattr(TransferRpc, "call", lambda *_: "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG")
    report = check_routes(RouteCheckRpc("mainnet-beta"), [MINT])
    assert report["status"] == "network_check_failed" and not report["results"]


def test_rpc_failure_detail_never_exposes_endpoint_or_provider_credentials(probe, monkeypatch):
    _, _, rpc, _ = probe
    def broken(*_):
        raise TransferError("https://example.org/private-provider-key")
    monkeypatch.setattr(TransferRpc, "call", broken)
    report = check_routes(rpc, [MINT])
    assert report["status"] == "network_check_failed"
    assert "private-provider-key" not in str(report)


def test_probe_refuses_broadcast_methods_before_transport(probe):
    _, _, rpc, calls = probe
    for method in ("sendTransaction", "requestAirdrop", "sendBundle"):
        with pytest.raises(ProbeRpcError, match="public reads"):
            rpc.call(method, [])
    assert not calls


def test_probe_refuses_signed_transaction(probe):
    wallet, bank, rpc, calls = probe
    state = read_state(bank, wallet.address, MINT)
    message = build_message(state, "buy", 1000, 1, str(Hash.default()))
    tx = Transaction.populate(Message.from_bytes(message), [Keypair().sign_message(message)])
    with pytest.raises(ProbeRpcError, match="signed"):
        rpc.call("simulateTransaction", [base64.b64encode(bytes(tx)).decode(), {"sigVerify": False}])
    assert not calls
    assert bytes(Transaction.from_bytes(unsigned_transaction(message)).signatures[0]) == bytes(64)


def test_cli_does_not_open_user_database_or_vault(probe, tmp_path, capsys):
    directory = tmp_path / "must-not-exist"
    assert cli.main(["--data-dir", str(directory), "route-check", MINT]) == 0
    assert "accounts_supported" in capsys.readouterr().out
    assert not directory.exists()


@pytest.mark.parametrize("kwargs", [{"sender": str(Keypair().pubkey())}, {"amount": "0.1"}])
def test_simulation_requires_amount_and_public_sender_together(probe, kwargs):
    _, _, rpc, _ = probe
    with pytest.raises(TransferError, match="both"):
        check_routes(rpc, [MINT], **kwargs)


def test_expired_probe_is_not_passing_evidence(probe):
    wallet, bank, rpc, _ = probe
    bank.expire = True
    assert check_routes(rpc, [MINT], sender=wallet.address, amount="0.1")["status"] == "incomplete"


def test_malformed_program_reply_is_reported(monkeypatch):
    def transport(self, method, params):
        return ("5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d" if method == "getGenesisHash"
                else {"context": {"slot": 20}, "value": []})
    monkeypatch.setattr(TransferRpc, "call", transport)
    assert check_routes(RouteCheckRpc("mainnet-beta"), [MINT])["status"] == "network_check_failed"


@pytest.mark.parametrize("failure", [None, "simulation", "tokens", "cash", "expiry"])
def test_round_trip_is_unsigned_and_reports_failure_instead_of_buy_only_success(probe, monkeypatch, failure):
    wallet, bank, rpc, calls = probe
    original = bank.call
    round_trips = []

    def transport(method, params):
        if method == "getBlockHeight" and round_trips and failure == "expiry":
            return 101
        if method == "simulateTransaction":
            tx = Transaction.from_bytes(base64.b64decode(params[0]))
            swaps = [i for i in tx.message.instructions
                     if str(tx.message.account_keys[i.program_id_index]) == PUMP_PROGRAM]
            if len(swaps) == 2:
                assert all(bytes(s) == bytes(64) for s in tx.signatures)
                assert params[1]["sigVerify"] is False
                assert len(tx.message.instructions) == 5
                assert bytes(swaps[0].data)[:8] == bytes([194, 171, 28, 70, 104, 77, 91, 47])
                assert bytes(swaps[1].data)[:8] != bytes(swaps[0].data)[:8]
                spend, _ = struct.unpack("<QQ", bytes(swaps[0].data)[8:])
                sold, minimum = struct.unpack("<QQ", bytes(swaps[1].data)[8:])
                assert minimum == 1 and sold == spend * 9
                round_trips.append(tx)
                cash = -spend + sold // 10 - bank.fee - bank.rent
                remaining = spend * 10 - sold
                if failure == "tokens":
                    remaining = 0
                if failure == "cash":
                    cash = -bank.balance
                return {"context": {"slot": 20}, "value": {
                    "err": "failed" if failure == "simulation" else None,
                    "accounts": [account(SYSTEM_PROGRAM, lamports=bank.balance + cash),
                                 token(wallet.address, remaining)],
                }}
        return original(method, params)

    monkeypatch.setattr(bank, "call", transport)
    report = check_routes(rpc, [MINT], sender=wallet.address, amount="0.1", round_trip=True)
    assert len(round_trips) == 1 and not bank.sent and "sendTransaction" not in calls
    assert report["mode"] == "unsigned_round_trip"
    if failure:
        assert report["status"] == "incomplete"
        assert report["results"][0]["status"] == "simulation_failed"
    else:
        assert report["status"] == "passed"
        assert report["results"][0]["round_trip"]["retained_token_atoms"] == 100_000_000
        assert report["results"][0]["round_trip"]["status"] == "unsigned_buy_then_sell_passed"


def test_round_trip_requires_buy_and_public_sender(probe):
    wallet, _, rpc, _ = probe
    with pytest.raises(TransferError, match="Round-trip"):
        check_routes(rpc, [MINT], round_trip=True)
    with pytest.raises(TransferError, match="Round-trip"):
        check_routes(rpc, [MINT], sender=wallet.address, side="sell", amount="1", round_trip=True)
