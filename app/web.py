"""HTTP 界面（标准库 http.server，零依赖）。

页面
----
/                 决议督办总览（责任人、期限、验收状态；逾期天数+原因）
/meeting?id=      会议记录（议题可折叠，含原始记录全文）
/decision?id=     决议详情（决议原文 ‖ 最新执行证据 并排；版本链/证据链/验收记录）
/todos            待办（按人筛选；撤销/已完成自动回收）
/review           验收 / 复议队列（按验收人筛选）
/archive          归档检索
/messages         模拟消息适配器 outbox

身份：顶部切换"当前操作人"（cookie: user）。执行人/验收人/主持人/秘书的越权校验
全部在领域层完成。
"""

from __future__ import annotations

import cgi
import html
import io
import os
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .domain import (
    DomainService, ArchivedError, ConflictError, DomainError,
    PermissionDenied, StateError,
)
from .storage import Database

STATUS_LABELS = {
    "draft": "草稿", "dispatched": "已分派", "pending": "待执行",
    "in_progress": "执行中", "submitted": "待验收", "in_review": "验收中",
    "rejected": "驳回待复议", "conflict": "验收冲突", "accepted": "已通过",
    "cancelled": "已回收", "done": "全部完成", "revoked": "已撤销",
    "archived": "已归档",
}
STATUS_CLASS = {
    "draft": "st-draft", "dispatched": "st-dispatched", "pending": "st-pending",
    "in_progress": "st-progress", "submitted": "st-progress", "in_review": "st-progress",
    "rejected": "st-reject", "conflict": "st-conflict", "accepted": "st-accept",
    "cancelled": "st-cancel", "done": "st-accept", "revoked": "st-cancel",
    "archived": "st-arch",
}

H = html.escape


def st_badge(status: str) -> str:
    cls = STATUS_CLASS.get(status, "st-draft")
    return f'<span class="badge {cls}">{H(STATUS_LABELS.get(status, status))}</span>'


# ---------------------------------------------------------------- 页面渲染

class Renderer:
    def __init__(self, svc: DomainService, current_user: str):
        self.svc = svc
        self.user = current_user

    def people(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.svc.db.query(
            "SELECT * FROM people ORDER BY id")]

    def layout(self, title: str, body: str, flash: str = "") -> str:
        names = "".join(
            f'<option value="{H(p["name"])}"{" selected" if p["name"]==self.user else ""}>'
            f'{H(p["name"])}（{H(p["department"])}）</option>'
            for p in self.people()
        )
        nav = [
            ("/", "决议总览"), ("/todos", "待办"), ("/review", "验收复议"),
            ("/meetings", "会议记录"), ("/archive", "归档检索"), ("/messages", "消息记录"),
        ]
        nav_html = "".join(
            f'<a href="{u}">{H(t)}</a>' for u, t in nav
        )
        flash_html = f'<div class="flash">{H(flash)}</div>' if flash else ""
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{H(title)} · 会议决议督办</title>
<style>{CSS}</style></head>
<body>
<header class="top">
  <div class="brand">◈ 会议决议督办</div>
  <nav>{nav_html}</nav>
  <form class="who" method="post" action="/switch-user">
    <label>当前操作人
      <select name="user" onchange="this.form.submit()">{names}</select>
    </label>
  </form>
</header>
<main class="wrap">
{flash_html}
{body}
</main>
<footer class="foot">白 · 炭灰 · 青绿 ｜ SQLite 存决议与版本，本地目录存附件，进程内提醒，模拟消息适配器</footer>
</body></html>"""

    # ------------------------------------------------------------ 总览
    def dashboard(self) -> str:
        decisions = self.svc.list_decisions()
        meetings = self.svc.list_meetings()
        mopt = "".join(
            f'<option value="{m["id"]}">{H(m["title"])}</option>' for m in meetings
        )
        rows = []
        for d in decisions:
            owners = "、".join(H(o) for o in d["owners"]) or "—"
            dues = "、".join(H(x) for x in d["due_dates"]) or "—"
            s = d["summary"]
            acc = (f'{s.accepted}/{s.active} 项通过'
                   + (f'，{s.conflict} 项冲突' if s.conflict else '')
                   + (f'，{s.rejected} 项驳回' if s.rejected else ''))
            rows.append(f"""<tr>
<td><a href="/decision?id={d['id']}"><b>{H(d['code'])}</b></a></td>
<td>{st_badge(d['status'])} {st_badge(d['rollup'])}</td>
<td class="w320">{H(d['original_text'])}</td>
<td>{owners}</td><td>{dues}</td>
<td>{H(acc)}</td></tr>""")
        body = f"""
<h1>决议总览</h1>
<p class="muted">汇总状态（验收进度）完全由各责任子项实时计算；撤销项不计入。</p>
<table class="grid">
<thead><tr><th>编号</th><th>状态 / 汇总</th><th>决议原文</th><th>责任人</th><th>完成期限</th><th>验收状态</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=6 class="empty">暂无决议</td></tr>'}</tbody>
</table>

<div class="cards">
<section class="card">
<h2>新建决议草稿（秘书整理）</h2>
<form method="post" action="/decision/new">
<label>会议<select name="meeting_id">{mopt}</select></label>
<label>决议编号<input name="code" placeholder="R-010" required></label>
<label>决议原文（验收核对基准，定稿后不可修改）
<textarea name="original_text" rows="3" required></textarea></label>
<button>秘书保存草稿</button>
</form>
</section>
</div>
"""
        return self.layout("决议总览", body)

    # ------------------------------------------------------------ 会议记录
    def meetings(self) -> str:
        cards = []
        for m0 in self.svc.list_meetings():
            m = self.svc.get_meeting(m0["id"])
            decisions = self.svc.list_decisions(m["id"])
            dlinks = "".join(
                f'<a class="chip" href="/decision?id={d["id"]}">{H(d["code"])} '
                f'{st_badge(d["rollup"])}</a>' for d in decisions
            )
            topics = "".join(
                f'<details class="topic"><summary>{H(a["topic"])}</summary>'
                f'<div class="topic-body">{H(a["content"]).replace(chr(10),"<br>")}</div></details>'
                for a in m["agenda"]
            ) or '<p class="muted">（无分议题）</p>'
            cards.append(f"""<section class="card meeting">
<h2>{H(m["title"])} <span class="muted">{H(m["held_on"])}</span>
{'<span class="badge st-arch">会议归档</span>' if m["archived"] else ""}</h2>
<details class="raw" open><summary>原始会议记录</summary>
<pre class="raw-minutes">{H(m["raw_minutes"])}</pre></details>
<h3>议题（点击折叠/展开）</h3>
{topics}
<p class="dlinks">形成决议：{dlinks or "无"}</p>
</section>""")
        return self.layout("会议记录", f"<h1>会议记录</h1>{''.join(cards)}")

    # ------------------------------------------------------------ 决议详情
    def decision_detail(self, did: int) -> str:
        d = self.svc.get_decision_detail(did)
        archived = d["status"] == "archived"
        # ---- 子项表 + 每子项证据/验收/版本
        item_blocks = []
        for it in d["items"]:
            ev_chain = self._evidence_chain_html(it)
            appr = self._approvals_html(it)
            versions = self._item_versions_html(it["id"])
            overdue = ""
            if it["overdue_days"] > 0 and it["status"] not in ("accepted", "cancelled"):
                overdue = (f'<span class="overdue">逾期 {it["overdue_days"]} 天'
                           f'（{H(it["overdue_reason"])}）</span>')
            actions = self._item_actions(d, it)
            item_blocks.append(f"""<div class="item">
<div class="item-head"><b>#{it["id"]} {H(it["department"])}</b>
 {st_badge(it["status"])} {overdue}
<span class="muted">负责人 {H(it["owner"])} ｜ 期限 {H(it["due_date"])} ｜ 验收人 {H(it["acceptor"])}</span></div>
<div class="split">
<div class="pane"><h4>执行证据</h4>{ev_chain}</div>
<div class="pane"><h4>验收 / 复议记录</h4>{appr}</div>
</div>
<details class="ver"><summary>子项版本链（v{it["current_version"]}）</summary>{versions}</details>
{actions}
</div>""")

        # ---- 并排：原文 ‖ 最新执行证据汇总
        latest_evs = []
        for it in d["items"]:
            if it["latest_evidence"]:
                ev = it["latest_evidence"]
                latest_evs.append(
                    f'<p><b>#{it["id"]} {H(it["owner"])}（第 {it["round"]} 轮）</b><br>'
                    f'📎 <a href="/attachment?id={ev["id"]}">{H(ev["original_name"])}</a>'
                    f'<span class="muted"> {H(ev["submitted_at"])}</span><br>'
                    f'{H(ev["note"])}</p>')
            else:
                latest_ef = f'<p class="muted">#{it["id"]} {H(it["owner"])}：尚未提交证据</p>'
                latest_evs.append(latest_ef)

        dec_versions = self._decision_versions_html(d)
        notes = self._archive_notes_html(d)

        # 顶部操作
        head_actions = []
        if d["status"] == "draft":
            head_actions.append(f"""<form method="post" action="/decision/{did}/dispatch" class="inline">
<button class="primary">主持人确认并分派</button></form>""")
        if not archived and not d["revoked"]:
            head_actions.append(f"""<details class="inline-dd"><summary>撤销决议</summary>
<form method="post" action="/decision/{did}/revoke" class="mini">
<input name="reason" placeholder="撤销原因（必填）" required>
<button class="danger">确认撤销（回收全部待办）</button></form></details>""")
        if d["status"] not in ("archived", "revoked", "draft"):
            head_actions.append(f"""<form method="post" action="/decision/{did}/archive" class="inline">
<button>归档（之后仅可补附注）</button></form>""")

        # 草稿期可加子项
        add_item = ""
        if d["status"] == "draft":
            people = self.people()
            opts = "".join(f'<option value="{H(p["name"])}">{H(p["name"])}（{H(p["department"])}）</option>' for p in people)
            add_item = f"""<section class="card"><h3>添加责任子项（同一决议可多项，分别跟踪）</h3>
<form method="post" action="/decision/{did}/item" class="gridform">
<label>责任部门<input name="department" required></label>
<label>负责人<select name="owner">{opts}</select></label>
<label>完成期限<input type="date" name="due_date" required></label>
<label>验收人<select name="acceptor">{opts}</select></label>
<div class="full"><button>添加子项</button></div></form>
<p class="muted">负责人与验收人不能为同一人（执行人不得验收自己的工作）。</p>
</section>"""

        body = f"""
<p><a href="/">← 返回总览</a> ｜ 来源会议：
<a href="/meeting?id={d["meeting_id"]}">{H(d["meeting"]["title"])}</a></p>
<h1>{H(d["code"])} {st_badge(d["status"])} {st_badge(d["rollup"])}
<span class="muted">v{d["current_version"]}</span></h1>
<div class="head-actions">{''.join(head_actions)}</div>

<div class="split sidebyside">
<section class="pane original"><h3>决议原文（核对基准，永不修改）</h3>
<pre>{H(d["original_text"])}</pre></section>
<section class="pane"><h3>最新执行证据（并排对照）</h3>{''.join(latest_evs)}</section>
</div>

<h3>责任子项（{d["summary"].total} 项，汇总：{H(STATUS_LABELS.get(d["rollup"], d["rollup"]))}）</h3>
{''.join(item_blocks) or '<p class="muted">暂无子项</p>'}
{add_item}
{notes}
<details class="ver"><summary>决议版本链</summary>{dec_versions}</details>
"""
        return self.layout(f"决议 {d['code']}", body)

    # -- 详情内片段 --
    def _evidence_chain_html(self, it: dict) -> str:
        chain = it["evidence_chain"]
        if not chain:
            return '<p class="muted">无证据</p>'
        out = []
        for i, ev in enumerate(chain, 1):
            replaced = ev["superseded_by"] is not None
            tag = '<span class="tag tag-old">已被替换</span>' if replaced else \
                  '<span class="tag tag-new">当前证据</span>'
            repl = f'<br><span class="muted">替换原因：{H(ev["replace_reason"])}</span>' if replaced else ""
            out.append(f"""<div class="ev {'old' if replaced else ''}">
<div>第 {i} 轮 {tag} 提交人 {H(ev["submitter"])} · {H(ev["submitted_at"])}</div>
📎 <a href="/attachment?id={ev["id"]}">{H(ev["original_name"])}</a>
<div class="muted">{H(ev["note"])}</div>{repl}</div>""")
        # 上传表单：当前证据可被新证据替换；执行人本人可见入口
        latest = it["latest_evidence_id"]
        upload = f"""
<details class="up"><summary>提交 / 替换证据（执行人 {H(it["owner"])}）</summary>
<form method="post" action="/item/{it["id"]}/evidence" enctype="multipart/form-data" class="mini">
<input type="hidden" name="replace_of" value="{latest or ''}">
<input type="file" name="file" required>
<input name="note" placeholder="过程说明（替换时须填原因）">
<button>上传证据到本地附件目录</button></form></details>"""
        return "".join(out) + upload

    def _approvals_html(self, it: dict) -> str:
        rows = it["approvals"]
        out = []
        for a in rows:
            v = "通过" if a["verdict"] == "approved" else \
                "驳回" if a["verdict"] == "rejected" else "并发冲突"
            cls = "ok" if a["verdict"] == "approved" else \
                  "bad" if a["verdict"] == "rejected" else "conf"
            out.append(f'<div class="ap {cls}">第 {a["round"]} 轮 · {H(a["acceptor"])} → {v}'
                       f'<span class="muted"> {H(a["decided_at"])}</span><br>{H(a["comment"])}</div>')
        form = ""
        if it["status"] in ("submitted", "rejected", "conflict"):
            form = f"""<form method="post" action="/item/{it["id"]}/approve" class="mini">
<div class="muted">验收人 {H(it["acceptor"])} 按决议原文核对
（执行人 {H(it["owner"])} 提交将被拒绝；与他人同轮提交将判冲突）</div>
<textarea name="comment" rows="2" placeholder="核对意见"></textarea>
<button name="verdict" value="approved" class="primary">核对通过</button>
<button name="verdict" value="rejected" class="danger">不通过（发还复议）</button></form>"""
            if it["status"] == "conflict":
                opts = "".join(
                    f'<option value="{a["id"]}">采纳 {H(a["acceptor"])} 的原始结论'
                    f'（{"通过" if a["original_verdict"]=="approved" else "不通过"}）</option>'
                    for a in rows if a["verdict"] == "conflict")
                form = f"""<div class="conflict-box">⚠ 两名验收人同轮并发提交，已挂起，等待主持人裁定。</div>
<form method="post" action="/item/{it["id"]}/resolve" class="mini">
<select name="winning_approval_id">{opts}</select>
<input name="note" placeholder="主持人说明（可选）">
<button class="primary">主持人裁定（按被采纳方原始结论生效）</button></form>""" + form
        return ("".join(out) or '<p class="muted">暂无验收记录</p>') + form

    def _item_actions(self, d: dict, it: dict) -> str:
        if d["status"] == "archived" or it["status"] in ("accepted", "cancelled"):
            return ""
        return f"""<details class="ver"><summary>改期 / 改派（责任人离职等，均留版本链）</summary>
<div class="split">
<form method="post" action="/item/{it["id"]}/reschedule" class="mini">
<input type="date" name="new_due" required>
<input name="reason" placeholder="改期原因（必填）" required>
<button>改期</button></form>
<form method="post" action="/item/{it["id"]}/reassign" class="mini">
<select name="new_owner">{''.join(f'<option value="{H(p["name"])}">{H(p["name"])}</option>' for p in self.people())}</select>
<input name="reason" placeholder="改派原因，如：原负责人离职" required>
<button>改派负责人</button></form>
</div></details>"""

    def _item_versions_html(self, item_id: int) -> str:
        vs = self.svc.item_versions(item_id)
        return self._version_rows(vs)

    def _decision_versions_html(self, d: dict) -> str:
        return self._version_rows(d["versions"])

    def _version_rows(self, vs: list[dict]) -> str:
        rows = "".join(
            f'<tr><td>v{v["version"]}</td><td>{H(v["change_type"])}</td>'
            f'<td>{H(v["reason"])}</td><td>{H(v["actor"])}</td>'
            f'<td>{H(v["changed_at"])}</td></tr>' for v in vs
        )
        return f'<table class="ver-table"><thead><tr><th>版本</th><th>类型</th><th>原因/内容</th><th>操作人</th><th>时间</th></tr></thead><tbody>{rows}</tbody></table>'

    def _archive_notes_html(self, d: dict) -> str:
        notes = "".join(
            f'<p>📝 {H(n["author"])} <span class="muted">{H(n["created_at"])}</span><br>{H(n["note"])}</p>'
            for n in d["archive_notes"]
        )
        form = ""
        if d["status"] == "archived":
            form = f"""<section class="card"><h3>归档补充附注（归档后唯一允许的变更）</h3>
<form method="post" action="/decision/{d["id"]}/note" class="mini">
<textarea name="note" rows="2" required placeholder="附注内容"></textarea>
<button>补充附注</button></form></section>"""
        return f'<h3>归档附注</h3>{notes or "<p class=muted>无</p>"}{form}'

    # ------------------------------------------------------------ 待办
    def todos(self, owner: str) -> str:
        people = self.people()
        opts = '<option value="">全部</option>' + "".join(
            f'<option value="{H(p["name"])}"{" selected" if p["name"]==owner else ""}>{H(p["name"])}</option>'
            for p in people)
        items = self.svc.todo_list(owner or None)
        rows = []
        for it in items:
            od = ""
            if it["overdue_days"] > 0:
                od = (f'<span class="overdue">逾期 {it["overdue_days"]} 天｜'
                      f'原因：{H(it["overdue_reason"])}</span>')
            rows.append(f"""<tr>
<td><a href="/decision?id={it["decision_id"]}">{H(it["decision_code"])}</a></td>
<td>{H(it["department"])}</td><td>{H(it["owner"])}</td>
<td>{H(it["due_date"])}</td><td>{st_badge(it["status"])}</td><td>{od}</td>
<td>{H(it["acceptor"])}</td></tr>""")
        body = f"""
<h1>待办</h1>
<form class="filter"><label>负责人
<select name="owner" onchange="location='?owner='+encodeURIComponent(this.value)">{opts}</select></label></form>
<p class="muted">已验收通过与决议撤销回收的子项不会出现在这里。</p>
<table class="grid"><thead><tr><th>决议</th><th>部门</th><th>负责人</th><th>期限</th>
<th>状态</th><th>逾期</th><th>验收人</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=7 class=empty>没有待办 🎉</td></tr>'}</tbody></table>"""
        return self.layout("待办", body)

    # ------------------------------------------------------------ 验收复议
    def review(self, acceptor: str) -> str:
        opts = '<option value="">全部</option>' + "".join(
            f'<option value="{H(p["name"])}"{" selected" if p["name"]==acceptor else ""}>{H(p["name"])}</option>'
            for p in self.people())
        items = self.svc.review_queue(acceptor or None)
        cards = []
        for it in items:
            ev = it["latest_evidence"]
            cards.append(f"""<div class="card rv">
<h3>{H(it["decision_code"])} 子项#{it["id"]} {st_badge(it["status"])}
<span class="muted">第 {it["round"]} 轮</span></h3>
<p class="muted">{H(it["decision_text"])}</p>
<p>负责人 {H(it["owner"])} ｜ 验收人 {H(it["acceptor"])} ｜ 期限 {H(it["due_date"])}</p>
<p>最新证据：{'<a href="/attachment?id='+str(ev["id"])+'">'+H(ev["original_name"])+"</a> — "+H(ev["note"]) if ev else "无"}</p>
<a class="btn" href="/decision?id={it["decision_id"]}">进入核对 / 复议</a>
</div>""")
        body = f"""<h1>验收 · 复议队列</h1>
<form class="filter"><label>验收人
<select name="acceptor" onchange="location='?acceptor='+encodeURIComponent(this.value)">{opts}</select></label></form>
<div class="cards">{''.join(cards) or '<p class="empty">队列已清空</p>'}</div>"""
        return self.layout("验收复议", body)

    # ------------------------------------------------------------ 归档检索
    def archive(self, q: str) -> str:
        items = self.svc.search_archive(q)
        cards = []
        for d in items:
            notes = "".join(f'<p class="muted">📝 {H(n["author"])}：{H(n["note"])}</p>'
                            for n in d["archive_notes"])
            cards.append(f"""<details class="card arc"><summary>
<b>{H(d["code"])}</b> {st_badge(d["status"])} {H(d["original_text"][:40])}…
<span class="muted">归档于 {H(d["archived_at"] or "")}</span></summary>
<p>{H(d["original_text"])}</p>{notes}
<a class="btn" href="/decision?id={d["id"]}">查看详情（仅可补附注）</a></details>""")
        body = f"""<h1>归档检索</h1>
<form class="filter"><input name="q" value="{H(q)}" placeholder="编号 / 原文 / 部门 / 负责人 / 原因">
<button>检索</button></form>
{''.join(cards) or '<p class="empty">无匹配归档</p>'}"""
        return self.layout("归档检索", body)

    # ------------------------------------------------------------ 消息
    def messages(self) -> str:
        rows = "".join(
            f'<tr><td>{H(m["created_at"])}</td><td><span class="chip">{H(m["channel"])}</span></td>'
            f'<td>{H(m["to_name"])}</td><td>{H(m["subject"])}</td><td>{H(m["body"])}</td></tr>'
            for m in self.svc.list_messages())
        body = f"""<h1>模拟消息适配器（发件箱）</h1>
<p class="muted">分派、改期、改派、撤销、待验收、验收冲突、逾期提醒均经此适配器返回，不真正外发。</p>
<table class="grid"><thead><tr><th>时间</th><th>通道</th><th>接收人</th><th>主题</th><th>内容</th></tr></thead>
<tbody>{rows or '<tr><td colspan=5 class=empty>暂无消息</td></tr>'}</tbody></table>"""
        return self.layout("消息记录", body)


# ---------------------------------------------------------------- 样式

CSS = """
:root{
 --white:#ffffff; --paper:#f7f8f9; --char:#2b2f33; --char2:#55606a;
 --line:#e3e7ea; --teal:#0e9488; --teal-d:#0b7a70; --teal-bg:#e6f5f3;
 --amber:#b7791f; --red:#c0392b; --red-bg:#fdecea; --green:#1e7d4f; --gray:#8a949c;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--char);
 font:15px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
a{color:var(--teal-d);text-decoration:none} a:hover{text-decoration:underline}
h1{font-size:22px;margin:6px 0 12px} h2{font-size:17px} h3{font-size:15px;margin:14px 0 6px}
h4{margin:2px 0 8px;color:var(--char2)}
.top{display:flex;align-items:center;gap:22px;background:var(--char);color:#fff;
 padding:10px 20px;flex-wrap:wrap}
.brand{font-weight:700;color:#fff;letter-spacing:.5px}
.top nav{display:flex;gap:14px;flex-wrap:wrap}
.top nav a{color:#d7dee2} .top nav a:hover{color:var(--teal);text-decoration:none}
.who{margin-left:auto}.who select{padding:4px 8px;border-radius:6px;border:1px solid var(--line)}
.wrap{max-width:1180px;margin:22px auto;padding:0 20px}
.foot{text-align:center;color:var(--gray);font-size:12px;padding:26px}
.muted{color:var(--char2);font-size:13px}.w320{max-width:340px}
.flash{background:var(--teal-bg);border:1px solid var(--teal);color:var(--teal-d);
 padding:10px 14px;border-radius:8px;margin-bottom:14px}
.grid{width:100%;border-collapse:collapse;background:var(--white);
 border:1px solid var(--line);border-radius:10px;overflow:hidden}
.grid th,.grid td{padding:9px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.grid th{background:#f0f3f4;font-weight:600;font-size:13px;color:var(--char2)}
.grid tr:last-child td{border-bottom:0}
.empty{text-align:center;color:var(--gray);padding:22px}
.badge{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px;white-space:nowrap}
.st-draft{background:#eceff1;color:#60707b}.st-dispatched{background:#e3edf7;color:#2f6491}
.st-pending{background:#eceff1;color:#60707b}.st-progress{background:var(--teal-bg);color:var(--teal-d)}
.st-accept{background:#e4f4ec;color:var(--green)}.st-reject{background:var(--red-bg);color:var(--red)}
.st-conflict{background:#fff3e0;color:var(--amber)}.st-cancel{background:#eceff1;color:var(--gray)}
.st-arch{background:#efece7;color:#7a6a52}
.overdue{color:var(--red);font-weight:600;font-size:13px;
 background:var(--red-bg);padding:1px 8px;border-radius:6px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px;margin-top:16px}
.card{background:var(--white);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card h2{margin-top:2px}
.split{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:10px 0}
@media(max-width:820px){.split{grid-template-columns:1fr}}
.pane{background:var(--white);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.sidebyside pre{white-space:pre-wrap;margin:0;font-family:inherit;background:var(--paper);
 padding:10px;border-radius:8px;border:1px dashed var(--line)}
.original{border-left:4px solid var(--teal)}
.item{background:var(--white);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:12px 0}
.item-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
.ev{padding:8px 10px;border:1px solid var(--line);border-radius:8px;margin-bottom:8px;background:#fcfdfd}
.ev.old{opacity:.62;background:#f4f5f6}
.tag{font-size:11px;padding:0 7px;border-radius:99px}
.tag-new{background:var(--teal-bg);color:var(--teal-d)}.tag-old{background:#eceff1;color:var(--gray)}
.ap{padding:7px 10px;border-radius:8px;margin-bottom:7px;font-size:13px}
.ap.ok{background:#e4f4ec}.ap.bad{background:var(--red-bg)}.ap.conf{background:#fff3e0}
.conflict-box{background:#fff3e0;border:1px solid #e0b878;color:var(--amber);
 padding:8px 10px;border-radius:8px;margin-bottom:8px;font-weight:600}
label{display:block;font-size:13px;color:var(--char2);margin:8px 0}
input,select,textarea{width:100%;padding:7px 9px;margin-top:3px;border:1px solid var(--line);
 border-radius:7px;font:inherit;background:#fff;color:var(--char)}
textarea{resize:vertical}
button,.btn{display:inline-block;width:auto;margin-top:8px;padding:7px 16px;border-radius:7px;
 border:1px solid var(--teal);background:var(--teal);color:#fff;cursor:pointer;font:inherit}
button:hover,.btn:hover{background:var(--teal-d);text-decoration:none}
button.primary{background:var(--teal-d)} button.danger{border-color:var(--red);background:var(--red)}
.inline{display:inline-block}.inline-dd{display:inline-block;margin-left:8px}
.inline-dd summary,.ver summary,.topic summary,.raw summary,.arc summary,.up summary{
 cursor:pointer;color:var(--teal-d);font-weight:600;outline:none}
.mini{background:var(--paper);border:1px dashed var(--line);border-radius:8px;padding:10px;margin-top:8px}
.gridform{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.gridform .full{grid-column:1/-1}
@media(max-width:820px){.gridform{grid-template-columns:1fr 1fr}}
.head-actions{margin:6px 0 12px}
.topic{border:1px solid var(--line);border-radius:8px;margin:8px 0;background:#fcfdfd}
.topic summary{padding:9px 12px;color:var(--char)}.topic-body{padding:0 12px 10px;color:var(--char2)}
.raw-minutes{white-space:pre-wrap;font-family:inherit;background:var(--paper);padding:10px;
 border-radius:8px;border:1px solid var(--line)}
.ver{margin-top:10px}.ver-table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
.ver-table th,.ver-table td{border:1px solid var(--line);padding:5px 8px;text-align:left}
.ver-table th{background:#f0f3f4}
.chip{display:inline-block;background:var(--teal-bg);color:var(--teal-d);border-radius:6px;
 padding:1px 8px;margin:2px;font-size:12px}
.dlinks{margin-top:10px}.filter{display:flex;gap:10px;align-items:end;margin-bottom:10px}
.filter input{width:300px}.filter button{margin:0}.filter select{width:160px}
.up{margin-top:8px}
"""


# ---------------------------------------------------------------- HTTP handler

class Handler(BaseHTTPRequestHandler):
    svc: DomainService = None  # 由 server 注入

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    # -- 工具 --
    def _cookie_user(self) -> str:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        if "user" in cookie:
            return urllib.parse.unquote(cookie["user"].value)
        # 默认第一个人
        row = self.svc.db.query_one("SELECT name FROM people ORDER BY id LIMIT 1")
        return row["name"] if row else "秘书"

    def _renderer(self) -> Renderer:
        return Renderer(self.svc, self._cookie_user())

    def _send(self, content: str, status: int = 200, ctype: str = "text/html; charset=utf-8",
              extra_headers: Optional[list[tuple[str, str]]] = None):
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra_headers or []:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str, flash: str = ""):
        loc = path + (("&" if "?" in path else "?") + "flash=" + urllib.parse.quote(flash)
                      if flash else "")
        self.send_response(303)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _flash(self) -> str:
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("flash", [""])[0]

    def _form(self) -> dict[str, Any]:
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" in ctype:
            env = {"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype}
            length = int(self.headers.get("Content-Length", 0))
            fs = cgi.FieldStorage(fp=io.BytesIO(self.rfile.read(length)),
                                  headers=self.headers, environ=env, keep_blank_values=True)
            out: dict[str, Any] = {}
            for key in fs.keys():
                item = fs[key]
                if getattr(item, "filename", None):
                    out[key] = item  # 文件字段保留 FieldStorage
                else:
                    out[key] = item.value
            return out
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _file_bytes(self, field: Any) -> tuple[bytes, str]:
        return field.file.read(), os.path.basename(field.filename or "attachment.bin")

    # -- GET --
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        r = self._renderer()
        try:
            if u.path == "/":
                self._send(r.layout("决议总览", r.dashboard(), self._flash()))
            elif u.path == "/meetings" or u.path == "/meeting":
                if "id" in q:
                    mid = int(q["id"][0])
                    m = r.svc.get_meeting(mid)
                    body = f'<p><a href="/meetings">← 全部会议</a></p><h1>{H(m["title"])}</h1>'
                    body += f'<p class="muted">{H(m["held_on"])}</p>'
                    body += "".join(
                        f'<details class="card topic" open><summary>{H(a["topic"])}</summary>'
                        f'<div class="topic-body">{H(a["content"]).replace(chr(10),"<br>")}</div></details>'
                        for a in m["agenda"])
                    body += f'<details class="card raw"><summary>原始会议记录全文</summary><pre class="raw-minutes">{H(m["raw_minutes"])}</pre></details>'
                    self._send(r.layout(m["title"], body))
                else:
                    self._send(r.meetings())
            elif u.path == "/decision":
                self._send(r.decision_detail(int(q["id"][0])))
            elif u.path == "/todos":
                self._send(r.todos(q.get("owner", [""])[0]))
            elif u.path == "/review":
                self._send(r.review(q.get("acceptor", [""])[0]))
            elif u.path == "/archive":
                self._send(r.archive(q.get("q", [""])[0]))
            elif u.path == "/messages":
                self._send(r.messages())
            elif u.path == "/attachment":
                ev = self.svc.db.query_one(
                    "SELECT * FROM evidence WHERE id=?", (int(q["id"][0]),))
                if not ev:
                    self._send("证据不存在", 404, "text/plain; charset=utf-8")
                    return
                path = self.svc.db.attachment_path(ev["filename"])
                with open(path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition",
                                 f'attachment; filename*=UTF-8\'\'{urllib.parse.quote(ev["original_name"])}')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send("404 未找到", 404, "text/plain; charset=utf-8")
        except (StateError, DomainError) as e:
            self._send(r.layout("出错", f'<div class="flash">{H(str(e))}</div>', str(e)), 400)

    # -- POST --
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path.strip("/").split("/")
        form = self._form()
        user = self._cookie_user()
        back = self.headers.get("Referer", "/")
        try:
            # 身份切换
            if u.path == "/switch-user":
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", f'user={urllib.parse.quote(form["user"])}; Path=/')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if u.path == "/decision/new":
                did = self.svc.create_decision(
                    int(form["meeting_id"]), form["code"].strip(),
                    form["original_text"].strip(), secretary=user)
                self._redirect(f"/decision?id={did}", "草稿已保存，可添加责任子项后交主持人确认分派")
                return

            if len(p) == 3 and p[0] == "decision" and p[2] == "item":
                did = int(p[1])
                self.svc.add_action_item(did, form["department"].strip(), form["owner"],
                                         form["due_date"], form["acceptor"], actor=user)
                self._redirect(f"/decision?id={did}", "责任子项已加入草稿")
                return

            if len(p) == 3 and p[0] == "decision" and p[2] == "dispatch":
                did = int(p[1])
                self.svc.confirm_and_dispatch(did, host=user)
                self._redirect(f"/decision?id={did}", "主持人已确认分派，责任人与验收人已收到通知")
                return

            if len(p) == 3 and p[0] == "decision" and p[2] == "revoke":
                did = int(p[1])
                self.svc.revoke_decision(did, form["reason"].strip(), actor=user)
                self._redirect(f"/decision?id={did}", "决议已撤销，名下待办已回收并通知")
                return

            if len(p) == 3 and p[0] == "decision" and p[2] == "archive":
                did = int(p[1])
                self.svc.archive_decision(did, actor=user)
                self._redirect(f"/decision?id={did}", "决议已归档，此后仅允许补充附注")
                return

            if len(p) == 3 and p[0] == "decision" and p[2] == "note":
                did = int(p[1])
                self.svc.add_archive_note(did, user, form["note"].strip())
                self._redirect(f"/decision?id={did}", "附注已补充")
                return

            if len(p) == 3 and p[0] == "item" and p[2] in ("evidence",):
                iid = int(p[1])
                data, name = self._file_bytes(form["file"])
                replace_of = form.get("replace_of", "")
                note = form.get("note", "")
                if replace_of:
                    self.svc.replace_evidence(int(replace_of), user, data, name, note)
                    flash = "证据已替换，旧证据保留于证据链，已通知重新验收"
                else:
                    self.svc.submit_evidence(iid, user, data, name, note)
                    flash = "证据已提交，等待验收人按决议原文核对"
                it = self.svc.db.query_one("SELECT decision_id FROM action_items WHERE id=?", (iid,))
                self._redirect(f"/decision?id={it['decision_id']}", flash)
                return

            if len(p) == 3 and p[0] == "item" and p[2] == "approve":
                iid = int(p[1])
                self.svc.submit_approval(iid, user, form["verdict"], form.get("comment", ""))
                it = self.svc.db.query_one("SELECT decision_id FROM action_items WHERE id=?", (iid,))
                self._redirect(f"/decision?id={it['decision_id']}",
                               "验收通过" if form["verdict"] == "approved" else "已驳回，等待执行人复议")
                return

            if len(p) == 3 and p[0] == "item" and p[2] == "resolve":
                iid = int(p[1])
                self.svc.resolve_conflict(iid, int(form["winning_approval_id"]),
                                          host=user, note=form.get("note", ""))
                it = self.svc.db.query_one("SELECT decision_id FROM action_items WHERE id=?", (iid,))
                self._redirect(f"/decision?id={it['decision_id']}", "主持人裁定已生效")
                return

            if len(p) == 3 and p[0] == "item" and p[2] == "reschedule":
                iid = int(p[1])
                self.svc.reschedule_item(iid, form["new_due"], form["reason"].strip(), user)
                it = self.svc.db.query_one("SELECT decision_id FROM action_items WHERE id=?", (iid,))
                self._redirect(f"/decision?id={it['decision_id']}", "改期已记录（版本链）")
                return

            if len(p) == 3 and p[0] == "item" and p[2] == "reassign":
                iid = int(p[1])
                self.svc.reassign_item(iid, form["new_owner"], form["reason"].strip(), user)
                it = self.svc.db.query_one("SELECT decision_id FROM action_items WHERE id=?", (iid,))
                self._redirect(f"/decision?id={it['decision_id']}", "改派已记录（版本链）")
                return

            self._send("404 未找到", 404, "text/plain; charset=utf-8")

        except ConflictError as e:
            self._redirect(back, f"⚠ 验收冲突：{e}（已通知主持人裁定）")
        except PermissionDenied as e:
            self._redirect(back, f"⛔ 越权被拦截：{e}")
        except ArchivedError as e:
            self._redirect(back, f"📦 归档限制：{e}")
        except (DomainError, StateError) as e:
            self._redirect(back, str(e))


def make_server(db: Database, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    from .domain import MessageAdapter
    svc = DomainService(db, MessageAdapter(db))
    Handler.svc = svc
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.svc = svc
    return httpd
