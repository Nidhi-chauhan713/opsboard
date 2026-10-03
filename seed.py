"""Create demo teams, users and a realistic volume of work items.

    python seed.py            # 5,000 items (default)
    python seed.py --items 50000 --reset

Every demo user's password is: password
"""
import argparse
import json
import os
import random
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.auth import hash_password  # noqa: E402
from app.db import connect, db_path, init_db, iso, now  # noqa: E402

TEAMS = ["Payments", "Platform", "Customer Support", "Compliance"]
USERS = [
    # email, name, is_admin, {team: role}
    ("admin@opsboard.dev", "Ada Admin", 1, {}),
    ("lead.payments@opsboard.dev", "Priya Lead", 0, {"Payments": "lead", "Compliance": "member"}),
    ("lead.platform@opsboard.dev", "Leo Lead", 0, {"Platform": "lead", "Payments": "lead"}),
    ("alice@opsboard.dev", "Alice Member", 0, {"Payments": "member", "Customer Support": "member"}),
    ("bob@opsboard.dev", "Bob Member", 0, {"Payments": "member", "Platform": "member"}),
    ("carol@opsboard.dev", "Carol Member", 0, {"Platform": "member"}),
    ("dave@opsboard.dev", "Dave Support", 0, {"Customer Support": "lead"}),
    ("erin@opsboard.dev", "Erin Compliance", 0, {"Compliance": "lead"}),
    ("victor@opsboard.dev", "Victor Viewer", 0, {"Payments": "viewer", "Platform": "viewer"}),
]
KINDS = ["customer_issue", "engineering", "payment", "incident", "compliance", "ops_task"]
TEAM_KINDS = {"Payments": ["payment", "customer_issue", "incident"],
              "Platform": ["engineering", "incident", "ops_task"],
              "Customer Support": ["customer_issue", "ops_task"],
              "Compliance": ["compliance", "ops_task"]}
SUBJECTS = ["Refund", "Chargeback", "Login failure", "Latency spike", "Duplicate invoice", "KYC review",
            "Payout delay", "Database failover", "Certificate expiry", "Data export request",
            "Vendor approval", "API timeout", "Webhook retries", "Ledger mismatch", "Access request"]
OBJECTS = ["for enterprise customer", "in EU region", "on checkout", "for merchant #{n}",
           "after deploy", "in nightly batch", "on mobile app", "for invoice INV-{n}"]
STATUSES = ["open"] * 5 + ["in_progress"] * 3 + ["awaiting_approval", "approved", "resolved", "closed", "closed"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--items", type=int, default=5000)
    p.add_argument("--reset", action="store_true")
    args = p.parse_args()

    if args.reset and os.path.exists(db_path()):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db_path() + suffix):
                os.remove(db_path() + suffix)
    init_db()
    conn = connect()
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        print("Database already seeded (use --reset to start over)")
        return

    rng = random.Random(42)
    ts = iso(now())
    pw = hash_password("password")
    conn.execute("BEGIN")
    team_ids = {}
    for name in TEAMS:
        team_ids[name] = conn.execute("INSERT INTO teams(name) VALUES (?)", (name,)).lastrowid
    team_members: dict[int, list[int]] = {t: [] for t in team_ids.values()}
    for email, name, is_admin, roles in USERS:
        uid = conn.execute("INSERT INTO users(email,name,password_hash,is_admin,created_at) VALUES (?,?,?,?,?)",
                           (email, name, pw, is_admin, ts)).lastrowid
        for team, role in roles.items():
            conn.execute("INSERT INTO memberships(user_id,team_id,role) VALUES (?,?,?)",
                         (uid, team_ids[team], role))
            if role != "viewer":
                team_members[team_ids[team]].append(uid)

    base = now() - timedelta(days=120)
    for i in range(args.items):
        team = rng.choice(TEAMS)
        tid = team_ids[team]
        kind = rng.choice(TEAM_KINDS[team])
        status = rng.choice(STATUSES)
        priority = rng.choices(["P1", "P2", "P3", "P4"], weights=[1, 3, 6, 3])[0]
        members = team_members[tid]
        assignee = None if status == "open" and rng.random() < 0.6 else rng.choice(members)
        creator = rng.choice(members)
        req_appr = 1 if kind in ("payment", "compliance") or status in ("awaiting_approval", "approved") else 0
        if not req_appr and status in ("awaiting_approval", "approved"):
            status = "in_progress"
        created = base + timedelta(minutes=rng.randint(0, 120 * 24 * 60))
        updated = created + timedelta(minutes=rng.randint(0, 3000))
        due = iso(created + timedelta(days=rng.randint(1, 30))) if rng.random() < 0.4 else None
        title = f"{rng.choice(SUBJECTS)} {rng.choice(OBJECTS).format(n=rng.randint(1000, 9999))}"
        approval_by = assignee if status == "awaiting_approval" else None
        item_id = conn.execute(
            "INSERT INTO work_items(team_id,title,description,kind,priority,priority_rank,status,requires_approval,"
            "assignee_id,created_by,approval_requested_by,due_at,version,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (tid, title, f"Seeded item #{i}. Reported via {rng.choice(['email', 'chat', 'phone', 'monitoring'])}.",
             kind, priority, int(priority[1]), status, req_appr, assignee, creator, approval_by, due,
             iso(created), iso(updated))).lastrowid
        conn.execute("INSERT INTO activity(item_id,actor_id,type,data,created_at) VALUES (?,?,?,?,?)",
                     (item_id, creator, "created", json.dumps({"title": title, "assignee_id": assignee}),
                      iso(created)))
    conn.execute("COMMIT")
    conn.execute("ANALYZE")
    print(f"Seeded {len(TEAMS)} teams, {len(USERS)} users, {args.items} items into {db_path()}")
    print("Log in with any of these (password: password):")
    for email, name, is_admin, roles in USERS:
        print(f"  {email:32} {name:18} {'ADMIN' if is_admin else roles}")


if __name__ == "__main__":
    main()
