"""领域规则测试。

覆盖任务要求的四个重点：
  1. 子项汇总状态完全从子项计算
  2. 执行人不能验收自己的工作（越权拦截）
  3. 两名验收人同轮并发提交 → 冲突
  4. 决议撤销后待办回收、提醒取消

另含：改期/改派/证据替换/驳回复议/归档附注的版本链与状态机。
"""

import os
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.domain import (  # noqa: E402
    ArchivedError, ConflictError, DomainError, DomainService,
    MessageAdapter, PermissionDenied, StateError,
)
from app.reminders import ReminderScheduler  # noqa: E402
from app.storage import Database  # noqa: E402


class AppCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="decision-test-")
        self.db = Database(os.path.join(self.tmp, "t.db"),
                           os.path.join(self.tmp, "attach"))
        self.svc = DomainService(self.db)
        self.today = date.today()
        self.due_future = (self.today + timedelta(days=10)).isoformat()
        self.due_past = (self.today - timedelta(days=6)).isoformat()
        for name, dept in [("秘书", "综合办"), ("主持人", "总经办"),
                           ("张三", "技术部"), ("李四", "市场部"),
                           ("验收甲", "质检部"), ("验收乙", "风控部"),
                           ("离职者", "市场部"), ("接手人", "财务部")]:
            self.db.execute("INSERT INTO people(name,department) VALUES(?,?)", (name, dept))
        self.mid = self.svc.create_meeting("测试会", self.today.isoformat(), "原始记录")

    def tearDown(self):
        self.db.close()

    # 便捷构造：一条已分派、带 n 个待办子项的决议
    def _dispatched(self, code="T-001", owners=None, acceptor="验收甲"):
        did = self.svc.create_decision(self.mid, code, f"{code} 原文", secretary="秘书")
        owners = owners or ["张三"]
        ids = []
        for o in owners:
            ids.append(self.svc.add_action_item(
                did, "技术部", o, self.due_future, acceptor, actor="秘书"))
        self.svc.confirm_and_dispatch(did, host="主持人")
        return did, ids

    def _submit(self, item_id, owner="张三", name="证据.txt", content=b"data"):
        self.svc.start_work(item_id, owner)
        return self.svc.submit_evidence(item_id, owner, content, name, "说明")


# ================================================================ 1. 子项汇总

class TestRollup(AppCase):
    def test_empty_and_pending(self):
        did = self.svc.create_decision(self.mid, "T-000", "x", secretary="秘书")
        self.assertEqual(self.svc.decision_summary(did).rollup_status, "pending")
        self.assertEqual(self.svc.decision_summary(did).total, 0)

    def test_all_accepted_is_done(self):
        did, (a, b) = self._dispatched("T-001", ["张三", "李四"])
        self._submit(a, "张三"); self.svc.submit_approval(a, "验收甲", "approved")
        self._submit(b, "李四"); self.svc.submit_approval(b, "验收甲", "approved")
        s = self.svc.decision_summary(did)
        self.assertEqual(s.accepted, 2)
        self.assertEqual(s.rollup_status, "done")

    def test_partial_is_in_progress(self):
        did, (a, b) = self._dispatched("T-002", ["张三", "李四"])
        self._submit(a, "张三"); self.svc.submit_approval(a, "验收甲", "approved")
        # b 仍 pending
        self.assertEqual(self.svc.decision_summary(did).rollup_status, "in_progress")

    def test_rejected_drives_in_review(self):
        did, (a,) = self._dispatched("T-003", ["张三"])
        self._submit(a)
        self.svc.submit_approval(a, "验收甲", "rejected", "不符")
        self.assertEqual(self.svc.decision_summary(did).rollup_status, "in_review")
        self.assertEqual(self.svc.decision_summary(did).rejected, 1)

    def test_conflict_outranks_in_review(self):
        did, (a,) = self._dispatched("T-004", ["张三"], acceptor="验收甲")
        self._submit(a)
        # 两个不同验收人同轮提交 → 冲突
        barrier = threading.Barrier(2)
        errs = []

        def vote(who, verdict):
            try:
                barrier.wait()
                self.svc.submit_approval(a, who, verdict, "意见")
            except ConflictError:
                pass
            except Exception as e:  # noqa
                errs.append(e)

        t1 = threading.Thread(target=vote, args=("验收甲", "approved"))
        t2 = threading.Thread(target=vote, args=("验收乙", "rejected"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertFalse(errs)
        self.assertEqual(self.svc.decision_summary(did).rollup_status, "conflict")

    def test_cancelled_excluded_from_done_denominator(self):
        # 两个子项，一个通过、一个随撤销回收 → 未全撤销，active=1 accepted=1 → done
        did, (a, b) = self._dispatched("T-005", ["张三", "李四"])
        self._submit(a); self.svc.submit_approval(a, "验收甲", "approved")
        # 不能撤销已 dispatched 的部分……直接整体撤销会把 a 也留 accepted（终态不回收）
        self.svc.revoke_decision(did, "方向调整")
        s = self.svc.decision_summary(did)
        self.assertEqual(s.cancelled, 1)
        self.assertEqual(s.accepted, 1)
        self.assertEqual(s.rollup_status, "done")  # active=1, accepted=1

    def test_all_cancelled_is_revoked(self):
        did, (a,) = self._dispatched("T-006", ["张三"])
        self.svc.revoke_decision(did, "取消")
        self.assertEqual(self.svc.decision_summary(did).rollup_status, "revoked")


# ================================================================ 2. 越权验收

class TestPermission(AppCase):
    def test_owner_cannot_accept_own_work(self):
        did, (a,) = self._dispatched("P-001", ["张三"], acceptor="验收甲")
        self._submit(a, "张三")
        with self.assertRaises(PermissionDenied):
            self.svc.submit_approval(a, "张三", "approved", "自我验收")
        # 子项不应被推进
        self.assertEqual(self.svc._item(a)["status"], "submitted")

    def test_non_owner_cannot_submit_evidence(self):
        did, (a,) = self._dispatched("P-002", ["张三"])
        with self.assertRaises(PermissionDenied):
            self.svc.submit_evidence(a, "李四", b"x", "x.txt")

    def test_non_owner_cannot_replace_evidence(self):
        did, (a,) = self._dispatched("P-003", ["张三"])
        eid = self._submit(a, "张三")
        with self.assertRaises(PermissionDenied):
            self.svc.replace_evidence(eid, "李四", b"y", "y.txt", "原因")

    def test_owner_and_acceptor_must_differ_at_creation(self):
        did = self.svc.create_decision(self.mid, "P-004", "x", secretary="秘书")
        with self.assertRaises(PermissionDenied):
            self.svc.add_action_item(did, "技术部", "张三", self.due_future, "张三")

    def test_reassign_to_acceptor_rejected(self):
        did, (a,) = self._dispatched("P-005", ["张三"], acceptor="验收甲")
        with self.assertRaises(PermissionDenied):
            self.svc.reassign_item(a, "验收甲", "离职改派", actor="主持人")


# ================================================================ 3. 并发提交冲突

class TestConcurrentConflict(AppCase):
    def test_two_acceptors_conflict_and_host_resolves(self):
        did, (a,) = self._dispatched("C-001", ["张三"], acceptor="验收甲")
        self._submit(a)
        results = {}
        barrier = threading.Barrier(2)

        def vote(who, verdict, key):
            try:
                barrier.wait()
                self.svc.submit_approval(a, who, verdict, f"{who}的意见")
                results[key] = "ok"
            except ConflictError as e:
                results[key] = e.approval_ids

        t1 = threading.Thread(target=vote, args=("验收甲", "approved", "jia"))
        t2 = threading.Thread(target=vote, args=("验收乙", "rejected", "yi"))
        t1.start(); t2.start(); t1.join(); t2.join()

        # 锁串行化下：后到者收到 ConflictError，先到者成功但其结论随即被系统改判冲突。
        # 无论谁先谁后，最终双方验收记录都必须是 conflict、子项进入冲突态。
        conflict_signals = [v for v in results.values() if isinstance(v, tuple)]
        self.assertEqual(len(conflict_signals), 1)
        it = self.svc._item(a)
        self.assertEqual(it["status"], "conflict")

        approvals = self.db.query(
            "SELECT acceptor,verdict,original_verdict FROM approvals WHERE item_id=? "
            "AND verdict='conflict'", (a,))
        self.assertEqual(len(approvals), 2)
        verdicts = {x["acceptor"]: x["original_verdict"] for x in approvals}
        self.assertEqual(verdicts["验收甲"], "approved")
        self.assertEqual(verdicts["验收乙"], "rejected")

        # 冲突期间任何人再提交都被状态挡住（非 submitted/rejected/conflict 允许进入，
        # conflict 允许，但同轮已有他人 → 继续判冲突；这里验证主持人裁定后恢复）
        # 主持人采纳"验收甲"（其原始结论 approved）
        win_row = next(r for r in self.db.query(
            "SELECT * FROM approvals WHERE item_id=? AND verdict='conflict'", (a,))
            if r["acceptor"] == "验收甲")
        final = self.svc.resolve_conflict(a, win_row["id"], host="主持人", note="采纳质检")
        self.assertEqual(final, "approved")
        self.assertEqual(self.svc._item(a)["status"], "accepted")
        self.assertEqual(self.svc.decision_summary(did).rollup_status, "done")

    def test_acceptor_cannot_double_submit_same_round(self):
        did, (a,) = self._dispatched("C-002", ["张三"])
        self._submit(a)
        self.svc.submit_approval(a, "验收甲", "approved")
        with self.assertRaises(StateError):
            self.svc.submit_approval(a, "验收甲", "approved", "重复提交")

    def test_conflict_message_sent_to_host(self):
        did, (a,) = self._dispatched("C-003", ["张三"])
        self._submit(a)
        barrier = threading.Barrier(2)

        def vote(who, verdict):
            try:
                barrier.wait()
                self.svc.submit_approval(a, who, verdict, "x")
            except ConflictError:
                pass

        ts = [threading.Thread(target=vote, args=("验收甲", "approved")),
              threading.Thread(target=vote, args=("验收乙", "rejected"))]
        for t in ts: t.start()
        for t in ts: t.join()
        conflict_msgs = [m for m in self.svc.messages.sent if m["channel"] == "conflict"]
        self.assertTrue(conflict_msgs, "应向主持人发送冲突消息")


# ================================================================ 4. 撤销后待办回收 + 提醒

class TestRevokeRecycling(AppCase):
    def test_revoked_items_leave_todo_and_get_cancelled(self):
        did, (a, b) = self._dispatched("V-001", ["张三", "李四"])
        self.assertEqual(len(self.svc.todo_list()), 2)
        self.svc.revoke_decision(did, "预算取消")
        todos = self.svc.todo_list()
        self.assertEqual(todos, [])
        self.assertEqual(self.svc._item(a)["status"], "cancelled")
        self.assertEqual(self.svc._item(b)["status"], "cancelled")
        self.assertEqual(self.svc._decision(did)["revoked"], 1)

    def test_reminders_cancel_for_revoked(self):
        did, (a,) = self._dispatched("V-002", ["张三"])
        sched = ReminderScheduler(self.db, self.svc.messages, interval_seconds=999)
        past = (self.today - timedelta(days=3)).isoformat()
        self.svc.reschedule_item(a, past, "延期到过去以便触发提醒", actor="张三")
        n = sched.tick(today=self.today)
        self.assertEqual(n, 1)  # 撤销前有逾期提醒
        self.svc.revoke_decision(did, "取消")
        # 换一个全新调度器（清空已提醒记忆）也不会再提醒已回收子项
        sched2 = ReminderScheduler(self.db, self.svc.messages, interval_seconds=999)
        self.assertEqual(sched2.tick(today=self.today), 0)

    def test_cannot_revoke_then_edit(self):
        did, (a,) = self._dispatched("V-003", ["张三"])
        self.svc.revoke_decision(did, "取消")
        with self.assertRaises(StateError):
            self.svc.reschedule_item(a, self.due_future, "再改期", actor="张三")

    def test_accepted_item_kept_on_revoke(self):
        did, (a, b) = self._dispatched("V-004", ["张三", "李四"])
        self._submit(a); self.svc.submit_approval(a, "验收甲", "approved")
        self.svc.revoke_decision(did, "部分完成后撤销")
        self.assertEqual(self.svc._item(a)["status"], "accepted")  # 已验收不回收
        self.assertEqual(self.svc._item(b)["status"], "cancelled")


# ================================================================ 版本链

class TestVersionChain(AppCase):
    def test_decision_version_chain_on_revoke(self):
        did, _ = self._dispatched("VC-1", ["张三"])
        self.svc.revoke_decision(did, "撤销原因")
        vs = self.svc.decision_versions(did)
        types = [v["change_type"] for v in vs]
        self.assertEqual(types, ["create", "dispatch", "revoke"])
        for i, v in enumerate(vs, 1):
            self.assertEqual(v["version"], i)
            if i > 1:
                self.assertEqual(v["parent_version"], i - 1)

    def test_reschedule_and_reassign_chain(self):
        did, (a,) = self._dispatched("VC-2", ["张三"])
        self.svc.reschedule_item(a, self.due_future, "第一次改期", actor="张三")
        self.svc.reassign_item(a, "接手人", "原负责人离职", actor="主持人")
        types = [v["change_type"] for v in self.svc.item_versions(a)]
        self.assertEqual(types, ["create", "reschedule", "reassign"])
        self.assertEqual(self.svc._item(a)["owner"], "接手人")
        self.assertEqual(self.svc._item(a)["current_version"], 3)

    def test_evidence_replace_keeps_chain(self):
        did, (a,) = self._dispatched("VC-3", ["张三"])
        old = self._submit(a, "张三", "v1.txt")
        new = self.svc.replace_evidence(old, "张三", b"v2", "v2.txt", "补材料")
        chain = self.svc.evidence_chain(a)
        self.assertEqual(len(chain), 2)
        self.assertIsNone(chain[0]["superseded_by"] if chain[0]["id"] == new else None)
        oldrow = next(x for x in chain if x["id"] == old)
        self.assertEqual(oldrow["superseded_by"], new)
        self.assertTrue(oldrow["replace_reason"])
        # 不能基于被替换的旧证据验收
        with self.assertRaises(StateError):
            self.svc.submit_approval(a, "验收甲", "approved", evidence_id=old)


# ================================================================ 驳回复议 / 归档

class TestReviewAndArchive(AppCase):
    def test_reject_then_new_evidence_round(self):
        did, (a,) = self._dispatched("RA-1", ["张三"])
        e1 = self._submit(a)
        self.assertEqual(self.svc.evidence_round(a, e1), 1)
        self.svc.submit_approval(a, "验收甲", "rejected", "材料不全")
        self.assertEqual(self.svc._item(a)["status"], "rejected")
        self.assertIn(a, [x["id"] for x in self.svc.review_queue()])
        e2 = self.svc.submit_evidence(a, "张三", b"v2", "补正.txt", "整改后")
        self.assertEqual(self.svc.evidence_round(a, e2), 2)
        self.assertEqual(self.svc._item(a)["status"], "submitted")
        self.svc.submit_approval(a, "验收甲", "approved", "整改到位")
        self.assertEqual(self.svc._item(a)["status"], "accepted")

    def test_archived_only_notes_allowed(self):
        did, (a,) = self._dispatched("AR-1", ["张三"])
        self._submit(a); self.svc.submit_approval(a, "验收甲", "approved")
        self.svc.archive_decision(did)
        # 归档后禁止改期/改派/再提交证据/撤销
        with self.assertRaises(ArchivedError):
            self.svc.reschedule_item(a, self.due_future, "x", actor="张三")
        with self.assertRaises(ArchivedError):
            self.svc.revoke_decision(did, "x")
        with self.assertRaises(ArchivedError):
            self.svc.submit_evidence(a, "张三", b"x", "x.txt")
        # 只允许附注
        self.svc.add_archive_note(did, "秘书", "补充一条存档说明")
        self.assertEqual(len(self.svc.search_archive("存档说明")), 1)
        with self.assertRaises(DomainError):
            self.svc.add_archive_note(did, "秘书", "  ")

    def test_archive_search(self):
        did, (a,) = self._dispatched("AR-2", ["张三"])
        self._submit(a); self.svc.submit_approval(a, "验收甲", "approved")
        self.svc.archive_decision(did)
        self.assertEqual(len(self.svc.search_archive("AR-2")), 1)
        self.assertEqual(len(self.svc.search_archive("不存在的关键字xyz")), 0)


# ================================================================ 逾期天数与原因

class TestOverdue(AppCase):
    def test_overdue_days_and_reason(self):
        did, (a, b) = self._dispatched("OD-1", ["张三", "李四"])
        past = (self.today - timedelta(days=7)).isoformat()
        self.svc.reschedule_item(a, past, "供应商到货延迟", actor="张三")
        items = {x["id"]: x for x in self.svc.todo_list()}
        self.assertEqual(items[a]["overdue_days"], 7)
        self.assertIn("到货延迟", items[a]["overdue_reason"])
        # 未到期待办不显示逾期
        self.assertEqual(items[b]["overdue_days"], 0)

    def test_accepted_shows_no_overdue(self):
        did, (a,) = self._dispatched("OD-2", ["张三"])
        # 给一个过去期限但已验收完成
        self.svc.reschedule_item(a, self.due_past, "曾延期", actor="郑昊")
        self._submit(a); self.svc.submit_approval(a, "验收甲", "approved")
        items = self.svc.list_items(did)
        self.assertEqual(items[0]["overdue_days"], 0)


# ================================================================ 消息适配器 & 草稿分派

class TestDispatch(AppCase):
    def test_draft_requires_items_and_host_dispatch(self):
        did = self.svc.create_decision(self.mid, "DP-1", "x", secretary="秘书")
        with self.assertRaises(StateError):
            self.svc.confirm_and_dispatch(did)  # 无子项
        self.svc.add_action_item(did, "技术部", "张三", self.due_future, "验收甲")
        self.svc.confirm_and_dispatch(did, host="主持人")
        with self.assertRaises(StateError):
            self.svc.confirm_and_dispatch(did)  # 不能重复分派
        channels = {(m["channel"], m["to_name"]) for m in self.svc.messages.sent}
        self.assertIn(("dispatch", "张三"), channels)
        self.assertIn(("dispatch", "验收甲"), channels)

    def test_attachment_written_to_local_dir(self):
        did, (a,) = self._dispatched("DP-2", ["张三"])
        eid = self.svc.submit_evidence(a, "张三", "合同扫描件".encode(), "合同.txt", "证据")
        ev = self.db.query_one("SELECT * FROM evidence WHERE id=?", (eid,))
        path = self.db.attachment_path(ev["filename"])
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read().decode(), "合同扫描件")


if __name__ == "__main__":
    unittest.main(verbosity=2)
