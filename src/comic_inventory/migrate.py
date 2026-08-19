#!/usr/bin/env python3
"""Numbered SQL migrations (Alembic-like, psycopg2, no SQLAlchemy).

  poetry run inventory-migrate status
  poetry run inventory-migrate up
  poetry run inventory-migrate up --to 0001
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$", re.IGNORECASE)

ENSURE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version     text PRIMARY KEY,
    name        text NOT NULL,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def project_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "migrations").is_dir():
            return candidate
    return here.parents[2]


def migrations_dir() -> Path:
    return project_root() / "migrations"


def _psycopg2():
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 is required. Install the project with: poetry install")
    return psycopg2


def load_database_url() -> str:
    url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not url:
        sys.exit(
            "Set DATABASE_URL (or SUPABASE_DB_URL) to a session-mode/direct "
            "Postgres URL. Copy .env.example to .env and fill it in."
        )
    if ":6543" in url.split("?")[0]:
        sys.exit(
            "Refusing to run DDL through the transaction pooler (port 6543).\n"
            "Use session-mode pooler (port 5432 on the pooler host) or a direct connection."
        )
    if "sslmode=" not in url and "supabase" in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url


def discover() -> list[tuple[str, str, Path]]:
    directory = migrations_dir()
    if not directory.is_dir():
        sys.exit(f"Migrations directory not found: {directory}")
    found = []
    for path in sorted(directory.iterdir()):
        match = FILE_RE.match(path.name)
        if match:
            found.append((match.group(1), match.group(2), path))
    if not found:
        sys.exit(f"No NNNN_name.sql files in {directory}")
    return found


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def connect():
    psycopg2 = _psycopg2()
    conn = psycopg2.connect(load_database_url())
    conn.set_session(autocommit=False)
    return conn


def ensure_table(conn) -> None:
    old = conn.autocommit
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(ENSURE_TABLE_SQL)
    conn.autocommit = old


def applied_rows(conn) -> dict[str, tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version, name, checksum FROM public.schema_migrations ORDER BY version"
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def cmd_status(conn) -> int:
    ensure_table(conn)
    applied = applied_rows(conn)
    files = discover()
    print(f"{'version':<8} {'name':<32} status")
    print("-" * 60)
    pending = 0
    for version, name, path in files:
        if version in applied:
            _, stored = applied[version]
            flag = "applied"
            if stored != checksum(path):
                flag = "applied (FILE CHANGED since apply — do not edit applied migrations)"
            print(f"{version:<8} {name:<32} {flag}")
        else:
            pending += 1
            print(f"{version:<8} {name:<32} pending")
    extra = set(applied) - {v for v, _, _ in files}
    for version in sorted(extra):
        print(f"{version:<8} {applied[version][0]:<32} applied (file missing)")
    print("-" * 60)
    print(f"{len(applied)} applied, {pending} pending")
    return 0


def apply_one(conn, version: str, name: str, path: Path) -> None:
    sql = path.read_text()
    digest = checksum(path)
    print(f"Applying {version}_{name} ...")
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            """
            INSERT INTO public.schema_migrations (version, name, checksum)
            VALUES (%s, %s, %s)
            """,
            (version, name, digest),
        )
    conn.commit()
    print(f"  ok  {version}_{name}")


def cmd_up(conn, stop_at: str | None) -> int:
    ensure_table(conn)
    applied = applied_rows(conn)
    files = discover()
    if stop_at is not None and not any(v == stop_at for v, _, _ in files):
        sys.exit(f"No migration file for --to {stop_at}")

    ran = 0
    for version, name, path in files:
        if version in applied:
            _, stored = applied[version]
            if stored != checksum(path):
                print(
                    f"WARNING: {version}_{name} was already applied but the file changed.",
                    file=sys.stderr,
                )
            if stop_at is not None and version == stop_at:
                break
            continue
        apply_one(conn, version, name, path)
        ran += 1
        if stop_at is not None and version == stop_at:
            break
    if ran == 0:
        print("Already up to date.")
    else:
        print(f"Applied {ran} migration(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Show applied vs pending migrations")
    up = sub.add_parser("up", help="Apply pending migrations")
    up.add_argument(
        "--to", dest="stop_at", metavar="VERSION", help="Stop after this version (e.g. 0001)"
    )
    args = parser.parse_args(argv)

    conn = connect()
    try:
        if args.command == "status":
            return cmd_status(conn)
        if args.command == "up":
            return cmd_up(conn, args.stop_at)
        parser.error(f"unknown command {args.command}")
        return 2
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
