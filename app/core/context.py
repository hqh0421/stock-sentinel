"""请求级「当前用户」上下文。

为什么用 contextvars
--------------------
FastAPI 的同步端点跑在线程池里。中间件在 ``call_next`` 之前 ``set``，
Starlette 调度线程池时会复制当前 context，端点里就能读到同一个 user_id，
不需要把 user_id 一路当参数透传到 service 的每个方法（那会动几十个签名）。

后台线程注意
------------
``ThreadPoolExecutor.submit`` **不会**自动复制 context。凡是线程池里会读
用户态数据的地方，必须显式 ``copy_context().run``（见 monitor_service）。
无请求上下文时（启动阶段、CLI、单元测试）回退到首个用户，保持单机老行为。
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Optional

_current_user_id: ContextVar[Optional[int]] = ContextVar("current_user_id", default=None)


def set_current_user_id(user_id: Optional[int]) -> Token:
    return _current_user_id.set(user_id)


def reset_current_user_id(token: Token) -> None:
    try:
        _current_user_id.reset(token)
    except (ValueError, RuntimeError):
        # 跨 context 重置会抛错；忽略即可，下一次请求会重新 set
        pass


def current_user_id() -> Optional[int]:
    return _current_user_id.get()


def default_user_id() -> Optional[int]:
    from ..db import store

    return store.first_user_id()


def resolve_user_id() -> int:
    """取当前用户；无上下文时回退首个用户。取不到抛异常（数据库尚未初始化）。"""
    uid = current_user_id()
    if uid is not None:
        return int(uid)
    uid = default_user_id()
    if uid is None:
        raise RuntimeError("数据库中还没有任何用户，请先完成初始化")
    return int(uid)


def resolve_user_id_optional() -> Optional[int]:
    """取当前用户或首个用户，取不到返回 None（供启动阶段的安全读取使用）。"""
    uid = current_user_id()
    if uid is not None:
        return int(uid)
    return default_user_id()
