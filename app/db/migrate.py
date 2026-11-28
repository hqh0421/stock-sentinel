"""把历史单用户 JSON 配置迁移到数据库的首个用户。

迁移原则
--------
1. **只搬不改**：原 ``config/*.json`` 文件保留不动，作为回滚备份；
   迁移后用 meta 表打标记，不会重复导入覆盖新数据。
2. **不覆盖新数据**：只有该用户在对应表里还没有数据时才导入。
3. 失败不阻断启动：任何单项迁移异常只记日志，其余项继续。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from . import store
from .database import get_meta, set_meta

logger = logging.getLogger("app.db.migrate")


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("legacy_read_failed file=%s error=%r", path.name, exc)
        return default


def migrate_legacy_json(user_id: int, config_dir: str | Path, now_text: str) -> Dict[str, int]:
    """把 config 目录下的用户态 JSON 导入指定用户。返回各表导入条数。"""
    marker = f"legacy_migrated_{user_id}"
    if get_meta(marker):
        return {}

    cfg = Path(config_dir)
    stats: Dict[str, int] = {}

    if not store.list_favorites(user_id):
        items = _read_json(cfg / "favorites.json", [])
        if isinstance(items, list) and items:
            store.replace_favorites(user_id, items)
            stats["favorites"] = len(items)

    if not store.get_settings(user_id):
        data = _read_json(cfg / "user_settings.json", {})
        if isinstance(data, dict) and data:
            store.save_settings(user_id, data, now_text)
            stats["settings"] = 1

    if store.count_trades(user_id) == 0:
        rows = _read_json(cfg / "trade_log.json", [])
        if isinstance(rows, list) and rows:
            store.replace_trades(user_id, rows)
            stats["trades"] = len(rows)

    if not store.list_strategies(user_id):
        rows = _read_json(cfg / "strategies.json", [])
        if isinstance(rows, list):
            count = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or "").strip()
                if not name:
                    continue
                store.upsert_strategy(
                    user_id,
                    name,
                    str(row.get("entry_expr") or ""),
                    str(row.get("exit_expr") or ""),
                    str(row.get("created_at") or now_text),
                )
                count += 1
            if count:
                stats["strategies"] = count

    if store.get_alert_rules(user_id) is None:
        data = _read_json(cfg / "alert_rules.json", {})
        if isinstance(data, dict) and data:
            store.save_alert_rules(user_id, data, now_text)
            stats["alert_rules"] = 1

    set_meta(marker, now_text)
    logger.info("legacy_config_migrated user_id=%s stats=%s", user_id, stats)
    return stats
