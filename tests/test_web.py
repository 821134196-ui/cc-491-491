"""HTTP 端到端集成测试：在临时端口起真实服务，走 multipart 上传与表单提交。"""

import io
import os
import sys
import tempfile
import threading
import unittest
import uuid
from datetime import date, timedelta
from urllib.parse import urlencode, quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.domain import DomainService  # noqa: E402
from app.storage import Database  # noqa: E402
from app.web import make_server  # noqa: E402


def multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    """构造 multipart/form-data 请求体。files: {name: (filename, bytes)}。"""
    boundary = "----test" + uuid.uuid4().hex
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        buf.write(f"{v}\r\n".encode())
    for k, (fname, data) in files.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'.encode())
        buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
        buf.write(data + b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


class WebCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="decision-web-")
        self.db = Database(os.path.join(self.tmp, "w.db"),
                           os.path.join(self.tmp, "att"))
        self.svc = DomainService(self.db)
        self.today = date.today()
        future = (self.today + timedelta(days=8)).isoformat()
        for n, dep in [("秘书", "综合办"), ("王总", "总经办"), ("张三", "技术部"),
                       ("李四", "市场部"), ("验收甲", "质检"), ("验收乙", "风控")]:
            self.db.execute("INSERT INTO people(name,department) VALUES(?,?)", (n, dep))
        mid = self.svc.create_meeting("W 会", self.today.isoformat(), "会议原文")
        self.did = self.svc.create_decision(mid, "W-1", "W 决议原文", secretary="秘书")
        self.iid = self.svc.add_action_item(
            self.did, "技术部", "张三", future, "验收甲", actor="秘书")
        self.svc.confirm_and_dispatch(self.did, host="王总")
        self.svc.start_work(self.iid, "张三")

        self.httpd = make_server(self.db, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()
        self.db.close()

    def _req(self, method, path, data=None, cookie=None, ctype="application/x-www-form-urlencoded"):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        body = None
        if data is not None:
            if ctype.startswith("multipart"):
                headers["Content-Type"] = ctype
            else:
                headers["Content-Type"] = ctype
                data = urlencode(data).encode()
            body = data
        if cookie:
            headers["Cookie"] = f"user={quote(cookie)}"
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        loc = resp.getheader("Location")
        conn.close()
        if loc:
            from urllib.parse import unquote
            loc = unquote(loc)
        return resp.status, loc, payload

    def test_pages_render(self):
        for path in ["/", "/todos", "/review", "/meetings", "/archive", "/messages",
                     f"/decision?id={self.did}"]:
            status, _, body = self._req("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn("会议决议督办".encode(), body)

    def test_owner_self_approval_blocked_over_http(self):
        # 张三（执行人）上传证据
        body, ctype = multipart({}, {"file": ("证据.txt", "过程证据内容".encode())})
        status, loc, _ = self._req(
            "POST", f"/item/{self.iid}/evidence", body, cookie="张三", ctype=ctype)
        self.assertEqual(status, 303)
        # 张三试图验收自己 → 重定向回详情，带越权提示
        status, loc, _ = self._req(
            "POST", f"/item/{self.iid}/approve",
            {"verdict": "approved", "comment": "自我验收"}, cookie="张三")
        self.assertEqual(status, 303)
        self.assertIn("越权", loc)
        # 状态仍是待验收
        self.assertEqual(self.svc._item(self.iid)["status"], "submitted")

    def test_acceptor_approves_flow(self):
        body, ctype = multipart({}, {"file": ("r.txt", b"x")})
        self._req("POST", f"/item/{self.iid}/evidence", body,
                  cookie="张三", ctype=ctype)
        status, loc, _ = self._req(
            "POST", f"/item/{self.iid}/approve",
            {"verdict": "approved", "comment": "按原文核对通过"}, cookie="验收甲")
        self.assertEqual(status, 303)
        self.assertEqual(self.svc._item(self.iid)["status"], "accepted")

    def test_concurrent_conflict_and_resolve(self):
        body, ctype = multipart({}, {"file": ("r.txt", b"x")})
        self._req("POST", f"/item/{self.iid}/evidence", body,
                  cookie="张三", ctype=ctype)
        barrier = threading.Barrier(2)
        results = []

        def vote(who, verdict):
            barrier.wait()
            st, loc, _ = self._req("POST", f"/item/{self.iid}/approve",
                                   {"verdict": verdict, "comment": "意见"}, cookie=who)
            results.append((who, st, loc or ""))

        t1 = threading.Thread(target=vote, args=("验收甲", "approved"))
        t2 = threading.Thread(target=vote, args=("验收乙", "rejected"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(self.svc._item(self.iid)["status"], "conflict")
        # 至少一方收到冲突提示
        self.assertTrue(any("冲突" in r[2] for r in results), results)

        # 主持人在页面上裁定：取验收甲的冲突记录
        win = next(r for r in self.db.query(
            "SELECT * FROM approvals WHERE item_id=? AND verdict='conflict' AND acceptor='验收甲'",
            (self.iid,)))
        st, loc, _ = self._req("POST", f"/item/{self.iid}/resolve",
                               {"winning_approval_id": win["id"], "note": "采纳质检"},
                               cookie="王总")
        self.assertEqual(st, 303)
        self.assertEqual(self.svc._item(self.iid)["status"], "accepted")

    def test_attachment_uploaded_and_downloadable(self):
        payload = "合同扫描件内容".encode()
        body, ctype = multipart({"note": "说明文字"}, {"file": ("合同.txt", payload)})
        self._req("POST", f"/item/{self.iid}/evidence", body,
                  cookie="张三", ctype=ctype)
        ev = self.db.query_one("SELECT id FROM evidence WHERE item_id=?", (self.iid,))
        status, _, data = self._req("GET", f"/attachment?id={ev['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(data, payload)

    def test_archive_only_note(self):
        # 完成并归档
        body, ctype = multipart({}, {"file": ("r.txt", b"x")})
        self._req("POST", f"/item/{self.iid}/evidence", body,
                  cookie="张三", ctype=ctype)
        self._req("POST", f"/item/{self.iid}/approve",
                  {"verdict": "approved", "comment": "ok"}, cookie="验收甲")
        self._req("POST", f"/decision/{self.did}/archive", {}, cookie="王总")
        # 归档后改期被拒
        st, loc, _ = self._req("POST", f"/item/{self.iid}/reschedule",
                               {"new_due": "2026-12-01", "reason": "改"}, cookie="王总")
        self.assertIn("归档", loc)
        # 附注成功
        st, _, _ = self._req("POST", f"/decision/{self.did}/note",
                             {"note": "归档补充"}, cookie="秘书")
        self.assertEqual(st, 303)
        self.assertEqual(len(self.svc.search_archive("归档补充")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
