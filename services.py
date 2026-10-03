"""Business logic. Route handlers stay thin; everything that must be correct lives here.

Every mutating function:
  1. runs inside ONE write transaction (db.write_tx),
  2. re-reads and re-authorizes the item inside that transaction,
  3. applies the change with a version-guarded UPDATE,
  4. appends to the activity log and the outbox in the same transaction.

So either the change, its history entry and its notification job all exist, or none do.
"""
import base64
import hashlib
import json
import sqlite3
from typing import Any, Callable, Optional

from .auth import CurrentUser, can_edit_item, load_item_for
from .db import now_iso, write_tx
from .errors import ApiError, bad_request, forbidden, not_found
from .workflow import ACTIVE_STATUSES, check_transition

PRIORITY_RANK = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
EDITABLE_FIELDS = ("title", "description", "priority", "kind", "due_at", "requires_approval")


# ---------------------------------------------------------------- serialisation

ITEM_SELECT = """
SELECT w.*, a.name AS assignee_name, c.name AS creator_name, t.name AS team_name
FROM work_items w
JOIN teams t ON t.id = w.team_id
JOIN users c ON c.id = w.created_by
LEFT JOIN users a ON a.id = w.assignee_id
"""


def item_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "team_id": row["team_id"],
        "team_name": row["team_name"],
        "title": row["title"],
        "description": row["description"],
        "kind": row["kind"],
        "priority": row["priority"],
        "status": row["status"],
        "requires_approval": bool(row["requires_approval"]),
        "assignee_id": row["assignee_id"],
        "assignee_name": row["assignee_name"],
        "created_by": row["created_by"],
        "creator_name": row["creator_name"],
        "approval_requested_by": row["approval_requested_by"],
        "due_at": row["due_at"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_item_dict(conn: sqlite3.Connection, item_id: int) -> dict:
    row = conn.execute(ITEM_SELECT + " WHERE w.id = ?", (item_id,)).fetchone()
    if row is None:
        raise not_found("Work item")
    return item_to_dict(row)


def conflict(conn: sqlite3.Connection, item_id: int, message: str, code: str = "version_conflict") -> ApiError:
    """409 that carries the current server state so the client can reconcile without another round-trip."""
    return ApiError(409, code, message, current=get_item_dict(conn, item_id))


# ---------------------------------------------------------------- history + outbox

def record(conn, item_id: int, actor_id: int, type_: str, data: dict) -> int:
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO activity(item_id, actor_id, type, data, created_at) VALUES (?,?,?,?,?)",
        (item_id, actor_id, type_, json.dumps(data), ts),
    )
    activity_id = cur.lastrowid
    conn.execute(
        "INSERT INTO outbox(topic, payload, available_at, created_at) VALUES (?,?,?,?)",
        ("notify", json.dumps({"activity_id": activity_id}), ts, ts),
    )
    return activity_id


# ---------------------------------------------------------------- idempotency

def idempotent(conn, user: CurrentUser, key: Optional[str], scope: str, payload: Any,
               fn: Callable[[], tuple[int, dict]]) -> tuple[int, dict]:
    """Run `fn` at most once per (user, Idempotency-Key).

    Must be called INSIDE a write transaction: BEGIN IMMEDIATE serialises
    concurrent duplicates, so the second one always sees the first one's stored
    response. Only successful results are stored — a failed attempt rolls back,
    so retrying it re-executes, which is what a client expects.
    """
    if not key:
        return fn()
    if len(key) > 200:
        raise bad_request("Idempotency-Key too long")
    request_hash = hashlib.sha256(json.dumps([scope, payload], sort_keys=True, default=str).encode()).hexdigest()
    row = conn.execute(
        "SELECT request_hash, status_code, response FROM idempotency_keys WHERE user_id=? AND key=?",
        (user.id, key),
    ).fetchone()
    if row:
        if row["request_hash"] != request_hash:
            raise ApiError(422, "idempotency_key_reused", "This Idempotency-Key was already used for a different request")
        body = json.loads(row["response"])
        body["idempotent_replay"] = True
        return row["status_code"], body
    status, body = fn()
    conn.execute(
        "INSERT INTO idempotency_keys(user_id, key, request_hash, status_code, response, created_at) VALUES (?,?,?,?,?,?)",
        (user.id, key, request_hash, status, json.dumps(body), now_iso()),
    )
    return status, body


# ---------------------------------------------------------------- validation helpers

def _validate_fields(fields: dict) -> dict:
    out = dict(fields)
    if "title" in out:
        out["title"] = (out["title"] or "").strip()
        if not out["title"]:
            raise bad_request("Title is required")
        if len(out["title"]) > 200:
            raise bad_request("Title must be at most 200 characters")
    if "description" in out and out["description"] is not None and len(out["description"]) > 20000:
        raise bad_request("Description too long")
    if "priority" in out and out["priority"] not in PRIORITY_RANK:
        raise bad_request("Priority must be one of P1..P4")
    if "kind" in out and out["kind"] not in (
        "customer_issue", "engineering", "payment", "incident", "compliance", "ops_task"
    ):
        raise bad_request("Unknown kind")
    if "requires_approval" in out:
        out["requires_approval"] = 1 if out["requires_approval"] else 0
    return out


def _assert_assignable(conn, team_id: int, user_id: int) -> None:
    row = conn.execute(
        "SELECT role FROM memberships WHERE team_id=? AND user_id=?", (team_id, user_id)
    ).fetchone()
    if row is None or row["role"] == "viewer":
        raise bad_request("Assignee must be a member or lead of the item's team", "invalid_assignee")


# ---------------------------------------------------------------- commands

def create_item(conn, user: CurrentUser, data: dict, idem_key: Optional[str]) -> tuple[int, dict]:
    with write_tx(conn):
        def run():
            team_id = data.get("team_id")
            if user.role_in(team_id) is None:
                raise not_found("Team")
            if not user.has_role(team_id, "member"):
                raise forbidden("Viewers cannot create work items")
            f = _validate_fields({k: data.get(k) for k in ("title", "description", "priority", "kind",
                                                           "due_at", "requires_approval")})
            assignee_id = data.get("assignee_id")
            if assignee_id is not None:
                if assignee_id != user.id and not user.has_role(team_id, "lead"):
                    raise forbidden("Only leads can assign work to someone else")
                _assert_assignable(conn, team_id, assignee_id)
            # Only leads may create items that skip approval for sensitive kinds.
            if f["kind"] in ("payment", "compliance") and not f.get("requires_approval"):
                f["requires_approval"] = 1
            ts = now_iso()
            cur = conn.execute(
                "INSERT INTO work_items(team_id,title,description,kind,priority,priority_rank,status,"
                "requires_approval,assignee_id,created_by,due_at,version,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,'open',?,?,?,?,1,?,?)",
                (team_id, f["title"], f.get("description") or "", f["kind"], f["priority"],
                 PRIORITY_RANK[f["priority"]], f.get("requires_approval", 0), assignee_id,
                 user.id, f.get("due_at"), ts, ts),
            )
            item_id = cur.lastrowid
            record(conn, item_id, user.id, "created", {"title": f["title"], "assignee_id": assignee_id})
            return 201, get_item_dict(conn, item_id)

        return idempotent(conn, user, idem_key, "create", data, run)


def update_item(conn, user: CurrentUser, item_id: int, version: int, changes: dict) -> dict:
    """Optimistic concurrency: the UPDATE only applies if nobody changed the row since `version`."""
    changes = {k: v for k, v in changes.items() if k in EDITABLE_FIELDS}
    if not changes:
        raise bad_request("No editable fields supplied")
    changes = _validate_fields(changes)
    with write_tx(conn):
        item = load_item_for(conn, user, item_id, "member")
        if not can_edit_item(user, item):
            raise forbidden("Only the owner, the requester or a team lead can edit this item")
        if "requires_approval" in changes and changes["requires_approval"] != item["requires_approval"]:
            if not user.has_role(item["team_id"], "lead"):
                raise forbidden("Only a team lead can change whether approval is required")
            if item["kind"] in ("payment", "compliance") and not changes["requires_approval"]:
                raise ApiError(422, "workflow_violation", "Payment and compliance items always require approval")
        if item["status"] == "closed":
            raise ApiError(422, "workflow_violation", "Closed items are read-only; reopen first")

        diff = {k: [item[k], v] for k, v in changes.items() if item[k] != v}
        if not diff:
            if item["version"] != version:
                raise conflict(conn, item_id, "Item changed since you loaded it")
            return get_item_dict(conn, item_id)

        sets = ", ".join(f"{k} = ?" for k in changes)
        params = list(changes.values())
        if "priority" in changes:
            sets += ", priority_rank = ?"
            params.append(PRIORITY_RANK[changes["priority"]])
        cur = conn.execute(
            f"UPDATE work_items SET {sets}, version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
            (*params, now_iso(), item_id, version),
        )
        if cur.rowcount == 0:
            raise conflict(conn, item_id, "Item changed since you loaded it — review the latest version and retry")
        record(conn, item_id, user.id, "updated", diff)
        return get_item_dict(conn, item_id)


def claim_item(conn, user: CurrentUser, item_id: int, idem_key: Optional[str]) -> tuple[int, dict]:
    """'Take ownership' — must be safe when two people click at the same moment.

    The guard lives in the WHERE clause (assignee_id IS NULL), so exactly one
    UPDATE can succeed no matter how requests interleave. A second click by the
    *same* user is treated as success (natural idempotency).
    """
    with write_tx(conn):
        def run():
            item = load_item_for(conn, user, item_id, "member")
            if item["status"] not in ("open", "rejected", "in_progress"):
                raise ApiError(422, "workflow_violation", f"Cannot claim an item in status '{item['status']}'")
            cur = conn.execute(
                "UPDATE work_items SET assignee_id = ?, version = version + 1, updated_at = ? "
                "WHERE id = ? AND assignee_id IS NULL",
                (user.id, now_iso(), item_id),
            )
            if cur.rowcount == 0:
                current = get_item_dict(conn, item_id)
                if current["assignee_id"] == user.id:
                    return 200, {**current, "already_owned": True}
                raise ApiError(409, "already_claimed",
                               f"Already owned by {current['assignee_name']}", current=current)
            record(conn, item_id, user.id, "claimed", {"assignee_id": [None, user.id]})
            return 200, get_item_dict(conn, item_id)

        return idempotent(conn, user, idem_key, f"claim:{item_id}", {}, run)


def assign_item(conn, user: CurrentUser, item_id: int, version: int, assignee_id: Optional[int]) -> dict:
    with write_tx(conn):
        item = load_item_for(conn, user, item_id, "member")
        is_lead = user.has_role(item["team_id"], "lead")
        releasing_self = assignee_id is None and item["assignee_id"] == user.id
        if not (is_lead or releasing_self):
            raise forbidden("Only a team lead can reassign; owners may only release their own items")
        if item["status"] in ("resolved", "closed"):
            raise ApiError(422, "workflow_violation", "Cannot reassign a resolved or closed item")
        if assignee_id is not None:
            _assert_assignable(conn, item["team_id"], assignee_id)
        new_status = item["status"]
        if assignee_id is None and item["status"] == "in_progress":
            new_status = "open"  # nobody is working on it any more
        cur = conn.execute(
            "UPDATE work_items SET assignee_id = ?, status = ?, version = version + 1, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (assignee_id, new_status, now_iso(), item_id, version),
        )
        if cur.rowcount == 0:
            raise conflict(conn, item_id, "Item changed since you loaded it — review and retry")
        data = {"assignee_id": [item["assignee_id"], assignee_id]}
        if new_status != item["status"]:
            data["status"] = [item["status"], new_status]
        record(conn, item_id, user.id, "assigned", data)
        return get_item_dict(conn, item_id)


def transition_item(conn, user: CurrentUser, item_id: int, version: int, to: str,
                    comment: Optional[str], idem_key: Optional[str]) -> tuple[int, dict]:
    with write_tx(conn):
        def run():
            item = load_item_for(conn, user, item_id, "viewer")
            if item["version"] != version:
                raise conflict(conn, item_id, "Item changed since you loaded it — review and retry")
            check_transition(user, item, to, comment)

            approval_by = item["approval_requested_by"]
            if to == "awaiting_approval":
                approval_by = user.id
            elif to in ("in_progress", "open"):
                approval_by = None  # any new cycle needs a fresh approval request
            cur = conn.execute(
                "UPDATE work_items SET status = ?, approval_requested_by = ?, version = version + 1, "
                "updated_at = ? WHERE id = ? AND version = ?",
                (to, approval_by, now_iso(), item_id, version),
            )
            if cur.rowcount == 0:  # defensive: cannot happen under BEGIN IMMEDIATE, but never trust it
                raise conflict(conn, item_id, "Item changed since you loaded it")
            data: dict = {"status": [item["status"], to]}
            if comment and comment.strip():
                data["comment"] = comment.strip()
            record(conn, item_id, user.id, "transitioned", data)
            return 200, get_item_dict(conn, item_id)

        return idempotent(conn, user, idem_key, f"transition:{item_id}",
                          {"to": to, "version": version, "comment": comment}, run)


def add_comment(conn, user: CurrentUser, item_id: int, body: str, idem_key: Optional[str]) -> tuple[int, dict]:
    body = (body or "").strip()
    if not body:
        raise bad_request("Comment cannot be empty")
    if len(body) > 10000:
        raise bad_request("Comment too long")
    with write_tx(conn):
        def run():
            load_item_for(conn, user, item_id, "member")
            # Comments don't bump the item version: commenting must never cause an
            # editor's pending field change to be rejected as stale.
            conn.execute("UPDATE work_items SET updated_at = ? WHERE id = ?", (now_iso(), item_id))
            activity_id = record(conn, item_id, user.id, "commented", {"body": body})
            return 201, activity_dict(conn.execute(ACTIVITY_SELECT + " WHERE a.id = ?", (activity_id,)).fetchone())

        return idempotent(conn, user, idem_key, f"comment:{item_id}", {"body": body}, run)


# ---------------------------------------------------------------- queries

ACTIVITY_SELECT = "SELECT a.*, u.name AS actor_name FROM activity a JOIN users u ON u.id = a.actor_id"


def activity_dict(row) -> dict:
    return {"id": row["id"], "item_id": row["item_id"], "actor_id": row["actor_id"],
            "actor_name": row["actor_name"], "type": row["type"],
            "data": json.loads(row["data"]), "created_at": row["created_at"]}


def list_activity(conn, user: CurrentUser, item_id: int, before: Optional[int], after: Optional[int],
                  limit: int) -> dict:
    load_item_for(conn, user, item_id, "viewer")
    limit = max(1, min(limit, 200))
    if after is not None:  # incremental polling: everything newer than what the client has
        rows = conn.execute(ACTIVITY_SELECT + " WHERE a.item_id = ? AND a.id > ? ORDER BY a.id ASC LIMIT ?",
                            (item_id, after, limit)).fetchall()
        return {"items": [activity_dict(r) for r in rows], "next_before": None}
    params: list = [item_id]
    where = "a.item_id = ?"
    if before is not None:
        where += " AND a.id < ?"
        params.append(before)
    rows = conn.execute(ACTIVITY_SELECT + f" WHERE {where} ORDER BY a.id DESC LIMIT ?",
                        (*params, limit + 1)).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [activity_dict(r) for r in rows],
            "next_before": rows[-1]["id"] if has_more and rows else None}


def _encode_cursor(values: list) -> str:
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode()


def _decode_cursor(cursor: str) -> list:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except Exception:
        raise bad_request("Invalid cursor")


def _fts_query(q: str) -> Optional[str]:
    """Turn free text into a safe FTS5 query: every word is a quoted prefix term (AND-ed)."""
    words = [w.replace('"', "") for w in q.split()]
    words = [w for w in words if w]
    if not words:
        return None
    return " ".join(f'"{w}"*' for w in words[:10])


def search_items(conn, user: CurrentUser, *, team_id: Optional[int], statuses: list[str],
                 priorities: list[str], kinds: list[str], assignee: Optional[str], q: Optional[str],
                 sort: str, cursor: Optional[str], limit: int, overdue: bool = False) -> dict:
    """Filtered, keyset-paginated list.

    Keyset (a.k.a. seek) pagination instead of OFFSET: the cost of page N is the
    same as page 1, and rows inserted while you scroll don't shift or duplicate
    results the way OFFSET does.
    """
    limit = max(1, min(limit, 100))
    visible = user.visible_team_ids(conn)
    if team_id is not None:
        if team_id not in visible:
            raise not_found("Team")
        visible = [team_id]
    if not visible:
        return {"items": [], "next_cursor": None}

    where = [f"w.team_id IN ({','.join('?' * len(visible))})"]
    params: list = list(visible)
    if statuses:
        if statuses == ["active"]:
            statuses = list(ACTIVE_STATUSES)
        where.append(f"w.status IN ({','.join('?' * len(statuses))})")
        params += statuses
    if priorities:
        where.append(f"w.priority IN ({','.join('?' * len(priorities))})")
        params += priorities
    if kinds:
        where.append(f"w.kind IN ({','.join('?' * len(kinds))})")
        params += kinds
    if assignee == "me":
        where.append("w.assignee_id = ?")
        params.append(user.id)
    elif assignee == "unassigned":
        where.append("w.assignee_id IS NULL")
    elif assignee:
        try:
            params.append(int(assignee))
        except ValueError:
            raise bad_request("assignee must be 'me', 'unassigned' or a user id")
        where.append("w.assignee_id = ?")
    if overdue:
        where.append("w.due_at IS NOT NULL AND w.due_at < ? AND w.status IN ('open','in_progress','awaiting_approval','approved','rejected')")
        params.append(now_iso())
    if q:
        fq = _fts_query(q)
        if fq:
            where.append("w.id IN (SELECT rowid FROM work_items_fts WHERE work_items_fts MATCH ?)")
            params.append(fq)

    if sort == "priority":
        order = "w.priority_rank ASC, w.id DESC"
        if cursor:
            rank, last_id = _decode_cursor(cursor)
            where.append("(w.priority_rank > ? OR (w.priority_rank = ? AND w.id < ?))")
            params += [rank, rank, last_id]
    elif sort == "updated":
        order = "w.updated_at DESC, w.id DESC"
        if cursor:
            ts, last_id = _decode_cursor(cursor)
            where.append("(w.updated_at < ? OR (w.updated_at = ? AND w.id < ?))")
            params += [ts, ts, last_id]
    elif sort == "created":
        order = "w.id DESC"
        if cursor:
            (last_id,) = _decode_cursor(cursor)
            where.append("w.id < ?")
            params.append(last_id)
    else:
        raise bad_request("sort must be priority, updated or created")

    sql = ITEM_SELECT + " WHERE " + " AND ".join(where) + f" ORDER BY {order} LIMIT ?"
    rows = conn.execute(sql, (*params, limit + 1)).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(
            [last["priority_rank"], last["id"]] if sort == "priority"
            else [last["updated_at"], last["id"]] if sort == "updated"
            else [last["id"]]
        )
    return {"items": [item_to_dict(r) for r in rows], "next_cursor": next_cursor}


def dashboard(conn, user: CurrentUser) -> dict:
    """Small set of COUNTs that answer 'what needs my attention?'. Each hits an index."""
    teams = user.visible_team_ids(conn)
    if not teams:
        return {"my_active": 0, "unassigned": 0, "p1_active": 0, "awaiting_my_approval": 0, "overdue": 0}
    ph = ",".join("?" * len(teams))
    active = "('open','in_progress','awaiting_approval','approved','rejected')"

    def count(sql, params):
        return conn.execute(sql, params).fetchone()[0]

    lead_teams = [t for t in teams if user.has_role(t, "lead")]
    awaiting = 0
    if lead_teams:
        lph = ",".join("?" * len(lead_teams))
        awaiting = count(
            f"SELECT COUNT(*) FROM work_items WHERE team_id IN ({lph}) AND status='awaiting_approval' "
            f"AND (approval_requested_by IS NULL OR approval_requested_by != ?)",
            (*lead_teams, user.id))
    return {
        "my_active": count(f"SELECT COUNT(*) FROM work_items WHERE assignee_id = ? AND status IN {active}", (user.id,)),
        "unassigned": count(f"SELECT COUNT(*) FROM work_items WHERE team_id IN ({ph}) AND assignee_id IS NULL "
                            f"AND status IN {active}", teams),
        "p1_active": count(f"SELECT COUNT(*) FROM work_items WHERE team_id IN ({ph}) AND priority='P1' "
                           f"AND status IN {active}", teams),
        "awaiting_my_approval": awaiting,
        "overdue": count(f"SELECT COUNT(*) FROM work_items WHERE team_id IN ({ph}) AND due_at IS NOT NULL "
                         f"AND due_at < ? AND status IN {active}", (*teams, now_iso())),
    }


def team_members(conn, user: CurrentUser, team_id: int) -> list[dict]:
    if user.role_in(team_id) is None:
        raise not_found("Team")
    rows = conn.execute(
        "SELECT u.id, u.name, u.email, m.role FROM memberships m JOIN users u ON u.id = m.user_id "
        "WHERE m.team_id = ? ORDER BY u.name", (team_id,)).fetchall()
    return [dict(r) for r in rows]


def set_membership(conn, user: CurrentUser, team_id: int, member_id: int, role: Optional[str]) -> dict:
    if user.role_in(team_id) is None:
        raise not_found("Team")
    if not user.has_role(team_id, "lead"):
        raise forbidden("Only team leads can manage membership")
    if role is not None and role not in ("viewer", "member", "lead"):
        raise bad_request("role must be viewer, member or lead")
    with write_tx(conn):
        if conn.execute("SELECT 1 FROM users WHERE id=?", (member_id,)).fetchone() is None:
            raise not_found("User")
        if role is None:
            owned = conn.execute(
                f"SELECT COUNT(*) FROM work_items WHERE team_id=? AND assignee_id=? AND status IN {tuple(ACTIVE_STATUSES)}",
                (team_id, member_id)).fetchone()[0]
            if owned:
                raise ApiError(422, "has_active_work", f"User still owns {owned} active item(s); reassign them first")
            conn.execute("DELETE FROM memberships WHERE team_id=? AND user_id=?", (team_id, member_id))
        else:
            conn.execute(
                "INSERT INTO memberships(user_id, team_id, role) VALUES (?,?,?) "
                "ON CONFLICT(user_id, team_id) DO UPDATE SET role = excluded.role",
                (member_id, team_id, role))
    return {"team_id": team_id, "user_id": member_id, "role": role}
