-- OpsBoard schema (SQLite). All timestamps are ISO-8601 UTC strings.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

-- A user may belong to many teams with a different role in each.
-- viewer < member < lead
CREATE TABLE IF NOT EXISTS memberships (
    user_id INTEGER NOT NULL REFERENCES users(id),
    team_id INTEGER NOT NULL REFERENCES teams(id),
    role    TEXT NOT NULL CHECK (role IN ('viewer','member','lead')),
    PRIMARY KEY (user_id, team_id)
);
CREATE INDEX IF NOT EXISTS idx_memberships_team ON memberships(team_id);

CREATE TABLE IF NOT EXISTS work_items (
    id                INTEGER PRIMARY KEY,
    team_id           INTEGER NOT NULL REFERENCES teams(id),
    title             TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    kind              TEXT NOT NULL CHECK (kind IN ('customer_issue','engineering','payment','incident','compliance','ops_task')),
    priority          TEXT NOT NULL CHECK (priority IN ('P1','P2','P3','P4')),
    priority_rank     INTEGER NOT NULL,           -- 1..4, sortable copy of priority
    status            TEXT NOT NULL CHECK (status IN ('open','in_progress','awaiting_approval','approved','rejected','resolved','closed')),
    requires_approval INTEGER NOT NULL DEFAULT 0,
    assignee_id       INTEGER REFERENCES users(id),
    created_by        INTEGER NOT NULL REFERENCES users(id),
    approval_requested_by INTEGER REFERENCES users(id),
    due_at            TEXT,
    version           INTEGER NOT NULL DEFAULT 1, -- optimistic concurrency token
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
-- Indexes chosen for the dashboard queries ("my open work", "team queue by priority", "recently changed").
CREATE INDEX IF NOT EXISTS idx_items_team_status_prio ON work_items(team_id, status, priority_rank, id);
CREATE INDEX IF NOT EXISTS idx_items_assignee_status  ON work_items(assignee_id, status);
CREATE INDEX IF NOT EXISTS idx_items_updated          ON work_items(updated_at, id);

-- Full-text search over title + description, kept in sync by triggers.
CREATE VIRTUAL TABLE IF NOT EXISTS work_items_fts USING fts5(
    title, description, content='work_items', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS work_items_ai AFTER INSERT ON work_items BEGIN
    INSERT INTO work_items_fts(rowid, title, description) VALUES (new.id, new.title, new.description);
END;
CREATE TRIGGER IF NOT EXISTS work_items_ad AFTER DELETE ON work_items BEGIN
    INSERT INTO work_items_fts(work_items_fts, rowid, title, description) VALUES ('delete', old.id, old.title, old.description);
END;
CREATE TRIGGER IF NOT EXISTS work_items_au AFTER UPDATE OF title, description ON work_items BEGIN
    INSERT INTO work_items_fts(work_items_fts, rowid, title, description) VALUES ('delete', old.id, old.title, old.description);
    INSERT INTO work_items_fts(rowid, title, description) VALUES (new.id, new.title, new.description);
END;

-- Append-only history. Written in the same transaction as the change it describes.
CREATE TABLE IF NOT EXISTS activity (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES work_items(id),
    actor_id   INTEGER NOT NULL REFERENCES users(id),
    type       TEXT NOT NULL,      -- created, updated, claimed, assigned, transitioned, commented
    data       TEXT NOT NULL,      -- JSON: {field: [old, new]} or {body: ...}
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_item ON activity(item_id, id);

-- Idempotency: the first response for (user, key) is stored and replayed on retries.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    user_id      INTEGER NOT NULL,
    key          TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code  INTEGER NOT NULL,
    response     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

-- Transactional outbox: secondary work is recorded atomically with the primary change,
-- then processed asynchronously by the worker with leases and retries.
CREATE TABLE IF NOT EXISTS outbox (
    id           INTEGER PRIMARY KEY,
    topic        TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','dead')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,     -- not processed before this time (backoff)
    locked_until TEXT,              -- lease; expired lease => another worker may take it
    last_error   TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_ready ON outbox(status, available_at);

-- Notifications produced by the worker. UNIQUE makes the handler idempotent:
-- if a job runs twice, the second insert is ignored.
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    item_id     INTEGER NOT NULL REFERENCES work_items(id),
    activity_id INTEGER NOT NULL REFERENCES activity(id),
    message     TEXT NOT NULL,
    read        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    UNIQUE (activity_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read, id);
