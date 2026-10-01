"""种子数据：一条命令启动时自动载入。

覆盖场景
--------
* R-001 正常完成（多子项全部验收通过 → 汇总 done）
* R-002 验收不通过 → 执行人复议（第 2 轮证据待验收）
* R-003 改期 / 逾期（展示逾期天数 + 原因）
* R-004 两名验收人同时提交 → 冲突，待主持人裁定（用真正的并发线程制造）
* R-005 决议撤销 → 子项回收、待办消失
* R-006 已归档（已完成 + 只能补附注）
* R-007 秘书草稿（尚未经主持人确认分派）
* R-008 证据被替换（旧证据保留、证据链可溯）
* R-009 责任人离职 → 改派
"""

from __future__ import annotations

import threading
from datetime import date, timedelta

from .domain import ConflictError, DomainService
from .storage import Database, now_iso


PEOPLE = [
    ("王总", "总经办"), ("李秘书", "综合办"), ("张伟", "技术部"),
    ("孙莉", "技术部"), ("周强", "市场部"), ("吴敏", "财务部"),
    ("郑昊", "行政部"), ("冯磊", "采购部"), ("陈静", "人力资源部"),
    ("赵诚", "质检部"), ("钱进", "风控部"), ("蒋芳", "综合办"),
]

RAW_MINUTES = """三季度运营例会记录
时间：2026-09-15 14:00
主持：王总    记录：李秘书
出席：各部门负责人

一、王总通报三季度目标完成度，强调决议事项必须责任到人、限期验收。
二、技术部汇报官网改版与数据安全整改进展；安全整改材料被质检指出缺等保测评附件，
   要求补齐后进入复议。
三、行政部反映节能改造设备到货延迟，申请改期，会议同意并要求登记原因。
四、采购付款流程优化的验收出现质检与风控两边同时提交意见，主持人要求系统提示冲突后裁定。
五、外地办事处增设案因预算方向调整暂缓，原决议撤销，相关待办一律回收。
六、档案数字化一期已完成验收，准予归档，后续仅以附注方式补充说明。
"""

AGENDA = [
    ("三季度目标通报", "王总通报完成度；决议事项责任到人、限期验收、以原文核对。"),
    ("技术整改与复议", "官网改版收尾；数据安全整改缺等保测评附件，驳回后补证据复议。"),
    ("节能改造改期", "设备到货延迟，行政部申请改期并登记原因，逾期需显示天数与理由。"),
    ("验收冲突处理", "采购付款优化出现两名验收人同时提交，系统须提示冲突并由主持人裁定。"),
    ("撤销与归档", "外地办事处决议撤销、待办回收；档案数字化一期验收通过并归档。"),
]


def seed(db: Database) -> None:
    if db.query_one("SELECT COUNT(*) c FROM decisions")["c"] > 0:
        return  # 已初始化，幂等跳过

    svc = DomainService(db)
    today = date.today()
    d = lambda n: (today + timedelta(days=n)).isoformat()

    # ---- 人员 ----
    for name, dept in PEOPLE:
        db.execute("INSERT OR IGNORE INTO people(name,department) VALUES(?,?)", (name, dept))

    # ---- 会议 ----
    mid = svc.create_meeting("2026 年三季度运营例会", "2026-09-15", RAW_MINUTES)
    agenda_ids = [svc.add_agenda_item(mid, t, c) for t, c in AGENDA]

    # ============ R-001 正常完成 ============
    r1 = svc.create_decision(mid, "R-001",
        "9 月 30 日前完成官网改版上线，技术部负责开发，市场部负责发布公告；"
        "以页面可正常访问且公告可见为验收标准。", agenda_ids[0], secretary="李秘书")
    i1a = svc.add_action_item(r1, "技术部", "张伟", d(-30), "赵诚", actor="李秘书")
    i1b = svc.add_action_item(r1, "市场部", "周强", d(-28), "蒋芳", actor="李秘书")
    svc.confirm_and_dispatch(r1, host="王总")
    svc.start_work(i1a, "张伟")
    e1a = svc.submit_evidence(i1a, "张伟", "上线检查清单：首页/登录/支付链路全部 200。\n".encode(),
                              "上线检查清单.txt", "灰度发布后全量，监控 24h 无告警")
    svc.submit_approval(i1a, "赵诚", "approved", "按决议原文核对：页面可正常访问，通过")
    svc.start_work(i1b, "周强")
    svc.submit_evidence(i1b, "周强", "官网改版公告已在官网及公众号发布，截图见附件。\n".encode(),
                        "改版公告.txt", "公告已于 9 月 29 日发布")
    svc.submit_approval(i1b, "蒋芳", "approved", "公告可见，符合验收标准")

    # ============ R-002 驳回 → 复议 ============
    r2 = svc.create_decision(mid, "R-002",
        "10 月上旬完成客户数据安全整改：完成等保三级测评并提交报告，"
        "验收以「加盖机构章的等保测评报告」为准。", agenda_ids[1], secretary="李秘书")
    i2 = svc.add_action_item(r2, "技术部", "孙莉", d(3), "赵诚", actor="李秘书")
    svc.confirm_and_dispatch(r2, host="王总")
    svc.start_work(i2, "孙莉")
    svc.submit_evidence(i2, "孙莉", "第一轮：内部自查表与加固记录（非正式测评报告）。\n".encode(),
                        "安全自查表.txt", "先提交内部自查材料")
    svc.submit_approval(i2, "赵诚", "rejected",
                        "缺少加盖机构章的等保测评报告，与决议原文不符，发还复议")
    svc.submit_evidence(i2, "孙莉",
                        "第二轮（复议）：等保三级测评报告扫描件，机构章齐全。\n".encode(),
                        "等保三级测评报告.txt", "已补齐正式测评报告，申请第 2 轮验收")
    # 停在"待验收（第 2 轮）"，便于演示复议队列

    # ============ R-003 改期 / 逾期 ============
    r3 = svc.create_decision(mid, "R-003",
        "完成办公区节能改造（照明与空调分区控制），行政部负责，验收以设备安装调试记录为准。",
        agenda_ids[2], secretary="李秘书")
    i3a = svc.add_action_item(r3, "行政部", "郑昊", d(-20), "蒋芳", actor="李秘书")
    i3b = svc.add_action_item(r3, "行政部", "郑昊", d(-18), "赵诚", actor="李秘书")
    svc.confirm_and_dispatch(r3, host="王总")
    svc.start_work(i3a, "郑昊")
    # 改期后仍逾期：逾期天数与原因同时展示
    svc.reschedule_item(i3a, d(-5), "节能灯具供应商到货延迟，安装顺延", actor="郑昊")
    svc.reschedule_item(i3b, d(10), "空调控制面板需与物业联调，改期至十月中", actor="郑昊")

    # ============ R-004 并发验收冲突（真并发）============
    r4 = svc.create_decision(mid, "R-004",
        "优化供应商付款流程：实现三单匹配与审批留痕；质检部与风控部均可核验，"
        "结论一致方可通过。", agenda_ids[3], secretary="李秘书")
    i4 = svc.add_action_item(r4, "采购部", "冯磊", d(6), "赵诚", actor="李秘书")
    svc.confirm_and_dispatch(r4, host="王总")
    svc.start_work(i4, "冯磊")
    svc.submit_evidence(i4, "冯磊",
                        "三单匹配规则配置截图、审批流留痕导出与试点付款清单。\n".encode(),
                        "付款流程优化试点材料.txt", "已在 3 家供应商试点")
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def _accept(acceptor: str, verdict: str, comment: str):
        try:
            barrier.wait()
            svc.submit_approval(i4, acceptor, verdict, comment)
        except ConflictError:
            pass  # 预期：双方都会收到冲突信号
        except Exception as e:  # noqa
            errors.append(e)

    t1 = threading.Thread(target=_accept, args=("赵诚", "approved", "三单匹配与留痕齐备，通过"))
    t2 = threading.Thread(target=_accept, args=("钱进", "rejected",
                                                "试点样本仅 3 家，覆盖不足，需扩大试点"))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors, f"冲突案例构造失败: {errors}"

    # ============ R-005 撤销 → 待办回收 ============
    r5 = svc.create_decision(mid, "R-005",
        "在成都增设区域办事处，财务部核预算、人力部定编制。",
        agenda_ids[4], secretary="李秘书")
    i5a = svc.add_action_item(r5, "财务部", "吴敏", d(14), "蒋芳", actor="李秘书")
    i5b = svc.add_action_item(r5, "人力资源部", "陈静", d(14), "赵诚", actor="李秘书")
    svc.confirm_and_dispatch(r5, host="王总")
    svc.start_work(i5a, "吴敏")
    svc.revoke_decision(r5, "集团预算方向调整，本年度不再新设外地办事处", actor="王总")

    # ============ R-006 归档 + 附注 ============
    r6 = svc.create_decision(mid, "R-006",
        "完成历史档案数字化一期：2023 年前合同档案全部扫描入库，可检索。",
        agenda_ids[4], secretary="李秘书")
    i6 = svc.add_action_item(r6, "人力资源部", "陈静", d(-60), "赵诚", actor="李秘书")
    svc.confirm_and_dispatch(r6, host="王总")
    svc.start_work(i6, "陈静")
    svc.submit_evidence(i6, "陈静",
                        "扫描清单 1287 份，OCR 后入库，抽检可检索率 100%。\n".encode(),
                        "档案数字化验收清册.txt", "一期范围全部完成")
    svc.submit_approval(i6, "赵诚", "approved", "抽检合格，数量与检索均符合决议")
    svc.archive_decision(r6, actor="王总")
    svc.add_archive_note(r6, "李秘书",
                         "附注：二期范围（财务凭证）另立新决议跟进，本条仅作存档说明。")

    # ============ R-007 草稿（未分派）============
    r7 = svc.create_decision(mid, "R-007",
        "启动新办公区搬迁方案：行政部出平面方案、IT 部出网络割接计划，待主持人确认后分派。",
        agenda_ids[0], secretary="李秘书")
    svc.add_action_item(r7, "行政部", "郑昊", d(20), "蒋芳", actor="李秘书")
    svc.add_action_item(r7, "技术部", "张伟", d(20), "赵诚", actor="李秘书")
    # 不调用 confirm_and_dispatch —— 保持草稿

    # ============ R-008 证据被替换 ============
    r8 = svc.create_decision(mid, "R-008",
        "修订供应商招聘与背调流程，形成正式制度文件并发布。",
        agenda_ids[1], secretary="李秘书")
    i8 = svc.add_action_item(r8, "人力资源部", "陈静", d(8), "赵诚", actor="李秘书")
    svc.confirm_and_dispatch(r8, host="王总")
    svc.start_work(i8, "陈静")
    old = svc.submit_evidence(i8, "陈静", "初稿 V1（漏附背调授权书模板）。\n".encode(),
                              "招聘背调流程_V1.txt", "初稿，缺附件模板")
    svc.replace_evidence(old, "陈静",
                         "定稿 V2：补入背调授权书模板与异议处理条款，已套红头。\n".encode(),
                         "招聘背调流程_V2.txt", "缺少背调授权书模板，按法务意见补齐后替换")
    # 停在"已替换、待重新验收"

    # ============ R-009 责任人离职改派 ============
    r9 = svc.create_decision(mid, "R-009",
        "完成年度客户满意度回访并出具分析报告。",
        agenda_ids[0], secretary="李秘书")
    i9 = svc.add_action_item(r9, "市场部", "周强", d(12), "蒋芳", actor="李秘书")
    svc.confirm_and_dispatch(r9, host="王总")
    svc.reassign_item(i9, "吴敏", "原负责人周强调离市场部（离职交接），改派吴敏接手",
                      actor="王总")
