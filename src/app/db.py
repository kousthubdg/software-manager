"""SQLite state under SM_STATE_DIR (Software Manager installs Postgres, so it cannot depend on it)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import STATE

DB_FILE = STATE / "software-manager.db"
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, role TEXT NOT NULL,
  disabled INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS releases (
  version TEXT PRIMARY KEY, software_name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  manifest_text TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, uploaded_by TEXT NOT NULL, uploaded_at REAL NOT NULL,
  status TEXT NOT NULL, scan_json TEXT NOT NULL DEFAULT '{}', resolved_json TEXT NOT NULL DEFAULT '{}',
  progress_json TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS nodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, ip TEXT NOT NULL, port INTEGER NOT NULL,
  token_enc TEXT NOT NULL, added_at REAL NOT NULL, info_json TEXT NOT NULL DEFAULT '{}',
  pods_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'unknown', last_seen REAL NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '', UNIQUE(ip, port));
CREATE TABLE IF NOT EXISTS deployments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, software_name TEXT NOT NULL,
  version TEXT NOT NULL, prev_version TEXT NOT NULL DEFAULT '', alarm_base_path TEXT NOT NULL,
  map_json TEXT NOT NULL, state TEXT NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL,
  updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, deployment_id INTEGER, kind TEXT NOT NULL, status TEXT NOT NULL,
  params_json TEXT NOT NULL DEFAULT '{}', steps_json TEXT NOT NULL DEFAULT '[]', log_text TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL, created_at REAL NOT NULL, started_at REAL, finished_at REAL);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, username TEXT NOT NULL, action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS registries (
  host TEXT PRIMARY KEY, username TEXT NOT NULL DEFAULT '', password_enc TEXT NOT NULL DEFAULT '',
  ca_pem TEXT NOT NULL DEFAULT '', insecure INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS alerts (
  key TEXT PRIMARY KEY, first_ts REAL NOT NULL, last_ts REAL NOT NULL, message TEXT NOT NULL, active INTEGER NOT NULL);
"""


def _connect() -> sqlite3.Connection:
    c = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


_conn = _connect()
_conn.executescript(SCHEMA)

# columns added after the first release; ADD COLUMN keeps existing rows
_COLUMNS = {
    "releases": {
        "revision": "INTEGER NOT NULL DEFAULT 1",
        "reupload_json": "TEXT NOT NULL DEFAULT ''",
    },
    "deployments": {
        "installed_json": "TEXT NOT NULL DEFAULT ''",
    },
}
for _table, _cols in _COLUMNS.items():
    _have = {r["name"] for r in _conn.execute(f"PRAGMA table_info({_table})").fetchall()}
    for _col, _decl in _cols.items():
        if _col not in _have:
            _conn.execute(f"ALTER TABLE {_table} ADD COLUMN {_col} {_decl}")


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    with _lock:
        _conn.execute("BEGIN IMMEDIATE")
        try:
            yield _conn
            _conn.execute("COMMIT")
        except Exception:
            _conn.execute("ROLLBACK")
            raise


def q(sql: str, *args: Any) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def one(sql: str, *args: Any) -> dict | None:
    rows = q(sql, *args)
    return rows[0] if rows else None


def ex(sql: str, *args: Any) -> int:
    with _lock:
        cur = _conn.execute(sql, args)
        return cur.lastrowid or cur.rowcount


def kv_get(key: str, default: Any = None) -> Any:
    r = one("SELECT value FROM kv WHERE key=?", key)
    return json.loads(r["value"]) if r else default


def kv_set(key: str, value: Any) -> None:
    ex("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       key, json.dumps(value))


def audit(user: str, action: str, target: str = "", detail: Any = "", ip: str = "") -> None:
    if not isinstance(detail, str):
        detail = json.dumps(detail)
    ex("INSERT INTO audit(ts,username,action,target,detail,ip) VALUES(?,?,?,?,?,?)",
       time.time(), user, action, target, detail[:4000], ip)
