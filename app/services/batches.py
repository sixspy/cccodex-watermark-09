from app.db import get_db
from app.services.depth_policy import current_policy
from app.services.errors import ServiceError
from app.services.lineage import (
    batches_by_codes,
    build_depth_map,
    stamped_roots,
)


def _annotate(conn, rows: list[dict], limit: int, depth: dict[str, int] | None = None) -> list[dict]:
    """给批次打上血缘深度与放行范围，列表/详情与追索同一口径。"""
    if depth is None:
        depth, _ = build_depth_map(conn, stamped_roots(conn))
    out = []
    for row in rows:
        item = dict(row)
        d = depth.get(item["run_code"])
        item["depth"] = d
        if item["status"] == "已盖印":
            item["scope"] = "源批次"
        elif d is None:
            item["scope"] = "未入链"
        elif d <= limit:
            item["scope"] = "深度内"
        else:
            item["scope"] = "超深下游"
        out.append(item)
    return out


def list_batches(db_path: str | None = None, status: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        limit = current_policy(conn)["depth_limit"]
        if status:
            rows = conn.execute(
                "SELECT * FROM run_batches WHERE status=? ORDER BY id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM run_batches ORDER BY id").fetchall()
        return _annotate(conn, [dict(r) for r in rows], limit)


def get_batch(batch_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        policy = current_policy(conn)
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not row:
            return None
        item = _annotate(conn, [dict(row)], policy["depth_limit"])[0]
        item["policy"] = policy
        return item


def stamp_batch(
    batch_id: int,
    watermark_value: str,
    db_path: str | None = None,
) -> dict:
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] == "已盖印":
            raise ServiceError("批次已盖印", 409)
        if batch["status"] != "待盖印":
            raise ServiceError("仅待盖印批次可盖印", 409)
        tpl = conn.execute(
            "SELECT * FROM watermark_templates WHERE table_name=? AND status='现行'",
            (batch["table_name"],),
        ).fetchone()
        if not tpl:
            raise ServiceError("无现行水印模板", 409)
        conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value=?, "
            "stamped_at=datetime('now') WHERE id=? AND status='待盖印'",
            (value, batch_id),
        )
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        return dict(row)
