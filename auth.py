"""Authentication (who are you?) and team-scoped authorization (what may you do?).

Roles are per team: viewer < member < lead. A global admin is treated as a lead
in every team. Authorization is checked on the server for every request; the UI
merely hides controls the server would reject anyway.
"""
import hashlib
import hmac
import secrets
import sqlite3
from typing import Optional

from fastapi import Header, Request

from .db import connect, now_iso
from .errors import ApiError, forbidden, not_found

ROLE_RANK = {"viewer": 1, "member": 2, "lead": 3}
SESSION_TTL_SECONDS = 12 * 3600


# ---------- passwords & sessions ----------

def hash_password(password: str, salt: Optional[bytes] = None, iterations: int = 120_000) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    _, iterations, salt_hex, digest_hex = stored.split("$")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
    return hmac.compare_digest(digest.hex(), digest_hex)


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions(token, user_id, expires_at) VALUES (?,?,?)",
        (token, user_id, now_iso(SESSION_TTL_SECONDS)),
    )
    return token


class CurrentUser:
    def __init__(self, row: sqlite3.Row, roles: dict[int, str]):
        self.id: int = row["id"]
        self.name: str = row["name"]
        self.email: str = row["email"]
        self.is_admin: bool = bool(row["is_admin"])
        self.roles = roles  # team_id -> role

    def role_in(self, team_id: int) -> Optional[str]:
        if self.is_admin:
            return "lead"
        return self.roles.get(team_id)

    def has_role(self, team_id: int, minimum: str) -> bool:
        role = self.role_in(team_id)
        return role is not None and ROLE_RANK[role] >= ROLE_RANK[minimum]

    def visible_team_ids(self, conn: sqlite3.Connection) -> list[int]:
        if self.is_admin:
            return [r["id"] for r in conn.execute("SELECT id FROM teams")]
        return list(self.roles.keys())


def load_user(conn: sqlite3.Connection, token: str) -> CurrentUser:
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_at > ?",
        (token, now_iso()),
    ).fetchone()
    if row is None:
        raise ApiError(401, "unauthenticated", "Missing, invalid or expired session")
    roles = {
        r["team_id"]: r["role"]
        for r in conn.execute("SELECT team_id, role FROM memberships WHERE user_id = ?", (row["id"],))
    }
    return CurrentUser(row, roles)


# ---------- FastAPI dependencies ----------

def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request, authorization: Optional[str] = Header(default=None)) -> CurrentUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "unauthenticated", "Missing bearer token")
    conn = connect()
    try:
        return load_user(conn, authorization.split(" ", 1)[1].strip())
    finally:
        conn.close()


# ---------- resource-level checks ----------

def load_item_for(conn: sqlite3.Connection, user: CurrentUser, item_id: int, minimum: str = "viewer") -> sqlite3.Row:
    """Fetch an item and ensure the user may act on it with at least `minimum` role.

    Non-members get 404 rather than 403 so item IDs from other teams don't leak.
    """
    row = conn.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
    if row is None or user.role_in(row["team_id"]) is None:
        raise not_found("Work item")
    if not user.has_role(row["team_id"], minimum):
        raise forbidden(f"Requires '{minimum}' role in this team")
    return row


def can_edit_item(user: CurrentUser, item: sqlite3.Row) -> bool:
    """Leads edit anything in their team; members edit what they created or own."""
    if user.has_role(item["team_id"], "lead"):
        return True
    return user.has_role(item["team_id"], "member") and user.id in (item["created_by"], item["assignee_id"])
