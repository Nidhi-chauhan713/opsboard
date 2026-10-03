"""Asynchronous processing: atomic enqueue, retries, dead-lettering, duplicate delivery."""
from app import worker
from app.db import now_iso


def q(env, sql, *params):
    conn = env.db()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def make_all_ready(env):
    conn = env.db()
    conn.execute("UPDATE outbox SET available_at = ? WHERE status='pending'", (now_iso(-1),))
    conn.close()


def test_change_and_job_are_written_atomically(env):
    item = env.create("lead1", assignee_id=env.ids["bob"])
    before = len(q(env, "SELECT id FROM outbox"))
    r = env.transition(item["id"], "closed", "lead1")  # illegal -> rolled back
    assert r.status_code == 422
    assert len(q(env, "SELECT id FROM outbox")) == before  # no orphan job for a change that never happened
    assert env.transition(item["id"], "in_progress", "bob").status_code == 200
    assert len(q(env, "SELECT id FROM outbox")) == before + 1


def test_notifications_delivered_to_others_not_actor(env):
    item = env.create("lead1", assignee_id=env.ids["bob"])
    env.client.post(f"/api/items/{item['id']}/comments", json={"body": "ping"}, headers=env.h("bob"))
    stats = worker.process_batch()
    assert stats["done"] == 2 and stats["retry"] == 0
    rows = q(env, "SELECT user_id, message FROM notifications ORDER BY id")
    recipients = [r["user_id"] for r in rows]
    assert env.ids["bob"] in recipients            # told about the item created for him
    assert env.ids["lead1"] in recipients          # told about bob's comment
    assert recipients.count(env.ids["bob"]) == 1   # not notified about his own comment


def test_failures_retry_with_backoff_then_dead_letter(env):
    env.create("lead1", assignee_id=env.ids["bob"])
    calls = []

    def flaky(conn, payload):
        calls.append(payload)
        raise RuntimeError("smtp down")

    for attempt in range(1, worker.MAX_ATTEMPTS + 1):
        stats = worker.process_batch({"notify": flaky})
        job = q(env, "SELECT * FROM outbox")[0]
        assert job["attempts"] == attempt
        if attempt < worker.MAX_ATTEMPTS:
            assert stats["retry"] == 1 and job["status"] == "pending"
            assert job["available_at"] > now_iso()  # backoff: not immediately re-run
            assert worker.process_batch({"notify": flaky}) == {"done": 0, "retry": 0, "dead": 0}
            make_all_ready(env)
        else:
            assert stats["dead"] == 1 and job["status"] == "dead"
            assert "smtp down" in job["last_error"]
    assert len(calls) == worker.MAX_ATTEMPTS
    # the primary action was never affected by the failing side effect
    assert q(env, "SELECT COUNT(*) n FROM work_items")[0]["n"] == 1


def test_transient_failure_then_success(env):
    env.create("lead1", assignee_id=env.ids["bob"])
    state = {"n": 0}

    def fails_once(conn, payload):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("timeout")
        worker.handle_notify(conn, payload)

    assert worker.process_batch({"notify": fails_once})["retry"] == 1
    make_all_ready(env)
    assert worker.process_batch({"notify": fails_once})["done"] == 1
    assert len(q(env, "SELECT id FROM notifications")) == 1


def test_duplicate_execution_is_harmless(env):
    """At-least-once delivery: the same job running twice must not double-notify."""
    env.create("lead1", assignee_id=env.ids["bob"])
    worker.process_batch()
    # simulate a worker that processed the job but crashed before marking it done
    conn = env.db()
    conn.execute("UPDATE outbox SET status='pending', locked_until=NULL, available_at=?", (now_iso(-1),))
    conn.close()
    worker.process_batch()
    assert len(q(env, "SELECT id FROM notifications")) == 1


def test_expired_lease_is_picked_up_again(env):
    env.create("lead1", assignee_id=env.ids["bob"])
    conn = env.db()
    leased = worker.lease_batch(conn, "crashed-worker")
    assert len(leased) == 1
    assert worker.lease_batch(conn, "other") == []  # still leased: nobody else may take it
    conn.execute("UPDATE outbox SET locked_until = ?", (now_iso(-1),))  # lease expires (worker died)
    conn.close()
    assert worker.process_batch()["done"] == 1
