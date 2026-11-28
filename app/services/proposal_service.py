"""建议单与两道确认状态机（human in the loop）。

流程固定为三步，任何一步都不能跳：

    proposed（Agent 给出分析+建议）
        │  用户点「采纳」——第一道确认
        ▼
    accepted
        │  用户点「确认执行」并显式 confirm=true——第二道确认
        ▼
    executed（此时才允许写交易台账/执行计划）

    proposed / accepted 也都可以 → ignored（忽略，只留痕不执行）

设计约束：
- 状态跃迁由服务端校验，非法跃迁直接拒绝（不能靠前端隐藏按钮）；
- 只有 `execute()` 会触碰执行动作，且必须 `confirm=True`；
- 每次跃迁写 audit_log，事后可追溯是谁在什么时候采纳/执行了什么。
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Callable, Dict, List, Optional

from ..db import database

VALID_KINDS = {"rebalance", "buy", "sell", "adjust_ratio", "classify", "review"}

_ALLOWED_FROM = {
    "accepted": {"proposed"},
    "executed": {"accepted"},
    "ignored": {"proposed", "accepted"},
}

_TS_FIELD = {"accepted": "accepted_at", "executed": "executed_at", "ignored": "ignored_at"}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _row(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    item = dict(row)
    raw = item.get("payload") or "{}"
    try:
        item["payload"] = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        item["payload"] = {}
    return item


def _audit(user_id: int, path: str, detail: str, status: int = 200) -> None:
    try:
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO audit_log(user_id, created_at, method, path, status, ip, detail) "
                "VALUES(?,?,?,?,?,?,?)",
                (user_id, _now(), "PROPOSAL", path, status, "", detail[:500]),
            )
    except Exception:  # noqa: BLE001  审计失败不影响主流程
        pass


def create(
    user_id: int,
    kind: str,
    title: str,
    payload: Dict[str, Any],
    analysis: str = "",
) -> Dict[str, Any]:
    """新建建议单，初始状态 proposed。"""
    kind = str(kind or "").strip().lower()
    if kind not in VALID_KINDS:
        raise ValueError(f"未知建议类型：{kind}")
    body = json.dumps(payload or {}, ensure_ascii=False)
    with database.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO agent_proposals(user_id, kind, status, title, payload, analysis, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, kind, "proposed", str(title or "")[:200], body, str(analysis or ""), _now()),
        )
        proposal_id = cur.lastrowid
    _audit(user_id, f"proposal:{proposal_id}:create", f"kind={kind} title={title}")
    return get(user_id, proposal_id)


def get(user_id: int, proposal_id: int) -> Dict[str, Any]:
    with database.connection() as conn:
        row = conn.execute(
            "SELECT * FROM agent_proposals WHERE user_id=? AND id=?",
            (int(user_id), int(proposal_id)),
        ).fetchone()
    return _row(row)


def list_proposals(user_id: int, status: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 20), 200))
    sql = "SELECT * FROM agent_proposals WHERE user_id=?"
    args: List[Any] = [int(user_id)]
    if status:
        sql += " AND status=?"
        args.append(str(status))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with database.connection() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row(row) for row in rows]


def _transition(user_id: int, proposal_id: int, to_status: str, note: str = "") -> Dict[str, Any]:
    allowed = _ALLOWED_FROM.get(to_status, set())
    ts_field = _TS_FIELD.get(to_status, "")
    if not ts_field:
        raise ValueError(f"未知目标状态：{to_status}")
    with database.transaction() as conn:
        row = conn.execute(
            "SELECT status FROM agent_proposals WHERE user_id=? AND id=?",
            (int(user_id), int(proposal_id)),
        ).fetchone()
        if row is None:
            raise LookupError("建议单不存在")
        current = str(row["status"])
        if current not in allowed:
            raise ValueError(f"非法状态跃迁：{current} → {to_status}")
        conn.execute(
            f"UPDATE agent_proposals SET status=?, {ts_field}=?, note=? WHERE user_id=? AND id=?",
            (to_status, _now(), str(note or "")[:500], int(user_id), int(proposal_id)),
        )
    _audit(user_id, f"proposal:{proposal_id}:{to_status}", f"{to_status} note={note}")
    return get(user_id, proposal_id)


def accept(user_id: int, proposal_id: int, note: str = "") -> Dict[str, Any]:
    """第一道确认：用户决定「是否跟随」。"""
    return _transition(user_id, proposal_id, "accepted", note)


def ignore(user_id: int, proposal_id: int, note: str = "") -> Dict[str, Any]:
    """忽略：只留痕，不产生任何执行。"""
    return _transition(user_id, proposal_id, "ignored", note)


def execute(
    user_id: int,
    proposal_id: int,
    confirm: bool = False,
    note: str = "",
    executor: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    """第二道确认：必须显式 confirm=True，且已通过第一道采纳。

    executor 由调用方注入（负责真正写台账/执行计划）。executor 抛错时状态保持
    accepted，不会变成 executed——避免「执行失败但显示已执行」。
    """
    if not confirm:
        raise ValueError("二次确认未提供：执行必须显式 confirm=true")
    current = get(user_id, proposal_id)
    if not current:
        raise LookupError("建议单不存在")
    if current.get("status") != "accepted":
        raise ValueError(f"未通过第一道确认，当前状态 {current.get('status')}，不能执行")

    result: Any = None
    if executor is not None:
        result = executor(current.get("payload") or {})

    updated = _transition(user_id, proposal_id, "executed", note)
    updated["executor_result"] = result
    return updated


def build_rebalance_proposal(
    user_id: int,
    plan: List[Dict[str, Any]],
    analysis: str = "",
    title: str = "四池再平衡建议",
) -> Dict[str, Any]:
    """把 allocation_service.rebalance_plan() 的输出包装成建议单。"""
    payload = {
        "type": "rebalance",
        "actions": plan,
        "constraint": "只在本池内调拨，不跨池挪用；执行前需二次确认",
    }
    return create(user_id, "rebalance", title, payload, analysis)
