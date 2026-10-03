"""HTTP layer: request parsing, auth dependency, error mapping. No business rules here."""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import services
from .auth import CurrentUser, create_session, current_user, get_conn, verify_password
from .db import init_db, write_tx
from .errors import ApiError
from .worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    worker = None
    if os.environ.get("OPSBOARD_WORKER", "1") == "1":
        worker = Worker()
        worker.start()
    yield
    if worker:
        worker.stop()


app = FastAPI(title="OpsBoard", version="1.0", lifespan=lifespan)


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status, content=exc.body())


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"error": {
        "code": "invalid_request", "message": "Request validation failed",
        "details": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]}})


# ---------------------------------------------------------------- request models

Kind = Literal["customer_issue", "engineering", "payment", "incident", "compliance", "ops_task"]
Priority = Literal["P1", "P2", "P3", "P4"]
Status = Literal["open", "in_progress", "awaiting_approval", "approved", "rejected", "resolved", "closed"]


class LoginIn(BaseModel):
    email: str
    password: str


class ItemCreate(BaseModel):
    team_id: int
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20000)
    kind: Kind
    priority: Priority = "P3"
    requires_approval: bool = False
    assignee_id: Optional[int] = None
    due_at: Optional[str] = None


class ItemUpdate(BaseModel):
    version: int
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=20000)
    kind: Optional[Kind] = None
    priority: Optional[Priority] = None
    requires_approval: Optional[bool] = None
    due_at: Optional[str] = None


class AssignIn(BaseModel):
    version: int
    assignee_id: Optional[int] = None


class TransitionIn(BaseModel):
    version: int
    to: Status
    comment: Optional[str] = Field(default=None, max_length=10000)


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class MembershipIn(BaseModel):
    role: Optional[Literal["viewer", "member", "lead"]] = None


class ReadIn(BaseModel):
    ids: Optional[list[int]] = None  # None = mark all read


IdemKey = Header(default=None, alias="Idempotency-Key")


# ---------------------------------------------------------------- auth

@app.post("/api/login")
def login(body: LoginIn, conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM users WHERE email = ?", (body.email.strip().lower(),)).fetchone()
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise ApiError(401, "invalid_credentials", "Wrong email or password")
    with write_tx(conn):
        token = create_session(conn, row["id"])
    return {"token": token}


@app.post("/api/logout")
def logout(authorization: Optional[str] = Header(default=None), conn=Depends(get_conn)):
    if authorization and authorization.lower().startswith("bearer "):
        with write_tx(conn):
            conn.execute("DELETE FROM sessions WHERE token = ?", (authorization.split(" ", 1)[1],))
    return {"ok": True}


@app.get("/api/me")
def me(user: CurrentUser = Depends(current_user), conn=Depends(get_conn)):
    teams = conn.execute("SELECT id, name FROM teams ORDER BY name").fetchall()
    return {
        "id": user.id, "name": user.name, "email": user.email, "is_admin": user.is_admin,
        "teams": [{"id": t["id"], "name": t["name"], "role": user.role_in(t["id"])}
                  for t in teams if user.role_in(t["id"])],
    }


# ---------------------------------------------------------------- teams

@app.get("/api/teams/{team_id}/members")
def members(team_id: int, user: CurrentUser = Depends(current_user), conn=Depends(get_conn)):
    return services.team_members(conn, user, team_id)


@app.put("/api/teams/{team_id}/members/{member_id}")
def put_member(team_id: int, member_id: int, body: MembershipIn,
               user: CurrentUser = Depends(current_user), conn=Depends(get_conn)):
    return services.set_membership(conn, user, team_id, member_id, body.role)


# ---------------------------------------------------------------- work items

@app.get("/api/dashboard")
def get_dashboard(user: CurrentUser = Depends(current_user), conn=Depends(get_conn)):
    return services.dashboard(conn, user)


@app.get("/api/items")
def list_items(
    team_id: Optional[int] = None,
    status: list[str] = Query(default=[]),
    priority: list[str] = Query(default=[]),
    kind: list[str] = Query(default=[]),
    assignee: Optional[str] = None,
    q: Optional[str] = Query(default=None, max_length=200),
    overdue: bool = False,
    sort: str = "priority",
    cursor: Optional[str] = None,
    limit: int = 25,
    user: CurrentUser = Depends(current_user), conn=Depends(get_conn),
):
    return services.search_items(conn, user, team_id=team_id, statuses=status, priorities=priority,
                                 kinds=kind, assignee=assignee, q=q, sort=sort, cursor=cursor,
                                 limit=limit, overdue=overdue)


@app.post("/api/items", status_code=201)
def create_item(body: ItemCreate, idem: Optional[str] = IdemKey,
                user: CurrentUser = Depends(current_user), conn=Depends(get_conn)):
    status, result = services.create_item(conn, user, body.model_dump(), idem)
    return JSONResponse(status_code=status, content=result)


@app.get("/api/items/{item_id}")
def get_item(item_id: int, user: CurrentUser = Depends(current_user), conn=Depends(get_conn)):
    from .auth import load_item_for
    load_item_for(conn, user, item_id, "viewer")
    item = services.get_item_dict(conn, item_id)
    item["permissions"] = _permissions(conn, user, item)
    return item


def _permissions(conn, user: CurrentUser, item: dict) -> dict:
    """Hints for the UI only. The server re-checks every one of these on mutation."""
    from .auth import can_edit_item, load_item_for
    from .workflow import TRANSITIONS, check_transition
    row = load_item_for(conn, user, item["id"], "viewer")
    allowed = []
    for to in sorted(TRANSITIONS.get(item["status"], ())):
        try:
            check_transition(user, row, to, "x")
            allowed.append(to)
        except ApiError:
            pass
    is_lead = user.has_role(item["team_id"], "lead")
    return {
        "edit": can_edit_item(user, row) and item["status"] != "closed",
        "comment": user.has_role(item["team_id"], "member"),
        "claim": user.has_role(item["team_id"], "member") and item["assignee_id"] is None
                 and item["status"] in ("open", "rejected", "in_progress"),
        "assign": is_lead and item["status"] not in ("resolved", "closed"),
        "release": item["assignee_id"] == user.id and item["status"] not in ("resolved", "closed"),
        "change_approval": is_lead,
        "transitions": allowed,
    }


@app.patch("/api/items/{item_id}")
def patch_item(item_id: int, body: ItemUpdate, user: CurrentUser = Depends(current_user), conn=Depends(get_conn)):
    changes = body.model_dump(exclude_unset=True)
    version = changes.pop("version")
    return services.update_item(conn, user, item_id, version, changes)


@app.post("/api/items/{item_id}/claim")
def claim(item_id: int, idem: Optional[str] = IdemKey,
          user: CurrentUser = Depends(current_user), conn=Depends(get_conn)):
    status, result = services.claim_item(conn, user, item_id, idem)
    return JSONResponse(status_code=status, content=result)


@app.post("/api/items/{item_id}/assign")
def assign(item_id: int, body: AssignIn, user: CurrentUser = Depends(current_user), conn=Depends(get_conn)):
    return services.assign_item(conn, user, item_id, body.version, body.assignee_id)


@app.post("/api/items/{item_id}/transition")
def transition(item_id: int, body: TransitionIn, idem: Optional[str] = IdemKey,
               user: CurrentUser = Depends(current_user), conn=Depends(get_conn)):
    status, result = services.transition_item(conn, user, item_id, body.version, body.to, body.comment, idem)
    return JSONResponse(status_code=status, content=result)


@app.get("/api/items/{item_id}/activity")
def activity(item_id: int, before: Optional[int] = None, after: Optional[int] = None, limit: int = 50,
             user: CurrentUser = Depends(current_user), conn=Depends(get_conn)):
    return services.list_activity(conn, user, item_id, before, after, limit)


@app.post("/api/items/{item_id}/comments", status_code=201)
def comment(item_id: int, body: CommentIn, idem: Optional[str] = IdemKey,
            user: CurrentUser = Depends(current_user), conn=Depends(get_conn)):
    status, result = services.add_comment(conn, user, item_id, body.body, idem)
    return JSONResponse(status_code=status, content=result)


# ---------------------------------------------------------------- notifications

@app.get("/api/notifications")
def notifications(unread_only: bool = False, limit: int = 30,
                  user: CurrentUser = Depends(current_user), conn=Depends(get_conn)):
    where = "user_id = ?" + (" AND read = 0" if unread_only else "")
    rows = conn.execute(f"SELECT * FROM notifications WHERE {where} ORDER BY id DESC LIMIT ?",
                        (user.id, max(1, min(limit, 100)))).fetchall()
    unread = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0", (user.id,)).fetchone()[0]
    return {"unread": unread, "items": [dict(r) for r in rows]}


@app.post("/api/notifications/read")
def mark_read(body: ReadIn, user: CurrentUser = Depends(current_user), conn=Depends(get_conn)):
    with write_tx(conn):
        if body.ids is None:
            conn.execute("UPDATE notifications SET read = 1 WHERE user_id = ?", (user.id,))
        elif body.ids:
            conn.execute(f"UPDATE notifications SET read = 1 WHERE user_id = ? AND id IN "
                         f"({','.join('?' * len(body.ids))})", (user.id, *body.ids))
    return {"ok": True}


# ---------------------------------------------------------------- ops

@app.get("/api/admin/outbox")
def outbox_status(user: CurrentUser = Depends(current_user), conn=Depends(get_conn)):
    if not user.is_admin:
        raise ApiError(403, "forbidden", "Admins only")
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM outbox GROUP BY status")}
    dead = conn.execute("SELECT id, topic, attempts, last_error FROM outbox WHERE status='dead' "
                        "ORDER BY id DESC LIMIT 20").fetchall()
    return {"counts": counts, "recent_dead": [dict(r) for r in dead]}


@app.get("/api/health")
def health(conn=Depends(get_conn)):
    conn.execute("SELECT 1")
    return {"ok": True}


# ---------------------------------------------------------------- frontend

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "index.html")
