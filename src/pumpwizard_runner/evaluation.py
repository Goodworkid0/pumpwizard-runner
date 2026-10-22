"""Complete offline evaluations, bound to immutable strategy and recording content.

No wallet, RPC, signing or order service is imported. Failed/cancelled replays do
not publish a completed report. Existing CLI replay runs remain untouched.
"""

from collections import Counter
import copy
from importlib.resources import files
import json
import math
from pathlib import Path
import sqlite3
import time

from .portfolio import PortfolioEngine
from .strategies import canonical_json, content_hash, parse_package
from .strategy_support import reviewed_eligible, strategy_identity


MAX_BYTES = 16 * 1024 * 1024
MAX_BATCHES = 20_000
SCHEMA = "solanamarket.local-evaluation/v1"


def example_recording():
    return json.loads(files("pumpwizard_runner").joinpath("data/core-frequency-replay.json").read_text())


def read_recording(path):
    with Path(path).open("rb") as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Recording exceeds the 16 MiB local evaluation limit")
    return json.loads(raw)


def validate_recording(strategy, recording):
    if not reviewed_eligible(strategy):
        raise ValueError("Replay is available only for the exact pinned core-frequency configuration without overlays")
    if not isinstance(recording, dict) or recording.get("schema") != "pumpwizard.portfolio-recording/v1":
        raise ValueError("Choose a pumpwizard.portfolio-recording/v1 recording")
    if recording.get("configuration") != strategy.effective_rules:
        raise ValueError("Recording configuration differs from the selected imported strategy")
    batches = recording.get("batches")
    if not isinstance(batches, list) or not 1 <= len(batches) <= MAX_BATCHES:
        raise ValueError("Recording must contain 1–20,000 batches")
    if len(canonical_json(recording).encode("utf-8")) > MAX_BYTES:
        raise ValueError("Recording exceeds the 16 MiB local evaluation limit")
    previous = None
    for index, batch in enumerate(batches):
        if not isinstance(batch, dict):
            raise ValueError(f"Batch {index + 1} must be an object")
        at = batch.get("at")
        if type(at) not in (int, float) or not math.isfinite(at) or not 0 <= at <= 253402300799:
            raise ValueError(f"Batch {index + 1} needs a finite timestamp between years 1970 and 9999")
        if previous is not None and at <= previous:
            raise ValueError("Recording times must increase strictly; duplicate/backward batches are rejected")
        if not isinstance(batch.get("quotes"), list) or not all(isinstance(q, dict) for q in batch["quotes"]):
            raise ValueError(f"Batch {index + 1} quotes must be an array of objects")
        if not isinstance(batch.get("evidence"), dict):
            raise ValueError(f"Batch {index + 1} evidence must be an object")
        previous = at
    return batches


def evaluate(data_dir, package, recording, *, cancelled=lambda: False):
    strategy = parse_package(copy.deepcopy(package))
    recording = copy.deepcopy(recording)
    batches = validate_recording(strategy, recording)
    identity = strategy_identity(strategy)
    input_hash = content_hash(recording)
    report_id = content_hash(dict(schema=SCHEMA, identity=identity, input_sha256=input_hash))
    engine = PortfolioEngine(recording["configuration"])
    events, equity_curve = [], []
    peak = engine.cfg["budget_usd"]
    max_drawdown = 0.0
    started = time.time()
    for batch in batches:
        if cancelled():
            raise ValueError("Evaluation cancelled; no completed report saved")
        events.extend(engine.step(batch))
        state = engine.state
        equity = state["cash_usd"] + sum(p["mark_value_usd"] for p in state["positions"].values())
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak if peak else 0)
        equity_curve.append(dict(at=batch["at"], equity_usd=equity))
    state = engine.state
    report = dict(
        schema=SCHEMA, id=report_id, kind="offline-modeled-replay", identity=identity,
        input_sha256=input_hash, input_schema=recording["schema"],
        input_origin="bundled synthetic example" if input_hash == content_hash(example_recording())
                     else "user-supplied recording; provenance unverified",
        started_at=started, completed_at=time.time(),
        period=dict(first_at=batches[0]["at"], last_at=batches[-1]["at"], batches=len(batches)),
        strategy_package=strategy.package, recording=recording,
        checkpoint=engine.checkpoint(), events=events, equity_curve=equity_curve,
        summary=dict(initial_usd=engine.cfg["budget_usd"], final_equity_usd=equity_curve[-1]["equity_usd"],
                     cash_usd=state["cash_usd"], realized_pnl_usd=state["realized_pnl_usd"],
                     failed_fees_usd=state["failed_fees_usd"], modeled_fees_usd=state["fees_usd"],
                     open_positions=len(state["positions"]), pending_orders=len(state["pending"]),
                     max_drawdown_fraction=max_drawdown,
                     event_counts=dict(Counter(e["action"] for e in events))),
        funded_acceptance=False, submission_enabled=False,
        limitations="Modeled fills, costs and failure draws; supplied input provenance is unverified. "
                     "Missing exit quotes may mark holdings at zero. Coverage is limited to this recording "
                     "and the pinned engine; this is neither unsigned chain simulation nor funded performance.",
    )
    payload = canonical_json(report)
    if cancelled():
        raise ValueError("Evaluation cancelled; no completed report saved")
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / "local-evaluations.sqlite", timeout=10)
    try:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS evaluations (id TEXT PRIMARY KEY, package_hash TEXT, completed REAL, payload TEXT)")
            db.execute("INSERT OR IGNORE INTO evaluations VALUES (?,?,?,?)",
                       (report_id, strategy.digest, report["completed_at"], payload))
            saved = json.loads(db.execute("SELECT payload FROM evaluations WHERE id=?", (report_id,)).fetchone()[0])
    finally:
        db.close()
    return saved


def reports(data_dir, package_hash):
    path = Path(data_dir) / "local-evaluations.sqlite"
    if not path.exists():
        return []
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM evaluations WHERE package_hash=? ORDER BY completed DESC LIMIT 100",
            (package_hash,))]
    finally:
        db.close()


def report_text(report):
    summary, identity = report["summary"], report["identity"]
    period = report["period"]
    def stamp(at):
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(at))
    return (
        "OFFLINE MODELED REPLAY · No funds moved\n\n"
        f"Input: {report['input_origin']}\n"
        f"Equity: ${summary['initial_usd']:,.2f} → ${summary['final_equity_usd']:,.2f}\n"
        f"Modeled cash: ${summary['cash_usd']:,.2f} · maximum sampled drawdown: {summary['max_drawdown_fraction']:.2%}\n"
        f"Open modeled positions: {summary['open_positions']} · pending modeled orders: {summary['pending_orders']}\n"
        f"Modeled fees: ${summary['modeled_fees_usd']:,.4f} · failed-attempt fees: ${summary['failed_fees_usd']:,.4f}\n"
        f"Events: {json.dumps(summary['event_counts'], sort_keys=True)}\n\n"
        f"Input period: {stamp(period['first_at'])} → {stamp(period['last_at'])} · {period['batches']} batches\n"
        f"Evaluated: {stamp(report['completed_at'])}\n"
        f"Engine: {identity['replay_engine']} · application: {identity['app_version']}\n"
        f"Package SHA-256: {identity['package_sha256']}\nRules SHA-256: {identity['rules_sha256']}\n"
        f"Recording SHA-256: {report['input_sha256']}\nReport ID: {report['id']}\n\n"
        f"{report['limitations']}\n\n"
        "Next: collect fresh local observations, inspect wallet/allocation readiness, then run unsigned "
        "assessment. Rehearsal cannot use these modeled positions as wallet holdings. "
        "Funded lifecycle acceptance is pending. Automatic execution is disabled."
    )
