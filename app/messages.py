"""模拟消息适配器：不真正外发，消息落库并返回模拟回执。"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime


class MessageAdapter:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.sent: list[dict] = []  # 进程内也留一份，便于测试断言

    def send(self, channel: str, target: str, title: str, body: str) -> dict:
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        ts = datetime.now().isoformat(timespec="seconds")
        receipt = f"SIM-{channel.upper()}-{uuid.uuid4().hex[:8].upper()}"
        self.conn.execute(
            "INSERT INTO messages (id,channel,target,title,body,receipt,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (mid, channel, target, title, body, receipt, ts))
        record = {"id": mid, "channel": channel, "target": target,
                  "title": title, "body": body, "receipt": receipt, "created_at": ts}
        self.sent.append(record)
        return record
