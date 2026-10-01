"""一条命令启动：python -m app 或 ./run.sh。

首次启动自动建表并载入演示案例（延期、离职改派、验收不通过复议、撤销、归档等）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .reminders import ReminderScheduler
from .db import connect
from .seed import seed_if_empty
from .server import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="会议决议执行跟踪系统")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--db", default=os.environ.get("DB_PATH", "data/decisions.db"))
    parser.add_argument("--attachments", default=os.environ.get("ATTACHMENTS_DIR",
                                                                "data/attachments"))
    parser.add_argument("--no-reminders", action="store_true", help="关闭进程内提醒线程")
    parser.add_argument("--no-seed", action="store_true", help="不载入演示数据")
    args = parser.parse_args(argv)

    base = Path(__file__).resolve().parent.parent
    db_path = str((base / args.db).resolve()) if not os.path.isabs(args.db) else args.db
    att = args.attachments
    attachments_dir = str((base / att).resolve()) if not os.path.isabs(att) else att

    if not args.no_seed:
        created = seed_if_empty(db_path, attachments_dir)
        if created:
            print(f"[seed] 已载入演示案例 -> {db_path}")

    httpd, scheduler = run(args.host, args.port, db_path, attachments_dir,
                           with_reminders=not args.no_reminders)
    if scheduler:
        # 启动即扫描一次，生成逾期提醒
        first = ReminderScheduler(lambda: connect(db_path), run_once=True)
        first.start()
    print(f"[server] 会议决议执行跟踪系统已启动: http://{args.host}:{args.port}")
    print("[server] Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] 正在停止...")
    finally:
        if scheduler:
            scheduler.stop()
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
