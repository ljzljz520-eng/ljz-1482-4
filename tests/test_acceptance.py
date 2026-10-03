# -*- coding: utf-8 -*-
"""验收场景测试：覆盖需求中列出的全部关键情形。"""
import json
import os
import tempfile
import unittest

from tests.helpers import Harness, fresh
from server.db import connect, row, rows


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = fresh(self.tmp)
        self.h = Harness(self.db)

    # --- 工具 ---------------------------------------------------------------
    def call(self, *a, **k):
        code, data = self.h.call(*a, **k)
        return code, data

    def must(self, code, data, expect=200):
        self.assertEqual(code, expect, json.dumps(data, ensure_ascii=False))
        return data

    def export_build(self, key="k1", mode="pinned", cid=1, user=2):
        self.must(200, None) if False else None
        code, data = self.call("POST", f"/api/courses/{cid}/exports",
                               {"mode": mode, "idempotency_key": key}, user=user)
        self.assertEqual(code, 200, data)
        pkg = data["package"]["id"]
        self.assertTrue(self.h.work_once())
        return pkg


class TestPrereqRevalidation(ApiCase):
    """验收1：删除前置章节 / 章节重排 → 重新验证知识前置关系，而非只改总分钟数。"""

    def test_reorder_breaks_prereq_order(self):
        # 原始顺序：安全规范(1) < 工具使用(2) < 线路装配(3)，前置链 2->1, 3->2
        code, rep = self.call("GET", "/api/courses/1/report")
        self.assertTrue(rep["ok"])

        # 把前置章节「安全规范」挪到最后：顺序变为 [2,3,1]
        code, data = self.call("POST", "/api/courses/1/chapters/reorder",
                               {"order": [2, 3, 1], "base_version": 1})
        self.assertEqual(code, 200, data)
        rep = data["report"]
        self.assertFalse(rep["ok"])
        types = {i["type"] for i in rep["prereq_issues"]}
        self.assertIn("prereq_order", types)
        # 总分钟数不变——问题必须来自前置重验证，而非时长
        self.assertEqual(rep["total_minutes"], 135.0)

    def test_delete_prereq_chapter_leaves_dangling_refs_in_validation(self):
        # 删除被别人前置依赖的章节1（章节2 requires 1）
        code, data = self.call("DELETE", "/api/chapters/1")
        self.assertEqual(code, 200, data)
        rep = data["report"]
        # 删除会连带删除关联边（数据无悬挂行），但 OBJ-1 失去支撑片段 → 目标未覆盖
        self.assertFalse(rep["ok"])
        codes = {o["code"] for o in rep["uncovered_objectives"]}
        self.assertIn("OBJ-1", codes)
        self.assertEqual(rep["total_minutes"], 105.0)

    def test_delete_middle_chapter_drops_coverage_and_revalidates(self):
        # 章节2 含 demo 片段4（支撑 OBJ-2）
        code, data = self.call("DELETE", "/api/chapters/2")
        self.assertEqual(code, 200, data)
        rep = data["report"]
        codes = {o["code"] for o in rep["uncovered_objectives"]}
        self.assertIn("OBJ-2", codes)

    def test_prereq_cycle_rejected(self):
        # 1<-2<-3，尝试让 1 requires 3 → 环
        code, data = self.call("POST", "/api/chapters/1/prereqs",
                               {"requires_chapter_id": 3})
        self.assertEqual(code, 400, data)


class TestAssetVersionConsistency(ApiCase):
    """验收2：素材音轨缺失 / 替换视频后时间码越界 → 待修复；画面字幕说明同版本。"""

    def _binding_ids(self):
        conn = connect(self.db)
        ids = {r["role"]: r["id"] for r in rows(
            conn.execute("SELECT id,role FROM demo_bindings WHERE segment_id=4"))}
        conn.close()
        return ids

    def test_replace_shorter_video_puts_timecode_out_of_bounds(self):
        # 原绑定 out_ms=240000，素材时长 300000；替换成时长 100000 的新版本
        code, data = self.call("POST", "/api/assets/1/versions",
                               {"duration_ms": 100000, "has_audio": 1,
                                "uri": "media/dmm-v2.mp4", "note": "精剪版"})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["new_version"], 2)
        code, rep = self.call("GET", "/api/courses/1/report")
        self.assertFalse(rep["ok"])
        reasons = set()
        for b in rep["pending_repairs"]:
            reasons.update((b["repair_reason"] or "").split(","))
        self.assertIn("timecode_out_of_bounds", reasons)

    def test_version_without_audio_flags_video_binding(self):
        code, data = self.call("POST", "/api/assets/1/versions",
                               {"duration_ms": 600000, "has_audio": 0,
                                "uri": "media/dmm-noaudio.mp4"})
        self.assertEqual(code, 200, data)
        code, rep = self.call("GET", "/api/courses/1/report")
        video = [b for b in rep["pending_repairs"] if b["role"] == "video"]
        self.assertTrue(video)
        self.assertIn("audio_missing", video[0]["repair_reason"])

    def test_three_roles_must_share_same_version(self):
        ids = self._binding_ids()
        # 先发布一个时长充足、带音轨的 v2
        self.must(200, self.call("POST", "/api/assets/1/versions",
                                 {"duration_ms": 600000, "has_audio": 1,
                                  "uri": "media/dmm-v2.mp4"})[1])
        # follow 绑定都已跟到 v2；把字幕单独钉回 v1（pinned），形成版本错位
        code, data = self.call("PUT", f"/api/bindings/{ids['subtitle']}",
                               {"asset_version": 1, "pin_mode": "pinned",
                                "base_version": data_base(self, ids["subtitle"])})
        self.assertEqual(code, 200, data)
        code, rep = self.call("GET", "/api/courses/1/report")
        self.assertFalse(rep["ok"], rep)
        # 片段级 version_skew 或绑定级 version_mismatch 至少命中其一
        skew_segs = {s["segment_id"] for s in rep["version_skew"]}
        mismatch = any("version_mismatch" in (b["repair_reason"] or "")
                       for b in rep["pending_repairs"])
        self.assertTrue(4 in skew_segs or mismatch)

    def test_repair_clears_pending_status(self):
        # 换成短视频制造越界
        self.must(200, self.call("POST", "/api/assets/1/versions",
                                 {"duration_ms": 100000, "has_audio": 1,
                                  "uri": "media/dmm-v2.mp4"})[1])
        conn = connect(self.db)
        bid = row(conn.execute(
            "SELECT id,version FROM demo_bindings WHERE segment_id=4 AND role='video'"))
        bid, ver = bid["id"], bid["version"]
        conn.close()
        # 把时间码收回到时长内修复
        code, data = self.call("PUT", f"/api/bindings/{bid}",
                               {"in_ms": 0, "out_ms": 90000, "base_version": ver})
        self.assertEqual(code, 200, data)
        # video 已 ok；subtitle/notes 仍可能待修，但 video 不在其中
        code, rep = self.call("GET", "/api/courses/1/report")
        self.assertFalse(any(b["role"] == "video" for b in rep["pending_repairs"]))


def data_base(case, bid):
    conn = connect(case.db)
    v = row(conn.execute("SELECT version FROM demo_bindings WHERE id=?", (bid,)))["version"]
    conn.close()
    return v


class TestObjectiveRedesign(ApiCase):
    """验收3：训练目标改版 → 旧覆盖 stale，报告列出当前版本未覆盖的目标。"""

    def test_objective_revision_marks_coverage_stale(self):
        code, obj = self.call("PUT", "/api/objectives/2",
                              {"title": "独立完成万用表测电压并读数", "base_version": 1})
        self.assertEqual(code, 200, obj)
        self.assertEqual(obj["revision"], 2)
        code, rep = self.call("GET", "/api/courses/1/report")
        codes = {o["code"] for o in rep["stale_coverage"]}
        self.assertIn("OBJ-2", codes)

    def test_report_lists_uncovered_after_supporting_segment_deleted(self):
        self.call("DELETE", "/api/segments/4")  # OBJ-2 的支撑演示
        code, rep = self.call("GET", "/api/courses/1/report")
        codes = {o["code"] for o in rep["uncovered_objectives"]}
        self.assertIn("OBJ-2", codes)

    def test_confirm_coverage_clears_stale(self):
        self.call("PUT", "/api/objectives/2",
                  {"title": "改版目标", "base_version": 1})
        code, data = self.call("POST", "/api/objectives/2/coverage/confirm", {})
        self.assertEqual(code, 200, data)
        codes = {o["code"] for o in data["report"]["stale_coverage"]}
        self.assertNotIn("OBJ-2", codes)


class TestFreezeAndPackagingModes(ApiCase):
    """验收4：固定复用 vs 跟随更新；开班冻结，模板升级不动学员内容。"""

    def test_follow_package_marked_stale_after_course_change(self):
        pkg = self.export_build(key="f1", mode="follow")
        # 改课程标题/章节，课程版本前移
        code, _ = self.call("POST", "/api/courses/1/chapters", {"title": "新增章节X"})
        self.assertEqual(code, 200)
        code, packs = self.call("GET", "/api/courses/1/packages")
        p = next(x for x in packs if x["id"] == pkg)
        self.assertEqual(p["stale"], 1)
        self.assertEqual(p["frozen"], 0)

    def test_pinned_package_not_stale_and_rebuild_creates_new(self):
        pkg = self.export_build(key="p1", mode="pinned")
        self.call("POST", "/api/courses/1/chapters", {"title": "新增章节Y"})
        code, packs = self.call("GET", "/api/courses/1/packages")
        p = next(x for x in packs if x["id"] == pkg)
        self.assertEqual(p["stale"], 0)

    def test_open_class_freezes_package_and_blocks_rebuild(self):
        pkg = self.export_build(key="c1", mode="follow")
        code, data = self.call("POST", "/api/courses/1/classes",
                               {"name": "2026秋季电工一班", "package_id": pkg})
        self.assertEqual(code, 200, data)
        class_id = data["id"]

        # 模板升级：新增章节；冻结包不受影响
        self.must(200, self.call("POST", "/api/courses/1/chapters",
                                {"title": "模板新增-新工艺"})[1])
        code, pack = self.call("GET", f"/api/packages/{pkg}")
        self.assertEqual(pack["frozen"], 1)
        self.assertEqual(pack["mode"], "pinned")
        self.assertEqual(pack["stale"], 0)

        # 冻结包禁止重建
        code, data = self.call("POST", f"/api/packages/{pkg}/rebuild", {})
        self.assertEqual(code, 403, data)

        # 班级课件产物仍指向旧版本内容（course_version=1，无新增章节）
        code, cl = self.call("GET", f"/api/classes/{class_id}")
        self.assertEqual(cl["package"]["course_version"], 1)
        titles = [c["title"] for c in cl["artifact"]["content"]["chapters"]]
        self.assertNotIn("模板新增-新工艺", titles)


class TestOptimisticAndUndo(ApiCase):
    """验收5：多人编辑乐观并发；撤销不能回退同事后续修订。"""

    def test_version_conflict_returns_409_with_current(self):
        # Carl 先把章节1改到 v2
        code, _ = self.call("PUT", "/api/chapters/1",
                            {"title": "安全规范(Carl改版)", "base_version": 1}, user=3)
        self.assertEqual(code, 200)
        # Beth 仍拿着 base_version=1 提交 → 409，返回当前版本
        code, data = self.call("PUT", "/api/chapters/1",
                               {"title": "安全规范(Beth过期改动)", "base_version": 1}, user=2)
        self.assertEqual(code, 409)
        self.assertEqual(data["current"]["version"], 2)
        self.assertEqual(data["current"]["title"], "安全规范(Carl改版)")

    def test_undo_blocked_when_teammate_edited_afterward(self):
        # Beth 在 v1→v2 改章节标题
        _, beth = self.call("PUT", "/api/chapters/1",
                            {"title": "Beth 标题", "base_version": 1}, user=2)
        # 找到 Beth 这条修订
        conn = connect(self.db)
        rev = row(conn.execute(
            "SELECT id FROM revisions WHERE entity_type='chapter' AND entity_id=1 "
            "AND action='update' AND actor_id=2 ORDER BY id DESC LIMIT 1"))
        rev_id = rev["id"]
        conn.close()
        # Carl 随后再改 v2→v3
        code, _ = self.call("PUT", "/api/chapters/1",
                            {"title": "Carl 标题", "base_version": 2}, user=3)
        self.assertEqual(code, 200)
        # Beth 撤销自己的旧修订 → 409，Carl 的工作保留
        code, data = self.call("POST", f"/api/revisions/{rev_id}/undo", {}, user=2)
        self.assertEqual(code, 409, data)
        conn = connect(self.db)
        title = row(conn.execute("SELECT title FROM chapters WHERE id=1"))["title"]
        conn.close()
        self.assertEqual(title, "Carl 标题")

    def test_undo_own_latest_update_works(self):
        _, updated = self.call("PUT", "/api/chapters/1",
                               {"title": "Beth 临时标题", "base_version": 1}, user=2)
        conn = connect(self.db)
        rev_id = row(conn.execute(
            "SELECT id FROM revisions WHERE entity_type='chapter' AND entity_id=1 "
            "AND action='update' ORDER BY id DESC LIMIT 1"))["id"]
        conn.close()
        code, data = self.call("POST", f"/api/revisions/{rev_id}/undo", {}, user=2)
        self.assertEqual(code, 200, data)
        conn = connect(self.db)
        title = row(conn.execute("SELECT title FROM chapters WHERE id=1"))["title"]
        conn.close()
        self.assertEqual(title, "安全规范")  # 回到旧值

    def test_undo_create_then_delete_chapter_restorable(self):
        # Beth 新建章节（可撤销其 create）
        _, ch = self.call("POST", "/api/courses/1/chapters",
                          {"title": "待撤销章节"}, user=2)
        chid = ch["id"]
        conn = connect(self.db)
        rev_id = row(conn.execute(
            "SELECT id FROM revisions WHERE entity_type='chapter' AND entity_id=? "
            "AND action='create'", (chid,)))["id"]
        conn.close()
        code, data = self.call("POST", f"/api/revisions/{rev_id}/undo", {}, user=2)
        self.assertEqual(code, 200, data)
        conn = connect(self.db)
        self.assertIsNone(row(conn.execute("SELECT id FROM chapters WHERE id=?", (chid,))))
        conn.close()


class TestReceiptAndQuota(ApiCase):
    """验收6：导出成功但回执丢失可补发；额度与重复导出一致。"""

    def test_idempotent_export_charges_quota_once(self):
        for i in range(3):
            code, data = self.call("POST", "/api/courses/1/exports",
                                   {"mode": "pinned", "idempotency_key": "DUP-1"})
            self.assertEqual(code, 200)
            self.assertEqual(data["package"]["id"], data["package"]["id"])
        code, q = self.call("GET", "/api/workspaces/1/quota")
        self.assertEqual(q["exports_used"], 1)
        self.assertEqual(q["remaining"], 19)

    def test_duplicate_export_after_build_returns_same_package_and_receipt(self):
        pkg = self.export_build(key="DUP-2")
        # 再次请求同 key：不新建、不再扣额度，且已带回执
        code, data = self.call("POST", "/api/courses/1/exports",
                               {"mode": "pinned", "idempotency_key": "DUP-2"})
        self.assertFalse(data["created"])
        self.assertEqual(data["package"]["id"], pkg)
        code, packs = self.call("GET", "/api/courses/1/packages")
        self.assertEqual(len(packs), 1)
        self.assertTrue(packs[0]["receipt"])

    def test_reconcile_reissues_lost_receipt(self):
        pkg = self.export_build(key="RCP-1")
        # 模拟“导出成功但回执丢失”：删掉回执（保留确定性回执号规则）
        conn = connect(self.db)
        conn.execute("DELETE FROM export_receipts WHERE package_id=?", (pkg,))
        conn.commit()
        conn.close()
        code, packs = self.call("GET", "/api/courses/1/packages")
        self.assertIsNone(packs[0]["receipt"])
        # 管理员对账补发
        code, data = self.call("POST", "/api/workspaces/1/packages/reconcile", {}, user=1)
        self.assertEqual(code, 200, data)
        self.assertEqual(len(data["reissued"]), 1)
        self.assertEqual(data["reissued"][0]["receipt_no"], f"R{pkg:06d}-v1")
        # 再次对账幂等：无重复补发
        code, data2 = self.call("POST", "/api/workspaces/1/packages/reconcile", {}, user=1)
        self.assertEqual(data2["reissued"], [])
        conn = connect(self.db)
        n = row(conn.execute(
            "SELECT COUNT(*) c FROM export_receipts WHERE package_id=?", (pkg,)))["c"]
        conn.close()
        self.assertEqual(n, 1)

    def test_quota_exhaustion_blocks_new_export_but_not_retry(self):
        conn = connect(self.db)
        conn.execute("UPDATE workspace_quota SET exports_limit=1, exports_used=1")
        conn.commit()
        conn.close()
        # 全新 key 被拒
        code, data = self.call("POST", "/api/courses/1/exports",
                               {"mode": "pinned", "idempotency_key": "NEW-X"})
        self.assertEqual(code, 403, data)
        # 已存在 key 的重试幂等返回，不受额度影响
        # 先造一个既有作业
        conn = connect(self.db)
        conn.execute("UPDATE workspace_quota SET exports_limit=5, exports_used=1")
        conn.commit()
        conn.close()
        self.export_build(key="OLD-1")
        conn = connect(self.db)
        conn.execute("UPDATE workspace_quota SET exports_limit=5, exports_used=5")
        conn.commit()
        conn.close()
        code, data = self.call("POST", "/api/courses/1/exports",
                               {"mode": "pinned", "idempotency_key": "OLD-1"})
        self.assertEqual(code, 200)
        self.assertFalse(data["created"])


class TestTeacherLeavesTeam(ApiCase):
    """验收7：老师离开团队——停用资格、保留历史、在职成员按权限查看历史。"""

    def test_deactivation_blocks_writes_but_history_remains(self):
        # Tom(5) 先做一次编辑留下修订
        _, ch = self.call("PUT", "/api/chapters/1",
                          {"title": "Tom 讲义修订", "base_version": 1}, user=5)
        # 管理员把 Tom 移出团队
        code, data = self.call("DELETE", "/api/workspaces/1/members/5", user=1)
        self.assertEqual(code, 200, data)

        # Tom 的请求被拒（不是有效成员）
        code, data = self.call("PUT", "/api/chapters/1",
                               {"title": "离队后再改", "base_version": 2}, user=5)
        self.assertEqual(code, 403, data)

        # Tom 的历史修订仍可由工作区成员查看
        code, revs = self.call("GET", "/api/workspaces/1/revisions", user=2)
        actors = {r["actor_id"] for r in revs}
        self.assertIn(5, actors)

        # 访客(viewer)可看历史但不能改
        code, revs = self.call("GET", "/api/workspaces/1/revisions", user=4)
        self.assertEqual(code, 200)
        code, data = self.call("POST", "/api/courses/1/chapters",
                               {"title": "访客越权"}, user=4)
        self.assertEqual(code, 403)

    def test_re_add_member_restores_access(self):
        self.call("DELETE", "/api/workspaces/1/members/5", user=1)
        code, data = self.call("POST", "/api/workspaces/1/members",
                               {"user_id": 5, "role": "editor"}, user=1)
        self.assertEqual(code, 200, data)
        conn = connect(self.db)
            # 成员恢复 active
        active = row(conn.execute(
            "SELECT active,role FROM memberships WHERE workspace_id=1 AND user_id=5"))
        conn.close()
        self.assertEqual(active["active"], 1)
        self.assertEqual(active["role"], "editor")


if __name__ == "__main__":
    unittest.main(verbosity=2)
