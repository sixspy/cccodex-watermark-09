"""一站式控制台快照：深度卡片、追索详情、批次列表、缺口台同查。

四处同查必须在**同一个 IMMEDIATE 事务**内完成：读到的是同一个绑码/版号，
不会出现“卡片亮 v2、列表按 v1 标注”的错位。
"""

import sqlite3

from app.db import get_db
from app.services.batches import _annotate
from app.services.depth_policy import parse_codes, policy_state_in
from app.services.lineage import build_depth_map, stamped_roots


def _reports(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM gap_reports ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["blocked_codes"] = parse_codes(item.get("blocked_codes", "[]"))
        out.append(item)
    return out


def _verifications(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM gap_verifications ORDER BY id DESC LIMIT 50"
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["blocked_codes"] = parse_codes(item["blocked_codes"])
        out.append(item)
    return out


def _trace_runs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM trace_runs ORDER BY id DESC LIMIT 50"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "watermark_value": r["watermark_value"],
            "policy_version": r["policy_version"],
            "binding_code": r["binding_code"],
            "depth_limit": r["depth_limit"],
            "outcome": r["outcome"],
            "max_depth": r["max_depth"],
            "blocked_count": r["blocked_count"],
            "blocked_codes": parse_codes(r["blocked_codes"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def console_snapshot(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        state = policy_state_in(conn)
        limit = state["current"]["depth_limit"]
        depth, _ = build_depth_map(conn, stamped_roots(conn), hard_cap=100)
        rows = conn.execute("SELECT * FROM run_batches ORDER BY id").fetchall()
        batches = _annotate(conn, [dict(r) for r in rows], limit, depth)
        templates = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM watermark_templates ORDER BY id"
            ).fetchall()
        ]
        return {
            "policy": state,
            "binding_code": state["current"]["binding_code"],
            "templates": templates,
            "batches": batches,
            "trace_runs": _trace_runs(conn),
            "gap_reports": _reports(conn),
            "verifications": _verifications(conn),
        }
