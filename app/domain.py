"""领域服务：全部业务规则集中在此，Web 层与测试共用。

规则对照（需求原文）
--------------------
* 秘书整理草稿 → 主持人确认后分派（draft → dispatched）。
* 同一决议多项责任分别跟踪；决议汇总状态只从子项实时计算，不落库。
* 改期 / 责任人离职(改派) / 撤销 / 证据被替换 / 验收不通过 —— 全部追加版本链。
* 执行人不能验收自己的工作。
* 两名验收人对同一轮证据同时提交 → 标记冲突(conflict)，通知主持人裁定。
* 验收不通过可复议：执行人补新证据（证据轮次 +1），验收人再验收。
* 已归档决议只允许补充附注，其他变更一律拒绝。
* 撤销决议：子项回收为 cancelled，提醒随之取消，待办列表不再出现。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

from .storage import Database, now_iso, parse_date, row_to_dict


# ---------------------------------------------------------------- 异常

class DomainError(Exception):
    """所有业务规则违例的基类。"""


class PermissionDenied(DomainError):
    """越权操作，如执行人验收自己的工作。"""


class ConflictError(DomainError):
    """两名验收人并发提交冲突。携带冲突双方信息。"""

    def __init__(self, message: str, approval_ids: tuple[int, int]):
        super().__init__(message)
        self.approval_ids = approval_ids


class ArchivedError(DomainError):
    """已归档决议只允许补充附注。"""


class StateError(DomainError):
    """对象当前状态不允许该操作。"""


# ---------------------------------------------------------------- 消息适配器

class MessageAdapter:
    """模拟消息适配器：消息只落库 outbox，不真正外发。"""

    def __init__(self, db: Database):
        self.db = db
        self.sent: list[dict[str, Any]] = []  # 进程内也留一份，方便测试断言

    def send(self, channel: str, to_name: str, subject: str, body: str) -> dict[str, Any]:
        msg = {
            "channel": channel,
            "to_name": to_name,
            "subject": subject,
            "body": body,
            "created_at": now_iso(),
            "delivered": 1,
        }
        cur = self.db.execute(
            "INSERT INTO messages(channel,to_name,subject,body,created_at,delivered)"
            " VALUES(:channel,:to_name,:subject,:body,:created_at,1)",
            msg,
        )
        msg["id"] = cur.lastrowid
        self.sent.append(msg)
        return msg


# ---------------------------------------------------------------- 状态机

# 子项终态（终态后不再出现在待办/提醒中）
TERMINAL_STATUSES = {"accepted", "cancelled"}

# 决议状态
DRAFT, DISPATCHED, IN_REVIEW, DONE, REVOKED, ARCHIVED = (
    "draft", "dispatched", "in_review", "done", "revoked", "archived",
)


@dataclass
class ItemSummary:
    total: int
    pending: int
    in_progress: int
    submitted: int
    rejected: int
    conflict: int
    accepted: int
    cancelled: int

    @property
    def active(self) -> int:
        return self.total - self.cancelled

    @property
    def rollup_status(self) -> str:
        """汇总状态：完全从子项计数计算。"""
        if self.total > 0 and self.cancelled == self.total:
            return "revoked"
        if self.active > 0 and self.accepted == self.active:
            return "done"
        if self.conflict > 0:
            return "conflict"
        if self.rejected > 0 or self.submitted > 0:
            return "in_review"
        if self.accepted > 0 or self.in_progress > 0:
            return "in_progress"
        return "pending"


# ---------------------------------------------------------------- 领域服务

class DomainService:
    def __init__(self, db: Database, messages: Optional[MessageAdapter] = None):
        self.db = db
        self.messages = messages or MessageAdapter(db)

    # ================================================================
    # 会议与决议草稿
    # ================================================================
    def create_meeting(self, title: str, held_on: str, raw_minutes: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO meetings(title,held_on,created_at,raw_minutes)"
            " VALUES(?,?,?,?)",
            (title, held_on, now_iso(), raw_minutes),
        )
        return cur.lastrowid

    def add_agenda_item(self, meeting_id: int, topic: str, content: str) -> int:
        pos = self.db.query_one(
            "SELECT COALESCE(MAX(position),0)+1 AS p FROM agenda_items WHERE meeting_id=?",
            (meeting_id,),
        )["p"]
        cur = self.db.execute(
            "INSERT INTO agenda_items(meeting_id,topic,content,position) VALUES(?,?,?,?)",
            (meeting_id, topic, content, pos),
        )
        return cur.lastrowid

    def list_meetings(self, include_archived: bool = True):
        sql = "SELECT * FROM meetings"
        if not include_archived:
            sql += " WHERE archived=0"
        return [dict(r) for r in self.db.query(sql + " ORDER BY held_on DESC, id DESC")]

    def get_meeting(self, meeting_id: int) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM meetings WHERE id=?", (meeting_id,))
        if not row:
            raise StateError(f"会议 {meeting_id} 不存在")
        m = dict(row)
        m["agenda"] = [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM agenda_items WHERE meeting_id=? ORDER BY position",
                (meeting_id,),
            )
        ]
        return m

    def create_decision(
        self,
        meeting_id: int,
        code: str,
        original_text: str,
        agenda_item_id: Optional[int] = None,
        secretary: str = "秘书",
    ) -> int:
        """秘书整理草稿。original_text 是验收核对基准，之后永不修改。"""
        ts = now_iso()
        with self.db.transaction() as db:
            cur = db.execute(
                "INSERT INTO decisions(meeting_id,agenda_item_id,code,original_text,"
                "status,current_version,created_at) VALUES(?,?,?,?,?,1,?)",
                (meeting_id, agenda_item_id, code, original_text, DRAFT, ts),
            )
            did = cur.lastrowid
            self._decision_version(db, did, "create", "秘书起草", secretary, ts, version=1)
        return did

    def add_action_item(
        self,
        decision_id: int,
        department: str,
        owner: str,
        due_date: str,
        acceptor: str,
        actor: str = "秘书",
    ) -> int:
        """草稿阶段添加责任子项（同决议可多项，分别跟踪）。"""
        d = self._decision(decision_id)
        self._require_editable(d)
        if owner == acceptor:
            raise PermissionDenied("负责人与验收人不能为同一人，执行人无法验收自己的工作")
        ts = now_iso()
        with self.db.transaction() as db:
            cur = db.execute(
                "INSERT INTO action_items(decision_id,version,current_version,department,"
                "owner,due_date,acceptor,status,created_at,updated_at)"
                " VALUES(?,1,1,?,?,?,?, 'pending',?,?)",
                (decision_id, department, owner, due_date, acceptor, ts, ts),
            )
            iid = cur.lastrowid
            db.execute(
                "INSERT INTO action_item_versions(item_id,version,parent_version,change_type,"
                "reason,actor,changed_at,snapshot) VALUES(?,1,NULL,'create',?,?,?,?)",
                (iid, "分派建立", actor, ts, self._item_snapshot_json(iid, ts)),
            )
        return iid

    # ================================================================
    # 主持人确认并分派
    # ================================================================
    def confirm_and_dispatch(self, decision_id: int, host: str = "主持人") -> None:
        d = self._decision(decision_id)
        if d["status"] != DRAFT:
            raise StateError(f"决议 {d['code']} 当前状态 {d['status']}，不能再次分派")
        items = self.list_items(decision_id)
        if not items:
            raise StateError("决议尚未分派任何责任项，无法确认")
        ts = now_iso()
        with self.db.transaction() as db:
            db.execute(
                "UPDATE decisions SET status=?, dispatched_at=? WHERE id=?",
                (DISPATCHED, ts, decision_id),
            )
            self._decision_version(
                db, decision_id, "dispatch",
                f"主持人确认分派，共 {len(items)} 项责任", host, ts,
            )
            for it in items:
                self.messages.send(
                    "dispatch", it["owner"],
                    f"决议 {d['code']} 责任分派",
                    f"你负责「{d['original_text']}」相关事项（{it['department']}），"
                    f"期限 {it['due_date']}，验收人 {it['acceptor']}。",
                )
                self.messages.send(
                    "dispatch", it["acceptor"],
                    f"决议 {d['code']} 验收委托",
                    f"请在到期后验收 {it['owner']} 负责的事项，以决议原文为准。",
                )

    # ================================================================
    # 执行过程：开始 / 证据提交 / 证据替换
    # ================================================================
    def start_work(self, item_id: int, owner: str) -> None:
        it = self._item(item_id)
        self._require_active_item(it)
        if it["owner"] != owner:
            raise PermissionDenied("只有负责人本人可以开始执行")
        if it["status"] == "pending":
            self.db.execute(
                "UPDATE action_items SET status='in_progress',updated_at=? WHERE id=?",
                (now_iso(), item_id),
            )

    def submit_evidence(
        self,
        item_id: int,
        submitter: str,
        data: bytes,
        original_name: str,
        note: str = "",
    ) -> int:
        """执行人上传过程证据（文件存本地目录）。"""
        it = self._item(item_id)
        self._require_active_item(it)
        d = self._decision(it["decision_id"])
        if it["owner"] != submitter:
            raise PermissionDenied("只有负责人本人可以上传执行证据")
        if it["status"] not in ("in_progress", "pending", "rejected", "conflict"):
            raise StateError(f"子项当前状态 {it['status']}，不能提交证据")
        stored = self.db.save_attachment(data, original_name)
        ts = now_iso()
        with self.db.transaction() as db:
            cur = db.execute(
                "INSERT INTO evidence(item_id,submitter,filename,original_name,note,submitted_at)"
                " VALUES(?,?,?,?,?,?)",
                (item_id, submitter, stored, original_name, note, ts),
            )
            eid = cur.lastrowid
            db.execute(
                "UPDATE action_items SET status='submitted',latest_evidence_id=?,updated_at=? WHERE id=?",
                (eid, ts, item_id),
            )
            self._item_version(db, it, "evidence", f"提交证据：{original_name}", submitter, ts)
            self.messages.send(
                "review", it["acceptor"],
                f"待验收：决议证据 {original_name}",
                f"{submitter} 提交了「{d['code']}」的执行证据，请按决议原文核对。",
            )
        return eid

    def replace_evidence(
        self,
        old_evidence_id: int,
        submitter: str,
        data: bytes,
        original_name: str,
        reason: str,
    ) -> int:
        """证据被替换：旧证据不删除，记录 superseded_by 与替换原因，形成证据链。"""
        old = self.db.query_one("SELECT * FROM evidence WHERE id=?", (old_evidence_id,))
        if not old:
            raise StateError("原证据不存在")
        it = self._item(old["item_id"])
        self._require_active_item(it)
        if it["owner"] != submitter:
            raise PermissionDenied("只有负责人本人可以替换证据")
        if not reason:
            raise DomainError("替换证据必须填写原因")
        stored = self.db.save_attachment(data, original_name)
        ts = now_iso()
        with self.db.transaction() as db:
            cur = db.execute(
                "INSERT INTO evidence(item_id,submitter,filename,original_name,note,submitted_at)"
                " VALUES(?,?,?,?,?,?)",
                (it["id"], submitter, stored, original_name,
                 f"替换证据（{old['original_name']}）：{reason}", ts),
            )
            new_id = cur.lastrowid
            db.execute(
                "UPDATE evidence SET superseded_by=?, replace_reason=? WHERE id=?",
                (new_id, reason, old_evidence_id),
            )
            db.execute(
                "UPDATE action_items SET status='submitted',latest_evidence_id=?,"
                "accepted_approval_id=NULL,updated_at=? WHERE id=?",
                (new_id, ts, it["id"]),
            )
            self._item_version(
                db, it, "evidence_replace",
                f"证据 {old['original_name']} 替换为 {original_name}：{reason}",
                submitter, ts,
            )
            self.messages.send(
                "review", it["acceptor"],
                f"证据已替换，待重新验收",
                f"{submitter} 替换了证据，原因：{reason}。原证据保留备查。",
            )
        return new_id

    # ================================================================
    # 验收 / 冲突 / 驳回复议
    # ================================================================
    def evidence_round(self, item_id: int, evidence_id: int) -> int:
        """证据轮次 = 该证据是子项的第几条证据（按时间序）。新证据即新一轮复议。"""
        rows = self.db.query(
            "SELECT id FROM evidence WHERE item_id=? ORDER BY submitted_at,id", (item_id,)
        )
        return next((i + 1 for i, r in enumerate(rows) if r["id"] == evidence_id), 1)

    def submit_approval(
        self,
        item_id: int,
        acceptor: str,
        verdict: str,
        comment: str = "",
        evidence_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """验收人按决议原文核对并提交结果。

        并发冲突：同一轮证据、两名不同验收人、在任一方被裁定前都提交 → 双方标记
        conflict，子项进入 conflict，提示并通知主持人。
        """
        if verdict not in ("approved", "rejected"):
            raise DomainError("验收结论只能是 approved / rejected")
        it = self._item(item_id)
        self._require_active_item(it)
        d = self._decision(it["decision_id"])
        if acceptor == it["owner"]:
            raise PermissionDenied(
                f"执行人 {acceptor} 不能验收自己负责的工作（子项 #{item_id}）"
            )
        evidence_id = evidence_id or it["latest_evidence_id"]
        if not evidence_id:
            raise StateError("尚无执行证据，无法验收")
        ev = self.db.query_one("SELECT * FROM evidence WHERE id=?", (evidence_id,))
        if ev["item_id"] != item_id or ev["superseded_by"]:
            raise StateError("证据不存在、不属于该子项或已被替换，请基于最新证据验收")
        rnd = self.evidence_round(item_id, evidence_id)
        ts = now_iso()
        conflict: Optional[tuple[int, int, str, str]] = None

        with self.db.transaction() as db:
            # 本轮已有投票（含普通结论与冲突挂起）
            prior = db.query(
                "SELECT * FROM approvals WHERE item_id=? AND round=? AND verdict IN "
                "('approved','rejected','conflict') ORDER BY id",
                (item_id, rnd),
            )
            # 主持人对本轮冲突的裁定行：verdict 为终态且 conflict_with 非空
            host_ruled = any(
                p["verdict"] in ("approved", "rejected") and p["conflict_with"] is not None
                for p in prior
            )
            prior_other = [p for p in prior if p["acceptor"] != acceptor]
            prior_self = [p for p in prior if p["acceptor"] == acceptor]

            if host_ruled:
                raise StateError(f"第 {rnd} 轮验收已经主持人裁定，不能再提交")

            # 同一轮证据，同一验收人已提交过；复议须基于新证据（轮次 +1）
            if prior_self:
                raise StateError(
                    f"你已在第 {rnd} 轮提交验收；如被执行人复议，请基于新证据（新一轮）再验收"
                )

            # 同一轮、主持人尚未裁定，已有另一名验收人投票 → 并发冲突。
            # 取"另一名验收人的有效投票"（排除可能存在的冲突挂起行之外仍计入）。
            other_votes = [p for p in prior_other]
            if other_votes:
                first = other_votes[0]
                cur = db.execute(
                    "INSERT INTO approvals(item_id,evidence_id,acceptor,verdict,comment,"
                    "decided_at,round,conflict_with,original_verdict)"
                    " VALUES(?,?,?,'conflict',?,?,?,?,?)",
                    (item_id, evidence_id, acceptor,
                     f"与 {first['acceptor']} 同时提交：{comment}", ts, rnd,
                     first["id"], verdict),
                )
                new_id = cur.lastrowid
                db.execute(
                    "UPDATE approvals SET verdict='conflict',conflict_with=?,"
                    "original_verdict=COALESCE(original_verdict, verdict) "
                    "WHERE id=? AND verdict!='conflict'",
                    (new_id, first["id"]),
                )
                db.execute(
                    "UPDATE action_items SET status='conflict',accepted_approval_id=NULL,"
                    "updated_at=? WHERE id=?",
                    (ts, item_id),
                )
                self._item_version(
                    db, it, "approval_conflict",
                    f"验收人 {first['acceptor']} 与 {acceptor} 在第 {rnd} 轮同时提交，需主持人裁定",
                    acceptor, ts,
                )
                self.messages.send(
                    "conflict", "主持人",
                    f"验收冲突：决议 {d['code']} 子项 #{item_id}",
                    f"验收人 {first['acceptor']} 与 {acceptor} 对同一证据同时提交验收，"
                    f"已挂起，请裁定后继续。",
                )
                conflict = (first["id"], new_id, first["acceptor"], acceptor)

            else:
                cur = db.execute(
                    "INSERT INTO approvals(item_id,evidence_id,acceptor,verdict,comment,"
                    "decided_at,round) VALUES(?,?,?,?,?,?,?)",
                    (item_id, evidence_id, acceptor, verdict, comment, ts, rnd),
                )
                apid = cur.lastrowid
                if verdict == "approved":
                    db.execute(
                        "UPDATE action_items SET status='accepted',accepted_approval_id=?,"
                        "updated_at=? WHERE id=?",
                        (apid, ts, item_id),
                    )
                    self._item_version(db, it, "approval_pass", f"验收通过：{comment}", acceptor, ts)
                    self.messages.send(
                        "review", it["owner"],
                        f"验收通过：决议 {d['code']}",
                        f"你负责的事项经 {acceptor} 按决议原文核对通过。",
                    )
                else:
                    db.execute(
                        "UPDATE action_items SET status='rejected',updated_at=? WHERE id=?",
                        (ts, item_id),
                    )
                    self._item_version(
                        db, it, "approval_reject", f"验收不通过：{comment}（可提交新证据复议）",
                        acceptor, ts,
                    )
                    self.messages.send(
                        "review", it["owner"],
                        f"验收不通过，需复议：决议 {d['code']}",
                        f"{acceptor} 驳回：{comment}。请整改后提交新证据进入第 {rnd + 1} 轮。",
                    )

        # 事务成功提交后再向调用方抛出冲突信号（否则回滚会清掉冲突标记）
        if conflict:
            raise ConflictError(
                f"两名验收人同时提交（{conflict[2]} / {conflict[3]}），已标记冲突并提交主持人",
                (conflict[0], conflict[1]),
            )

        apid = self.db.query_one(
            "SELECT id FROM approvals WHERE item_id=? ORDER BY id DESC LIMIT 1", (item_id,)
        )["id"]
        return dict(self.db.query_one("SELECT * FROM approvals WHERE id=?", (apid,)))

    def resolve_conflict(
        self, item_id: int, winning_approval_id: int, host: str = "主持人", note: str = ""
    ) -> str:
        """主持人对并发冲突裁定：采纳一方的原始结论生效。返回最终结论。"""
        it = self._item(item_id)
        if it["status"] != "conflict":
            raise StateError("该子项不处于验收冲突状态")
        win = self.db.query_one("SELECT * FROM approvals WHERE id=?", (winning_approval_id,))
        if not win or win["item_id"] != item_id or win["verdict"] != "conflict":
            raise StateError("被采纳的验收记录无效（必须是冲突中的一方）")
        final_verdict = win["original_verdict"] or "approved"
        final_status = "accepted" if final_verdict == "approved" else "rejected"
        ts = now_iso()
        with self.db.transaction() as db:
            cur = db.execute(
                "INSERT INTO approvals(item_id,evidence_id,acceptor,verdict,comment,"
                "decided_at,round,conflict_with) VALUES(?,?,?,?,?,?,?,?)",
                (item_id, win["evidence_id"], host, final_verdict,
                 f"主持人裁定采纳验收人 {win['acceptor']} 的原始结论（{final_verdict}）。{note}",
                 ts, win["round"], win["id"]),
            )
            apid = cur.lastrowid
            if final_verdict == "approved":
                db.execute(
                    "UPDATE action_items SET status='accepted',accepted_approval_id=?,"
                    "updated_at=? WHERE id=?",
                    (apid, ts, item_id),
                )
            else:
                db.execute(
                    "UPDATE action_items SET status='rejected',accepted_approval_id=NULL,"
                    "updated_at=? WHERE id=?",
                    (ts, item_id),
                )
            self._item_version(
                db, it, "conflict_resolved",
                f"主持人裁定冲突，采纳 {win['acceptor']}（{final_verdict}），结论：{final_status}",
                host, ts,
            )
        return final_verdict

    # ================================================================
    # 改期 / 改派（责任人离职）/ 撤销
    # ================================================================
    def reschedule_item(self, item_id: int, new_due: str, reason: str, actor: str) -> None:
        if not reason:
            raise DomainError("改期必须填写原因")
        parse_date(new_due)  # 校验
        it = self._item(item_id)
        self._require_mutable_item(it)
        ts = now_iso()
        with self.db.transaction() as db:
            db.execute(
                "UPDATE action_items SET due_date=?,delay_reason=?,updated_at=? WHERE id=?",
                (new_due, reason, ts, item_id),
            )
            self._item_version(
                db, it, "reschedule",
                f"期限 {it['due_date']} → {new_due}：{reason}", actor, ts,
            )
            self.messages.send(
                "dispatch", it["owner"], "责任项改期通知",
                f"你的责任项期限改为 {new_due}。原因：{reason}",
            )

    def reassign_item(self, item_id: int, new_owner: str, reason: str, actor: str) -> None:
        """责任人离职等原因改派。"""
        if not reason:
            raise DomainError("改派必须填写原因（如：责任人离职）")
        it = self._item(item_id)
        self._require_mutable_item(it)
        if new_owner == it["acceptor"]:
            raise PermissionDenied("新负责人不能与验收人为同一人")
        old_owner = it["owner"]
        ts = now_iso()
        with self.db.transaction() as db:
            db.execute(
                "UPDATE action_items SET owner=?,updated_at=? WHERE id=?",
                (new_owner, ts, item_id),
            )
            self._item_version(
                db, it, "reassign",
                f"负责人 {old_owner} → {new_owner}：{reason}", actor, ts,
            )
            self.messages.send(
                "dispatch", new_owner, "责任项改派通知",
                f"原由 {old_owner} 负责的事项改派给你（原因：{reason}），期限 {it['due_date']}。",
            )

    def revoke_decision(self, decision_id: int, reason: str, actor: str = "主持人") -> None:
        """撤销决议：子项全部回收(cancelled)，提醒失效；全程留版本链。"""
        if not reason:
            raise DomainError("撤销必须填写原因")
        d = self._decision(decision_id)
        if d["status"] == ARCHIVED:
            raise ArchivedError("已归档决议不能撤销，只允许补充附注")
        if d["revoked"]:
            raise StateError("决议已撤销")
        ts = now_iso()
        with self.db.transaction() as db:
            db.execute(
                "UPDATE decisions SET revoked=1,status=? WHERE id=?",
                (REVOKED, decision_id),
            )
            self._decision_version(
                db, decision_id, "revoke", f"决议撤销：{reason}", actor, ts,
            )
            for it in db.query("SELECT * FROM action_items WHERE decision_id=?", (decision_id,)):
                if it["status"] not in TERMINAL_STATUSES:
                    db.execute(
                        "UPDATE action_items SET status='cancelled',updated_at=? WHERE id=?",
                        (ts, it["id"]),
                    )
                    self._item_version(
                        db, dict(it), "cancel",
                        f"决议撤销，待办回收：{reason}", actor, ts,
                    )
                    self.messages.send(
                        "revoke", it["owner"], "决议撤销，责任项回收",
                        f"决议 {d['code']} 已撤销（{reason}），你名下待办自动关闭。",
                    )

    # ================================================================
    # 归档：只允许补充附注
    # ================================================================
    def archive_decision(self, decision_id: int, actor: str = "主持人") -> None:
        d = self._decision(decision_id)
        if d["status"] == ARCHIVED:
            raise StateError("决议已归档")
        ts = now_iso()
        with self.db.transaction() as db:
            db.execute(
                "UPDATE decisions SET status=?,archived_at=? WHERE id=?",
                (ARCHIVED, ts, decision_id),
            )
            db.execute("UPDATE meetings SET archived=1 WHERE id=?", (d["meeting_id"],))
            self._decision_version(
                db, decision_id, "archive", "决议归档", actor, ts,
            )

    def add_archive_note(self, decision_id: int, author: str, note: str) -> int:
        d = self._decision(decision_id)
        if d["status"] != ARCHIVED:
            raise StateError("只有已归档决议才通过附注入口补充说明")
        note = (note or "").strip()
        if not note:
            raise DomainError("附注内容不能为空")
        cur = self.db.execute(
            "INSERT INTO archive_notes(decision_id,author,note,created_at) VALUES(?,?,?,?)",
            (decision_id, author, note, now_iso()),
        )
        return cur.lastrowid

    # ================================================================
    # 查询：列表 / 详情 / 待办 / 版本链 / 归档检索
    # ================================================================
    def list_items(self, decision_id: int) -> list[dict[str, Any]]:
        items = [
            self._enrich_item(dict(r))
            for r in self.db.query(
                "SELECT * FROM action_items WHERE decision_id=? ORDER BY id", (decision_id,)
            )
        ]
        return items

    def decision_summary(self, decision_id: int) -> ItemSummary:
        """汇总状态只从子项实时计算。"""
        counts = {k: 0 for k in
                  ("pending", "in_progress", "submitted", "rejected", "conflict",
                   "accepted", "cancelled")}
        rows = self.db.query(
            "SELECT status,COUNT(*) c FROM action_items WHERE decision_id=? GROUP BY status",
            (decision_id,),
        )
        total = 0
        for r in rows:
            counts[r["status"]] = r["c"]
            total += r["c"]
        return ItemSummary(total=total, **counts)

    def list_decisions(self, meeting_id: Optional[int] = None) -> list[dict[str, Any]]:
        """决议列表：责任人、期限、验收状态（汇总自子项）。"""
        sql = "SELECT * FROM decisions"
        params: tuple = ()
        if meeting_id:
            sql += " WHERE meeting_id=?"
            params = (meeting_id,)
        out = []
        for r in self.db.query(sql + " ORDER BY code", params):
            d = dict(r)
            items = self.list_items(d["id"])
            s = self.decision_summary(d["id"])
            d["rollup"] = s.rollup_status
            d["summary"] = s
            d["items"] = items
            d["owners"] = sorted({i["owner"] for i in items if i["status"] != "cancelled"})
            d["due_dates"] = sorted(
                {i["due_date"] for i in items if i["status"] != "cancelled"}
            )
            out.append(d)
        return out

    def get_decision_detail(self, decision_id: int) -> dict[str, Any]:
        """详情：决议原文 与 最新执行证据 并排。"""
        d = self._decision(decision_id)
        d = dict(d)
        d["items"] = self.list_items(decision_id)
        s = self.decision_summary(decision_id)
        d["rollup"] = s.rollup_status
        d["summary"] = s
        for it in d["items"]:
            it["evidence_chain"] = self.evidence_chain(it["id"])
            it["approvals"] = [
                dict(r) for r in self.db.query(
                    "SELECT * FROM approvals WHERE item_id=? ORDER BY id", (it["id"],)
                )
            ]
        d["versions"] = self.decision_versions(decision_id)
        d["archive_notes"] = [
            dict(r) for r in self.db.query(
                "SELECT * FROM archive_notes WHERE decision_id=? ORDER BY id", (decision_id,)
            )
        ]
        d["meeting"] = self.get_meeting(d["meeting_id"])
        return d

    def evidence_chain(self, item_id: int) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM evidence WHERE item_id=? ORDER BY id", (item_id,)
        )
        return [dict(r) for r in rows]

    def decision_versions(self, decision_id: int) -> list[dict[str, Any]]:
        return [
            dict(r) for r in self.db.query(
                "SELECT * FROM decision_versions WHERE decision_id=? ORDER BY version",
                (decision_id,),
            )
        ]

    def item_versions(self, item_id: int) -> list[dict[str, Any]]:
        return [
            dict(r) for r in self.db.query(
                "SELECT * FROM action_item_versions WHERE item_id=? ORDER BY version",
                (item_id,),
            )
        ]

    def todo_list(self, owner: Optional[str] = None) -> list[dict[str, Any]]:
        """待办：只含未终结子项；撤销(cancelled)/已验收(accepted) 自动回收，不出现在此。"""
        sql = ("SELECT ai.*, d.code decision_code, d.original_text decision_text, "
               "m.title meeting_title "
               "FROM action_items ai JOIN decisions d ON d.id=ai.decision_id "
               "JOIN meetings m ON m.id=d.meeting_id "
               "WHERE ai.status NOT IN ('accepted','cancelled') AND d.revoked=0")
        params: tuple = ()
        if owner:
            sql += " AND ai.owner=?"
            params = (owner,)
        sql += " ORDER BY ai.due_date, ai.id"
        return [self._enrich_item(dict(r)) for r in self.db.query(sql, params)]

    def review_queue(self, acceptor: Optional[str] = None) -> list[dict[str, Any]]:
        """验收/复议队列：等待验收人处理的子项（含历轮驳回后的新证据）。"""
        sql = ("SELECT ai.*, d.code decision_code, d.original_text decision_text "
               "FROM action_items ai JOIN decisions d ON d.id=ai.decision_id "
               "WHERE ai.status IN ('submitted','rejected','conflict') AND d.revoked=0")
        params: tuple = ()
        if acceptor:
            sql += " AND ai.acceptor=?"
            params = (acceptor,)
        return [self._enrich_item(dict(r)) for r in self.db.query(sql, params)]

    def search_archive(self, keyword: str = "") -> list[dict[str, Any]]:
        """归档检索：按决议编号 / 原文 / 责任部门 / 负责人 / 归档附注 搜已归档决议。"""
        like = f"%{keyword}%"
        rows = self.db.query(
            "SELECT DISTINCT d.* FROM decisions d "
            "LEFT JOIN action_items ai ON ai.decision_id=d.id "
            "LEFT JOIN archive_notes an ON an.decision_id=d.id "
            "WHERE d.status='archived' AND ("
            "d.code LIKE ? OR d.original_text LIKE ? OR ai.department LIKE ? "
            "OR ai.owner LIKE ? OR COALESCE(ai.delay_reason,'') LIKE ? "
            "OR COALESCE(an.note,'') LIKE ?) "
            "ORDER BY d.archived_at DESC",
            (like, like, like, like, like, like),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["items"] = self.list_items(d["id"])
            d["archive_notes"] = [
                dict(x) for x in self.db.query(
                    "SELECT * FROM archive_notes WHERE decision_id=? ORDER BY id", (d["id"],)
                )
            ]
            out.append(d)
        return out

    def list_messages(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM messages ORDER BY id DESC LIMIT 100")]

    # ================================================================
    # 内部工具
    # ================================================================
    def _decision(self, decision_id: int) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM decisions WHERE id=?", (decision_id,))
        if not row:
            raise StateError(f"决议 {decision_id} 不存在")
        return dict(row)

    def _item(self, item_id: int) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM action_items WHERE id=?", (item_id,))
        if not row:
            raise StateError(f"子项 {item_id} 不存在")
        return dict(row)

    def _enrich_item(self, it: dict[str, Any]) -> dict[str, Any]:
        """附加逾期天数与原因、最新证据、轮次等展示字段。逾期天数实时计算。"""
        today = date.today()
        due = parse_date(it["due_date"])
        it["overdue_days"] = 0
        it["overdue_reason"] = ""
        if it["status"] not in TERMINAL_STATUSES and due < today:
            it["overdue_days"] = (today - due).days
            it["overdue_reason"] = it.get("delay_reason") or "未说明原因（已计入逾期）"
        if it.get("latest_evidence_id"):
            ev = self.db.query_one(
                "SELECT * FROM evidence WHERE id=?", (it["latest_evidence_id"],)
            )
            it["latest_evidence"] = dict(ev) if ev else None
            it["round"] = self.evidence_round(it["id"], it["latest_evidence_id"])
        else:
            it["latest_evidence"] = None
            it["round"] = 0
        return it

    def _require_editable(self, d: dict[str, Any]) -> None:
        if d["status"] == ARCHIVED:
            raise ArchivedError("已归档决议只允许补充附注")
        if d["revoked"]:
            raise StateError("决议已撤销，不能编辑")

    def _require_active_item(self, it: dict[str, Any]) -> None:
        d = self._decision(it["decision_id"])
        self._require_editable(d)
        if it["status"] == "cancelled":
            raise StateError("子项已随决议撤销回收")

    def _require_mutable_item(self, it: dict[str, Any]) -> None:
        """改期/改派：已通过验收的子项不再变动期限。"""
        self._require_active_item(it)
        if it["status"] == "accepted":
            raise StateError("子项已验收通过，不能改期/改派")

    def _item_snapshot_json(self, item_id: int, ts: str) -> str:
        import json
        row = self.db.query_one("SELECT * FROM action_items WHERE id=?", (item_id,))
        return json.dumps(dict(row) if row else {}, ensure_ascii=False)

    def _decision_version(self, db, decision_id, ctype, reason, actor, ts, version=None):
        import json
        if version is None:
            version = db.query_one(
                "SELECT COALESCE(MAX(version),0)+1 v FROM decision_versions WHERE decision_id=?",
                (decision_id,))["v"]
        row = db.query_one("SELECT * FROM decisions WHERE id=?", (decision_id,))
        db.execute(
            "UPDATE decisions SET current_version=? WHERE id=?", (version, decision_id))
        db.execute(
            "INSERT INTO decision_versions(decision_id,version,parent_version,change_type,"
            "reason,actor,changed_at,snapshot) VALUES(?,?,?,?,?,?,?,?)",
            (decision_id, version, version - 1 if version > 1 else None, ctype,
             reason, actor, ts, json.dumps(dict(row), ensure_ascii=False)),
        )

    def _item_version(self, db, it, ctype, reason, actor, ts):
        """追加一条子项版本（version+1）。版本号在事务内从 MAX(version) 实时分配，
        避免并发提交时两个调用方用同一快照算出相同版本号。"""
        import json
        item_id = it["id"]
        new_ver = (db.query_one(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM action_item_versions WHERE item_id=?",
            (item_id,))["v"])
        parent = db.query_one(
            "SELECT current_version FROM action_items WHERE id=?", (item_id,))["current_version"]
        row = db.query_one("SELECT * FROM action_items WHERE id=?", (item_id,))
        snapshot = json.dumps(dict(row) if row else {}, ensure_ascii=False)
        db.execute(
            "INSERT INTO action_item_versions(item_id,version,parent_version,change_type,"
            "reason,actor,changed_at,snapshot) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, new_ver, parent, ctype, reason, actor, ts, snapshot),
        )
        db.execute(
            "UPDATE action_items SET current_version=? WHERE id=?", (new_ver, item_id)
        )
