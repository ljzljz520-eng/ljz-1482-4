# -*- coding: utf-8 -*-
"""HTTP API：路由、鉴权(X-User-Id)、权限矩阵、乐观并发(409)、审计。仅标准库。"""
import json
import os
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import domain as D
from .db import connect, begin_immediate, row, rows

ROLE_LEVEL = {"viewer": 0, "editor": 1, "admin": 2}
ROUTES = []


def route(method, pattern, perm):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, perm, fn))
        return fn
    return deco


class Ctx:
    def __init__(self, conn, user, membership, query):
        self.conn = conn
        self.user = user
        self.membership = membership
        self.query = query
        self.body = {}

    def require(self, perm):
        need = {"read": 0, "edit": 1, "admin": 2}[perm]
        if self.membership is None or not self.membership["active"]:
            raise D.Forbidden("不是该工作区的有效成员")
        if ROLE_LEVEL[self.membership["role"]] < need:
            raise D.Forbidden(
                f"需要 {perm} 权限，当前角色 {self.membership['role']}")

    def actor(self):
        return {"id": self.user["id"], "name": self.user["name"]}


def body_of(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise D.BadRequest("请求体不是合法 JSON")


def course_of(conn, cid):
    r = row(conn.execute("SELECT * FROM courses WHERE id=?", (cid,)))
    if r is None:
        raise D.NotFound(f"课程 {cid} 不存在")
    return r


def ws_of_course(conn, cid):
    return course_of(conn, cid)["workspace_id"]


def chapter_course(conn, chid):
    r = row(conn.execute("SELECT * FROM chapters WHERE id=?", (chid,)))
    if r is None:
        raise D.NotFound(f"章节 {chid} 不存在")
    return r


def segment_course(conn, sid):
    r = row(conn.execute("SELECT * FROM segments WHERE id=?", (sid,)))
    if r is None:
        raise D.NotFound(f"片段 {sid} 不存在")
    return r


def membership_for(conn, ws_id, uid):
    return row(conn.execute(
        "SELECT * FROM memberships WHERE workspace_id=? AND user_id=?", (ws_id, uid)))


def list_users(ctx):
    return rows(ctx.conn.execute("SELECT * FROM users ORDER BY id"))


def list_workspaces(ctx):
    return rows(ctx.conn.execute("SELECT * FROM workspaces ORDER BY id"))


def me(ctx):
    if ctx.user is None:
        raise D.Forbidden("缺少 X-User-Id")
    return {"user": ctx.user,
            "memberships": rows(ctx.conn.execute(
                "SELECT * FROM memberships WHERE user_id=?", (ctx.user["id"],)))}


@route("GET", r"/api/workspaces/(\d+)/members", "read")
def list_members(ctx, wid):
    return rows(ctx.conn.execute(
        """SELECT m.workspace_id, m.role, m.active, u.id AS user_id, u.name
           FROM memberships m JOIN users u ON u.id=m.user_id
           WHERE m.workspace_id=? ORDER BY u.id""", (wid,)))


@route("POST", r"/api/workspaces/(\d+)/members", "admin")
def add_member(ctx, wid):
    data = ctx.body
    u = row(ctx.conn.execute("SELECT * FROM users WHERE id=?", (data["user_id"],)))
    if u is None:
        raise D.BadRequest("用户不存在")
    if data.get("role") not in ("admin", "editor", "viewer"):
        raise D.BadRequest("role 必须是 admin/editor/viewer")
    ctx.conn.execute(
        "INSERT INTO memberships(workspace_id,user_id,role,active) VALUES(?,?,?,1) "
        "ON CONFLICT(workspace_id,user_id) DO UPDATE SET role=excluded.role, active=1",
        (wid, data["user_id"], data["role"]))
    return {"ok": True}


@route("DELETE", r"/api/workspaces/(\d+)/members/(\d+)", "admin")
def remove_member(ctx, wid, uid):
    """老师离开团队：停用成员资格；其历史修订保留，工作区按权限仍可查看。"""
    m = membership_for(ctx.conn, wid, uid)
    if m is None or not m["active"]:
        raise D.NotFound("成员不存在或已停用")
    ctx.conn.execute(
        "UPDATE memberships SET active=0 WHERE workspace_id=? AND user_id=?", (wid, uid))
    return {"deactivated": uid}


@route("GET", r"/api/workspaces/(\d+)/quota", "read")
def get_quota(ctx, wid):
    q = row(ctx.conn.execute(
        "SELECT * FROM workspace_quota WHERE workspace_id=?", (wid,)))
    if q is None:
        raise D.NotFound("工作区额度未初始化")
    d = dict(q)
    d["remaining"] = d["exports_limit"] - d["exports_used"]
    return d


@route("GET", r"/api/workspaces/(\d+)/courses", "read")
def list_courses(ctx, wid):
    return rows(ctx.conn.execute(
        "SELECT * FROM courses WHERE workspace_id=? ORDER BY id", (wid,)))


@route("POST", r"/api/workspaces/(\d+)/courses", "edit")
def create_course(ctx, wid):
    title = ctx.body.get("title")
    if not title:
        raise D.BadRequest("缺少 title")
    cur = ctx.conn.execute(
        "INSERT INTO courses(workspace_id,title,created_by,created_at) VALUES(?,?,?,?)",
        (wid, title, ctx.user["id"], D.utcnow()))
    cid = cur.lastrowid
    ctx.conn.execute(
        "INSERT INTO workspace_quota(workspace_id,exports_limit,exports_used) VALUES(?,20,0) "
        "ON CONFLICT(workspace_id) DO NOTHING", (wid,))
    after = D.get_or_404(ctx.conn, "courses", cid)
    D.log_revision(ctx.conn, wid, cid, "course", cid, after["version"],
                   "create", ctx.actor(), after=after)
    return after


@route("GET", r"/api/courses/(\d+)", "read")
def get_course(ctx, cid):
    course = course_of(ctx.conn, cid)
    chapters = rows(ctx.conn.execute(
        "SELECT * FROM chapters WHERE course_id=? ORDER BY position", (cid,)))
    chapters = [dict(c) for c in chapters]
    for c in chapters:
        segs = rows(ctx.conn.execute(
            "SELECT * FROM segments WHERE chapter_id=? ORDER BY position", (c["id"],)))
        c["segments"] = [dict(s) for s in segs]
        c["requires"] = [e["requires_chapter_id"] for e in rows(ctx.conn.execute(
            "SELECT * FROM prereq_edges WHERE chapter_id=?", (c["id"],)))]
    objectives = [dict(o) for o in rows(ctx.conn.execute(
        "SELECT * FROM objectives WHERE course_id=? ORDER BY id", (cid,)))]
    for o in objectives:
        o["coverage"] = [dict(x) for x in rows(ctx.conn.execute(
            "SELECT * FROM objective_coverage WHERE objective_id=?", (o["id"],)))]
    return {"course": course, "chapters": chapters, "objectives": objectives}


@route("PUT", r"/api/courses/(\d+)", "edit")
def update_course(ctx, cid):
    course = course_of(ctx.conn, cid)
    fields = {}
    if "title" in ctx.body:
        fields["title"] = ctx.body["title"]
    if "status" in ctx.body:
        if ctx.body["status"] not in ("draft", "published", "archived"):
            raise D.BadRequest("非法 status")
        fields["status"] = ctx.body["status"]
    if not fields:
        raise D.BadRequest("没有可更新字段")
    updated = D.update_versioned(ctx.conn, "courses", cid, fields,
                                 int(ctx.body.get("base_version", course["version"])))
    D.log_revision(ctx.conn, course["workspace_id"], cid, "course", cid,
                   updated["version"], "update", ctx.actor(),
                   before=dict(course), after=dict(updated))
    return updated


@route("POST", r"/api/courses/(\d+)/chapters", "edit")
def create_chapter(ctx, cid):
    course = course_of(ctx.conn, cid)
    title = ctx.body.get("title")
    if not title:
        raise D.BadRequest("缺少 title")
    pos = row(ctx.conn.execute(
        "SELECT COALESCE(MAX(position),0)+1 AS p FROM chapters WHERE course_id=?", (cid,)))["p"]
    minutes = float(ctx.body.get("minutes", 0))
    cur = ctx.conn.execute(
        "INSERT INTO chapters(course_id,title,position,minutes) VALUES(?,?,?,?)",
        (cid, title, pos, minutes))
    chid = cur.lastrowid
    D.bump_course(ctx.conn, cid)
    after = D.get_or_404(ctx.conn, "chapters", chid)
    D.log_revision(ctx.conn, course["workspace_id"], cid, "chapter", chid,
                   after["version"], "create", ctx.actor(), after=dict(after))
    return after


@route("PUT", r"/api/chapters/(\d+)", "edit")
def update_chapter(ctx, chid):
    before = chapter_course(ctx.conn, chid)
    course = course_of(ctx.conn, before["course_id"])
    fields = {}
    for k in ("title", "minutes", "position"):
        if k in ctx.body:
            fields[k] = float(ctx.body[k]) if k == "minutes" else ctx.body[k]
    if not fields:
        raise D.BadRequest("没有可更新字段")
    updated = D.update_versioned(ctx.conn, "chapters", chid, fields,
                                 int(ctx.body.get("base_version", before["version"])))
    D.bump_course(ctx.conn, before["course_id"])
    D.log_revision(ctx.conn, course["workspace_id"], before["course_id"], "chapter", chid,
                   updated["version"], "update", ctx.actor(),
                   before=dict(before), after=dict(updated))
    return updated


@route("POST", r"/api/courses/(\d+)/chapters/reorder", "edit")
def reorder_chapters(ctx, cid):
    """章节重排：提交后必须重新验证知识前置关系，而不是只更新总分钟数。"""
    course = course_of(ctx.conn, cid)
    order = ctx.body.get("order")
    if not isinstance(order, list):
        raise D.BadRequest("缺少 order 数组")
    existing = sorted(x["id"] for x in rows(ctx.conn.execute(
        "SELECT id FROM chapters WHERE course_id=?", (cid,))))
    if sorted(order) != existing:
        raise D.BadRequest("order 必须恰好包含课程全部章节 id")
    base = int(ctx.body.get("base_version", course["version"]))
    for pos, chid in enumerate(order, start=1):
        ctx.conn.execute(
            "UPDATE chapters SET position=?, version=version+1 WHERE id=?", (pos, chid))
    new_ver = D.bump_course(ctx.conn, cid)
    D.log_revision(ctx.conn, course["workspace_id"], cid, "course", cid, new_ver,
                   "reorder", ctx.actor(), before={"version": base}, after={"version": new_ver})
    return {"report": D.validate_course(ctx.conn, cid)}


@route("DELETE", r"/api/chapters/(\d+)", "edit")
def delete_chapter(ctx, chid):
    """删除章节（可能是别人的前置）：级联快照入审计，返回重验证报告。"""
    before = chapter_course(ctx.conn, chid)
    cid = before["course_id"]
    snap, new_ver = D.delete_chapter(ctx.conn, chid, ctx.actor())
    return {"deleted": chid, "course_version": new_ver,
            "snapshot": snap, "report": D.validate_course(ctx.conn, cid)}


@route("POST", r"/api/chapters/(\d+)/prereqs", "edit")
def add_prereq(ctx, chid):
    ch = chapter_course(ctx.conn, chid)
    cid = ch["course_id"]
    req = int(ctx.body["requires_chapter_id"])
    target = chapter_course(ctx.conn, req)
    if target["course_id"] != cid:
        raise D.BadRequest("前置章节必须属于同一课程")
    if D.would_cycle(ctx.conn, cid, chid, req):
        raise D.BadRequest("该前置关系会形成循环依赖")
    try:
        cur = ctx.conn.execute(
            "INSERT INTO prereq_edges(course_id,chapter_id,requires_chapter_id) VALUES(?,?,?)",
            (cid, chid, req))
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise D.Conflict("前置关系已存在")
        raise
    D.bump_course(ctx.conn, cid)
    return D.get_or_404(ctx.conn, "prereq_edges", cur.lastrowid)


@route("DELETE", r"/api/prereqs/(\d+)", "edit")
def del_prereq(ctx, eid):
    e = row(ctx.conn.execute("SELECT * FROM prereq_edges WHERE id=?", (eid,)))
    if e is None:
        raise D.NotFound("前置关系不存在")
    ctx.conn.execute("DELETE FROM prereq_edges WHERE id=?", (eid,))
    cid = e["course_id"]
    D.bump_course(ctx.conn, cid)
    return {"ok": True, "report": D.validate_course(ctx.conn, cid)}


@route("POST", r"/api/chapters/(\d+)/segments", "edit")
def create_segment(ctx, chid):
    ch = chapter_course(ctx.conn, chid)
    course = course_of(ctx.conn, ch["course_id"])
    kind = ctx.body.get("kind")
    if kind not in ("lecture", "demo", "exercise"):
        raise D.BadRequest("kind 必须是 lecture/demo/exercise")
    pos = row(ctx.conn.execute(
        "SELECT COALESCE(MAX(position),0)+1 AS p FROM segments WHERE chapter_id=?",
        (chid,)))["p"]
    cur = ctx.conn.execute(
        "INSERT INTO segments(chapter_id,kind,title,body,position,minutes) VALUES(?,?,?,?,?,?)",
        (chid, kind, ctx.body.get("title", ""), ctx.body.get("body", ""), pos,
         float(ctx.body.get("minutes", 0))))
    sid = cur.lastrowid
    D.bump_course(ctx.conn, ch["course_id"])
    after = D.get_or_404(ctx.conn, "segments", sid)
    D.log_revision(ctx.conn, course["workspace_id"], ch["course_id"], "segment", sid,
                   after["version"], "create", ctx.actor(), after=dict(after))
    return after


@route("PUT", r"/api/segments/(\d+)", "edit")
def update_segment(ctx, sid):
    before = segment_course(ctx.conn, sid)
    course = course_of(ctx.conn,
                       chapter_course(ctx.conn, before["chapter_id"])["course_id"])
    fields = {}
    for k in ("title", "body", "kind", "minutes"):
        if k in ctx.body:
            fields[k] = float(ctx.body[k]) if k == "minutes" else ctx.body[k]
    if not fields:
        raise D.BadRequest("没有可更新字段")
    updated = D.update_versioned(ctx.conn, "segments", sid, fields,
                                 int(ctx.body.get("base_version", before["version"])))
    D.bump_course(ctx.conn, course["id"])
    D.log_revision(ctx.conn, course["workspace_id"], course["id"], "segment", sid,
                   updated["version"], "update", ctx.actor(),
                   before=dict(before), after=dict(updated))
    return updated


@route("DELETE", r"/api/segments/(\d+)", "edit")
def delete_segment(ctx, sid):
    before = segment_course(ctx.conn, sid)
    ch = chapter_course(ctx.conn, before["chapter_id"])
    course = course_of(ctx.conn, ch["course_id"])
    bindings = [dict(b) for b in rows(ctx.conn.execute(
        "SELECT * FROM demo_bindings WHERE segment_id=?", (sid,)))]
    coverage = [dict(c) for c in rows(ctx.conn.execute(
        "SELECT * FROM objective_coverage WHERE segment_id=?", (sid,)))]
    ctx.conn.execute("DELETE FROM demo_bindings WHERE segment_id=?", (sid,))
    ctx.conn.execute("DELETE FROM objective_coverage WHERE segment_id=?", (sid,))
    ctx.conn.execute("DELETE FROM segments WHERE id=?", (sid,))
    new_ver = D.bump_course(ctx.conn, ch["course_id"])
    D.log_revision(ctx.conn, course["workspace_id"], course["id"], "segment", sid,
                   before["version"], "delete", ctx.actor(),
                   before={"segment": dict(before),
                           "bindings": [dict(b) for b in bindings],
                           "coverage": [dict(c) for c in coverage]})
    return {"deleted": sid, "course_version": new_ver}


@route("POST", r"/api/courses/(\d+)/objectives", "edit")
def create_objective(ctx, cid):
    course = course_of(ctx.conn, cid)
    code = ctx.body.get("code")
    title = ctx.body.get("title")
    if not code or not title:
        raise D.BadRequest("缺少 code/title")
    cur = ctx.conn.execute(
        "INSERT INTO objectives(course_id,code,title,description) VALUES(?,?,?,?)",
        (cid, code, title, ctx.body.get("description", "")))
    oid = cur.lastrowid
    ctx.conn.execute(
        "INSERT INTO objective_revisions(objective_id,revision,title,description,changed_by,"
        "changed_at) VALUES(?,1,?,?,?,?)",
        (oid, title, ctx.body.get("description", ""), ctx.user["id"], D.utcnow()))
    after = D.get_or_404(ctx.conn, "objectives", oid)
    D.log_revision(ctx.conn, course["workspace_id"], cid, "objective", oid,
                   after["version"], "create", ctx.actor(), after=dict(after))
    return after


@route("PUT", r"/api/objectives/(\d+)", "edit")
def update_objective(ctx, oid):
    """训练目标改版：revision+1，既有覆盖关系标记 stale，需重新确认。"""
    before = D.get_or_404(ctx.conn, "objectives", oid)
    course = course_of(ctx.conn, before["course_id"])
    fields = {}
    if "title" in ctx.body:
        fields["title"] = ctx.body["title"]
    if "description" in ctx.body:
        fields["description"] = ctx.body["description"]
    if "status" in ctx.body:
        if ctx.body["status"] not in ("active", "retired"):
            raise D.BadRequest("非法 status")
        fields["status"] = ctx.body["status"]
    if not fields:
        raise D.BadRequest("没有可更新字段")
    updated = D.update_versioned(ctx.conn, "objectives", oid, fields,
                                 int(ctx.body.get("base_version", before["version"])))
    ctx.conn.execute("UPDATE objectives SET revision=revision+1 WHERE id=?", (oid,))
    ctx.conn.execute(
        "INSERT INTO objective_revisions(objective_id,revision,title,description,changed_by,"
        "changed_at) VALUES(?,?,?,?,?,?)",
        (oid, updated["revision"] + 1, updated["title"], updated["description"],
         ctx.user["id"], D.utcnow()))
    ctx.conn.execute("UPDATE objective_coverage SET stale=1 WHERE objective_id=?", (oid,))
    D.bump_course(ctx.conn, course["id"])
    after = D.get_or_404(ctx.conn, "objectives", oid)
    D.log_revision(ctx.conn, course["workspace_id"], course["id"], "objective", oid,
                   after["version"], "update", ctx.actor(),
                   before=dict(before), after=dict(after))
    return after


@route("POST", r"/api/objectives/(\d+)/coverage", "edit")
def add_coverage(ctx, oid):
    o = D.get_or_404(ctx.conn, "objectives", oid)
    sid = int(ctx.body["segment_id"])
    seg = segment_course(ctx.conn, sid)
    ch = chapter_course(ctx.conn, seg["chapter_id"])
    if ch["course_id"] != o["course_id"]:
        raise D.BadRequest("片段必须属于同一课程")
    try:
        cur = ctx.conn.execute(
            "INSERT INTO objective_coverage(objective_id,segment_id,created_by) VALUES(?,?,?)",
            (oid, sid, ctx.user["id"]))
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise D.Conflict("该覆盖关系已存在")
        raise
    D.bump_course(ctx.conn, o["course_id"])
    return D.get_or_404(ctx.conn, "objective_coverage", cur.lastrowid)


@route("DELETE", r"/api/coverage/(\d+)", "edit")
def del_coverage(ctx, cid_cov):
    cov = row(ctx.conn.execute(
        "SELECT * FROM objective_coverage WHERE id=?", (cid_cov,)))
    if cov is None:
        raise D.NotFound("覆盖关系不存在")
    o = D.get_or_404(ctx.conn, "objectives", cov["objective_id"])
    ctx.conn.execute("DELETE FROM objective_coverage WHERE id=?", (cid_cov,))
    D.bump_course(ctx.conn, o["course_id"])
    return {"ok": True, "report": D.validate_course(ctx.conn, o["course_id"])}


@route("POST", r"/api/objectives/(\d+)/coverage/confirm", "edit")
def confirm_coverage(ctx, oid):
    o = D.get_or_404(ctx.conn, "objectives", oid)
    ctx.conn.execute("UPDATE objective_coverage SET stale=0 WHERE objective_id=?", (oid,))
    return {"ok": True, "report": D.validate_course(ctx.conn, o["course_id"])}


@route("GET", r"/api/courses/(\d+)/report", "read")
def course_report(ctx, cid):
    course_of(ctx.conn, cid)
    return D.validate_course(ctx.conn, cid)


@route("GET", r"/api/workspaces/(\d+)/assets", "read")
def list_assets(ctx, wid):
    assets = [dict(a) for a in rows(ctx.conn.execute(
        "SELECT * FROM assets WHERE workspace_id=? ORDER BY id", (wid,)))]
    for a in assets:
        a["versions"] = [dict(v) for v in rows(ctx.conn.execute(
            "SELECT * FROM asset_versions WHERE asset_id=? ORDER BY version", (a["id"],)))]
    return assets


@route("POST", r"/api/workspaces/(\d+)/assets", "edit")
def create_asset(ctx, wid):
    name = ctx.body.get("name")
    if not name:
        raise D.BadRequest("缺少 name")
    kind = ctx.body.get("kind", "video")
    cur = ctx.conn.execute(
        "INSERT INTO assets(workspace_id,name,kind) VALUES(?,?,?)", (wid, name, kind))
    aid = cur.lastrowid
    ctx.conn.execute(
        "INSERT INTO asset_versions(asset_id,version,duration_ms,has_audio,uri,created_by,"
        "created_at) VALUES(?,1,?,?,?,?,?,?)",
        (aid, int(ctx.body.get("duration_ms", 0)), 1 if ctx.body.get("has_audio", True) else 0,
         ctx.body.get("uri", ""), ctx.user["id"], D.utcnow()))
    after = D.get_or_404(ctx.conn, "assets", aid)
    D.log_revision(ctx.conn, wid, None, "asset", aid, after["version"],
                   "create", ctx.actor(), after=dict(after))
    return after


@route("POST", r"/api/assets/(\d+)/versions", "edit")
def replace_asset_version(ctx, aid):
    """替换视频：follow 绑定跟随新版本；时间码越界/音轨缺失/版本不一致 → 待修复。"""
    before = D.get_or_404(ctx.conn, "assets", aid)
    new_version = before["current_version"] + 1
    ctx.conn.execute(
        "INSERT INTO asset_versions(asset_id,version,duration_ms,has_audio,uri,note,created_by,"
        "created_at) VALUES(?,?,?,?,?,?,?,?)",
        (aid, new_version, int(ctx.body.get("duration_ms", 0)),
         1 if ctx.body.get("has_audio", True) else 0, ctx.body.get("uri", ""),
         ctx.body.get("note", ""), ctx.user["id"], D.utcnow()))
    updated = D.update_versioned(
        ctx.conn, "assets", aid, {"current_version": new_version}, before["version"])
    D.log_revision(ctx.conn, before["workspace_id"], None, "asset", aid,
                   updated["version"], "update", ctx.actor(),
                   before=dict(before), after=dict(updated))
    # follow 绑定先跟随，再按新版本时长/音轨全部重校验
    affected = rows(ctx.conn.execute(
        "SELECT * FROM demo_bindings WHERE asset_id=?", (aid,)))
    D.recheck_asset_bindings(ctx.conn, aid)
    touched_segments = sorted({b["segment_id"] for b in affected})
    return {"new_version": new_version, "touched_segments": touched_segments}


@route("POST", r"/api/segments/(\d+)/bindings", "edit")
def create_binding(ctx, sid):
    seg = segment_course(ctx.conn, sid)
    if seg["kind"] != "demo":
        raise D.BadRequest("只有 demo 片段可以绑定素材")
    role = ctx.body.get("role")
    if role not in ("video", "subtitle", "notes"):
        raise D.BadRequest("role 必须是 video/subtitle/notes")
    aid = int(ctx.body["asset_id"])
    asset = D.get_or_404(ctx.conn, "assets", aid)
    ver = int(ctx.body.get("asset_version", asset["current_version"]))
    av = row(ctx.conn.execute(
        "SELECT * FROM asset_versions WHERE asset_id=? AND version=?", (aid, ver)))
    if av is None:
        raise D.BadRequest("素材版本不存在")
    try:
        cur = ctx.conn.execute(
            "INSERT INTO demo_bindings(segment_id,role,asset_id,asset_version,pin_mode,in_ms,"
            "out_ms) VALUES(?,?,?,?,?,?,?)",
            (sid, role, aid, ver, ctx.body.get("pin_mode", "follow"),
             int(ctx.body.get("in_ms", 0)), int(ctx.body.get("out_ms", 0))))
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise D.Conflict("该片段已有同角色绑定")
        raise
    bid = cur.lastrowid
    ch = chapter_course(ctx.conn, seg["chapter_id"])
    course = course_of(ctx.conn, ch["course_id"])
    D.recheck_segment_bindings(ctx.conn, sid)
    D.bump_course(ctx.conn, course["id"])
    after = D.get_or_404(ctx.conn, "demo_bindings", bid)
    D.log_revision(ctx.conn, course["workspace_id"], course["id"], "binding", bid,
                   after["version"], "create", ctx.actor(), after=dict(after))
    return after


@route("PUT", r"/api/bindings/(\d+)", "edit")
def repair_binding(ctx, bid):
    """待修复条目：调整时间码/版本后重校验，全部通过才回到 ok。"""
    before = D.get_or_404(ctx.conn, "demo_bindings", bid)
    seg = segment_course(ctx.conn, before["segment_id"])
    ch = chapter_course(ctx.conn, seg["chapter_id"])
    course = course_of(ctx.conn, ch["course_id"])
    fields = {}
    for k in ("asset_id", "asset_version", "pin_mode", "in_ms", "out_ms"):
        if k in ctx.body:
            fields[k] = ctx.body[k]
    if not fields:
        raise D.BadRequest("没有可更新字段")
    D.update_versioned(ctx.conn, "demo_bindings", bid, fields,
                       int(ctx.body.get("base_version", before["version"])))
    D.recheck_segment_bindings(ctx.conn, before["segment_id"])
    D.bump_course(ctx.conn, course["id"])
    after = D.get_or_404(ctx.conn, "demo_bindings", bid)
    D.log_revision(ctx.conn, course["workspace_id"], course["id"], "binding", bid,
                   after["version"], "update", ctx.actor(),
                   before=dict(before), after=dict(after))
    return after


@route("GET", r"/api/courses/(\d+)/bindings", "read")
def list_bindings(ctx, cid):
    return rows(ctx.conn.execute(
        """SELECT b.*, s.title AS segment_title, s.kind, a.name AS asset_name
           FROM demo_bindings b
           JOIN segments s ON s.id=b.segment_id
           JOIN chapters ch ON ch.id=s.chapter_id
           JOIN assets a ON a.id=b.asset_id
           WHERE ch.course_id=? ORDER BY b.id""", (cid,)))


@route("POST", r"/api/courses/(\d+)/exports", "edit")
def export_course(ctx, cid):
    """重复导出作业保持一致：同一 idempotency_key 返回同一包，配额只计一次。"""
    course_of(ctx.conn, cid)
    mode = ctx.body.get("mode", "pinned")
    if mode not in ("pinned", "follow"):
        raise D.BadRequest("mode 必须是 pinned/follow")
    idem = ctx.body.get("idempotency_key")
    if not idem:
        raise D.BadRequest("缺少 idempotency_key")
    pkg, created = D.create_export(ctx.conn, cid, mode, idem, ctx.actor())
    return {"created": created, "package": pkg}


@route("GET", r"/api/courses/(\d+)/packages", "read")
def list_packages(ctx, cid):
    out = []
    for p in rows(ctx.conn.execute(
            "SELECT * FROM packages WHERE course_id=? ORDER BY id DESC", (cid,))):
        d = dict(p)
        d["receipt"] = row(ctx.conn.execute(
            "SELECT * FROM export_receipts WHERE package_id=?", (p["id"],)))
        out.append(d)
    return out


@route("GET", r"/api/packages/(\d+)", "read")
def get_package(ctx, pid):
    pkg = D.get_or_404(ctx.conn, "packages", pid)
    d = dict(pkg)
    d["receipt"] = row(ctx.conn.execute(
        "SELECT * FROM export_receipts WHERE package_id=?", (pid,)))
    return d


@route("GET", r"/api/packages/(\d+)/artifact", "read")
def get_artifact(ctx, pid):
    pkg = D.get_or_404(ctx.conn, "packages", pid)
    if not pkg["artifact_uri"]:
        raise D.BadRequest("课件包尚未生成")
    try:
        with open(pkg["artifact_uri"], "r", encoding="utf-8") as f:
            return json.loads(f.read())
    except FileNotFoundError:
        raise D.NotFound("课件包文件缺失")


@route("POST", r"/api/packages/(\d+)/rebuild", "edit")
def rebuild_package(ctx, pid):
    pkg = D.get_or_404(ctx.conn, "packages", pid)
    if pkg["frozen"]:
        raise D.Forbidden("课件包已被开班冻结，不能重建；学员正在使用的内容不受模板升级影响")
    course = course_of(ctx.conn, pkg["course_id"])
    new_pkg, created = D.create_export(
        ctx.conn, course["id"], pkg["mode"], f"rebuild:{pid}:{course['version']}",
        ctx.actor())
    return {"created": created, "package": new_pkg}


@route("POST", r"/api/workspaces/(\d+)/packages/reconcile", "admin")
def reconcile(ctx, wid):
    return {"reissued": D.reconcile_receipts(ctx.conn, wid)}


@route("GET", r"/api/courses/(\d+)/packaging/modes", "read")
def packaging_modes(ctx, cid):
    """固定课件版本复用 vs 模块跟随更新 的对比与当前状态。"""
    packages = rows(ctx.conn.execute(
        "SELECT * FROM packages WHERE course_id=? ORDER BY id DESC", (cid,)))
    active_classes = rows(ctx.conn.execute(
        "SELECT * FROM classes WHERE course_id=? AND status='active'", (cid,)))
    return {
        "modes": [
            {"mode": "pinned",
             "label": "固定课件版本复用",
             "description": "导出时固化课程版本快照，之后模板升级不影响该包；可无限复用同一产物",
             "fit": "已开班课程、对外发布、审计留档"},
            {"mode": "follow",
             "label": "模块跟随更新",
             "description": "包跟随模块更新：课程内容变化后标记 stale，需重新构建才生效",
             "fit": "未开班的试听课件、内部评审"},
        ],
        "packages": [dict(p) for p in packages],
        "active_classes": [dict(c) for c in active_classes],
        "freeze_rule": "班级开班时其课件包被冻结(frozen=1)并固定为 pinned；冻结包禁止重建，"
                       "模板升级不会改动学员正在使用的内容",
    }


@route("POST", r"/api/courses/(\d+)/classes", "edit")
def create_class(ctx, cid):
    data = ctx.body
    if not data.get("name") or not data.get("package_id"):
        raise D.BadRequest("缺少 name/package_id")
    return D.start_class(ctx.conn, cid, data["name"], int(data["package_id"]), ctx.actor())


@route("GET", r"/api/courses/(\d+)/classes", "read")
def list_classes(ctx, cid):
    out = []
    for cl in rows(ctx.conn.execute(
            "SELECT * FROM classes WHERE course_id=? ORDER BY id", (cid,))):
        d = dict(cl)
        d["package"] = row(ctx.conn.execute(
            "SELECT * FROM packages WHERE id=?", (cl["package_id"],)))
        out.append(d)
    return out


@route("GET", r"/api/classes/(\d+)", "read")
def get_class(ctx, clid):
    cl = D.get_or_404(ctx.conn, "classes", clid)
    pkg = D.get_or_404(ctx.conn, "packages", cl["package_id"])
    d = dict(cl)
    d["package"] = dict(pkg)
    if pkg["artifact_uri"] and os.path.exists(pkg["artifact_uri"]):
        with open(pkg["artifact_uri"], "r", encoding="utf-8") as f:
            d["artifact"] = json.loads(f.read())
    return d


@route("GET", r"/api/courses/(\d+)/revisions", "read")
def course_revisions(ctx, cid):
    return rows(ctx.conn.execute(
        "SELECT * FROM revisions WHERE course_id=? OR "
        "(entity_type='course' AND entity_id=?) ORDER BY id DESC LIMIT 200", (cid, cid)))


@route("GET", r"/api/workspaces/(\d+)/revisions", "read")
def ws_revisions(ctx, wid):
    return rows(ctx.conn.execute(
        "SELECT * FROM revisions WHERE workspace_id=? ORDER BY id DESC LIMIT 200", (wid,)))


@route("POST", r"/api/revisions/(\d+)/undo", "edit")
def undo(ctx, rid):
    return D.undo_revision(ctx.conn, rid, ctx.actor())


# 公共路由（无需工作区成员校验）
route("GET", r"/api/users", "public")(list_users)
route("GET", r"/api/workspaces", "public")(list_workspaces)
route("GET", r"/api/me", "public")(me)


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

def make_handler(db_path, static_dir):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send_json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False, default=dict).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            path = parsed.path
            if not path.startswith("/api/"):
                return self._static(path)

            uid = self.headers.get("X-User-Id")
            conn = connect(db_path)
            try:
                user = row(conn.execute("SELECT * FROM users WHERE id=?", (uid,))) if uid else None
                for m, rx, perm, fn in ROUTES:
                    if m != method:
                        continue
                    match = rx.match(path)
                    if not match:
                        continue
                    args = [int(g) for g in match.groups()]
                    ws_id = self._workspace_of(conn, path, args)
                    membership = (membership_for(conn, ws_id, user["id"])
                                  if user and ws_id else None)
                    ctx = Ctx(conn, user, membership, parse_qs(parsed.query))
                    ctx.body = body_of(self) if method in ("POST", "PUT") else {}

                    if perm != "public":
                        if not user:
                            raise D.Forbidden("缺少 X-User-Id 头")
                        ctx.require(perm)

                    begin_immediate(conn)
                    result = fn(ctx, *args)
                    conn.commit()
                    return self._send_json(200, result)
                self._send_json(404, {"error": "not_found", "path": path})
            except D.NotFound as e:
                conn.rollback()
                self._send_json(404, {"error": "not_found", "message": str(e)})
            except D.Conflict as e:
                conn.rollback()
                self._send_json(409, {"error": "conflict", "message": str(e),
                                      "current": dict(e.payload) if e.payload is not None else None})
            except D.Forbidden as e:
                conn.rollback()
                self._send_json(403, {"error": "forbidden", "message": str(e)})
            except D.BadRequest as e:
                conn.rollback()
                self._send_json(400, {"error": "bad_request", "message": str(e)})
            except Exception as e:
                conn.rollback()
                traceback.print_exc()
                self._send_json(500, {"error": "internal", "message": str(e)})
            finally:
                conn.close()

        def _workspace_of(self, conn, path, args):
            m = re.match(r"^/api/workspaces/(\d+)", path)
            if m:
                return int(m.group(1))
            m = re.match(r"^/api/(courses|chapters|segments|objectives|assets|bindings|"
                         r"packages|classes|revisions|prereqs|coverage)/(\d+)", path)
            if not m:
                return None
            kind, rid = m.group(1), int(m.group(2))
            queries = {
                "courses": "SELECT workspace_id AS w FROM courses WHERE id=?",
                "chapters": "SELECT c.workspace_id AS w FROM chapters ch JOIN courses c "
                            "ON c.id=ch.course_id WHERE ch.id=?",
                "segments": "SELECT c.workspace_id AS w FROM segments s JOIN chapters ch "
                            "ON ch.id=s.chapter_id JOIN courses c ON c.id=ch.course_id WHERE s.id=?",
                "objectives": "SELECT c.workspace_id AS w FROM objectives o JOIN courses c "
                              "ON c.id=o.course_id WHERE o.id=?",
                "assets": "SELECT workspace_id AS w FROM assets WHERE id=?",
                "bindings": "SELECT c.workspace_id AS w FROM demo_bindings b JOIN segments s "
                            "ON s.id=b.segment_id JOIN chapters ch ON ch.id=s.chapter_id "
                            "JOIN courses c ON c.id=ch.course_id WHERE b.id=?",
                "packages": "SELECT c.workspace_id AS w FROM packages p JOIN courses c "
                            "ON c.id=p.course_id WHERE p.id=?",
                "classes": "SELECT c.workspace_id AS w FROM classes cl JOIN courses c "
                           "ON c.id=cl.course_id WHERE cl.id=?",
                "revisions": "SELECT workspace_id AS w FROM revisions WHERE id=?",
                "prereqs": "SELECT c.workspace_id AS w FROM prereq_edges e JOIN courses c "
                           "ON c.id=e.course_id WHERE e.id=?",
                "coverage": "SELECT c.workspace_id AS w FROM objective_coverage oc "
                            "JOIN objectives o ON o.id=oc.objective_id JOIN courses c "
                            "ON c.id=o.course_id WHERE oc.id=?",
            }
            r = row(conn.execute(queries[kind], (rid,)))
            return r["w"] if r else None

        def _static(self, path):
            rel = path.lstrip("/") or "index.html"
            full = os.path.normpath(os.path.join(static_dir, rel))
            if not full.startswith(os.path.abspath(static_dir)) or not os.path.isfile(full):
                full = os.path.join(static_dir, "index.html")
            ctype = {".html": "text/html; charset=utf-8",
                     ".js": "application/javascript; charset=utf-8",
                     ".css": "text/css; charset=utf-8"}.get(
                os.path.splitext(full)[1], "application/octet-stream")
            with open(full, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def make_server(db_path, static_dir, port=8000):
    handler = make_handler(db_path, static_dir)
    return ThreadingHTTPServer(("0.0.0.0", port), handler)
