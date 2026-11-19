"""Wipe all data from the Pyrometer DuckDB database.

Truncates all four tables (runs, metrics, profile_events, system_snapshots)
without deleting the database file itself, so the schema stays intact.

Usage:
    python scripts/wipe_db.py          # uses default ~/.pyrometer/data.duckdb
    python scripts/wipe_db.py --yes    # skip the confirmation prompt
"""

import argparse
import os
import sys
from pathlib import Path

DB_PATH = Path(
    os.environ.get("PYROMETER_DB_PATH", Path.home() / ".pyrometer" / "data.duckdb")
)

TABLES = ["metrics", "profile_events", "system_snapshots", "runs"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Wipe all Pyrometer run data.")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        print("Nothing to wipe.")
        return

    if not args.yes:
        answer = input(f"Wipe all data in {DB_PATH}? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    import duckdb

    con = duckdb.connect(str(DB_PATH))
    for table in TABLES:
        con.execute(f"DELETE FROM {table}")
    con.close()

    print(f"Wiped {len(TABLES)} tables in {DB_PATH}")


if __name__ == "__main__":
    main()
