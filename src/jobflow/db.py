from __future__ import annotations

import os
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[2]


def connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg.connect(url)


def migrate() -> None:
    sql = (ROOT / "migrations" / "001_initial.sql").read_text()
    with connect() as conn:
        conn.execute(sql)
