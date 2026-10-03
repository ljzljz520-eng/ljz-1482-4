# -*- coding: utf-8 -*-
"""SQLite 持久层：连接、Schema、事务助手。仅使用标准库。"""
import os
import sqlite3

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS workspaces(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships(
  workspace_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','editor','viewer')),
  active INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(workspace_id, user_id)
);
CREATE TABLE IF NOT EXISTS workspace_quota(
  workspace_id INTEGER PRIMARY KEY,
  exports_limit INTEGER NOT NULL DEFAULT 20,
  exports_used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS courses(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  version INTEGER NOT NULL DEFAULT 1,
  created_by INTEGER,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS chapters(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  position INTEGER NOT NULL,
  minutes REAL NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS segments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chapter_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('lecture','demo','exercise')),
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL DEFAULT 0,
  minutes REAL NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS prereq_edges(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL,
  chapter_id INTEGER NOT NULL,
  requires_chapter_id INTEGER NOT NULL,
  UNIQUE(chapter_id, requires_chapter_id)
);
CREATE TABLE IF NOT EXISTS objectives(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active',
  version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS objective_revisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  objective_id INTEGER NOT NULL,
  revision INTEGER NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  changed_by INTEGER,
  changed_at TEXT
);
CREATE TABLE IF NOT EXISTS objective_coverage(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  objective_id INTEGER NOT NULL,
  segment_id INTEGER NOT NULL,
  stale INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER,
  UNIQUE(objective_id, segment_id)
);
CREATE TABLE IF NOT EXISTS assets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'video',
  current_version INTEGER NOT NULL DEFAULT 1,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS asset_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL,
  has_audio INTEGER NOT NULL DEFAULT 1,
  uri TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_by INTEGER,
  created_at TEXT,
  UNIQUE(asset_id, version)
);
CREATE TABLE IF NOT EXISTS demo_bindings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  segment_id INTEGER NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('video','subtitle','notes')),
  asset_id INTEGER NOT NULL,
  asset_version INTEGER NOT NULL,
  pin_mode TEXT NOT NULL DEFAULT 'follow' CHECK(pin_mode IN ('follow','pinned')),
  in_ms INTEGER NOT NULL DEFAULT 0,
  out_ms INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ok' CHECK(status IN ('ok','pending_repair')),
  repair_reason TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  UNIQUE(segment_id, role)
);
CREATE TABLE IF NOT EXISTS packages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL,
  course_version INTEGER NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('pinned','follow')),
  status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','building','done','failed')),
  artifact_uri TEXT,
  idempotency_key TEXT NOT NULL UNIQUE,
  requested_by INTEGER,
  created_at TEXT,
  finished_at TEXT,
  frozen INTEGER NOT NULL DEFAULT 0,
  stale INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS export_receipts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  package_id INTEGER NOT NULL UNIQUE,
  receipt_no TEXT NOT NULL UNIQUE,
  issued_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS classes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  package_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  started_at TEXT,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued',
  run_after REAL NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT UNIQUE,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS revisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL,
  course_id INTEGER,
  entity_type TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  action TEXT NOT NULL,
  actor_id INTEGER,
  actor_name TEXT,
  before_json TEXT,
  after_json TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rev_entity ON revisions(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_rev_course ON revisions(course_id);
CREATE INDEX IF NOT EXISTS idx_seg_chapter ON segments(chapter_id);
CREATE INDEX IF NOT EXISTS idx_ch_course ON chapters(course_id);
"""


def connect(db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)


def begin_immediate(conn):
    conn.execute("BEGIN IMMEDIATE")


def rows(cur):
    """fetchall 助手：rows(conn.execute(...))。"""
    return cur.fetchall()


def row(cur):
    """fetchone 助手：row(conn.execute(...))。"""
    return cur.fetchone()
