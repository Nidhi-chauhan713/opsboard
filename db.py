"""SQLite access layer.

Design notes
- One connection per request (SQLite connections are cheap; sharing them across
  threads is not safe).
- WAL mode: readers never block the single writer, writers never block readers.
- Every write runs inside `BEGIN IMMEDIATE`, which takes the write lock up front.
  That makes each write transaction serialisable and avoids the classic SQLite
  "deferred transaction upgrade" deadlock. busy_timeout makes concurrent writers
  wait instead of failing.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def db_path() -> str:
    return os.environ.get("OPSBOARD_DB", str(Path(__file__).resolve().parent.parent / "opsboard.db"))


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_iso(offset_seconds: float = 0) -> str:
    return iso(now() + timedelta(seconds=offset_seconds))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_PATH.read_text())
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """Serialised write transaction. Commits on success, rolls back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
