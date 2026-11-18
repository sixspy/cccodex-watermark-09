"""血缘边维护：追索深度只有在多跳链上才有意义，这里提供受控的补边入口。

补边与深度改配、追索共用同一个 IMMEDIATE 写锁：同一对上游/下游不会因为
并发请求被写成两条边（表上另有 UNIQUE 兜底）。
"""

from app.db import get_db
from app.services.errors import ServiceError


def add_edge(
    upstream_run_code: str,
    downstream_run_code: str,
    db_path: str | None = None,
) -> dict:
    up = (upstream_run_code or "").strip()
    down = (downstream_run_code or "").strip()
    if not up or not down:
        raise ServiceError("上游/下游运行码不能为空")
    if up == down:
        raise ServiceError("上下游不能相同")
    with get_db(db_path) as conn:
        for code in (up, down):
            row = conn.execute(
                "SELECT 1 FROM run_batches WHERE run_code=?", (code,)
            ).fetchone()
            if not row:
                raise ServiceError(f"批次不存在：{code}", 404)
        cur = conn.execute(
            "INSERT OR IGNORE INTO lineage_edges"
            "(upstream_run_code, downstream_run_code) VALUES (?,?)",
            (up, down),
        )
        row = conn.execute(
            "SELECT * FROM lineage_edges ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "edge": dict(row) if row else {
                "upstream_run_code": up,
                "downstream_run_code": down,
            },
            "created": cur.rowcount > 0,
        }


def list_edges(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT * FROM lineage_edges ORDER BY id").fetchall()
        return [dict(r) for r in rows]
