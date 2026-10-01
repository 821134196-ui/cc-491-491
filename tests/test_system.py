"""系统测试：子项汇总、越权验收、并发提交、撤销后待办回收、版本链等。

运行：python -m unittest -v   或   python tests/test_system.py
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from app.db import connect, init_db
from app.messages import MessageAdapter
from app.server import make_handler
from app.service import Conflict, Forbidden, Service
from http.server import ThreadingHTTPServer


def http_post(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class Harness:
    """最小数据集：一场会议、一项多责任决议、两名验收人。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dt-test-"))
        self.db = self.tmp / "db.sqlite"
        self.att = self.tmp / "att"
        self.conn = connect(self.db)
        init_db(self.conn)
        cur = self.conn.cursor()
        self.users = [
            ("u_host", "主持人", "综合办", 1),
            ("u_sec", "秘书", "综合办", 1),
            ("u_a", "执行人甲", "研发部", 1),
            ("u_b", "执行人乙", "研发部", 1),
            ("u_v1", "验收人甲", "质量部", 1),
            ("u_v2", "验收人乙", "审计部", 1),
            ("u_left", "离职员工", "运营部", 0),
        ]
        for uid, name, dept, active in self.users:
            cur.execute("INSERT INTO users VALUES (?,?,?,?)", (uid, name, dept, active))
        cur.execute(
            "INSERT INTO meetings VALUES ('m1','测试会','2026-09-01T14:00:00','会议室',"
            "'u_sec','u_host','2026-09-01T14:00:00')")
        self.svc = Service(self.conn, self.att, MessageAdapter(self.conn))

    def make_resolution(self, verifiers=("u_v1", "u_v2"), due_offset=10,
                        assignees=("u_a", "u_b"), assignee_is_verifier=False):
        future = (date.today() + timedelta(days=due_offset)).isoformat()
        items = []
        for idx, assignee in enumerate(assignees):
            vids = list(verifiers)
            if assignee_is_verifier and assignee not in vids:
                vids.append(assignee)
            items.append({"content": f"任务{idx + 1}", "department": "研发部",
                          "assignee_id": assignee, "due_date": future,
                          "verifier_ids": vids})
        rid = self.svc.create_draft("u_sec", {
            "meeting_id": "m1", "code": "JZ-T-001", "title": "测试决议",
            "original_text": "决议原文：任务一、任务二须按标准完成。",
            "items": items,
        })
        self.svc.confirm("u_host", rid)
        self.rid = rid
        self.item_ids = [i["id"] for i in self.svc._items(rid)]
        return rid

    def close(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def h_svc(self):
        return self.h.svc

    # ------------------------------------------------- 1. 子项汇总状态

    def test_rollup_computed_from_subitems(self):
        h = self.h
        h.make_resolution()
        svc = h.svc

        r = svc.get_resolution(h.rid)
        self.assertEqual({i["item_status"] for i in r["items"]}, {"pending"})
        listing = {x["id"]: x for x in svc.list_resolutions()}
        self.assertEqual(listing[h.rid]["rollup"], "pending")
        self.assertEqual(listing[h.rid]["approved_count"], 0)

        # 第一项：证据 → 通过
        i0 = h.item_ids[0]
        svc.upload_evidence("u_a", i0, "a.pdf", b"data", "说明")
        svc.submit_verdict("u_v1", i0, True, "与原文一致")
        r = {x["id"]: x for x in svc.list_resolutions()}[h.rid]
        self.assertEqual(r["rollup"], "in_progress")
        self.assertEqual(r["approved_count"], 1)
        self.assertIn("1/2", r["rollup_cn"])

        # 第二项通过 → 决议汇总自动变为已完成
        i1 = h.item_ids[1]
        svc.upload_evidence("u_b", i1, "b.pdf", b"data", "说明")
        svc.submit_verdict("u_v2", i1, True, "ok")
        r = {x["id"]: x for x in svc.list_resolutions()}[h.rid]
        self.assertEqual(r["rollup"], "completed")
        self.assertEqual(r["approved_count"], 2)

        # 全部通过后允许归档；归档后汇总显示已归档
        svc.archive("u_host", h.rid, "归档")
        r = {x["id"]: x for x in svc.list_resolutions()}[h.rid]
        self.assertEqual(r["status"], "archived")

    def test_overdue_days_and_delay_reason(self):
        h = self.h
        h.make_resolution()
        i0 = h.item_ids[0]
        past = (date.today() - timedelta(days=7)).isoformat()
        h.svc.reschedule("u_host", i0, past, "接口方延期，等待联调窗口")
        view = h.svc._item_view(h.svc._item(i0))
        self.assertTrue(view["overdue"])
        self.assertEqual(view["overdue_days"], 7)
        self.assertIn("接口方延期", view["delay_reason"])

    # ------------------------------------------------- 2. 越权验收

    def test_assignee_cannot_verify_own_work(self):
        h = self.h
        h.make_resolution(assignees=("u_a", "u_b"), assignee_is_verifier=True)
        i0 = h.item_ids[0]
        h.svc.upload_evidence("u_a", i0, "a.pdf", b"x", "")
        # 即使该执行人同时被登记为验收人也不行
        with self.assertRaises(Forbidden):
            h.svc.submit_verdict("u_a", i0, True, "自验收")

    def test_non_verifier_and_non_assignee_forbidden(self):
        h = self.h
        h.make_resolution()
        i0 = h.item_ids[0]
        # 非责任人不能传证据
        with self.assertRaises(Forbidden):
            h.svc.upload_evidence("u_b", i0, "x.pdf", b"x", "")
        h.svc.upload_evidence("u_a", i0, "a.pdf", b"x", "")
        # 非验收人不能提交验收
        with self.assertRaises(Forbidden):
            h.svc.submit_verdict("u_b", i0, True, "")

    # ------------------------------------------------- 3. 并发验收冲突（服务层 + HTTP）

    def test_concurrent_verdicts_service_layer(self):
        h = self.h
        h.make_resolution()
        i0 = h.item_ids[0]
        h.svc.upload_evidence("u_a", i0, "a.pdf", b"x", "")

        results = []
        barrier = threading.Barrier(2)

        def vote(verifier):
            c = connect(h.db)
            s = Service(c, h.att, MessageAdapter(c))
            barrier.wait()
            try:
                s.submit_verdict(verifier, i0, True, f"{verifier} 提交")
                results.append("ok")
            except Conflict:
                results.append("conflict")
            finally:
                c.close()

        t1 = threading.Thread(target=vote, args=("u_v1",))
        t2 = threading.Thread(target=vote, args=("u_v2",))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(results), ["conflict", "ok"])
        verdicts = h.conn.execute(
            "SELECT COUNT(*) c FROM verdicts WHERE item_id=? AND is_appeal=0", (i0,)
        ).fetchone()["c"]
        self.assertEqual(verdicts, 1)

    # ------------------------------------------------- 4. 撤销后待办回收

    def test_revoke_recycles_open_todos(self):
        h = self.h
        h.make_resolution()
        before = h.conn.execute(
            "SELECT COUNT(*) c FROM todos WHERE state='open'").fetchone()["c"]
        # 2 个执行人 + 每项 2 个验收人 = 6
        self.assertEqual(before, 6)
        h.svc.revoke("u_host", h.rid, "政策变化")
        self.assertEqual(h.conn.execute(
            "SELECT COUNT(*) c FROM todos WHERE state='open'").fetchone()["c"], 0)
        recycled = h.conn.execute(
            "SELECT COUNT(*) c FROM todos WHERE state='recycled'").fetchone()["c"]
        self.assertEqual(recycled, 6)
        # 撤销后不能再传证据
        with self.assertRaises(Conflict):
            h.svc.upload_evidence("u_a", h.item_ids[0], "x.pdf", b"x", "")
        # 版本链记录撤销原因
        chain = h.svc.version_chain(h.rid)
        self.assertTrue(any(e["kind"] == "revoke" and "政策变化" in e["summary"]
                            for e in chain))

    def test_reassign_recycles_and_reopens(self):
        h = self.h
        h.make_resolution(assignees=("u_a",))
        i0 = h.item_ids[0]
        h.svc.reassign("u_host", i0, "u_b", "责任人离职")
        rows = h.conn.execute(
            "SELECT user_id,state,close_reason FROM todos WHERE item_id=? AND kind='executor'",
            (i0,)).fetchall()
        states = {(r["user_id"], r["state"]) for r in rows}
        self.assertIn(("u_a", "recycled"), states)
        self.assertIn(("u_b", "open"), states)
        # 不能改派给离职人员
        with self.assertRaises(Exception):
            h.svc.reassign("u_host", i0, "u_left")

    # ------------------------------------------------- 5. 证据替换与轮次版本链

    def test_evidence_replacement_keeps_chain(self):
        h = self.h
        h.make_resolution(assignees=("u_a",), verifiers=("u_v1",))
        i0 = h.item_ids[0]
        h.svc.upload_evidence("u_a", i0, "v1.pdf", b"1", "第一版")
        h.svc.upload_evidence("u_a", i0, "v2.pdf", b"22", "替换版")
        history = h.svc.evidence_history(i0)
        self.assertEqual(len(history), 2)
        self.assertIsNotNone(history[0]["superseded_by"])
        self.assertEqual(history[1]["id"], history[0]["superseded_by"])
        # 旧文件仍保留在本地附件目录
        files = list(Path(h.att).iterdir())
        self.assertEqual(len(files), 2)

    def test_reject_then_resubmit_new_round_and_review(self):
        h = self.h
        h.make_resolution(assignees=("u_a",), verifiers=("u_v1",))
        i0 = h.item_ids[0]
        h.svc.upload_evidence("u_a", i0, "v1.pdf", b"1", "")
        h.svc.submit_verdict("u_v1", i0, False, "不符合原文")
        view = h.svc._item_view(h.svc._item(i0))
        self.assertEqual(view["item_status"], "rejected")
        # 复议申请与主持人裁定支持 → 通过
        rid = h.svc.request_review("u_a", i0, "已覆盖全部要求")
        h.svc.decide_review("u_host", rid, True, "情况属实")
        view = h.svc._item_view(h.svc._item(i0))
        self.assertEqual(view["item_status"], "approved")
        # 原驳回记录与复议结论都保留
        verdicts = h.conn.execute(
            "SELECT approved,is_appeal FROM verdicts WHERE item_id=? ORDER BY created_at",
            (i0,)).fetchall()
        self.assertEqual([(r["approved"], r["is_appeal"]) for r in verdicts],
                         [(0, 0), (1, 1)])

    def test_reject_then_resubmit_creates_next_round(self):
        h = self.h
        h.make_resolution(assignees=("u_a",), verifiers=("u_v1",))
        i0 = h.item_ids[0]
        h.svc.upload_evidence("u_a", i0, "v1.pdf", b"1", "")
        h.svc.submit_verdict("u_v1", i0, False, "缺材料")
        h.svc.upload_evidence("u_a", i0, "v2.pdf", b"2", "补齐")
        item = h.svc._item(i0)
        self.assertEqual(item["current_round"], 2)
        h.svc.submit_verdict("u_v1", i0, True, "补齐后通过")
        self.assertEqual(h.svc._item_view(item)["item_status"], "approved")

    # ------------------------------------------------- 6. 归档冻结

    def test_archived_only_notes_allowed(self):
        h = self.h
        h.make_resolution(assignees=("u_a",), verifiers=("u_v1",))
        i0 = h.item_ids[0]
        h.svc.upload_evidence("u_a", i0, "a.pdf", b"x", "")
        h.svc.submit_verdict("u_v1", i0, True, "")
        h.svc.archive("u_host", h.rid, "完整")
        for call in (
            lambda: h.svc.reschedule("u_host", i0, "2030-01-01", "x"),
            lambda: h.svc.upload_evidence("u_a", i0, "b.pdf", b"y", ""),
            lambda: h.svc.submit_verdict("u_v1", i0, False, ""),
        ):
            with self.assertRaises(Conflict):
                call()
        # 附注是唯一允许的新增
        h.svc.add_note("u_sec", h.rid, "归档后补充说明")
        notes = h.svc.get_resolution(h.rid)["notes"]
        self.assertEqual(notes[-1]["body"], "归档后补充说明")

    def test_cannot_archive_with_unapproved_items(self):
        h = self.h
        h.make_resolution()
        with self.assertRaises(Conflict):
            h.svc.archive("u_host", h.rid, "")

    # ------------------------------------------------- 7. 草稿权限

    def test_only_secretary_drafts_only_host_confirms(self):
        h = self.h
        future = (date.today() + timedelta(days=5)).isoformat()
        payload = {"meeting_id": "m1", "code": "X", "title": "t", "original_text": "x",
                   "items": [{"content": "t", "department": "研发部", "assignee_id": "u_a",
                              "due_date": future, "verifier_ids": ["u_v1"]}]}
        with self.assertRaises(Forbidden):
            h.svc.create_draft("u_a", payload)
        rid = h.svc.create_draft("u_sec", payload)
        with self.assertRaises(Forbidden):
            h.svc.confirm("u_sec", rid)


class HttpTests(unittest.TestCase):
    """通过真实 HTTP 服务器验证状态码与两名验收人并发提交。"""

    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_Ctx(cls.h)))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.h.make_resolution()
        cls.i0 = cls.h.item_ids[0]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.h.close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_http_self_verification_403(self):
        # 构造一个自验收场景
        h = Harness()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_Ctx(h)))
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            h.make_resolution(assignees=("u_a",), verifiers=("u_a", "u_v1"))
            iid = h.item_ids[0]
            # 先经服务层传证据（multipart 用服务层等价覆盖）
            h.svc.upload_evidence("u_a", iid, "a.pdf", b"x", "")
            status, body = http_post(
                f"http://127.0.0.1:{port}/api/items/verdict",
                {"actor_id": "u_a", "item_id": iid, "approved": True, "comment": "自验"})
            self.assertEqual(status, 403)
            self.assertIn("自己", body["error"])
        finally:
            httpd.shutdown(); h.close()

    def test_http_concurrent_verdict_409_conflict(self):
        # 准备证据
        self.h.svc.upload_evidence("u_a", self.i0, "a.pdf", b"x", "")
        barrier = threading.Barrier(2)
        out = []

        def vote(verifier):
            barrier.wait()
            out.append(http_post(self.url("/api/items/verdict"), {
                "actor_id": verifier, "item_id": self.i0,
                "approved": True, "comment": f"{verifier} 同时提交"}))

        t1 = threading.Thread(target=vote, args=("u_v1",))
        t2 = threading.Thread(target=vote, args=("u_v2",))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = sorted(s for s, _ in out)
        self.assertEqual(statuses, [201, 409])
        conflict_body = next(b for s, b in out if s == 409)
        self.assertIn("冲突", conflict_body["error"])
        self.assertIn("verifier_name", conflict_body["info"])

    def test_http_revoke_then_upload_409(self):
        h = Harness()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_Ctx(h)))
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            h.make_resolution(assignees=("u_a",), verifiers=("u_v1",))
            iid = h.item_ids[0]
            status, _ = http_post(f"http://127.0.0.1:{port}/api/resolutions/{h.rid}/revoke",
                                  {"actor_id": "u_host", "reason": "测试撤销"})
            self.assertEqual(status, 200)
            # 撤销后待办在回收站
            conn = connect(h.db)
            recycled = conn.execute(
                "SELECT COUNT(*) c FROM todos WHERE state='recycled'").fetchone()["c"]
            conn.close()
            self.assertGreaterEqual(recycled, 2)
        finally:
            httpd.shutdown(); h.close()


class _Ctx:
    """测试用 AppContext 替身。"""
    def __init__(self, harness: Harness):
        self.db_path = str(harness.db)
        self.attachments_dir = harness.att

    def service(self):
        # 与正式 AppContext 相同：每线程独立连接，制造真实数据库竞争
        import threading as _t
        tls = getattr(self, "_tls", None)
        if tls is None:
            tls = _t.local(); self._tls = tls
        if not hasattr(tls, "svc"):
            c = connect(self.db_path)
            tls.svc = Service(c, self.attachments_dir, MessageAdapter(c))
        return tls.svc


if __name__ == "__main__":
    unittest.main(verbosity=2)
