# OpsBoard — operational work coordination

A first usable version of an internal app that replaces "chat + spreadsheets + email" for
operational work: customer issues, engineering problems, payment investigations, incidents,
compliance requests and ops tasks that need approval.

**Stack:** Python 3.11+ · FastAPI · SQLite (WAL, FTS5) · dependency-free HTML/JS frontend · pytest.

## Run it

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python seed.py --reset --items 20000     # demo teams/users + 20k work items (~2 s)
uvicorn app.main:app --port 8000         # API + UI + background worker in one process
```

Open <http://localhost:8000>. Every demo account's password is **`password`**; the login
screen has a dropdown of accounts with their roles. Good accounts to try side by side
(use two browser profiles / one private window):

| Account | Role |
|---|---|
| `alice@opsboard.dev` | member of Payments + Customer Support |
| `bob@opsboard.dev` | member of Payments + Platform |
| `lead.payments@opsboard.dev` | lead of Payments |
| `lead.platform@opsboard.dev` | lead of Platform **and** Payments (second approver) |
| `victor@opsboard.dev` | read-only viewer |
| `admin@opsboard.dev` | global admin (acts as lead everywhere; sees `/api/admin/outbox`) |

### Run the tests

```bash
cd backend
pytest -q                  # 34 tests, ~3 s
```

### Useful knobs

| Env var | Default | Purpose |
|---|---|---|
| `OPSBOARD_DB` | `backend/opsboard.db` | SQLite file path |
| `OPSBOARD_WORKER` | `1` | `0` disables the in-process outbox worker |
| `OPSBOARD_FAIL_RATE` | `0` | e.g. `0.3` makes 30 % of async jobs fail, to watch retries in the demo |

API docs (OpenAPI/Swagger) are at <http://localhost:8000/docs>.

## Demo script (≈5 minutes)

1. **Claim race** — Alice and Bob open the same unassigned Payments item and both click
   *Take ownership*. One wins; the other's optimistic UI rolls back and shows "Too late — already owned by …".
2. **Stale edit** — Alice starts editing a title; the lead changes the priority. Alice's panel shows
   a yellow "changed while you were editing" banner. If she saves anyway the server returns 409 with the
   current state; she chooses *use theirs* or *re-apply mine on top* — nothing is silently overwritten.
3. **Workflow & separation of duties** — create a *payment* item (approval is forced on). Try to resolve it
   directly → rejected. Request approval as `lead.payments`; that same lead cannot approve it, but
   `lead.platform` (also a Payments lead) can. Rejection requires a reason.
4. **Duplicate submit** — `curl` the same `POST /api/items` twice with one `Idempotency-Key`: one item.
5. **Async failure** — start with `OPSBOARD_FAIL_RATE=0.5`, do some actions, watch the log show retries
   and notifications still arrive exactly once.

## Layout

```
backend/
  app/
    schema.sql     tables, indexes, FTS5 triggers
    db.py          connections, WAL, BEGIN IMMEDIATE write transactions
    auth.py        sessions, password hashing, per-team roles, resource checks
    workflow.py    the state machine (edges + guards), pure functions
    services.py    all business logic: optimistic locking, claim, idempotency, search
    worker.py      transactional-outbox worker: leases, retries, backoff, dead letter
    main.py        HTTP layer only (validation, error mapping, routes)
  seed.py          demo data generator
  tests/           risk-focused tests (concurrency, idempotency, workflow/authz, outbox, search)
frontend/
  index.html  app.js  styles.css
ENGINEERING_DECISIONS.md
```

## Architecture

```
 Browser (vanilla JS)                         FastAPI process
 ┌──────────────────────┐   HTTPS/JSON    ┌───────────────────────────────────────┐
 │ list (keyset pages)  │ ──────────────▶ │ main.py   → auth (401/403/404)         │
 │ detail + 4s polling  │  version, Idem- │ services  → ONE write transaction:     │
 │ optimistic claim /   │  potency-Key    │   guarded UPDATE … WHERE version=?     │
 │ comment + rollback   │ ◀────────────── │   + activity row + outbox row          │
 └──────────────────────┘ 409 + current   │ worker thread → leases outbox jobs,    │
                                          │   retries w/ backoff → notifications   │
                                          └───────────────┬───────────────────────┘
                                                          ▼
                                                SQLite (WAL, FTS5)
```

## API summary

| Method & path | Notes |
|---|---|
| `POST /api/login` · `POST /api/logout` · `GET /api/me` | bearer-token sessions |
| `GET /api/dashboard` | counts: my active, unassigned, P1, awaiting my approval, overdue |
| `GET /api/items?q&team_id&status&priority&kind&assignee&overdue&sort&cursor&limit` | FTS + filters, keyset pagination |
| `POST /api/items` | `Idempotency-Key` supported |
| `GET /api/items/{id}` | includes UI permission hints |
| `PATCH /api/items/{id}` | body must include `version`; 409 + `current` on conflict |
| `POST /api/items/{id}/claim` | atomic; 409 `already_claimed`; `Idempotency-Key` |
| `POST /api/items/{id}/assign` | lead reassigns / owner releases; needs `version` |
| `POST /api/items/{id}/transition` | `{to, version, comment}`; workflow guards; `Idempotency-Key` |
| `GET /api/items/{id}/activity?before|after` | paginated history; `after` for polling |
| `POST /api/items/{id}/comments` | `Idempotency-Key` |
| `GET /api/notifications` · `POST /api/notifications/read` | produced asynchronously |
| `GET/PUT /api/teams/{id}/members[/{user}]` | leads manage roles |
| `GET /api/admin/outbox` | job counts + recent dead letters (admin) |

All errors share one shape: `{"error": {"code", "message", ...}}`.

## Known limitations

- **Polling, not push.** The detail view polls every 4 s and the dashboard every 15 s. Fine for a
  first version; SSE/WebSockets would cut latency and load (see decisions doc).
- **SQLite = single writer, single node.** Comfortable for this scale on one machine (writes are
  short transactions), but you can't run several app servers against it. The SQL is deliberately
  portable to PostgreSQL (`SELECT … FOR UPDATE SKIP LOCKED` for the worker).
- **Notifications are in-app only.** The outbox is ready for email/Slack handlers; none are wired.
- **No SSO.** Local accounts with PBKDF2 passwords; no password reset, no user-admin UI (seed script /
  membership API only).
- **Idempotency keys are never purged** (should expire after ~24 h via a periodic job).
- **No field-level merge.** A version conflict is resolved by the user choosing; we don't auto-merge
  non-overlapping fields.
- **Comment editing/deletion, attachments, SLAs, saved views and audit export** are not built.
- The UI was exercised headlessly (jsdom against the live server) and by API scripts; it has not had
  cross-browser or accessibility testing.

