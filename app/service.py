"""领域服务：决议分派、子项跟踪、证据、验收、复议、版本链、待办回收。

所有写操作都同时写 log 表形成版本链；状态不从冗余列读取，
决议/子项汇总状态一律由证据与验收记录现算。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path

from .messages import MessageAdapter


class DomainError(Exception):
    """业务规则违例，status 是建议的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class NotFound(DomainError):
    def __init__(self, what: str):
        super().__init__(f"{what}不存在", 404)


class Forbidden(DomainError):
    def __init__(self, message: str):
        super().__init__(message, 403)


class Conflict(DomainError):
    def __init__(self, message: str, info=None):
        super().__init__(message, 409)
        self.info = info


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Service:
    def __init__(self, conn: sqlite3.Connection, attachments_dir: Path,
                 messenger: MessageAdapter | None = None):
        self.conn = conn
        self.attachments = Path(attachments_dir)
        self.attachments.mkdir(parents=True, exist_ok=True)
        self.msg = messenger or MessageAdapter(conn)

    # ---------------------------------------------------------------- 用户

    def list_users(self, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM users"
        if active_only:
            sql += " WHERE active=1"
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY id")]

    def _user(self, uid: str) -> dict:
        row = self.conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise NotFound("用户")
        return dict(row)

    # ---------------------------------------------------------------- 会议

    def list_meetings(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT m.*, s.name secretary_name, h.name host_name
               FROM meetings m
               JOIN users s ON s.id=m.secretary_id
               JOIN users h ON h.id=m.host_id
               ORDER BY m.held_at DESC"""
        ).fetchall()
        meetings = [dict(r) for r in rows]
        for m in meetings:
            m["topics"] = [dict(r) for r in self.conn.execute(
                "SELECT * FROM topics WHERE meeting_id=? ORDER BY order_no", (m["id"],))]
        return meetings

    def get_meeting(self, meeting_id: str) -> dict:
        row = self.conn.execute(
            """SELECT m.*, s.name secretary_name, h.name host_name
               FROM meetings m
               JOIN users s ON s.id=m.secretary_id
               JOIN users h ON h.id=m.host_id
               WHERE m.id=?""", (meeting_id,)).fetchone()
        if not row:
            raise NotFound("会议")
        m = dict(row)
        m["topics"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM topics WHERE meeting_id=? ORDER BY order_no", (meeting_id,))]
        return m

    # ---------------------------------------------------------------- 日志

    def _log(self, resolution_id: str, kind: str, actor_id: str | None,
             summary: str, item_id: str | None = None, payload: dict | None = None):
        self.conn.execute(
            """INSERT INTO log (id,resolution_id,item_id,kind,actor_id,summary,payload,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (_uid("log"), resolution_id, item_id, kind, actor_id, summary,
             json.dumps(payload or {}, ensure_ascii=False), _now()))

    def version_chain(self, resolution_id: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT l.*, u.name actor_name FROM log l
               LEFT JOIN users u ON u.id=l.actor_id
               WHERE l.resolution_id=? ORDER BY l.created_at""", (resolution_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"] or "{}")
            out.append(d)
        return out

    # ------------------------------------------------------------ 决议草稿

    def create_draft(self, secretary_id: str, data: dict) -> str:
        meeting = self.get_meeting(data["meeting_id"])
        if secretary_id != meeting["secretary_id"]:
            raise Forbidden("只有本次会议秘书可以整理决议草稿")
        items = data.get("items") or []
        if not items:
            raise DomainError("至少要有一项责任事项")
        rid = _uid("res")
        now = _now()
        self.conn.execute(
            """INSERT INTO resolutions
               (id,meeting_id,topic_id,code,title,original_text,status,
                created_by,archive_note,created_at,updated_at)
               VALUES (?,?,?,?,?,?,'draft',?,'',?,?)""",
            (rid, meeting["id"], data.get("topic_id"), data["code"], data["title"],
             data["original_text"], secretary_id, now, now))
        for seq, it in enumerate(items, 1):
            iid = _uid("item")
            self.conn.execute(
                """INSERT INTO items (id,resolution_id,seq,content,department,
                      assignee_id,due_date,current_round,delay_reason,created_at)
                   VALUES (?,?,?,?,?,?,?,1,'',?)""",
                (iid, rid, seq, it["content"], it["department"],
                 it["assignee_id"], it["due_date"], now))
            for vid in it.get("verifier_ids", []):
                self.conn.execute(
                    "INSERT INTO item_verifiers (item_id,user_id) VALUES (?,?)",
                    (iid, vid))
        self._log(rid, "draft", secretary_id,
                  f"秘书 {self._user(secretary_id)['name']} 整理决议草稿《{data['title']}》",
                  payload={"items": len(items)})
        return rid

    def confirm(self, host_id: str, resolution_id: str) -> None:
        """主持人确认草稿并分派：生成执行人/验收人待办并发出通知。"""
        res = self._resolution(resolution_id)
        if host_id != self._meeting_host(res["meeting_id"]):
            raise Forbidden("只有主持人可以确认分派决议")
        if res["status"] != "draft":
            raise Conflict(f"决议当前为「{self.STATUS_CN[res['status']]}」，不能确认分派")
        now = _now()
        self.conn.execute(
            """UPDATE resolutions SET status='confirmed',confirmed_by=?,confirmed_at=?,
               updated_at=? WHERE id=?""", (host_id, now, now, resolution_id))
        for item in self._items(resolution_id):
            self._open_todo(item["id"], "executor", item["assignee_id"], 1, now)
            for v in self._verifier_ids(item["id"]):
                self._open_todo(item["id"], "verifier", v, 1, now)
            self.msg.send("executor", item["assignee_id"],
                          f"决议 {res['code']} 已分派",
                          f"您负责「{item['content']}」，期限 {item['due_date']}")
            for v in self._verifier_ids(item["id"]):
                self.msg.send("verifier", v, f"决议 {res['code']} 待验收任务",
                              f"请关注「{item['content']}」，执行人提交证据后按决议原文验收")
        self._log(resolution_id, "confirm", host_id,
                  f"主持人 {self._user(host_id)['name']} 确认并分派决议",
                  payload={"item_count": len(self._items(resolution_id))})

    STATUS_CN = {"draft": "草稿", "confirmed": "已分派", "completed": "已完成",
                 "revoked": "已撤销", "archived": "已归档"}

    # ------------------------------------------------------------ 决议读取

    def _resolution(self, rid: str) -> dict:
        row = self.conn.execute("SELECT * FROM resolutions WHERE id=?", (rid,)).fetchone()
        if not row:
            raise NotFound("决议")
        return dict(row)

    def _meeting_host(self, meeting_id: str) -> str:
        row = self.conn.execute("SELECT host_id FROM meetings WHERE id=?",
                                (meeting_id,)).fetchone()
        return row["host_id"] if row else ""

    def _items(self, rid: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM items WHERE resolution_id=? ORDER BY seq", (rid,))]

    def _verifier_ids(self, item_id: str) -> list[str]:
        return [r["user_id"] for r in self.conn.execute(
            "SELECT user_id FROM item_verifiers WHERE item_id=?", (item_id,))]

    def _item_view(self, item: dict) -> dict:
        d = dict(item)
        d["verifier_ids"] = self._verifier_ids(item["id"])
        assignee = self._user(item["assignee_id"])
        d["assignee_name"] = assignee["name"]
        d["assignee_active"] = bool(assignee["active"])
        d["verifiers"] = [self._user(v) for v in d["verifier_ids"]]

        evidences = [dict(r) for r in self.conn.execute(
            "SELECT * FROM evidence WHERE item_id=? ORDER BY created_at", (item["id"],))]
        verdicts = [dict(r) for r in self.conn.execute(
            "SELECT * FROM verdicts WHERE item_id=? ORDER BY created_at", (item["id"],))]
        requests = [dict(r) for r in self.conn.execute(
            "SELECT * FROM review_requests WHERE item_id=? ORDER BY created_at",
            (item["id"],))]

        current = [e for e in evidences if e["round"] == item["current_round"]
                   and e["superseded_by"] is None]
        d["latest_evidence"] = current[-1] if current else None
        d["evidence_count"] = len(evidences)
        last_verdict = verdicts[-1] if verdicts else None
        d["last_verdict"] = last_verdict
        if last_verdict:
            d["last_verdict"]["verifier_name"] = self._user(last_verdict["verifier_id"])["name"]
        pending_appeal = any(r["status"] == "pending" for r in requests)
        d["appeal_pending"] = pending_appeal

        if last_verdict and last_verdict["approved"]:
            d["item_status"] = "approved"
        elif pending_appeal:
            d["item_status"] = "appeal_pending"
        elif last_verdict and not last_verdict["approved"]:
            d["item_status"] = "rejected"
        elif d["latest_evidence"]:
            d["item_status"] = "submitted"
        else:
            d["item_status"] = "pending"

        today = date.today().isoformat()
        d["overdue"] = d["item_status"] != "approved" and item["due_date"] < today
        if d["overdue"]:
            d["overdue_days"] = (date.today() - date.fromisoformat(item["due_date"])).days
        else:
            d["overdue_days"] = 0
        d["verdicts"] = verdicts
        d["review_requests"] = requests
        return d

    ITEM_STATUS_CN = {"pending": "待提交", "submitted": "待验收", "rejected": "验收不通过",
                      "approved": "验收通过", "appeal_pending": "复议中"}

    def list_resolutions(self) -> list[dict]:
        out = []
        for r in self.conn.execute(
                "SELECT * FROM resolutions ORDER BY created_at DESC").fetchall():
            res = dict(r)
            items = [self._item_view(i) for i in self._items(res["id"])]
            res["items"] = items
            res["status_cn"] = self.STATUS_CN[res["status"]]
            if res["status"] == "confirmed":
                approved = sum(i["item_status"] == "approved" for i in items)
                if approved == len(items):
                    res["rollup"] = "completed"
                    res["rollup_cn"] = "已完成"
                elif any(i["item_status"] != "pending" for i in items):
                    res["rollup"] = "in_progress"
                    res["rollup_cn"] = f"进行中（{approved}/{len(items)} 项通过）"
                else:
                    res["rollup"] = "pending"
                    res["rollup_cn"] = "待执行"
                res["approved_count"] = approved
            else:
                res["rollup"] = res["status"]
                res["rollup_cn"] = res["status_cn"]
                res["approved_count"] = sum(i["item_status"] == "approved" for i in items)
            res["total_count"] = len(items)
            res["overdue_count"] = sum(i["overdue"] for i in items)
            meeting = self.conn.execute(
                "SELECT title,held_at FROM meetings WHERE id=?", (res["meeting_id"],)).fetchone()
            res["meeting_title"] = meeting["title"]
            res["held_at"] = meeting["held_at"]
            out.append(res)
        return out

    def get_resolution(self, rid: str) -> dict:
        res = self._resolution(rid)
        res["items"] = [self._item_view(i) for i in self._items(rid)]
        res["status_cn"] = self.STATUS_CN[res["status"]]
        res["meeting"] = self.get_meeting(res["meeting_id"])
        res["notes"] = [dict(r) for r in self.conn.execute(
            """SELECT n.*, u.name author_name FROM notes n JOIN users u ON u.id=n.author_id
               WHERE n.resolution_id=? ORDER BY n.created_at""", (rid,))]
        res["log"] = self.version_chain(rid)
        approved = sum(i["item_status"] == "approved" for i in res["items"])
        res["approved_count"] = approved
        res["total_count"] = len(res["items"])
        return res

    # ------------------------------------------------------------ 生命周期

    def _require_editable(self, res: dict) -> None:
        if res["status"] == "archived":
            raise Conflict("决议已归档，只允许补充附注")
        if res["status"] == "revoked":
            raise Conflict("决议已撤销，不能再变更")
        if res["status"] != "confirmed":
            raise Conflict("决议尚未确认分派")

    def reschedule(self, actor_id: str, item_id: str, new_due: str, reason: str) -> None:
        item = self._item(item_id)
        res = self._resolution(item["resolution_id"])
        self._require_editable(res)
        if actor_id != self._meeting_host(res["meeting_id"]):
            raise Forbidden("只有主持人可以批准改期")
        if not reason.strip():
            raise DomainError("改期必须填写原因")
        date.fromisoformat(new_due)  # 校验格式
        old_due = item["due_date"]
        if new_due == old_due:
            raise DomainError("新期限与原期限相同")
        self.conn.execute("UPDATE items SET due_date=?, delay_reason=? WHERE id=?",
                          (new_due, reason.strip(), item_id))
        self._open_todo(item_id, "executor", item["assignee_id"], item["current_round"])
        self._log(res["id"], "reschedule", actor_id,
                  f"期限改期：{old_due} → {new_due}（原因：{reason.strip()}）",
                  item_id, {"old_due": old_due, "new_due": new_due, "reason": reason.strip()})
        self.msg.send("executor", item["assignee_id"],
                      f"决议任务改期通知",
                      f"「{item['content']}」完成期限改为 {new_due}。原因：{reason.strip()}")

    def reassign(self, actor_id: str, item_id: str, new_assignee_id: str,
                 reason: str = "责任人离职") -> None:
        """责任人离职等原因的改派：旧待办回收留痕，新待办开启。"""
        item = self._item(item_id)
        res = self._resolution(item["resolution_id"])
        self._require_editable(res)
        if actor_id != self._meeting_host(res["meeting_id"]):
            raise Forbidden("只有主持人可以改派责任人")
        new_user = self._user(new_assignee_id)
        if not new_user["active"]:
            raise DomainError("不能改派给已离职人员")
        old_id = item["assignee_id"]
        if old_id == new_assignee_id:
            raise DomainError("新旧责任人相同")
        self.conn.execute(
            "UPDATE items SET assignee_id=? WHERE id=?", (new_assignee_id, item_id))
        self._recycle_todos(item_id, "executor", old_id,
                            f"{reason}，改派 {new_user['name']}")
        self._open_todo(item_id, "executor", new_assignee_id, item["current_round"])
        self._log(res["id"], "reassign", actor_id,
                  f"责任人变更：{self._user(old_id)['name']} → {new_user['name']}（{reason}）",
                  item_id, {"old_assignee": old_id, "new_assignee": new_assignee_id,
                            "reason": reason})
        self.msg.send("executor", new_assignee_id, "决议任务改派通知",
                      f"「{item['content']}」改由您负责，期限 {item['due_date']}")

    def revoke(self, actor_id: str, resolution_id: str, reason: str) -> None:
        res = self._resolution(resolution_id)
        if actor_id != self._meeting_host(res["meeting_id"]):
            raise Forbidden("只有主持人可以撤销决议")
        if res["status"] == "revoked":
            raise Conflict("决议已处于撤销状态")
        if res["status"] == "archived":
            raise Conflict("已归档决议不能撤销，只允许补充附注")
        now = _now()
        self.conn.execute(
            """UPDATE resolutions SET status='revoked',revoked_by=?,revoke_reason=?,
               updated_at=? WHERE id=?""", (actor_id, reason, now, resolution_id))
        for item in self._items(resolution_id):
            self._recycle_todos(item["id"], None, None, f"决议撤销：{reason}")
        self._log(resolution_id, "revoke", actor_id, f"决议撤销（原因：{reason}）",
                  payload={"reason": reason})
        for item in self._items(resolution_id):
            self.msg.send("executor", item["assignee_id"], "决议撤销通知",
                          f"「{item['content']}」所属决议已撤销，待办已回收。原因：{reason}")

    def archive(self, actor_id: str, resolution_id: str, note: str = "") -> None:
        res = self._resolution(resolution_id)
        if actor_id != self._meeting_host(res["meeting_id"]):
            raise Forbidden("只有主持人可以归档决议")
        if res["status"] == "archived":
            raise Conflict("决议已归档")
        if res["status"] != "confirmed":
            raise Conflict("只有已分派决议可以归档")
        items = self._items(resolution_id)
        views = [self._item_view(i) for i in items]
        not_done = [i for i in views if i["item_status"] != "approved"]
        if not_done:
            raise Conflict(f"还有 {len(not_done)} 项未验收通过，不能归档")
        now = _now()
        self.conn.execute(
            "UPDATE resolutions SET status='archived',archived_by=?,archived_at=?,"
            "archive_note=?,updated_at=? WHERE id=?",
            (actor_id, now, note, now, resolution_id))
        for item in items:
            self._recycle_todos(item["id"], None, None, "决议归档，待办关闭")
        self._log(resolution_id, "archive", actor_id, "决议归档" +
                  (f"（{note}）" if note else ""), payload={"note": note})

    def add_note(self, actor_id: str, resolution_id: str, body: str) -> None:
        res = self._resolution(resolution_id)
        if not body.strip():
            raise DomainError("附注内容不能为空")
        now = _now()
        self.conn.execute(
            "INSERT INTO notes (id,resolution_id,author_id,body,created_at) VALUES (?,?,?,?,?)",
            (_uid("note"), resolution_id, actor_id, body.strip(), now))
        label = "归档后补充附注" if res["status"] == "archived" else "补充附注"
        self._log(resolution_id, "note", actor_id, f"{label}：{body.strip()[:60]}")

    # ---------------------------------------------------------------- 待办

    def _open_todo(self, item_id, kind, user_id, round_no, when=None):
        self.conn.execute(
            """INSERT INTO todos (id,item_id,kind,user_id,state,opened_at,round)
               VALUES (?,?,?,?, 'open',?,?)""",
            (_uid("todo"), item_id, kind, user_id, when or _now(), round_no))

    def _recycle_todos(self, item_id, kind, user_id, reason):
        sql = "UPDATE todos SET state='recycled',closed_at=?,close_reason=? WHERE state='open'"
        args = [_now(), reason]
        if item_id is not None:
            sql += " AND item_id=?"; args.append(item_id)
        if kind is not None:
            sql += " AND kind=?"; args.append(kind)
        if user_id is not None:
            sql += " AND user_id=?"; args.append(user_id)
        self.conn.execute(sql, args)

    def _close_todos(self, item_id, kind, user_id, round_no, reason):
        self.conn.execute(
            """UPDATE todos SET state='closed',closed_at=?,close_reason=?
               WHERE state='open' AND item_id=? AND kind=? AND user_id=? AND round=?""",
            (_now(), reason, item_id, kind, user_id, round_no))

    def my_todos(self, user_id: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT t.*, i.content, i.due_date, i.resolution_id, i.current_round,
                      r.code resolution_code, r.title resolution_title, r.status res_status,
                      u.name user_name
               FROM todos t
               JOIN items i ON i.id=t.item_id
               JOIN resolutions r ON r.id=i.resolution_id
               JOIN users u ON u.id=t.user_id
               WHERE t.user_id=? ORDER BY t.state, i.due_date""", (user_id,)).fetchall()
        out = []
        today = date.today().isoformat()
        for r in rows:
            d = dict(r)
            d["kind_cn"] = "执行" if d["kind"] == "executor" else "验收"
            d["state_cn"] = {"open": "待处理", "closed": "已完成",
                             "recycled": "已回收"}[d["state"]]
            d["overdue"] = d["state"] == "open" and d["due_date"] < today
            if d["overdue"]:
                d["overdue_days"] = (date.today() - date.fromisoformat(d["due_date"])).days
            out.append(d)
        return out

    def recycle_bin(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT t.*, i.content, u.name user_name, r.code resolution_code
               FROM todos t JOIN items i ON i.id=t.item_id
               JOIN users u ON u.id=t.user_id
               JOIN resolutions r ON r.id=i.resolution_id
               WHERE t.state='recycled' ORDER BY t.closed_at DESC""").fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 证据

    def _item(self, item_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise NotFound("责任事项")
        return dict(row)

    def upload_evidence(self, actor_id: str, item_id: str, filename: str,
                        data: bytes, note: str) -> dict:
        item = self._item(item_id)
        res = self._resolution(item["resolution_id"])
        self._require_editable(res)
        if item["assignee_id"] != actor_id:
            raise Forbidden("只有责任人本人可以上传执行证据")
        if not filename.strip():
            raise DomainError("文件名不能为空")
        current_view = self._item_view(item)
        if current_view["item_status"] == "approved":
            raise Conflict("该事项已验收通过，不能再提交证据")

        last = self.conn.execute(
            "SELECT * FROM verdicts WHERE item_id=? ORDER BY created_at DESC LIMIT 1",
            (item_id,)).fetchone()
        if last and not last["approved"]:
            # 验收不通过后提交：进入下一轮
            new_round = item["current_round"] + 1
            self.conn.execute("UPDATE items SET current_round=? WHERE id=?",
                              (new_round, item_id))
            item["current_round"] = new_round
        round_no = item["current_round"]

        # 同一轮再次上传：替换证据，旧行保留并指向新行
        prev = self.conn.execute(
            """SELECT * FROM evidence WHERE item_id=? AND round=? AND superseded_by IS NULL
               ORDER BY created_at DESC LIMIT 1""", (item_id, round_no)).fetchone()

        eid = _uid("ev")
        safe = Path(filename).name.replace(" ", "_")
        stored = f"{eid}__{safe}"
        (self.attachments / stored).write_bytes(data)
        self.conn.execute(
            """INSERT INTO evidence (id,item_id,round,filename,stored_name,note,
                 uploaded_by,created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (eid, item_id, round_no, safe, stored, note, actor_id, _now()))
        if prev:
            self.conn.execute("UPDATE evidence SET superseded_by=? WHERE id=?",
                              (eid, prev["id"]))
        # 新证据进入待验收：开验收待办，关闭旧轮次残留
        for v in self._verifier_ids(item_id):
            self._close_todos(item_id, "verifier", v, round_no, "新证据到达，刷新验收任务")
            self._open_todo(item_id, "verifier", v, round_no)
        for v in self._verifier_ids(item_id):
            self.msg.send("verifier", v, "有新的执行证据待验收",
                          f"「{item['content']}」第 {round_no} 轮证据已提交：{safe}")
        kind = "证据替换" if prev else ("证据重新提交" if last and not last["approved"]
                                    else "证据提交")
        self._log(res["id"], "evidence", actor_id,
                  f"{kind}：{safe}（第 {round_no} 轮）" +
                  (f"，替换旧证据 {prev['filename']}" if prev else ""),
                  item_id, {"round": round_no, "filename": safe,
                            "replaced": prev["filename"] if prev else None})
        return {"id": eid, "stored_name": stored, "round": round_no,
                "replaced": prev["filename"] if prev else None}

    def evidence_history(self, item_id: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT e.*, u.name uploader_name FROM evidence e
               JOIN users u ON u.id=e.uploaded_by
               WHERE e.item_id=? ORDER BY e.created_at""", (item_id,)).fetchall()
        return [dict(r) for r in rows]

    def attachment_path(self, evidence_id: str) -> tuple[Path, str]:
        row = self.conn.execute("SELECT * FROM evidence WHERE id=?",
                                (evidence_id,)).fetchone()
        if not row:
            raise NotFound("证据")
        return self.attachments / row["stored_name"], row["filename"]

    # ---------------------------------------------------------------- 验收

    def submit_verdict(self, actor_id: str, item_id: str, approved: bool,
                       comment: str) -> dict:
        item = self._item(item_id)
        res = self._resolution(item["resolution_id"])
        self._require_editable(res)
        if actor_id not in self._verifier_ids(item_id):
            raise Forbidden("您不是该事项的验收人")
        if actor_id == item["assignee_id"]:
            raise Forbidden("执行人不能验收自己的工作")
        if not self.conn.execute(
                "SELECT 1 FROM evidence WHERE item_id=? AND round=? AND superseded_by IS NULL",
                (item_id, item["current_round"])).fetchone():
            raise Conflict("当前轮次还没有证据，无法验收")

        vid = _uid("v")
        round_no = item["current_round"]
        try:
            # UNIQUE(item_id, round)：两名验收人并发提交时只有一条成功
            self.conn.execute(
                """INSERT INTO verdicts (id,item_id,round,verifier_id,approved,
                     comment,created_at) VALUES (?,?,?,?,?,?,?)""",
                (vid, item_id, round_no, actor_id, 1 if approved else 0,
                 comment.strip(), _now()))
        except sqlite3.IntegrityError:
            existing = self.conn.execute(
                """SELECT v.*, u.name verifier_name FROM verdicts v
                   JOIN users u ON u.id=v.verifier_id
                   WHERE v.item_id=? AND v.round=?""", (item_id, round_no)).fetchone()
            raise Conflict("两名验收人同时提交，已发生冲突；该轮次已有验收结论，"
                           "请查看后再决定是否需要复议",
                           info=dict(existing) if existing else None)

        now_text = _now()
        if approved:
            self._close_todos(item_id, "executor", item["assignee_id"], round_no,
                              f"第 {round_no} 轮验收通过")
            self._close_todos(item_id, "verifier", actor_id, round_no,
                              f"第 {round_no} 轮验收通过")
            for v in self._verifier_ids(item_id):
                if v != actor_id:
                    self._close_todos(item_id, "verifier", v, round_no, "他人已完成验收")
        else:
            self._close_todos(item_id, "verifier", actor_id, round_no,
                              f"第 {round_no} 轮验收不通过")
            for v in self._verifier_ids(item_id):
                if v != actor_id:
                    self._close_todos(item_id, "verifier", v, round_no,
                                      f"第 {round_no} 轮已有验收结论")
        self._log(res["id"], "verdict", actor_id,
                  f"第 {round_no} 轮验收{'通过' if approved else '不通过'}"
                  f"（验收人 {self._user(actor_id)['name']}）"
                  f"：{comment.strip()[:80]}",
                  item_id, {"round": round_no, "approved": bool(approved),
                            "comment": comment.strip()})
        self.msg.send("executor", item["assignee_id"],
                      f"第 {round_no} 轮验收{'通过' if approved else '不通过'}",
                      f"「{item['content']}」" +
                      (f"验收意见：{comment.strip()}" if comment.strip() else "已通过验收"))
        return {"id": vid, "round": round_no, "approved": approved}

    # ---------------------------------------------------------------- 复议

    def request_review(self, actor_id: str, item_id: str, reason: str) -> str:
        item = self._item(item_id)
        res = self._resolution(item["resolution_id"])
        self._require_editable(res)
        if actor_id != item["assignee_id"]:
            raise Forbidden("只有责任人可以申请复议")
        last = self.conn.execute(
            "SELECT * FROM verdicts WHERE item_id=? ORDER BY created_at DESC LIMIT 1",
            (item_id,)).fetchone()
        if not last or last["approved"]:
            raise Conflict("只有验收不通过的事项可以申请复议")
        if self.conn.execute(
                "SELECT 1 FROM review_requests WHERE item_id=? AND status='pending'",
                (item_id,)).fetchone():
            raise Conflict("已有待裁定的复议申请")
        rid = _uid("rr")
        self.conn.execute(
            """INSERT INTO review_requests (id,item_id,requested_by,reason,status,
                 created_at) VALUES (?,?,?,?,'pending',?)""",
            (rid, item_id, actor_id, reason.strip(), _now()))
        self._log(res["id"], "review_request", actor_id,
                  f"责任人申请复议：{reason.strip()[:80]}", item_id,
                  {"reason": reason.strip()})
        host = self._meeting_host(res["meeting_id"])
        self.msg.send("host", host, "有验收复议待裁定",
                      f"「{item['content']}」责任人申请复议：{reason.strip()}")
        return rid

    def decide_review(self, actor_id: str, request_id: str, approve_appeal: bool,
                      note: str) -> None:
        """主持人裁定复议：支持则按复议通过记一轮新验收结论，驳回则维持不通过。"""
        row = self.conn.execute("SELECT * FROM review_requests WHERE id=?",
                                (request_id,)).fetchone()
        if not row:
            raise NotFound("复议申请")
        req = dict(row)
        if req["status"] != "pending":
            raise Conflict("该复议申请已经裁定")
        item = self._item(req["item_id"])
        res = self._resolution(item["resolution_id"])
        self._require_editable(res)
        if actor_id != self._meeting_host(res["meeting_id"]):
            raise Forbidden("只有主持人可以裁定复议")
        now = _now()
        self.conn.execute(
            """UPDATE review_requests SET status=?,decided_by=?,decision_note=?,
               decided_at=? WHERE id=?""",
            ("approved" if approve_appeal else "rejected",
             actor_id, note.strip(), now, request_id))
        if approve_appeal:
            # 复议通过：作为当前轮的复议结论单独入表，不覆盖原不通过记录
            self.conn.execute(
                """INSERT INTO verdicts (id,item_id,round,verifier_id,approved,
                     comment,created_at,is_appeal) VALUES (?,?,?,?,1,?,?,1)""",
                (_uid("v"), item["id"], item["current_round"], actor_id,
                 f"复议通过：{note.strip()}", now))
            self._close_todos(item["id"], "executor", item["assignee_id"],
                              item["current_round"], "复议通过")
            for v in self._verifier_ids(item["id"]):
                self._close_todos(item["id"], "verifier", v, item["current_round"],
                                  "复议通过")
        self._log(res["id"], "review_decision", actor_id,
                  f"复议裁定{'支持原执行结果，验收通过' if approve_appeal else '驳回，维持不通过'}"
                  f"：{note.strip()[:80]}", item["id"],
                  {"approve_appeal": approve_appeal, "note": note.strip()})
        self.msg.send("executor", item["assignee_id"], "复议裁定结果",
                      f"「{item['content']}」复议{'通过' if approve_appeal else '被驳回，维持不通过'}")

    # ---------------------------------------------------------------- 杂项

    def list_messages(self, user_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM messages"
        args: list = []
        if user_id:
            sql += " WHERE target=?"; args.append(user_id)
        sql += " ORDER BY created_at DESC LIMIT 100"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def search_archive(self, keyword: str) -> list[dict]:
        like = f"%{keyword}%"
        rows = self.conn.execute(
            """SELECT * FROM resolutions WHERE status='archived'
               AND (title LIKE ? OR code LIKE ? OR original_text LIKE ?)
               ORDER BY archived_at DESC""", (like, like, like)).fetchall()
        return [dict(r) for r in rows]
