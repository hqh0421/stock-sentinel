"""登录 / 登出 / 注册 / 改密 / 会话状态（多用户）。"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ..core import auth
from ..db import store

router = APIRouter(prefix="/api/auth")
logger = logging.getLogger("app.auth_router")

FAIL_HINT = "用户名或口令错误"


class LoginRequest(BaseModel):
    username: str = ""
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""


class UserActiveRequest(BaseModel):
    active: bool


def _cookie_secure() -> bool:
    return os.environ.get("SESSION_SECURE", "0").strip().lower() in ("1", "true", "yes", "on")


def _set_session_cookie(response: Response, user_id: int, request: Request) -> None:
    token = auth.create_session_token(user_id, user_agent=request.headers.get("user-agent", ""))
    response.set_cookie(
        key=auth.SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=auth.SESSION_TTL,
        path="/",
        secure=_cookie_secure(),
    )


def _resolve_username(username: str) -> str:
    """未填用户名时落到首个用户：兼容旧版只有口令的登录页。"""
    name = str(username or "").strip()
    if name:
        return name
    uid = store.first_user_id()
    if uid is None:
        return ""
    user = store.get_user_by_id(uid)
    return str((user or {}).get("username") or "")


def _current_user_id(request: Request) -> int | None:
    return auth.verify_session_token(request.cookies.get(auth.SESSION_COOKIE))


@router.post("/login")
async def login(req: LoginRequest, request: Request, response: Response):
    username = _resolve_username(req.username)
    if not username:
        raise HTTPException(status_code=401, detail=FAIL_HINT)

    # 1) 惩罚窗口内直接拒绝：不做口令校验，避免被用来消耗 CPU/线程池
    wait = auth.login_block_remaining(username)
    if wait > 0:
        retry = int(wait) + 1
        logger.warning("登录处于退避窗口 user=%s 剩余 %ss", username, retry)
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁，请 {retry} 秒后再试",
            headers={"Retry-After": str(retry)},
        )

    # 2) 口令校验：PBKDF2 是 CPU 密集操作，放线程池，别堵住事件循环
    user = await asyncio.to_thread(auth.authenticate, username, req.password or "")
    if not user:
        delay = auth.register_login_failure(username)
        if delay > 0:
            retry = int(delay) + 1
            raise HTTPException(
                status_code=429,
                detail=f"口令错误次数过多，请 {retry} 秒后再试",
                headers={"Retry-After": str(retry)},
            )
        await asyncio.sleep(0.25)
        raise HTTPException(status_code=401, detail=FAIL_HINT)

    auth.reset_login_failures(username)
    _set_session_cookie(response, int(user["id"]), request)
    return {"ok": True, "user": auth.get_user_public(user)}


@router.post("/register")
async def register(req: RegisterRequest, request: Request, response: Response):
    # 没有用户时允许自举；已有用户时必须显式开启 APP_ALLOW_REGISTER=1
    if store.count_users() > 0 and not auth.allow_register():
        raise HTTPException(status_code=403, detail="本站未开放注册，请联系管理员开通账号")
    err = auth.validate_username(req.username) or auth.validate_password(req.password)
    if err:
        raise HTTPException(status_code=400, detail=err)
    try:
        user = await asyncio.to_thread(
            auth.create_user, req.username, req.password, req.display_name or ""
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _set_session_cookie(response, int(user["id"]), request)
    return {"ok": True, "user": auth.get_user_public(user)}


@router.post("/logout")
def logout(request: Request, response: Response):
    auth.revoke_session_token(request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    user_id = _current_user_id(request)
    if not user_id:
        return {"authenticated": False, "user": None}
    user = store.get_user_by_id(user_id)
    if not user:
        return {"authenticated": False, "user": None}
    return {"authenticated": True, "user": auth.get_user_public(user)}


@router.post("/change_password")
async def change_password(req: ChangePasswordRequest, request: Request):
    user_id = _current_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        token = request.cookies.get(auth.SESSION_COOKIE) or ""
        keep_sid = token.split(".")[0] if token else None
        await asyncio.to_thread(
            auth.change_password,
            user_id,
            req.old_password or "",
            req.new_password or "",
            keep_sid,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "message": "口令已修改，其它设备需重新登录"}


@router.get("/status")
def status():
    """登录页用：是否已存在用户、是否允许注册。不泄露任何用户信息。"""
    return {
        "has_users": store.count_users() > 0,
        "allow_register": auth.allow_register() or store.count_users() == 0,
    }


def _require_admin(request: Request) -> int:
    user_id = _current_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="请先登录")
    if not auth.is_admin(user_id):
        raise HTTPException(status_code=403, detail="仅管理员可管理账号")
    return user_id


@router.get("/users")
def list_users(request: Request):
    _require_admin(request)
    return {"data": store.list_users()}


@router.post("/users")
async def admin_create_user(req: CreateUserRequest, request: Request):
    _require_admin(request)
    err = auth.validate_username(req.username) or auth.validate_password(req.password)
    if err:
        raise HTTPException(status_code=400, detail=err)
    try:
        user = await asyncio.to_thread(
            auth.create_user, req.username, req.password, req.display_name or ""
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "user": auth.get_user_public(user)}


@router.post("/users/{user_id}/active")
def set_user_active(user_id: int, req: UserActiveRequest, request: Request):
    operator = _require_admin(request)
    if int(user_id) == int(operator) and not req.active:
        raise HTTPException(status_code=400, detail="不能停用当前登录的管理员账号")
    target = store.get_user_by_id(int(user_id))
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    store.set_user_active(int(user_id), bool(req.active))
    if not req.active:
        # 停用后立刻踢掉该用户所有会话
        auth.revoke_user_sessions(int(user_id))
    logger.info("user_active_changed operator=%s target=%s active=%s", operator, user_id, req.active)
    return {"ok": True}
