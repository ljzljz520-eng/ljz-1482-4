# -*- coding: utf-8 -*-
'''SQLite 持久层：单连接 + RLock（多线程 HTTP 与后台工作器共享）。'''
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id INTEGER NOT NULL REFERENCES teams(id),
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','editor','viewer')),
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS quotas(
  team_id INTEGER PRIMARY KEY REFERENCES teams(id),
  export_remaining INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS courses(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id INTEGER NOT NULL REFERENCES teams(id),
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft'
);
CREATE TABLE IF NOT EXISTS objectives(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL REFERENCES courses(id),
  code TEXT NOT NULL,
  title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chapters(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL REFERENCES courses(id),
  title TEXT NOT NULL,
  position INTEGER NOT NULL,
  requires TEXT NOT NULL DEFAULT '[]'   -- JSON: 前置章节 id 列表
);
CREATE TABLE IF NOT EXISTS segments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chapter_id INTEGER NOT NULL REFERENCES chapters(id),
  kind TEXT NOT NULL CHECK(kind IN ('lecture','demo','exercise')),
  title TEXT NOT NULL,
  script TEXT NOT NULL DEFAULT '',       -- 讲师台词 / 练习提示文案
  position INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok',     -- ok | pending_repair
  asset_version_id INTEGER,              -- demo 绑定的素材版本（画面+字幕+说明同源）
  timecodes TEXT NOT NULL DEFAULT '[]'   -- JSON: [{label, at_ms}]
);
CREATE TABLE IF NOT EXISTS assessment_points(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL REFERENCES courses(id),
  objective_id INTEGER NOT NULL,         -- 考核点 -> 训练目标
  segment_id INTEGER NOT NULL,           -- 考核点 -> 支撑它的教学片段
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS assets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id INTEGER NOT NULL REFERENCES teams(id),
  title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL REFERENCES assets(id),
  version INTEGER NOT NULL,
  video_uri TEXT,          -- 画面
  subtitle_uri TEXT,       -- 字幕
  notes_uri TEXT,          -- 说明
  duration_ms INTEGER NOT NULL DEFAULT 0,
  has_audio INTEGER NOT NULL DEFAULT 1,  -- 音轨是否存在
  UNIQUE(asset_id, version)
);
CREATE TABLE IF NOT EXISTS segment_issues(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  segment_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  resolved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS packages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL REFERENCES courses(id),
  version INTEGER NOT NULL,
  snapshot TEXT NOT NULL,   -- 不可变快照（冻结规则的载体）
  issues TEXT NOT NULL DEFAULT '[]',
  sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id INTEGER NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,  -- 幂等键：重复导出作业去重
  type TEXT NOT NULL,
  status TEXT NOT NULL,                  -- queued|running|succeeded|failed
  payload TEXT NOT NULL DEFAULT '{}',
  result TEXT,
  receipt TEXT,                          -- 回执持久化，丢失可重取
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS classes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL REFERENCES courses(id),
  name TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('pinned','follow')),  -- 固定版本复用 | 模块跟随更新
  pinned_package_id INTEGER REFERENCES packages(id),
  started INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  rev INTEGER NOT NULL,
  actor_id INTEGER,
  action TEXT NOT NULL,     -- create|update|delete|undo
  before TEXT,
  after TEXT,
  course_id INTEGER,        -- 归属课程，便于按课程查历史
  undoes INTEGER,           -- 若是撤销，指向被撤销的 revision id
  ts TEXT NOT NULL,
  UNIQUE(entity, entity_id, rev)
);
CREATE TABLE IF NOT EXISTS entity_heads(
  entity TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  head_rev INTEGER NOT NULL,
  PRIMARY KEY(entity, entity_id)
);
"""


class DB:
    def __init__(self, path):
        self.path = path
        # check_same_thread=False：HTTP 线程与 worker 线程共享同一连接；RLock 串行化。
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.lock = threading.RLock()
        self.init()

    def init(self):
        with self.lock:
            self.conn.executescript(SCHEMA)

    @contextmanager
    def txn(self):
        '''写事务：BEGIN IMMEDIATE ... COMMIT / ROLLBACK，全程持锁。'''
        with self.lock:
            try:
                self.conn.execute('BEGIN IMMEDIATE')
                yield self.conn
                self.conn.execute('COMMIT')
            except Exception:
                self.conn.execute('ROLLBACK')
                raise

    def q(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def q1(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()
