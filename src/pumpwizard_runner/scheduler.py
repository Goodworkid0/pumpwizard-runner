"""Shared read-only market scans, independent agent state, and restart recovery."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import queue
import threading
import time
import uuid

from .library import StrategyLibrary
from .market import CompositePumpMarketSource, MarketDataError
from .runtime import LocalMonitor
from .strategies import parse_package


class MonitorScheduler:
    """One database lease per app directory; no signing or execution access.

    At most four distinct scans are dispatched per pass. Agents share matching
    cap bands. Campaigns share only an exact frozen provider/band/priority/limit
    identity, so another consumer cannot silently broaden their evidence universe.
    Each agent commits independently, so one invalid configuration/provider
    failure does not discard another's work.
    """

    def __init__(self, data_dir: Path, source_factory=CompositePumpMarketSource, *, recorder=None,
                 campaigns=None, owner: str | None = None, clock=None):
        self.data_dir, self.source_factory = data_dir, source_factory
        self.recorder = recorder
        self.campaigns = campaigns
        self.owner = owner or uuid.uuid4().hex
        self.clock = clock or time.time
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.events = queue.SimpleQueue()
        self.thread = None
        self.cycle_lock = threading.Lock()
        self.last_cycle = {"completed": 0, "failures": 0}

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="wizard-monitor-scheduler", daemon=True)
            self.thread.start()

    def wake(self):
        self.wake_event.set()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()

    def _run(self):
        library = None
        try:
            library = StrategyLibrary(self.data_dir)
            while not self.stop_event.is_set():
                self.wake_event.clear()
                self.run_cycle(library)
                self.wake_event.wait(1)
        except Exception as exc:
            self.events.put((None, "Scheduler stopped: " + str(exc)[:180]))
        finally:
            if library:
                library.release_monitor_lease(self.owner)
                library.close()

    def run_cycle(self, library: StrategyLibrary) -> int:
        if not self.cycle_lock.acquire(blocking=False):
            return 0
        try:
            return self._run_cycle(library)
        finally:
            self.cycle_lock.release()

    def _run_cycle(self, library: StrategyLibrary) -> int:
        self.last_cycle = {"completed": 0, "failures": 0}
        now = float(self.clock())
        if not library.claim_monitor_lease(self.owner, now):
            self.events.put((None, "Another PumpWizard Runner window is monitoring this local library."))
            self.last_cycle["failures"] = 1
            return 0
        groups = {}
        for settings in library.scheduled_instances():
            if settings["fault"] or settings["next_at"] > now + 3600:
                # A backwards system-clock change must not stall workers for hours.
                if settings["fault"]:
                    continue
            elif settings["next_at"] > now:
                continue
            ident = settings["id"]
            try:
                instance = library.instance(ident)
                rules = parse_package(instance["package"]).effective_rules
                band = (float(rules.get("entry_cap_min", rules.get("entry_cap_min_usd"))),
                        float(rules.get("entry_cap_max", rules.get("entry_cap_max_usd"))))
                group_key = ("agents", *band)
                if group_key not in groups and len(groups) >= 4:
                    continue
                group = groups.setdefault(group_key, {
                    "kind": "agents", "band": band,
                    "agents": [], "priority": set(), "limit": 24,
                })
                group["agents"].append(settings)
                group["priority"].update(p.mint for p in library.position_views(ident))
                group["limit"] = min(100, max(group["limit"], int(rules.get("max_positions", 1))*3))
            except (ValueError, KeyError, TypeError) as exc:
                self.last_cycle["failures"] += 1
                self._failure(library, settings, now, exc, fault=True)
        if self.campaigns is not None:
            for demand in self.campaigns.scan_demands(now=now):
                band = (float(demand["minimum_cap"]), float(demand["maximum_cap"]))
                campaign_priority = tuple(sorted(demand["priority_mints"]))
                group_key = (
                    "campaign", str(demand["provider"]), *band,
                    int(demand["limit"]), campaign_priority,
                )
                if group_key not in groups and len(groups) >= 4:
                    continue
                group = groups.setdefault(
                    group_key, {
                        "kind": "campaign", "band": band,
                        "agents": [], "priority": set(campaign_priority),
                        "limit": int(demand["limit"]), "campaigns": {},
                        "campaign_providers": {}, "campaign_request_remaining": {},
                    },
                )
                group.setdefault("campaigns", {})[demand["campaign_id"]] = demand["arm_count"]
                group.setdefault("campaign_providers", {})[
                    demand["campaign_id"]
                ] = demand["provider"]
                group.setdefault("campaign_request_remaining", {})[
                    demand["campaign_id"]
                ] = demand["remaining_requests"]
        for group in groups.values():
            group.setdefault("campaigns", {})
            group.setdefault("campaign_providers", {})
            group.setdefault("campaign_request_remaining", {})
        if not groups or self.stop_event.is_set():
            return 0

        def collect(band, group):
            # Providers cap one request at 100; rotate all held mints in bounded
            # chunks rather than silently dropping holdings beyond that cap.
            priority = sorted(group["priority"])
            chunks = [set(priority[i:i+30]) for i in range(0, len(priority), 30)] or [set()]
            try:
                source = self.source_factory()
            except Exception as exc:
                if self.recorder is not None:
                    run_id = self.recorder.begin_scan(
                        minimum_cap=band[0], maximum_cap=band[1],
                        source=getattr(self.source_factory, "__name__", type(self.source_factory).__name__),
                        priority=chunks[0], limit=max(group["limit"], len(chunks[0])),
                        campaign_consumers=group["campaigns"],
                    )
                    failed = self.recorder.fail_scan(run_id, exc)
                    if self.campaigns is not None and group["campaigns"]:
                        self.campaigns.record_scan(
                            list(group["campaigns"]), success=False,
                            finished_at=float(failed["received_at"]),
                            next_retry_at=failed["next_retry_at"],
                            detail=str(failed["gap_reason"]),
                        )
                raise
            actual_provider = type(source).__name__
            campaign_consumers = {
                campaign_id: arms
                for campaign_id, arms in group["campaigns"].items()
                if group["campaign_providers"].get(campaign_id) == actual_provider
            }
            mismatched = sorted(set(group["campaigns"]) - set(campaign_consumers))
            if self.campaigns is not None:
                for campaign_id in mismatched:
                    self.campaigns.block_provider_mismatch(
                        campaign_id, actual_provider=actual_provider, at=now,
                    )
            if not group["agents"] and not campaign_consumers:
                return []
            result = {}
            for chunk in chunks:
                if self.stop_event.is_set():
                    break
                request_consumers = {
                    campaign_id: arms
                    for campaign_id, arms in campaign_consumers.items()
                    if group["campaign_request_remaining"].get(campaign_id, 0) > 0
                }
                if not group["agents"] and not request_consumers:
                    break
                limit = (
                    max(group["limit"], len(chunk))
                    if group["kind"] == "agents" else group["limit"]
                )
                run_id = None
                if self.recorder is not None:
                    run_id = self.recorder.begin_scan(
                        minimum_cap=band[0], maximum_cap=band[1],
                        source=type(source).__name__, priority=chunk, limit=limit,
                        campaign_consumers=request_consumers,
                    )
                for campaign_id in request_consumers:
                    group["campaign_request_remaining"][campaign_id] -= 1
                try:
                    quotes = list(source.collect(*band, limit=limit, priority=chunk))
                    if self.recorder is not None:
                        completed_run = self.recorder.complete_scan(
                            run_id, quotes,
                            diagnostics=getattr(source, "last_diagnostics", None),
                        )
                        if self.campaigns is not None and request_consumers:
                            self.campaigns.record_scan(
                                list(request_consumers), success=True,
                                finished_at=float(completed_run["received_at"]),
                            )
                except Exception as exc:
                    if self.recorder is not None and run_id is not None:
                        failed = self.recorder.fail_scan(
                            run_id, exc,
                            diagnostics=getattr(source, "last_diagnostics", None),
                        )
                        if self.campaigns is not None and request_consumers:
                            self.campaigns.record_scan(
                                list(request_consumers), success=False,
                                finished_at=float(failed["received_at"]),
                                next_retry_at=failed["next_retry_at"],
                                detail=str(failed["gap_reason"]),
                            )
                    raise
                for quote in quotes:
                    result[quote.mint] = quote
            return list(result.values())

        completed = 0
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="wizard-shared-scan") as pool:
            pending = {
                pool.submit(collect, group["band"], group): group
                for group in groups.values()
            }
            for future in as_completed(pending):
                if self.stop_event.is_set():
                    continue
                # Renew only if nobody took over an expired lease while reads ran.
                if not library.claim_monitor_lease(self.owner, float(self.clock())):
                    continue
                group = pending[future]
                try:
                    quotes = future.result()
                except Exception as exc:
                    self.last_cycle["failures"] += 1
                    for settings in group["agents"]:
                        self._failure(library, settings, now, exc, fault=not isinstance(exc, MarketDataError))
                    continue
                for settings in group["agents"]:
                    if self.stop_event.is_set():
                        break
                    try:
                        monitor = LocalMonitor(library, settings["id"], None)
                        result = monitor.process_quotes(quotes, now, float(self.clock()),
                            allow_entries=bool(settings["enabled"] and settings["status"] == "monitoring"),
                            revision=settings["revision"], owner=self.owner)
                        if result:
                            completed += 1
                            self.events.put((settings["id"], f"{result.observations} observations · {result.decisions} decisions"))
                    except Exception as exc:
                        self.last_cycle["failures"] += 1
                        self._failure(library, settings, now, exc, fault=True)
        self.last_cycle["completed"] = completed
        return completed

    def _failure(self, library, settings, started_at, exc, *, fault):
        error = (type(exc).__name__ + ": " + str(exc))[:500]
        if library.commit_monitor_cycle(settings["id"], quotes=[], decisions=[], checkpoint={}, marks=[],
                started_at=started_at, finished_at=float(self.clock()), revision=settings["revision"], owner=self.owner,
                error=error, fault=fault):
            self.events.put((settings["id"], error))
