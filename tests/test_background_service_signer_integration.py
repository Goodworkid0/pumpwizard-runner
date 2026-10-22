"""Service engine + Worker 2 synthetic signer integration; never funded."""

import time

from pumpwizard_runner.background_service import BackgroundServiceEngine
from pumpwizard_runner.service_signing import ServiceRuntimeManager, ServiceSigningController
from pumpwizard_runner.transfers import GENESIS
from test_agent_execution import setup as _agent_setup
from test_main_wallet_autopilot import configure, plan
from test_service_signing import set_service_brief
from test_service_wallet_vault import PASSWORD, envelope


def wait_for(predicate, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("Timed out waiting for service-owned synthetic execution")


class RecordingSigner:
    def __init__(self):
        self.locks = []

    def lock(self, reason):
        self.locks.append(reason)
        return {"signer_state": "locked"}


def test_clock_or_sleep_discontinuity_locks_signing_before_another_cycle(tmp_path):
    wall = [1_000.0]
    monotonic = [500.0]
    signer = RecordingSigner()
    engine = BackgroundServiceEngine(
        tmp_path,
        signer=signer,
        clock=lambda: wall[0],
        monotonic=lambda: monotonic[0],
    )

    wall[0] += 5
    monotonic[0] += 5
    engine._detect_resume()
    assert signer.locks == []

    # A wall-clock correction that does not match monotonic elapsed time is
    # distinct from browser closure and always removes signing authority.
    wall[0] -= 120
    monotonic[0] += 1
    engine._detect_resume()
    assert signer.locks == [
        "Sleep/resume or clock discontinuity requires fresh trading unlock"
    ]


def test_service_engine_executes_after_authorization_without_browser_polling_and_locks_on_stop(
    tmp_path,
):
    source = _agent_setup.__wrapped__(tmp_path)
    values = next(source)
    wallet, rpc, journal, library, session_id, _, _, _ = values
    rpc.network = "mainnet-beta"
    rpc.genesis = lambda: GENESIS["mainnet-beta"]
    set_service_brief(library, session_id)
    configure(values)
    controller = ServiceSigningController(
        tmp_path,
        manager=ServiceRuntimeManager(allow_synthetic=True),
        rpc_factory=lambda _network: rpc,
    )
    class OfflineSource:
        def sol_usd_price(self):
            return 100.0

        def collect(self, *args, **kwargs):
            return []

    engine = BackgroundServiceEngine(tmp_path, signer=controller, source_factory=OfflineSource)
    engine.start()
    try:
        controller.enroll(
            envelope(wallet), PASSWORD,
            expected_address=wallet.address, expected_network="mainnet-beta",
        )
        review = controller.review(
            library, sender=wallet.address, network="mainnet-beta",
            session_id=session_id, plan=plan(),
        )
        allowance = controller.authorize(
            library, sender=wallet.address, network="mainnet-beta",
            session_id=session_id, review_id=review["id"],
            confirmation=f"ALLOW SERVICE {session_id[:8].upper()}",
        )
        assert engine.store.job()["enabled"] is False
        engine.start_signing({
            'sender': wallet.address, 'network': 'mainnet-beta',
            'allowance_id': allowance['id'], 'service_unlock_secret': PASSWORD,
            'confirmation': f"START {session_id[:8].upper()}",
        })
        assert engine.store.job()["enabled"] is True

        # No browser request, heartbeat, tab token or polling call participates.
        wait_for(lambda: len(rpc.sent) == 1)
        assert journal.trades(wallet.address)[0]["status"] == "pending"
        assert controller.state()["signer_state"] == "unlocked"
    finally:
        stopped = engine.stop()
        assert stopped["stopped"] is True
    assert controller.state()["signer_state"] == "locked"
    assert len(rpc.sent) == 1
    next(source, None)
