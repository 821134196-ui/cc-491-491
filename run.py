#!/usr/bin/env python3
"""一条命令启动：初始化 SQLite、载入延期与复议案例、启动 Web 与进程内提醒。

用法
----
    python3 run.py            # 默认 127.0.0.1:8000，数据在 ./data
    python3 run.py --reset    # 清空数据并重新载入种子案例
    python3 run.py --port 8080
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

from app.domain import DomainService, MessageAdapter
from app.reminders import ReminderScheduler
from app.seed import seed
from app.storage import Database
from app.web import make_server

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "decisions.db")
ATTACH_DIR = os.path.join(DATA_DIR, "attachments")


def main() -> int:
    ap = argparse.ArgumentParser(description="会议决议督办系统（零依赖）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true", help="清空数据并重新载入种子案例")
    ap.add_argument("--no-reminder", action="store_true", help="不启动进程内提醒线程")
    args = ap.parse_args()

    if args.reset and os.path.isdir(DATA_DIR):
        shutil.rmtree(DATA_DIR)
        print(f"[init] 已清空 {DATA_DIR}")

    os.makedirs(DATA_DIR, exist_ok=True)
    db = Database(DB_PATH, ATTACH_DIR)
    fresh = db.query_one("SELECT COUNT(*) c FROM decisions")["c"] == 0
    seed(db)
    if fresh:
        print("[seed] 已载入演示案例：含逾期改期、驳回复议、并发验收冲突、"
              "证据替换、离职改派、撤销回收、归档附注")

    svc = DomainService(db)  # 复用同一个 messages outbox（web handler 内建 adapter 也写同表）

    scheduler = None
    if not args.no_reminder:
        scheduler = ReminderScheduler(db, svc.messages, interval_seconds=300)
        scheduler.start()
        fired = scheduler.tick()
        if fired:
            print(f"[reminder] 启动扫描发出 {fired} 条到期/逾期提醒（见消息记录页）")

    httpd = make_server(db, args.host, args.port)
    url = f"http://{args.host}:{args.port}/"
    print("=" * 64)
    print("会议决议督办系统已启动")
    print(f"  地址     : {url}")
    print(f"  数据库   : {DB_PATH}")
    print(f"  附件目录 : {ATTACH_DIR}")
    print("  提示     : 右上角可切换操作人（秘书/主持人/执行人/验收人）")
    print("  停止     : Ctrl+C")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[shutdown] 正在停止…")
    finally:
        if scheduler:
            scheduler.stop()
        httpd.server_close()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
