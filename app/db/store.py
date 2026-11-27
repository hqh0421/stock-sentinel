"""仓储层：所有用户态数据的读写入口。

约定
----
- 每个函数第一个参数都是 ``user_id``，不做「当前用户」推断——推断是上层
  （``app/core/context.py``）的职责，这里只认显式传入的用户。
- 返回的都是普通 dict/list，业务层无需感知 sqlite3.Row。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .database import connection, transaction


def _row_to_dict(row: Any) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


# ---------------------------------------------------------------- 用户

def count_users() -> int:
    with connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return int(row["n"]) if row else 0


def first_user_id() -> Optional[int]:
    with connection() as conn:
        row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (str(username or "").strip(),)
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
    return _row_to_dict(row) if row else None


def list_users() -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT id, username, display_name, created_at, is_active FROM users ORDER BY id"
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def list_active_user_ids() -> List[int]:
    """调度器用：只跑启用中的账号。"""
    with connection() as conn:
        rows = conn.execute("SELECT id FROM users WHERE is_active = 1 ORDER BY id").fetchall()
    return [int(row["id"]) for row in rows]


def create_user(
    username: str,
    salt: str,
    password_hash: str,
    display_name: str = "",
    created_at: str = "",
) -> int:
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO users(username, display_name, salt, password_hash, created_at, is_active) "
            "VALUES(?, ?, ?, ?, ?, 1)",
            (
                str(username or "").strip(),
                str(display_name or "").strip(),
                salt,
                password_hash,
                created_at,
            ),
        )
        return int(cur.lastrowid)


def update_user_password(user_id: int, salt: str, password_hash: str) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE users SET salt = ?, password_hash = ? WHERE id = ?",
            (salt, password_hash, int(user_id)),
        )


def set_user_active(user_id: int, active: bool) -> None:
    with transaction() as conn:
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, int(user_id)))


# ---------------------------------------------------------------- 会话

def create_session(session_id: str, user_id: int, expiry: int, user_agent: str, created_at: str) -> None:
    with transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions(id, user_id, expiry, created_at, user_agent) "
            "VALUES(?, ?, ?, ?, ?)",
            (session_id, int(user_id), int(expiry), created_at, str(user_agent or "")[:400]),
        )


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (str(session_id or ""),)).fetchone()
    return _row_to_dict(row) if row else None


def delete_session(session_id: str) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (str(session_id or ""),))


def delete_user_sessions(user_id: int, keep_session_id: Optional[str] = None) -> None:
    with transaction() as conn:
        if keep_session_id:
            conn.execute(
                "DELETE FROM sessions WHERE user_id = ? AND id != ?",
                (int(user_id), str(keep_session_id)),
            )
        else:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (int(user_id),))


def purge_expired_sessions(now_ts: int) -> int:
    with transaction() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expiry <= ?", (int(now_ts),))
        return int(cur.rowcount or 0)


# ---------------------------------------------------------------- 设置

def get_settings(user_id: int) -> Dict[str, Any]:
    with connection() as conn:
        row = conn.execute("SELECT data FROM user_settings WHERE user_id = ?", (int(user_id),)).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["data"] or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(user_id: int, data: Dict[str, Any], updated_at: str) -> None:
    payload = json.dumps(data if isinstance(data, dict) else {}, ensure_ascii=False)
    with transaction() as conn:
        conn.execute(
            "INSERT INTO user_settings(user_id, data, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (int(user_id), payload, updated_at),
        )


# ---------------------------------------------------------------- 自选/持仓

def list_favorites(user_id: int) -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT secID, shares, cost, horizon, group_name, note, pool FROM favorites "
            "WHERE user_id = ? ORDER BY ordinal, secID",
            (int(user_id),),
        ).fetchall()
    return [
        {
            "secID": row["secID"],
            "shares": int(row["shares"] or 0),
            "cost": float(row["cost"] or 0.0),
            "horizon": str(row["horizon"] or "long"),
            "group": str(row["group_name"] or ""),
            "note": str(row["note"] or ""),
            "pool": str(row["pool"] or ""),
        }
        for row in rows
    ]


def replace_favorites(user_id: int, items: List[Dict[str, Any]]) -> None:
    """整表同步。自选一般只有几只，整表替换比 diff 更不容易出错。"""
    rows = []
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        sec_id = str(item.get("secID") or "").strip()
        if not sec_id:
            continue
        rows.append(
            (
                int(user_id),
                sec_id,
                int(item.get("shares") or 0),
                float(item.get("cost") or 0.0),
                str(item.get("horizon") or "long"),
                index,
                str(item.get("group") or item.get("group_name") or "").strip()[:40],
                str(item.get("note") or "").strip()[:200],
                str(item.get("pool") or "").strip()[:20],
            )
        )
    with transaction() as conn:
        conn.execute("DELETE FROM favorites WHERE user_id = ?", (int(user_id),))
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO favorites"
                "(user_id, secID, shares, cost, horizon, ordinal, group_name, note, pool) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )


# ---------------------------------------------------------------- 交易流水

def list_trades(user_id: int, limit: int = 2000) -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT payload FROM trades WHERE user_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(user_id), max(1, int(limit))),
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            record = json.loads(row["payload"] or "{}")
        except Exception:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def count_trades(user_id: int) -> int:
    with connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM trades WHERE user_id = ?", (int(user_id),)).fetchone()
    return int(row["n"]) if row else 0


def replace_trades(user_id: int, records: List[Dict[str, Any]]) -> None:
    """整表同步（保留最近 N 条）。流水是只追加的，整表替换保证顺序与内容一致。"""
    rows = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        record_id = str(record.get("id") or "").strip()
        if not record_id:
            continue
        rows.append(
            (
                int(user_id),
                record_id,
                str(record.get("created_at") or ""),
                str(record.get("trade_date") or ""),
                str(record.get("secID") or ""),
                str(record.get("side") or ""),
                json.dumps(record, ensure_ascii=False),
            )
        )
    with transaction() as conn:
        conn.execute("DELETE FROM trades WHERE user_id = ?", (int(user_id),))
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO trades(user_id, id, created_at, trade_date, secID, side, payload) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                rows,
            )


# ---------------------------------------------------------------- 策略（表达式因子）

# 因子（表达式策略）可持久化的全部字段；新增字段走 _COLUMN_MIGRATIONS 补列，老库默认值为空/0
_STRATEGY_COLUMNS = (
    "name, entry_expr, exit_expr, description, benchmark, capital, fee_rate, "
    "stamp_tax_rate, slippage_pct, position_pct, metrics_json, created_at, updated_at"
)


def _parse_strategy_row(row: Any) -> Dict[str, Any]:
    item = _row_to_dict(row)
    # metrics_json：最近一次回测指标快照，解析失败退化为 None
    raw = item.pop("metrics_json", None) or ""
    try:
        item["metrics"] = json.loads(raw) if raw else None
    except Exception:
        item["metrics"] = None
    if not item.get("updated_at"):
        item.pop("updated_at", None)
    return item


def list_strategies(user_id: int) -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            f"SELECT {_STRATEGY_COLUMNS} FROM strategies "
            "WHERE user_id = ? ORDER BY created_at, name",
            (int(user_id),),
        ).fetchall()
    return [_parse_strategy_row(row) for row in rows]


def get_strategy(user_id: int, name: str) -> Optional[Dict[str, Any]]:
    with connection() as conn:
        row = conn.execute(
            f"SELECT {_STRATEGY_COLUMNS} FROM strategies WHERE user_id = ? AND name = ?",
            (int(user_id), str(name)),
        ).fetchone()
    return _parse_strategy_row(row) if row else None


def save_factor(user_id: int, factor: Dict[str, Any], now_text: str) -> None:
    """全字段 upsert 一个表达式因子。仅写传入的字段，缺失项用默认值/保留旧值。"""
    if not isinstance(factor, dict):
        return
    name = str(factor.get("name") or "").strip()
    if not name:
        return
    entry_expr = str(factor.get("entry_expr") or "")
    exit_expr = str(factor.get("exit_expr") or "")
    description = str(factor.get("description") or "").strip()[:500]
    benchmark = str(factor.get("benchmark") or "hs300").strip()[:20] or "hs300"

    def num(v: Any, default: float) -> float:
        try:
            fv = float(v)
            return fv if __import__("math").isfinite(fv) else default
        except Exception:
            return default

    capital = num(factor.get("capital"), 100000.0)
    fee_rate = num(factor.get("fee_rate"), 0.00075)
    stamp_tax_rate = num(factor.get("stamp_tax_rate"), 0.0005)
    slippage_pct = num(factor.get("slippage_pct"), 0.001)
    position_pct = max(0.05, min(1.0, num(factor.get("position_pct"), 1.0)))
    metrics = factor.get("metrics")
    try:
        metrics_json = json.dumps(metrics, ensure_ascii=False) if metrics else ""
    except Exception:
        metrics_json = ""

    with transaction() as conn:
        existing = conn.execute(
            "SELECT created_at FROM strategies WHERE user_id = ? AND name = ?",
            (int(user_id), name),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE strategies SET entry_expr=?, exit_expr=?, description=?, benchmark=?, "
                "capital=?, fee_rate=?, stamp_tax_rate=?, slippage_pct=?, position_pct=?, "
                "metrics_json=?, updated_at=? WHERE user_id=? AND name=?",
                (entry_expr, exit_expr, description, benchmark, capital, fee_rate,
                 stamp_tax_rate, slippage_pct, position_pct, metrics_json, now_text,
                 int(user_id), name),
            )
        else:
            conn.execute(
                "INSERT INTO strategies(user_id, name, entry_expr, exit_expr, description, benchmark, "
                "capital, fee_rate, stamp_tax_rate, slippage_pct, position_pct, metrics_json, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(user_id), name, entry_expr, exit_expr, description, benchmark, capital,
                 fee_rate, stamp_tax_rate, slippage_pct, position_pct, metrics_json,
                 now_text, now_text),
            )


def upsert_strategy(user_id: int, name: str, entry_expr: str, exit_expr: str, now_text: str) -> None:
    """旧签名兼容（历史 JSON 迁移用）：只写表达式，其余字段走默认值。"""
    save_factor(
        user_id,
        {"name": name, "entry_expr": entry_expr, "exit_expr": exit_expr},
        now_text,
    )


def delete_strategy(user_id: int, name: str) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM strategies WHERE user_id = ? AND name = ?", (int(user_id), str(name)))


# ---------------------------------------------------------------- 告警规则

def get_alert_rules(user_id: int) -> Optional[Dict[str, Any]]:
    with connection() as conn:
        row = conn.execute("SELECT data FROM alert_rules WHERE user_id = ?", (int(user_id),)).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["data"] or "{}")
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def save_alert_rules(user_id: int, data: Dict[str, Any], updated_at: str) -> None:
    payload = json.dumps(data if isinstance(data, dict) else {}, ensure_ascii=False)
    with transaction() as conn:
        conn.execute(
            "INSERT INTO alert_rules(user_id, data, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (int(user_id), payload, updated_at),
        )


# ---------------------------------------------------------------- 告警历史

def add_alert_log(
    user_id: int,
    created_at: str,
    channel: str,
    title: str,
    content: str,
    item_count: int = 0,
    ok: bool = False,
    error: str = "",
    source: str = "auto",
) -> int:
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO alert_log(user_id, created_at, channel, title, content, item_count, ok, error, source) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(user_id),
                created_at,
                str(channel or "")[:80],
                str(title or "")[:200],
                str(content or "")[:8000],
                int(item_count or 0),
                1 if ok else 0,
                str(error or "")[:1000],
                str(source or "auto")[:20],
            ),
        )
        return int(cur.lastrowid)


def list_alert_logs(
    user_id: int,
    limit: int = 50,
    offset: int = 0,
    channel: str = "",
    only_failed: bool = False,
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM alert_log WHERE user_id = ?"
    params: List[Any] = [int(user_id)]
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    if only_failed:
        sql += " AND ok = 0"
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, int(limit)), max(0, int(offset))])
    with connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for row in rows:
        item = _row_to_dict(row)
        item["ok"] = bool(item.get("ok"))
        out.append(item)
    return out


def count_alert_logs(user_id: int, channel: str = "", only_failed: bool = False) -> int:
    sql = "SELECT COUNT(*) AS n FROM alert_log WHERE user_id = ?"
    params: List[Any] = [int(user_id)]
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    if only_failed:
        sql += " AND ok = 0"
    with connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def purge_alert_logs(user_id: int, keep: int = 500) -> int:
    """只保留最近 N 条，防止告警历史把库撑大（监控一分钟一条也不会失控）。"""
    with transaction() as conn:
        cur = conn.execute(
            "DELETE FROM alert_log WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM alert_log WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (int(user_id), int(user_id), max(1, int(keep))),
        )
        return int(cur.rowcount or 0)


# ---------------------------------------------------------------- Agent 对话

def add_agent_message(
    user_id: int,
    conversation_id: str,
    role: str,
    content: str,
    meta: Optional[Dict[str, Any]] = None,
    created_at: str = "",
) -> int:
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO agent_messages(user_id, conversation_id, role, content, meta, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (
                int(user_id),
                str(conversation_id or ""),
                str(role or ""),
                str(content or "")[:20000],
                json.dumps(meta or {}, ensure_ascii=False)[:20000],
                created_at,
            ),
        )
        return int(cur.lastrowid)


def list_agent_messages(user_id: int, conversation_id: str = "", limit: int = 60) -> List[Dict[str, Any]]:
    with connection() as conn:
        if conversation_id:
            rows = conn.execute(
                "SELECT * FROM agent_messages WHERE user_id = ? AND conversation_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (int(user_id), str(conversation_id), max(1, int(limit))),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (int(user_id), max(1, int(limit))),
            ).fetchall()
    out = []
    for row in reversed(rows):
        item = _row_to_dict(row)
        try:
            item["meta"] = json.loads(item.get("meta") or "{}")
        except Exception:
            item["meta"] = {}
        out.append(item)
    return out


def list_agent_conversations(user_id: int, limit: int = 30) -> List[Dict[str, Any]]:
    """会话列表：每个 conversation_id 取最后一条问答作为摘要。"""
    with connection() as conn:
        rows = conn.execute(
            "SELECT conversation_id, MAX(id) AS last_id, MIN(created_at) AS started_at, "
            "MAX(created_at) AS updated_at, COUNT(*) AS n "
            "FROM agent_messages WHERE user_id = ? GROUP BY conversation_id "
            "ORDER BY last_id DESC LIMIT ?",
            (int(user_id), max(1, int(limit))),
        ).fetchall()
        out = []
        for row in rows:
            msg = conn.execute(
                "SELECT content, role FROM agent_messages WHERE id = ?", (int(row["last_id"]),)
            ).fetchone()
            question = conn.execute(
                "SELECT content FROM agent_messages WHERE user_id = ? AND conversation_id = ? "
                "AND role = 'user' ORDER BY id LIMIT 1",
                (int(user_id), row["conversation_id"]),
            ).fetchone()
            out.append(
                {
                    "conversation_id": row["conversation_id"],
                    "title": (question["content"] if question else "")[:80],
                    "last_content": (msg["content"] if msg else "")[:160],
                    "message_count": int(row["n"] or 0),
                    "started_at": row["started_at"],
                    "updated_at": row["updated_at"],
                }
            )
    return out


def delete_agent_conversation(user_id: int, conversation_id: str) -> int:
    with transaction() as conn:
        cur = conn.execute(
            "DELETE FROM agent_messages WHERE user_id = ? AND conversation_id = ?",
            (int(user_id), str(conversation_id)),
        )
        return int(cur.rowcount or 0)


def purge_agent_messages(user_id: int, keep: int = 2000) -> int:
    with transaction() as conn:
        cur = conn.execute(
            "DELETE FROM agent_messages WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM agent_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (int(user_id), int(user_id), max(1, int(keep))),
        )
        return int(cur.rowcount or 0)


# ---------------------------------------------------------------- 审计日志

def add_audit_log(
    user_id: Optional[int],
    created_at: str,
    method: str,
    path: str,
    status: int,
    ip: str = "",
    detail: str = "",
) -> None:
    with transaction() as conn:
        conn.execute(
            "INSERT INTO audit_log(user_id, created_at, method, path, status, ip, detail) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                int(user_id) if user_id else None,
                created_at,
                str(method or "")[:10],
                str(path or "")[:300],
                int(status or 0),
                str(ip or "")[:64],
                str(detail or "")[:2000],
            ),
        )


def list_audit_logs(
    user_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
    path_like: str = "",
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM audit_log WHERE 1=1"
    params: List[Any] = []
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(int(user_id))
    if path_like:
        sql += " AND path LIKE ?"
        params.append(f"%{path_like}%")
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, int(limit)), max(0, int(offset))])
    with connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def purge_audit_logs(keep_days: int = 90) -> int:
    """审计日志按天保留：太老的价值低，还会把库撑大。"""
    import datetime as _dt

    cutoff = (_dt.datetime.now() - _dt.timedelta(days=max(1, int(keep_days)))).strftime("%Y-%m-%d %H:%M:%S")
    with transaction() as conn:
        cur = conn.execute("DELETE FROM audit_log WHERE created_at < ?", (cutoff,))
        return int(cur.rowcount or 0)


# ---------------------------------------------------------------- 市场情绪快照
# 全局数据，无 user_id：同一市场的同一交易日只有一条，多用户共享同一份。

_SENTIMENT_COLUMNS = ("trade_date", "zt_count", "dt_count", "max_board",
                      "seal_rate", "temperature", "phase")


def list_sentiment_snapshots(limit: int = 90) -> List[Dict[str, Any]]:
    """按日期升序返回最近 N 条快照（升序便于算「连续几天」「分位」）。

    字段名对齐旧 JSON 的 ``date``，上层（情绪历史分析）不用改口径。
    """
    with connection() as conn:
        rows = conn.execute(
            'SELECT trade_date AS "date", zt_count, dt_count, max_board, '
            "seal_rate, temperature, phase "
            "FROM sentiment_snapshots ORDER BY trade_date DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [_row_to_dict(row) for row in reversed(rows)]


def count_sentiment_snapshots() -> int:
    with connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM sentiment_snapshots").fetchone()
    return int(row["n"]) if row else 0


def upsert_sentiment_snapshot(item: Dict[str, Any], now_text: str = "") -> None:
    """写一条快照。同一天重复写按覆盖处理（当天盘中可能刷新多次）。"""
    values = (
        str(item.get("trade_date") or "")[:10],
        _to_int(item.get("zt_count")),
        _to_int(item.get("dt_count")),
        _to_int(item.get("max_board")),
        _to_float(item.get("seal_rate")),
        _to_float(item.get("temperature")),
        str(item.get("phase") or ""),
        now_text,
    )
    if not values[0]:
        return
    with transaction() as conn:
        conn.execute(
            "INSERT INTO sentiment_snapshots"
            "(trade_date, zt_count, dt_count, max_board, seal_rate, temperature, phase, created_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(trade_date) DO UPDATE SET "
            "zt_count=excluded.zt_count, dt_count=excluded.dt_count, "
            "max_board=excluded.max_board, seal_rate=excluded.seal_rate, "
            "temperature=excluded.temperature, phase=excluded.phase",
            values,
        )


def prune_sentiment_snapshots(keep: int = 90) -> int:
    """只留最近 N 条。按日期倒序取第 N 条作为水位，删更早的。"""
    keep = max(1, int(keep))
    with transaction() as conn:
        row = conn.execute(
            "SELECT trade_date FROM sentiment_snapshots ORDER BY trade_date DESC LIMIT 1 OFFSET ?",
            (keep - 1,),
        ).fetchone()
        if not row:
            return 0
        cur = conn.execute("DELETE FROM sentiment_snapshots WHERE trade_date < ?",
                           (row["trade_date"],))
        return int(cur.rowcount or 0)


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 时间止损豁免
# 全局表（项目实际单用户）：豁免是「这笔持仓暂时不砍」的决策记录，要留痕可复盘。

def list_time_stop_overrides() -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT sec_id, reason, basis, created_on, expires_on, days, meta, updated_at "
            "FROM time_stop_overrides ORDER BY created_on"
        ).fetchall()
    out = []
    for row in rows:
        item = _row_to_dict(row)
        item["secID"] = item.pop("sec_id")
        try:
            item["meta"] = json.loads(item.get("meta") or "{}")
        except Exception:  # noqa: BLE001
            item["meta"] = {}
        out.append(item)
    return out


def upsert_time_stop_override(item: Dict[str, Any]) -> None:
    values = (
        str(item.get("secID") or ""),
        str(item.get("reason") or ""),
        str(item.get("basis") or ""),
        str(item.get("created_on") or ""),
        str(item.get("expires_on") or ""),
        int(item.get("days") or 0),
        json.dumps(item.get("meta") or {}, ensure_ascii=False),
        str(item.get("updated_at") or ""),
    )
    if not values[0]:
        return
    with transaction() as conn:
        conn.execute(
            "INSERT INTO time_stop_overrides"
            "(sec_id, reason, basis, created_on, expires_on, days, meta, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(sec_id) DO UPDATE SET "
            "reason=excluded.reason, basis=excluded.basis, created_on=excluded.created_on, "
            "expires_on=excluded.expires_on, days=excluded.days, meta=excluded.meta, "
            "updated_at=excluded.updated_at",
            values,
        )


def delete_time_stop_override(sec_id: str) -> bool:
    with transaction() as conn:
        cur = conn.execute("DELETE FROM time_stop_overrides WHERE sec_id = ?", (str(sec_id or ""),))
        return bool(cur.rowcount)
