"""Retries must never duplicate work (user unsure whether the first request succeeded)."""
import threading

from app import services
from app.db import connect


def count(env, sql, *params):
    conn = env.db()
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def test_retried_create_returns_same_item(env):
    body = {"team_id": env.teams["Payments"], "title": "Chargeback", "kind": "customer_issue", "priority": "P2"}
    r1 = env.client.post("/api/items", json=body, headers=env.h("alice", idem="k-create-1"))
    r2 = env.client.post("/api/items", json=body, headers=env.h("alice", idem="k-create-1"))
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["idempotent_replay"] is True
    assert count(env, "SELECT COUNT(*) FROM work_items WHERE title='Chargeback'") == 1


def test_concurrent_duplicate_requests_create_once(env):
    """Double-click / client retry racing the original request."""
    data = {"team_id": env.teams["Payments"], "title": "Race", "kind": "ops_task", "priority": "P3"}
    barrier = threading.Barrier(8)
    ids = []

    def go():
        barrier.wait()
        conn = connect()
        try:
            _, body = services.create_item(conn, env.user("alice", conn), dict(data), "same-key")
            ids.append(body["id"])
        finally:
            conn.close()

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(set(ids)) == 1
    assert count(env, "SELECT COUNT(*) FROM work_items WHERE title='Race'") == 1


def test_key_reused_for_different_request_is_rejected(env):
    base = {"team_id": env.teams["Payments"], "kind": "ops_task", "priority": "P3"}
    assert env.client.post("/api/items", json={**base, "title": "A"}, headers=env.h("alice", "k1")).status_code == 201
    r = env.client.post("/api/items", json={**base, "title": "B"}, headers=env.h("alice", "k1"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "idempotency_key_reused"


def test_keys_are_scoped_per_user(env):
    body = {"team_id": env.teams["Payments"], "title": "Scoped", "kind": "ops_task", "priority": "P3"}
    a = env.client.post("/api/items", json=body, headers=env.h("alice", "shared")).json()
    b = env.client.post("/api/items", json=body, headers=env.h("bob", "shared")).json()
    assert a["id"] != b["id"]


def test_retried_comment_and_transition_apply_once(env):
    item = env.create("alice", assignee_id=env.ids["alice"])
    for _ in range(3):
        r = env.client.post(f"/api/items/{item['id']}/comments", json={"body": "On it"}, headers=env.h("alice", "c-1"))
        assert r.status_code == 201
    v = item["version"]
    for _ in range(3):  # client retries with the SAME version + key after a timeout
        r = env.transition(item["id"], "in_progress", "alice", version=v, idem="t-1")
        assert r.status_code == 200
    assert count(env, "SELECT COUNT(*) FROM activity WHERE item_id=? AND type='commented'", item["id"]) == 1
    assert count(env, "SELECT COUNT(*) FROM activity WHERE item_id=? AND type='transitioned'", item["id"]) == 1
    assert env.get(item["id"])["version"] == v + 1


def test_failed_request_is_not_cached_so_retry_can_succeed(env):
    item = env.create("alice")
    # bob is not yet allowed to start work (no owner) -> fails, nothing stored
    r = env.transition(item["id"], "in_progress", "bob", version=item["version"], idem="t-fail")
    assert r.status_code == 422
    assert count(env, "SELECT COUNT(*) FROM idempotency_keys WHERE key='t-fail'") == 0
    # and nothing partial was written (atomic rollback)
    assert count(env, "SELECT COUNT(*) FROM outbox") == 1  # only the 'created' event
    assert env.client.post(f"/api/items/{item['id']}/claim", headers=env.h("bob")).status_code == 200
    v = env.get(item["id"])["version"]
    r = env.transition(item["id"], "in_progress", "bob", version=v, idem="t-ok")
    assert r.status_code == 200
