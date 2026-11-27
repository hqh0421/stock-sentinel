"""交易绩效统计：把台账里的成交记录算成可复核的指标。

为什么要单独一层，而不是塞进 trade_ledger
------------------------------------------
``trade_ledger.summary()`` 只回答「总共赚了多少」。但判断一个人的交易水平，
光看总盈亏没用：胜率高但赚小亏大会亏钱，胜率低但盈亏比够好能长期赚钱。
所以这里把四个必须一起看的指标摆在一起，并且**每个口径都随结果返回**，
避免出现「胜率 62%」这种无法核对、也不知道分母是谁的数字。

口径说明（重要，不要凭直觉改）
------------------------------
- 只统计 ``realized_pnl`` 不为 None 的卖出记录。买入没有已实现盈亏；
  卖出但成本未知的（流水未覆盖建仓时）也算不出来，这两类都不进分母。
- **胜率分母 = 可计算的卖出笔数**，不是全部卖出笔数。分母被悄悄放大会让胜率虚低。
- **盈亏比（profit factor）= 总盈利 / 总亏损的绝对值**，不是「平均盈利/平均亏损」。
  前者是资金口径，后者是单笔口径，差别很大；两个都返回，但主推 profit_factor。
- 期望值 = 平均每笔已实现盈亏。这是「这个打法值不值得做」的直接答案。
- 费用单独汇总，因为它在低频策略里不该被忽略，但也不该混进胜率。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    return round(float(value), digits) if value is not None else None


def compute(since: Optional[str] = None, limit: int = 20000) -> Dict[str, Any]:
    """按标的、按月、按整体汇总已实现盈亏与胜率结构。纯读。"""
    from .trade_ledger import trade_ledger

    rows = trade_ledger.list_trades(limit=limit, since=since)
    if not rows:
        return {
            "ok": True,
            "empty": True,
            "hint": "台账里还没有成交记录。可以先从投资账本同步：sync_trade_ledger()",
            "since": since,
        }

    buys = [r for r in rows if r.get("side") == "buy"]
    sells = [r for r in rows if r.get("side") == "sell"]
    computable = [r for r in sells if r.get("realized_pnl") is not None]

    wins = [r for r in computable if float(r["realized_pnl"]) > 0]
    losses = [r for r in computable if float(r["realized_pnl"]) < 0]
    flats = [r for r in computable if float(r["realized_pnl"]) == 0]

    gross_win = sum(float(r["realized_pnl"]) for r in wins)
    gross_loss = sum(float(r["realized_pnl"]) for r in losses)   # 负数
    realized = gross_win + gross_loss
    total_fee = sum(float(r.get("fee") or 0) for r in rows)

    win_rate = _round(len(wins) * 100.0 / len(computable)) if computable else None
    avg_win = _round(gross_win / len(wins)) if wins else None
    avg_loss = _round(gross_loss / len(losses)) if losses else None
    profit_factor = _round(gross_win / abs(gross_loss), 3) if gross_loss else None
    payoff = _round(avg_win / abs(avg_loss), 3) if (avg_win and avg_loss) else None
    expectancy = _round(realized / len(computable)) if computable else None

    # 按标的聚合：回答「哪只票在给我赚钱、哪只在放血」
    by_stock: Dict[str, Dict[str, Any]] = {}
    for rec in rows:
        sec = str(rec.get("secID") or "")
        if not sec:
            continue
        slot = by_stock.setdefault(sec, {
            "secID": sec,
            "name": rec.get("name") or sec,
            "buy_count": 0, "sell_count": 0,
            "realized_pnl": 0.0, "fee": 0.0,
            "win": 0, "loss": 0,
        })
        slot["fee"] += float(rec.get("fee") or 0)
        if rec.get("side") == "buy":
            slot["buy_count"] += 1
        elif rec.get("side") == "sell":
            slot["sell_count"] += 1
            pnl = rec.get("realized_pnl")
            if pnl is not None:
                slot["realized_pnl"] += float(pnl)
                if float(pnl) > 0:
                    slot["win"] += 1
                elif float(pnl) < 0:
                    slot["loss"] += 1
    for slot in by_stock.values():
        slot["realized_pnl"] = _round(slot["realized_pnl"])
        slot["fee"] = _round(slot["fee"])
        judged = slot["win"] + slot["loss"]
        slot["win_rate"] = _round(slot["win"] * 100.0 / judged) if judged else None
    stock_list = sorted(by_stock.values(), key=lambda x: -(x["realized_pnl"] or 0))

    # 按月聚合：回答「哪个月在亏、亏损是否集中在某段时间」
    by_month: Dict[str, Dict[str, Any]] = {}
    for rec in rows:
        month = str(rec.get("trade_date") or "")[:7]
        if not month:
            continue
        slot = by_month.setdefault(month, {"month": month, "count": 0,
                                           "buy_count": 0, "sell_count": 0,
                                           "realized_pnl": 0.0})
        slot["count"] += 1
        if rec.get("side") == "buy":
            slot["buy_count"] += 1
        elif rec.get("side") == "sell":
            slot["sell_count"] += 1
            if rec.get("realized_pnl") is not None:
                slot["realized_pnl"] += float(rec["realized_pnl"])
    months = []
    for slot in sorted(by_month.values(), key=lambda x: x["month"]):
        slot["realized_pnl"] = _round(slot["realized_pnl"])
        months.append(slot)

    dates = [str(r.get("trade_date") or "") for r in rows if r.get("trade_date")]
    date_range = [min(dates), max(dates)] if dates else None
    month_span = max(1, len(by_month))

    best = max(computable, key=lambda r: float(r["realized_pnl"]), default=None)
    worst = min(computable, key=lambda r: float(r["realized_pnl"]), default=None)

    def brief(rec: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not rec:
            return None
        return {
            "trade_date": rec.get("trade_date"),
            "name": rec.get("name"),
            "secID": rec.get("secID"),
            "shares": rec.get("shares"),
            "price": rec.get("price"),
            "realized_pnl": rec.get("realized_pnl"),
            "note": rec.get("note"),
        }

    return {
        "ok": True,
        "empty": False,
        "since": since,
        "date_range": date_range,
        "scope": "胜率与盈亏比的分母只含「可计算已实现盈亏」的卖出笔数",
        "total_records": len(rows),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "computable_sell_count": len(computable),
        "uncomputable_sell_count": len(sells) - len(computable),
        "realized_pnl": _round(realized),
        "total_fee": _round(total_fee),
        "net_after_fee": _round(realized - total_fee),
        "win_count": len(wins),
        "loss_count": len(losses),
        "flat_count": len(flats),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "payoff_ratio": payoff,
        "expectancy": expectancy,
        "best_trade": brief(best),
        "worst_trade": brief(worst),
        "avg_trades_per_month": _round(len(rows) / month_span, 1),
        "by_stock": stock_list,
        "by_month": months,
    }
