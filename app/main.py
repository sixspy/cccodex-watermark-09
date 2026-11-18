from pathlib import Path

from blacksheep import Application, Content, Cookie, Request, Response
from blacksheep.server.responses import redirect
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import init_db
from app.services import (
    add_edge,
    audit_alignment,
    console_snapshot,
    get_batch,
    list_batches,
    list_edges,
    list_reports,
    list_templates,
    list_verifications,
    policy_state,
    run_gap_report,
    set_depth_limit,
    stamp_batch,
    trace_downstream,
    trace_evidence,
    verify_gap_report,
)
from app.services.errors import ServiceError
from app.services.trace import DepthRejected

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-watermark-session")

app = Application()
app.serve_files(str(STATIC), root_path="/static")


@app.exception_handler(ServiceError)
async def _service_error(_app, _request: Request, exc: ServiceError) -> Response:
    return json_response({"ok": False, "error": exc.message}, exc.status_code)


@app.exception_handler(Exception)
async def _unhandled(_app, _request: Request, _exc: Exception) -> Response:
    """兜底：内部异常不回堆栈/细节，只回短句。"""
    return json_response({"ok": False, "error": "服务内部错误"}, 500)


@app.on_start
async def _on_start(_app) -> None:
    init_db()


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("watermark_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401)
    return user


def json_response(data: dict, status: int = 200) -> Response:
    import json

    return Response(
        status,
        content=Content(
            b"application/json",
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
        ),
    )


def html_response(path: Path) -> Response:
    return Response(
        200,
        content=Content(b"text/html; charset=utf-8", path.read_bytes()),
    )


async def read_json(request: Request) -> dict:
    """空体/坏 JSON 一律当空字典，避免 500 泄露内部细节。"""
    import json

    try:
        data = await request.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def field_text(body: dict, key: str, default: str = "") -> str:
    """JSON null 不能被 str() 变成字面量 "None"。"""
    value = body.get(key)
    return default if value is None else str(value)


@app.router.get("/")
async def root(request: Request):
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return redirect("/app/")
    return redirect("/login")


@app.router.get("/login")
async def login_page():
    return html_response(STATIC / "login.html")


@app.router.get("/app/")
async def spa_page(request: Request):
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return redirect("/login")
    return html_response(STATIC / "index.html")


@app.router.post("/api/login")
async def api_login(request: Request):
    body = await read_json(request)
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json_response({"ok": False, "error": "账号或密码错误"}, 400)
    resp = json_response({"ok": True, "user": user})
    resp.set_cookie(
        Cookie(
            "watermark_session",
            serializer.dumps({"user": user}),
            http_only=True,
            path="/",
        )
    )
    return resp


@app.router.post("/api/logout")
async def api_logout():
    resp = json_response({"ok": True})
    resp.unset_cookie("watermark_session")
    return resp


@app.router.get("/api/meta")
async def api_meta(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response(
        {
            "lab_name": Config.LAB_NAME,
            "product_line": "cc-watermark-lab",
            "chain": ["水印模板", "批次盖印", "下游追索", "缺口对账"],
        }
    )


@app.router.get("/api/templates")
async def api_templates(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_templates(status=status_val)})


@app.router.get("/api/batches")
async def api_batches(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_batches(status=status_val)})


@app.router.get("/api/batches/{batch_id}")
async def api_batch_detail(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    batch_id = int(request.route_values["batch_id"])
    batch = get_batch(batch_id)
    if not batch:
        return json_response({"error": "批次不存在"}, 404)
    return json_response({"batch": batch})


@app.router.post("/api/batches/{batch_id}/stamp")
async def api_stamp(request: Request):
    try:
        require_user(request)
        batch_id = int(request.route_values["batch_id"])
        body = await read_json(request)
        batch = stamp_batch(batch_id, field_text(body, "watermark_value"))
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, "batch": batch})


@app.router.post("/api/trace")
async def api_trace(request: Request):
    try:
        require_user(request)
        body = await read_json(request)
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    try:
        result = trace_downstream(
            field_text(body, "watermark_value"),
            expected_binding=body.get("binding_code"),
        )
    except DepthRejected as exc:
        # 证据已在事务内提交；对外只回短句 + 绑码/裁定，便于页面亮码。
        detail = exc.detail
        return json_response(
            {
                "ok": False,
                "error": exc.message,
                "outcome": detail["outcome"],
                "depth_limit": detail["depth_limit"],
                "max_depth": detail["max_depth"],
                "blocked": detail["blocked"],
                "policy": detail["policy"],
                "trace_run_id": detail["trace_run_id"],
            },
            exc.status_code,
        )
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, **result})


@app.router.get("/api/depth-policy")
async def api_depth_policy(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response(policy_state())


@app.router.get("/api/console")
async def api_console(request: Request):
    """四处同查：深度卡片 / 追索详情 / 批次列表 / 缺口台 同一事务快照。"""
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response(console_snapshot())


@app.router.post("/api/depth-policy")
async def api_depth_set(request: Request):
    try:
        require_user(request)
        body = await read_json(request)
        result = set_depth_limit(
            body.get("depth_limit"), note=field_text(body, "note")
        )
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    except (ValueError, TypeError, AttributeError):
        return json_response({"ok": False, "error": "深度配置无效"}, 400)
    return json_response({"ok": True, **result, "state": policy_state()})


@app.router.get("/api/trace-runs")
async def api_trace_runs(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": trace_evidence()})


@app.router.get("/api/trace-audit")
async def api_trace_audit(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response(audit_alignment())


@app.router.get("/api/edges")
async def api_edge_list(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_edges()})


@app.router.post("/api/edges")
async def api_edge_add(request: Request):
    try:
        require_user(request)
        body = await read_json(request)
        result = add_edge(
            field_text(body, "upstream_run_code"),
            field_text(body, "downstream_run_code"),
        )
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, **result})


@app.router.get("/api/gap-reports")
async def api_gap_list(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response(
        {"items": list_reports(), "verifications": list_verifications()}
    )


@app.router.post("/api/gap-reports")
async def api_gap_run(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    body = await read_json(request)
    try:
        report = run_gap_report(field_text(body, "as_of_date", "2026-11-16"))
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, "report": report})


@app.router.post("/api/gap-reports/{report_id}/verify")
async def api_gap_verify(request: Request):
    try:
        require_user(request)
        report_id = int(request.route_values["report_id"])
        result = verify_gap_report(report_id)
    except (ServiceError, ValueError) as exc:
        status = getattr(exc, "status_code", 400)
        message = getattr(exc, "message", "对账编号无效")
        return json_response({"ok": False, "error": message}, status)
    return json_response({"ok": True, "verification": result})
