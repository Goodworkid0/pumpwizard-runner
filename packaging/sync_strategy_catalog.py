"""Build the offline catalog from a public summary and checked-out research definitions.

Run with PYTHONPATH=src; does not open ledgers, start collectors, or contact wallets.
"""
import argparse
import json
from pathlib import Path
import runpy

from pumpwizard_runner.catalog import SCHEMA, SOURCE, public_rows, read_catalog


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pumpwizard-root", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    rows = public_rows(json.loads(args.snapshot.read_text()))
    by_id = {row["id"]: row for row in rows}
    generate = runpy.run_path(str(args.pumpwizard_root / "paper/experiment_catalog.py"))["catalog"]
    for entry in generate(by_id["early-frequency"]["config"]):
        if entry["id"] not in by_id:
            by_id[entry["id"]] = {key: entry[key] for key in ("id", "label", "family", "description", "config")} | {
                "trial": None, "report": None, "source_as_of": None, "source_stale": False,
            }
    document = {"schema": SCHEMA, "source": SOURCE, "strategies": list(by_id.values())}
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    read_catalog(encoded)
    target = Path(__file__).parents[1] / "src/pumpwizard_runner/data/strategies.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(encoded + "\n", encoding="utf-8")
    print(f"Bundled {len(rows)} simulation results and {len(by_id)-len(rows)} untested definitions")


if __name__ == "__main__":
    main()
