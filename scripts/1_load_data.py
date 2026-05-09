"""Compile the 9 Olist CSVs into data/olist.db and print the introspected schema.

Usage:
    python scripts/1_load_data.py                # load only missing tables
    python scripts/1_load_data.py --rebuild      # drop the .db and reload everything
    python scripts/1_load_data.py --schema-only  # skip loading; print live schema
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.config import load_config
from src.db.loader import build_database
from src.db.schema import format_schema_for_llm, introspect_schema


def _on_table(stage: str, spec, info: dict) -> None:
    if stage == "load":
        print(f"  loaded   {spec.name:<32} {info['rows']:>9,} rows  ({info['seconds']:.2f}s)")
    elif stage == "skip":
        print(f"  skipped  {spec.name:<32} {info['rows']:>9,} rows already present")
    elif stage == "index":
        if info["count"]:
            print(f"  indexed  {spec.name:<32} {info['count']} index(es)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rebuild", action="store_true", help="Delete the existing DB and reload everything.")
    parser.add_argument("--schema-only", action="store_true", help="Print the introspected schema without loading.")
    args = parser.parse_args()

    cfg = load_config()
    print(f"CSV dir : {cfg.olist_csv_dir}")
    print(f"DB path : {cfg.sqlite_path}")

    if not args.schema_only:
        if not cfg.olist_csv_dir.exists():
            print(f"ERROR: CSV directory does not exist: {cfg.olist_csv_dir}", file=sys.stderr)
            return 2

        print(f"\nBuilding database (rebuild={args.rebuild})...")
        start = time.perf_counter()
        stats = build_database(
            csv_dir=cfg.olist_csv_dir,
            sqlite_path=cfg.sqlite_path,
            replace=args.rebuild,
            on_table=_on_table,
        )
        elapsed = time.perf_counter() - start

        total_rows = sum(s["rows"] for s in stats.values())
        print(f"\nDone. {total_rows:,} rows across {len(stats)} tables in {elapsed:.1f}s.\n")

    if not cfg.sqlite_path.exists():
        print(f"ERROR: DB not found at {cfg.sqlite_path}", file=sys.stderr)
        return 2

    print("=" * 78)
    schema = introspect_schema(cfg.sqlite_path)
    print(format_schema_for_llm(schema))
    return 0


if __name__ == "__main__":
    sys.exit(main())
