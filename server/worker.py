# -*- coding: utf-8 -*-
"""后台工作器：轮询 jobs 表，构建课件包；包产物与回执在同一事务提交。"""
import json
import threading
import time
import traceback

from . import domain as D
from .db import connect, begin_immediate, row


class Worker:
    def __init__(self, db_path, artifacts_dir, interval=0.2):
        self.db_path = db_path
        self.artifacts_dir = artifacts_dir
        self.interval = interval
        self._stop = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name="package-worker", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        if self.thread:
            self.thread.join(timeout=5)

    def run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # 工作循环不能因单次异常退出
                traceback.print_exc()
                time.sleep(0.5)
            self._stop.wait(self.interval)

    def tick(self):
        conn = connect(self.db_path)
        claimed = None
        try:
            # 1) 认领一个作业（独立事务，立即提交）
            begin_immediate(conn)
            job = row(conn.execute(
                "SELECT * FROM jobs WHERE status='queued' AND run_after<=? "
                "ORDER BY id LIMIT 1", (time.time(),)))
            if job is None:
                conn.execute("COMMIT")
                return False
            conn.execute("UPDATE jobs SET status='running', attempts=attempts+1 WHERE id=?",
                         (job["id"],))
            conn.execute("COMMIT")
            claimed = dict(job)
            claimed["attempts"] += 1

            # 2) 执行：产物写盘 + 回执 + 包状态在同一事务提交
            try:
                self.process(conn, job)
                begin_immediate(conn)
                conn.execute("UPDATE jobs SET status='done' WHERE id=?", (job["id"],))
                conn.execute("COMMIT")
            except Exception as exc:
                traceback.print_exc()
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                self._fail_or_retry(conn, job, exc)
            return True
        finally:
            conn.close()

    def _fail_or_retry(self, conn, job, exc):
        begin_immediate(conn)
        if job["attempts"] + 1 >= 3:
            payload = json.loads(job["payload"] or "{}")
            if job["kind"] == "build_package" and "package_id" in payload:
                conn.execute(
                    "UPDATE packages SET status='failed', error=? WHERE id=?",
                    (str(exc), payload["package_id"]))
            conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (job["id"],))
        else:
            conn.execute(
                "UPDATE jobs SET status='queued', run_after=? WHERE id=?",
                (time.time() + 2 ** (job["attempts"] + 1), job["id"]))
        conn.execute("COMMIT")

    def process(self, conn, job):
        kind = job["kind"]
        if kind != "build_package":
            raise ValueError(f"未知作业类型 {kind}")
        payload = json.loads(job["payload"] or "{}")
        pkg = row(conn.execute("SELECT * FROM packages WHERE id=?", (payload["package_id"],)))
        if pkg is None:
            raise ValueError("package 不存在")
        # 整个构建在一个事务内：building → 写产物 → 插回执 → done，要么全成要么全败
        begin_immediate(conn)
        conn.execute("UPDATE packages SET status='building' WHERE id=?", (pkg["id"],))
        D.build_package(conn, pkg, self.artifacts_dir)
        conn.execute("COMMIT")


def run_once(db_path, artifacts_dir):
    """同步处理一个作业（测试/脚本用），返回是否处理了作业。"""
    return Worker(db_path, artifacts_dir).tick()
