# -*- coding: utf-8 -*-
'''验收测试：覆盖需求列出的全部场景。
运行: python3 -m unittest discover -s tests -v
'''
import http.client
import json
import tempfile
import threading
import time
import unittest

from app.server import create_server

SERVER = {}


def req(method, path, body=None, user=None):
    conn = http.client.HTTPConnection('127.0.0.1', SERVER['port'], timeout=10)
    headers = {'Content-Type': 'application/json'}
    if user:
        headers['X-User-Id'] = str(user)
    conn.request(method, path, json.dumps(body).encode() if body is not None else b'', headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, json.loads(data) if data else {}


def setUpModule():
    db_path = tempfile.mktemp(suffix='.db')
    httpd, db, stop = create_server(db_path, port=0, worker_interval=0.02)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    SERVER.update(httpd=httpd, db=db, stop=stop, port=port, db_path=db_path)

    s, team = req('POST', '/teams', {'name': '培训部'})
    assert s == 201, (s, team)
    tid = team['id']
    users = {}
    for name, role in (('管理员', 'admin'), ('老师甲', 'editor'),
                       ('老师乙', 'editor'), ('观察者', 'viewer')):
        s, u = req('POST', '/teams/%d/users' % tid, {'name': name, 'role': role})
        assert s == 201, (s, u)
        users[role + ':' + name] = u['users']['id']
    SERVER.update(
        team_id=tid,
        admin_id=users['admin:管理员'],
        e1=users['editor:老师甲'],
        e2=users['editor:老师乙'],
        v1=users['viewer:观察者'],
    )


def tearDownModule():
    SERVER['stop'].set()
    SERVER['httpd'].shutdown()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for k in ('team_id', 'admin_id', 'e1', 'e2', 'v1', 'db'):
            setattr(cls, k, SERVER[k])

    def mk_course(self, title):
        s, r = req('POST', '/courses', {'team_id': self.team_id, 'title': title}, self.e1)
        self.assertEqual(s, 201, r)
        return r['course']['id']

    def mk_chapter(self, cid, title, requires=None):
        s, r = req('POST', '/courses/%d/chapters' % cid,
                   {'title': title, 'requires': requires or []}, self.e1)
        self.assertEqual(s, 201, r)
        return r['chapter']

    def mk_segment(self, chid, kind, title, script=''):
        s, r = req('POST', '/chapters/%d/segments' % chid,
                   {'kind': kind, 'title': title, 'script': script}, self.e1)
        self.assertEqual(s, 201, r)
        return r['segment']

    def mk_objective(self, cid, code, title):
        s, r = req('POST', '/courses/%d/objectives' % cid,
                   {'code': code, 'title': title}, self.e1)
        self.assertEqual(s, 201, r)
        return r['objective']

    def mk_asset_version(self, duration_ms, has_audio):
        s, r = req('POST', '/teams/%d/assets' % self.team_id,
                   {'title': '录屏素材'}, self.e1)
        self.assertEqual(s, 201, r)
        aid = r['id']
        s, r = req('POST', '/assets/%d/versions' % aid, {
            'video_uri': 'v/%d/1.mp4' % aid,
            'subtitle_uri': 'v/%d/1.srt' % aid,
            'notes_uri': 'v/%d/1.md' % aid,
            'duration_ms': duration_ms,
            'has_audio': has_audio,
        }, self.e1)
        self.assertEqual(s, 201, r)
        return aid, r

    def wait_job(self, jid, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            s, r = req('GET', '/jobs/%d' % jid, user=self.e1)
            if r['job']['status'] in ('succeeded', 'failed'):
                return r['job']
            time.sleep(0.05)
        self.fail('作业超时未完成')

    def head_of(self, entity, eid):
        return self.db.q1('SELECT head_rev FROM entity_heads WHERE entity=? AND entity_id=?',
                          (entity, eid))['head_rev']


class TestPrerequisiteRevalidation(Base):
    '''验收1：章节重排或删除前置章节后，重新验证知识前置关系（而非仅更新总分钟数）。'''

    def test_reorder_and_delete_prerequisite(self):
        cid = self.mk_course('前置关系课')
        a = self.mk_chapter(cid, 'A 基础')
        b = self.mk_chapter(cid, 'B 进阶', requires=[a['id']])
        c = self.mk_chapter(cid, 'C 实战', requires=[b['id']])

        s, r = req('GET', '/courses/%d/validation' % cid, user=self.e1)
        self.assertEqual(s, 200)
        self.assertEqual(r['issues'], [])

        # 把 B 排到 A 前面（C->B->A 的顺序被破坏）
        s, r = req('POST', '/courses/%d/chapters/reorder' % cid,
                   {'order': [b['id'], a['id'], c['id']]}, self.e1)
        self.assertEqual(s, 200, r)
        codes = {i['code'] for i in r['validation']}
        self.assertIn('prerequisite_order', codes)
        issue = next(i for i in r['validation'] if i['code'] == 'prerequisite_order')
        self.assertEqual(issue['required_chapter_id'], a['id'])

        # 恢复正确顺序
        req('POST', '/courses/%d/chapters/reorder' % cid,
            {'order': [a['id'], b['id'], c['id']]}, self.e1)

        # 删除前置章节 A -> B 的前置引用悬空
        rev = self.head_of('chapters', a['id'])
        s, r = req('DELETE', '/chapters/%d?expected_rev=%d' % (a['id'], rev), user=self.e1)
        self.assertEqual(s, 200, r)
        codes = {i['code'] for i in r['validation']}
        self.assertIn('prerequisite_missing', codes)
        miss = next(i for i in r['validation'] if i['code'] == 'prerequisite_missing')
        self.assertEqual(miss['chapter_id'], b['id'])
        self.assertEqual(miss['missing_chapter_id'], a['id'])

    def test_reorder_rejects_incomplete_order(self):
        cid = self.mk_course('重排校验课')
        a = self.mk_chapter(cid, 'A')
        b = self.mk_chapter(cid, 'B')
        s, r = req('POST', '/courses/%d/chapters/reorder' % cid,
                   {'order': [a['id']]}, self.e1)
        self.assertEqual(s, 400, r)


class TestAssessmentCoverage(Base):
    '''验收：每个考核点引用支撑它的教学片段；删演示后报告列出未覆盖目标。'''

    def test_delete_demo_orphans_assessment(self):
        cid = self.mk_course('覆盖课')
        obj = self.mk_objective(cid, 'OBJ-1', '能独立完成数据导入')
        ch = self.mk_chapter(cid, '第一章')
        demo = self.mk_segment(ch['id'], 'demo', '导入演示')
        s, r = req('POST', '/courses/%d/assessments' % cid,
                   {'objective_id': obj['id'], 'segment_id': demo['id'], 'note': '跟做一遍'},
                   self.e1)
        self.assertEqual(s, 201, r)
        ap_id = r['id']

        s, cov = req('GET', '/courses/%d/coverage' % cid, user=self.e1)
        self.assertEqual(cov['coverage']['uncovered_objective_ids'], [])

        rev = self.head_of('segments', demo['id'])
        s, r = req('DELETE', '/segments/%d?expected_rev=%d' % (demo['id'], rev), user=self.e1)
        self.assertEqual(s, 200, r)
        codes = {i['code'] for i in r['validation']}
        self.assertIn('assessment_orphan', codes)
        self.assertIn(obj['id'], r['coverage']['uncovered_objective_ids'])
        self.assertIn(ap_id, r['coverage']['broken_assessments'])

        s, r = req('GET', '/courses/%d/coverage' % cid, user=self.e1)
        self.assertIn(obj['id'], r['coverage']['uncovered_objective_ids'])
        uncovered = r['coverage']['uncovered']
        self.assertEqual(uncovered[0]['code'], 'OBJ-1')


class TestAssetVersionConsistency(Base):
    '''验收：画面/字幕/说明同属一个素材版本；替换视频后时间码超界进入待修复。'''

    def test_replace_video_out_of_range_timecode_pending_repair(self):
        cid = self.mk_course('素材课')
        ch = self.mk_chapter(cid, '演示章')
        demo = self.mk_segment(ch['id'], 'demo', '操作演示')
        aid, v1 = self.mk_asset_version(60000, True)

        s, r = req('POST', '/segments/%d/bind_asset' % demo['id'],
                   {'asset_version_id': v1['id'], 'expected_rev': 1}, self.e1)
        self.assertEqual(s, 200, r)

        s, r = req('POST', '/segments/%d/timecodes' % demo['id'],
                   {'timecodes': [{'label': '点击保存', 'at_ms': 55000}],
                    'expected_rev': 2}, self.e1)
        self.assertEqual(r['segment']['status'], 'ok', r)

        # 替换为时长更短的素材版本（画面/字幕/说明同源 v2）
        s, v2 = req('POST', '/assets/%d/versions' % aid, {
            'video_uri': 'v/%d/2.mp4' % aid,
            'subtitle_uri': 'v/%d/2.srt' % aid,
            'notes_uri': 'v/%d/2.md' % aid,
            'duration_ms': 30000, 'has_audio': True}, self.e1)
        self.assertEqual(s, 201, v2)

        s, r = req('POST', '/segments/%d/bind_asset' % demo['id'],
                   {'asset_version_id': v2['id'], 'expected_rev': 3}, self.e1)
        self.assertEqual(r['segment']['status'], 'pending_repair', r)
        codes = {i['code'] for i in r['validation']}
        self.assertIn('timecode_out_of_range', codes)
        self.assertIn('segment_pending_repair', codes)

        # 修正时间码到范围内 -> 自动恢复 ok
        s, r = req('POST', '/segments/%d/timecodes' % demo['id'],
                   {'timecodes': [{'label': '点击保存', 'at_ms': 25000}],
                    'expected_rev': 4}, self.e1)
        self.assertEqual(r['segment']['status'], 'ok')
        self.assertNotIn('timecode_out_of_range', {i['code'] for i in r['validation']})

    def test_missing_audio_track_flagged_and_packaged(self):
        cid = self.mk_course('音轨课')
        ch = self.mk_chapter(cid, '章')
        demo = self.mk_segment(ch['id'], 'demo', '无声演示')
        _, v = self.mk_asset_version(10000, False)
        s, r = req('POST', '/segments/%d/bind_asset' % demo['id'],
                   {'asset_version_id': v['id'], 'expected_rev': 1}, self.e1)
        codes = {i['code'] for i in r['validation']}
        self.assertIn('audio_missing', codes)

        # 音轨缺失是可导出的告警（不阻断），问题随包留存
        s, r = req('POST', '/courses/%d/export' % cid,
                   {'idempotency_key': 'audio-test-1'}, self.e1)
        self.assertEqual(s, 200, r)
        job = self.wait_job(r['job']['id'])
        self.assertEqual(job['status'], 'succeeded')
        self.assertIn('audio_missing', {i['code'] for i in job['result']['issues']})


class TestObjectiveRevisionCoverage(Base):
    '''验收：训练目标改版后，报告列出当前版本未覆盖的目标。'''

    def test_objective_revision_report(self):
        cid = self.mk_course('目标课')
        o1 = self.mk_objective(cid, 'O-1', '旧目标一')
        o2 = self.mk_objective(cid, 'O-2', '目标二')
        ch = self.mk_chapter(cid, '章')
        seg = self.mk_segment(ch['id'], 'lecture', '讲解')

        # 只覆盖 O-1
        req('POST', '/courses/%d/assessments' % cid,
            {'objective_id': o1['id'], 'segment_id': seg['id']}, self.e1)
        s, r = req('GET', '/courses/%d/coverage' % cid, user=self.e1)
        self.assertEqual(r['coverage']['uncovered_objective_ids'], [o2['id']])

        # 目标改版（标题更新）不改变覆盖关系，报告仍按当前版本列出未覆盖目标
        rev = self.head_of('objectives', o2['id'])
        s, r = req('PUT', '/objectives/%d' % o2['id'],
                   {'title': '目标二（2026 新版）', 'expected_rev': rev}, self.e1)
        self.assertEqual(s, 200, r)
        s, r = req('GET', '/courses/%d/coverage' % cid, user=self.e1)
        self.assertEqual(r['coverage']['uncovered_objective_ids'], [o2['id']])
        self.assertEqual(r['coverage']['objectives'][1]['title'], '目标二（2026 新版）')

        # 补上 O-2 覆盖
        req('POST', '/courses/%d/assessments' % cid,
            {'objective_id': o2['id'], 'segment_id': seg['id']}, self.e1)
        s, r = req('GET', '/courses/%d/coverage' % cid, user=self.e1)
        self.assertEqual(r['coverage']['uncovered_objective_ids'], [])


class TestExportIdempotencyQuotaReceipt(Base):
    '''验收：导出成功但回执丢失可重取；重复导出作业幂等且额度一致。'''

    def test_idempotent_export_quota_and_receipt_recovery(self):
        cid = self.mk_course('导出课')
        self.mk_chapter(cid, '章')
        s, q = req('GET', '/teams/%d/quota' % self.team_id, user=self.e1)
        before = q['export_remaining']

        s, r = req('POST', '/courses/%d/export' % cid,
                   {'idempotency_key': 'exp-001'}, self.e1)
        self.assertEqual(s, 200, r)
        self.assertFalse(r['deduplicated'])
        jid = r['job']['id']

        # 重复提交同幂等键 -> 去重，返回同一作业
        s, r2 = req('POST', '/courses/%d/export' % cid,
                    {'idempotency_key': 'exp-001'}, self.e1)
        self.assertTrue(r2['deduplicated'])
        self.assertEqual(r2['job']['id'], jid)

        s, q = req('GET', '/teams/%d/quota' % self.team_id, user=self.e1)
        self.assertEqual(q['export_remaining'], before - 1)
        cnt = self.db.q1("SELECT COUNT(*) c FROM jobs WHERE idempotency_key='exp-001'")['c']
        self.assertEqual(cnt, 1)

        job = self.wait_job(jid)
        self.assertEqual(job['status'], 'succeeded')

        # 回执丢失后可凭作业反复重取
        s, r = req('GET', '/jobs/%d/receipt' % jid, user=self.e1)
        self.assertEqual(s, 200)
        s, r = req('GET', '/jobs/%d/receipt' % jid, user=self.e1)
        self.assertTrue(r['receipt']['receipt_id'].startswith('rcpt-'))
        self.assertEqual(r['receipt']['sha256'], job['result']['sha256'])

    def test_quota_exceeded_and_idempotent_replay_still_works(self):
        cid = self.mk_course('额度课')
        with self.db.txn() as conn:
            conn.execute('UPDATE quotas SET export_remaining=1 WHERE team_id=?',
                         (self.team_id,))
        s, r1 = req('POST', '/courses/%d/export' % cid,
                    {'idempotency_key': 'exp-q1'}, self.e1)
        self.assertEqual(s, 200, r1)
        s, r2 = req('POST', '/courses/%d/export' % cid,
                    {'idempotency_key': 'exp-q2'}, self.e1)
        self.assertEqual(s, 409)
        self.assertIn('quota_exceeded', r2['detail'])

        # 额度用尽后，重放旧幂等键仍返回原作业，不报错、不再扣额度
        s, r3 = req('POST', '/courses/%d/export' % cid,
                    {'idempotency_key': 'exp-q1'}, self.e1)
        self.assertEqual(s, 200, r3)
        self.assertEqual(r3['job']['id'], r1['job']['id'])
        self.assertTrue(r3['deduplicated'])
        with self.db.txn() as conn:
            conn.execute('UPDATE quotas SET export_remaining=20 WHERE team_id=?',
                         (self.team_id,))


class TestConcurrencyAndUndo(Base):
    '''验收：多人编辑用版本条件处理冲突；撤销不能回退同事的后续修订。'''

    def test_optimistic_concurrency_conflict(self):
        cid = self.mk_course('并发课')
        ch = self.mk_chapter(cid, '第一章')
        rev = ch['head_rev']
        # 甲先改成功（rev -> rev+1）
        s, r = req('PUT', '/chapters/%d' % ch['id'],
                   {'title': '甲的标题', 'expected_rev': rev}, self.e1)
        self.assertEqual(s, 200, r)
        # 乙仍用旧版本号 -> 冲突
        s, r = req('PUT', '/chapters/%d' % ch['id'],
                   {'title': '乙的标题', 'expected_rev': rev}, self.e2)
        self.assertEqual(s, 409)
        self.assertIn('版本冲突', r['detail'])
        # 乙刷新版本号后可提交
        new_rev = self.head_of('chapters', ch['id'])
        s, r = req('PUT', '/chapters/%d' % ch['id'],
                   {'title': '乙的标题', 'expected_rev': new_rev}, self.e2)
        self.assertEqual(s, 200, r)

    def test_undo_does_not_revert_colleague(self):
        cid = self.mk_course('撤销课')
        ch = self.mk_chapter(cid, '原始标题')
        r1 = self.head_of('chapters', ch['id'])
        s, r = req('PUT', '/chapters/%d' % ch['id'],
                   {'title': '甲改成B', 'expected_rev': r1}, self.e1)
        self.assertEqual(s, 200)
        r2 = self.head_of('chapters', ch['id'])
        s, r = req('PUT', '/chapters/%d' % ch['id'],
                   {'title': '乙改成C', 'expected_rev': r2}, self.e2)
        self.assertEqual(s, 200)

        s, hist = req('GET', '/courses/%d/history' % cid, user=self.e1)
        # 找到甲的那次修订
        a_rev = next(x for x in hist['revisions']
                     if x['entity'] == 'chapters' and x['entity_id'] == ch['id']
                     and x['rev'] == r2)
        s, r = req('POST', '/revisions/%d/undo' % a_rev['id'], {}, self.e1)
        self.assertEqual(r['status'], 'skipped')
        self.assertIn('title', r['skipped'])
        # 同事的值保持
        s, tree = req('GET', '/courses/%d' % cid, user=self.e1)
        self.assertEqual(tree['chapters'][0]['title'], '乙改成C')

        # 撤销乙的最新修订 -> 回到甲的值
        b_head = self.head_of('chapters', ch['id'])
        s, hist = req('GET', '/courses/%d/history' % cid, user=self.e1)
        b_rev = next(x for x in hist['revisions']
                     if x['entity'] == 'chapters' and x['entity_id'] == ch['id']
                     and x['rev'] == b_head)
        s, r = req('POST', '/revisions/%d/undo' % b_rev['id'], {}, self.e2)
        self.assertEqual(r['status'], 'applied')
        s, tree = req('GET', '/courses/%d' % cid, user=self.e1)
        self.assertEqual(tree['chapters'][0]['title'], '甲改成B')

    def test_undo_delete_restores(self):
        cid = self.mk_course('恢复课')
        ch = self.mk_chapter(cid, '章')
        seg = self.mk_segment(ch['id'], 'lecture', '要被删的片段')
        rev = self.head_of('segments', seg['id'])
        s, r = req('DELETE', '/segments/%d?expected_rev=%d' % (seg['id'], rev), user=self.e1)
        self.assertEqual(s, 200, r)

        s, hist = req('GET', '/courses/%d/history' % cid, user=self.e1)
        dele = next(x for x in hist['revisions']
                    if x['entity'] == 'segments' and x['entity_id'] == seg['id']
                    and x['action'] == 'delete')
        s, r = req('POST', '/revisions/%d/undo' % dele['id'], {}, self.e1)
        self.assertEqual(r['effect'], 'delete_reverted')

        s, tree = req('GET', '/courses/%d' % cid, user=self.e1)
        titles = [x['title'] for x in tree['chapters'][0]['segments']]
        self.assertIn('要被删的片段', titles)


class TestFreezeRules(Base):
    '''验收：已开班课程冻结规则 —— pinned 固定复用 vs follow 模块跟随更新。'''

    def _export(self, cid, key):
        s, r = req('POST', '/courses/%d/export' % cid,
                   {'idempotency_key': key}, self.e1)
        self.assertEqual(s, 200, r)
        job = self.wait_job(r['job']['id'])
        self.assertEqual(job['status'], 'succeeded')
        s, pk = req('GET', '/courses/%d/packages' % cid, user=self.e1)
        return pk['packages'][0]

    def test_pinned_frozen_follow_floats(self):
        cid = self.mk_course('冻结课')
        ch = self.mk_chapter(cid, '模板章节v1')
        self.mk_segment(ch['id'], 'lecture', '讲解')
        p1 = self._export(cid, 'freeze-1')
        self.assertEqual(p1['version'], 1)

        s, r = req('POST', '/courses/%d/classes' % cid,
                   {'name': '3月班', 'mode': 'pinned', 'package_id': p1['id']}, self.e1)
        self.assertEqual(s, 201, r)
        pinned_cls = r['id']
        s, r = req('POST', '/courses/%d/classes' % cid,
                   {'name': '4月班', 'mode': 'follow'}, self.e1)
        self.assertEqual(s, 201, r)
        follow_cls = r['id']

        # 模板升级 + 再导出 v2
        rev = self.head_of('chapters', ch['id'])
        req('PUT', '/chapters/%d' % ch['id'],
            {'title': '模板章节v2（升级）', 'expected_rev': rev}, self.e1)
        p2 = self._export(cid, 'freeze-2')
        self.assertEqual(p2['version'], 2)

        # pinned 班级仍看到 v1 旧内容（冻结）
        s, r = req('GET', '/classes/%d/package' % pinned_cls, user=self.e1)
        self.assertEqual(r['package']['id'], p1['id'])
        self.assertEqual(r['package']['snapshot']['chapters'][0]['title'], '模板章节v1')

        # follow 班级浮动到 v2 新内容
        s, r = req('GET', '/classes/%d/package' % follow_cls, user=self.e1)
        self.assertEqual(r['package']['version'], 2)
        self.assertEqual(r['package']['snapshot']['chapters'][0]['title'],
                         '模板章节v2（升级）')

        s, r = req('GET', '/courses/%d/impact' % cid, user=self.e1)
        effects = {c['name']: c['effect'] for c in r['classes']}
        self.assertIn('frozen', effects['3月班'])
        self.assertIn('follow', effects['4月班'])


class TestTeamMembership(Base):
    '''验收：老师离开团队后不能编辑；工作区仍可按权限查看历史。'''

    def test_teacher_leave_team(self):
        cid = self.mk_course('人事课')
        ch = self.mk_chapter(cid, '章节')

        s, r = req('POST', '/users/%d/leave' % self.e1, {}, self.e1)
        self.assertEqual(s, 200, r)

        # 离职后写操作被拒
        s, r = req('PUT', '/chapters/%d' % ch['id'],
                   {'title': 'x', 'expected_rev': ch['head_rev']}, self.e1)
        self.assertEqual(s, 403)
        self.assertIn('member_inactive', r['detail'])

        # 历史仍可只读查看（含本人曾经的署名）
        s, hist = req('GET', '/courses/%d/history' % cid, user=self.e1)
        self.assertEqual(s, 200)
        self.assertIn('老师甲', {x.get('actor_name') for x in hist['revisions']})

        # 在职同事仍可编辑
        rev = self.head_of('chapters', ch['id'])
        s, r = req('PUT', '/chapters/%d' % ch['id'],
                   {'title': 'y', 'expected_rev': rev}, self.e2)
        self.assertEqual(s, 200, r)

        with self.db.txn() as conn:
            conn.execute('UPDATE users SET active=1 WHERE id=?', (self.e1,))


if __name__ == '__main__':
    unittest.main()
