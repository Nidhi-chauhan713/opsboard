"""Concurrency: the behaviours most likely to silently corrupt data if wrong.

These tests race real threads, each with its own SQLite connection, against the
service layer — the same code path HTTP requests take.
"""
import threading

from app import services
from app.db import connect
from app.errors import ApiError


def race(n, fn):
    """Run fn(i) in n threads released at the same instant. Returns list of (ok, value)."""
    barrier = threading.Barrier(n)
    results = [None] * n

    def run(i):
        barrier.wait()
        try:
            results[i] = (True, fn(i))
        except ApiError as e:
            results[i] = (False, e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_two_people_claiming_same_item_exactly_one_wins(env):
    item = env.create("alice")
    claimers = ["alice", "bob", "lead1", "lead2"] * 3  # 12 concurrent attempts, 4 distinct people

    def attempt(i):
        conn = connect()
        try:
            return services.claim_item(conn, env.user(claimers[i], conn), item["id"], None)
        finally:
            conn.close()

    results = race(len(claimers), attempt)
    winner_id = env.get(item["id"])["assignee_id"]
    winner = next(n for n, uid in env.ids.items() if uid == winner_id)

    for i, (ok, val) in enumerate(results):
        if claimers[i] == winner:
            assert ok  # the winner's duplicate clicks are harmless successes
        else:
            assert not ok and val.status == 409 and val.code == "already_claimed"
            assert val.extra["current"]["assignee_id"] == winner_id  # loser learns who won

    conn = env.db()
    claims = conn.execute("SELECT COUNT(*) FROM activity WHERE item_id=? AND type='claimed'", (item["id"],)).fetchone()[0]
    assert claims == 1, "history must show exactly one successful claim"


def test_stale_edit_is_rejected_and_returns_current_state(env):
    item = env.create("alice")
    v1 = item["version"]
    # lead changes priority while alice still has v1 on screen
    r = env.client.patch(f"/api/items/{item['id']}", json={"version": v1, "priority": "P1"}, headers=env.h("lead1"))
    assert r.status_code == 200 and r.json()["version"] == v1 + 1

    r = env.client.patch(f"/api/items/{item['id']}", json={"version": v1, "title": "New title"}, headers=env.h("alice"))
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "version_conflict"
    assert err["current"]["priority"] == "P1" and err["current"]["version"] == v1 + 1

    # nothing from the rejected write leaked through
    assert env.get(item["id"])["title"] == "Refund for ACME"

    # after reviewing, alice retries against the version she has now seen
    r = env.client.patch(f"/api/items/{item['id']}", json={"version": v1 + 1, "title": "New title"}, headers=env.h("alice"))
    assert r.status_code == 200
    assert r.json()["priority"] == "P1" and r.json()["title"] == "New title"  # both changes survive


def test_simultaneous_edits_from_same_version_no_lost_update(env):
    item = env.create("alice")
    editors = ["alice", "lead1", "lead2"] * 4

    def attempt(i):
        conn = connect()
        try:
            return services.update_item(conn, env.user(editors[i], conn), item["id"], item["version"],
                                        {"description": f"edit {i}"})
        finally:
            conn.close()

    results = race(len(editors), attempt)
    oks = [v for ok, v in results if ok]
    assert len(oks) == 1
    assert all(v.status == 409 for ok, v in results if not ok)
    assert env.get(item["id"])["description"] == oks[0]["description"]


def test_stale_transition_rejected(env):
    item = env.create("alice", assignee_id=env.ids["alice"])
    v = item["version"]
    assert env.transition(item["id"], "in_progress", "alice", version=v).status_code == 200
    # a second tab still showing the old version tries to act on it
    r = env.transition(item["id"], "in_progress", "lead1", version=v)
    assert r.status_code == 409
    assert r.json()["error"]["current"]["status"] == "in_progress"


def test_comments_do_not_invalidate_concurrent_editors(env):
    """A comment must not make someone's pending field edit fail as stale."""
    item = env.create("alice")
    r = env.client.post(f"/api/items/{item['id']}/comments", json={"body": "looking"}, headers=env.h("bob"))
    assert r.status_code == 201
    r = env.client.patch(f"/api/items/{item['id']}", json={"version": item["version"], "priority": "P1"},
                         headers=env.h("alice"))
    assert r.status_code == 200
