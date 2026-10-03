# -*- coding: utf-8 -*-
"""启动脚本：初始化数据库 + 种子数据 + 后台工作器 + HTTP 服务。"""
import argparse
import os

from server.db import connect, init_db
from server.seed import seed_if_empty
from server.worker import Worker
from server.app import make_server

DEFAULT_DB = os.environ.get("STORYBOARD_DB", "data/storyboard.db")
DEFAULT_ARTIFACTS = os.environ.get("ARTIFACTS_DIR", "artifacts")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--artifacts", default=DEFAULT_ARTIFACTS)
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--no-worker", action="store_true")
    args = ap.parse_args()

    conn = connect(args.db)
    init_db(conn)
    conn.close()
    seed_if_empty(args.db)

    worker = None
    if not args.no_worker:
        worker = Worker(args.db, args.artifacts, interval=0.2)
        worker.start()

    httpd = make_server(args.db, STATIC_DIR, args.port)
    print(f"职业培训分镜应用已启动: http://localhost:{args.port}  (db={args.db})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if worker:
            worker.stop()


if __name__ == "__main__":
    main()
