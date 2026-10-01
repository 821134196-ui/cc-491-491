"""HTTP 入口：JSON API + 静态页面 + 附件下载，纯标准库。

每个请求使用独立 SQLite 连接（WAL 模式），保证两名验收人并发提交时
真正在数据库层竞争，由部分唯一索引裁决。
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .db import connect, init_db
from .messages import MessageAdapter
from .reminders import ReminderScheduler
from .service import Conflict, DomainError, Forbidden, NotFound, Service

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

_tls = threading.local()


class AppContext:
    def __init__(self, db_path: str, attachments_dir: Path):
        self.db_path = db_path
        self.attachments_dir = attachments_dir
        self.lock = threading.Lock()
        init_db(connect(db_path))

    def service(self) -> Service:
        if not hasattr(_tls, "conn"):
            _tls.conn = connect(self.db_path)
        return Service(_tls.conn, self.attachments_dir, MessageAdapter(_tls.conn))


# ---------------------------------------------------------- multipart 解析

def parse_multipart(body: bytes, boundary: bytes) -> dict[str, dict]:
    """极简 multipart/form-data 解析：返回 {字段名: {filename?, data}}。"""
    delimiter = b"--" + boundary
    fields: dict[str, dict] = {}
    for part in body.split(delimiter):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        header_blob, _, content = part.partition(b"\r\n\r\n")
        headers = {}
        for line in header_blob.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        cd = headers.get("content-disposition", "")
        name = None
        filename = None
        for token in cd.split(";"):
            token = token.strip()
            if token.startswith("name="):
                name = token[5:].strip('"')
            elif token.startswith("filename="):
                filename = token[9:].strip('"')
        if name:
            fields[name] = {"filename": filename, "data": content,
                            "content_type": headers.get("content-type", "")}
    return fields


# ---------------------------------------------------------------- 路由表

def make_handler(ctx: AppContext):

    class Handler(BaseHTTPRequestHandler):
        server_version = "DecisionTracker/1.0"

        def log_message(self, fmt, *args):
            print(f"[http] {self.address_string()} - {fmt % args}")

        # ---- 基础工具 ----
        def _send_json(self, obj, status=200):
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _error(self, exc: DomainError):
            info = getattr(exc, "info", None)
            self._send_json({"error": str(exc), "info": info}, exc.status)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                raise DomainError("请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise DomainError("请求体必须是 JSON 对象")
            return data

        def do_GET(self):
            try:
                self._route_get()
            except DomainError as e:
                self._error(e)
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": f"服务器错误: {e}"}, 500)

        def do_POST(self):
            try:
                self._route_post()
            except DomainError as e:
                self._error(e)
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": f"服务器错误: {e}"}, 500)

        # ---- GET 路由 ----
        def _route_get(self):
            parsed = urllib.parse.urlparse(self.path)
            path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
            svc = ctx.service()

            if path == "/api/meta":
                return self._send_json({
                    "users": svc.list_users(),
                    "statuses": svc.STATUS_CN,
                    "item_statuses": svc.ITEM_STATUS_CN,
                })
            if path == "/api/meetings":
                return self._send_json({"meetings": svc.list_meetings()})
            if path.startswith("/api/meetings/"):
                mid = path.rsplit("/", 1)[-1]
                return self._send_json({"meeting": svc.get_meeting(mid)})
            if path == "/api/resolutions":
                return self._send_json({"resolutions": svc.list_resolutions()})
            if path.startswith("/api/resolutions/") and path.endswith("/evidence"):
                rid = path.split("/")[3]
                svc.get_resolution(rid)  # 存在性校验
                item_id = qs.get("item", [""])[0]
                return self._send_json({"evidence": svc.evidence_history(item_id)})
            if path.startswith("/api/resolutions/"):
                rid = path.rsplit("/", 1)[-1]
                return self._send_json({"resolution": svc.get_resolution(rid)})
            if path == "/api/todos":
                uid = qs.get("user", [""])[0]
                if not uid:
                    raise DomainError("缺少 user 参数")
                return self._send_json({"todos": svc.my_todos(uid)})
            if path == "/api/recycle-bin":
                return self._send_json({"todos": svc.recycle_bin()})
            if path == "/api/messages":
                uid = qs.get("user", [None])[0]
                return self._send_json({"messages": svc.list_messages(uid)})
            if path == "/api/archive":
                kw = qs.get("q", [""])[0]
                return self._send_json({"results": svc.search_archive(kw)})
            if path.startswith("/api/attachments/"):
                return self._download(path.rsplit("/", 1)[-1])
            if path == "/" or path == "/index.html":
                return self._static("index.html", "text/html; charset=utf-8")
            return self._serve_static(path)

        def _download(self, evidence_id: str):
            fpath, filename = ctx.service().attachment_path(evidence_id)
            if not fpath.exists():
                raise NotFound("附件文件")
            data = fpath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_static(self, path: str):
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
            }.get(Path(path).suffix, "application/octet-stream")
            return self._static(path.lstrip("/"), ctype)

        def _static(self, rel: str, ctype: str):
            target = (STATIC_DIR / rel).resolve()
            if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ---- POST 路由 ----
        def _route_post(self):
            path = urllib.parse.urlparse(self.path).path
            svc = ctx.service()

            if path == "/api/drafts":
                d = self._read_json()
                rid = svc.create_draft(d.pop("actor_id"), d)
                return self._send_json({"id": rid}, 201)
            if path.endswith("/confirm"):
                d = self._read_json()
                svc.confirm(d["actor_id"], path.split("/")[3])
                return self._send_json({"ok": True})
            if path.endswith("/revoke"):
                d = self._read_json()
                svc.revoke(d["actor_id"], path.split("/")[3], d.get("reason", ""))
                return self._send_json({"ok": True})
            if path.endswith("/archive"):
                d = self._read_json()
                svc.archive(d["actor_id"], path.split("/")[3], d.get("note", ""))
                return self._send_json({"ok": True})
            if path.endswith("/notes"):
                d = self._read_json()
                svc.add_note(d["actor_id"], path.split("/")[3], d.get("body", ""))
                return self._send_json({"ok": True}, 201)
            if path == "/api/items/reschedule":
                d = self._read_json()
                svc.reschedule(d["actor_id"], d["item_id"], d["new_due"], d.get("reason", ""))
                return self._send_json({"ok": True})
            if path == "/api/items/reassign":
                d = self._read_json()
                svc.reassign(d["actor_id"], d["item_id"], d["new_assignee_id"],
                             d.get("reason", "责任人离职"))
                return self._send_json({"ok": True})
            if path == "/api/items/evidence":
                return self._upload(svc)
            if path == "/api/items/verdict":
                d = self._read_json()
                out = svc.submit_verdict(d["actor_id"], d["item_id"],
                                         bool(d.get("approved")), d.get("comment", ""))
                return self._send_json(out, 201)
            if path == "/api/items/review-request":
                d = self._read_json()
                rid = svc.request_review(d["actor_id"], d["item_id"], d.get("reason", ""))
                return self._send_json({"id": rid}, 201)
            if path == "/api/reviews/decide":
                d = self._read_json()
                svc.decide_review(d["actor_id"], d["request_id"],
                                  bool(d.get("approve_appeal")), d.get("note", ""))
                return self._send_json({"ok": True})
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")

        def _upload(self, svc: Service):
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                raise DomainError("证据上传必须使用 multipart/form-data")
            boundary = ctype.split("boundary=", 1)[1].strip().encode()
            length = int(self.headers.get("Content-Length", 0))
            fields = parse_multipart(self.rfile.read(length), boundary)
            required = {"actor_id", "item_id", "file"}
            missing = required - fields.keys()
            if missing:
                raise DomainError(f"缺少字段: {','.join(sorted(missing))}")
            meta = fields["file"]
            out = svc.upload_evidence(
                fields["actor_id"]["data"].decode(),
                fields["item_id"]["data"].decode(),
                meta["filename"] or "attachment",
                meta["data"],
                fields.get("note", {}).get("data", b"").decode("utf-8", "replace"))
            return self._send_json(out, 201)

    return Handler


def run(host: str = "127.0.0.1", port: int = 8000,
        db_path: str = "data/decisions.db", attachments_dir: str = "data/attachments",
        with_reminders: bool = True) -> tuple[ThreadingHTTPServer, ReminderScheduler | None]:
    ctx = AppContext(db_path, Path(attachments_dir))
    httpd = ThreadingHTTPServer((host, port), make_handler(ctx))
    scheduler = None
    if with_reminders:
        scheduler = ReminderScheduler(lambda: connect(db_path), interval=60.0)
        scheduler.start()
    return httpd, scheduler
