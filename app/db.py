import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config
from app.policy import binding_code, policy_hash


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None：事务由 get_db 显式管理，使用 BEGIN IMMEDIATE。
    conn = sqlite3.connect(
        path,
        check_same_thread=False,
        timeout=15,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    """所有读写都走 BEGIN IMMEDIATE：深度改配与追索/落边串行化，避免双写。

    SQLite 的 IMMEDIATE 事务一开始就取 RESERVED 写锁；同库另一写入方
    （改深度、追索落证、补血缘边）会在 busy_timeout 内排队，不会读到
    半写状态，也不会并发插入同一版本或同一条边。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  column_name TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('现行','停用')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS run_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  run_code TEXT NOT NULL UNIQUE,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待盖印','已盖印','已作废')),
  watermark_value TEXT,
  stamped_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_run_code TEXT NOT NULL,
  downstream_run_code TEXT NOT NULL,
  UNIQUE(upstream_run_code, downstream_run_code)
);

CREATE TABLE IF NOT EXISTS gap_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  stamped_count INTEGER NOT NULL,
  gap_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  policy_version INTEGER NOT NULL DEFAULT 0,
  binding_code TEXT NOT NULL DEFAULT '',
  depth_limit INTEGER NOT NULL DEFAULT 0,
  blocked_count INTEGER NOT NULL DEFAULT 0,
  blocked_codes TEXT NOT NULL DEFAULT '[]',
  outcome TEXT NOT NULL DEFAULT '放行',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 旁路版本表：深度的唯一凭证来源。追加式写入，旧版只读保留。
CREATE TABLE IF NOT EXISTS trace_depth_policies (
  version_no INTEGER PRIMARY KEY,
  depth_limit INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  binding_code TEXT NOT NULL UNIQUE,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TRIGGER IF NOT EXISTS trace_depth_policies_no_update
BEFORE UPDATE ON trace_depth_policies
BEGIN
  SELECT RAISE(ABORT, '深度策略历史只读');
END;

CREATE TRIGGER IF NOT EXISTS trace_depth_policies_no_delete
BEFORE DELETE ON trace_depth_policies
BEGIN
  SELECT RAISE(ABORT, '深度策略历史只读');
END;

-- 每次追索的裁定留证：绑码、深度、放行/拒绝、超深下游。
CREATE TABLE IF NOT EXISTS trace_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  watermark_value TEXT NOT NULL,
  policy_version INTEGER NOT NULL,
  binding_code TEXT NOT NULL,
  depth_limit INTEGER NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('放行','拒绝')),
  max_depth INTEGER NOT NULL DEFAULT 0,
  blocked_count INTEGER NOT NULL DEFAULT 0,
  source_codes TEXT NOT NULL DEFAULT '[]',
  downstream_codes TEXT NOT NULL DEFAULT '[]',
  blocked_codes TEXT NOT NULL DEFAULT '[]',
  node_depths TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 每次追索走过的边：库内留证，深链不受后续改边的掩盖。
CREATE TABLE IF NOT EXISTS trace_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_run_id INTEGER NOT NULL,
  upstream_run_code TEXT NOT NULL,
  downstream_run_code TEXT NOT NULL,
  depth INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(trace_run_id, upstream_run_code, downstream_run_code)
);

-- 缺口复验留证：超深下游在复验中必须仍被拒。
CREATE TABLE IF NOT EXISTS gap_verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gap_report_id INTEGER NOT NULL,
  policy_version INTEGER NOT NULL,
  binding_code TEXT NOT NULL,
  depth_limit INTEGER NOT NULL,
  blocked_count INTEGER NOT NULL DEFAULT 0,
  blocked_codes TEXT NOT NULL DEFAULT '[]',
  outcome TEXT NOT NULL DEFAULT '放行',
  stale INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# 旧库补列（新库由 SCHEMA 直接建好）。
_MIGRATIONS = [
    ("gap_reports", "policy_version", "INTEGER NOT NULL DEFAULT 0"),
    ("gap_reports", "binding_code", "TEXT NOT NULL DEFAULT ''"),
    ("gap_reports", "depth_limit", "INTEGER NOT NULL DEFAULT 0"),
    ("gap_reports", "blocked_count", "INTEGER NOT NULL DEFAULT 0"),
    ("gap_reports", "blocked_codes", "TEXT NOT NULL DEFAULT '[]'"),
    ("gap_reports", "outcome", "TEXT NOT NULL DEFAULT '放行'"),
]

SEED_TEMPLATES = [
    ("dw_orders_d", "wm_batch", 1, "现行"),
    ("dw_payments_d", "wm_batch", 1, "现行"),
    ("dm_gmv_d", "wm_trace", 1, "现行"),
]

SEED_BATCHES = [
    ("dw_orders_d", "ord-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_payments_d", "pay-20261114-01", "2026-11-14", "待盖印", None),
    ("dm_gmv_d", "gmv-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_orders_d", "ord-20261113-01", "2026-11-13", "已盖印", "WM-ORD-131"),
]

SEED_EDGES = [
    ("ord-20261114-01", "gmv-20261114-01"),
    ("pay-20261114-01", "gmv-20261114-01"),
    ("ord-20261113-01", "gmv-20261114-01"),
]

# 默认上限 2 层下，这条 3 跳深链的末端属于“超深下游”，用于演示追索拒绝、
# 批次列表拦截、缺口排除与复验仍拒。
DEPTH_DEMO_TEMPLATES = [
    ("dw_agg_d", "wm_batch", 1, "现行"),
    ("dm_report_d", "wm_trace", 1, "现行"),
    ("dm_export_d", "wm_trace", 1, "现行"),
    ("dw_push_d", "wm_batch", 1, "现行"),
]

DEPTH_DEMO_BATCHES = [
    ("dw_agg_d", "agg-20261110-01", "2026-11-10", "已盖印", "WM-DEEP-131"),
    ("dm_report_d", "rep-20261110-01", "2026-11-10", "待盖印", None),
    ("dm_export_d", "exp-20261110-01", "2026-11-10", "待盖印", None),
    ("dw_push_d", "psh-20261110-01", "2026-11-10", "待盖印", None),
]

DEPTH_DEMO_EDGES = [
    ("agg-20261110-01", "rep-20261110-01"),
    ("rep-20261110-01", "exp-20261110-01"),
    ("exp-20261110-01", "psh-20261110-01"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {
            r["name"]
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _insert_batch(conn: sqlite3.Connection, row: tuple, ignore: bool = False) -> None:
    table, code, biz, status, wm = row
    verb = "INSERT OR IGNORE" if ignore else "INSERT"
    conn.execute(
        f"{verb} INTO run_batches"
        "(table_name, run_code, biz_date, status, watermark_value, stamped_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            table,
            code,
            biz,
            status,
            wm,
            f"{biz} 18:00:00" if status == "已盖印" else None,
        ),
    )


def _seed(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
    if not row:
        for table, col, ver, status in SEED_TEMPLATES:
            conn.execute(
                "INSERT INTO watermark_templates(table_name, column_name, version_no, status) "
                "VALUES (?,?,?,?)",
                (table, col, ver, status),
            )
        for batch in SEED_BATCHES:
            _insert_batch(conn, batch)
        for up, down in SEED_EDGES:
            conn.execute(
                "INSERT INTO lineage_edges(upstream_run_code, downstream_run_code) VALUES (?,?)",
                (up, down),
            )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")

    # 深链演示数据独立补齐，旧库升级后也能看到深度限制效果。
    for table, col, ver, status in DEPTH_DEMO_TEMPLATES:
        conn.execute(
            "INSERT OR IGNORE INTO watermark_templates"
            "(table_name, column_name, version_no, status) VALUES (?,?,?,?)",
            (table, col, ver, status),
        )
    for batch in DEPTH_DEMO_BATCHES:
        _insert_batch(conn, batch, ignore=True)
    for up, down in DEPTH_DEMO_EDGES:
        conn.execute(
            "INSERT OR IGNORE INTO lineage_edges"
            "(upstream_run_code, downstream_run_code) VALUES (?,?)",
            (up, down),
        )

    count = conn.execute("SELECT COUNT(*) AS c FROM trace_depth_policies").fetchone()["c"]
    if count == 0:
        version = 1
        limit = Config.DEFAULT_DEPTH_LIMIT
        content = policy_hash(version, limit)
        conn.execute(
            "INSERT INTO trace_depth_policies"
            "(version_no, depth_limit, content_hash, binding_code, note) "
            "VALUES (?,?,?,?,?)",
            (version, limit, content, binding_code(version, content), "初始深度上限"),
        )


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _seed(conn)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        conn.close()
