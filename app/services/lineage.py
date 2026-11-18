"""血缘多跳展开与深度计算。

追索不设深度时，深链会把与业务无关的末端批次也拖进来；这里的 BFS
统一按“源批次 = 深度 0，直接下游 = 深度 1”计算每个节点的最小深度，
供追索裁定、批次列表范围标注与缺口对账共用同一口径。
"""

from collections import deque
import sqlite3

ADJACENCY_SQL = "SELECT upstream_run_code, downstream_run_code FROM lineage_edges"


def stamped_roots(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT run_code FROM run_batches "
        "WHERE status='已盖印' AND watermark_value IS NOT NULL AND watermark_value<>'' "
        "ORDER BY id"
    ).fetchall()
    return [r["run_code"] for r in rows]


def build_depth_map(
    conn: sqlite3.Connection,
    roots: list[str],
    hard_cap: int = 100,
) -> tuple[dict[str, int], list[dict]]:
    """返回 (run_code→最小深度, 走过的边)。环与超长链由 hard_cap 截断。"""
    adjacency: dict[str, list[str]] = {}
    for row in conn.execute(ADJACENCY_SQL).fetchall():
        adjacency.setdefault(row["upstream_run_code"], []).append(
            row["downstream_run_code"]
        )

    depth: dict[str, int] = {}
    traversed: list[dict] = []
    seen_edges: set[tuple[str, str]] = set()
    queue: deque[str] = deque()
    for root in roots:
        if root not in depth:
            depth[root] = 0
            queue.append(root)

    hops = 0
    while queue and hops < hard_cap:
        node = queue.popleft()
        hops += 1
        for child in adjacency.get(node, []):
            key = (node, child)
            if key not in seen_edges:
                seen_edges.add(key)
                traversed.append(
                    {"upstream_run_code": node, "downstream_run_code": child}
                )
            candidate = depth[node] + 1
            if child not in depth or candidate < depth[child]:
                depth[child] = candidate
                if candidate <= hard_cap:
                    queue.append(child)
    return depth, traversed


def batches_by_codes(
    conn: sqlite3.Connection, codes: list[str]
) -> dict[str, dict]:
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT * FROM run_batches WHERE run_code IN ({placeholders}) ORDER BY id",
        codes,
    ).fetchall()
    return {r["run_code"]: dict(r) for r in rows}


def split_by_depth(
    conn: sqlite3.Connection,
    depth: dict[str, int],
    depth_limit: int,
    exclude: set[str] | None = None,
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """把已达到的节点按深度上限切成（放行下游, 超深下游, 节点深度）。"""
    exclude = exclude or set()
    codes = [
        code
        for code in depth
        if code not in exclude
    ]
    rows = batches_by_codes(conn, codes)
    allowed: list[dict] = []
    blocked: list[dict] = []
    for code in sorted(rows):
        item = rows[code]
        item["depth"] = depth[code]
        if depth[code] <= depth_limit:
            allowed.append(item)
        else:
            blocked.append(item)
    allowed.sort(key=lambda b: (b["depth"], b["id"]))
    blocked.sort(key=lambda b: (b["depth"], b["id"]))
    # 只回报确实存在批次的节点深度，避免悬空边把“实际最深”抬高。
    node_depths = {code: depth[code] for code in rows}
    return allowed, blocked, node_depths
