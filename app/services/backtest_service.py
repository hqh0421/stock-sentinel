"""日线策略回测引擎。

设计要点（与原型 docs/product/prototypes/stock-monitor-redesign.html 的回测页对齐）：
- 信号在 bar t 收盘后用表达式判定，成交放在 bar t+1 开盘价，避免使用当日收盘价的未来信息。
- 支持手续费（每边佣金，最低 5 元）、卖出印花税、滑点、仓位比例。
- 只做多、单笔持仓、100 股整数倍。
- 输出总收益/年化/最大回撤/夏普/胜率/交易次数，以及与基准指数（沪深300 等）的对比。

已知限制：行情源（雪球）日线只返回最近约 284 根，若请求区间早于数据起点会做截断并在
warnings 中提示；不处理涨跌停/停牌（缺数据自然跳过），不做分红除权复权之外的调整。
"""

from __future__ import annotations

import datetime
import logging
import math
from typing import Any, Dict, List, Optional

from ..core import decision_rules
from ..core.expr import check_expr, eval_signal_expr, normalize_expr as normalize_strategy_expr

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = int(decision_rules.get("fees.trading_days_per_year", 252))
LOT_SIZE = int(decision_rules.get("fees.lot_size", 100))
MIN_COMMISSION = float(decision_rules.get("fees.commission_min", 5.0))

# 费率口径已与交易台账统一：不再使用拍脑袋的「每边综合 0.00075」。
# 实际撮合费用一律由 decision_rules.calc_trade_fee() 计算（佣金+最低佣金+印花税+过户费）；
# DEFAULT_FEE_RATE 只是等效展示费率，供未显式传参时回填。
DEFAULT_FEE_RATE = decision_rules.effective_fee_rate()
DEFAULT_STAMP_TAX_RATE = float(decision_rules.get("fees.stamp_rate", 0.0005))
DEFAULT_SLIPPAGE_PCT = float(decision_rules.get("fees.slippage_pct", 0.001))

# 基准指数（雪球 symbol）
BENCHMARKS: Dict[str, Dict[str, str]] = {
    "hs300": {"code": "sh000300", "name": "沪深300"},
    "zz500": {"code": "sh000905", "name": "中证500"},
    "sh": {"code": "sh000001", "name": "上证指数"},
    "sz": {"code": "sz399001", "name": "深证成指"},
}


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _eval_expr(expr: str, series: List[Dict[str, Any]], idx: int) -> bool:
    """委托给 app.core.expr：与监控信号共用同一份安全求值实现，不再使用 eval。"""
    return eval_signal_expr(expr, series, idx)


def _max_drawdown_pct(equity: List[float]) -> float:
    peak = None
    mdd = 0.0
    for value in equity:
        if peak is None or value > peak:
            peak = value
        if peak and peak > 0:
            dd = (peak - value) / peak
            if dd > mdd:
                mdd = dd
    return mdd * 100.0


def _sharpe(equity: List[float]) -> Optional[float]:
    if len(equity) < 3:
        return None
    returns = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev and prev > 0:
            returns.append(equity[i] / prev - 1.0)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / len(returns)
    std = math.sqrt(var)
    if std <= 1e-12:
        return None
    return mean / std * math.sqrt(TRADING_DAYS_PER_YEAR)


def _annualized_pct(initial: float, final: float, days: int) -> Optional[float]:
    if not initial or initial <= 0 or days <= 0:
        return None
    if final <= 0:
        return -100.0
    years = days / TRADING_DAYS_PER_YEAR
    if years <= 0:
        return None
    try:
        return ((final / initial) ** (1.0 / years) - 1.0) * 100.0
    except Exception:
        return None


class BacktestService:
    """基于表达式策略的日线回测。"""

    def run(
        self,
        code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        capital: float = 100000.0,
        entry_expr: str = "",
        exit_expr: str = "",
        benchmark: str = "hs300",
        fee_rate: float = DEFAULT_FEE_RATE,
        stamp_tax_rate: float = DEFAULT_STAMP_TAX_RATE,
        slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
        position_pct: float = 1.0,
        strategy_name: str = "",
    ) -> Dict[str, Any]:
        from .stock_service import stock_service  # 延迟导入避免循环依赖

        warnings: List[str] = []
        try:
            capital = float(capital or 0)
        except Exception:
            capital = 0.0
        if capital <= 0:
            return {"ok": False, "error": "initial_capital_invalid", "message": "初始资金必须大于 0"}

        position_pct = max(0.05, min(1.0, float(position_pct or 1.0)))

        today = datetime.datetime.now().strftime("%Y-%m-%d")
        end_date = (end_date or today).strip()
        start_date = (start_date or (datetime.datetime.now() - datetime.timedelta(days=365)).strftime("%Y-%m-%d")).strip()
        if start_date > end_date:
            start_date, end_date = end_date, start_date

        try:
            series = stock_service.get_k_line_data(code, start_date, end_date, "d", caller="backtest")
        except Exception as exc:  # 行情异常如实返回
            logger.warning("backtest_kline_failed code=%s error=%r", code, exc)
            return {"ok": False, "error": "kline_failed", "message": f"获取K线失败：{exc}"}

        series = [bar for bar in (series or []) if _to_float(bar.get("close")) is not None]
        if len(series) < 20:
            return {
                "ok": False,
                "error": "insufficient_data",
                "message": f"K线数据不足（{len(series)} 根），至少需要 20 根日线",
            }

        data_start = str(series[0].get("date") or "")
        data_end = str(series[-1].get("date") or "")
        if data_start > start_date:
            warnings.append(f"行情源仅提供 {data_start} 之后的日线，回测区间已从 {start_date} 截断至 {data_start}")

        # 指标（MA/MACD/RSI）已在整段序列上算好，因此信号在全序列求值、只在请求区间内成交
        window_indices = [i for i, bar in enumerate(series) if start_date <= str(bar.get("date") or "") <= end_date]
        if len(window_indices) < 10:
            return {
                "ok": False,
                "error": "insufficient_range",
                "message": f"区间 {start_date} ~ {end_date} 内只有 {len(window_indices)} 根日线，至少需要 10 根",
            }
        first_idx, last_idx = window_indices[0], window_indices[-1]
        actual_start = str(series[first_idx].get("date") or "")
        actual_end = str(series[last_idx].get("date") or "")

        entry_expr = normalize_strategy_expr(entry_expr or "")
        exit_expr = normalize_strategy_expr(exit_expr or "")
        for label, text in (("入场", entry_expr), ("出场", exit_expr)):
            valid, reason = check_expr(text)
            if not valid:
                return {"ok": False, "error": "invalid_expression",
                        "message": f"{label}表达式不合法：{reason}"}

        if not entry_expr:
            warnings.append("未提供入场条件，回测不会产生任何交易")

        # ---------- 逐 bar 撮合 ----------
        # fee_rate / stamp_tax_rate 仅保留在返回体里做展示与兼容；
        # 真实撮合费用统一走 decision_rules.calc_trade_fee（与交易台账同口径）。
        fee_rate = max(0.0, float(fee_rate or 0.0))
        stamp_tax_rate = max(0.0, float(stamp_tax_rate or 0.0))
        slippage_pct = max(0.0, float(slippage_pct or 0.0))

        cash = capital
        shares = 0
        entry_cost = 0.0
        entry_date = ""
        open_trade: Optional[Dict[str, Any]] = None
        trades: List[Dict[str, Any]] = []
        round_trips: List[float] = []
        hold_days: List[int] = []
        equity_points: List[Dict[str, Any]] = []
        equity_values: List[float] = []

        pending: Optional[str] = None  # "buy" / "sell"

        for idx in range(first_idx, last_idx + 1):
            bar = series[idx]
            bar_date = str(bar.get("date") or "")
            open_price = _to_float(bar.get("open"))
            close_price = _to_float(bar.get("close"))

            # 1) 先执行上一根 bar 产生的信号（在今日开盘成交）
            if pending and open_price and open_price > 0:
                if pending == "buy" and shares == 0:
                    fill = open_price * (1.0 + slippage_pct)
                    budget = cash * position_pct
                    lots = int(budget / (fill * LOT_SIZE)) if fill > 0 else 0
                    while lots > 0:
                        qty = lots * LOT_SIZE
                        gross = qty * fill
                        fee = decision_rules.calc_trade_fee("buy", gross)["total"]
                        if gross + fee <= cash + 1e-9:
                            break
                        lots -= 1
                    if lots > 0:
                        qty = lots * LOT_SIZE
                        gross = qty * fill
                        fee = decision_rules.calc_trade_fee("buy", gross)["total"]
                        cash -= gross + fee
                        shares = qty
                        entry_cost = (gross + fee) / qty
                        entry_date = bar_date
                        open_trade = {
                            "entry_date": bar_date,
                            "entry_price": round(fill, 4),
                            "shares": qty,
                        }
                        trades.append({
                            "date": bar_date,
                            "side": "buy",
                            "price": round(fill, 4),
                            "shares": qty,
                            "fee": round(fee, 2),
                            "amount": round(gross, 2),
                            "pnl_pct": None,
                            "cash_after": round(cash, 2),
                        })
                elif pending == "sell" and shares > 0:
                    fill = open_price * (1.0 - slippage_pct)
                    gross = shares * fill
                    fee = decision_rules.calc_trade_fee("sell", gross)["total"]
                    cash += gross - fee
                    pnl_pct = ((gross - fee) / (entry_cost * shares) - 1.0) * 100.0 if entry_cost and shares else 0.0
                    round_trips.append(pnl_pct)
                    try:
                        hold_days.append(
                            (datetime.date.fromisoformat(bar_date) - datetime.date.fromisoformat(entry_date)).days
                            if entry_date else 0
                        )
                    except Exception:
                        hold_days.append(0)
                    trades.append({
                        "date": bar_date,
                        "side": "sell",
                        "price": round(fill, 4),
                        "shares": shares,
                        "fee": round(fee, 2),
                        "amount": round(gross, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "cash_after": round(cash, 2),
                    })
                    open_trade = None
                    shares = 0
                    entry_cost = 0.0
                    entry_date = ""
                pending = None

            # 2) 用今日收盘后的指标判定信号，留到下一交易日开盘执行
            if idx < last_idx:
                if shares == 0 and entry_expr and _eval_expr(entry_expr, series, idx):
                    pending = "buy"
                elif shares > 0 and exit_expr and _eval_expr(exit_expr, series, idx):
                    pending = "sell"

            # 3) 记录当日收盘净值（必须是当天真实持仓，不能事后回算）
            mark_price = close_price or open_price or 0.0
            equity_values.append(cash + shares * mark_price)
            equity_points.append({
                "date": bar_date,
                "equity": round(cash + shares * mark_price, 2),
            })

        final_capital = equity_values[-1] if equity_values else capital

        # 归一化净值（以初始资金为 1.0），便于与基准同图对比
        base = capital or 1.0
        for point in equity_points:
            point["strategy"] = round(point["equity"] / base, 6) if base else 1.0
            # 含再投资的复利净值：每次平仓后的资金全额滚入下一笔，
            # 与「每笔固定本金、收益不复投」的 simple_sum 相对照。
            point["compound"] = point["strategy"]

        total_return_pct = (final_capital / capital - 1.0) * 100.0 if capital else 0.0
        days = len(equity_values)
        annual_pct = _annualized_pct(capital, final_capital, days)
        mdd_pct = _max_drawdown_pct(equity_values)
        sharpe = _sharpe(equity_values)
        win_count = sum(1 for p in round_trips if p > 0)
        win_rate = (win_count / len(round_trips) * 100.0) if round_trips else None

        first_close = _to_float(series[first_idx].get("close")) or 0.0
        last_close = _to_float(series[last_idx].get("close")) or 0.0
        hold_return_pct = ((last_close / first_close - 1.0) * 100.0) if first_close else None

        # ---------- 基准 ----------
        benchmark_payload = {"key": benchmark or "none", "name": "-", "available": False, "return_pct": None}
        bench_key = (benchmark or "none").strip().lower()
        if bench_key in BENCHMARKS and bench_key != "none":
            meta = BENCHMARKS[bench_key]
            benchmark_payload.update({"key": bench_key, "code": meta["code"], "name": meta["name"]})
            try:
                bench_series = stock_service.get_k_line_data(
                    meta["code"], actual_start, actual_end, "d", caller="backtest_benchmark"
                )
                bench_map = {
                    str(b.get("date") or ""): _to_float(b.get("close"))
                    for b in (bench_series or [])
                }
                base_bench = None
                aligned = 0
                for point in equity_points:
                    value = bench_map.get(point["date"])
                    if value is None:
                        continue
                    if base_bench is None:
                        base_bench = value
                    point["benchmark"] = round(value / base_bench, 6) if base_bench else 1.0
                    aligned += 1
                if base_bench and aligned >= 5:
                    last_bench = None
                    for point in reversed(equity_points):
                        if point.get("benchmark") is not None:
                            last_bench = point["benchmark"]
                            break
                    if last_bench is not None:
                        benchmark_payload.update({
                            "available": True,
                            "return_pct": round((last_bench - 1.0) * 100.0, 2),
                            "aligned_days": aligned,
                        })
                else:
                    warnings.append(f"基准 {meta['name']} 与个股交易日重叠不足，未画对比曲线")
            except Exception as exc:
                logger.warning("backtest_benchmark_failed key=%s error=%r", bench_key, exc)
                warnings.append(f"基准 {meta['name']} 数据获取失败，未做对比")

        excess_pct = None
        if benchmark_payload.get("return_pct") is not None:
            excess_pct = round(total_return_pct - benchmark_payload["return_pct"], 2)

        # 兜底：无基准时曲线仍可画（benchmark 缺省等于策略自身，前端会隐藏该线）
        for point in equity_points:
            if point.get("benchmark") is None:
                point["benchmark"] = point.get("strategy", 1.0)

        stock_name = self._resolve_name(code)

        logger.info(
            "backtest_done code=%s range=%s~%s bars=%s trades=%s return=%.2f%% mdd=%.2f%%",
            code, actual_start, actual_end, days, len(round_trips), total_return_pct, mdd_pct,
        )

        return {
            "ok": True,
            "code": code,
            "name": stock_name or code,
            "strategy_name": strategy_name or "",
            "entry_expr": entry_expr,
            "exit_expr": exit_expr,
            "requested_start": start_date,
            "requested_end": end_date,
            "start_date": actual_start,
            "end_date": actual_end,
            "data_start": data_start,
            "data_end": data_end,
            "truncated": data_start > start_date,
            "bars": days,
            "initial_capital": round(capital, 2),
            "final_capital": round(final_capital, 2),
            "return_pct": round(total_return_pct, 2),
            "hold_return_pct": round(hold_return_pct, 2) if hold_return_pct is not None else None,
            "metrics": {
                "total_return_pct": round(total_return_pct, 2),
                "annual_return_pct": round(annual_pct, 2) if annual_pct is not None else None,
                "max_drawdown_pct": round(mdd_pct, 2),
                "sharpe": round(sharpe, 2) if sharpe is not None else None,
                "win_rate_pct": round(win_rate, 2) if win_rate is not None else None,
                "trade_count": len(round_trips),
                "closed_trades": len(round_trips),
                "avg_hold_days": round(sum(hold_days) / len(hold_days), 1) if hold_days else None,
            },
            "benchmark": benchmark_payload,
            "excess_pct": excess_pct,
            "fees": {
                "fee_rate": fee_rate,
                "stamp_tax_rate": stamp_tax_rate,
                "slippage_pct": slippage_pct,
                "position_pct": position_pct,
                "fee_model": "ledger_aligned",
                "fee_source": "config/decision_rules.json",
            },
            "compound": {
                "reinvest": True,
                "curve_field": "compound",
                "compound_return_pct": round(total_return_pct, 2),
                "simple_sum_pct": round(sum(round_trips), 2),
                "note": "compound 为含再投资复利净值；simple_sum 为各笔收益率直接相加（不复投）的对照",
            },
            "equity": equity_points,
            "trades": trades,
            "open_position": open_trade,
            "warnings": warnings,
        }

    def _resolve_name(self, code: str) -> Optional[str]:
        try:
            from .stock_service import stock_service

            normalized = stock_service._normalize_stock_code(code)  # noqa: SLF001
            sec_id = f"{normalized['digits']}.{normalized['market'].upper()}"
            for item in getattr(stock_service, "_favorite_cache", []) or []:
                if item.get("secID") == sec_id and item.get("name"):
                    return str(item["name"])
            return None
        except Exception:
            return None


backtest_service = BacktestService()
