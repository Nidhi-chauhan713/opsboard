# Engineering decisions

Six decisions that shaped OpsBoard, with the alternatives I rejected and what I'd change at larger scale.

---

## 1. Optimistic concurrency with a `version` column — and an atomic claim

**Problem.** Two people edit or act on the same item; one is looking at stale data.

**Decision.** Every work item has an integer `version`. Every mutation sends the version the user
was looking at, and the write is a guarded update:

```sql
UPDATE work_items SET …, version = version + 1 WHERE id = ? AND version = ?
```

Zero rows updated ⇒ **409 `version_conflict`**, and the response carries the *current* item so the
client can show what changed and let the user choose ("use theirs" / "re-apply mine") without an
extra round-trip.

"Take ownership" is different: two people clicking *Claim* at the same moment should not get a
version error — one should simply win. So the guard is the business condition itself:
`… WHERE id = ? AND assignee_id IS NULL`. Exactly one UPDATE can match. The loser gets
**409 `already_claimed`** naming the winner; the winner clicking twice gets a harmless 200.

**Why not pessimistic locks / "lock for editing"?** Locks held across a human think-time leak
(closed tabs) and need timeouts and lock-stealing UI. Conflicts here are rare; detecting them is
cheaper than preventing them.

**Deliberately *not* versioned:** comments. A comment doesn't change the item's fields, so it must
not make someone's in-flight edit fail. (Tested.)

**Trade-off.** Users occasionally see a conflict dialog. I chose that over last-write-wins, which
silently loses work — exactly the failure the brief describes.

---

## 2. One transaction = change + history + outbox job

**Decision.** Every command (create, edit, claim, assign, transition, comment) runs inside one
`BEGIN IMMEDIATE` transaction that:

1. re-reads and **re-authorizes** the item inside the transaction (no check-then-act gap),
2. applies the guarded UPDATE,
3. appends an `activity` row (who/what/old→new), and
4. inserts an `outbox` row for asynchronous work.

Either all four happen or none. History therefore can't miss a change, and we can't notify people
about a change that rolled back.

`activity` is append-only and stores structured diffs (`{"priority": ["P3","P1"]}`), so the UI can
render "Priority P3 → P1", and the timeline answers "why/when did this change and who did it".

**Why `BEGIN IMMEDIATE`?** SQLite's default deferred transactions upgrade read locks to write locks
lazily, which can deadlock under concurrency. Taking the write lock up front serialises writers
cleanly; WAL mode keeps readers unblocked.

**Trade-off.** Duplicated data: the item holds current state *and* the log holds every change. I
chose a state table + log over pure event-sourcing because queries (filters, counts, search) need
the current state indexed, and rebuilding it from events is overkill for v1.

---

## 3. Idempotency keys for retry-prone actions

**Problem.** "A user repeating an action because they are unsure whether the first request
succeeded" — double clicks, timeouts, flaky Wi-Fi.

**Decision.** `POST /items`, `/claim`, `/transition`, `/comments` accept an `Idempotency-Key`
header. The client generates one UUID **per user intent** (per open of the New-item dialog, per
click, per comment) and reuses it on automatic retries. On the server, inside the same write
transaction as the action:

- key unseen → run the action, store `(user, key, request_hash, status, response)`;
- key seen with the same request hash → **replay the stored response** (flagged `idempotent_replay`);
- key seen with a *different* body → **422 `idempotency_key_reused`** (client bug, fail loudly).

Because the lookup and the action are in one serialised transaction, eight concurrent requests with
the same key still create exactly one item (tested with real threads). Only successes are stored —
a failed attempt rolled back, so retrying it must re-execute. Keys are scoped per user.

`PATCH` and `assign` don't need keys: they're naturally idempotent via `version` (a replay hits a
409 whose `current` already shows the change applied).

**Trade-off.** An extra table and a write per keyed request; keys need periodic purging (not built).

---

## 4. Authorization: per-team roles, enforced in the service layer, 404 for outsiders

**Decision.** Roles are per team membership: `viewer < member < lead`, plus a global `is_admin`
that acts as lead everywhere. A person can be a lead in one team and a member in another.

| Action | Rule |
|---|---|
| read | any role in the item's team |
| create, comment, claim | member+ |
| edit fields | lead, or the item's requester/owner |
| change "requires approval" | lead only; never off for payment/compliance |
| reassign | lead; an owner may only release their own item |
| approve / reject | lead **who did not request the approval** (separation of duties, admins included) |
| close | requester or lead |

Checks live in `auth.load_item_for()` + `workflow.check_transition()` and run inside the write
transaction. Users outside the team get **404, not 403**, so item IDs from other teams don't leak.
Search is scoped by `team_id IN (my teams)` in SQL, so it's impossible to list what you can't read.

The UI receives a `permissions` hint object computed *by calling the same guard functions*, so
buttons match reality — but the hint is cosmetic; every mutation is re-checked. Tests hit the API
directly as viewers/outsiders to prove that.

**Trade-off.** Roles are coarse. Per-field ACLs or custom roles would need a policy table; three
roles cover the brief's scenarios and stay explainable.

---

## 5. Asynchronous work via a transactional outbox (at-least-once + idempotent handler)

**Decision.** Notifications are secondary work and shouldn't slow down or fail the primary action.
The command writes an `outbox` row in its own transaction (decision 2). A worker thread:

1. **leases** a batch (`locked_until = now + 30s`, `attempts + 1`) inside a write transaction, so two
   workers never take the same job;
2. runs the handler;
3. on success marks it `done`; on failure sets `available_at = now + 2^attempts` (capped) and
   clears the lease; after 5 attempts marks it **`dead`** (visible at `/api/admin/outbox`) instead of
   retrying forever.

If a worker crashes mid-job, its lease expires and another worker retries it. That makes delivery
**at-least-once**, so the handler is idempotent: `notifications` has `UNIQUE(activity_id, user_id)` and
inserts with `INSERT OR IGNORE`. Running a job twice is harmless (tested by forcing re-delivery).

`OPSBOARD_FAIL_RATE` injects failures to demo this live.

**What's synchronous:** everything the user needs to see as the result of their action — the state
change, history and the response. **Async:** notifications (and any future email/Slack/enrichment).

**Why not Celery/Redis?** One fewer moving part to deploy and explain; the outbox table *is* the
durable queue, and it's transactionally consistent with the data, which a separate broker isn't
without exactly this pattern anyway.

---

## 6. Scale: indexed SQL, keyset pagination, FTS5 — nothing loaded wholesale

**Decision.**

- **Keyset pagination** (`WHERE (priority_rank, id) > (?, ?) ORDER BY … LIMIT n+1`) with an opaque
  cursor, never `OFFSET`. Page 500 costs the same as page 1, and items created while a user scrolls
  don't cause duplicates or skips (tested).
- **Composite indexes** match the actual screens: `(team_id, status, priority_rank, id)` for team
  queues, `(assignee_id, status)` for "my work", `(updated_at, id)` for "recently changed".
- **FTS5** full-text index over title + description, kept in sync by triggers. User input is turned
  into quoted prefix terms, so hostile input (`"`, `NEAR(`, SQL fragments) can't break the query.
- Dashboard = five `COUNT(*)`s on indexed columns; activity is paginated too.
- The browser holds one page of results plus the open item. Search-as-you-type requests are
  sequence-numbered so a slow old response can't overwrite a newer one.

With 20k seeded items, list, search and dashboard requests each complete in roughly 5–20 ms locally.

---

## What I intentionally did not build

- **Real-time push** (SSE/WebSockets). Polling + versioned conflict handling gives correctness; push is
  a latency optimisation. Next step: an SSE stream fed by the `activity` table (`id > last_seen`).
- **PostgreSQL / multiple app servers.** The design ports directly: `FOR UPDATE SKIP LOCKED` for
  leasing, `tsvector` for search, same version/idempotency/outbox tables.
- **Field-level auto-merge** of non-overlapping edits — nice, but the user-choice dialog is safer for v1.
- SSO, attachments, SLAs/escalation timers, saved views, comment editing.

## If this grew significantly

1. Postgres with read replicas for list/search; partition `activity` by month.
2. Separate worker processes; outbox → a real broker only if throughput demands it.
3. SSE push for item/list updates; cache dashboard counts per team.
4. Dedicated search (OpenSearch) once FTS ranking/facets become product needs.
5. SLA engine as another outbox consumer ("P1 unowned for 15 min → page the lead").
