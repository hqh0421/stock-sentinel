"""运维/可观测相关接口：调度器状态、备份、告警历史、审计日志。

权限约定
--------
- 调度器状态、告警历史、审计日志：**按用户隔离**，各看各的；
- 数据库备份/恢复、审计全量：**仅管理员**（备份里含所有用户数据）；
- 所有写操作失败都不静默，错误信息如实返回，避免"点了没反应"。
"""
from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse

from ..core.auth import is_admin
from ..core.context import current_user_id, resolve_user_id
from ..db import store
from ..services import backup_service
from ..services.scheduler import scheduler

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["ops"])


def _require_admin(action: str) -> int:
    uid = current_user_id()
    if not is_admin(uid):
        raise HTTPException(status_code=403, detail=f"仅管理员可{action}")
    return int(uid or 0)


# ------------------------------------------------------------------ 调度器

@router.get("/scheduler/status")
def scheduler_status() -> Dict[str, Any]:
    return scheduler.status()


@router.post("/scheduler/run")
def scheduler_run(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """立即跑一次当前用户的监控（忽略交易时段，用于验证钉钉/策略配置）。"""
    uid = resolve_user_id()
    logger.info("手动触发调度 user=%s", uid)
    return scheduler.run_now(user_id=uid)


# ------------------------------------------------------------------ 备份

@router.get("/backups")
def list_backups() -> Dict[str, Any]:
    _require_admin("查看数据库备份")
    from ..db.database import get_meta

    return {"data": backup_service.list_backups(), "last_backup_date": get_meta("last_backup_date") or ""}


@router.post("/backups/run")
def run_backup() -> Dict[str, Any]:
    _require_admin("执行数据库备份")
    try:
        info = backup_service.create_backup(reason="manual")
    except Exception as exc:  # noqa: BLE001
        logger.exception("手动备份失败")
        raise HTTPException(status_code=500, detail=f"备份失败：{exc}")
    return {"ok": True, "backup": info}


@router.get("/backups/download/{name}")
def download_backup(name: str):
    _require_admin("下载数据库备份")
    try:
        path = backup_service.resolve_backup(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return FileResponse(
        str(path),
        media_type="application/gzip",
        filename=path.name,
    )


@router.post("/backups/restore")
def restore_backup(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """用备份覆盖当前库。危险操作：恢复前会自动生成一份「恢复前快照」。"""
    _require_admin("恢复数据库备份")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    try:
        return backup_service.restore_backup(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("恢复备份失败")
        raise HTTPException(status_code=500, detail=f"恢复失败：{exc}")


@router.get("/backups/export")
def export_my_data() -> Dict[str, Any]:
    """导出当前用户的全部数据（JSON），便于人工核对或迁移。"""
    return backup_service.export_user_json(resolve_user_id())


# ------------------------------------------------------------------ 告警历史

@router.get("/alert_logs")
def list_alert_logs(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    channel: str = "",
    only_failed: bool = False,
) -> Dict[str, Any]:
    uid = resolve_user_id()
    return {
        "data": store.list_alert_logs(uid, limit=limit, offset=offset, channel=channel, only_failed=only_failed),
        "total": store.count_alert_logs(uid, channel=channel, only_failed=only_failed),
    }


@router.post("/alert_logs/clear")
def clear_alert_logs(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    uid = resolve_user_id()
    keep = int(payload.get("keep") or 0)
    if keep <= 0:
        raise HTTPException(status_code=400, detail="keep 必须大于 0（保留最近 N 条）")
    removed = store.purge_alert_logs(uid, keep=keep)
    return {"ok": True, "removed": removed, "kept": keep}


# ------------------------------------------------------------------ 审计日志

@router.get("/audit_logs")
def list_audit_logs(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    path_like: str = "",
    all_users: bool = False,
) -> Dict[str, Any]:
    uid = resolve_user_id()
    if all_users:
        _require_admin("查看全站审计日志")
        return {"data": store.list_audit_logs(user_id=None, limit=limit, offset=offset, path_like=path_like)}
    return {"data": store.list_audit_logs(user_id=uid, limit=limit, offset=offset, path_like=path_like)}
