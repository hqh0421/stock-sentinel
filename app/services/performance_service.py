"""账户绩效分析：净值曲线、最大回撤、胜率、基准对比。

口径说明（重要，避免"数字对不上"的争论）
--------------------------------------
- **权益 = 现金 + 持仓市值**。现金由「当前可用资金」按交易流水**反向回推**：
  某天之后买过多少就加回去、卖过多少就扣回来，保证曲线末端严格等于今天的总资产。
- **持仓**由流水重放得出，不依赖 favorites 的当前值（当前值只是末端快照）。
- **价格**用日线收盘价；当天没有收盘价（停牌/新上市）就沿用最近一个有效收盘价。
- **基准**为沪深 300（``sh000300``），与净值曲线同起点归一化为 1.0，只比形态不比绝对值。
- **胜率/盈亏比**只统计带 ``realized_pnl`` 的卖出记录（账本在卖出时已按加权成本算好）。

为什么不缓存
------------
绩效是"打开才看"的低频操作，缓存带来的失效问题（流水/持仓一变就脏）比收益大。
日线本身在 ``stock_service`` 里已有缓存，这里的重复成本可以接受。
"""
from __future__ import annotations

import datetime
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from ..core.context import resolve_user_id
from ..db import store

logger = logging.getLogger(__name__)

BENCHMARK_CODE = "sh000300"
BENCHMARK_NAME = "沪深300"
MAX_POSITION_SERIES = 30
# 质量闸门：少于这些根数就认为数据源返回残缺，不拿它算净值
MIN_BARS = 10
MIN_DAYS = 5


def _f(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _trade_day(record: Dict[str, Any]) -> str:
    day = str(record.get("trade_date") or "").strip()[:10]
    if day:
        return day
    return str(record.get("created_at") or "").strip()[:10]


def _cash_effect(record: Dict[str, Any]) -> float:
    """一笔流水对现金的影响：买入为负、卖出为正，纠偏(adjust)不动现金。"""
    side = str(record.get("side") or "").lower()
    amount = _f(record.get("amount")) or 0.0
    fee = _f(record.get("fee")) or 0.0
    if side == "buy":
        return -(amount + fee)
    if side == "sell":
        return amount - fee
    return 0.0


def _shares_delta(record: Dict[str, Any]) -> Tuple[str, int]:
    side = str(record.get("side") or "").lower()
    sec = str(record.get("secID") or "")
    shares = int(_f(record.get("shares")) or 0)
    if side == "buy":
        return sec, shares
    if side == "sell":
        return sec, -shares
    if side == "adjust":
        after = int(_f(record.get("shares_after")) or 0)
        before = int(_f(record.get("shares_before")) or 0)
        return sec, after - before
    return sec, 0


def _max_drawdown(values: List[float]) -> Tuple[float, float]:
    """返回 (最大回撤百分比, 当前回撤百分比)，均为正数。"""
    peak = None
    worst = 0.0
    current = 0.0
    for value in values:
        if value is None:
            continue
        if peak is None or value > peak:
            peak = value
        if peak and peak > 0:
            dd = (peak - value) / peak * 100.0
            worst = max(worst, dd)
            current = dd
    return round(worst, 2), round(current, 2)


def _parse_day(text: Any) -> Optional[datetime.date]:
    raw = str(text or "").strip()[:10]
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return None


def _merge_flows(pairs: List[Tuple[str, float]]) -> List[Tuple[datetime.date, float]]:
    """同日多笔合并，避免同一折现因子被重复计算（结果不变，只是更干净）。"""
    by_day: Dict[str, float] = {}
    for day, amount in pairs:
        parsed = _parse_day(day)
        if parsed is None or abs(amount) < 1e-9:
            continue
        key = parsed.isoformat()
        by_day[key] = by_day.get(key, 0.0) + amount
    return sorted(((datetime.date.fromisoformat(k), v) for k, v in by_day.items()),
                  key=lambda x: x[0])


def _npv(rate: float, flows: List[Tuple[datetime.date, float]]) -> float:
    base = flows[0][0]
    total = 0.0
    for day, amount in flows:
        years = (day - base).days / 365.0
        total += amount / ((1.0 + rate) ** years)
    return total


def _npv_deriv(rate: float, flows: List[Tuple[datetime.date, float]]) -> float:
    base = flows[0][0]
    total = 0.0
    for day, amount in flows:
        years = (day - base).days / 365.0
        total += -years * amount / ((1.0 + rate) ** (years + 1.0))
    return total


def xirr(flows: List[Tuple[datetime.date, float]]) -> Optional[float]:
    """资金加权年化收益率（百分数）。无解或全同号返回 None，绝不猜。

    牛顿法优先、二分兜底：单一算法在接近 -100% 或长跨度时会不收敛。
    """
    if len(flows) < 2:
        return None
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    if max((d - flows[0][0]).days for d, _ in flows) <= 0:
        return None

    rate = 0.1
    for _ in range(60):
        f = _npv(rate, flows)
        d = _npv_deriv(rate, flows)
        if abs(d) < 1e-12:
            break
        nxt = rate - f / d
        if nxt <= -0.999999:
            nxt = (rate - 0.999999) / 2.0
        if abs(nxt - rate) < 1e-10:
            return nxt * 100.0 if abs(_npv(nxt, flows)) < 1e-6 else None
        rate = nxt

    lo, hi = -0.999999, 10.0
    f_lo, f_hi = _npv(lo, flows), _npv(hi, flows)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = _npv(mid, flows)
        if abs(f_mid) < 1e-9:
            return mid * 100.0
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0 * 100.0


def money_weighted(trades: List[Dict[str, Any]], terminal_value: Any,
                   as_of: Optional[datetime.date] = None) -> Optional[float]:
    """账户资金加权收益率：买入流出、卖出流入、期末权益为终值。

    与净值曲线的「时间加权」并列——两者回答不同问题：
    时间加权看策略本身，资金加权看「我投入的钱实际赚了多少」。
    只统计**已记账**流水；手工直接填的持仓不在流水中，结果可能偏低。
    """
    pairs: List[Tuple[str, float]] = []
    for record in trades or []:
        if not isinstance(record, dict):
            continue
        side = str(record.get("side") or "").lower()
        if side not in ("buy", "sell"):
            continue
        pairs.append((_trade_day(record), _cash_effect(record)))
    flows = _merge_flows(pairs)
    terminal = _f(terminal_value) or 0.0
    if terminal > 0:
        flows.append(((as_of or datetime.date.today()), terminal))
    return xirr(flows)


class PerformanceService:
    def analyze(self, days: int = 180, benchmark: str = BENCHMARK_CODE) -> Dict[str, Any]:
        from .stock_service import stock_service

        uid = resolve_user_id()
        warnings: List[str] = []
        try:
            days = max(20, min(1095, int(days)))
        except (TypeError, ValueError):
            days = 180

        trades = store.list_trades(uid, limit=100000)
        trades.sort(key=lambda r: (_trade_day(r), str(r.get("created_at") or "")))
        favorites = stock_service.get_favorites_list(force_refresh=False, include_quotes=False) or []
        settings = store.get_settings(uid)

        today = datetime.date.today()
        start = (today - datetime.timedelta(days=days)).isoformat()

        # 现金：优先用账户快照（与页面口径一致），没有就退化成设置里的可用资金
        cash_now = _f(settings.get("available_cash"))
        try:
            snap = stock_service.get_account_snapshot(use_cache=False) or {}
            if _f(snap.get("available_cash")) is not None:
                cash_now = _f(snap.get("available_cash"))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"账户快照获取失败，现金按设置值估算：{exc}")
        if cash_now is None:
            cash_now = 0.0
            if settings.get("available_cash") is None:
                warnings.append("未设置「可用资金」，现金按 0 计；到设置页填写后曲线更准确")

        # ---- 现金按日聚合（用于反向回推）
        cash_flow_by_day: Dict[str, float] = {}
        for record in trades:
            day = _trade_day(record)
            if not day:
                continue
            cash_flow_by_day[day] = cash_flow_by_day.get(day, 0.0) + _cash_effect(record)

        # ---- 持仓按日聚合
        delta_by_day: Dict[str, Dict[str, int]] = {}
        for record in trades:
            day = _trade_day(record)
            if not day:
                continue
            sec, delta = _shares_delta(record)
            if not sec or not delta:
                continue
            bucket = delta_by_day.setdefault(day, {})
            bucket[sec] = bucket.get(sec, 0) + delta

        # ---- 需要日线的标的：当前持仓 + 历史出现过的
        current_holdings = {
            item["secID"]: int(item.get("shares") or 0)
            for item in favorites
            if int(item.get("shares") or 0) > 0
        }
        involved = set(current_holdings)
        for bucket in delta_by_day.values():
            involved.update(bucket.keys())
        involved = [s for s in sorted(involved) if s]

        series: Dict[str, Dict[str, float]] = {}

        def _load_bars(sec: str, force: bool = False) -> List[Dict[str, Any]]:
            """拉日线。force=True 时走 *_refresh 绕过缓存（用于修正残缺数据）。"""
            caller = "performance_refresh" if force else "performance"
            return stock_service.get_k_line_data(sec, start, today.isoformat(), "d", caller=caller) or []

        for sec in involved[:MAX_POSITION_SERIES]:
            bars: List[Dict[str, Any]] = []
            try:
                bars = _load_bars(sec)
                # 主源（星耀）偶尔只回一两根就结束，这种残缺数据算出来的曲线是假的：
                # 直接判为不可用，绕缓存再试一次，仍不行就丢弃该标的并明确告警。
                if len(bars) < MIN_BARS:
                    logger.warning("绩效日线疑似残缺 sec=%s rows=%s，绕缓存重试", sec, len(bars))
                    bars = _load_bars(sec, force=True)
                if len(bars) < MIN_BARS:
                    warnings.append(f"{sec} 日线仅 {len(bars)} 根（疑似数据源残缺），该标的按成本价近似")
                    bars = bars if bars else []
            except Exception as exc:  # noqa: BLE001
                logger.warning("绩效日线失败 sec=%s error=%r", sec, exc)
                warnings.append(f"{sec} 日线获取失败，市值按成本价近似")
                continue
            mapping: Dict[str, float] = {}
            for bar in bars:
                day = str(bar.get("date") or "")[:10]
                close = _f(bar.get("close"))
                if day and close is not None:
                    mapping[day] = close
            if mapping:
                series[sec] = mapping

        if len(involved) > MAX_POSITION_SERIES:
            warnings.append(f"持仓超过 {MAX_POSITION_SERIES} 只，仅前 {MAX_POSITION_SERIES} 只计入市值曲线")

        # ---- 时间轴：所有出现过的交易日
        timeline = sorted({day for mapping in series.values() for day in mapping.keys() if day >= start})
        if not timeline:
            return {
                "ok": False,
                "reason": "no_data",
                "message": "窗口内没有可用的日线数据，无法计算净值曲线",
                "warnings": warnings,
            }
        if len(timeline) < MIN_DAYS:
            return {
                "ok": False,
                "reason": "insufficient_series",
                "message": (
                    f"窗口内只取到 {len(timeline)} 个交易日（至少需要 {MIN_DAYS} 个），"
                    "日线数据源可能暂时异常，请稍后重试或切换行情源"
                ),
                "warnings": warnings,
            }

        # 基准
        bench_series: Dict[str, float] = {}
        try:
            bench_bars = stock_service.get_k_line_data(benchmark or BENCHMARK_CODE, start, today.isoformat(), "d", caller="performance_benchmark") or []
            for bar in bench_bars:
                day = str(bar.get("date") or "")[:10]
                close = _f(bar.get("close"))
                if day and close is not None:
                    bench_series[day] = close
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"基准 {BENCHMARK_NAME} 日线获取失败：{exc}")

        def close_at(sec: str, day: str, fallback: float) -> float:
            """当天没有收盘价就沿用最近一个有效收盘价（停牌不该让市值瞬间归零）。"""
            mapping = series.get(sec) or {}
            if day in mapping:
                return mapping[day]
            candidates = [d for d in mapping if d <= day]
            return mapping[max(candidates)] if candidates else fallback

        cost_map = {item["secID"]: _f(item.get("cost")) or 0.0 for item in favorites}

        # ---- 持仓口径：以「当前持仓」为锚点反推历史
        #
        # 为什么不是纯粹正向重放流水：很多用户是直接手填持仓（或只从某天开始记账），
        # 流水不完整。正向重放会让窗口早段市值直接归零，曲线看起来像"钱消失了"。
        # 以当前持仓为基准、用流水把窗口内的变动倒推回去，末端必然等于真实持仓，
        # 中间也不会出现凭空跳变。
        total_delta: Dict[str, int] = {}
        for bucket in delta_by_day.values():
            for sec, delta in bucket.items():
                total_delta[sec] = total_delta.get(sec, 0) + delta

        flow_days = sorted(delta_by_day.keys())
        flow_idx = 0
        applied_delta: Dict[str, int] = {}
        positions: Dict[str, int] = dict(current_holdings)

        points: List[Dict[str, Any]] = []
        equities: List[float] = []
        prev_equity: Optional[float] = None
        daily_returns: List[float] = []

        bench_base: Optional[float] = None
        for day in timeline:
            while flow_idx < len(flow_days) and flow_days[flow_idx] <= day:
                for sec, delta in (delta_by_day.get(flow_days[flow_idx]) or {}).items():
                    applied_delta[sec] = applied_delta.get(sec, 0) + delta
                flow_idx += 1

            positions = {}
            for sec in set(current_holdings) | set(total_delta) | set(applied_delta):
                remaining = total_delta.get(sec, 0) - applied_delta.get(sec, 0)  # day 之后还会发生的变动
                shares = int(current_holdings.get(sec, 0)) - remaining
                if shares > 0:
                    positions[sec] = shares

            market_value = 0.0
            for sec, shares in positions.items():
                market_value += shares * close_at(sec, day, cost_map.get(sec, 0.0))

            # 现金 = 今天现金 - (今天之后发生的现金流)
            cash_after = sum(amount for d, amount in cash_flow_by_day.items() if d > day)
            cash = float(cash_now) - cash_after
            equity = round(cash + market_value, 2)
            equities.append(equity)

            if prev_equity and prev_equity > 0:
                daily_returns.append(equity / prev_equity - 1.0)
            prev_equity = equity

            if bench_series:
                bench_close = bench_series.get(day)
                if bench_close is None:
                    candidates = [d for d in bench_series if d <= day]
                    bench_close = bench_series[max(candidates)] if candidates else None
                if bench_close is not None:
                    if bench_base is None:
                        bench_base = bench_close
                    bench_value = round(bench_close / bench_base, 6) if bench_base else 1.0
                else:
                    bench_value = None
            else:
                bench_value = None

            points.append({"date": day, "equity": equity, "benchmark": bench_value})

        # 回撤序列
        peak = None
        for point in points:
            value = point["equity"]
            peak = value if peak is None or value > peak else peak
            point["drawdown"] = round((peak - value) / peak * 100.0, 2) if peak else 0.0

        equity_start = points[0]["equity"] if points else 0.0
        equity_now = points[-1]["equity"] if points else 0.0
        total_return = round((equity_now / equity_start - 1.0) * 100.0, 2) if equity_start > 0 else None
        max_dd, current_dd = _max_drawdown(equities)

        # 年化与波动（日收益）
        annualized = None
        volatility = None
        sharpe = None
        if len(daily_returns) >= 5:
            mean = sum(daily_returns) / len(daily_returns)
            variance = sum((r - mean) ** 2 for r in daily_returns) / max(1, len(daily_returns) - 1)
            std = math.sqrt(variance)
            volatility = round(std * math.sqrt(252) * 100.0, 2)
            if equity_start > 0:
                days_span = max(1, len(points))
                annualized = round(((equity_now / equity_start) ** (252.0 / days_span) - 1.0) * 100.0, 2)
            if std > 0:
                sharpe = round((mean * 252) / (std * math.sqrt(252)), 2)

        # 基准收益
        bench_return = None
        if bench_series and points and points[-1].get("benchmark"):
            bench_return = round((points[-1]["benchmark"] - 1.0) * 100.0, 2)
        excess = round(total_return - bench_return, 2) if total_return is not None and bench_return is not None else None

        # 交易胜率
        sells = [
            _f(r.get("realized_pnl"))
            for r in trades
            if str(r.get("side") or "") == "sell" and _f(r.get("realized_pnl")) is not None
        ]
        wins = [v for v in sells if v > 0]
        losses = [v for v in sells if v < 0]
        win_rate = round(len(wins) / len(sells) * 100.0, 1) if sells else None
        profit_factor = None
        if wins and losses:
            profit_factor = round(sum(wins) / abs(sum(losses)), 2)
        elif wins and not losses:
            profit_factor = None

        # 当前持仓明细
        position_rows: List[Dict[str, Any]] = []
        for sec, shares in sorted(positions.items()):
            if shares <= 0:
                continue
            cost = cost_map.get(sec, 0.0)
            last = close_at(sec, points[-1]["date"] if points else "", cost)
            market_value = round(shares * last, 2)
            pnl = round((last - cost) * shares, 2) if cost else None
            digits = "".join(ch for ch in sec if ch.isdigit()) or sec
            display_name = stock_service._get_cached_stock_name(digits) or sec
            position_rows.append(
                {
                    "secID": sec,
                    "name": display_name,
                    "shares": shares,
                    "cost": round(cost, 3),
                    "price": round(last, 3),
                    "market_value": market_value,
                    "pnl": pnl,
                    "pnl_pct": round((last / cost - 1.0) * 100.0, 2) if cost else None,
                }
            )

        realized = round(sum(sells), 2) if sells else None
        fees = round(sum((_f(r.get("fee")) or 0.0) for r in trades), 2)
        # 资金加权：全历史已记账流水 + 期末权益。与上面的时间加权并列，口径不同不冲突。
        xirr_value = money_weighted(trades, equity_now, today)

        return {
            "ok": True,
            "range": {"start": points[0]["date"] if points else start, "end": points[-1]["date"] if points else today.isoformat(), "days": days},
            "summary": {
                "equity_now": equity_now,
                "equity_start": equity_start,
                "total_return_pct": total_return,
                "annualized_pct": annualized,
                "xirr_pct": round(xirr_value, 2) if xirr_value is not None else None,
                "xirr_method": "资金加权：全历史已记账流水 + 期末权益",
                "xirr_note": "与「年化收益」口径不同：年化按净值曲线，XIRR 按实际投入资金；手工填的持仓不在流水中",
                "max_drawdown_pct": max_dd,
                "current_drawdown_pct": current_dd,
                "volatility_pct": volatility,
                "sharpe": sharpe,
                "benchmark_return_pct": bench_return,
                "excess_pct": excess,
                "win_rate_pct": win_rate,
                "profit_factor": profit_factor,
                "sell_count": len(sells),
                "realized_pnl": realized,
                "total_fee": fees,
                "trade_count": len(trades),
                "cash": round(float(cash_now), 2),
            },
            "points": points,
            "positions": position_rows,
            "benchmark": {"code": BENCHMARK_CODE, "name": BENCHMARK_NAME, "available": bool(bench_series)},
            "warnings": warnings,
        }


performance_service = PerformanceService()
