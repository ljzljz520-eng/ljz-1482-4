# -*- coding: utf-8 -*-
'''服务入口：API + 静态 Web 端 + 后台工作器线程。仅依赖标准库。'''
import argparse
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import api, worker
from .db import DB

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static')
_CTYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.ico': 'image/x-icon',
}


def make_handler(db):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def _send(self, status, body, ctype='application/json; charset=utf-8'):
            if isinstance(body, str):
                body = body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_static(self):
            rel = self.path.split('?', 1)[0]
            if rel == '/' or rel == '':
                rel = '/index.html'
            rel = rel.lstrip('/')
            fpath = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not fpath.startswith(STATIC_DIR) or not os.path.isfile(fpath):
                self._send(404, '{"error":"not_found"}')
                return
            ext = os.path.splitext(fpath)[1]
            with open(fpath, 'rb') as f:
                self._send(200, f.read(), _CTYPES.get(ext, 'application/octet-stream'))

        def do_GET(self):
            # 先尝试 API 路由（/courses、/jobs 等位于根路径）；未命中再回退静态资源
            import json as _json
            path = self.path
            if path.startswith('/api'):
                path = path[4:]
            if path.split('?', 1)[0] not in ('/', ''):
                status, resp = api.dispatch(db, 'GET', path, self.headers, b'')
                if not (status == 404 and resp.get('error') == 'no_route'):
                    self._send(status, _json.dumps(resp, ensure_ascii=False))
                    return
            self._serve_static()

        def do_POST(self):
            self._handle('POST')

        def do_PUT(self):
            self._handle('PUT')

        def do_DELETE(self):
            self._handle('DELETE')

        def _handle(self, method):
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(length) if length else b''
            path = self.path
            if path.startswith('/api'):
                path = path[4:]
            import json as _json
            status, resp = api.dispatch(db, method, path, self.headers, raw)
            self._send(status, _json.dumps(resp, ensure_ascii=False))

        def log_message(self, *args):
            pass

    return Handler


def create_server(db_path='studio.db', port=8000, worker_interval=0.2):
    db = DB(db_path)
    stop = threading.Event()
    t = threading.Thread(target=worker.loop, args=(db, worker_interval, stop), daemon=True)
    t.start()
    httpd = ThreadingHTTPServer(('127.0.0.1', port), make_handler(db))
    return httpd, db, stop


def main():
    ap = argparse.ArgumentParser(description='职业培训分镜应用')
    ap.add_argument('--db', default='studio.db')
    ap.add_argument('--port', type=int, default=8000)
    httpd, db, stop = create_server(ap.parse_args().db, ap.parse_args().port)
    print('▶ 培训分镜应用已启动: http://127.0.0.1:%d  (数据库: %s)'
          % (ap.parse_args().port, ap.parse_args().db))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        stop.set()
        httpd.shutdown()


if __name__ == '__main__':
    main()
