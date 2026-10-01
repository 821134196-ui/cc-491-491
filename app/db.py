"""SQLite 连接管理与表结构。

决议、子项、证据、验收、待办、版本链全部落在同一个数据库文件中。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    department TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS meetings (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    held_at      TEXT NOT NULL,
    location     TEXT NOT NULL DEFAULT '',
    secretary_id TEXT NOT NULL REFERENCES users(id),
    host_id      TEXT NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    id         TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    order_no   INTEGER NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT ''
);

-- 决议：lifecycle 状态机 draft -> confirmed -> archived；confirmed 可 -> revoked
CREATE TABLE IF NOT EXISTS resolutions (
    id            TEXT PRIMARY KEY,
    meeting_id    TEXT NOT NULL REFERENCES meetings(id),
    topic_id      TEXT REFERENCES topics(id),
    code          TEXT NOT NULL,
    title         TEXT NOT NULL,
    original_text TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft',
    created_by    TEXT NOT NULL REFERENCES users(id),
    confirmed_by  TEXT REFERENCES users(id),
    confirmed_at  TEXT,
    revoked_by    TEXT REFERENCES users(id),
    revoke_reason TEXT,
    archived_by   TEXT REFERENCES users(id),
    archived_at   TEXT,
    archive_note  TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,
    resolution_id TEXT NOT NULL REFERENCES resolutions(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    content       TEXT NOT NULL,
    department    TEXT NOT NULL,
    assignee_id   TEXT NOT NULL REFERENCES users(id),
    due_date      TEXT NOT NULL,
    current_round INTEGER NOT NULL DEFAULT 0,
    delay_reason  TEXT DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_verifiers (
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (item_id, user_id)
);

-- 证据：被替换时 superseded_by 指向新证据，旧行与磁盘文件均保留，形成版本链
CREATE TABLE IF NOT EXISTS evidence (
    id           TEXT PRIMARY KEY,
    item_id      TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    round        INTEGER NOT NULL,
    filename     TEXT NOT NULL,
    stored_name  TEXT NOT NULL,
    note         TEXT DEFAULT '',
    uploaded_by  TEXT NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    superseded_by TEXT REFERENCES evidence(id)
);

-- 验收结论：同一轮次只允许一条，并发写入时后到者收到冲突
CREATE TABLE IF NOT EXISTS verdicts (
    id          TEXT PRIMARY KEY,
    item_id     TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    round       INTEGER NOT NULL,
    verifier_id TEXT NOT NULL REFERENCES users(id),
    approved    INTEGER NOT NULL,
    comment     TEXT DEFAULT '',
    is_appeal   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- 同一轮正常验收结论唯一；两名验收人并发提交时后到者被数据库拒绝。
-- is_appeal=1 的复议结论不受此约束，可与原结论同轮共存，保留版本链。
CREATE UNIQUE INDEX IF NOT EXISTS idx_verdicts_unique_round
    ON verdicts(item_id, round) WHERE is_appeal=0;

CREATE TABLE IF NOT EXISTS review_requests (
    id            TEXT PRIMARY KEY,
    item_id       TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    requested_by  TEXT NOT NULL REFERENCES users(id),
    reason        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending', -- pending / approved / rejected
    decided_by    TEXT REFERENCES users(id),
    decision_note TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    decided_at    TEXT
);

-- 附注：归档后唯一允许新增的内容
CREATE TABLE IF NOT EXISTS notes (
    id            TEXT PRIMARY KEY,
    resolution_id TEXT NOT NULL REFERENCES resolutions(id) ON DELETE CASCADE,
    author_id     TEXT NOT NULL REFERENCES users(id),
    body          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- 待办：open / closed / recycled。撤销与离职交接通过状态流转保留去向
CREATE TABLE IF NOT EXISTS todos (
    id           TEXT PRIMARY KEY,
    item_id      TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,            -- executor / verifier
    user_id      TEXT NOT NULL REFERENCES users(id),
    state        TEXT NOT NULL DEFAULT 'open',
    opened_at    TEXT NOT NULL,
    closed_at    TEXT,
    close_reason TEXT DEFAULT '',
    round        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    channel    TEXT NOT NULL,
    target     TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    receipt    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id          TEXT PRIMARY KEY,
    item_id     TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,   -- due_today / overdue
    remind_date TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (item_id, kind, remind_date)
);

-- 版本链：改期、离职改派、撤销、证据替换、验收不通过、复议全部留痕
CREATE TABLE IF NOT EXISTS log (
    id            TEXT PRIMARY KEY,
    resolution_id TEXT NOT NULL REFERENCES resolutions(id) ON DELETE CASCADE,
    item_id       TEXT,
    kind          TEXT NOT NULL,
    actor_id      TEXT REFERENCES users(id),
    summary       TEXT NOT NULL,
    payload       TEXT DEFAULT '{}',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_resolution ON items(resolution_id);
CREATE INDEX IF NOT EXISTS idx_evidence_item ON evidence(item_id);
CREATE INDEX IF NOT EXISTS idx_todos_user ON todos(user_id, state);
CREATE INDEX IF NOT EXISTS idx_log_resolution ON log(resolution_id);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
