# -*- coding: utf-8 -*-
'''后台工作器：轮询 jobs 表，生成可预览课件包；回执持久化到作业记录。'''
import json
import time

from . import services


def process_once(db):
    '''处理一个排队中的作业。返回是否有作业被处理。'''
    with db.txn() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not job:
            return False
        conn.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?",
                     (services.now(), job['id']))
        job_id = job['id']
        payload = json.loads(job['payload'] or '{}')
        jtype = job['type']
        try:
            if jtype != 'export_package':
                raise ValueError('未知作业类型 %s' % jtype)
            pkg = services.build_package(conn, payload['course_id'])
            result = {'package_id': pkg['id'], 'version': pkg['version'],
                      'sha256': pkg['sha256'], 'issues': json.loads(pkg['issues'])}
            receipt = {'receipt_id': 'rcpt-%s' % pkg['sha256'][:16],
                       'package_id': pkg['id'], 'version': pkg['version'],
                       'sha256': pkg['sha256'], 'issued_at': services.now()}
            conn.execute("UPDATE jobs SET status='succeeded', result=?, receipt=?, "
                         "updated_at=? WHERE id=?",
                         (json.dumps(result, ensure_ascii=False),
                          json.dumps(receipt, ensure_ascii=False), services.now(), job_id))
        except Exception as e:  # 作业失败持久化，额度不回退（由对账/人工处理）
            conn.execute("UPDATE jobs SET status='failed', result=?, updated_at=? WHERE id=?",
                         (json.dumps({'error': str(e)}, ensure_ascii=False),
                          services.now(), job_id))
    return True


def loop(db, interval=0.2, stop=None):
    while not (stop and stop.is_set()):
        try:
            process_once(db)
        except Exception:
            pass
        time.sleep(interval)
