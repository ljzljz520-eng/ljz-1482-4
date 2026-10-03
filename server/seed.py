# -*- coding: utf-8 -*-
"""演示数据：工作区、成员、课程、章节、目标、素材、绑定、配额。"""
from .db import connect, init_db, row


def seed_if_empty(db_path):
    conn = connect(db_path)
    try:
        init_db(conn)
        if row(conn.execute("SELECT id FROM workspaces LIMIT 1")) is not None:
            conn.close()
            return False

        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO workspaces(name) VALUES('职业技能培训院')")
        ws = 1
        conn.execute(
            "INSERT INTO workspace_quota(workspace_id,exports_limit,exports_used) VALUES(1,20,0)")

        users = [
            ("Anna 管理员", "admin"),
            ("Beth 编辑", "editor"),
            ("Carl 编辑", "editor"),
            ("Vera 访客", "viewer"),
            ("Tom 讲师", "editor"),
        ]
        for i, (name, role) in enumerate(users, start=1):
            conn.execute("INSERT INTO users(id,name) VALUES(?,?)", (i, name))
            conn.execute("INSERT INTO memberships(workspace_id,user_id,role) VALUES(1,?,?)",
                         (i, role))

        conn.execute(
            "INSERT INTO courses(id,workspace_id,title,created_by,created_at) "
            "VALUES(1,1,'电工基础实训',1,datetime('now'))")

        chapters = [("安全规范", 1, 30), ("工具使用", 2, 45), ("线路装配", 3, 60)]
        for i, (t, p, m) in enumerate(chapters, start=1):
            conn.execute(
                "INSERT INTO chapters(id,course_id,title,position,minutes) VALUES(?,1,?,?,?)",
                (i, t, p, m))
        conn.execute(
            "INSERT INTO prereq_edges(course_id,chapter_id,requires_chapter_id) VALUES(1,2,1)")
        conn.execute(
            "INSERT INTO prereq_edges(course_id,chapter_id,requires_chapter_id) VALUES(1,3,2)")

        segs = [
            (1, 1, "lecture", "开场与安全意识", "各位学员，安全是电工操作的第一原则……", 1, 10),
            (2, 1, "exercise", "安全规范随堂练", "判断下列操作是否合规……", 2, 5),
            (3, 2, "lecture", "万用表介绍", "万用表分为指针式与数字式……", 1, 15),
            (4, 2, "demo", "万用表测电压演示", "演示用万用表测量直流电压的完整流程", 2, 20),
            (5, 3, "lecture", "装配流程讲解", "按照图纸依次完成进线、开关与插座……", 1, 25),
        ]
        for sid, ch, kind, t, body, pos, mins in segs:
            conn.execute(
                "INSERT INTO segments(id,chapter_id,kind,title,body,position,minutes) "
                "VALUES(?,?,?,?,?,?,?)", (sid, ch, kind, t, body, pos, mins))

        objectives = [
            ("OBJ-1", "能说出三条高压安全红线", "考核安全规范掌握"),
            ("OBJ-2", "独立完成万用表测电压", "考核仪表操作"),
            ("OBJ-3", "按图完成单控线路装配", "考核装配工艺"),
        ]
        for i, (code, t, d) in enumerate(objectives, start=1):
            conn.execute(
                "INSERT INTO objectives(id,course_id,code,title,description) VALUES(?,1,?,?,?)",
                (i, code, t, d))
            conn.execute(
                "INSERT INTO objective_revisions(objective_id,revision,title,description,"
                "changed_by,changed_at) VALUES(?,1,?,?,1,datetime('now'))", (i, t, d))

        conn.execute(
            "INSERT INTO objective_coverage(objective_id,segment_id,created_by) VALUES(1,1,1)")
        conn.execute(
            "INSERT INTO objective_coverage(objective_id,segment_id,created_by) VALUES(2,4,1)")
        conn.execute(
            "INSERT INTO objective_coverage(objective_id,segment_id,created_by) VALUES(3,5,1)")

        conn.execute(
            "INSERT INTO assets(id,workspace_id,name,kind) VALUES(1,1,'万用表操作演示.mp4','video')")
        conn.execute(
            "INSERT INTO asset_versions(asset_id,version,duration_ms,has_audio,uri,created_by,"
            "created_at) VALUES(1,1,300000,1,'media/dmm-v1.mp4',1,datetime('now'))")
        for role, in_ms, out_ms in (("video", 0, 240000),
                                    ("subtitle", 0, 240000),
                                    ("notes", 5000, 60000)):
            conn.execute(
                "INSERT INTO demo_bindings(segment_id,role,asset_id,asset_version,pin_mode,"
                "in_ms,out_ms) VALUES(4,?,1,1,'follow',?,?)", (role, in_ms, out_ms))

        conn.commit()
    finally:
        conn.close()
    return True
