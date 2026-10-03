"""Workflow rules and resource-level authorization, enforced by the API (not the UI)."""


def approval_item(env):
    """A payment item owned by alice, in progress."""
    item = env.create("alice", kind="payment", assignee_id=env.ids["alice"])
    assert item["requires_approval"] is True  # payments always need approval
    assert env.transition(item["id"], "in_progress", "alice").status_code == 200
    return item


def test_cannot_skip_approval(env):
    item = approval_item(env)
    r = env.transition(item["id"], "resolved", "alice")
    assert r.status_code == 422 and r.json()["error"]["code"] == "workflow_violation"


def test_full_approval_flow_with_separation_of_duties(env):
    item = approval_item(env)
    # lead1 takes over and requests approval himself
    r = env.client.post(f"/api/items/{item['id']}/assign",
                        json={"version": env.get(item["id"])["version"], "assignee_id": env.ids["lead1"]},
                        headers=env.h("lead1"))
    assert r.status_code == 200
    assert env.transition(item["id"], "awaiting_approval", "lead1").status_code == 200

    # the requester may not approve their own request, even as a lead
    r = env.transition(item["id"], "approved", "lead1")
    assert r.status_code == 422 and "cannot approve" in r.json()["error"]["message"]
    # a member may not approve at all
    assert env.transition(item["id"], "approved", "alice").status_code == 403
    # another lead can
    assert env.transition(item["id"], "approved", "lead2").status_code == 200
    assert env.transition(item["id"], "resolved", "lead1").status_code == 200
    assert env.transition(item["id"], "closed", "alice").status_code == 200  # requester closes


def test_reject_requires_reason_and_reopening_requires_new_approval(env):
    item = approval_item(env)
    assert env.transition(item["id"], "awaiting_approval", "alice").status_code == 200
    assert env.transition(item["id"], "rejected", "lead1").status_code == 422
    assert env.transition(item["id"], "rejected", "lead1", comment="Missing invoice").status_code == 200
    assert env.transition(item["id"], "in_progress", "alice").status_code == 200
    assert env.get(item["id"])["approval_requested_by"] is None  # old approval request cleared
    assert env.transition(item["id"], "resolved", "alice").status_code == 422


def test_illegal_edge_rejected(env):
    item = env.create("alice", assignee_id=env.ids["alice"])
    r = env.transition(item["id"], "closed", "lead1")
    assert r.status_code == 422


def test_work_cannot_start_without_owner(env):
    item = env.create("alice")
    r = env.transition(item["id"], "in_progress", "lead1")
    assert r.status_code == 422 and "claim" in r.json()["error"]["message"]


def test_viewer_can_read_but_not_write(env):
    item = env.create("alice")
    assert env.client.get(f"/api/items/{item['id']}", headers=env.h("victor")).status_code == 200
    body = {"team_id": env.teams["Payments"], "title": "x", "kind": "ops_task"}
    assert env.client.post("/api/items", json=body, headers=env.h("victor")).status_code == 403
    assert env.client.post(f"/api/items/{item['id']}/claim", headers=env.h("victor")).status_code == 403
    assert env.client.post(f"/api/items/{item['id']}/comments", json={"body": "hi"},
                           headers=env.h("victor")).status_code == 403
    perms = env.client.get(f"/api/items/{item['id']}", headers=env.h("victor")).json()["permissions"]
    assert perms["transitions"] == [] and not perms["claim"] and not perms["edit"]


def test_other_team_items_are_invisible(env):
    item = env.create("alice")
    # carol (Platform only) gets 404 — not 403 — so IDs from other teams don't leak
    for method, url, body in [
        ("get", f"/api/items/{item['id']}", None),
        ("get", f"/api/items/{item['id']}/activity", None),
        ("post", f"/api/items/{item['id']}/claim", None),
        ("post", f"/api/items/{item['id']}/comments", {"body": "x"}),
        ("patch", f"/api/items/{item['id']}", {"version": 1, "title": "pwned"}),
    ]:
        r = getattr(env.client, method)(url, headers=env.h("carol"), **({"json": body} if body else {}))
        assert r.status_code == 404, (method, url, r.status_code)
    listing = env.client.get("/api/items?status=", headers=env.h("carol")).json()
    assert listing["items"] == []
    r = env.client.get(f"/api/items?team_id={env.teams['Payments']}", headers=env.h("carol"))
    assert r.status_code == 404
    body = {"team_id": env.teams["Payments"], "title": "x", "kind": "ops_task"}
    assert env.client.post("/api/items", json=body, headers=env.h("carol")).status_code == 404


def test_member_cannot_edit_someone_elses_item(env):
    item = env.create("alice")
    r = env.client.patch(f"/api/items/{item['id']}", json={"version": 1, "title": "x"}, headers=env.h("bob"))
    assert r.status_code == 403
    r = env.client.patch(f"/api/items/{item['id']}", json={"version": 1, "title": "mine"}, headers=env.h("alice"))
    assert r.status_code == 200


def test_approval_requirement_is_protected(env):
    item = env.create("alice", kind="ops_task")
    r = env.client.patch(f"/api/items/{item['id']}", json={"version": 1, "requires_approval": True},
                         headers=env.h("alice"))
    assert r.status_code == 403  # members can't change approval rules
    pay = env.create("alice", kind="payment")
    r = env.client.patch(f"/api/items/{pay['id']}", json={"version": 1, "requires_approval": False},
                         headers=env.h("lead1"))
    assert r.status_code == 422  # not even leads for payments


def test_only_leads_reassign_and_assignee_must_be_team_member(env):
    item = env.create("alice", assignee_id=env.ids["alice"])
    r = env.client.post(f"/api/items/{item['id']}/assign", json={"version": 1, "assignee_id": env.ids["bob"]},
                        headers=env.h("alice"))
    assert r.status_code == 403
    r = env.client.post(f"/api/items/{item['id']}/assign", json={"version": 1, "assignee_id": env.ids["carol"]},
                        headers=env.h("lead1"))
    assert r.status_code == 400  # carol isn't in Payments
    r = env.client.post(f"/api/items/{item['id']}/assign", json={"version": 1, "assignee_id": None},
                        headers=env.h("alice"))
    assert r.status_code == 200  # but alice may release her own item


def test_unauthenticated_requests_rejected(env):
    assert env.client.get("/api/items").status_code == 401
    assert env.client.get("/api/items", headers={"Authorization": "Bearer nope"}).status_code == 401
