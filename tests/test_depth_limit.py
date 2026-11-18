import asyncio
import json
import sqlite3
import threading

import pytest

from app.db import connect, get_db, init_db
from app.services import (
    audit_alignment,
    list_batches,
    policy_state,
    run_gap_report,
    set_depth_limit,
    trace_downstream,
    verify_gap_report,
)
from app.services.errors import ServiceError
from app.services.trace import DepthRejected


def test_default_policy_exists(tmp_path):
    db = str(tmp_path / "p.db")
    init_db(db)
    state = policy_state(db)
    assert state["current"]["depth_limit"] == 2
    assert state["current"]["version_no"] == 1
    assert state["current"]["binding_code"].startswith("DP-v1-")
    assert state["history"][0]["is_current"] is True


def test_deep_chain_rejected_and_evidence_kept(tmp_path):
    db = str(tmp_path / "d.db")
    init_db(db)
    with pytest.raises(DepthRejected) as info:
        trace_downstream("WM-DEEP-131", db)
    detail = info.value.detail
    assert detail["outcome"] == "拒绝"
    assert [b["run_code"] for b in detail["blocked"]] == ["psh-20261110-01"]
    assert detail["max_depth"] == 3
    assert detail["policy"]["binding_code"].startswith("DP-v1-")

    # 拒绝也要在库内留证。
    with get_db(db) as conn:
        run = conn.execute("SELECT * FROM trace_runs ORDER BY id DESC LIMIT 1").fetchone()
        edges = conn.execute(
            "SELECT * FROM trace_edges WHERE trace_run_id=?", (run["id"],)
        ).fetchall()
    assert run["outcome"] == "拒绝"
    assert run["blocked_codes"] == json.dumps(["psh-20261110-01"], ensure_ascii=False)
    assert len(edges) == 3
    assert audit_alignment(db)["ok"] is True


def test_raising_depth_releases_new_version_and_binding(tmp_path):
    db = str(tmp_path / "r.db")
    init_db(db)
    old = policy_state(db)["current"]
    changed = set_depth_limit(3, db, note="放开到三层")
    assert changed["changed"] is True
    assert changed["version_no"] == old["version_no"] + 1
    assert changed["binding_code"] != old["binding_code"]
    assert changed["depth_limit"] == 3

    result = trace_downstream("WM-DEEP-131", db)
    assert result["outcome"] == "放行"
    assert "psh-20261110-01" in {d["run_code"] for d in result["downstream"]}
    assert result["policy"]["binding_code"] == changed["binding_code"]

    # 幂等：同值不追加版本。
    same = set_depth_limit(3, db)
    assert same["changed"] is False
    assert same["version_no"] == changed["version_no"]


def test_policy_history_is_read_only(tmp_path):
    db = str(tmp_path / "ro.db")
    init_db(db)
    set_depth_limit(4, db)
    conn = connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE trace_depth_policies SET depth_limit=8 WHERE version_no=1"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM trace_depth_policies WHERE version_no=1")
    finally:
        conn.close()


def test_batch_list_marks_deep_downstream(tmp_path):
    db = str(tmp_path / "b.db")
    init_db(db)
    rows = {b["run_code"]: b for b in list_batches(db)}
    assert rows["agg-20261110-01"]["scope"] == "源批次"
    assert rows["rep-20261110-01"]["scope"] == "深度内"
    assert rows["exp-20261110-01"]["scope"] == "深度内"
    assert rows["psh-20261110-01"]["scope"] == "超深下游"
    assert rows["psh-20261110-01"]["depth"] == 3


def test_gap_excludes_deep_and_reverify_still_blocks(tmp_path):
    db = str(tmp_path / "g.db")
    init_db(db)
    report = run_gap_report("2026-11-16", db)
    assert report["blocked_codes"] == ["psh-20261110-01"]
    assert report["binding_code"].startswith("DP-v1-")
    assert "超深下游" in report["summary"]

    # 应盖印口径已排除超深下游：总数等于未作废批次减 1。
    with get_db(db) as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM run_batches WHERE biz_date<=? AND status!='已作废'",
            ("2026-11-16",),
        ).fetchone()["c"]
    assert report["expected_count"] == total - 1

    ver = verify_gap_report(report["id"], db)
    assert ver["outcome"] == "仍拒"
    assert "psh-20261110-01" in ver["blocked_codes"]
    assert ver["stale"] == 0

    # 放宽深度后复验：记录过的超深下游被放行 → 判为不一致。
    set_depth_limit(3, db)
    ver2 = verify_gap_report(report["id"], db)
    assert ver2["outcome"] == "不一致"
    assert ver2["stale"] == 1
    assert ver2["leaked_codes"] == ["psh-20261110-01"]


def test_stale_or_tampered_policy_stops(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    conn = connect(db)
    try:
        # 追加一条自相矛盾的版本（哈希与上限不匹配），模拟吃了旧深度。
        conn.execute(
            "INSERT INTO trace_depth_policies"
            "(version_no, depth_limit, content_hash, binding_code, note) "
            "VALUES (2, 8, 'deadbeef', 'DP-v2-deadbeef0000', '伪造')"
        )
    finally:
        conn.close()
    with pytest.raises(ServiceError) as info:
        trace_downstream("WM-ORD-131", db)
    assert "绑码" in info.value.message


def test_stale_page_binding_stops(tmp_path):
    db = str(tmp_path / "sb.db")
    init_db(db)
    old = policy_state(db)["current"]["binding_code"]
    set_depth_limit(4, db, note="换版")
    with pytest.raises(ServiceError) as info:
        trace_downstream("WM-ORD-131", db, expected_binding=old)
    assert "已过期" in info.value.message
    # 旧绑码被拒时不得落任何追索证据（未按任何深度裁定）。
    with get_db(db) as conn:
        runs = conn.execute("SELECT COUNT(*) AS c FROM trace_runs").fetchone()["c"]
    assert runs == 0
    # 用当前绑码则正常裁定。
    cur = policy_state(db)["current"]["binding_code"]
    assert trace_downstream("WM-ORD-131", db, expected_binding=cur)["outcome"] == "放行"


def test_concurrent_depth_change_and_trace(tmp_path):
    db = str(tmp_path / "c.db")
    init_db(db)
    errors: list[BaseException] = []

    def changer(start: int):
        try:
            for i in range(start, start + 6):
                set_depth_limit(1 + (i % 5), db, note=f"并发{i}")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def tracer():
        try:
            for _ in range(12):
                try:
                    trace_downstream("WM-DEEP-131", db)
                except DepthRejected:
                    pass
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=changer, args=(0,)),
        threading.Thread(target=changer, args=(10,)),
        threading.Thread(target=tracer),
        threading.Thread(target=tracer),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors
    with get_db(db) as conn:
        versions = conn.execute(
            "SELECT version_no FROM trace_depth_policies ORDER BY version_no"
        ).fetchall()
        dup_edges = conn.execute(
            "SELECT trace_run_id, upstream_run_code, downstream_run_code, COUNT(*) c "
            "FROM trace_edges GROUP BY 1,2,3 HAVING c > 1"
        ).fetchall()
        runs = conn.execute("SELECT COUNT(*) AS c FROM trace_runs").fetchone()["c"]
    # 版号严格连续、无重复（缺锁会双写同版）。
    assert [r["version_no"] for r in versions] == list(range(1, len(versions) + 1))
    assert not dup_edges
    assert runs > 0
    assert audit_alignment(db)["ok"] is True


@pytest.mark.asyncio
async def test_api_depth_and_trace_contract(authed):
    pol = await authed.get("/api/depth-policy")
    assert pol.status == 200
    body = await pol.json()
    binding = body["current"]["binding_code"]
    assert binding.startswith("DP-v")

    denied = await authed.post("/api/trace", json={"watermark_value": "WM-DEEP-131"})
    assert denied.status == 409
    dbody = await denied.json()
    assert dbody["ok"] is False
    assert dbody["outcome"] == "拒绝"
    assert dbody["policy"]["binding_code"] == binding
    assert dbody["blocked"][0]["run_code"] == "psh-20261110-01"

    runs = await authed.get("/api/trace-runs")
    assert (await runs.json())["items"][0]["outcome"] == "拒绝"

    # 只改卡片展示不改校验会验收失败：这里必须真的换版并放行。
    bump = await authed.post("/api/depth-policy", json={"depth_limit": 3})
    assert bump.status == 200
    new_binding = (await bump.json())["binding_code"]
    assert new_binding != binding

    allowed = await authed.post("/api/trace", json={"watermark_value": "WM-DEEP-131"})
    assert allowed.status == 200
    abody = await allowed.json()
    assert abody["outcome"] == "放行"
    assert abody["policy"]["binding_code"] == new_binding

    bad = await authed.post("/api/depth-policy", json={"depth_limit": 0})
    assert bad.status == 400


@pytest.mark.asyncio
async def test_gap_verify_api(authed):
    rep = await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})
    report = (await rep.json())["report"]
    assert report["binding_code"].startswith("DP-v")
    ver = await authed.post(f"/api/gap-reports/{report['id']}/verify", json={})
    assert ver.status == 200
    v = (await ver.json())["verification"]
    assert v["outcome"] == "仍拒"
    assert "psh-20261110-01" in v["blocked_codes"]


@pytest.mark.asyncio
async def test_console_snapshot_is_one_version(authed):
    """四处同查：卡片、批次标注、缺口、追索证据必须同一绑码/版号。"""
    first = await (await authed.get("/api/console")).json()
    old_binding = first["binding_code"]
    assert first["policy"]["current"]["binding_code"] == old_binding
    assert all(
        b["scope"] != "超深下游" for b in first["batches"] if b["depth"] is not None
        and b["depth"] <= first["policy"]["current"]["depth_limit"]
    )

    await authed.post("/api/depth-policy", json={"depth_limit": 1})
    await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})
    await authed.post("/api/trace", json={"watermark_value": "WM-DEEP-131"})

    snap = await (await authed.get("/api/console")).json()
    new_binding = snap["binding_code"]
    assert new_binding != old_binding
    limit = snap["policy"]["current"]["depth_limit"]
    # 列表按同一新上限标注。
    psh = next(b for b in snap["batches"] if b["run_code"] == "psh-20261110-01")
    assert psh["depth"] > limit and psh["scope"] == "超深下游"
    # 缺口台与追索证据也落在同一绑码上。
    assert snap["gap_reports"][0]["binding_code"] == new_binding
    assert snap["trace_runs"][0]["binding_code"] == new_binding
    assert snap["trace_runs"][0]["outcome"] == "拒绝"


@pytest.mark.asyncio
async def test_api_stale_binding_rejected(authed):
    pol = await (await authed.get("/api/depth-policy")).json()
    old = pol["current"]["binding_code"]
    await authed.post("/api/depth-policy", json={"depth_limit": 4})
    stale = await authed.post(
        "/api/trace", json={"watermark_value": "WM-ORD-131", "binding_code": old}
    )
    assert stale.status == 409
    body = await stale.json()
    assert "已过期" in body["error"]
    fresh = await authed.post(
        "/api/trace", json={"watermark_value": "WM-ORD-131", "binding_code": None}
    )
    assert fresh.status == 200


@pytest.mark.asyncio
async def test_growing_chain_blocks_later_depth(authed):
    """补一条边就把原“末端”变成中间节点：新下游按新深度被拒。"""
    # 让 psh 之下再挂一跳，默认 2 层下明显超深。
    stamp = await authed.post("/api/batches/3/stamp", json={"watermark_value": "WM-GMV-X"})
    assert stamp.status == 200
    r = await authed.post(
        "/api/edges",
        json={
            "upstream_run_code": "psh-20261110-01",
            "downstream_run_code": "gmv-20261114-01",
        },
    )
    assert r.status == 200
    denied = await authed.post("/api/trace", json={"watermark_value": "WM-DEEP-131"})
    assert denied.status == 409
    dbody = await denied.json()
    assert {b["run_code"] for b in dbody["blocked"]} >= {"psh-20261110-01"}
    assert dbody["max_depth"] >= 4


@pytest.mark.asyncio
async def test_edge_validation(authed):
    bad = await authed.post(
        "/api/edges",
        json={"upstream_run_code": "nope", "downstream_run_code": "gmv-20261114-01"},
    )
    assert bad.status == 404
    same = await authed.post(
        "/api/edges",
        json={"upstream_run_code": "agg-20261110-01", "downstream_run_code": "agg-20261110-01"},
    )
    assert same.status == 400
    empty = await authed.post("/api/edges", json={})
    assert empty.status == 400
