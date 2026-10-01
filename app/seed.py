"""演示数据：启动时若库为空则载入。

覆盖案例：逾期（含改期原因）、会后改期、责任人离职改派、验收不通过与复议中、
两名验收人（可现场触发并发冲突）、撤销后待办回收、已归档（含归档后附注）、未确认草稿。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .db import connect, init_db
from .messages import MessageAdapter
from .service import Service

USERS = [
    ("u_zhao", "赵铮", "综合办", 1),
    ("u_qian", "钱颖", "综合办", 1),
    ("u_sun", "孙磊", "研发部", 1),
    ("u_li", "李岚", "市场部", 1),
    ("u_zhou", "周凯", "运营部", 0),   # 已离职
    ("u_wu", "吴桐", "运营部", 1),
    ("u_zheng", "郑彻", "质量部", 1),
    ("u_feng", "冯杉", "审计部", 1),
    ("u_wang", "王鉴", "综合办", 1),
]


def seed_if_empty(db_path: str, attachments_dir: str) -> bool:
    conn = connect(db_path)
    init_db(conn)
    if conn.execute("SELECT COUNT(*) c FROM meetings").fetchone()["c"]:
        conn.close()
        return False
    svc = Service(conn, Path(attachments_dir), MessageAdapter(conn))
    cur = conn.cursor()

    for uid, name, dept, active in USERS:
        cur.execute("INSERT INTO users (id,name,department,active) VALUES (?,?,?,?)",
                    (uid, name, dept, active))

    def meeting(mid, title, held, location, secretary, host):
        cur.execute(
            """INSERT INTO meetings (id,title,held_at,location,secretary_id,host_id,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (mid, title, held, location, secretary, host, held + ":00"))

    def topic(tid, mid, order_no, title, body):
        cur.execute("INSERT INTO topics (id,meeting_id,order_no,title,body) VALUES (?,?,?,?,?)",
                    (tid, mid, order_no, title, body))

    meeting("m_2026q3", "2026年第三季度经营例会", "2026-09-20T14:00",
            "公司三楼第一会议室", "u_qian", "u_zhao")
    topic("t_report", "m_2026q3", 1, "议题一：智能报表模块升级",
          "会议听取了研发部关于智能报表模块升级进度的汇报，"
          "要求在三季度内完成联调上线，并同步完成市场材料更新。")
    topic("t_ops", "m_2026q3", 2, "议题二：华东区运营负责人调整",
          "会议讨论了华东区运营工作交接安排。")
    topic("t_misc", "m_2026q3", 3, "议题三：其他事项",
          "秘书就决议跟踪方式作了说明。")

    meeting("m_2026q2", "2026年第二季度经营例会", "2026-06-18T14:00",
            "公司三楼第一会议室", "u_qian", "u_zhao")
    topic("t_budget", "m_2026q2", 1, "议题：秋季市场活动预算",
           "会议审议并通过了秋季市场活动预算安排，要求按决议执行并归档存查。")

    # ---------------- R1：多责任、多状态的进行中决议 ----------------
    r1 = svc.create_draft("u_qian", {
        "meeting_id": "m_2026q3", "topic_id": "t_report",
        "code": "JZ-2026-009",
        "title": "关于完成智能报表模块升级及配套材料更新的决议",
        "original_text": (
            "一、研发部于9月30日前完成智能报表模块联调并上线；\n"
            "二、研发部完成上线后操作手册更新并交质量部验收；\n"
            "三、市场部于9月25日前提交渠道投放明细及供应商素材清单；\n"
            "四、市场部完成秋季宣传页定稿并归档；\n"
            "五、运营部完成华东区客户数据交接。\n"
            "各项须留痕，验收人按本决议原文核对。"),
        "items": [
            {"content": "完成智能报表模块联调并上线", "department": "研发部",
             "assignee_id": "u_sun", "due_date": "2026-09-20",
             "verifier_ids": ["u_zheng"]},
            {"content": "更新上线操作手册并提交验收", "department": "研发部",
             "assignee_id": "u_sun", "due_date": "2026-10-20",
             "verifier_ids": ["u_zheng", "u_feng"]},
            {"content": "提交渠道投放明细及供应商素材清单", "department": "市场部",
             "assignee_id": "u_li", "due_date": "2026-09-25",
             "verifier_ids": ["u_zheng", "u_wang"]},
            {"content": "完成秋季宣传页定稿并归档", "department": "市场部",
             "assignee_id": "u_li", "due_date": "2026-08-20",
             "verifier_ids": ["u_wang"]},
            {"content": "完成华东区客户数据交接", "department": "运营部",
             "assignee_id": "u_zhou", "due_date": "2026-10-20",
             "verifier_ids": ["u_zheng"]},
        ],
    })
    svc.confirm("u_zhao", r1)
    items = {i["content"]: i["id"] for i in svc._items(r1)}

    # 案例1：改期后仍逾期（页面显示逾期天数与改期原因）
    svc.reschedule("u_zhao", items["完成智能报表模块联调并上线"],
                   "2026-09-28", "第三方数据接口联调比计划多出两轮，经主持人同意顺延")

    # 案例2：已提交证据，两名验收人，可现场演示并发冲突
    svc.upload_evidence(
        "u_sun", items["更新上线操作手册并提交验收"], "操作手册_v1.pdf",
        ("[模拟附件] 智能报表操作手册 v1，含上线步骤与回滚方案。").encode("utf-8"),
        "手册初稿，请验收")

    # 案例3：验收不通过 → 改期（延期）→ 责任人申请复议（复议中）
    ev3 = svc.upload_evidence(
        "u_li", items["提交渠道投放明细及供应商素材清单"], "渠道投放明细.xlsx",
        ("[模拟附件] 渠道投放明细初稿。").encode("utf-8"), "首轮提交")
    svc.submit_verdict(
        "u_wang", items["提交渠道投放明细及供应商素材清单"], False,
        "与决议原文不符：缺少供应商素材清单，渠道投放明细缺少金额列，请补充后重新提交")
    svc.reschedule("u_zhao", items["提交渠道投放明细及供应商素材清单"],
                   "2026-10-15", "供应商素材延期交付，附供应商延期说明")
    svc.request_review("u_li", items["提交渠道投放明细及供应商素材清单"],
                       "明细已覆盖全部在册渠道，供应商素材另案随附，申请复议维持本轮提交有效")

    # 案例4：证据 → 验收通过
    svc.upload_evidence(
        "u_li", items["完成秋季宣传页定稿并归档"], "秋季宣传页定稿.pdf",
        ("[模拟附件] 秋季宣传页终稿（已归档编号 MKT-2026-031）。").encode("utf-8"),
        "终稿 PDF 与归档凭证")
    svc.submit_verdict(
        "u_wang", items["完成秋季宣传页定稿并归档"], True,
        "与决议原文一致，宣传页已定稿归档，通过")

    # 案例5：责任人离职改派（旧待办回收、新待办开启，版本链保留）
    svc.reassign("u_zhao", items["完成华东区客户数据交接"], "u_wu",
                 "责任人周凯离职，工作交接给吴桐")

    # ---------------- R2：已撤销（待办全部回收） ----------------
    r2 = svc.create_draft("u_qian", {
        "meeting_id": "m_2026q3", "topic_id": "t_ops",
        "code": "JZ-2026-010",
        "title": "关于调整华东区运营负责人的决议",
        "original_text": "任命周凯为华东区运营负责人，于10月15日前完成交接并上任。",
        "items": [
            {"content": "完成华东区运营工作交接并上任", "department": "运营部",
             "assignee_id": "u_zhou", "due_date": "2026-10-15",
             "verifier_ids": ["u_zheng"]},
        ],
    })
    svc.confirm("u_zhao", r2)
    item2 = svc._items(r2)[0]["id"]
    svc.upload_evidence("u_zhou", item2, "交接计划.docx",
                        ("[模拟附件] 交接计划草案。").encode("utf-8"), "交接安排")
    svc.revoke("u_zhao", r2, "与集团新发布的区域人事管理规定冲突，停止执行")

    # ---------------- R3：已归档（全部通过 + 归档后附注） ----------------
    r3 = svc.create_draft("u_qian", {
        "meeting_id": "m_2026q2", "topic_id": "t_budget",
        "code": "JZ-2026-006",
        "title": "关于秋季市场活动预算的决议",
        "original_text": (
            "一、批准秋季市场活动预算 80 万元，由市场部按渠道分解执行；\n"
            "二、财务部于7月10日前完成预算科目建账。"),
        "items": [
            {"content": "按渠道分解并执行80万元活动预算", "department": "市场部",
             "assignee_id": "u_li", "due_date": "2026-07-05",
             "verifier_ids": ["u_feng"]},
            {"content": "完成活动预算科目建账", "department": "综合办",
             "assignee_id": "u_wang", "due_date": "2026-07-10",
             "verifier_ids": ["u_feng"]},
        ],
    })
    svc.confirm("u_zhao", r3)
    its = svc._items(r3)
    svc.upload_evidence("u_li", its[0]["id"], "预算分解表.xlsx",
                        ("[模拟附件] 渠道预算分解及执行台账。").encode("utf-8"), "预算分解完成")
    svc.submit_verdict("u_feng", its[0]["id"], True, "科目与金额符合决议原文，通过")
    svc.upload_evidence("u_wang", its[1]["id"], "建账凭证.pdf",
                        ("[模拟附件] 财务系统建账截图与凭证号。").encode("utf-8"), "已建账")
    svc.submit_verdict("u_feng", its[1]["id"], True, "建账完成，通过")
    svc.archive("u_zhao", r3, "全部事项验收通过，材料完整，归档存查")
    # 归档后只允许补充附注
    svc.add_note("u_qian", r3, "归档后补充：财务凭证编号 V-20260702 已在审计抽查后补齐扫描件。")

    # ---------------- R4：草稿（主持人尚未确认分派） ----------------
    svc.create_draft("u_qian", {
        "meeting_id": "m_2026q3", "topic_id": "t_misc",
        "code": "JZ-2026-011",
        "title": "关于启用决议执行线上跟踪的决议（草案）",
        "original_text": "自10月起，各类会议通过决议均使用本系统登记、分派、留痕与归档。",
        "items": [
            {"content": "制定系统使用规范并全员通知", "department": "综合办",
             "assignee_id": "u_wang", "due_date": "2026-10-25",
             "verifier_ids": ["u_zhao"]},
        ],
    })

    conn.close()
    return True
