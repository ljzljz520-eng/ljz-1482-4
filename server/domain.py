# -*- coding: utf-8 -*-
"""领域服务：前置关系重验证、目标覆盖、素材版本一致性、撤销、冻结、配额幂等。"""
import datetime
import json
import os

from .db import row, rows


class NotFound(Exception):
    def __init__(self, msg, payload=None):
        super().__init__(msg)
        self.payload = payload


class Conflict(Exception):
    def __init__(self, msg, payload=None):
        super().__init__(msg)
        self.payload = payload


class Forbidden(Exception):
    pass


class BadRequest(Exception):
    pass


# 可撤销实体 → 物理表
UNDO_TABLES = {
    "chapter": "chapters",
    "segment": "segments",
    "objective": "objectives",
    "demo_bindings": "demo_bindings",
    "binding": "demo_bindings",
    "course": "courses",
    "asset": "assets",
}


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def get_or_404(conn, table, rid):
    r = row(conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)))
    if r is None:
        raise NotFound(f"{table}#{rid} 不存在")
    return r


def update_versioned(conn, table, rid, fields, base_version):
    """乐观并发：仅当当前版本等于 base_version 时才更新。"""
    sets = ", ".join(f"{k}=?" for k in fields)
    if sets:
        sets = f"{sets}, version=version+1"
    else:
        sets = "version=version+1"
    sql = f"UPDATE {table} SET {sets} WHERE id=? AND version=?"
    cur = conn.execute(sql, [*fields.values(), rid, base_version])
    if cur.rowcount == 0:
        cur_row = row(conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)))
        if cur_row is None:
            raise NotFound(f"{table}#{rid} 不存在")
        raise Conflict(
            f"{table}#{rid} 版本冲突：期望 v{base_version}，实际 v{cur_row['version']}",
            cur_row,
        )
    return get_or_404(conn, table, rid)


def log_revision(conn, workspace_id, course_id, entity_type, entity_id, version,
                 action, actor, before=None, after=None):
    conn.execute(
        """INSERT INTO revisions(workspace_id, course_id, entity_type, entity_id, version,
                                 action, actor_id, actor_name, before_json, after_json, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (workspace_id, course_id, entity_type, entity_id, version, action,
         actor["id"], actor["name"],
         json.dumps(before, ensure_ascii=False) if before is not None else None,
         json.dumps(after, ensure_ascii=False) if after is not None else None,
         utcnow()),
    )


def bump_course(conn, course_id):
    """课程内容变化：课程版本 +1；follow 模式且未冻结的已完成课件包标记为过期。"""
    conn.execute("UPDATE courses SET version=version+1 WHERE id=?", (course_id,))
    conn.execute(
        "UPDATE packages SET stale=1 "
        "WHERE course_id=? AND mode='follow' AND status='done' AND frozen=0",
        (course_id,),
    )
    return row(conn.execute("SELECT version FROM courses WHERE id=?", (course_id,)))["version"]


# ---------------------------------------------------------------------------
# 章节快照 / 恢复（用于删除审计与撤销删除）
# ---------------------------------------------------------------------------

def chapter_ids(conn, course_id):
    return [r["id"] for r in rows(conn.execute(
        "SELECT id FROM chapters WHERE course_id=? ORDER BY position", (course_id,)))]


def would_cycle(conn, course_id, chapter_id, requires_id):
    """若新增 chapter_id -> requires_id -> ... 形成回环则返回 True。"""
    adj = {}
    for e in rows(conn.execute(
            "SELECT chapter_id, requires_chapter_id FROM prereq_edges WHERE course_id=?",
            (course_id,))):
        adj.setdefault(e["chapter_id"], []).append(e["requires_chapter_id"])
    adj.setdefault(chapter_id, []).append(requires_id)

    stack = [chapter_id]
    seen = set()
    while stack:
        u = stack.pop()
        if u in seen:
            continue
        seen.add(u)
        for v in adj.get(u, []):
            if v == chapter_id and u != chapter_id:
                return True
            if v == chapter_id and (u == chapter_id and requires_id == chapter_id):
                return True
            if v not in seen:
                stack.append(v)
    return False


def snapshot_chapter(conn, ch):
    segs = [dict(s) for s in rows(conn.execute(
        "SELECT * FROM segments WHERE chapter_id=? ORDER BY position", (ch["id"],)))]
    seg_ids = [s["id"] for s in segs]
    bindings = []
    coverage = []
    if seg_ids:
        q = ",".join("?" * len(seg_ids))
        bindings = [dict(b) for b in rows(conn.execute(
            f"SELECT * FROM demo_bindings WHERE segment_id IN ({q})", seg_ids))]
        coverage = [dict(c) for c in rows(conn.execute(
            f"SELECT * FROM objective_coverage WHERE segment_id IN ({q})", seg_ids))]
    edges = [dict(e) for e in rows(conn.execute(
        "SELECT * FROM prereq_edges WHERE chapter_id=? OR requires_chapter_id=?",
        (ch["id"], ch["id"])))]
    return {"chapter": dict(ch), "segments": segs, "bindings": bindings,
            "coverage": coverage, "edges": edges}


def restore_chapter(conn, snap):
    ch = snap["chapter"]
    conn.execute(
        "INSERT INTO chapters(id, course_id, title, position, minutes, version) VALUES(?,?,?,?,?,?)",
        (ch["id"], ch["course_id"], ch["title"], ch["position"], ch["minutes"], ch["version"]))
    for s in snap["segments"]:
        conn.execute(
            "INSERT INTO segments(id, chapter_id, kind, title, body, position, minutes, version) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (s["id"], s["chapter_id"], s["kind"], s["title"], s["body"],
             s["position"], s["minutes"], s["version"]))
    for b in snap["bindings"]:
        conn.execute(
            "INSERT INTO demo_bindings(id, segment_id, role, asset_id, asset_version, pin_mode, "
            "in_ms, out_ms, status, repair_reason, version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (b["id"], b["segment_id"], b["role"], b["asset_id"], b["asset_version"],
             b["pin_mode"], b["in_ms"], b["out_ms"], b["status"], b["repair_reason"],
             b["version"]))
    for c in snap["coverage"]:
        conn.execute(
            "INSERT OR IGNORE INTO objective_coverage(id, objective_id, segment_id, stale, created_by) "
            "VALUES(?,?,?,?,?)",
            (c["id"], c["objective_id"], c["segment_id"], c["stale"], c["created_by"]))
    for e in snap["edges"]:
        conn.execute(
            "INSERT OR IGNORE INTO prereq_edges(id, course_id, chapter_id, requires_chapter_id) "
            "VALUES(?,?,?,?)",
            (e["id"], e["course_id"], e["chapter_id"], e["requires_chapter_id"]))


def delete_chapter(conn, chapter_id, actor):
    """删除章节：级联删除其片段/绑定/覆盖/前置边；快照入审计可撤销；课程版本前移。"""
    ch = get_or_404(conn, "chapters", chapter_id)
    snap = snapshot_chapter(conn, ch)
    seg_ids = [s["id"] for s in snap["segments"]]
    if seg_ids:
        q = ",".join("?" * len(seg_ids))
        conn.execute(f"DELETE FROM demo_bindings WHERE segment_id IN ({q})", seg_ids)
        conn.execute(f"DELETE FROM objective_coverage WHERE segment_id IN ({q})", seg_ids)
    conn.execute(
        "DELETE FROM prereq_edges WHERE chapter_id=? OR requires_chapter_id=?",
        (chapter_id, chapter_id))
    conn.execute("DELETE FROM segments WHERE chapter_id=?", (chapter_id,))
    conn.execute("DELETE FROM chapters WHERE id=?", (chapter_id,))

    course = get_or_404(conn, "courses", ch["course_id"])
    new_ver = bump_course(conn, ch["course_id"])
    log_revision(conn, course["workspace_id"], ch["course_id"], "chapter", chapter_id,
                 ch["version"], "delete", actor, before=dict(snap))
    return snap, new_ver


# ---------------------------------------------------------------------------
# 全量重验证：前置关系（循环/悬挂/顺序）、目标覆盖、素材版本一致性
# ---------------------------------------------------------------------------

def validate_course(conn, course_id):
    """章节重排/删除/目标改版/素材替换后必须重跑：不能只更新总分钟数。"""
    chapters = rows(conn.execute(
        "SELECT * FROM chapters WHERE course_id=? ORDER BY position", (course_id,)))
    ch_pos = {c["id"]: c["position"] for c in chapters}
    ch_title = {c["id"]: c["title"] for c in chapters}
    issues = []

    edges = rows(conn.execute(
        "SELECT * FROM prereq_edges WHERE course_id=?", (course_id,)))
    adj = {}
    for e in edges:
        adj.setdefault(e["chapter_id"], []).append(e["requires_chapter_id"])

    # 有向图三色 DFS 检测环
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {}

    def dfs(u, path):
        color[u] = GRAY
        for v in adj.get(u, []):
            if color.get(v, WHITE) == GRAY:
                issues.append({
                    "type": "prereq_cycle",
                    "path": [ch_title.get(x, x) for x in path + [u, v]],
                })
            elif color.get(v, WHITE) == WHITE:
                dfs(v, path + [u])
        color[u] = BLACK

    for e in edges:
        if color.get(e["chapter_id"], WHITE) == WHITE:
            dfs(e["chapter_id"], [])

    for e in edges:
        a, b = e["chapter_id"], e["requires_chapter_id"]
        if a not in ch_pos or b not in ch_pos:
            issues.append({"type": "prereq_dangling", "edge_id": e["id"]})
            continue
        if ch_pos[b] >= ch_pos[a]:
            issues.append({
                "type": "prereq_order",
                "chapter": ch_title.get(a, a),
                "requires": ch_title.get(b, b),
                "detail": f"「{ch_title.get(a, a)}」排在它的前置「{ch_title.get(b, b)}」之前",
            })

    seg_alive = {r["id"] for r in rows(conn.execute(
        """SELECT s.id FROM segments s JOIN chapters c ON c.id=s.chapter_id
           WHERE c.course_id=?""", (course_id,)))}

    objectives = rows(conn.execute(
        "SELECT * FROM objectives WHERE course_id=? AND status='active'", (course_id,)))
    uncovered, stale_cov = [], []
    for o in objectives:
        links = rows(conn.execute(
            "SELECT * FROM objective_coverage WHERE objective_id=?", (o["id"],)))
        live = [l for l in links if l["segment_id"] in seg_alive]
        if not live:
            uncovered.append({
                "objective_id": o["id"], "code": o["code"],
                "title": o["title"], "revision": o["revision"],
            })
        elif any(l["stale"] for l in live):
            stale_cov.append({
                "objective_id": o["id"], "code": o["code"],
                "title": o["title"], "revision": o["revision"],
            })

    repairs = rows(conn.execute(
        """SELECT b.*, s.title AS segment_title FROM demo_bindings b
           JOIN segments s ON s.id=b.segment_id
           JOIN chapters c ON c.id=s.chapter_id
           WHERE c.course_id=? AND b.status='pending_repair'""", (course_id,)))

    skew = rows(conn.execute(
        """SELECT s.id AS segment_id, s.title,
                  GROUP_CONCAT(b.role || '@v' || b.asset_version) AS bindings
           FROM demo_bindings b
           JOIN segments s ON s.id=b.segment_id
           JOIN chapters c ON c.id=s.chapter_id
           WHERE c.course_id=?
           GROUP BY s.id HAVING COUNT(DISTINCT b.asset_version) > 1""", (course_id,)))

    total_minutes = round(sum(c["minutes"] for c in chapters), 2)
    ok = not (issues or uncovered or stale_cov or repairs or skew)
    return {
        "ok": ok,
        "prereq_issues": issues,
        "uncovered_objectives": uncovered,
        "stale_coverage": stale_cov,
        "pending_repairs": repairs,
        "version_skew": skew,
        "total_minutes": total_minutes,
    }


# ---------------------------------------------------------------------------
# 素材版本一致性
# ---------------------------------------------------------------------------

def recheck_segment_bindings(conn, segment_id):
    """画面/字幕/说明必须对应同一素材版本；时间码不得越界；视频必须有音轨。"""
    bs = rows(conn.execute(
        "SELECT * FROM demo_bindings WHERE segment_id=?", (segment_id,)))
    if not bs:
        return
    versions = {b["asset_version"] for b in bs}
    for b in bs:
        av = row(conn.execute(
            "SELECT * FROM asset_versions WHERE asset_id=? AND version=?",
            (b["asset_id"], b["asset_version"])))
        reasons = []
        if av is None:
            reasons.append("asset_version_missing")
        else:
            if b["out_ms"] > av["duration_ms"]:
                reasons.append("timecode_out_of_bounds")
            if b["role"] == "video" and not av["has_audio"]:
                reasons.append("audio_missing")
        if len(versions) > 1:
            reasons.append("version_mismatch")
        status = "pending_repair" if reasons else "ok"
        conn.execute(
            "UPDATE demo_bindings SET status=?, repair_reason=?, version=version+1 WHERE id=?",
            (status, ",".join(reasons) if reasons else None, b["id"]))


def recheck_asset_bindings(conn, asset_id):
    """素材替换新版本后：follow 绑定跟随到新版，随后全部重校验。"""
    asset = get_or_404(conn, "assets", asset_id)
    affected = rows(conn.execute(
        "SELECT * FROM demo_bindings WHERE asset_id=?", (asset_id,)))
    for b in affected:
        if b["pin_mode"] == "follow":
            conn.execute(
                "UPDATE demo_bindings SET asset_version=?, version=version+1 WHERE id=?",
                (asset["current_version"], b["id"]))
    for sid in {b["segment_id"] for b in affected}:
        recheck_segment_bindings(conn, sid)


# ---------------------------------------------------------------------------
# 课程快照 / 导出 / 回执 / 对账 / 冻结
# ---------------------------------------------------------------------------

def course_snapshot(conn, course_id):
    course = dict(get_or_404(conn, "courses", course_id))
    out_chapters = []
    for ch in rows(conn.execute(
            "SELECT * FROM chapters WHERE course_id=? ORDER BY position", (course_id,))):
        ch = dict(ch)
        out_segs = []
        for s in rows(conn.execute(
                "SELECT * FROM segments WHERE chapter_id=? ORDER BY position",
                (ch["id"],))):
            s = dict(s)
            s["bindings"] = [dict(b) for b in rows(conn.execute(
                "SELECT * FROM demo_bindings WHERE segment_id=?", (s["id"],)))]
            out_segs.append(s)
        ch["segments"] = out_segs
        out_chapters.append(ch)
    out_objectives = []
    for o in rows(conn.execute(
            "SELECT * FROM objectives WHERE course_id=? AND status='active'",
            (course_id,))):
        o = dict(o)
        o["coverage"] = [dict(x) for x in rows(conn.execute(
            "SELECT * FROM objective_coverage WHERE objective_id=?", (o["id"],)))]
        out_objectives.append(o)
    edges = [dict(e) for e in rows(conn.execute(
        "SELECT * FROM prereq_edges WHERE course_id=?", (course_id,)))]
    return {"course": course, "chapters": out_chapters,
            "objectives": out_objectives, "prereq_edges": edges}


def receipt_no_for(pkg):
    return f"R{pkg['id']:06d}-v{pkg['course_version']}"


def create_export(conn, course_id, mode, idem_key, actor):
    """幂等：同一 idempotency_key 返回同一作业，配额只扣一次。"""
    course = get_or_404(conn, "courses", course_id)
    ws = course["workspace_id"]
    existing = row(conn.execute(
        "SELECT * FROM packages WHERE idempotency_key=?", (idem_key,)))
    if existing:
        return existing, False

    cur = conn.execute(
        "UPDATE workspace_quota SET exports_used=exports_used+1 "
        "WHERE workspace_id=? AND exports_used < exports_limit", (ws,))
    if cur.rowcount == 0:
        raise Forbidden("导出额度已用尽")

    now = utcnow()
    cur = conn.execute(
        """INSERT INTO packages(course_id, course_version, mode, status, idempotency_key,
                                requested_by, created_at)
           VALUES(?,?,?,'queued',?,?,?)""",
        (course_id, course["version"], mode, idem_key, actor["id"], now))
    pkg_id = cur.lastrowid
    conn.execute(
        """INSERT INTO jobs(kind, payload, status, run_after, idempotency_key, created_at)
           VALUES('build_package', ?, 'queued', 0, ?, ?)""",
        (json.dumps({"package_id": pkg_id}), f"export:{idem_key}", now))
    return get_or_404(conn, "packages", pkg_id), True


def build_package(conn, pkg, artifacts_dir):
    """工作器：生成课件包 + 回执，同一事务提交，避免‘导出成功但回执丢失’。"""
    snap = course_snapshot(conn, pkg["course_id"])
    artifact = {
        "package_id": pkg["id"],
        "mode": pkg["mode"],
        "course_version": pkg["course_version"],
        "generated_at": utcnow(),
        "content": snap,
    }
    os.makedirs(artifacts_dir, exist_ok=True)
    path = os.path.join(artifacts_dir, f"package_{pkg['id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False, indent=1)

    conn.execute(
        """INSERT INTO export_receipts(package_id, receipt_no, issued_at)
           VALUES(?,?,?) ON CONFLICT(package_id) DO NOTHING""",
        (pkg["id"], receipt_no_for(pkg), utcnow()))
    conn.execute(
        "UPDATE packages SET status='done', artifact_uri=?, finished_at=? WHERE id=?",
        (path, utcnow(), pkg["id"]))


def reconcile_receipts(conn, workspace_id):
    """对账：找出‘已完成但无回执’的包并补发（回执号确定性生成，幂等）。"""
    missing = rows(conn.execute(
        """SELECT p.* FROM packages p
           JOIN courses c ON c.id=p.course_id
           LEFT JOIN export_receipts r ON r.package_id=p.id
           WHERE c.workspace_id=? AND p.status='done' AND r.id IS NULL""",
        (workspace_id,)))
    fixed = []
    for p in missing:
        conn.execute(
            """INSERT OR IGNORE INTO export_receipts(package_id, receipt_no, issued_at)
               VALUES(?,?,?)""",
            (p["id"], receipt_no_for(p), utcnow()))
        fixed.append({"package_id": p["id"], "receipt_no": receipt_no_for(p)})
    return fixed


def start_class(conn, course_id, name, package_id, actor):
    """开班冻结：课件包固化为 pinned/frozen=1，模板升级不再影响学员内容。"""
    pkg = get_or_404(conn, "packages", package_id)
    if pkg["course_id"] != course_id:
        raise BadRequest("课件包不属于该课程")
    if pkg["status"] != "done":
        raise BadRequest("课件包尚未构建完成")

    conn.execute(
        "UPDATE packages SET frozen=1, mode='pinned', stale=0 WHERE id=?", (package_id,))
    cur = conn.execute(
        "INSERT INTO classes(course_id, name, package_id, status, started_at) "
        "VALUES(?,?,?,'active',?)",
        (course_id, name, package_id, utcnow()))
    course = get_or_404(conn, "courses", course_id)
    log_revision(conn, course["workspace_id"], course_id, "class", cur.lastrowid, 1,
                 "create", actor,
                 after={"name": name, "package_id": package_id})
    return get_or_404(conn, "classes", cur.lastrowid)


# ---------------------------------------------------------------------------
# 受限撤销
# ---------------------------------------------------------------------------

def undo_revision(conn, revision_id, actor):
    """受限撤销：仅当实体仍停留在该修订版本时才可回退；
    同事在此之后的后续修订会使版本前移，撤销以 409 拒绝，绝不回退他人工作。"""
    r = get_or_404(conn, "revisions", revision_id)
    et = r["entity_type"]
    if et not in UNDO_TABLES:
        raise BadRequest(f"实体类型 {et} 不支持撤销")
    table = UNDO_TABLES[et]
    eid = r["entity_id"]
    before = json.loads(r["before_json"]) if r["before_json"] else None
    current = row(conn.execute(f"SELECT * FROM {table} WHERE id=?", (eid,)))

    if r["action"] == "create":
        if current is None:
            raise Conflict("撤销冲突：实体已被删除", None)
        if current["version"] != r["version"]:
            raise Conflict("撤销冲突：该修订之后已有后续修改，不能回退同事的工作", current)
        if et == "chapter":
            segs = [x["id"] for x in rows(conn.execute(
                "SELECT id FROM segments WHERE chapter_id=?", (eid,)))]
            for sid in segs:
                conn.execute("DELETE FROM demo_bindings WHERE segment_id=?", (sid,))
                conn.execute("DELETE FROM objective_coverage WHERE segment_id=?", (sid,))
            conn.execute("DELETE FROM segments WHERE chapter_id=?", (eid,))
            conn.execute(
                "DELETE FROM prereq_edges WHERE chapter_id=? OR requires_chapter_id=?",
                (eid, eid))
        elif et == "segment":
            conn.execute("DELETE FROM demo_bindings WHERE segment_id=?", (eid,))
            conn.execute("DELETE FROM objective_coverage WHERE segment_id=?", (eid,))
        conn.execute(f"DELETE FROM {table} WHERE id=? AND version=?",
                     (eid, r["version"]))

    elif r["action"] == "update":
        if current is None:
            raise Conflict("撤销冲突：实体已被删除", None)
        if current["version"] != r["version"]:
            raise Conflict("撤销冲突：该修订之后已有后续修改，不能回退同事的工作", current)
        fields = {k: v for k, v in before.items() if k not in ("id", "version")}
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE {table} SET {sets}, version=version+1 WHERE id=? AND version=?",
            [*fields.values(), eid, r["version"]])

    elif r["action"] == "delete":
        if current is not None:
            raise Conflict("撤销冲突：实体已存在（可能被同事重建）", current)
        if et == "chapter":
            restore_chapter(conn, before)
        else:
            cols = list(before.keys())
            conn.execute(
                f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                [before[k] for k in cols])
    else:
        raise BadRequest(f"动作 {r['action']} 不支持撤销")

    course_id = r["course_id"]
    if course_id:
        bump_course(conn, course_id)

    if current is not None:
        ver = current["version"] + 1
    elif et == "chapter" and isinstance(before, dict) and "chapter" in before:
        ver = before["chapter"].get("version", 1)
    elif isinstance(before, dict):
        ver = before.get("version", 1)
    else:
        ver = 1

    log_revision(conn, r["workspace_id"], course_id, et, eid, ver, "undo", actor,
                 after={"undone_revision": revision_id})
    return {"undone_revision": revision_id, "entity_type": et, "entity_id": eid}
