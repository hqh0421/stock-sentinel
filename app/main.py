from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Dict, Any
import datetime
import logging
import os

from .schemas import ModelConfig
from .config_loader import load_config, save_config, ensure_config_dirs
from .routers.company_router import router as company_router
from .routers.stock_router import router as stock_router
from .routers.agent_router import router as agent_router
from .routers.admin_router import router as admin_router
from .core.auth import SESSION_COOKIE, ensure_initialized, is_admin, verify_session_token
from .core.context import current_user_id, reset_current_user_id, set_current_user_id
from .core.errors import register_exception_handlers
from .core.logging_setup import setup_logging
from .core.middleware import request_context_middleware
from .routers.auth_router import router as auth_router

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
FRONTEND_DIR = BASE_DIR / "frontend"
LOGS_DIR = BASE_DIR / "logs"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动后台监控调度器。

    为什么放在 lifespan 而不是模块级：TestClient / uvicorn --reload 会反复
    导入模块，模块级启动会留下多个僵尸线程。lifespan 与进程生命周期对齐。
    """
    from .services.scheduler import scheduler

    scheduler.start()

    # 启动即后台预热星耀常驻池：星耀慢的只是首次登录（实测 4.8~11s），
    # 热状态批量报价 0.46s，是自选股现价的主源。服务器已下线、本地容器是
    # 该账号唯一消费方，不存在互踢，因此无条件预热（旧版默认关的前提已不成立）。
    import threading

    def _warmup_amazingdata() -> None:
        try:
            from .services.stock_service import stock_service

            stock_service.prewarm_quote_sources()
        except Exception as exc:  # noqa: BLE001
            logger.warning("amazingdata_warmup_task_failed error=%r", exc)

    threading.Thread(target=_warmup_amazingdata, name="ad-warmup", daemon=True).start()
    logger.info("amazingdata_warmup_scheduled")

    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(title="Monitor Stock", lifespan=lifespan)
app.include_router(stock_router)
app.include_router(company_router)
app.include_router(auth_router)
app.include_router(agent_router)
app.include_router(admin_router)
app.middleware("http")(request_context_middleware)


# ---------------------------------------------------------------------------
# 鉴权门禁：未登录访问受保护资源时，HTML 导航重定向到 /login，API 返回 401。
# 公开白名单：登录页本身、鉴权接口、健康检查、登录页静态资源、favicon。
# ---------------------------------------------------------------------------
_PUBLIC_EXACT = {"/login", "/api/health", "/favicon.ico"}
_PUBLIC_PREFIX = {"/api/auth/"}
_PUBLIC_STATIC = {"/static/login.html", "/static/login.css", "/static/login.js"}

# 审计只记「会改状态」的请求；GET 太吵，而且没有改动可追
_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _write_audit(request, user_id: int, status: int) -> None:
    """写一条审计记录。失败不影响响应——审计不能成为新的故障点。

    规则类写接口会把「哪一档从多少改成多少」放在 request.state.audit_detail，
    由这里统一落库；普通接口 detail 为空。明细由路由层生成，中间件不读请求体，
    因此不会碰到 body 流，也不会把口令等敏感字段带进审计表。
    """
    try:
        from .db import store as db_store

        db_store.add_audit_log(
            user_id=int(user_id),
            created_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            method=request.method,
            path=request.url.path,
            status=int(status or 0),
            ip=str(getattr(request.client, "host", "") or ""),
            detail=str(getattr(request.state, "audit_detail", "") or "")[:2000],
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("审计写入失败: %r", exc)


@app.middleware("http")
async def auth_middleware(request, call_next):
    path = request.url.path
    if (
        path in _PUBLIC_EXACT
        or path.startswith("/api/auth/")
        or path in _PUBLIC_STATIC
    ):
        return await call_next(request)

    token = request.cookies.get(SESSION_COOKIE)
    user_id = verify_session_token(token)
    if user_id:
        # 把当前用户写进请求上下文（contextvars），下游 service 直接按用户读写数据，
        # 不必把 user_id 一层层透传。同步端点跑线程池时会复制该 context。
        ctx_token = set_current_user_id(user_id)
        try:
            response = await call_next(request)
        finally:
            reset_current_user_id(ctx_token)
        if request.method in _AUDIT_METHODS and path.startswith("/api/"):
            _write_audit(request, user_id, response.status_code)
        return response

    # 未认证：API 返回 401，浏览器导航返回登录页
    if path.startswith("/api/"):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized", "detail": "请先登录"},
        )
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/login", status_code=307)
register_exception_handlers(app)


# ---------------------------------------------------------------------------
# 安全响应头。本中间件在 auth_middleware 之后注册，Starlette 中后注册的包在外层，
# 因此未登录时的 401/307 响应也会带上这些头。
# ---------------------------------------------------------------------------
@app.middleware("http")
async def security_headers_middleware(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    if request.url.path.startswith("/api/"):
        # 接口响应（含持仓、配置、会话状态）一律不落浏览器缓存
        response.headers.setdefault("Cache-Control", "no-store")
    return response


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


def disable_local_proxy_env() -> None:
    # Force all outbound requests to bypass local/system proxy settings.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)


def ensure_dirs():
    ensure_config_dirs()
    FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    disable_local_proxy_env()
    setup_logging(LOGS_DIR)


ensure_dirs()

# 启动时初始化鉴权（生成/载入口令与会话密钥，未设置 APP_PASSWORD 时随机生成并打印到日志）
ensure_initialized()


@app.get("/")
def index():
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="frontend/index.html not found")
    return FileResponse(str(index_path))


@app.get("/login")
def login_page():
    login_path = FRONTEND_DIR / "login.html"
    if not login_path.exists():
        raise HTTPException(status_code=404, detail="frontend/login.html not found")
    return FileResponse(str(login_path))


@app.get("/api/health")
def health():
    # 公开接口（无鉴权），不返回任何文件系统路径等内部信息
    return {"status": "ok", "service": "monitor_stock"}


def _require_admin(action: str) -> None:
    """实例级共享配置（模型 Key）只有管理员能改，避免普通用户换掉全站模型。"""
    if not is_admin(current_user_id()):
        raise HTTPException(status_code=403, detail=f"仅管理员可{action}")


def _mask_secret(value: Any) -> Any:
    """把密钥类字段打码，只留首尾便于辨识。"""
    text = str(value or "")
    if not text:
        return value
    if len(text) <= 8:
        return "****"
    return f"{text[:6]}…{text[-4:]}"


@app.get("/api/models")
def get_model_config():
    cfg = load_config()
    payload = cfg.model_dump()
    # api_key / vision_api_key 不回传明文；只给打码预览与「是否已配置」标志
    payload["api_key"] = _mask_secret(cfg.api_key)
    payload["api_key_set"] = bool(cfg.api_key)
    if cfg.vision_api_key:
        payload["vision_api_key"] = _mask_secret(cfg.vision_api_key)
    return payload


@app.post("/api/models", response_model=ModelConfig)
def set_model_config(cfg: ModelConfig):
    _require_admin("修改模型配置")
    if not cfg.provider or not cfg.model:
        raise HTTPException(status_code=400, detail="provider and model are required")
    current = load_config()
    # 前端拿到的是打码值：为空或含打码符号时保留原密钥，避免把 Key 覆盖成 "ms-337…7a54"
    if not cfg.api_key or "…" in cfg.api_key or "****" in cfg.api_key:
        cfg.api_key = current.api_key
    if cfg.vision_api_key and ("…" in cfg.vision_api_key or "****" in cfg.vision_api_key):
        cfg.vision_api_key = current.vision_api_key
    save_config(cfg)
    return cfg


@app.get("/api/models/list")
def list_supported_models() -> List[str]:
    cfg = load_config()
    base_models = [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-3.5-turbo",
        "claude-3-opus",
        "claude-3-sonnet",
        "claude-3-haiku",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    ]
    custom = cfg.custom_models or []
    merged = base_models + [m for m in custom if m not in base_models]
    return merged


@app.post("/api/models/custom/add")
def add_custom_model(payload: Dict[str, str]):
    _require_admin("修改模型列表")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    cfg = load_config()
    if name not in cfg.custom_models:
        cfg.custom_models.append(name)
    save_config(cfg)
    return {"ok": True, "models": cfg.custom_models}


@app.post("/api/models/custom/remove")
def remove_custom_model(payload: Dict[str, str]):
    _require_admin("修改模型列表")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    cfg = load_config()
    cfg.custom_models = [m for m in cfg.custom_models if m != name]
    save_config(cfg)
    return {"ok": True, "models": cfg.custom_models}
