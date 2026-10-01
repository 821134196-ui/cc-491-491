"""SQLite 存储层：决议、子项、版本链、证据、验收、消息 outbox。

设计要点
--------
* 所有"可变"记录（决议、子项、证据）都保留整行快照版本（version + parent_version），
  形成版本链；当前表只保留最新版本指针。
* 证据替换：不覆盖旧行，原证据 superseded_by 指向新证据，链可追溯。
* 验收（approvals）追加写入：通过/驳回/复议都是追加事件，另在子项上维护
  最新成功验收指针，便于状态计算与冲突检测。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, date, timezone
from typing import Any, Optional

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    held_on TEXT NOT NULL,
    created_at TEXT NOT NULL,
    raw_minutes TEXT NOT NULL DEFAULT '',          -- 原始会议记录（全文）
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agenda_items (
    id INTEGER PRIMARY KEY,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
    topic TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    department TEXT NOT NULL DEFAULT ''
);

-- 决议主表：只存"当前状态 + 当前版本指针"。历史在 decision_versions。
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
    agenda_item_id INTEGER REFERENCES agenda_items(id),
    code TEXT NOT NULL UNIQUE,                     -- 决议编号，如 R-001
    original_text TEXT NOT NULL,                   -- 决议原文（核对基准，永不改）
    status TEXT NOT NULL DEFAULT 'draft',          -- draft/dispatched/in_review/done/revoked/archived
    current_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    dispatched_at TEXT,
    archived_at TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS decision_versions (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    version INTEGER NOT NULL,
    parent_version INTEGER,                       -- 上一版本号，构成版本链
    change_type TEXT NOT NULL,                     -- create/reschedule/reassign/revoke/note/archive/dispatch
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    changed_at TEXT NOT NULL,
    snapshot TEXT NOT NULL,                        -- 当时决议字段的 JSON 快照
    UNIQUE(decision_id, version)
);

-- 子项（责任分派）：同一条决议的多项责任分别跟踪。
CREATE TABLE IF NOT EXISTS action_items (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    version INTEGER NOT NULL DEFAULT 1,
    current_version INTEGER NOT NULL DEFAULT 1,
    department TEXT NOT NULL DEFAULT '',           -- 责任部门
    owner TEXT NOT NULL,                           -- 负责人
    due_date TEXT NOT NULL,                        -- 完成期限 YYYY-MM-DD
    acceptor TEXT NOT NULL,                        -- 验收人
    status TEXT NOT NULL DEFAULT 'pending',        -- pending/in_progress/submitted/rejected/accepted/cancelled
    delay_reason TEXT NOT NULL DEFAULT '',         -- 逾期/改期原因
    latest_evidence_id INTEGER,                    -- 最新证据
    accepted_approval_id INTEGER,                  -- 最新通过验收
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_item_versions (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES action_items(id),
    version INTEGER NOT NULL,
    parent_version INTEGER,
    change_type TEXT NOT NULL,                     -- create/reschedule/reassign/cancel/note
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    changed_at TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    UNIQUE(item_id, version)
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES action_items(id),
    submitter TEXT NOT NULL,
    filename TEXT NOT NULL,                        -- 本地目录中的存储名
    original_name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL,
    superseded_by INTEGER,                         -- 被哪条新证据替换
    replace_reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES action_items(id),
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    acceptor TEXT NOT NULL,
    verdict TEXT NOT NULL,                        -- approved/rejected/conflict
    comment TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL,
    round INTEGER NOT NULL DEFAULT 1,             -- 验收/复议轮次
    conflict_with INTEGER,                        -- 冲突的另一条验收 id
    original_verdict TEXT                         -- 被改判为 conflict 前的原始结论
);

-- 归档决议只允许补充附注
CREATE TABLE IF NOT EXISTS archive_notes (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    author TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 模拟消息适配器的发件箱
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,                         -- dispatch/reminder/conflict/revoke/review
    to_name TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_items_decision ON action_items(decision_id);
CREATE INDEX IF NOT EXISTS idx_evidence_item ON evidence(item_id);
CREATE INDEX IF NOT EXISTS idx_approvals_item ON approvals(item_id);
CREATE INDEX IF NOT EXISTS idx_versions_decision ON decision_versions(decision_id);
"""


def now_iso() -> str:
    # 统一本地时间、秒级精度，便于种子数据与展示
    return datetime.now().replace(microsecond=0).isoformat()


class Database:
    """薄封装：线程安全的 sqlite 连接 + 附件目录 + 迁移。"""

    def __init__(self, path: str, attachment_dir: str):
        self.path = path
        self.attachment_dir = attachment_dir
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        os.makedirs(attachment_dir, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- 基础工具 ----
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def transaction(self):
        return _Transaction(self)

    # ---- 附件（本地目录）----
    def save_attachment(self, data: bytes, original_name: str) -> str:
        safe = os.path.basename(original_name).replace(" ", "_") or "attachment"
        stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        stored = f"{stamp}_{safe}"
        full = os.path.join(self.attachment_dir, stored)
        with open(full, "wb") as f:
            f.write(data)
        return stored

    def attachment_path(self, stored_name: str) -> str:
        return os.path.join(self.attachment_dir, os.path.basename(stored_name))

    def close(self) -> None:
        with self._lock:
            self.conn.close()


class _Transaction:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db.conn.execute("BEGIN")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.db.conn.commit()
            else:
                self.db.conn.rollback()
        finally:
            self.db._lock.release()
        return False


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()
