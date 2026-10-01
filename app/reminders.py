"""进程内提醒：后台线程按固定间隔扫描待办，对到期/逾期事项写 reminders 表并发消息。

不依赖任何外部调度器，随服务器进程启动、退出。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, timedelta

from .messages import MessageAdapter


class ReminderScheduler(threading.Thread):
    def __init__(self, conn_factory, interval: float = 60.0, run_once: bool = False):
        super().__init__(daemon=True, name="reminder-scheduler")
        self.conn_factory = conn_factory
        self.interval = interval
        self.run_once = run_once
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def scan(self, conn: sqlite3.Connection) -> int:
        """扫描一次，返回新产生的提醒条数。"""
        msg = MessageAdapter(conn)
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        rows = conn.execute(
            """SELECT i.* FROM todos t JOIN items i ON i.id=t.item_id
               JOIN resolutions r ON r.id=i.resolution_id
               WHERE t.state='open' AND t.kind='executor' AND r.status='confirmed'
               GROUP BY i.id""").fetchall()
        created = 0
        for r in rows:
            item = dict(r)
            if item["due_date"] < today:
                days = (date.today() - date.fromisoformat(item["due_date"])).days
                created += self._remind(conn, msg, item, "overdue", today,
                                        f"任务已逾期 {days} 天，请尽快处理。"
                                        f"登记原因：{item['delay_reason'] or '（未填写）'}")
            elif item["due_date"] == tomorrow:
                created += self._remind(conn, msg, item, "due_today", today,
                                        "任务明天到期，请留意完成期限")
        return created

    def _remind(self, conn, msg, item, kind, remind_date, body) -> int:
        rid = f"rem_{item['id']}_{kind}_{remind_date}"
        try:
            conn.execute(
                "INSERT INTO reminders (id,item_id,kind,remind_date,created_at) "
                "VALUES (?,?,?,?,datetime('now','localtime'))",
                (rid, item["id"], kind, remind_date))
        except sqlite3.IntegrityError:
            return 0
        msg.send("reminder", item["assignee_id"],
                 f"{'逾期提醒' if kind == 'overdue' else '到期提醒'}：{item['content'][:20]}",
                 body)
        return 1

    def run(self):
        conn = self.conn_factory()
        try:
            while not self.stop_event.is_set():
                try:
                    self.scan(conn)
                except Exception as exc:  # 提醒失败不能拖垮进程
                    print(f"[reminder] scan error: {exc}")
                if self.run_once:
                    return
                self.stop_event.wait(self.interval)
        finally:
            conn.close()
