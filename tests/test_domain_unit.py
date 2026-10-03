# -*- coding: utf-8 -*-
"""领域服务直接单元测试：撤销删除章节、工作器重试、覆盖确认等。"""
import json
import os
import tempfile
import unittest

from tests.helpers import Harness, fresh
from server.db import connect, row, rows, begin_immediate
from server import domain as D
from server import worker


ACTOR = {"id": 2, "name": "Beth 编辑"}


class DomainCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = fresh(self.tmp)
        self.art = os.path.join(self.tmp, "art")
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()

    def test_undo_delete_restores_chapter_and_children(self):
        # 删除章节2（含片段3/4、绑定、覆盖、前置边）
        snap, _ = D.delete_chapter(self.conn, 2, ACTOR)
        rev_id = row(self.conn.execute(
            "SELECT id FROM revisions WHERE entity_type='chapter' AND entity_id=2 "
            "AND action='delete'"))["id"]
        self.assertIsNone(row(self.conn.execute(
            "SELECT id FROM chapters WHERE id=2")))
        # 撤销删除：章节、片段、绑定、覆盖、边全部回来
        D.undo_revision(self.conn, rev_id, ACTOR)
        self.assertIsNotNone(row(self.conn.execute(
            "SELECT id FROM chapters WHERE id=2")))
        self.assertEqual(row(self.conn.execute(
            "SELECT COUNT(*) c FROM segments WHERE chapter_id=2"))["c"], 2)
        self.assertEqual(row(self.conn.execute(
            "SELECT COUNT(*) c FROM demo_bindings WHERE segment_id=4"))["c"], 3)
        self.assertIsNotNone(row(self.conn.execute(
            "SELECT id FROM objective_coverage WHERE objective_id=2 AND segment_id=4")))
        self.assertIsNotNone(row(self.conn.execute(
            "SELECT id FROM prereq_edges WHERE chapter_id=2 AND requires_chapter_id=1")))

    def test_undo_delete_chapter_after_teammate_recreate_conflicts(self):
        snap, _ = D.delete_chapter(self.conn, 2, ACTOR)
        rev_id = row(self.conn.execute(
            "SELECT id FROM revisions WHERE entity_type='chapter' AND entity_id=2 "
            "AND action='delete'"))["id"]
        # 别人重建了同 id 章节（模拟）
        self.conn.execute(
            "INSERT INTO chapters(id,course_id,title,position,minutes) VALUES(2,1,'X',9,0)")
        with self.assertRaises(D.Conflict):
            D.undo_revision(self.conn, rev_id, ACTOR)

    def test_worker_retries_then_fails_package(self):
        # 构造一个必然失败的构建：把包指向不存在课程的快照前先建包再删课程
        pkg, _ = D.create_export(self.conn, 1, "pinned", "W-1", ACTOR)
        # 破坏作业负载指向不存在的 package
        self.conn.execute("UPDATE packages SET course_id=999 WHERE id=?", (pkg["id"],))
        # 让退避立即到期，确保三次尝试都能跑到
        self.conn.execute("UPDATE jobs SET run_after=0")
        self.conn.commit()
        self.conn.close()
        w = worker.Worker(self.db, self.art, interval=0)
        for _ in range(3):
            w.tick()
            c = connect(self.db)
            c.execute("UPDATE jobs SET run_after=0 WHERE status='queued'")
            c.commit(); c.close()
        c = connect(self.db)
        job = row(c.execute("SELECT status,attempts FROM jobs WHERE id=1"))
        p = row(c.execute("SELECT status FROM packages WHERE id=?", (pkg["id"],)))
        c.close()
        self.connect = None
        self.assertEqual(job["status"], "failed")
        self.assertEqual(p["status"], "failed")

    def test_cycle_detection_self_and_indirect(self):
        # 自环
        self.assertTrue(D.would_cycle(self.conn, 1, 1, 1))
        # 1->2->3 已存在边(2->1,3->2)；新增 1->3 形成 1->3->2->1
        self.assertTrue(D.would_cycle(self.conn, 1, 1, 3))
        # 不构成环：让 1 依赖一个新节点4 不存在于后继 → False
        # （would_cycle 只看图，新增 3->1 才是环）
        self.assertFalse(D.would_cycle(self.conn, 1, 1, 999))

    def test_start_class_requires_done_package(self):
        pkg, _ = D.create_export(self.conn, 1, "pinned", "C-1", ACTOR)
        with self.assertRaises(D.BadRequest):
            D.start_class(self.conn, 1, "未构建班", pkg["id"], ACTOR)
        self.conn.commit(); self.conn.close()
        worker.run_once(self.db, self.art)
        self.conn = connect(self.db)
        pkg2 = D.get_or_404(self.conn, "packages", pkg["id"])
        cl = D.start_class(self.conn, 1, "正常班", pkg2["id"], ACTOR)
        frozen = row(self.conn.execute(
            "SELECT frozen,mode FROM packages WHERE id=?", (pkg["id"],)))
        self.assertEqual(frozen["frozen"], 1)
        self.assertEqual(frozen["mode"], "pinned")

    def test_pinned_follow_stale_logic_on_bump(self):
        D.create_export(self.conn, 1, "follow", "F-1", ACTOR)
        D.create_export(self.conn, 1, "pinned", "P-1", ACTOR)
        self.conn.commit(); self.conn.close()
        worker.run_once(self.db, self.art)
        self.conn = connect(self.db)
        D.bump_course(self.conn, 1)
        stales = {r["id"]: r["stale"] for r in rows(self.conn.execute(
            "SELECT id,stale FROM packages"))}
        self.assertEqual(len(stales), 2)
        # follow 包过期，pinned 包不过期（具体哪个 id 取决于插入顺序）
        follow = row(self.conn.execute(
            "SELECT stale FROM packages WHERE idempotency_key='F-1'"))["stale"]
        pinned = row(self.conn.execute(
            "SELECT stale FROM packages WHERE idempotency_key='P-1'"))["stale"]
        self.assertEqual(follow, 1)
        self.assertEqual(pinned, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
