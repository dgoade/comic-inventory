"""Shared Postgres connection for migrate + ETL."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def project_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "migrations").is_dir():
            return candidate
    return here.parents[2]


def load_project_env(path: Path | None = None) -> bool:
    """Load KEY=value pairs from the project ``.env``.

    Variables already set in the process environment are left unchanged.
    A missing file is not an error.
    """
    env_file = project_root() / ".env" if path is None else path
    return load_dotenv(env_file)


def psycopg2():
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 is required. Install the project with: poetry install")
    return psycopg2


def load_database_url() -> str:
    load_project_env()
    url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not url:
        sys.exit(
            "Set DATABASE_URL (or SUPABASE_DB_URL) in the environment or in .env. "
            "Copy .env.example to .env and fill it in."
        )
    if ":6543" in url.split("?")[0]:
        sys.exit(
            "Refusing to run through the transaction pooler (port 6543).\n"
            "Use session-mode pooler (port 5432 on the pooler host) or a direct connection."
        )
    if "sslmode=" not in url and "supabase" in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url


def connect():
    conn = psycopg2().connect(load_database_url())
    conn.set_session(autocommit=False)
    return conn
