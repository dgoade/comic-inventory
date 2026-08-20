#!/usr/bin/env python3
"""Dump all rows in schema inventory (does not touch public).

  poetry run inventory-backup
  poetry run inventory-backup --sql
  poetry run inventory-backup --with-schema
  poetry run inventory-backup --out /path/to/file.dump
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from psycopg2 import sql

from comic_inventory.db import connect, load_database_url, project_root


def _pg_dump() -> str:
    path = shutil.which("pg_dump")
    if not path:
        sys.exit(
            "pg_dump not found on PATH. Install Postgres client tools "
            "(e.g. brew install libpq && brew link --force libpq)."
        )
    return path


def default_backup_path(directory: Path, sql: bool) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = ".sql" if sql else ".dump"
    return directory / f"inventory-{stamp}{suffix}"


def inventory_table_counts(conn) -> list[tuple[str, int]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'inventory'
              AND c.relkind = 'r'
            ORDER BY c.relname
            """
        )
        tables = [row[0] for row in cur.fetchall()]
        counts: list[tuple[str, int]] = []
        for table in tables:
            cur.execute(
                sql.SQL("SELECT count(*) FROM inventory.{}").format(
                    sql.Identifier(table)
                )
            )
            counts.append((table, cur.fetchone()[0]))
    return counts


def run_pg_dump(
    *,
    output: Path,
    data_only: bool,
    plain_sql: bool,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _pg_dump(),
        f"--dbname={load_database_url()}",
        "--schema=inventory",
        "--no-owner",
        "--no-acl",
        "-v",
        "-f",
        str(output),
    ]
    if data_only:
        cmd.append("--data-only")
    cmd.extend(["-F", "p" if plain_sql else "c"])
    subprocess.run(cmd, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--sql",
        action="store_true",
        help="Plain SQL INSERTs instead of pg_dump custom format",
    )
    parser.add_argument(
        "--with-schema",
        action="store_true",
        help="Include inventory DDL (can drift from numbered migrations)",
    )
    parser.add_argument("--out", metavar="PATH", help="Output file path")
    parser.add_argument(
        "--dir",
        metavar="DIR",
        default=str(project_root() / "backups"),
        help="Directory for the timestamped file (default: ./backups)",
    )
    args = parser.parse_args(argv)

    if args.out:
        output = Path(args.out)
    else:
        output = default_backup_path(Path(args.dir), sql=args.sql)

    print(f"Writing {output} ...", flush=True)
    run_pg_dump(
        output=output,
        data_only=not args.with_schema,
        plain_sql=args.sql,
    )
    print(f"Wrote {output} ({output.stat().st_size} bytes)")

    conn = connect()
    try:
        counts = inventory_table_counts(conn)
    finally:
        conn.close()

    print(f"{'table':<28} rows")
    print("-" * 36)
    for name, n in counts:
        print(f"{name:<28} {n}")
    print("-" * 36)
    print(f"{len(counts)} tables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
