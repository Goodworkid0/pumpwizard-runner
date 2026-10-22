"""Unsigned execution-risk evidence; RPC banks and wallets are synthetic."""
import base64
import copy
import json
import sqlite3
import struct
import time

import pytest

from pumpwizard_runner.execution_risk import RiskStore, net_values
from pumpwizard_runner.shadow import UnsignedRpc, run_shadow, shadow_context
from pumpwizard_runner.transfers import TransferError
from test_agent_execution import MINT, fill_buy, observe, setup as _agent_setup
from test_pump_swap_route import SwapRpc


@pytest.fixture(name='setup')
def agent_fixture(tmp_path):
    yield from _agent_setup.__wrapped__(tmp_path)


def snapshot(setup):
    _,_,journal,library,_,_,_,_=setup
    return (list(journal.db.iterdump()),list(library._connection.iterdump()))


def shadow(setup,**kwargs):
    wallet,rpc,_,_,agent,cash,_,directory=setup
    return run_shadow(directory,rpc,agent,wallet.address,cash['id'],**kwargs)


def scale_sells(rpc,factor):
    original=rpc.effects
    rpc.effects=lambda amount,buy: original(amount,buy) if buy else (amount*factor//10-rpc.fee,-amount)


def test_entry_shadow_is_unsigned_and_leaves_all_financial_and_strategy_state_unchanged(setup,monkeypatch):
    from pumpwizard_runner.wallet_vault import WalletVault

    before=snapshot(setup)
    def forbidden(*args,**kwargs):
        pytest.fail('Shadow opened a wallet vault')
    monkeypatch.setattr(WalletVault,'unlock',forbidden)
    result=shadow(setup,mint=MINT)
    assert result['results'][0]['status']=='ready'
    assert not result['submission_enabled']
    from pumpwizard_runner.strategies import content_hash

    package = setup[3].instance(setup[4])['package']
    assert result['identity']['package_sha256'] == content_hash(package)
    assert result['finished_at'] >= result['at']
    local_input = result['results'][0]['local_input']
    assert local_input['observation_sha256'] == content_hash(local_input['observation'])
    assert result['results'][0]['proposal']['side']=='buy'
    assert [r['method'] for r in result['results'][0]['rpc_evidence']].count('simulateTransaction')==2
    assert snapshot(setup)==before and not setup[1].sent
    store=RiskStore(setup[-1])
    try:
        assert store.reports(setup[4])[0]['report']['results']==result['results']
    finally:
        store.close()


@pytest.mark.parametrize('failure', ['stale','simulation','balance','liquidity'])
def test_shadow_records_why_candidate_is_blocked_without_mutations(setup,monkeypatch,failure):
    rpc=setup[1]
    if failure=='stale':
        now=time.time()
        monkeypatch.setattr('pumpwizard_runner.agent_execution.time.time',lambda:now+40)
    elif failure=='simulation':
        rpc.bad_sim=True
    elif failure=='balance':
        rpc.balance=1000
    else:
        raw=bytearray(base64.b64decode(rpc.bank['curve']['data'][0]))
        struct.pack_into('<Q',raw,32,1000)
        rpc.bank['curve']['data'][0]=base64.b64encode(raw).decode()
    before=snapshot(setup)
    item=shadow(setup,mint=MINT)['results'][0]
    assert item['status']=='blocked' and item['reason']
    if failure=='simulation':
        assert any(x.get('err') for x in item['rpc_evidence'])
    assert snapshot(setup)==before and not rpc.sent


def test_net_stop_uses_wallet_cash_and_conservative_slippage_floor_not_spot(setup):
    wallet,rpc,journal,library,agent,_,_,_=setup
    p,pos=fill_buy(setup)
    observe(library,agent,time.time(),115)
    original=rpc.effects
    rpc.effects=lambda amount,buy: original(amount,buy) if buy else (amount//20-rpc.fee,-amount)
    before=snapshot(setup)
    item=shadow(setup,mint=MINT,position_id=p['id'])['results'][0]
    assert item['status']=='ready' and item['reason']=='stop_loss'
    risk=item['risk']
    expected=int(pos['quantity'])//20-rpc.fee
    assert risk['expected_net_lamports']==expected
    assert risk['net_floor_lamports']==(expected+rpc.fee)*9900//10000-rpc.fee-20_000_000
    assert float(risk['net_floor_usd']) < float(risk['remaining_cost_usd'])*.7
    assert int(pos['quantity'])/1e6*.0115 > int(pos['remaining_cost'])/1e9*100  # spot alone shows profit
    assert snapshot(setup)==before and len(rpc.sent)==1
    assert journal.position(p['id'])['quantity']==pos['quantity']


def test_hold_still_records_fresh_full_position_evidence(setup):
    p,_=fill_buy(setup)
    before=snapshot(setup)
    item=shadow(setup)['results'][0]
    assert item['position_id']==p['id'] and item['status']=='hold'
    assert item['risk']['net_floor_lamports']>0
    assert snapshot(setup)==before


def test_nonpositive_net_floor_blocks_even_when_expected_sale_cash_is_positive(setup):
    p,_=fill_buy(setup)
    rpc=setup[1]
    original=rpc.effects
    rpc.effects=lambda amount,buy: original(amount,buy) if buy else (10_000_000,-amount)
    item=shadow(setup,mint=MINT,position_id=p['id'])['results'][0]
    assert item['status']=='blocked' and 'net exit floor is not positive' in item['reason']
    assert len(rpc.sent)==1


def test_net_trailing_peak_survives_restart_and_does_not_need_spot_drop(setup):
    wallet,rpc,journal,library,agent,cash,service,_=setup
    p,_=fill_buy(setup)
    scale_sells(rpc,3)
    observe(library,agent,time.time(),320)
    partial=service.prepare(agent,wallet.address,MINT,cash['id'],position_id=p['id'])
    service.submit(partial['id'],wallet)
    service.manual.reconcile(wallet.address)
    assert json.loads(journal.db.execute('SELECT sold_stages FROM agent_positions').fetchone()[0])==[0]
    # The local spot quote remains at its peak. Executable sale value falls.
    scale_sells(rpc,2)
    before=snapshot(setup)
    item=shadow(setup,mint=MINT,position_id=p['id'])['results'][0]
    assert item['status']=='ready' and item['reason']=='trailing_exit'
    assert float(item['risk']['net_floor_usd']) > float(item['risk']['remaining_cost_usd'])
    again=shadow(setup,mint=MINT,position_id=p['id'])['results'][0]
    assert again['reason']=='trailing_exit'
    assert again['risk']['peak_net_usd_per_atom']==item['risk']['peak_net_usd_per_atom']
    assert snapshot(setup)==before and len(rpc.sent)==2


@pytest.mark.parametrize('failure',[None,'missing_pool','old_slot'])
def test_shadow_migration_exit_and_failure_evidence(setup,failure):
    wallet,rpc,_,library,agent,cash,_,directory=setup
    p,_=fill_buy(setup)
    swap=SwapRpc(wallet)
    swap.bank=copy.deepcopy(rpc.bank)
    swap.balance,swap.tokens=rpc.balance,rpc.tokens
    swap.graduated=True
    if failure:
        setattr(swap,failure,True)
    observe(library,agent,time.time(),320)
    before=snapshot(setup)
    item=run_shadow(directory,swap,agent,wallet.address,cash['id'],mint=MINT,position_id=p['id'])['results'][0]
    if failure:
        assert item['status']=='blocked'
    else:
        assert item['status']=='ready' and item['proposal']['route'].startswith('pumpswap-')
        assert item['risk']['route']==item['proposal']['route']
    assert snapshot(setup)==before and not swap.sent


def test_readonly_connections_and_signer_boundaries_are_enforced(setup):
    wallet,rpc,_,_,_,_,_,directory=setup
    with shadow_context(directory,rpc) as (service,_):
        with pytest.raises(sqlite3.OperationalError,match='readonly'):
            service.journal.db.execute("DELETE FROM agent_reservations")
        with pytest.raises(sqlite3.OperationalError,match='readonly'):
            service.library._connection.execute('DELETE FROM decisions')
        for method in (service.submit,service.manual.submit,service.prepare):
            with pytest.raises(TransferError,match='Shadow mode'):
                method('anything',wallet)
        with pytest.raises(TransferError,match='public reads'):
            service.rpc.call('sendTransaction',[])
    assert not rpc.sent


def test_unsigned_rpc_rejects_signed_simulation(setup):
    from pumpwizard_runner.pump_route import unsigned_transaction
    from pumpwizard_runner.transfers import transfer_message
    from pumpwizard_runner.localnet import _encode58

    wallet,rpc,*_=setup
    message=transfer_message(wallet.address,_encode58(bytes([7])*32),1000,_encode58(bytes([3])*32))
    raw=bytearray(unsigned_transaction(message))
    raw[1]=1
    with pytest.raises(TransferError,match='rejects signed'):
        UnsignedRpc(rpc).call('simulateTransaction',[base64.b64encode(raw).decode(),{'sigVerify':False}])


def test_changed_executable_trigger_rejects_review_before_signing(setup):
    wallet,rpc,_,library,agent,cash,service,_=setup
    p,_=fill_buy(setup)
    observe(library,agent,time.time(),115)
    original=rpc.effects
    rpc.effects=lambda amount,buy: original(amount,buy) if buy else (amount//20-rpc.fee,-amount)
    exit_order=service.prepare(agent,wallet.address,MINT,cash['id'],position_id=p['id'])
    assert exit_order['agent_order']['reason']=='stop_loss'
    scale_sells(rpc,2)
    with pytest.raises(TransferError,match='trigger changed'):
        service.submit(exit_order['id'],wallet)
    assert len(rpc.sent)==1


def test_net_quote_expiry_is_rejected():
    with pytest.raises(TransferError,match='fresh'):
        net_values({'side':'sell','created_at':time.time()-61})


def test_shadow_cli_and_history_do_not_create_orders(setup,monkeypatch,capsys):
    from pumpwizard_runner import cli

    wallet,rpc,_,_,agent,cash,_,directory=setup
    monkeypatch.setattr('pumpwizard_runner.route_check.RouteCheckRpc',lambda *args:rpc)
    before=snapshot(setup)
    assert cli.main(['--data-dir',str(directory),'shadow-agent',agent,wallet.address,
                     '--cash-reservation',cash['id'],'--mint',MINT])==0
    report=json.loads(capsys.readouterr().out)
    assert report['results'][0]['status']=='ready'
    assert cli.main(['--data-dir',str(directory),'shadow-reports',agent])==0
    history=json.loads(capsys.readouterr().out)
    assert history[0]['id']==report['report_id']
    assert snapshot(setup)==before and not rpc.sent


def test_shadow_missing_data_does_not_create_application_ledgers(tmp_path):
    with pytest.raises(TransferError,match='existing local'):
        with shadow_context(tmp_path,object()):
            pytest.fail('Missing library was accepted')
    assert list(tmp_path.iterdir())==[]


def test_shadow_rejects_old_schema_without_migrating(setup):
    library=setup[3]
    with library._connection:
        library._connection.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    before=snapshot(setup)
    with pytest.raises(TransferError,match='upgrade its strategy library'):
        shadow(setup,mint=MINT)
    assert snapshot(setup)==before and not setup[1].sent


def test_slow_partial_quote_cannot_reuse_expired_full_position_risk(setup,monkeypatch):
    _,rpc,_,library,agent,_,service,_=setup
    p,_=fill_buy(setup)
    scale_sells(rpc,3)
    observe(library,agent,time.time(),320)
    original=service.manual.preview
    now=time.time()
    count=0

    def slow_preview(*args,**kwargs):
        nonlocal count
        result=original(*args,**kwargs)
        count+=1
        if count==2:
            monkeypatch.setattr('pumpwizard_runner.agent_execution.time.time',lambda:now+35)
            result['created_at']=now+35
        return result

    monkeypatch.setattr(service.manual,'preview',slow_preview)
    before=snapshot(setup)
    with pytest.raises(TransferError,match='Full-position net risk quote expired'):
        service.prepare(agent,setup[0].address,MINT,setup[5]['id'],position_id=p['id'])
    assert snapshot(setup)==before and len(rpc.sent)==1


def test_slow_agent_revalidation_cannot_sign_an_expired_review(setup,monkeypatch):
    wallet,rpc,journal,_,agent,cash,service,_=setup
    p=service.prepare(agent,wallet.address,MINT,cash['id'])
    validator=service.manual.agent_authorizer
    now=time.time()
    count=0

    def slow_validate(preview):
        nonlocal count
        validator(preview)
        count+=1
        if count==2:
            monkeypatch.setattr('pumpwizard_runner.manual_trades.time.time',lambda:now+61)

    def forbidden(*args):
        pytest.fail('Expired review reached the signer')

    monkeypatch.setattr(service.manual,'agent_authorizer',slow_validate)
    monkeypatch.setattr(type(wallet),'sign',forbidden)
    with pytest.raises(TransferError,match='Quote or blockhash expired'):
        service.submit(p['id'],wallet)
    assert not rpc.sent and journal.db.execute('SELECT COUNT(*) FROM trades').fetchone()[0]==0
