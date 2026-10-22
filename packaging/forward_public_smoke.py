#!/usr/bin/env python3
"""One bounded, keyless read of the configured public market-data provider."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from pumpwizard_runner.market import (
    CompositePumpMarketSource,
    PumpPublicSource,
    PumpSwapPairSource,
)


SCHEMA = "pumpwizard.forward-public-provider-smoke/v2"
LIMIT = 3
MAXIMUM_HTTP_REQUESTS = 3 + LIMIT
MAXIMUM_ELAPSED_SECONDS = 60
MAXIMUM_RESPONSE_BYTES = 12_000_000
MAXIMUM_CONCURRENCY = 2


class BoundedPumpPublicSource(PumpPublicSource):
    """Apply explicit acceptance bounds below the global 100-read/ten-minute cap."""

    def __init__(self) -> None:
        super().__init__(
            timeout_seconds=8,
            max_workers=MAXIMUM_CONCURRENCY,
            maximum_http_requests=MAXIMUM_HTTP_REQUESTS,
            maximum_elapsed_seconds=MAXIMUM_ELAPSED_SECONDS,
            maximum_response_bytes=MAXIMUM_RESPONSE_BYTES,
        )


def run() -> dict[str, object]:
    started_at = time.time()
    curve = BoundedPumpPublicSource()
    source = CompositePumpMarketSource(
        curve_source=curve,
        amm_source=PumpSwapPairSource(timeout_seconds=8),
    )
    status = "success"
    error_class = None
    error = None
    quotes = []
    diagnostics = None
    try:
        # Empty priority means the AMM continuity adapter performs no request.
        quotes = source.collect(1_000, 150_000, limit=LIMIT, priority=set())
        diagnostics = source.last_diagnostics
        if diagnostics is not None:
            status = str(diagnostics["status"])
        elif not quotes:
            status = "accessible-no-matching-quotes"
    except Exception as exc:  # The artifact records access failure without credentials or URLs.
        status = "access-failure"
        error_class = type(exc).__name__
        error = "The bounded keyless public-provider read did not complete."
        diagnostics = source.last_diagnostics
    completed_at = time.time()
    source_ages = [max(0.0, completed_at - quote.source_at) for quote in quotes]
    routes = sorted({quote.route for quote in quotes})
    providers = sorted({quote.source for quote in quotes})
    return {
        "schema": SCHEMA,
        "observed_at": datetime.fromtimestamp(completed_at, timezone.utc).isoformat(),
        "provider": "CompositePumpMarketSource",
        "keyless": True,
        "wallet_access": False,
        "signing_authority": False,
        "provider_purchase_authority": False,
        "bounds": {
            "minimum_cap_usd": 1_000,
            "maximum_cap_usd": 150_000,
            "quote_limit": LIMIT,
            "maximum_http_requests": MAXIMUM_HTTP_REQUESTS,
            "timeout_seconds_per_request": 8,
            "maximum_elapsed_seconds": MAXIMUM_ELAPSED_SECONDS,
            "maximum_response_bytes": MAXIMUM_RESPONSE_BYTES,
            "maximum_concurrency": MAXIMUM_CONCURRENCY,
        },
        "status": status,
        "error_class": error_class,
        "error": error,
        "http_requests": (
            diagnostics["stages"]["http_requests"] if diagnostics is not None else 0
        ),
        "elapsed_seconds": completed_at - started_at,
        "quote_count": len(quotes),
        "routes": routes,
        "quote_sources": providers,
        "maximum_source_age_seconds": max(source_ages, default=None),
        "first_receipt_at": min((quote.observed_at for quote in quotes), default=None),
        "stage_diagnostics": diagnostics,
        "retained_batch5_zero_quote_cause": {
            "status": "diagnosed",
            "cause": "The legacy adapter selected listing candidates, then required an unavailable exact-mint detail route whose selected reads returned HTTP 404.",
            "correction": "Curve discovery now normalizes the complete supported listing snapshots without inventing detail responses or source timestamps.",
        },
        "cost": {
            "currency": "USD",
            "measured_cost": None,
            "basis": "Public endpoints expose no per-request invoice; unknown is not zero.",
        },
        "interpretation": (
            "A short provider-access, stage and normalization check only; it is not "
            "uptime, capacity, or strategy-performance evidence."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run()
    encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    # Access failure is retained evidence, not a reason to retry or hide the artifact.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
