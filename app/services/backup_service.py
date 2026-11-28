"""数据库备份与导出。

为什么必须有
------------
多用户改造之后，``config/app.db`` 成了所有用户数据的唯一真源：丢了它，
自选、持仓、交易流水、设置全部归零。以前丢一个 JSON 只是丢一项，现在
丢的是所有人的全部。

策略
----
- **每日自动快照**：调度器每小时 tick 一次，发现当天还没备份就跑一份；
- **手动备份**：管理后台按钮；
- **保留策略**：默认保留 14 天且最多 30 份，按时间从旧到新删；
- **导出口径**：备份文件是完整 SQLite（``.db.gz``），可以直接下载后用
  sqlite3 打开；另提供按用户导出 JSON，便于人工核对。

恢复为什么用 sqlite backup API 而不是拷文件
------------------------------------------
拷文件会漏掉 WAL 里还没 checkpoint 的数据，且正写到一半时拷出来的库可能
是坏的。``sqlite3.Connection.backup`` 是官方支持的在线热备，源目标都开着
连接也能拿到一致快照。
"""
from __future__ import annotations

import datetime
import gzip
import json
import logging
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..db import database, store

logger = logging.getLogger(__name__)

BACKUP_DIR_NAME = "backups"
DEFAULT_KEEP_DAYS = 14
DEFAULT_KEEP_COUNT = 30
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def backup_dir() -> Path:
    override = os.environ.get("APP_BACKUP_DIR", "").strip()
    root = Path(override) if override else database.db_path().parent / BACKUP_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def create_backup(reason: str = "manual") -> Dict[str, Any]:
    """生成一份 gzip 压缩的完整库快照。返回文件信息。"""
    database.init_db()
    src_path = database.db_path()
    if not src_path.exists():
        raise FileNotFoundError("数据库文件不存在，无法备份")
    safe_reason = re.sub(r"[^a-z0-9_-]", "", str(reason or "manual").lower())[:20] or "manual"
    name = f"app-{_timestamp()}-{safe_reason}.db.gz"
    target = backup_dir() / name
    tmp_db = backup_dir() / f".tmp-{name}.db"
    try:
        # 1) 在线热备到一个临时 db
        src = sqlite3.connect(str(src_path), timeout=10.0)
        try:
            dst = sqlite3.connect(str(tmp_db))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        # 2) 压缩后删除明文临时文件（磁盘上不出现第二份未加密的完整库）
        with open(tmp_db, "rb") as fin, gzip.open(target, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout)
    finally:
        for suffix in ("", "-wal", "-shm"):
            leftover = Path(str(tmp_db) + suffix)
            if leftover.exists():
                try:
                    leftover.unlink()
                except OSError:
                    pass
    size = target.stat().st_size if target.exists() else 0
    logger.info("数据库备份完成 file=%s size=%s reason=%s", name, size, reason)
    prune_backups()
    return {
        "name": name,
        "size": int(size),
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reason": safe_reason,
    }


def ensure_daily_backup() -> Optional[Dict[str, Any]]:
    """当天还没备份就备份一次；返回新建的文件信息（已备份过返回 None）。"""
    today = datetime.date.today().isoformat()
    last = database.get_meta("last_backup_date") or ""
    if last == today:
        return None
    try:
        info = create_backup(reason="daily")
    except Exception as exc:  # noqa: BLE001
        logger.warning("每日备份失败 error=%r", exc)
        return None
    database.set_meta("last_backup_date", today)
    return info


def list_backups(limit: int = 50) -> List[Dict[str, Any]]:
    root = backup_dir()
    items: List[Dict[str, Any]] = []
    for path in sorted(root.glob("app-*.db.gz"), key=lambda p: p.name, reverse=True)[: max(1, limit)]:
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            {
                "name": path.name,
                "size": int(stat.st_size),
                "created_at": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return items


def resolve_backup(name: str) -> Path:
    """把文件名解析成路径，严格挡路径穿越。"""
    clean = str(name or "").strip()
    if not clean or not _SAFE_NAME.match(clean) or not clean.endswith(".db.gz"):
        raise ValueError("非法的备份文件名")
    path = backup_dir() / clean
    if not path.exists():
        raise FileNotFoundError("备份文件不存在")
    return path


def prune_backups(keep_days: int = DEFAULT_KEEP_DAYS, keep_count: int = DEFAULT_KEEP_COUNT) -> int:
    root = backup_dir()
    files = sorted(root.glob("app-*.db.gz"), key=lambda p: p.name)
    cutoff = datetime.datetime.now() - datetime.timedelta(days=max(1, keep_days))
    removed = 0
    keep_tail = files[-max(1, keep_count):]
    for path in files:
        if path in keep_tail:
            continue
        try:
            mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:  # noqa: BLE001
            logger.warning("删除过期备份失败 file=%s error=%r", path.name, exc)
    if removed:
        logger.info("清理过期备份 removed=%s", removed)
    return removed


def restore_backup(name: str) -> Dict[str, Any]:
    """用备份覆盖当前库。恢复前会先把当前库另存一份，防止恢复错文件。"""
    path = resolve_backup(name)
    safety = create_backup(reason="beforerestore")
    target = database.db_path()
    tmp_db = backup_dir() / f".restore-{_timestamp()}.db"
    try:
        with gzip.open(path, "rb") as fin, open(tmp_db, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        # 校验：能打开且表结构在
        check = sqlite3.connect(str(tmp_db))
        try:
            tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            check.close()
        if "users" not in tables:
            raise ValueError("备份文件不是本系统的数据库（缺少 users 表）")
        src = sqlite3.connect(str(tmp_db))
        try:
            dst = sqlite3.connect(str(target))
            try:
                src.backup(dst)
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                dst.close()
        finally:
            src.close()
        database.init_db(force=True)
        logger.warning("数据库已从备份恢复 file=%s 恢复前快照=%s", name, safety.get("name"))
        return {"ok": True, "restored": name, "safety_backup": safety.get("name")}
    finally:
        if tmp_db.exists():
            try:
                tmp_db.unlink()
            except OSError:
                pass


def export_user_json(user_id: int) -> Dict[str, Any]:
    """按用户导出全部数据，便于人工核对或迁移。"""
    user = store.get_user_by_id(int(user_id)) or {}
    return {
        "exported_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user": {
            "id": user.get("id"),
            "username": user.get("username"),
            "display_name": user.get("display_name"),
        },
        "settings": store.get_settings(int(user_id)),
        "favorites": store.list_favorites(int(user_id)),
        "trades": store.list_trades(int(user_id), limit=100000),
        "strategies": store.list_strategies(int(user_id)),
        "alert_rules": store.get_alert_rules(int(user_id)) or {},
        "alert_logs": store.list_alert_logs(int(user_id), limit=1000),
    }
