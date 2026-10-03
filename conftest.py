import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["OPSBOARD_WORKER"] = "0"  # tests drive the worker explicitly

from fastapi.testclient import TestClient  # noqa: E402

from app.auth import hash_password, load_user  # noqa: E402
from app.db import connect, init_db, now_iso  # noqa: E402
from app.main import app  # noqa: E402

PW = hash_password("pw", iterations=1000)

USERS = {
    # name: (is_admin, {team: role})
    "lead1": (0, {"Payments": "lead"}),
    "lead2": (0, {"Payments": "lead"}),
    "alice": (0, {"Payments": "member"}),
    "bob": (0, {"Payments": "member"}),
    "carol": (0, {"Platform": "member"}),
    "victor": (0, {"Payments": "viewer"}),
    "admin": (1, {}),
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPSBOARD_DB", str(tmp_path / "test.db"))
    init_db()
    conn = connect()
    teams = {name: conn.execute("INSERT INTO teams(name) VALUES (?)", (name,)).lastrowid
             for name in ("Payments", "Platform")}
    ids = {}
    for name, (is_admin, roles) in USERS.items():
        ids[name] = conn.execute(
            "INSERT INTO users(email,name,password_hash,is_admin,created_at) VALUES (?,?,?,?,?)",
            (f"{name}@t.dev", name, PW, is_admin, now_iso())).lastrowid
        for team, role in roles.items():
            conn.execute("INSERT INTO memberships(user_id,team_id,role) VALUES (?,?,?)", (ids[name], teams[team], role))
    conn.close()

    with TestClient(app) as client:
        tokens = {}
        for name in USERS:
            r = client.post("/api/login", json={"email": f"{name}@t.dev", "password": "pw"})
            assert r.status_code == 200, r.text
            tokens[name] = r.json()["token"]
        yield Env(client, teams, ids, tokens)


class Env:
    def __init__(self, client, teams, ids, tokens):
        self.client, self.teams, self.ids, self.tokens = client, teams, ids, tokens

    def h(self, who, idem=None):
        headers = {"Authorization": f"Bearer {self.tokens[who]}"}
        if idem:
            headers["Idempotency-Key"] = idem
        return headers

    def user(self, who, conn=None):
        own = conn is None
        conn = conn or connect()
        try:
            return load_user(conn, self.tokens[who])
        finally:
            if own:
                conn.close()

    def create(self, who="alice", team="Payments", **kw):
        body = {"team_id": self.teams[team], "title": kw.pop("title", "Refund for ACME"),
                "kind": kw.pop("kind", "customer_issue"), "priority": kw.pop("priority", "P2"), **kw}
        r = self.client.post("/api/items", json=body, headers=self.h(who))
        assert r.status_code == 201, r.text
        return r.json()

    def get(self, item_id, who="lead1"):
        return self.client.get(f"/api/items/{item_id}", headers=self.h(who)).json()

    def transition(self, item_id, to, who, version=None, comment=None, idem=None):
        version = version if version is not None else self.get(item_id, who)["version"]
        return self.client.post(f"/api/items/{item_id}/transition",
                                json={"version": version, "to": to, "comment": comment},
                                headers=self.h(who, idem))

    def db(self):
        return connect()
