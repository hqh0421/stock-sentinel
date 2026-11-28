"""组合级风控与相对位置（纯计算，不联网）。

对应 `docs/量化方法论与监控系统设计原则.md` §4 里排期但未落地的三件事：
- 相对位置指标：N 日区间位置 / 距 MA200 偏离，让「低位/高位」有可核对的数字；
- 波动率目标：按已实现波动率反推建议仓位；
- 组合回撤护栏：权益曲线回撤超阈值时给出降仓动作。

所有阈值来自 `config/decision_rules.json`（`risk` 段），不在这里硬编码。
本模块被监控信号、回测页与 Agent 工具共用，保证口径一致。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from ..core import decision_rules


def _f(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def relative_position(kline: List[Dict[str, Any]], window: Optional[int] = None,
                      ma_long: Optional[int] = None) -> Dict[str, Any]:
    """N 日区间位置 + 距长期均线偏离。

    range_position: 0 = 区间最低，1 = 区间最高；这是"位置"而不是"估值"。
    ma_deviation_pct: 现价相对 MA{ma_long} 的偏离（负数 = 在均线下方）。
    """
    rules = decision_rules.risk_rules()
    window = int(window or rules.get("relative_position_window", 250))
    ma_long = int(ma_long or rules.get("ma_long", 200))
    closes = [_f(bar.get("close")) for bar in (kline or [])]
    closes = [v for v in closes if v is not None]
    if not closes:
        return {"available": False, "reason": "no_kline"}
    last = closes[-1]
    sample = closes[-min(len(closes), max(2, window)):]
    low, high = min(sample), max(sample)
    span = high - low
    pos = (last - low) / span if span > 0 else 0.5
    ma_sample = closes[-min(len(closes), ma_long):]
    ma = sum(ma_sample) / len(ma_sample) if ma_sample else None
    return {
        "available": True,
        "range_position": round(max(0.0, min(1.0, pos)), 4),
        "range_low": round(low, 4),
        "range_high": round(high, 4),
        "window": len(sample),
        "ma_long": ma_long,
        "ma_long_value": round(ma, 4) if ma else None,
        "ma_deviation_pct": round((last / ma - 1.0) * 100.0, 2) if ma else None,
        "label": "区间低位" if pos <= 0.25 else ("区间高位" if pos >= 0.75 else "区间中部"),
    }


def realized_volatility(kline: List[Dict[str, Any]], lookback: Optional[int] = None) -> Optional[float]:
    """日收益标准差年化（按 252 交易日）。"""
    rules = decision_rules.risk_rules()
    lookback = int(lookback or rules.get("vol_lookback", 20))
    closes = [_f(bar.get("close")) for bar in (kline or [])]
    closes = [v for v in closes if v is not None]
    if len(closes) < 3:
        return None
    returns = []
    sample = closes[-(lookback + 1):]
    for i in range(1, len(sample)):
        if sample[i - 1] > 0:
            returns.append(sample[i] / sample[i - 1] - 1.0)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / len(returns)
    trading_days = float(decision_rules.get("fees.trading_days_per_year", 252))
    return round(math.sqrt(var) * math.sqrt(trading_days), 6)


def vol_target_position(vol: Optional[float]) -> Dict[str, Any]:
    """波动率目标：波动越大，建议仓位越低（上限 100%）。"""
    rules = decision_rules.risk_rules()
    target = float(rules.get("vol_target_annual", 0.2))
    if not vol or vol <= 0:
        return {"available": False, "reason": "vol_unavailable"}
    suggested = max(0.0, min(1.0, target / vol))
    return {
        "available": True,
        "target_vol_annual": target,
        "realized_vol_annual": round(vol, 4),
        "suggested_position_pct": round(suggested * 100.0, 2),
        "note": "高波动时自动压低仓位上限，这是波动率目标而非择时信号",
    }


def portfolio_drawdown_guard(equity: List[float], max_dd_pct: Optional[float] = None) -> Dict[str, Any]:
    """权益曲线回撤护栏：超阈值给降仓建议。"""
    rules = decision_rules.risk_rules()
    limit = float(max_dd_pct if max_dd_pct is not None else rules.get("max_portfolio_drawdown_pct", 15.0))
    values = [float(v) for v in (equity or []) if v is not None]
    if len(values) < 2:
        return {"available": False, "reason": "insufficient_equity"}
    peak, mdd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100.0)
    breached = mdd >= limit
    return {
        "available": True,
        "current_max_drawdown_pct": round(mdd, 2),
        "limit_pct": limit,
        "breached": breached,
        "action": f"回撤已达 {mdd:.1f}%，超过 {limit:.0f}% 护栏：暂停新增买入、先减高位仓" if breached
                  else "回撤在护栏内，按纪律正常执行",
    }


def price_drawdown_guard(kline: List[Dict[str, Any]],
                         limit_pct: Optional[float] = None) -> Dict[str, Any]:
    """单票距区间峰值的回撤护栏（组合护栏在逐票监控里的可核对代理）。"""
    rules = decision_rules.risk_rules()
    limit = float(limit_pct if limit_pct is not None else rules.get("max_portfolio_drawdown_pct", 15.0))
    closes = [_f(bar.get("close")) for bar in (kline or [])]
    closes = [v for v in closes if v is not None]
    min_bars = int(rules.get("min_bars_for_guard", 20))
    if len(closes) < min_bars:
        return {"available": False, "reason": "insufficient_bars", "min_bars": min_bars}
    peak = max(closes)
    dd = (peak - closes[-1]) / peak * 100.0 if peak > 0 else 0.0
    return {
        "available": True,
        "drawdown_from_peak_pct": round(dd, 2),
        "limit_pct": limit,
        "breached": dd >= limit,
        "action": "已越过回撤护栏，暂停新增买入" if dd >= limit else "回撤在护栏内",
    }


def concentration(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """集中度与行业暴露检查（输入为持仓市值列表）。"""
    rules = decision_rules.risk_rules()
    single_limit = float(rules.get("max_single_weight_pct", 35.0))
    industry_limit = float(rules.get("max_industry_weight_pct", 50.0))
    rows = [p for p in (positions or []) if isinstance(p, dict)]
    total = sum(float(p.get("market_value") or 0.0) for p in rows)
    if total <= 0:
        return {"available": False, "reason": "no_positions"}
    by_industry: Dict[str, float] = {}
    breaches: List[str] = []
    top = None
    for p in rows:
        weight = float(p.get("market_value") or 0.0) / total * 100.0
        if top is None or weight > top["weight_pct"]:
            top = {"name": p.get("name") or p.get("code") or "-", "weight_pct": round(weight, 2)}
        if weight > single_limit:
            breaches.append(f"{p.get('name') or p.get('code') or '-'} 单票 {weight:.1f}% 超 {single_limit:.0f}%")
        industry = str(p.get("industry") or "未分类")
        by_industry[industry] = by_industry.get(industry, 0.0) + weight
    for industry, weight in by_industry.items():
        if weight > industry_limit:
            breaches.append(f"{industry} 合计 {weight:.1f}% 超 {industry_limit:.0f}%")
    return {
        "available": True,
        "top_position": top,
        "industry_weights": {k: round(v, 2) for k, v in by_industry.items()},
        "breaches": breaches,
        "ok": not breaches,
    }


def assess(kline: List[Dict[str, Any]], equity: Optional[List[float]] = None,
           positions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """一次算齐所有风控指标，供 API 与监控信号复用。"""
    rules = decision_rules.risk_rules()
    pos = relative_position(kline)
    vol = realized_volatility(kline)
    return {
        "relative_position": pos,
        "vol_target": vol_target_position(vol),
        "drawdown_guard": portfolio_drawdown_guard(equity or []),
        "concentration": concentration(positions or []),
        "rules": {
            "vol_target_enabled": bool(decision_rules.get("monitor.vol_target_enabled", False)),
            "drawdown_guard_enabled": bool(decision_rules.get("monitor.portfolio_drawdown_guard_enabled", False)),
            "max_single_weight_pct": rules.get("max_single_weight_pct"),
            "max_industry_weight_pct": rules.get("max_industry_weight_pct"),
        },
    }


# ---------------------------------------------------------------------------
# 风控开关（界面可切换）
# 只放行这两个布尔：其余 monitor 字段（窗口、冷却等）仍只走规则文件手工编辑，
# 避免前端一次写入把整套监控口径改掉。
# ---------------------------------------------------------------------------

MONITOR_SWITCHES = ("vol_target_enabled", "portfolio_drawdown_guard_enabled",
                    "pool_drift_alert_enabled")


def save_monitor_switches(patch: Dict[str, Any]) -> Dict[str, Any]:
    """校验并落盘两个风控开关；不合法抛 ValueError（由 router 转 400）。

    复用 decision_rules.update（原子落盘 + .bak + 缓存刷新），不另写一套写文件逻辑。
    """
    if not isinstance(patch, dict) or not patch:
        raise ValueError("至少提供一个开关")
    unknown = sorted(set(patch) - set(MONITOR_SWITCHES))
    if unknown:
        raise ValueError(f"不支持的风控开关：{', '.join(unknown)}")
    cleaned: Dict[str, Any] = {}
    for key, value in patch.items():
        if not isinstance(value, bool):
            raise ValueError(f"{key} 必须是 true / false")
        cleaned[key] = value
    decision_rules.update({"monitor": cleaned})
    return decision_rules.monitor_rules()
