"""Pure prospective paper evaluator backed by the portfolio replay ledger."""
from __future__ import annotations

import copy
import json
from typing import Any, Iterable

from .portfolio_replay import (
    ENGINE_VERSION,
    PortfolioReplay,
    PortfolioReplayError,
    _event,
)
from .strategies import canonical_json, content_hash


STATE_SCHEMA = "pumpwizard.incremental-paper-state/v2"
EVALUATOR_VERSION = "pumpwizard-incremental-paper/2"


class PaperEvaluationError(PortfolioReplayError):
    """Prospective input changed, arrived out of order, or used future evidence."""


class IncrementalPaperEvaluator:
    """Idempotently advance one immutable point-in-time event at a time.

    Entry decisions are appended to ``decisions`` before the replay can create a
    later fill or score an exit.  Checkpoints contain simulated state only and
    carry no wallet allocation, signer, or execution authority.
    """

    def __init__(
        self,
        *,
        run_id: str,
        strategy_sha256: str,
        policy: dict[str, Any],
        costs: dict[str, Any],
        execution: dict[str, Any],
        checkpoint: dict[str, Any] | None = None,
    ):
        if not isinstance(run_id, str) or not run_id.strip():
            raise PaperEvaluationError("Paper run ID is required")
        if not isinstance(strategy_sha256, str) or len(strategy_sha256) != 64:
            raise PaperEvaluationError("Paper strategy identity must be a SHA-256 digest")
        self.run_id = run_id.strip()
        self.strategy_sha256 = strategy_sha256
        if checkpoint is None:
            self.replay = PortfolioReplay(policy, costs, execution)
            self.state = {
                "observations": {}, "decisions": [], "gaps": [],
                "started_at": None, "last_first_known_at": None,
            }
        else:
            value = self._checkpoint(checkpoint)
            if value["run_id"] != self.run_id or value["strategy_sha256"] != strategy_sha256:
                raise PaperEvaluationError("Paper checkpoint identity changed")
            self.replay = PortfolioReplay(
                policy, costs, execution, checkpoint=value["replay_checkpoint"]
            )
            self.state = copy.deepcopy(value["paper_state"])

    @staticmethod
    def _checkpoint(value: dict[str, Any]) -> dict[str, Any]:
        try:
            result = json.loads(canonical_json(value))
        except (TypeError, ValueError) as exc:
            raise PaperEvaluationError("Paper checkpoint must be canonical JSON") from exc
        if result.get("schema") != STATE_SCHEMA or result.get("evaluator_version") != EVALUATOR_VERSION:
            raise PaperEvaluationError("Unsupported paper checkpoint")
        if result.get("replay_engine_version") != ENGINE_VERSION:
            raise PaperEvaluationError("Paper checkpoint replay engine changed")
        return result

    def advance(self, raw_event: dict[str, Any]) -> list[dict[str, Any]]:
        event = _event(raw_event)
        digest = content_hash(event)
        prior = self.state["observations"].get(event["id"])
        if prior is not None:
            if prior != digest:
                raise PaperEvaluationError("Paper observation ID was reused with changed content")
            return []
        known_at = float(event["first_known_at"])
        previous = self.state["last_first_known_at"]
        if previous is not None and known_at < float(previous):
            raise PaperEvaluationError("Paper observations cannot be backfilled into prior history")
        before = copy.deepcopy(self.state)
        replay_before = self.replay.checkpoint()
        if self.state["started_at"] is None:
            self.state["started_at"] = known_at
        if event["kind"] == "entry_signal":
            evidence = set(event["evidence_ids"])
            if not evidence <= set(self.state["observations"]):
                raise PaperEvaluationError("Paper decision references evidence not yet recorded")
            self.state["decisions"].append({
                "id": event["id"], "mint": event["mint"],
                "decision": "entry_signal", "decided_at": known_at,
                "recorded_before_outcome": True, "evidence_ids": list(event["evidence_ids"]),
                "outcome_status": "pending", "fill_ids": [],
            })
        try:
            transitions = self.replay.apply(event)
            self.state["observations"][event["id"]] = digest
            self.state["last_first_known_at"] = known_at
            self._score(transitions)
        except Exception:
            self.state = before
            self.replay = PortfolioReplay(
                replay_before["policy"], replay_before["costs"],
                replay_before["execution"], checkpoint=replay_before,
            )
            raise
        return transitions

    def record_gap(self, *, gap_id: str, started_at: float, ended_at: float | None,
                   reason: str) -> None:
        if not isinstance(gap_id, str) or not gap_id:
            raise PaperEvaluationError("Paper gap ID is required")
        if any(row["id"] == gap_id for row in self.state["gaps"]):
            existing = next(row for row in self.state["gaps"] if row["id"] == gap_id)
            candidate = {"id": gap_id, "started_at": started_at, "ended_at": ended_at, "reason": reason}
            if existing != candidate:
                raise PaperEvaluationError("Paper gap ID was reused with changed content")
            return
        if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
            raise PaperEvaluationError("Paper gap start must be numeric")
        if ended_at is not None and (
            isinstance(ended_at, bool) or not isinstance(ended_at, (int, float)) or ended_at < started_at
        ):
            raise PaperEvaluationError("Paper gap end must not precede its start")
        if not isinstance(reason, str) or not reason.strip():
            raise PaperEvaluationError("Paper gap reason is required")
        self.state["gaps"].append({
            "id": gap_id, "started_at": started_at, "ended_at": ended_at,
            "reason": reason.strip(),
        })

    def _score(self, transitions: list[dict[str, Any]]) -> None:
        entry_by_position = {}
        for fill in self.replay.state["fills"]:
            if fill["side"] == "entry":
                entry_by_position[fill["position_id"]] = fill
        for transition in transitions:
            fill_id = transition.get("id")
            if transition.get("side") == "entry":
                signal_id = self.replay.state["positions"][transition["mint"]]["signal_id"]
                decision = next(row for row in self.state["decisions"] if row["id"] == signal_id)
                decision["fill_ids"].append(fill_id)
                decision["outcome_status"] = "open"
            elif transition.get("side") == "exit":
                entry = entry_by_position.get(transition["position_id"])
                if entry is None:
                    raise PaperEvaluationError("Paper exit lost its prior entry decision")
                decision = next(
                    row for row in self.state["decisions"]
                    if entry["id"] in row["fill_ids"]
                )
                decision["fill_ids"].append(fill_id)
                if transition["position_id"] not in {
                    position["id"] for position in self.replay.state["positions"].values()
                }:
                    decision["outcome_status"] = "closed"
                    decision["outcome_recorded_at"] = transition["at"]
                    decision["modeled_realized_pnl_micros"] = sum(
                        fill["realized_pnl_micros"]
                        for fill in self.replay.state["fills"]
                        if fill["side"] == "exit" and fill["position_id"] == transition["position_id"]
                    )
        opportunity_by_signal = {
            row["signal_id"]: row for row in self.replay.state["opportunities"]
        }
        terminal = {
            "failed_entry": "failed_entry",
            "skipped_expired": "expired_unfilled",
            "skipped_unexecutable": "unexecutable",
            "skipped_unexecutable_evidence": "unexecutable_evidence",
            "skipped_existing_exposure": "skipped_existing_exposure",
            "skipped_capacity": "skipped_capacity",
            "skipped_insufficient_cash": "skipped_insufficient_cash",
        }
        for decision in self.state["decisions"]:
            if decision["outcome_status"] != "pending":
                continue
            status = opportunity_by_signal.get(decision["id"], {}).get("status")
            if status in terminal:
                decision["outcome_status"] = terminal[status]

    def checkpoint(self) -> dict[str, Any]:
        return {
            "schema": STATE_SCHEMA, "evaluator_version": EVALUATOR_VERSION,
            "replay_engine_version": ENGINE_VERSION, "run_id": self.run_id,
            "strategy_sha256": self.strategy_sha256,
            "paper_state": copy.deepcopy(self.state),
            "replay_checkpoint": self.replay.checkpoint(),
            "wallet_allocation_micros": 0, "signing_authority": False,
            "submission_enabled": False,
        }

    def report(self) -> dict[str, Any]:
        replay = self.replay.report()
        core = {
            "schema": "pumpwizard.incremental-paper-report/v2",
            "evaluator_version": EVALUATOR_VERSION,
            "run_id": self.run_id, "strategy_sha256": self.strategy_sha256,
            "started_at": self.state["started_at"],
            "last_first_known_at": self.state["last_first_known_at"],
            "decisions": copy.deepcopy(self.state["decisions"]),
            "gaps": copy.deepcopy(self.state["gaps"]), "replay": replay,
            "prospective": True, "backfill_repairs_history": False,
            "parity_contract": {
                "same_ordered_events_match_batch_replay": True,
                "recorded_gaps_do_not_advance_or_repair_replay_history": True,
                "missed_prospective_evidence_is_not_backfilled": True,
            },
            "assumptions": [
                "Fills and failures are configured replay scenarios, not transaction receipts.",
                "An ID absent from the failure scenario is not evidence that a live fill was available.",
                "Unknown or stale exits remain open and reduce valuation coverage.",
            ],
            "wallet_allocation_micros": 0, "signing_authority": False,
            "submission_enabled": False,
        }
        ident = content_hash(core)
        return {**core, "id": ident, "reproducibility_sha256": ident}


def evaluate_incrementally(
    events: Iterable[dict[str, Any]], *, run_id: str, strategy_sha256: str,
    policy: dict[str, Any], costs: dict[str, Any], execution: dict[str, Any],
) -> dict[str, Any]:
    evaluator = IncrementalPaperEvaluator(
        run_id=run_id, strategy_sha256=strategy_sha256,
        policy=policy, costs=costs, execution=execution,
    )
    ordered = sorted((_event(event) for event in events), key=lambda row: (
        row["first_known_at"], row["event_at"],
        {"quote": 0, "exit_signal": 1, "entry_signal": 2, "clock": 3}[row["kind"]],
        row.get("mint", ""), row["id"],
    ))
    for event in ordered:
        evaluator.advance(event)
    return evaluator.report()
