"""Asynchronous processing via a transactional outbox.

Why an outbox: the primary action (e.g. a status change) and the "please notify
people" job are written in the SAME transaction. If the process crashes right
after commit, the job is still on disk and will be picked up later. Nothing is
lost, and no notification is ever sent for a change that rolled back.

Delivery semantics: at-least-once.
  * A job is leased (locked_until) before processing. If the worker dies mid-job,
    the lease expires and another worker retries it.
  * Failures are retried with exponential backoff; after MAX_ATTEMPTS the job is
    parked as 'dead' (visible at /api/admin/outbox) instead of retrying forever.
  * Because a job may run more than once, the handler is idempotent:
    notifications have UNIQUE(activity_id, user_id) and are inserted with
    INSERT OR IGNORE, so duplicates are harmless.
"""
import json
import logging
import os
import random
import threading
import time
from typing import Callable, Optional

from .db import connect, now_iso, write_tx

log = logging.getLogger("opsboard.worker")

MAX_ATTEMPTS = 5
LEASE_SECONDS = 30
BATCH_SIZE = 50


def backoff_seconds(attempts: int) -> float:
    return min(300, 2 ** attempts)  # 2, 4, 8, 16 ... capped at 5 min


# ---------------------------------------------------------------- handlers

def _describe(activity, actor_name: str, title: str) -> str:
    data = json.loads(activity["data"])
    t = activity["type"]
    if t == "transitioned":
        return f"{actor_name} moved “{title}” to {data['status'][1].replace('_', ' ')}"
    if t == "commented":
        return f"{actor_name} commented on “{title}”"
    if t in ("claimed", "assigned"):
        return f"{actor_name} changed the owner of “{title}”"
    if t == "created":
        return f"{actor_name} created “{title}”"
    return f"{actor_name} updated “{title}”"


def handle_notify(conn, payload: dict) -> None:
    activity = conn.execute("SELECT * FROM activity WHERE id = ?", (payload["activity_id"],)).fetchone()
    if activity is None:
        return  # nothing to do; treat as success (cannot ever succeed later)
    item = conn.execute("SELECT * FROM work_items WHERE id = ?", (activity["item_id"],)).fetchone()
    actor = conn.execute("SELECT name FROM users WHERE id = ?", (activity["actor_id"],)).fetchone()
    data = json.loads(activity["data"])

    recipients = {item["assignee_id"], item["created_by"]}
    if "assignee_id" in data and isinstance(data["assignee_id"], list):
        recipients.add(data["assignee_id"][0])  # previous owner hears they lost it
    if item["status"] == "awaiting_approval" and activity["type"] == "transitioned":
        leads = conn.execute("SELECT user_id FROM memberships WHERE team_id=? AND role='lead'", (item["team_id"],))
        recipients |= {r["user_id"] for r in leads}
    recipients.discard(None)
    recipients.discard(activity["actor_id"])  # don't notify people about their own actions

    message = _describe(activity, actor["name"], item["title"])
    with write_tx(conn):
        for uid in recipients:
            conn.execute(
                "INSERT OR IGNORE INTO notifications(user_id, item_id, activity_id, message, created_at) "
                "VALUES (?,?,?,?,?)",
                (uid, item["id"], activity["id"], message, now_iso()),
            )


HANDLERS: dict[str, Callable] = {"notify": handle_notify}

# Test / demo hook: OPSBOARD_FAIL_RATE=0.3 makes 30% of jobs fail to exercise retries.
FAIL_RATE = float(os.environ.get("OPSBOARD_FAIL_RATE", "0"))


# ---------------------------------------------------------------- engine

def lease_batch(conn, worker_id: str, limit: int = BATCH_SIZE) -> list:
    """Atomically pick ready jobs and lease them so no other worker takes them."""
    ts = now_iso()
    with write_tx(conn):
        rows = conn.execute(
            "SELECT * FROM outbox WHERE status='pending' AND available_at <= ? "
            "AND (locked_until IS NULL OR locked_until < ?) ORDER BY id LIMIT ?",
            (ts, ts, limit),
        ).fetchall()
        if rows:
            conn.execute(
                f"UPDATE outbox SET locked_until = ?, attempts = attempts + 1 "
                f"WHERE id IN ({','.join('?' * len(rows))})",
                (now_iso(LEASE_SECONDS), *[r["id"] for r in rows]),
            )
    return rows


def process_batch(handlers: Optional[dict] = None, worker_id: str = "w1") -> dict:
    handlers = handlers or HANDLERS
    conn = connect()
    stats = {"done": 0, "retry": 0, "dead": 0}
    try:
        for job in lease_batch(conn, worker_id):
            attempts = job["attempts"] + 1
            try:
                if FAIL_RATE and random.random() < FAIL_RATE:
                    raise RuntimeError("injected failure")
                handlers[job["topic"]](conn, json.loads(job["payload"]))
            except Exception as exc:  # noqa: BLE001 - any failure must be recorded, never lost
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                dead = attempts >= MAX_ATTEMPTS
                with write_tx(conn):
                    conn.execute(
                        "UPDATE outbox SET status = ?, locked_until = NULL, last_error = ?, available_at = ? "
                        "WHERE id = ?",
                        ("dead" if dead else "pending", repr(exc)[:500],
                         now_iso(backoff_seconds(attempts)), job["id"]),
                    )
                stats["dead" if dead else "retry"] += 1
                log.warning("job %s failed (attempt %s): %r", job["id"], attempts, exc)
            else:
                with write_tx(conn):
                    conn.execute("UPDATE outbox SET status='done', locked_until=NULL WHERE id=?", (job["id"],))
                stats["done"] += 1
    finally:
        conn.close()
    return stats


class Worker:
    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="outbox-worker", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.is_set():
            try:
                stats = process_batch()
                busy = sum(stats.values()) > 0
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("worker loop error")
                busy = False
            self._stop.wait(0 if busy else self.interval)
