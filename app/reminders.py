"""进程内提醒：后台线程周期性扫描待办，到期/逾期经模拟消息适配器提醒。

* 不依赖任何外部队列/定时服务。
* 撤销决议的子项为 cancelled，天然不会再被扫到（提醒随之取消）。
* 每个 (item_id, kind) 只提醒一次；改期后期限变化，以新期限重新提醒。
"""

from __future__ import annotations

import threading
from datetime import date, datetime
from typing import Optional

from .storage import Database, now_iso, parse_date
from .domain import MessageAdapter


class ReminderScheduler:
    def __init__(self, db: Database, messages: MessageAdapter,
                 interval_seconds: float = 60.0):
        self.db = db
        self.messages = messages
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 记录已提醒键：item_id:due_date:kind —— 改期(due 变了)会再次提醒
        self._fired: set[str] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="reminder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def tick(self, today: Optional[date] = None) -> int:
        """执行一轮扫描。返回本轮发出的提醒数。可被测试直接调用。"""
        today = today or date.today()
        rows = self.db.query(
            "SELECT ai.*, d.code FROM action_items ai JOIN decisions d ON d.id=ai.decision_id "
            "WHERE ai.status NOT IN ('accepted','cancelled') AND d.revoked=0"
        )
        sent = 0
        for r in rows:
            due = parse_date(r["due_date"])
            if due == today:
                key = f"{r['id']}:{r['due_date']}:due"
                if self._mark(key):
                    self.messages.send(
                        "reminder", r["owner"],
                        f"今日到期：决议 {r['code']}",
                        f"你负责的「{r['department']}」事项今日（{r['due_date']}）到期，请及时提交证据。",
                    )
                    sent += 1
            elif due < today:
                overdue = (today - due).days
                key = f"{r['id']}:{r['due_date']}:overdue"
                if self._mark(key):
                    reason = r["delay_reason"] or "未说明改期原因"
                    self.messages.send(
                        "reminder", r["owner"],
                        f"已逾期 {overdue} 天：决议 {r['code']}",
                        f"你负责的事项期限为 {r['due_date']}，已逾期 {overdue} 天。"
                        f"登记原因：{reason}。请尽快处理。",
                    )
                    sent += 1
        return sent

    def _mark(self, key: str) -> bool:
        with self._lock:
            if key in self._fired:
                return False
            self._fired.add(key)
            return True

    def _run(self) -> None:
        # 启动即先扫一次，之后按周期
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # 提醒失败不应拖垮进程
                print(f"[reminder] 扫描异常: {e}")
            self._stop.wait(self.interval)
