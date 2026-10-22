"""Real browser bridge + signed messages + retained synthetic finalized receipts."""
import base64
import json

import pytest
from solders.keypair import Keypair

from pumpwizard_runner import web_trading
from pumpwizard_runner.manual_trades import TradeJournal
from pumpwizard_runner.transfers import TransferError
from browser_trade_bank import BrowserBank, MINT
from test_reward_routes import configure_rewards


@pytest.fixture
def browser(tmp_path, monkeypatch):
    wallet = Keypair()
    address = str(wallet.pubkey())
    bank = BrowserBank(address)
    monkeypatch.setattr(web_trading, "rpc_for", lambda network, directory=None: bank)
    return tmp_path, wallet, bank


def review(browser, side="buy", amount="0.1", position=None):
    path, wallet, _ = browser
    return web_trading.preview(path, dict(sender=str(wallet.pubkey()), network="mainnet-beta", mint=MINT,
        side=side, amount=amount, slippage_bps=100, position_id=position))["preview"]


def signed_body(browser, preview, *, wallet=None):
    _, original, _ = browser
    signer = wallet or original
    raw = base64.b64decode(preview["unsigned_transaction"])
    signed = b"\x01" + bytes(signer.sign_message(raw[65:])) + raw[65:]
    return dict(sender=str(original.pubkey()), network="mainnet-beta", preview_id=preview["id"],
                signed_transaction=base64.b64encode(signed).decode())


def reconcile(browser, final=True):
    path, wallet, bank = browser
    if final:
        for signature, receipt in bank.receipts.items():
            bank.statuses[signature] = {"confirmationStatus": "finalized", "err": receipt["meta"]["err"]}
    return web_trading.reconcile(path, dict(sender=str(wallet.pubkey()), network="mainnet-beta"))


@pytest.mark.parametrize('holder', [False, True])
def test_browser_buy_partial_restart_full_sell_retains_exact_accounting(browser, holder):
    path, _, bank = browser
    configure_rewards(bank, holder=holder, fee=75, editable=True)
    initial = bank.balance
    buy = review(browser)
    assert buy["amount"] == "100000000" and buy["decimals"] == 6
    assert buy["priority_fee"] == "0" and buy["fee"] == "5000"
    assert buy["reward_mode"] == ('holders' if holder else 'creator')
    assert buy["expires_at"] > 0
    result = web_trading.submit(path, signed_body(browser, buy))
    assert result["state"] == "pending"
    assert reconcile(browser, final=False)["positions"] == []
    state = reconcile(browser)
    assert len(state["positions"]) == 1
    position = state["positions"][0]
    assert position["quantity"] == "1000000000"
    assert position["transaction_fees"] == "5000"
    assert state["orders"][0]["receipt"]["wallet_delta"] == "-102044280"
    partial = review(browser, "sell", "400", buy["id"])
    bank.disconnect = True
    submitted = web_trading.submit(path, signed_body(browser, partial))
    assert submitted["state"] == "unknown"
    state = reconcile(browser, final=False)
    assert state["pending"] and state["positions"][0]["quantity"] == "1000000000"
    with pytest.raises(TransferError, match="Resolve"):
        review(browser)
    # Every bridge call closes its journal, then reopens it just as after a restart.
    state = reconcile(browser)
    assert state["positions"][0]["quantity"] == "600000000"
    assert state["positions"][0]["remaining_cost"] == "61226568"
    assert state["positions"][0]["realized_pnl"] == "-822712"
    bank.disconnect = False
    full = review(browser, "sell", "600", buy["id"])
    web_trading.submit(path, signed_body(browser, full))
    state = reconcile(browser)
    assert reconcile(browser) == state
    closed = state["positions"][0]
    assert closed["status"] == "closed" and closed["quantity"] == closed["remaining_cost"] == "0"
    assert int(closed["realized_pnl"]) == bank.balance - initial == -2054280
    assert closed["transaction_fees"] == "15000"
    assert len(state["orders"]) == len(bank.sent) == 3 and not state["pending"]


@pytest.mark.parametrize("mutation", ["wallet", "network", "expiry", "balance", "simulation", "message", "stale", "reward_mode", "creator_fee", "editable_fee"])
def test_signed_browser_submission_revalidates_before_any_broadcast(browser, mutation):
    path, _, bank = browser
    preview = review(browser)
    body = signed_body(browser, preview)
    if mutation == "wallet":
        body = signed_body(browser, preview, wallet=Keypair())
    if mutation == "network":
        body["network"] = "devnet"
        bank.network = "devnet"
    if mutation == "expiry":
        bank.expire = True
    if mutation == "balance":
        bank.balance -= 1
    if mutation == "simulation":
        bank.bad_sim = True
    if mutation in ('reward_mode', 'creator_fee', 'editable_fee'):
        configure_rewards(bank, holder=mutation == 'reward_mode',
            fee=75 if mutation == 'creator_fee' else 0, editable=mutation == 'editable_fee')
    if mutation == "message":
        raw = bytearray(base64.b64decode(body["signed_transaction"]))
        raw[-1] ^= 1
        body["signed_transaction"] = base64.b64encode(raw).decode()
    if mutation == "stale":
        journal = TradeJournal(path)
        try:
            payload = journal.previews(preview["id"])
            payload["created_at"] -= 61
            with journal.db:
                journal.db.execute("UPDATE trade_previews SET payload=? WHERE id=?", (json.dumps(payload), preview["id"]))
        finally:
            journal.close()
    with pytest.raises(TransferError):
        web_trading.submit(path, body)
    assert bank.sent == [] and reconcile(browser)["positions"] == []


def test_duplicate_preview_and_changed_position_cannot_broadcast(browser):
    path, _, bank = browser
    buy = review(browser)
    body = signed_body(browser, buy)
    web_trading.submit(path, body)
    reconcile(browser)
    with pytest.raises(TransferError, match="already"):
        web_trading.submit(path, body)
    partial = review(browser, "sell", "400", buy["id"])
    journal = TradeJournal(path)
    try:
        with journal.db:
            journal.db.execute("UPDATE trade_positions SET remaining_cost='1' WHERE id=?", (buy["id"],))
    finally:
        journal.close()
    with pytest.raises(TransferError, match="Selected position changed"):
        web_trading.submit(path, signed_body(browser, partial))
    assert len(bank.sent) == 1


@pytest.mark.parametrize('mutation', ['account_order', 'priority_fee', 'blockhash'])
def test_valid_signature_over_changed_message_still_cannot_broadcast(browser, mutation):
    from solders.compute_budget import set_compute_unit_price
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from pumpwizard_runner.solana_message import wallet_message

    path, wallet, bank = browser
    preview = review(browser)
    original = base64.b64decode(preview['unsigned_transaction'])[65:]
    message = Message.from_bytes(original)
    h = message.header
    def writable(index):
        return (index < h.num_required_signatures - h.num_readonly_signed_accounts
                if index < h.num_required_signatures else
                index < len(message.account_keys) - h.num_readonly_unsigned_accounts)
    instructions = [Instruction(message.account_keys[ix.program_id_index], ix.data,
        [AccountMeta(message.account_keys[i], message.is_signer(i), writable(i)) for i in ix.accounts])
        for ix in message.instructions]
    blockhash = message.recent_blockhash
    if mutation == 'priority_fee':
        instructions[1] = set_compute_unit_price(375_000)
    if mutation == 'blockhash':
        blockhash = Hash.new_unique()
    rebuilt = Message.new_with_blockhash(instructions, wallet.pubkey(), blockhash)
    changed = bytes(rebuilt) if mutation == 'account_order' else wallet_message(rebuilt)
    assert changed != original
    signature = wallet.sign_message(changed)
    assert signature.verify(wallet.pubkey(), changed)
    body = signed_body(browser, preview)
    body['signed_transaction'] = base64.b64encode(b'\x01' + bytes(signature) + changed).decode()
    with pytest.raises(TransferError, match='exact reviewed transaction'):
        web_trading.submit(path, body)
    assert bank.sent == []
    journal = TradeJournal(path)
    try:
        assert journal.trades(str(wallet.pubkey())) == []
        assert journal.db.execute('SELECT count(*) FROM wallet_intents').fetchone()[0] == 0
    finally:
        journal.close()


def test_browser_migration_requires_a_fresh_review(browser):
    path, _, bank = browser
    buy = review(browser)
    web_trading.submit(path, signed_body(browser, buy))
    reconcile(browser)
    sell = review(browser, "sell", "400", buy["id"])
    bank.graduated = True
    with pytest.raises(TransferError, match="Route changed"):
        web_trading.submit(path, signed_body(browser, sell))
    fresh = review(browser, "sell", "400", buy["id"])
    assert fresh["route"].startswith("pumpswap")
    web_trading.submit(path, signed_body(browser, fresh))
    assert reconcile(browser)["positions"][0]["quantity"] == "600000000"


def test_failed_sell_has_fee_only_receipt_and_keeps_unsold_quantity(browser):
    path, _, bank = browser
    buy = review(browser)
    web_trading.submit(path, signed_body(browser, buy))
    reconcile(browser)
    sell = review(browser, "sell", "400", buy["id"])
    bank.failed = True
    web_trading.submit(path, signed_body(browser, sell))
    state = reconcile(browser)
    assert state["orders"][0]["status"] == "failed"
    assert state["orders"][0]["receipt"]["wallet_delta"] == "-5000"
    assert state["positions"][0]["quantity"] == "1000000000"
    assert state["positions"][0]["realized_pnl"] == "-5000"
    assert state["positions"][0]["transaction_fees"] == "10000"
    assert not state["pending"]


def test_browser_wallet_proof_is_single_use_revocable_and_not_restored(browser):
    _, wallet, _ = browser
    address = str(wallet.pubkey())
    authorizer = web_trading.WalletAuthorizer()
    challenge = authorizer.challenge(address, "mainnet-beta", "http://127.0.0.1:8765")
    body = dict(sender=address, network="mainnet-beta", challenge_id=challenge["challenge_id"],
                signature=base64.b64encode(bytes(wallet.sign_message(base64.b64decode(challenge["message"])))).decode())
    authorizer.verify(body, "http://127.0.0.1:8765")
    authorizer.require(address, "mainnet-beta")
    with pytest.raises(TransferError):
        authorizer.verify(body, "http://127.0.0.1:8765")
    with pytest.raises(TransferError):
        web_trading.WalletAuthorizer().require(address, "mainnet-beta")
    authorizer.revoke(address, "mainnet-beta")
    with pytest.raises(TransferError):
        authorizer.require(address, "mainnet-beta")


def test_insufficient_sol_cannot_prepare_a_browser_order(browser):
    _, _, bank = browser
    bank.balance = 100_000_001
    with pytest.raises(TransferError, match="Insufficient SOL reserve"):
        review(browser, amount="0.1")
    assert not bank.sent and not reconcile(browser)["positions"]


def test_browser_quote_expiring_at_the_wallet_lock_never_broadcasts(browser, monkeypatch):
    from pumpwizard_runner import manual_trades

    path, _, bank = browser
    preview = review(browser)
    original = TradeJournal.check_agent_reservations

    def delay_at_lock(journal, reviewed, state):
        if journal.db.in_transaction:
            monkeypatch.setattr(manual_trades.time, "time", lambda: preview["expires_at"] + 1)
        return original(journal, reviewed, state)

    monkeypatch.setattr(TradeJournal, "check_agent_reservations", delay_at_lock)
    with pytest.raises(TransferError, match="expired while waiting for the wallet lock"):
        web_trading.submit(path, signed_body(browser, preview))
    assert not bank.sent and not reconcile(browser)["pending"]


def test_position_change_at_wallet_lock_invalidates_signed_sell(browser, monkeypatch):
    path, _, bank = browser
    buy = review(browser)
    web_trading.submit(path, signed_body(browser, buy))
    reconcile(browser)
    sell = review(browser, "sell", "400", buy["id"])
    original = TradeJournal.check_agent_reservations

    def change_at_lock(journal, reviewed, state):
        if journal.db.in_transaction:
            journal.db.execute("UPDATE trade_positions SET remaining_cost='1' WHERE id=?", (buy["id"],))
        return original(journal, reviewed, state)

    monkeypatch.setattr(TradeJournal, "check_agent_reservations", change_at_lock)
    with pytest.raises(TransferError, match="Selected position changed"):
        web_trading.submit(path, signed_body(browser, sell))
    assert len(bank.sent) == 1 and not reconcile(browser)["pending"]


def test_browser_buy_cannot_attach_itself_to_another_position(browser):
    with pytest.raises(TransferError, match="buy cannot reference"):
        review(browser, position="existing-position")
