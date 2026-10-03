# -*- coding: utf-8 -*-
'''HTTP API：stdlib http.server + 微型路由。认证用 X-User-Id 头（演示用）。
写操作要求 active 成员且角色>=editor；读操作（含历史）对离职成员仍开放。'''
import json
import re

from . import services

ROUTES = []


def route(method, pattern, write=False):
    rx = re.compile(re.sub(r'\{(\w+)\}', r'(?P<\1>[^/]+)', '^' + pattern) + '$')

    def deco(fn):
        ROUTES.append((method, rx, fn, write))
        return fn
    return deco


class Ctx:
    def __init__(self, db, conn, user, body, query):
        self.db = db
        self.conn = conn
        self.user = user
        self.body = body or {}
        self.query = query or {}


def require(body, key):
    if key not in body or body[key] in (None, ''):
        raise services.BadRequest('缺少字段 %s' % key)
    return body[key]


def _qrev(v):
    return int(v) if v not in (None, '') else None


def get_user(conn, uid):
    u = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        raise services.Unauthorized('用户 #%s 不存在' % uid)
    return dict(u)


# ---------------- 团队 / 用户 / 额度 ----------------
@route('POST', '/teams')
def create_team(ctx):
    name = require(ctx.body, 'name')
    cur = ctx.conn.execute('INSERT INTO teams(name) VALUES(?)', (name,))
    tid = cur.lastrowid
    ctx.conn.execute('INSERT INTO quotas(team_id, export_remaining) VALUES(?,?)', (tid, 20))
    return 201, {'id': tid, 'name': name}


@route('POST', '/teams/{tid}/users')
def create_user(ctx, tid):
    name = require(ctx.body, 'name')
    role = ctx.body.get('role', 'editor')
    if role not in ('admin', 'editor', 'viewer'):
        raise services.BadRequest('role 必须为 admin/editor/viewer')
    cur = ctx.conn.execute('INSERT INTO users(team_id,name,role) VALUES(?,?,?)',
                          (tid, name, role))
    row = ctx.conn.execute('SELECT * FROM users WHERE id=?', (cur.lastrowid,)).fetchone()
    return 201, {'users': dict(row)}


@route('GET', '/teams/{tid}/users')
def list_users(ctx, tid):
    rows = ctx.conn.execute('SELECT * FROM users WHERE team_id=? ORDER BY id', (tid,)).fetchall()
    return 200, {'users': [dict(r) for r in rows]}


@route('GET', '/teams/{tid}/quota')
def get_quota(ctx, tid):
    r = ctx.conn.execute('SELECT export_remaining FROM quotas WHERE team_id=?',
                         (tid,)).fetchone()
    if not r:
        raise services.NotFound('团队额度不存在')
    return 200, {'export_remaining': r['export_remaining']}


@route('GET', '/teams/{tid}/courses')
def list_courses(ctx, tid):
    rows = ctx.conn.execute('SELECT * FROM courses WHERE team_id=? ORDER BY id', (tid,)).fetchall()
    return 200, {'courses': [dict(r) for r in rows]}


@route('GET', '/teams/{tid}/assets')
def list_assets(ctx, tid):
    out = []
    for a in ctx.conn.execute('SELECT * FROM assets WHERE team_id=? ORDER BY id', (tid,)):
        d = dict(a)
        d['versions'] = [dict(v) for v in ctx.conn.execute(
            'SELECT * FROM asset_versions WHERE asset_id=? ORDER BY version', (a['id'],))]
        out.append(d)
    return 200, {'assets': out}


@route('POST', '/users/{uid}/leave', write=True)
def leave_team(ctx, uid):
    uid = int(uid)
    if ctx.user['id'] != uid and ctx.user['role'] != 'admin':
        raise services.Forbidden('仅本人或管理员可办理离开团队')
    services.get_or_404(ctx.conn, 'users', uid)
    ctx.conn.execute('UPDATE users SET active=0 WHERE id=?', (uid,))
    return 200, {'id': uid, 'active': 0,
                 'message': '已离开团队：写权限关闭，历史仍可按权限查看'}


# ---------------- 课程 / 目标 / 章节 ----------------
@route('POST', '/courses')
def create_course(ctx):
    tid = require(ctx.body, 'team_id')
    title = require(ctx.body, 'title')
    services.get_or_404(ctx.conn, 'teams', tid)
    row = services.create_entity(ctx.conn, 'courses', ctx.user['id'],
                                 {'team_id': tid, 'title': title, 'status': 'draft'})
    cid = row['id']
    ctx.conn.execute("UPDATE revisions SET course_id=? WHERE entity='courses' AND entity_id=?",
                     (cid, cid))
    return 201, {'course': row}


def course_tree(conn, cid):
    course = services.get_or_404(conn, 'courses', cid)
    course = dict(course)
    course['head_rev'] = services.get_head(conn, 'courses', cid)
    objectives = [dict(r) for r in conn.execute(
        'SELECT * FROM objectives WHERE course_id=? ORDER BY id', (cid,))]
    chapters = []
    for ch in conn.execute(
            'SELECT * FROM chapters WHERE course_id=? ORDER BY position, id', (cid,)):
        c = dict(ch)
        c['requires'] = json.loads(c['requires'] or '[]')
        c['head_rev'] = services.get_head(conn, 'chapters', c['id'])
        segs = []
        for sg in conn.execute(
                'SELECT * FROM segments WHERE chapter_id=? ORDER BY position, id', (c['id'],)):
            s = dict(sg)
            s['timecodes'] = json.loads(s['timecodes'] or '[]')
            s['head_rev'] = services.get_head(conn, 'segments', s['id'])
            if s['asset_version_id']:
                av = conn.execute('SELECT * FROM asset_versions WHERE id=?',
                                  (s['asset_version_id'],)).fetchone()
                if av:
                    s['asset_version'] = dict(av)
            segs.append(s)
        c['segments'] = segs
        chapters.append(c)
    aps = [dict(r) for r in conn.execute(
        'SELECT * FROM assessment_points WHERE course_id=? ORDER BY id', (cid,))]
    classes = [dict(r) for r in conn.execute(
        'SELECT * FROM classes WHERE course_id=? ORDER BY id', (cid,))]
    return {'course': course, 'objectives': objectives, 'chapters': chapters,
            'assessment_points': aps, 'classes': classes}


@route('GET', '/courses/{cid}')
def get_course(ctx, cid):
    return 200, course_tree(ctx.conn, int(cid))


@route('PUT', '/courses/{cid}', write=True)
def update_course(ctx, cid):
    changes = {k: ctx.body[k] for k in ('title', 'status') if k in ctx.body}
    row = services.update_entity(ctx.conn, 'courses', int(cid), ctx.user['id'],
                                 ctx.body.get('expected_rev'), changes, course_id=int(cid))
    return 200, {'course': row}


@route('POST', '/courses/{cid}/objectives', write=True)
def create_objective(ctx, cid):
    cid = int(cid)
    services.get_or_404(ctx.conn, 'courses', cid)
    row = services.create_entity(
        ctx.conn, 'objectives', ctx.user['id'],
        {'course_id': cid, 'code': require(ctx.body, 'code'),
         'title': require(ctx.body, 'title')}, course_id=cid)
    return 201, {'objective': row}


@route('PUT', '/objectives/{oid}', write=True)
def update_objective(ctx, oid):
    old = services.get_or_404(ctx.conn, 'objectives', oid)
    changes = {k: ctx.body[k] for k in ('code', 'title') if k in ctx.body}
    row = services.update_entity(ctx.conn, 'objectives', oid, ctx.user['id'],
                                 ctx.body.get('expected_rev'), changes,
                                 course_id=old['course_id'])
    return 200, {'objective': row}


def check_requires(conn, course_id, requires, self_id=None):
    if not isinstance(requires, list):
        raise services.BadRequest('requires 必须为章节 id 数组')
    for rid in requires:
        if self_id is not None and rid == self_id:
            raise services.BadRequest('章节不能以前置依赖引用自身')
        ok = conn.execute('SELECT id FROM chapters WHERE id=? AND course_id=?',
                          (rid, course_id)).fetchone()
        if not ok:
            raise services.BadRequest('前置章节 #%s 不在本课程中' % rid)


@route('POST', '/courses/{cid}/chapters', write=True)
def create_chapter(ctx, cid):
    cid = int(cid)
    services.get_or_404(ctx.conn, 'courses', cid)
    requires = ctx.body.get('requires', [])
    check_requires(ctx.conn, cid, requires)
    pos = ctx.conn.execute(
        'SELECT COALESCE(MAX(position),0)+1 p FROM chapters WHERE course_id=?',
        (cid,)).fetchone()['p']
    row = services.create_entity(
        ctx.conn, 'chapters', ctx.user['id'],
        {'course_id': cid, 'title': require(ctx.body, 'title'), 'position': pos,
         'requires': json.dumps(requires)}, course_id=cid)
    row['requires'] = requires
    return 201, {'chapter': row}


@route('PUT', '/chapters/{chid}', write=True)
def update_chapter(ctx, chid):
    old = services.get_or_404(ctx.conn, 'chapters', chid)
    cid = old['course_id']
    changes = {}
    if 'title' in ctx.body:
        changes['title'] = ctx.body['title']
    if 'requires' in ctx.body:
        check_requires(ctx.conn, cid, ctx.body['requires'], self_id=chid)
        changes['requires'] = json.dumps(ctx.body['requires'])
    row = services.update_entity(ctx.conn, 'chapters', chid, ctx.user['id'],
                                 ctx.body.get('expected_rev'), changes, course_id=cid)
    row['requires'] = json.loads(row['requires'] or '[]')
    return 200, {'chapter': row}


@route('DELETE', '/chapters/{chid}', write=True)
def delete_chapter(ctx, chid):
    chid = int(chid)
    old = services.get_or_404(ctx.conn, 'chapters', chid)
    cid = old['course_id']
    services._check_expected(ctx.conn, 'chapters', chid, _qrev(ctx.query.get('expected_rev')))
    for sg in ctx.conn.execute('SELECT id FROM segments WHERE chapter_id=?', (chid,)).fetchall():
        services.delete_entity(ctx.conn, 'segments', sg['id'], ctx.user['id'],
                               services.get_head(ctx.conn, 'segments', sg['id']),
                               course_id=cid)
    services.record_revision(ctx.conn, 'chapters', chid, ctx.user['id'], 'delete',
                             dict(old), None, cid)
    ctx.conn.execute('DELETE FROM chapters WHERE id=?', (chid,))
    issues = services.validate_course(ctx.conn, cid)
    tree = course_tree(ctx.conn, cid)
    return 200, {'validation': issues, 'chapters': tree['chapters'], 'coverage':
                 services.coverage_report(ctx.conn, cid)}


@route('POST', '/courses/{cid}/chapters/reorder', write=True)
def reorder_chapters(ctx, cid):
    cid = int(cid)
    services.get_or_404(ctx.conn, 'courses', cid)
    order = require(ctx.body, 'order')
    ids = [int(x) for x in order]
    existing = {r['id'] for r in ctx.conn.execute(
        'SELECT id FROM chapters WHERE course_id=?', (cid,)).fetchall()}
    if set(ids) != existing or len(ids) != len(existing):
        raise services.BadRequest('order 必须恰好包含本课程全部章节 id（乐观校验）')
    for pos, chid in enumerate(ids, start=1):
        before = dict(ctx.conn.execute('SELECT * FROM chapters WHERE id=?', (chid,)).fetchone())
        ctx.conn.execute('UPDATE chapters SET position=? WHERE id=?', (pos, chid))
        after = dict(ctx.conn.execute('SELECT * FROM chapters WHERE id=?', (chid,)).fetchone())
        services.record_revision(ctx.conn, 'chapters', chid, ctx.user['id'], 'update',
                                 before, after, cid)
    issues = services.validate_course(ctx.conn, cid)
    tree = course_tree(ctx.conn, cid)
    return 200, {'validation': issues, 'chapters': tree['chapters']}


# ---------------- 片段（台词/演示/练习） ----------------
@route('POST', '/chapters/{chid}/segments', write=True)
def create_segment(ctx, chid):
    ch = services.get_or_404(ctx.conn, 'chapters', chid)
    kind = require(ctx.body, 'kind')
    if kind not in ('lecture', 'demo', 'exercise'):
        raise services.BadRequest('kind 必须为 lecture(讲师台词)/demo(操作演示)/exercise(练习提示)')
    pos = ctx.conn.execute(
        'SELECT COALESCE(MAX(position),0)+1 p FROM segments WHERE chapter_id=?',
        (chid,)).fetchone()['p']
    row = services.create_entity(
        ctx.conn, 'segments', ctx.user['id'],
        {'chapter_id': chid, 'kind': kind, 'title': require(ctx.body, 'title'),
         'script': ctx.body.get('script', ''), 'position': pos,
         'status': 'ok', 'timecodes': '[]'}, course_id=ch['course_id'])
    row['timecodes'] = []
    return 201, {'segment': row}


@route('PUT', '/segments/{sid}', write=True)
def update_segment(ctx, sid):
    old = services.get_or_404(ctx.conn, 'segments', sid)
    cid = services.course_of_chapter(ctx.conn, old['chapter_id'])
    changes = {k: ctx.body[k] for k in ('title', 'script') if k in ctx.body}
    row = services.update_entity(ctx.conn, 'segments', sid, ctx.user['id'],
                                 ctx.body.get('expected_rev'), changes, course_id=cid)
    return 200, {'segment': row}


@route('DELETE', '/segments/{sid}', write=True)
def delete_segment(ctx, sid):
    sid = int(sid)
    old = services.get_or_404(ctx.conn, 'segments', sid)
    cid = services.course_of_chapter(ctx.conn, old['chapter_id'])
    services.delete_entity(ctx.conn, 'segments', sid, ctx.user['id'],
                           ctx.query.get('expected_rev'), course_id=cid)
    issues = services.validate_course(ctx.conn, cid)
    return 200, {'validation': issues, 'coverage': services.coverage_report(ctx.conn, cid)}


def _bind_or_timecodes(ctx, sid, changes):
    sid = int(sid)
    old = services.get_or_404(ctx.conn, 'segments', sid)
    cid = services.course_of_chapter(ctx.conn, old['chapter_id'])
    if 'asset_version_id' in changes and old['kind'] != 'demo':
        raise services.BadRequest('仅操作演示(demo)可绑定素材版本')
    if changes.get('asset_version_id'):
        av = services.get_or_404(ctx.conn, 'asset_versions', changes['asset_version_id'])
        asset = services.get_or_404(ctx.conn, 'assets', av['asset_id'])
        course = services.get_or_404(ctx.conn, 'courses', cid)
        if asset['team_id'] != course['team_id']:
            raise services.Forbidden('素材属于其他团队')
    row = services.update_entity(ctx.conn, 'segments', sid, ctx.user['id'],
                                 ctx.body.get('expected_rev'), changes, course_id=cid)
    services.refresh_repair_state(ctx.conn, sid)
    row = dict(ctx.conn.execute('SELECT * FROM segments WHERE id=?', (sid,)).fetchone())
    row['timecodes'] = json.loads(row['timecodes'] or '[]')
    row['head_rev'] = services.get_head(ctx.conn, 'segments', sid)
    issues = services.validate_course(ctx.conn, cid)
    return 200, {'segment': row, 'validation': issues,
                 'coverage': services.coverage_report(ctx.conn, cid)}


@route('POST', '/segments/{sid}/bind_asset', write=True)
def bind_asset(ctx, sid):
    return _bind_or_timecodes(ctx, int(sid),
                              {'asset_version_id': require(ctx.body, 'asset_version_id')})


@route('POST', '/segments/{sid}/timecodes', write=True)
def set_timecodes(ctx, sid):
    tcs = ctx.body.get('timecodes')
    if not isinstance(tcs, list):
        raise services.BadRequest('timecodes 必须为数组')
    for tc in tcs:
        if not isinstance(tc.get('at_ms'), int) or tc['at_ms'] < 0:
            raise services.BadRequest('timecodes[].at_ms 必须为非负整数')
        if 'label' not in tc:
            raise services.BadRequest('timecodes[].label 缺失')
    return _bind_or_timecodes(ctx, int(sid),
                              {'timecodes': json.dumps(tcs, ensure_ascii=False)})


# ---------------- 考核点 ----------------
@route('POST', '/courses/{cid}/assessments', write=True)
def create_assessment(ctx, cid):
    cid = int(cid)
    services.get_or_404(ctx.conn, 'courses', cid)
    oid = require(ctx.body, 'objective_id')
    sid = require(ctx.body, 'segment_id')
    if not ctx.conn.execute('SELECT 1 FROM objectives WHERE id=? AND course_id=?',
                            (oid, cid)).fetchone():
        raise services.BadRequest('目标不属于本课程')
    seg = services.get_or_404(ctx.conn, 'segments', sid)
    ch = services.get_or_404(ctx.conn, 'chapters', seg['chapter_id'])
    if ch['course_id'] != cid:
        raise services.BadRequest('片段不属于本课程')
    row = services.create_entity(
        ctx.conn, 'assessment_points', ctx.user['id'],
        {'course_id': cid, 'objective_id': oid, 'segment_id': sid,
         'note': ctx.body.get('note', '')}, course_id=cid)
    return 201, row


@route('DELETE', '/assessments/{aid}', write=True)
def delete_assessment(ctx, aid):
    old = services.get_or_404(ctx.conn, 'assessment_points', aid)
    services.delete_entity(ctx.conn, 'assessment_points', aid, ctx.user['id'],
                           ctx.query.get('expected_rev'), course_id=old['course_id'])
    return 200, {'id': aid, 'deleted': True}


# ---------------- 素材 ----------------
@route('POST', '/teams/{tid}/assets', write=True)
def create_asset(ctx, tid):
    services.get_or_404(ctx.conn, 'teams', tid)
    cur = ctx.conn.execute('INSERT INTO assets(team_id,title) VALUES(?,?)',
                           (tid, require(ctx.body, 'title')))
    row = ctx.conn.execute('SELECT * FROM assets WHERE id=?', (cur.lastrowid,)).fetchone()
    return 201, dict(row)


@route('POST', '/assets/{aid}/versions', write=True)
def create_asset_version(ctx, aid):
    services.get_or_404(ctx.conn, 'assets', aid)
    v = ctx.conn.execute('SELECT COALESCE(MAX(version),0)+1 v FROM asset_versions WHERE asset_id=?',
                         (aid,)).fetchone()['v']
    cur = ctx.conn.execute(
        'INSERT INTO asset_versions(asset_id,version,video_uri,subtitle_uri,notes_uri,'
        'duration_ms,has_audio) VALUES(?,?,?,?,?,?,?)',
        (aid, v, ctx.body.get('video_uri'), ctx.body.get('subtitle_uri'),
         ctx.body.get('notes_uri'), ctx.body.get('duration_ms', 0),
         1 if ctx.body.get('has_audio', True) else 0))
    row = ctx.conn.execute('SELECT * FROM asset_versions WHERE id=?',
                           (cur.lastrowid,)).fetchone()
    return 201, dict(row)


# ---------------- 校验 / 覆盖 / 历史 / 影响 ----------------
@route('GET', '/courses/{cid}/validation')
def get_validation(ctx, cid):
    services.get_or_404(ctx.conn, 'courses', int(cid))
    return 200, {'issues': services.validate_course(ctx.conn, int(cid))}


@route('GET', '/courses/{cid}/coverage')
def get_coverage(ctx, cid):
    cid = int(cid)
    services.get_or_404(ctx.conn, 'courses', cid)
    return 200, {'coverage': services.coverage_report(ctx.conn, cid)}


@route('GET', '/courses/{cid}/history')
def get_history(ctx, cid):
    services.get_or_404(ctx.conn, 'courses', int(cid))
    rows = ctx.conn.execute(
        'SELECT r.*, u.name actor_name FROM revisions r LEFT JOIN users u ON u.id=r.actor_id '
        'WHERE r.course_id=? ORDER BY r.id DESC LIMIT 200', (cid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ('before', 'after'):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except Exception:
                    pass
        out.append(d)
    return 200, {'revisions': out}


@route('GET', '/courses/{cid}/packages')
def list_packages(ctx, cid):
    services.get_or_404(ctx.conn, 'courses', int(cid))
    rows = [services.package_view(dict(r)) for r in ctx.conn.execute(
        'SELECT * FROM packages WHERE course_id=? ORDER BY version DESC', (cid,))]
    return 200, {'packages': rows}


@route('GET', '/courses/{cid}/impact')
def course_impact(ctx, cid):
    cid = int(cid)
    services.get_or_404(ctx.conn, 'courses', cid)
    latest = ctx.conn.execute(
        'SELECT * FROM packages WHERE course_id=? ORDER BY version DESC LIMIT 1',
        (cid,)).fetchone()
    out = []
    for cl in ctx.conn.execute('SELECT * FROM classes WHERE course_id=? AND started=1',
                               (cid,)).fetchall():
        d = dict(cl)
        if cl['mode'] == 'pinned':
            d['package_id'] = cl['pinned_package_id']
            d['effect'] = ('frozen: 固定复用课件包 #%s，模板升级不影响已开班内容'
                           % cl['pinned_package_id'])
        else:
            d['package_id'] = latest['id'] if latest else None
            d['effect'] = ('follow: 模块跟随更新，当前解析到课件包 #%s' % latest['id']
                           if latest else
                           'follow: 模块跟随更新，当前解析到课件包 无（下次导出后自动指向新版本）')
        out.append(d)
    return 200, {'classes': out}


# ---------------- 导出 / 作业 / 班级 ----------------
@route('POST', '/courses/{cid}/export', write=True)
def export_course(ctx, cid):
    cid = int(cid)
    course = services.get_or_404(ctx.conn, 'courses', cid)
    key = require(ctx.body, 'idempotency_key')
    job, dedup = services.create_export_job(ctx.conn, course['team_id'], cid, key)
    job['deduplicated'] = dedup
    return 200, {'job': services.job_view(job), 'deduplicated': dedup}


@route('GET', '/jobs/{jid}')
def get_job(ctx, jid):
    job = services.get_or_404(ctx.conn, 'jobs', int(jid))
    return 200, {'job': services.job_view(dict(job))}


@route('GET', '/jobs/{jid}/receipt')
def get_receipt(ctx, jid):
    job = services.get_or_404(ctx.conn, 'jobs', int(jid))
    if not job['receipt']:
        raise services.Conflict('回执尚未生成（作业未完成）')
    return 200, {'receipt': json.loads(job['receipt'])}


@route('POST', '/courses/{cid}/classes', write=True)
def create_class(ctx, cid):
    cid = int(cid)
    services.get_or_404(ctx.conn, 'courses', cid)
    mode = ctx.body.get('mode', 'pinned')
    if mode not in ('pinned', 'follow'):
        raise services.BadRequest('mode 必须为 pinned(固定课件版本复用) 或 follow(模块跟随更新)')
    pinned = None
    if mode == 'pinned':
        pid = ctx.body.get('package_id')
        if pid:
            pkg = ctx.conn.execute('SELECT * FROM packages WHERE id=?', (pid,)).fetchone()
            if not pkg or pkg['course_id'] != cid:
                raise services.BadRequest('课件包不属于本课程')
            pinned = pid
        else:
            latest = ctx.conn.execute(
                'SELECT id FROM packages WHERE course_id=? ORDER BY version DESC LIMIT 1',
                (cid,)).fetchone()
            if not latest:
                raise services.BadRequest('尚无课件包：请先导出，再以 pinned 模式开班')
            pinned = latest['id']
    cur = ctx.conn.execute(
        'INSERT INTO classes(course_id,name,mode,pinned_package_id,started,created_at) '
        'VALUES(?,?,?,?,1,?)',
        (cid, require(ctx.body, 'name'), mode, pinned, services.now()))
    row = ctx.conn.execute('SELECT * FROM classes WHERE id=?', (cur.lastrowid,)).fetchone()
    return 201, dict(row)


@route('GET', '/classes/{clid}/package')
def class_package(ctx, clid):
    cl = services.get_or_404(ctx.conn, 'classes', int(clid))
    if cl['mode'] == 'pinned':
        pkg = services.get_or_404(ctx.conn, 'packages', cl['pinned_package_id'])
    else:
        pkg = ctx.conn.execute(
            'SELECT * FROM packages WHERE course_id=? ORDER BY version DESC LIMIT 1',
            (cl['course_id'],)).fetchone()
        if not pkg:
            raise services.Conflict('尚无课件包')
    return 200, {'package': services.package_view(dict(pkg))}


@route('POST', '/revisions/{rid}/undo', write=True)
def undo(ctx, rid):
    return 200, services.undo_revision(ctx.conn, int(rid), ctx.user['id'])


# ---------------- 分发 ----------------
def dispatch(db, method, raw_path, headers, raw_body):
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(raw_path)
    path = parsed.path
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    body = {}
    if raw_body:
        try:
            body = json.loads(raw_body.decode('utf-8'))
        except Exception:
            return 400, {'error': 'bad_request', 'detail': '请求体不是合法 JSON'}

    for m, rx, fn, is_write in ROUTES:
        if m != method:
            continue
        match = rx.match(path)
        if not match:
            continue
        uid = headers.get('X-User-Id')
        anon = getattr(fn, '_anon', False)
        user = None
        if not anon:
            if not uid:
                return 401, {'error': 'unauthorized', 'detail': '缺少 X-User-Id 请求头'}
        kwargs = {k: v for k, v in match.groupdict().items()}
        try:
            with db.lock:
                if uid:
                    user = get_user(db.conn, int(uid))
                if is_write and not anon:
                    if not user['active']:
                        raise services.Forbidden('member_inactive: 成员已离开团队，仅可只读查看')
                    if user['role'] == 'viewer':
                        raise services.Forbidden('角色权限不足')
            if is_write:
                with db.txn() as conn:
                    ctx = Ctx(db, conn, user, body, query)
                    status, resp = fn(ctx, **kwargs)
            else:
                with db.lock:
                    ctx = Ctx(db, db.conn, user, body, query)
                    status, resp = fn(ctx, **kwargs)
            return status, resp
        except services.Unauthorized as e:
            return 401, {'error': 'unauthorized', 'detail': str(e)}
        except services.Forbidden as e:
            return 403, {'error': 'forbidden', 'detail': str(e)}
        except services.NotFound as e:
            return 404, {'error': 'not_found', 'detail': str(e)}
        except services.Conflict as e:
            return 409, {'error': 'conflict', 'detail': str(e)}
        except services.BadRequest as e:
            return 400, {'error': 'bad_request', 'detail': str(e)}
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            return 500, {'error': 'internal', 'detail': str(e)}
    return 404, {'error': 'no_route', 'detail': '未匹配路由: %s %s' % (method, path)}


# 匿名（无需登录）端点：建团队 / 建用户（演示环境引导用）
for _m, _rx, _fn, _w in ROUTES:
    if (_m, _rx.pattern) in (('POST', r'^/teams$'),
                             ('POST', r'^/teams/(?P<tid>[^/]+)/users$')):
        _fn._anon = True
