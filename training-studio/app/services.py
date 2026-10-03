# -*- coding: utf-8 -*-
'''领域服务：版本条件并发、字段级撤销、前置关系重验证、素材版本一致性、
目标覆盖报告、幂等导出作业与额度、已开班冻结规则。'''
import hashlib
import json
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Forbidden(Exception):
    pass


class BadRequest(Exception):
    pass


class Unauthorized(Exception):
    pass


def now():
    return datetime.now(CST).isoformat(timespec='seconds')


def jloads(s, default):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default


def get_or_404(conn, table, eid):
    row = conn.execute('SELECT * FROM %s WHERE id=?' % table, (eid,)).fetchone()
    if not row:
        raise NotFound('%s #%s 不存在' % (table, eid))
    return row


def get_head(conn, entity, eid):
    r = conn.execute('SELECT head_rev FROM entity_heads WHERE entity=? AND entity_id=?',
                     (entity, eid)).fetchone()
    return r['head_rev'] if r else 0


def record_revision(conn, entity, eid, actor_id, action, before, after,
                    course_id=None, undoes=None):
    rev = get_head(conn, entity, eid) + 1
    conn.execute(
        'INSERT INTO revisions(entity,entity_id,rev,actor_id,action,before,after,'
        'course_id,undoes,ts) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (entity, eid, rev, actor_id, action,
         json.dumps(before, ensure_ascii=False) if before is not None else None,
         json.dumps(after, ensure_ascii=False) if after is not None else None,
         course_id, undoes, now()))
    conn.execute(
        'INSERT INTO entity_heads(entity,entity_id,head_rev) VALUES(?,?,?) '
        'ON CONFLICT(entity,entity_id) DO UPDATE SET head_rev=excluded.head_rev',
        (entity, eid, rev))
    return rev


def _rowdict(r):
    return {k: r[k] for k in r.keys()} if r is not None else None


def create_entity(conn, table, actor_id, fields, course_id=None):
    cols = ','.join(fields.keys())
    ph = ','.join('?' for _ in fields)
    cur = conn.execute('INSERT INTO %s(%s) VALUES(%s)' % (table, cols, ph),
                       tuple(fields.values()))
    eid = cur.lastrowid
    row = _rowdict(conn.execute('SELECT * FROM %s WHERE id=?' % table, (eid,)).fetchone())
    rev = record_revision(conn, table, eid, actor_id, 'create', None, row, course_id)
    row['head_rev'] = rev
    return row


def _check_expected(conn, table, eid, expected_rev):
    if expected_rev is None:
        raise Conflict('缺少 expected_rev（版本条件），写操作必须携带当前版本号')
    expected_rev = int(expected_rev)
    head = get_head(conn, table, eid)
    if expected_rev != head:
        raise Conflict('版本冲突：提交的 expected_rev=%s，当前 head=%s，请刷新后重试'
                       % (expected_rev, head))


def update_entity(conn, table, eid, actor_id, expected_rev, changes, course_id=None):
    _check_expected(conn, table, eid, expected_rev)
    before = _rowdict(get_or_404(conn, table, eid))
    sets = ','.join('%s=?' % k for k in changes)
    conn.execute('UPDATE %s SET %s WHERE id=?' % (table, sets),
                 tuple(changes.values()) + (eid,))
    after = _rowdict(conn.execute('SELECT * FROM %s WHERE id=?' % table, (eid,)).fetchone())
    rev = record_revision(conn, table, eid, actor_id, 'update', before, after, course_id)
    after['head_rev'] = rev
    return after


def delete_entity(conn, table, eid, actor_id, expected_rev, course_id=None):
    _check_expected(conn, table, eid, expected_rev)
    before = _rowdict(get_or_404(conn, table, eid))
    conn.execute('DELETE FROM %s WHERE id=?' % table, (eid,))
    rev = record_revision(conn, table, eid, actor_id, 'delete', before, None, course_id)
    return rev


def undo_revision(conn, rid, actor_id):
    '''字段级补偿撤销：只回退该修订引入、且此后未被他人再改动的字段；
    已被同事后续修订覆盖的字段保持现值（skipped），绝不回退他人工作。'''
    r = conn.execute('SELECT * FROM revisions WHERE id=?', (rid,)).fetchone()
    if not r:
        raise NotFound('revision #%s 不存在' % rid)
    entity, eid, action = r['entity'], r['entity_id'], r['action']
    before = jloads(r['before'], None)
    after = jloads(r['after'], None)
    course_id = r['course_id']
    current = conn.execute('SELECT * FROM %s WHERE id=?' % entity, (eid,)).fetchone()

    if action == 'create':
        if not current:
            return {'status': 'applied', 'effect': 'create_reverted',
                    'new_rev': get_head(conn, entity, eid)}  # 实体已不存在，无需撤销创建
        # 创建后若有字段被后续修订改动，则撤销创建会回退他人工作 -> 拒绝
        later = [x for x in conn.execute(
            'SELECT * FROM revisions WHERE entity=? AND entity_id=? AND id>? ORDER BY id',
            (entity, eid, rid)).fetchall()]
        if later:
            raise Conflict('创建后已存在后续修订，撤销创建将回退他人工作，已拒绝')
        conn.execute('DELETE FROM %s WHERE id=?' % entity, (eid,))
        new_rev = record_revision(conn, entity, eid, actor_id, 'undo',
                                  _rowdict(current), None, course_id, undoes=rid)
        return {'status': 'applied', 'effect': 'create_reverted', 'new_rev': new_rev}

    if action == 'delete':
        if current:
            raise Conflict('实体已被重建，不能撤销删除')
        data = dict(before)
        cols = ','.join(data.keys())
        ph = ','.join('?' for _ in data)
        conn.execute('INSERT INTO %s(%s) VALUES(%s)' % (entity, cols, ph),
                     tuple(data.values()))
        new_rev = record_revision(conn, entity, eid, actor_id, 'undo', None, before,
                                  course_id, undoes=rid)
        return {'status': 'applied', 'effect': 'delete_reverted', 'new_rev': new_rev}

    # update 撤销：字段级
    if not current:
        raise Conflict('实体已被删除，不能撤销该修订')
    # 该修订之后是否又有人改过同一字段
    later = conn.execute(
        'SELECT * FROM revisions WHERE entity=? AND entity_id=? AND id>? ORDER BY id',
        (entity, eid, rid)).fetchall()
    curd = _rowdict(current)
    applied, skipped = [], []
    changes = {}
    for k, oldv in (before or {}).items():
        if k == 'id':
            continue
        # 若后续修订把该字段改成了别的值（与该修订落地后的值不同），则跳过
        later_val = curd.get(k)
        if later_val != (after or {}).get(k):
            skipped.append(k)
            continue
        if curd.get(k) != oldv:
            changes[k] = oldv
            applied.append(k)
    if changes:
        sets = ','.join('%s=?' % k for k in changes)
        conn.execute('UPDATE %s SET %s WHERE id=?' % (entity, sets),
                     tuple(changes.values()) + (eid,))
    if changes:
        new_after = _rowdict(conn.execute('SELECT * FROM %s WHERE id=?' % entity, (eid,)).fetchone())
        new_rev = record_revision(conn, entity, eid, actor_id, 'undo', curd, new_after,
                                  course_id, undoes=rid)
    else:
        # 全部字段都被同事后续修订覆盖：无可回退内容，不生成新版本号
        new_rev = get_head(conn, entity, eid)
    status = 'applied' if applied else 'skipped'
    return {'status': status, 'applied': applied, 'skipped': skipped, 'new_rev': new_rev}


def course_of_chapter(conn, chapter_id):
    return get_or_404(conn, 'chapters', chapter_id)['course_id']


def course_of_segment(conn, segment_id):
    chid = get_or_404(conn, 'segments', segment_id)['chapter_id']
    return course_of_chapter(conn, chid)


def validate_course(conn, course_id):
    '''章节重排、删除演示、替换素材后调用：重新验证整门课，
    而不是仅更新总分钟数等派生数字。'''
    issues = []
    chapters = [dict(r) for r in conn.execute(
        'SELECT * FROM chapters WHERE course_id=? ORDER BY position, id', (course_id,))]
    by_id = {c['id']: c for c in chapters}
    for ch in chapters:
        for rid in json.loads(ch['requires'] or '[]'):
            tgt = by_id.get(rid)
            if not tgt:
                issues.append({'code': 'prerequisite_missing', 'chapter_id': ch['id'],
                               'missing_chapter_id': rid,
                               'message': '章节《%s》的前置章节 #%s 已不存在'
                                          % (ch['title'], rid)})
                continue
            if ch['position'] < tgt['position'] or (
                    ch['position'] == tgt['position'] and ch['id'] < tgt['id']):
                issues.append({'code': 'prerequisite_order', 'chapter_id': ch['id'],
                               'required_chapter_id': rid,
                               'message': '章节《%s》排在其前置章节《%s》之前'
                                          % (ch['title'], tgt['title'])})

    segs = [dict(r) for r in conn.execute(
        'SELECT s.* FROM segments s JOIN chapters c ON c.id=s.chapter_id '
        'WHERE c.course_id=?', (course_id,))]
    for s in segs:
        if s['status'] == 'pending_repair':
            issues.append({'code': 'segment_pending_repair', 'segment_id': s['id'],
                           'message': '片段《%s》处于待修复状态' % s['title']})
        if s['kind'] != 'demo':
            continue
        av = None
        if s['asset_version_id']:
            av = conn.execute('SELECT * FROM asset_versions WHERE id=?',
                              (s['asset_version_id'],)).fetchone()
        if not av:
            issues.append({'code': 'demo_asset_missing', 'segment_id': s['id'],
                           'message': '操作演示《%s》未绑定素材版本' % s['title']})
            continue
        av = dict(av)
        if not av['video_uri']:
            issues.append({'code': 'video_missing', 'segment_id': s['id'],
                           'asset_version_id': av['id'],
                           'message': '素材版本 #%s 缺少画面(video)' % av['id']})
        if not av['subtitle_uri']:
            issues.append({'code': 'subtitle_missing', 'segment_id': s['id'],
                           'asset_version_id': av['id'],
                           'message': '素材版本 #%s 缺少字幕(subtitle)' % av['id']})
        if not av['notes_uri']:
            issues.append({'code': 'notes_missing', 'segment_id': s['id'],
                           'asset_version_id': av['id'],
                           'message': '素材版本 #%s 缺少说明(notes)' % av['id']})
        if not av['has_audio']:
            issues.append({'code': 'audio_missing', 'segment_id': s['id'],
                           'asset_version_id': av['id'],
                           'message': '素材版本 #%s 音轨缺失' % av['id']})
        for tc in json.loads(s['timecodes'] or '[]'):
            at = tc.get('at_ms', 0)
            if av['duration_ms'] and at > av['duration_ms']:
                issues.append({
                    'code': 'timecode_out_of_range', 'segment_id': s['id'],
                    'asset_version_id': av['id'], 'at_ms': at,
                    'message': '时间码《%s》@%sms 超出素材时长 %sms'
                               % (tc.get('label', ''), at, av['duration_ms'])})

    for ap in conn.execute('SELECT * FROM assessment_points WHERE course_id=?',
                           (course_id,)).fetchall():
        if not conn.execute('SELECT id FROM segments WHERE id=?', (ap['segment_id'],)).fetchone():
            issues.append({'code': 'assessment_orphan', 'assessment_id': ap['id'],
                           'segment_id': ap['segment_id'],
                           'message': '考核点 #%s 引用的教学片段 #%s 已删除'
                                      % (ap['id'], ap['segment_id'])})
        if not conn.execute('SELECT id FROM objectives WHERE id=?',
                            (ap['objective_id'],)).fetchone():
            issues.append({'code': 'objective_missing', 'assessment_id': ap['id'],
                           'objective_id': ap['objective_id'],
                           'message': '考核点 #%s 引用的目标 #%s 不存在'
                                      % (ap['id'], ap['objective_id'])})
    return issues


def coverage_report(conn, course_id):
    '''目标覆盖报告：列出当前版本未覆盖的训练目标。'''
    objectives = [dict(r) for r in conn.execute(
        'SELECT * FROM objectives WHERE course_id=? ORDER BY id', (course_id,))]
    aps = [dict(r) for r in conn.execute(
        'SELECT * FROM assessment_points WHERE course_id=?', (course_id,))]
    covered = set()
    broken_assessments = []
    for ap in aps:
        seg_ok = conn.execute('SELECT id FROM segments WHERE id=?',
                              (ap['segment_id'],)).fetchone() is not None
        if seg_ok:
            covered.add(ap['objective_id'])
        else:
            broken_assessments.append(ap['id'])
    uncovered = [o for o in objectives if o['id'] not in covered]
    return {
        'objectives': objectives,
        'covered_objective_ids': sorted(covered),
        'uncovered_objective_ids': [o['id'] for o in uncovered],
        'uncovered': uncovered,
        'broken_assessments': broken_assessments,
    }


def refresh_repair_state(conn, segment_id):
    '''替换素材版本 / 修改时间码后重算：时间码超界 → 待修复。'''
    s = get_or_404(conn, 'segments', segment_id)
    if s['kind'] != 'demo':
        return
    conn.execute("UPDATE segment_issues SET resolved=1 WHERE segment_id=? "
                 "AND code='timecode_out_of_range' AND resolved=0", (segment_id,))
    if s['asset_version_id']:
        av = conn.execute('SELECT * FROM asset_versions WHERE id=?',
                          (s['asset_version_id'],)).fetchone()
        if av:
            for tc in json.loads(s['timecodes'] or '[]'):
                at = tc.get('at_ms', 0)
                if av['duration_ms'] and at > av['duration_ms']:
                    conn.execute('INSERT INTO segment_issues(segment_id,code,detail) '
                                 'VALUES(?,?,?)',
                                 (segment_id, 'timecode_out_of_range',
                                  '时间码《%s》@%sms 超出素材时长'
                                  % (tc.get('label', ''), at)))
    open_issues = conn.execute(
        'SELECT COUNT(*) c FROM segment_issues WHERE segment_id=? AND resolved=0',
        (segment_id,)).fetchone()['c']
    if open_issues:
        conn.execute("UPDATE segments SET status='pending_repair' WHERE id=?", (segment_id,))
    else:
        conn.execute("UPDATE segments SET status='ok' WHERE id=?", (segment_id,))


def build_package(conn, course_id):
    '''后台工作器调用：生成不可变课件包快照（含重验证结果）。'''
    course = get_or_404(conn, 'courses', course_id)
    objectives = [dict(r) for r in conn.execute(
        'SELECT * FROM objectives WHERE course_id=? ORDER BY id', (course_id,))]
    chs = []
    total_ms = 0
    for ch in conn.execute(
            'SELECT * FROM chapters WHERE course_id=? ORDER BY position, id',
            (course_id,)).fetchall():
        ch = dict(ch)
        ch['requires'] = json.loads(ch['requires'] or '[]')
        segs = []
        for sg in conn.execute(
                'SELECT * FROM segments WHERE chapter_id=? ORDER BY position, id',
                (ch['id'],)).fetchall():
            s = dict(sg)
            s['timecodes'] = json.loads(s['timecodes'] or '[]')
            if s['asset_version_id']:
                av = conn.execute('SELECT * FROM asset_versions WHERE id=?',
                                  (s['asset_version_id'],)).fetchone()
                if av:
                    s['asset_version'] = dict(av)
                    total_ms += av['duration_ms'] or 0
            segs.append(s)
        ch['segments'] = segs
        chs.append(ch)
    aps = [dict(r) for r in conn.execute(
        'SELECT * FROM assessment_points WHERE course_id=?', (course_id,))]
    issues = validate_course(conn, course_id)
    snap = {
        'course': dict(course),
        'objectives': objectives,
        'chapters': chs,
        'assessments': aps,
        'total_minutes': round(total_ms / 60000, 2),
        'generated_at': now(),
    }
    body = json.dumps(snap, ensure_ascii=False, sort_keys=True)
    sha = hashlib.sha256(body.encode('utf-8')).hexdigest()
    version = conn.execute(
        'SELECT COALESCE(MAX(version),0) v FROM packages WHERE course_id=?',
        (course_id,)).fetchone()['v'] + 1
    cur = conn.execute(
        'INSERT INTO packages(course_id,version,snapshot,issues,sha256,created_at) '
        'VALUES(?,?,?,?,?,?)',
        (course_id, version, body, json.dumps(issues, ensure_ascii=False), sha, now()))
    pkg = conn.execute('SELECT * FROM packages WHERE id=?', (cur.lastrowid,)).fetchone()
    return dict(pkg)


def create_export_job(conn, team_id, course_id, idem_key):
    '''幂等创建导出作业：同键返回既有作业，额度只在首次创建时扣减一次。'''
    existing = conn.execute('SELECT * FROM jobs WHERE idempotency_key=?',
                            (idem_key,)).fetchone()
    if existing:
        return dict(existing), True
    qrow = conn.execute('SELECT export_remaining FROM quotas WHERE team_id=?',
                        (team_id,)).fetchone()
    if not qrow:
        raise BadRequest('团队额度不存在')
    if qrow['export_remaining'] <= 0:
        raise Conflict('quota_exceeded: 导出额度已用尽')
    conn.execute('UPDATE quotas SET export_remaining=export_remaining-1 WHERE team_id=?',
                 (team_id,))
    cur = conn.execute(
        'INSERT INTO jobs(team_id,idempotency_key,type,status,payload,created_at,'
        'updated_at) VALUES(?,?,?,?,?,?,?)',
        (team_id, idem_key, 'export_package', 'queued',
         json.dumps({'course_id': course_id}), now(), now()))
    job = conn.execute('SELECT * FROM jobs WHERE id=?', (cur.lastrowid,)).fetchone()
    return dict(job), False


def job_view(job):
    out = dict(job)
    for k in ('payload', 'result', 'receipt'):
        if out.get(k):
            try:
                out[k] = json.loads(out[k])
            except Exception:
                pass
    out['deduplicated'] = bool(job.get('deduplicated'))
    return out


def package_view(pkg):
    out = dict(pkg)
    try:
        out['snapshot'] = json.loads(pkg['snapshot'])
    except Exception:
        pass
    try:
        out['issues'] = json.loads(pkg['issues'] or '[]')
    except Exception:
        pass
    return out
