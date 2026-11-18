"""缺口对账：应盖印集合只算“深度内”下游，超深下游不进缺口，避免对账膨胀。

复验（reverify）在**单个 IMMEDIATE 事务**内重新读取当前深度版本，
逐条重跑深链裁定；超深下游必须仍被拒，若发现当前上限反而会放行，
标记 stale 并说明不一致。
"""

import json

from app.db import get_db
from app.services.depth_policy import current_policy, parse_codes
from app.services.errors import ServiceError
from app.services.lineage import build_depth_map, split_by_depth, stamped_roots


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM gap_reports ORDER BY id DESC"
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["blocked_codes"] = parse_codes(item.get("blocked_codes", "[]"))
            out.append(item)
        return out


def run_gap_report(as_of_date: str, db_path: str | None = None) -> dict:
    day = (as_of_date or "").strip() or "2026-11-16"
    with get_db(db_path) as conn:
        policy = current_policy(conn)
        limit = policy["depth_limit"]
        depth, _ = build_depth_map(
            conn, stamped_roots(conn), hard_cap=100
        )
        batches = conn.execute(
            "SELECT * FROM run_batches WHERE biz_date<=? AND status!='已作废'",
            (day,),
        ).fetchall()

        expected = 0
        stamped = 0
        blocked: list[str] = []
        for row in batches:
            d = depth.get(row["run_code"])
            if d is not None and d > limit:
                blocked.append(row["run_code"])
                continue  # 超深下游不纳入缺口口径
            expected += 1
            if row["status"] == "已盖印":
                stamped += 1
        gap = expected - stamped
        if blocked:
            summary = (
                f"截至 {day}：应盖印 {expected}，已盖印 {stamped}，缺口 {gap}；"
                f"超深下游 {len(blocked)} 个已按深度上限 v{policy['version_no']} 排除"
            )
        else:
            summary = f"截至 {day}：应盖印 {expected}，已盖印 {stamped}，缺口 {gap}"
        cur = conn.execute(
            "INSERT INTO gap_reports"
            "(as_of_date, expected_count, stamped_count, gap_count, summary, "
            " policy_version, binding_code, depth_limit, blocked_count, blocked_codes, "
            " outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                day,
                expected,
                stamped,
                gap,
                summary,
                policy["version_no"],
                policy["binding_code"],
                limit,
                len(blocked),
                json.dumps(sorted(blocked), ensure_ascii=False),
                "拦截超深" if blocked else "放行",
            ),
        )
        row = conn.execute(
            "SELECT * FROM gap_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        item = dict(row)
        item["blocked_codes"] = parse_codes(item["blocked_codes"])
        item["policy"] = policy
        return item


def verify_gap_report(report_id: int, db_path: str | None = None) -> dict:
    """缺口复验：超深下游必须仍被拒；绑码须与当前版本一致。"""
    with get_db(db_path) as conn:
        report = conn.execute(
            "SELECT * FROM gap_reports WHERE id=?", (report_id,)
        ).fetchone()
        if not report:
            raise ServiceError("对账记录不存在", 404)
        policy = current_policy(conn)
        limit = policy["depth_limit"]
        recorded_blocked = set(parse_codes(report["blocked_codes"]))
        depth, _ = build_depth_map(
            conn, stamped_roots(conn), hard_cap=100
        )
        _, still_blocked, _ = split_by_depth(conn, depth, limit)
        still_codes = {b["run_code"] for b in still_blocked}
        leaked = sorted(recorded_blocked - still_codes)

        stale = policy["binding_code"] != report["binding_code"]
        if leaked:
            outcome = "不一致"
            message = (
                "复验失败：当前深度上限会放行此前被拒的超深下游 "
                + "、".join(leaked)
            )
        elif stale:
            outcome = "需复验"
            message = (
                f"深度绑码已变（记录 {report['binding_code']} → 当前 "
                f"{policy['binding_code']}），超深下游仍被拒 {len(still_codes)} 个"
            )
        elif still_codes:
            outcome = "仍拒"
            message = f"复验通过：超深下游仍被拒 {len(still_codes)} 个"
        else:
            outcome = "放行"
            message = "复验通过：该口径下无超深下游"

        cur = conn.execute(
            "INSERT INTO gap_verifications"
            "(gap_report_id, policy_version, binding_code, depth_limit, "
            " blocked_count, blocked_codes, outcome, stale, message) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                report_id,
                policy["version_no"],
                policy["binding_code"],
                limit,
                len(still_codes),
                json.dumps(sorted(still_codes), ensure_ascii=False),
                outcome,
                1 if stale else 0,
                message,
            ),
        )
        row = conn.execute(
            "SELECT * FROM gap_verifications WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        item = dict(row)
        item["blocked_codes"] = parse_codes(item["blocked_codes"])
        item["leaked_codes"] = leaked
        item["report"] = dict(report)
        return item


def list_verifications(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM gap_verifications ORDER BY id DESC LIMIT 50"
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["blocked_codes"] = parse_codes(item["blocked_codes"])
            out.append(item)
        return out
