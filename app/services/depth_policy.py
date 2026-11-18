"""追索深度策略：旁路版本表 + 事务内判定。

规则：
- 每次追索在 **同一个 BEGIN IMMEDIATE 事务** 内读取当前深度版本并完成裁定；
  改深度也必须在该事务写锁下追加新版本，因此不存在“读到旧深度还放行”的窗口。
- 版本表追加式写入，UPDATE/DELETE 由触发器拒绝，旧版永久只读留证。
- 绑码 ``DP-v{版号}-{内容哈希前12位}`` 是页面、库内、缺口复验共用的唯一口径。
"""

import json
import sqlite3

from app.config import Config
from app.db import get_db
from app.policy import binding_code, policy_hash
from app.services.errors import ServiceError


def _row_to_policy(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "version_no": row["version_no"],
        "depth_limit": row["depth_limit"],
        "content_hash": row["content_hash"],
        "binding_code": row["binding_code"],
        "note": row["note"],
        "created_at": row["created_at"],
    }


def _head(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT * FROM trace_depth_policies ORDER BY version_no DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise ServiceError("深度策略缺失，追索已停", 409)
    return _row_to_policy(row)


def _assert_sound(policy: dict) -> None:
    expect_hash = policy_hash(policy["version_no"], policy["depth_limit"])
    expect_code = binding_code(policy["version_no"], expect_hash)
    if (
        policy["content_hash"] != expect_hash
        or policy["binding_code"] != expect_code
    ):
        raise ServiceError("深度绑码不一致，追索已停", 409)
    if not (Config.MIN_DEPTH_LIMIT <= policy["depth_limit"] <= Config.MAX_DEPTH_LIMIT):
        raise ServiceError("深度上限越界，追索已停", 409)


def current_policy(conn: sqlite3.Connection) -> dict:
    """在调用方事务内读取当前生效版本并校验自洽；不自洽直接停。"""
    policy = _head(conn)
    _assert_sound(policy)
    return policy


def set_depth_limit(
    depth_limit: int,
    db_path: str | None = None,
    note: str = "",
) -> dict:
    """追加一个新深度版本。校验与写入在同一 IMMEDIATE 事务内完成。"""
    try:
        limit = int(depth_limit)
    except (TypeError, ValueError):
        raise ServiceError("深度上限必须是整数")
    lo, hi = Config.MIN_DEPTH_LIMIT, Config.MAX_DEPTH_LIMIT
    if limit < lo or limit > hi:
        raise ServiceError(f"深度上限须在 {lo}-{hi} 之间")

    with get_db(db_path) as conn:
        head = _head(conn)  # 允许在“头被污染”时人工写一条自洽新版恢复
        try:
            _assert_sound(head)
            sound = True
        except ServiceError:
            sound = False
        if sound and head["depth_limit"] == limit:
            # 未变动：不追加新版本，回当前绑码（幂等）。
            return {**head, "changed": False}
        version = head["version_no"] + 1
        content = policy_hash(version, limit)
        code = binding_code(version, content)
        conn.execute(
            "INSERT INTO trace_depth_policies"
            "(version_no, depth_limit, content_hash, binding_code, note) "
            "VALUES (?,?,?,?,?)",
            (version, limit, content, code, (note or "").strip()[:200]),
        )
        row = conn.execute(
            "SELECT * FROM trace_depth_policies WHERE version_no=?", (version,)
        ).fetchone()
        return {**_row_to_policy(row), "changed": True}


def policy_state(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        return policy_state_in(conn)


def policy_state_in(conn: sqlite3.Connection) -> dict:
    """同一连接（事务）内取当前版本 + 只读历史。"""
    head = current_policy(conn)
    rows = conn.execute(
        "SELECT * FROM trace_depth_policies ORDER BY version_no"
    ).fetchall()
    history = [
        {**_row_to_policy(r), "is_current": r["version_no"] == head["version_no"]}
        for r in rows
    ]
    return {
        "current": head,
        "history": history,
        "min_limit": Config.MIN_DEPTH_LIMIT,
        "max_limit": Config.MAX_DEPTH_LIMIT,
        "credential_mode": "旁路版本表+事务内判定",
    }


def parse_codes(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []
