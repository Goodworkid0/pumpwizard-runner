"""Reviewed agent orders use synthetic bank receipts, never funded trades."""
import json
import base64
import struct
import time

import pytest

from pumpwizard_runner.agent_execution import AgentTradeService, cancel
from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.manual_trades import ManualTradeService, TradeJournal
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.reservations import release, reserve, rows
from pumpwizard_runner.strategies import parse_package
from pumpwizard_runner.strategy_risk import save_policy
from pumpwizard_runner.transfers import TransferError
from test_manual_trades import MINT, setup as _bank


@pytest.fixture
def setup(tmp_path):
    bank = _bank.__wrapped__(tmp_path)
    wallet,rpc,journal,manual,_ = next(bank)
    # Synthetic chain cap/liquidity agree with the strategy's entry band.
    raw = bytearray(base64.b64decode(rpc.bank['curve']['data'][0]))
    struct.pack_into('<Q',raw,8,10**15)
    struct.pack_into('<Q',raw,16,120_000_000_000)
    struct.pack_into('<Q',raw,32,50_000_000_000)
    struct.pack_into('<Q',raw,40,10**15)
    rpc.bank['curve']['data'][0] = base64.b64encode(raw).decode()
    library = StrategyLibrary(tmp_path)
    strategy = parse_package({'schema':'pumpwizard.strategy-handoff/v1','strategy':{'id':'core-frequency','label':'Core'},'configuration':CONFIG})
    library.import_strategy(strategy)
    agent = library.create_instance(strategy.digest,entry_mode='manual')
    now = time.time()
    for i in range(4):
        observe(library,agent,now-30+i*10,110+i,40+i)
    library.record_decisions(agent,[dict(at=now,kind='entry_recommended',mint=MINT,reason='strategy_entry_signal',payload={})])
    cash = reserve(journal,rpc,agent,wallet.address,lamports=200_000_000)
    service = AgentTradeService(library,rpc,journal)
    yield wallet,rpc,journal,library,agent,cash,service,tmp_path
    library.close()
    next(bank,None)


def observe(library,agent,at,y=115,real=50):
    library.append_observations(agent,[MarketQuote.from_record(dict(mint=MINT,symbol='TEST',observed_at=at,
        source_at=at,fx_at=at,route='pump_bonding_curve_sol',curve_address='Curve',x=1e6,y=y,real_x=5e5,
        real_y=real,supply=1e6,quote_usd=100,market_cap_usd=y*100,decimals=6)).as_record()])


def prepare_buy(setup):
    wallet,_,_,_,agent,cash,service,_ = setup
    return service.prepare(agent,wallet.address,MINT,cash['id'])


def fill_buy(setup):
    wallet,rpc,journal,_,_,_,service,_ = setup
    p = prepare_buy(setup)
    service.submit(p['id'],wallet)
    rpc.status = {'confirmationStatus':'finalized','err':None}
    service.manual.reconcile(wallet.address)
    return p,journal.position(p['id'])


def test_workspace_cash_pool_executes_and_settles_without_a_duplicate_strategy_hold(setup):
    wallet, rpc, journal, _, agent, cash, service, _ = setup
    workspace_id = "shared-workspace"
    owner_id = f"workspace:{workspace_id}"
    with journal.db:
        journal.db.execute(
            "UPDATE agent_reservations SET agent_id=? WHERE id=?", (owner_id, cash["id"])
        )
        journal.db.execute(
            "INSERT INTO session_workspace_allocations VALUES (?,?,?,?,?,?,?,NULL,'held')",
            (
                workspace_id, owner_id, rpc.genesis(), wallet.address, cash["id"],
                cash["lamports"], time.time(),
            ),
        )
    saved = save_policy(
        journal,
        agent,
        rpc.genesis(),
        wallet.address,
        {
            "allocation_lamports": 200_000_000,
            "buys_enabled": True,
            "sells_enabled": True,
            "min_trade_lamports": 1,
            "max_trade_lamports": 150_000_000,
            "max_daily_buy_lamports": 180_000_000,
            "max_trades_per_day": 4,
            "max_daily_loss_lamports": 40_000_000,
            "max_daily_loss_bps": 2_000,
            "max_trade_loss_bps": 2_000,
            "max_open_positions": 2,
        },
        rpc.balance,
        workspace_id=workspace_id,
    )
    assert saved["hold_id"] == cash["id"]
    assert journal.db.execute(
        "SELECT COUNT(*) FROM agent_reservations WHERE status='held' AND position_id IS NULL"
    ).fetchone()[0] == 1

    preview = prepare_buy(setup)
    service.submit(preview["id"], wallet)
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    service.manual.reconcile(wallet.address)

    shared = journal.db.execute(
        "SELECT * FROM agent_reservations WHERE id=?", (cash["id"],)
    ).fetchone()
    assert shared["agent_id"] == owner_id
    assert int(shared["lamports"]) < int(cash["lamports"])
    assert journal.db.execute(
        "SELECT COUNT(*) FROM agent_reservations WHERE agent_id=? AND position_id IS NULL",
        (agent,),
    ).fetchone()[0] == 0


def test_review_does_not_sign_and_finality_atomically_attributes_buy(setup):
    wallet,rpc,journal,_,agent,cash,service,_ = setup
    p = prepare_buy(setup)
    assert not rpc.sent and not journal.positions(wallet.address)
    assert rows(journal)[0]['lamports'] == cash['lamports']
    with pytest.raises(TransferError,match='Cancel'):
        release(journal,cash['id'])
    with pytest.raises(TransferError,match='workflow'):
        ManualTradeService(rpc,journal).submit(p['id'],wallet)
    service.submit(p['id'],wallet)
    assert not journal.positions(wallet.address)
    assert rows(journal)[0]['lamports'] == cash['lamports']
    rpc.status = {'confirmationStatus':'finalized','err':None}
    service.manual.reconcile(wallet.address)
    owner = journal.db.execute('SELECT * FROM agent_positions').fetchone()
    assert owner['agent_id'] == agent and owner['position_id'] == p['id']
    position = journal.position(p['id'])
    token_hold = next(r for r in rows(journal) if r['id']==owner['token_hold'])
    assert token_hold['token_atoms'] == position['quantity']
    assert int(next(r for r in rows(journal) if r['id']==cash['id'])['lamports']) == int(cash['lamports'])-int(position['original_cost'])
    service.manual.reconcile(wallet.address)
    assert journal.db.execute('SELECT COUNT(*) FROM agent_fills').fetchone()[0] == 1
    with pytest.raises(TransferError,match='already attempted'):
        prepare_buy(setup)


def test_partial_exit_pause_and_restart_then_full_exit(setup):
    wallet,rpc,journal,library,agent,cash,service,directory = setup
    p,pos = fill_buy(setup)
    observe(library,agent,time.time(),320)
    library.set_instance_status(agent,'paused')
    partial = service.prepare(agent,wallet.address,MINT,cash['id'],position_id=p['id'])
    assert partial['amount'] == int(pos['initial_quantity'])*4//10
    service.submit(partial['id'],wallet)
    service.manual.reconcile(wallet.address)
    remaining = int(journal.position(p['id'])['quantity'])
    assert remaining == int(pos['initial_quantity'])-partial['amount']
    assert json.loads(journal.db.execute('SELECT sold_stages FROM agent_positions').fetchone()[0]) == [0]
    reopened = TradeJournal(directory)
    try:
        service = AgentTradeService(library,rpc,reopened)
        observe(library,agent,time.time(),170)
        full = service.prepare(agent,wallet.address,MINT,cash['id'],position_id=p['id'])
        # The conservative net floor now reaches the stop before the spot trail.
        assert full['amount'] == remaining and full['agent_order']['reason']=='stop_loss'
        service.submit(full['id'],wallet)
        service.manual.reconcile(wallet.address)
        assert reopened.position(p['id'])['status'] == 'closed'
        assert next(r for r in rows(reopened) if r['position_id']==p['id'])['token_atoms']=='0'
        assert reopened.db.execute('SELECT COUNT(*) FROM agent_fills').fetchone()[0]==3
    finally:
        reopened.close()


def test_pause_after_review_blocks_entry_and_cancellation_preserves_hold(setup):
    wallet,rpc,journal,library,agent,cash,service,_ = setup
    p = prepare_buy(setup)
    library.set_instance_status(agent,'paused')
    with pytest.raises(TransferError,match='paused'):
        service.submit(p['id'],wallet)
    assert not rpc.sent
    cancel(journal,p['id'])
    assert rows(journal)[0]['lamports']==cash['lamports']
    release(journal,cash['id'])


def test_unknown_keeps_hold_and_attribution_waits_for_receipt_after_restart(setup):
    wallet,rpc,journal,library,_,cash,service,directory = setup
    p = prepare_buy(setup)
    rpc.disconnect = True
    service.submit(p['id'],wallet)
    assert journal.trades(wallet.address)[0]['status']=='unknown'
    reopened = TradeJournal(directory)
    try:
        rpc.expire = True
        service = AgentTradeService(library,rpc,reopened)
        service.manual.reconcile(wallet.address)
        assert rows(reopened)[0]['lamports']==cash['lamports']
        assert not reopened.positions(wallet.address)
        with pytest.raises(TransferError):
            cancel(reopened,p['id'])
        with pytest.raises(TransferError):
            release(reopened,cash['id'])
        rpc.status = {'confirmationStatus':'finalized','err':None}
        service.manual.reconcile(wallet.address)
        assert reopened.position(p['id'])['status']=='open'
        assert len(rpc.sent)==1
    finally:
        reopened.close()


def test_two_reviews_cannot_claim_same_hold_and_manual_cannot_spend_it(setup):
    wallet,rpc,journal,_,agent,cash,service,_ = setup
    p = prepare_buy(setup)
    with pytest.raises(TransferError,match='already has'):
        prepare_buy(setup)
    with pytest.raises(TransferError):
        journal.check_agent_reservations(dict(p,agent_order=dict(p['agent_order'],agent_id='other')),p['state'])
    cancel(journal,p['id'])
    again = prepare_buy(setup)
    assert again['id'] != p['id'] and again['agent_order']['agent_id']==agent


def test_failed_receipt_debits_only_fee_and_never_creates_agent_position(setup):
    wallet,rpc,journal,_,_,cash,service,_ = setup
    p = prepare_buy(setup)
    service.submit(p['id'],wallet)
    rpc.receipt['meta']['err'] = {'InstructionError':[0,'Custom']}
    rpc.receipt['meta']['postBalances'][0] = rpc.receipt['meta']['preBalances'][0]-rpc.fee
    rpc.status = {'confirmationStatus':'finalized','err':rpc.receipt['meta']['err']}
    service.manual.reconcile(wallet.address)
    assert not journal.positions(wallet.address)
    assert not journal.db.execute('SELECT 1 FROM agent_positions').fetchone()
    assert int(rows(journal)[0]['lamports'])==int(cash['lamports'])-rpc.fee
    assert journal.db.execute('SELECT status FROM agent_orders').fetchone()[0]=='failed'


def test_receipt_and_agent_hold_updates_roll_back_together(setup):
    import sqlite3
    from pumpwizard_runner.manual_trades import validate_receipt

    wallet,rpc,journal,_,_,cash,service,_ = setup
    p = prepare_buy(setup)
    service.submit(p['id'],wallet)
    row = journal.trades(wallet.address)[0]
    receipt = validate_receipt(row,rpc.receipt)
    journal.db.execute("CREATE TRIGGER fail_agent_fill BEFORE INSERT ON agent_fills BEGIN SELECT RAISE(ABORT,'test crash'); END")
    with pytest.raises(sqlite3.IntegrityError,match='test crash'):
        journal.settle(row,receipt)
    assert not journal.positions(wallet.address)
    assert rows(journal)[0]['lamports']==cash['lamports']
    assert journal.pending(wallet.address,rpc.genesis())
    assert not journal.db.execute('SELECT 1 FROM agent_positions').fetchone()
    journal.db.execute('DROP TRIGGER fail_agent_fill')
    journal.settle(row,receipt)
    journal.settle(row,receipt)
    assert journal.db.execute('SELECT COUNT(*) FROM agent_fills').fetchone()[0]==1
    assert len(journal.positions(wallet.address))==1


def test_changed_onchain_cap_or_local_decision_blocks_signing(setup):
    wallet,rpc,_,library,agent,_,service,_ = setup
    p = prepare_buy(setup)
    raw = bytearray(base64.b64decode(rpc.bank['curve']['data'][0]))
    struct.pack_into('<Q',raw,16,300_000_000_000)
    rpc.bank['curve']['data'][0]=base64.b64encode(raw).decode()
    with pytest.raises(TransferError,match='on-chain curve'):
        service.submit(p['id'],wallet)
    assert not rpc.sent
    library.record_decisions(agent,[dict(at=time.time(),mint=MINT,kind='entry_blocked',reason='changed',payload={})])
    with pytest.raises(ValueError,match='not actionable'):
        service.submit(p['id'],wallet)
    assert not rpc.sent


def test_cli_prepare_cancel_history_and_refuse_noninteractive_signing(setup,monkeypatch,capsys):
    from pumpwizard_runner import cli,manual_trades

    wallet,rpc,journal,_,agent,cash,_,directory = setup
    monkeypatch.setattr(manual_trades,'TradeRpc',lambda network,url:rpc)
    prefix = ['--data-dir',str(directory)]
    assert cli.main(prefix+['prepare-agent-order',agent,wallet.address,MINT,'--cash-reservation',cash['id']])==0
    preview = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(cli.sys.stdin,'isatty',lambda:False)
    assert cli.main(prefix+['review-agent-order',preview['id']])==2
    assert 'Interactive terminal' in capsys.readouterr().err
    assert not rpc.sent
    assert cli.main(prefix+['agent-orders',agent])==0
    assert json.loads(capsys.readouterr().out)[0]['status']=='review'
    assert cli.main(prefix+['cancel-agent-order',preview['id']])==0
    assert json.loads(capsys.readouterr().out)['status']=='cancelled'
    assert rows(journal)[0]['lamports']==cash['lamports']


def test_agent_position_tokens_are_protected_from_manual_exit(setup):
    wallet,rpc,journal,_,_,_,_,_ = setup
    p,pos = fill_buy(setup)
    with pytest.raises(TransferError,match='reserved'):
        ManualTradeService(rpc,journal).preview(wallet.address,MINT,'sell',str(int(pos['quantity'])/1e6),position_id=p['id'])


def test_unverified_configuration_is_rejected(setup):
    wallet,_,_,library,_,cash,service,_ = setup
    altered = parse_package({'schema':'pumpwizard.strategy-handoff/v1','strategy':{'id':'core-frequency','label':'Changed'},
                             'configuration':dict(CONFIG,order_usd=20)})
    library.import_strategy(altered)
    instance = library.create_instance(altered.digest,entry_mode='manual')
    with pytest.raises(TransferError,match='pinned'):
        service.prepare(instance,wallet.address,MINT,cash['id'])


def test_paused_agent_stays_scheduled_for_finalized_position_exits(setup):
    from pumpwizard_runner.scheduler import MonitorScheduler

    wallet,rpc,_,library,agent,_,_,directory = setup
    p,_ = fill_buy(setup)
    library.set_tracking(agent,False)
    scheduled = library.scheduled_instances()
    assert len(scheduled)==1 and scheduled[0]['id']==agent and scheduled[0]['held']==1
    assert library.position_views(agent)[0].position_id==p['id']
    calls = []

    class Source:
        def collect(self,low,high,*,limit,priority):
            calls.append(priority)
            at=time.time()
            return [MarketQuote(MINT,'TEST',at,'curve','Curve',32000,.032,.00032,
                                5000,50,quote_usd=100,source_at=at,fx_at=at)]

    scheduler = MonitorScheduler(directory,lambda:Source())
    try:
        assert scheduler.run_cycle(library)==1
        assert calls==[{MINT}]
        latest = library.decisions(agent)[0]
        assert latest['kind']=='partial_exit_recommended'
        assert latest['payload']['position_id']==p['id']
        assert len(rpc.sent)==1  # monitoring never signs the recommended exit
    finally:
        library.release_monitor_lease(scheduler.owner)


def test_other_agent_reservations_are_unchanged_by_receipts(setup):
    wallet,rpc,journal,_,_,_,_,_ = setup
    other = reserve(journal,rpc,'other-agent',wallet.address,lamports=100_000_000)
    fill_buy(setup)
    assert next(r for r in rows(journal) if r['id']==other['id'])==other


def test_failed_exit_keeps_tokens_and_targets_for_retry(setup):
    wallet,rpc,journal,library,agent,cash,service,_ = setup
    p,pos=fill_buy(setup)
    observe(library,agent,time.time(),320)
    exit_order=service.prepare(agent,wallet.address,MINT,cash['id'],position_id=p['id'])
    cash_before=int(next(r for r in rows(journal) if r['id']==cash['id'])['lamports'])
    service.submit(exit_order['id'],wallet)
    rpc.receipt['meta']['err']={'InstructionError':[0,'Custom']}
    rpc.receipt['meta']['postBalances'][0]=rpc.receipt['meta']['preBalances'][0]-rpc.fee
    rpc.status={'confirmationStatus':'finalized','err':rpc.receipt['meta']['err']}
    service.manual.reconcile(wallet.address)
    assert journal.position(p['id'])['quantity']==pos['quantity']
    assert json.loads(journal.db.execute('SELECT sold_stages FROM agent_positions').fetchone()[0])==[]
    assert next(r for r in rows(journal) if r['position_id']==p['id'])['token_atoms']==pos['quantity']
    assert int(next(r for r in rows(journal) if r['id']==cash['id'])['lamports'])==cash_before-rpc.fee


def test_interactive_cli_authorizes_exact_review_and_reconciles(setup,monkeypatch,capsys):
    import builtins
    import getpass
    from types import SimpleNamespace
    from pumpwizard_runner import cli,manual_trades,wallet_vault

    wallet,rpc,journal,_,_,_,_,directory=setup
    p=prepare_buy(setup)
    rpc.status={'confirmationStatus':'finalized','err':None}
    monkeypatch.setattr(manual_trades,'TradeRpc',lambda network,url:rpc)
    monkeypatch.setattr(cli.sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(builtins,'input',lambda prompt:'SIGN '+p['id'])
    monkeypatch.setattr(getpass,'getpass',lambda prompt:'fixture password')
    unlocked=[]

    def unlock(password):
        unlocked.append(password)
        return wallet

    monkeypatch.setattr(wallet_vault,'WalletVault',lambda directory:SimpleNamespace(unlock=unlock))
    assert cli.main(['--data-dir',str(directory),'review-agent-order',p['id']])==0
    output=capsys.readouterr().out
    assert 'fixture password' not in output and 'finalized' in output
    assert len(rpc.sent)==1 and len(unlocked)==1
    assert journal.db.execute('SELECT status FROM agent_orders WHERE id=?',(p['id'],)).fetchone()[0]=='finalized'
