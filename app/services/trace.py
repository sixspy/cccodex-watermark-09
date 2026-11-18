"""下游追索：在单个 IMMEDIATE 事务内读深度版本 → 裁定 → 落证。

裁定结果与绑码一起写入 trace_runs / trace_edges；接口失败时只回短句
（不泄露内部细节），但拒绝证据已经提交。超深下游一律拒绝。

“吃旧深度即停”：读到版本行后先校验绑码与内容哈希、版号、上限三者自洽，
不自洽说明拿到了陈旧/被篡改的深度，直接停，不会继续按旧深度放行。
"""

import json

from app.config import Config
from app.db import get_db
from app.policy import binding_code, policy_hash
from app.services.depth_policy import current_policy
from app.services.errors import ServiceError
from app.services.lineage import build_depth_map, split_by_depth


def _assert_policy_sound(policy: dict) -> None:
    """二次防线：即便 current_policy 被绕过，也在裁定时校验绑码自洽。"""
    expect_hash = policy_hash(policy["version_no"], policy["depth_limit"])
    expect_code = binding_code(policy["version_no"], expect_hash)
    if (
        policy["content_hash"] != expect_hash
        or policy["binding_code"] != expect_code
        or policy["depth_limit"] < Config.MIN_DEPTH_LIMIT
        or policy["depth_limit"] > Config.MAX_DEPTH_LIMIT
    ):
        raise ServiceError("深度绑码不一致，追索已停", 409)


def trace_downstream(
    watermark_value: str,
    db_path: str | None = None,
    expected_binding: str | None = None,
) -> dict:
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")

    rejected: dict | None = None
    result: dict | None = None

    with get_db(db_path) as conn:
        policy = current_policy(conn)
        _assert_policy_sound(policy)
        # 页面亮出的绑码与库内当前版本必须对齐；吃旧深度即停。
        want = (expected_binding or "").strip()
        if want and want != policy["binding_code"]:
            raise ServiceError(
                f"深度绑码已过期（页面 {want} → 当前 {policy['binding_code']}），"
                "追索已停，请刷新后重试",
                409,
            )
        limit = policy["depth_limit"]
        sources = conn.execute(
            "SELECT * FROM run_batches WHERE watermark_value=? ORDER BY id",
            (value,),
        ).fetchall()
        if not sources:
            raise ServiceError("未找到该水印值的盖印批次", 404)
        root_codes = [s["run_code"] for s in sources]
        depth, edges = build_depth_map(
            conn, root_codes, hard_cap=Config.DEPTH_HARD_CAP
        )
        allowed, blocked, node_depths = split_by_depth(
            conn, depth, limit, exclude=set(root_codes)
        )
        max_depth = max(node_depths.values()) if node_depths else 0
        outcome = "拒绝" if blocked else "放行"

        cur = conn.execute(
            "INSERT INTO trace_runs"
            "(watermark_value, policy_version, binding_code, depth_limit, outcome, "
            " max_depth, blocked_count, source_codes, downstream_codes, blocked_codes, "
            " node_depths) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                value,
                policy["version_no"],
                policy["binding_code"],
                limit,
                outcome,
                max_depth,
                len(blocked),
                json.dumps(root_codes, ensure_ascii=False),
                json.dumps([b["run_code"] for b in allowed], ensure_ascii=False),
                json.dumps([b["run_code"] for b in blocked], ensure_ascii=False),
                json.dumps(node_depths, ensure_ascii=False, sort_keys=True),
            ),
        )
        run_id = cur.lastrowid
        for edge in edges:
            conn.execute(
                "INSERT OR IGNORE INTO trace_edges"
                "(trace_run_id, upstream_run_code, downstream_run_code, depth) "
                "VALUES (?,?,?,?)",
                (
                    run_id,
                    edge["upstream_run_code"],
                    edge["downstream_run_code"],
                    node_depths.get(edge["downstream_run_code"], 0),
                ),
            )

        if blocked:
            rejected = {
                "outcome": outcome,
                "policy": policy,
                "depth_limit": limit,
                "blocked": blocked,
                "max_depth": max_depth,
                "trace_run_id": run_id,
            }
        else:
            result = {
                "watermark_value": value,
                "sources": [dict(s) for s in sources],
                "edges": edges,
                "downstream": allowed,
                "depth_limit": limit,
                "max_depth": max_depth,
                "outcome": outcome,
                "blocked": [],
                "policy": policy,
                "trace_run_id": run_id,
            }

    # 事务已提交（拒绝证据保留），此处才对外回短句。
    if rejected is not None:
        raise DepthRejected("追索被拒：存在超深下游，已按深度上限拦截", rejected)
    assert result is not None
    return result


class DepthRejected(ServiceError):
    def __init__(self, message: str, detail: dict):
        super().__init__(message, 409)
        self.detail = detail


def trace_evidence(db_path: str | None = None, limit: int = 50) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM trace_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            for key in ("source_codes", "downstream_codes", "blocked_codes"):
                try:
                    item[key] = json.loads(item[key])
                except (TypeError, ValueError):
                    item[key] = []
            try:
                item["node_depths"] = json.loads(item["node_depths"])
            except (TypeError, ValueError):
                item["node_depths"] = {}
            out.append(item)
        return out


def audit_alignment(db_path: str | None = None) -> dict:
    """核验每条追索证据的绑码/上限/裁定是否与版本表对齐。"""
    with get_db(db_path) as conn:
        policies = {
            r["version_no"]: dict(r)
            for r in conn.execute("SELECT * FROM trace_depth_policies").fetchall()
        }
        rows = conn.execute("SELECT * FROM trace_runs ORDER BY id").fetchall()
        issues: list[dict] = []
        for r in rows:
            p = policies.get(r["policy_version"])
            if p is None:
                issues.append({"trace_run_id": r["id"], "issue": "绑定版本不存在"})
                continue
            if p["binding_code"] != r["binding_code"]:
                issues.append({"trace_run_id": r["id"], "issue": "绑码与版本表不一致"})
            if p["depth_limit"] != r["depth_limit"]:
                issues.append({"trace_run_id": r["id"], "issue": "深度上限与版本表不一致"})
            blocked = json.loads(r["blocked_codes"] or "[]")
            depths = json.loads(r["node_depths"] or "{}")
            over = [
                code for code in depths if depths[code] > r["depth_limit"]
            ]
            if r["outcome"] == "放行" and (blocked or over):
                issues.append({"trace_run_id": r["id"], "issue": "放行却存在超深下游"})
            if r["outcome"] == "拒绝" and not blocked:
                issues.append({"trace_run_id": r["id"], "issue": "拒绝却无超深下游"})
        return {"checked": len(rows), "issues": issues, "ok": not issues}
