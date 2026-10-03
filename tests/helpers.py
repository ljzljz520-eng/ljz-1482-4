# -*- coding: utf-8 -*-
"""测试辅助：临时数据库 + 直接调用 HTTP handler（不起端口）。"""
import json
import os
import shutil
import tempfile

from server.db import connect, init_db
from server.seed import seed_if_empty
from server.app import make_handler
from server import worker


class Resp:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.body = b""

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers[k] = v

    def end_headers(self):
        pass

    def wfile_write(self, b):
        self.body += b


class Harness:
    """内存式 HTTP 调用：直接跑 BaseHTTPRequestHandler 的分发逻辑。"""
    def __init__(self, db_path):
        self.db_path = db_path
        self.artifacts = os.path.join(os.path.dirname(db_path), "artifacts")
        Handler = make_handler(db_path, os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "..", "web"))
        self.Handler = Handler

    def call(self, method, path, body=None, user=2):
        h = object.__new__(self.Handler)
        h.headers = {"X-User-Id": str(user)} if user else {}
        h.path = path
        h.rfile = None
        payload = json.dumps(body or {}).encode()
        h.headers["Content-Length"] = str(len(payload))

        resp = Resp()
        h.send_response = resp.send_response
        h.send_header = resp.send_header
        h.end_headers = resp.end_headers
        h.wfile = type("W", (), {"write": lambda self, b: resp.wfile_write(b)})()

        raw = payload

        # 复刻 _dispatch，对 body_of 做本地替换
        def body_of_local(handler):
            return json.loads(raw.decode() or "{}")

        import server.app as app
        orig = app.body_of
        app.body_of = body_of_local
        try:
            h._dispatch(method)
        finally:
            app.body_of = orig
        try:
            data = json.loads(resp.body.decode())
        except Exception:
            data = resp.body
        return resp.status, data

    def work_once(self):
        return worker.run_once(self.db_path, self.artifacts)


def fresh(tmpdir):
    db = os.path.join(tmpdir, "t.db")
    seed_if_empty(db)
    return db


# 常用角色 ID（见 seed）：1 admin, 2 editor Beth, 3 editor Carl, 4 viewer Vera, 5 editor Tom
