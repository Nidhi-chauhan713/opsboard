"""Search & pagination must stay correct as data grows and changes while users scroll."""
from app.db import now_iso


def bulk(env, n, team="Payments"):
    conn = env.db()
    conn.execute("BEGIN")
    for i in range(n):
        p = ["P1", "P2", "P3", "P4"][i % 4]
        conn.execute(
            "INSERT INTO work_items(team_id,title,description,kind,priority,priority_rank,status,created_by,"
            "version,created_at,updated_at) VALUES (?,?,?,?,?,?,'open',?,1,?,?)",
            (env.teams[team], f"Item {i} ledger" if i % 10 == 0 else f"Item {i}", "", "ops_task", p, int(p[1]),
             env.ids["alice"], now_iso(), now_iso()))
    conn.execute("COMMIT")
    conn.close()


def walk(env, params, who="alice", insert_after_first_page=0):
    seen, cursor, pages = [], None, 0
    while True:
        url = f"/api/items?limit=7&{params}" + (f"&cursor={cursor}" if cursor else "")
        r = env.client.get(url, headers=env.h(who))
        assert r.status_code == 200, r.text
        body = r.json()
        seen += [it["id"] for it in body["items"]]
        pages += 1
        if pages == 1 and insert_after_first_page:
            bulk(env, insert_after_first_page)
        cursor = body["next_cursor"]
        if not cursor:
            return seen


def test_keyset_pagination_is_complete_and_ordered(env):
    bulk(env, 60)
    for sort in ("priority", "updated", "created"):
        ids = walk(env, f"sort={sort}")
        assert len(ids) == 60 and len(set(ids)) == 60, sort


def test_no_duplicates_when_items_are_added_mid_scroll(env):
    bulk(env, 40)
    ids = walk(env, "sort=created", insert_after_first_page=15)
    assert len(ids) == len(set(ids))  # OFFSET pagination would repeat rows here
    assert len(ids) == 40             # new rows (higher ids) are above the cursor, not mixed in


def test_full_text_search_prefix_and_filters(env):
    bulk(env, 50)
    ids = walk(env, "q=ledg")
    assert len(ids) == 5
    ids = walk(env, "q=ledger&priority=P1")
    assert all(env.get(i)["priority"] == "P1" for i in ids)


def test_hostile_search_input_does_not_error(env):
    bulk(env, 5)
    for q in ['"', 'foo" OR (', "NEAR(", "*", "a AND", "ledger'; DROP TABLE work_items; --"]:
        r = env.client.get("/api/items", params={"q": q}, headers=env.h("alice"))
        assert r.status_code == 200, q


def test_search_index_follows_edits(env):
    item = env.create("alice", title="Printer jam")
    env.client.patch(f"/api/items/{item['id']}", json={"version": 1, "title": "Scanner jam"}, headers=env.h("alice"))
    assert walk(env, "q=printer") == []
    assert walk(env, "q=scanner") == [item["id"]]


def test_dashboard_counts(env):
    env.create("alice", assignee_id=env.ids["alice"], priority="P1")
    env.create("alice")
    d = env.client.get("/api/dashboard", headers=env.h("alice")).json()
    assert d["my_active"] == 1 and d["unassigned"] == 1 and d["p1_active"] == 1
