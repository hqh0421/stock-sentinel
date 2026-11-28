"""卖出目标价计算（多因子合成）。

设计原则
--------
1. **纯计算、不联网**：K 线、PE/PB、财报、行业周期、大盘周期全部由调用方取好传进来。
2. **每个价位都有依据**：目标价锚定到「估值上沿 / 技术压力位 / 波动率递推」之一，
   并在 `basis` 里写明，Agent 可以直接引用、用户可以直接质疑。
3. **估值是天花板，技术是台阶**：技术位决定"先撞到哪"，估值决定"最多值多少"。
   当估值上限低于现价时，宁可如实说"上面没空间、反弹就减"，也不为了好看硬给一个高价。
4. **风格只影响参数，不影响纪律**：长线/中长线/波段三档差别在止损容忍度、目标距离
   与估值区间宽度。短线属系统禁令，统一按波段口径处理并提示。

档位含义（供 Agent 与前端复用）：
    T1 第一目标 —— 到位先减一部分，锁定利润、降低回撤
    T2 主目标   —— 核心卖出区，多数仓位在此了结
    T3 延伸目标 —— 仅在趋势/题材延续时才看
"""

from __future__ import annotations

import datetime

from ..core import decision_rules
import logging
from typing import Any, Dict, List, Optional, Tuple

from .sell_factors import (
    compute_factors,
    estimate_fair_value,
    moving_average,
)

logger = logging.getLogger("app.sell_target")

# 各风格的参数表。band 为估值区间宽度（0.18 = 中枢上下各 18%）。
_STYLE_PARAMS: Dict[str, Dict[str, Any]] = decision_rules.sell_style_params()

_LEVEL_NAMES = ("第一目标", "主目标", "延伸目标")


def normalize_style(horizon: Any) -> str:
    """把任意 horizon 值归一到三种可计算风格之一。short 按波段处理（系统禁短线）。"""
    text = str(horizon or "").strip().lower()
    if text in ("long", "长线"):
        return "long"
    if text in ("mid_long", "mid", "medium", "middle", "中长线"):
        return "mid_long"
    if text in ("swing", "band", "波段"):
        return "swing"
    if text in ("short", "短线", "超短"):
        # 系统级禁令：不出短线方案，退化为波段口径
        return "swing"
    return "long"


# ---------------------------------------------------------------------------
# 基础量
# ---------------------------------------------------------------------------

def _f(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return None
    if fv != fv:
        return None
    return fv


def _round_price(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 2)


def _atr(bars: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
    if len(bars) < 3:
        return None
    trs: List[float] = []
    for i in range(1, len(bars)):
        high = _f(bars[i].get("high"))
        low = _f(bars[i].get("low"))
        prev_close = _f(bars[i - 1].get("close"))
        if high is None or low is None:
            continue
        candidates = [high - low]
        if prev_close is not None:
            candidates.append(abs(high - prev_close))
            candidates.append(abs(low - prev_close))
        trs.append(max(candidates))
    if not trs:
        return None
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window)


def _range_high(bars: List[Dict[str, Any]], days: int) -> Optional[float]:
    highs = [_f(b.get("high")) for b in bars[-days:]]
    highs = [h for h in highs if h is not None]
    return max(highs) if highs else None


def _range_low(bars: List[Dict[str, Any]], days: int) -> Optional[float]:
    lows = [_f(b.get("low")) for b in bars[-days:]]
    lows = [l for l in lows if l is not None]
    return min(lows) if lows else None


def _vwap(bars: List[Dict[str, Any]], days: int = 60) -> Optional[float]:
    total_amount = 0.0
    total_volume = 0.0
    for bar in bars[-days:]:
        amount = _f(bar.get("amount"))
        volume = _f(bar.get("volume"))
        if amount and volume:
            total_amount += amount
            total_volume += volume
            continue
        close = _f(bar.get("close"))
        if close and volume:
            total_amount += close * volume
            total_volume += volume
    if total_volume <= 0:
        return None
    value = total_amount / total_volume
    if value <= 0 or value > 1e6:
        return None
    return value


# ---------------------------------------------------------------------------
# 技术参考位
# ---------------------------------------------------------------------------

def _collect_levels(
    bars: List[Dict[str, Any]],
    close: float,
    atr: float,
    params: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """收集现价上方的所有技术参考位，按价格升序、8‰ 内视为同一档去重。"""
    closes = [_f(b.get("close")) for b in bars]
    closes = [c for c in closes if c and c > 0]
    last = bars[-1]

    ma20 = _f(last.get("MA20")) or moving_average(closes, 20)
    ma60 = _f(last.get("MA60")) or moving_average(closes, 60)
    ma120 = moving_average(closes, 120)
    ma250 = moving_average(closes, 250)
    high_20, high_60, high_120, high_250 = (_range_high(bars, d) for d in (20, 60, 120, 250))
    low_20 = _range_low(bars, 20)
    vwap_60 = _vwap(bars, 60)
    vwap_250 = _vwap(bars, 250)

    refs = {
        "ma20": _round_price(ma20), "ma60": _round_price(ma60),
        "ma120": _round_price(ma120), "ma250": _round_price(ma250),
        "high_20": _round_price(high_20), "high_60": _round_price(high_60),
        "high_120": _round_price(high_120), "high_250": _round_price(high_250),
        "low_20": _round_price(low_20),
        "vwap_60": _round_price(vwap_60), "vwap_250": _round_price(vwap_250),
    }

    raw: List[Dict[str, Any]] = []
    for label, value, kind, threshold in (
        ("MA20", ma20, "均线", 1.012),
        ("MA60", ma60, "均线", 1.012),
        ("MA120", ma120, "均线", 1.012),
        ("MA250 年线", ma250, "均线", 1.012),
        ("20 日高点", high_20, "前高", 1.02),
        ("60 日高点", high_60, "前高", 1.02),
        ("120 日高点", high_120, "前高", 1.02),
        ("250 日高点", high_250, "前高", 1.02),
        ("60 日成交密集区", vwap_60, "筹码", 1.02),
        ("250 日成交密集区", vwap_250, "筹码", 1.02),
    ):
        # 只在明显高于现价时才算"目标位"：贴着现价的前高/均线只是盘中阻力，
        # 当成卖出目标会让 T1 变成 +1%，失去操作意义
        if value and value > close * threshold:
            raw.append({"price": float(value), "label": label, "kind": kind})

    for k in params["atr_targets"]:
        raw.append({
            "price": close + k * atr,
            "label": f"现价 +{k:.1f} 倍 ATR",
            "kind": "波动率",
        })

    raw.sort(key=lambda item: item["price"])
    merged: List[Dict[str, Any]] = []
    for item in raw:
        if merged and abs(item["price"] - merged[-1]["price"]) <= merged[-1]["price"] * 0.008:
            # 同一价格区间有多个依据 → 合并，保留更低（先撞到）的价格
            if item["label"] not in merged[-1]["label"]:
                merged[-1]["label"] += "／" + item["label"]
            continue
        merged.append(dict(item))
    return merged, refs


# ---------------------------------------------------------------------------
# 目标价阶梯
# ---------------------------------------------------------------------------

_LADDER_RATIOS: Dict[str, Tuple[float, float, float]] = {
    "long": (0.33, 0.66, 1.0),
    "mid_long": (0.35, 0.70, 1.0),
    "swing": (0.40, 0.75, 1.0),
}


def _build_targets(
    levels: List[Dict[str, Any]],
    close: float,
    cost: float,
    holding: bool,
    atr: float,
    fair: Dict[str, Any],
    factors: Dict[str, Any],
    shares: int,
    composite: float = 0.0,
    style: str = "long",
) -> Tuple[List[Dict[str, Any]], bool]:
    """把估值空间与技术位合成 T1/T2/T3，并给出每档减仓比例与股数。

    阶梯怎么来的
    ------------
    先把「现价 → 估值上沿」这段空间按风格切成三档（长线 33%/66%/100%），
    再把每一档**吸附**到附近的技术位（前高、均线、成交密集区）——
    因为估值说"该值 54"，但价格实际会先在前高 43 停下来。

    为什么不是"从最近的压力位往上数三档"：那样会出现「估值中枢 54、
    延伸目标却只有 44」的自相矛盾，长线用户会拿到一个明显过早的卖点。

    valuation_blocked=True 表示估值上沿已贴到现价（2% 以内）甚至低于现价：
    上方没有估值支撑的空间，只能给技术反抽位，并提示"反弹即减"。
    """
    v_high = _f(fair.get("high")) or close
    v_ceiling = _f(fair.get("ceiling")) or v_high
    theme = factors.get("theme") or {}
    heat = float(theme.get("heat") or 0.0)
    risk = float(theme.get("risk") or 0.0)

    valuation_blocked = v_high <= close * 1.02

    candidates: List[Dict[str, Any]] = [dict(item) for item in levels]
    candidates.append({
        "price": v_high,
        "label": f"估值上沿 {v_high:.2f}（{fair.get('low', 0):.2f}~{v_high:.2f} 区间顶）",
        "kind": "估值",
    })
    if v_ceiling > v_high * 1.01:
        candidates.append({
            "price": v_ceiling,
            "label": f"估值上限 + 题材溢价 {v_ceiling:.2f}",
            "kind": "情绪",
        })
    candidates.sort(key=lambda item: item["price"])

    min_gap = max(atr * 1.0, close * 0.012)

    if valuation_blocked:
        # —— 估值透支：只给技术反抽位，绝不为了好看往上画 ——
        upper_bound = max(v_ceiling, close * 1.03) * 1.02
        picked: List[Dict[str, Any]] = []
        for cand in candidates:
            if cand["price"] <= close * 1.008 or cand["price"] > upper_bound:
                continue
            if picked and cand["price"] < picked[-1]["price"] + min_gap:
                continue
            picked.append(cand)
            if len(picked) >= 2:
                break
        if not picked:
            picked.append({
                "price": close + max(atr * 1.2, close * 0.015),
                "label": "上方无有效压力位，按 1.2 倍 ATR 递推",
                "kind": "波动率",
            })
        budget = close + (v_ceiling - close) * 0.5
        if len(picked) == 1:
            nxt = min(picked[0]["price"] + max(atr * 1.8, close * 0.02), max(budget, close * 1.03))
            if nxt > picked[0]["price"] * 1.01:
                picked.append({
                    "price": nxt,
                    "label": "前一档之上按 ATR 递推（仅作反抽高度参考）",
                    "kind": "波动率",
                })
    else:
        # —— 正常：按估值空间分档 + 吸附技术位 ——
        span = v_high - close
        ratios = _LADDER_RATIOS.get(style, _LADDER_RATIOS["long"])
        window = span * 0.18
        picked = []
        for ratio in ratios:
            ideal = close + span * ratio
            if picked and ideal <= picked[-1]["price"] * 1.01:
                ideal = picked[-1]["price"] * 1.015
            best = None
            for cand in candidates:
                if cand["price"] <= close * 1.02:
                    continue
                if picked and cand["price"] <= picked[-1]["price"] * 1.005:
                    continue
                if abs(cand["price"] - ideal) > window:
                    continue
                if best is None or abs(cand["price"] - ideal) < abs(best["price"] - ideal):
                    best = cand
            if best is not None:
                picked.append({"price": best["price"], "label": best["label"], "kind": best["kind"]})
            else:
                picked.append({
                    "price": ideal,
                    "label": f"现价 + {ratio:.0%} 估值空间（估值上沿 {v_high:.2f}）",
                    "kind": "估值空间",
                })
        # 第三档若已被题材推高，允许抬到情绪上限
        if v_ceiling > v_high * 1.03 and heat >= 0.35 and risk < 0.6:
            picked[-1] = {
                "price": v_ceiling,
                "label": f"情绪上限 {v_ceiling:.2f}（题材热度 {heat:.0%} 才看得到）",
                "kind": "情绪",
            }

    # --- 减仓比例 ---
    n = len(picked)
    if valuation_blocked:
        base_trims = [0.50, 0.50, 0.0]
    elif risk >= 0.5:
        base_trims = [0.40, 0.40, 0.20]
    elif composite <= -0.1:
        base_trims = [0.35, 0.40, 0.25]
    else:
        base_trims = [0.30, 0.40, 0.30]
    trims = base_trims[:n]
    trim_total = sum(trims) or 1.0
    trims = [round(t / trim_total * 100) for t in trims]
    # 取整误差补到第一档，保证合计 100%
    if trims and sum(trims) != 100:
        trims[0] += 100 - sum(trims)

    targets: List[Dict[str, Any]] = []
    for idx, item in enumerate(picked):
        price = _round_price(item["price"])
        level = f"T{idx + 1}"
        driver = item["kind"]
        basis = item["label"]
        if driver == "估值" and valuation_blocked:
            basis += f"（只在现价上方 {(v_high / close - 1) * 100:.1f}%，属于估值透支区，只能当反抽位）"
        elif driver == "估值":
            basis += "，估值不支持更高"
        elif driver == "情绪":
            basis += "，只在题材延续时看"

        entry: Dict[str, Any] = {
            "level": level,
            "name": _LEVEL_NAMES[idx] if idx < len(_LEVEL_NAMES) else f"第 {idx + 1} 档",
            "price": price,
            "pct_from_now": round((price - close) / close * 100.0, 2),
            "basis": basis,
            "driver": driver,
            "trim_pct": trims[idx] if idx < len(trims) else 0,
        }
        if holding:
            entry["pct_from_cost"] = round((price - cost) / cost * 100.0, 2)
        if shares > 0 and entry["trim_pct"] > 0:
            raw_shares = int(shares * entry["trim_pct"] / 100 / 100) * 100
            if raw_shares == 0 and shares >= 100:
                raw_shares = 100
            entry["trim_shares"] = min(raw_shares, shares)
        targets.append(entry)

    return targets, valuation_blocked


# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------

_POOL_LOT = 100


def _round_lot(shares: float) -> int:
    """A 股最小 100 股一手，减仓股数向下取整到整手（宁可少卖，不可报错单）。"""
    return int(max(0, shares) // _POOL_LOT * _POOL_LOT)


def _pool_exit_plan(
    pool: Any,
    profit_pct: Optional[float],
    shares: int,
    close: Optional[float],
    holding: bool,
    weeks_held: Optional[float] = None,
    has_started: Optional[bool] = None,
) -> Dict[str, Any]:
    """把四池的止盈阶梯与时间止损合成为「该卖多少股」的指令。

    这两条规则以前只写在 decision_rules.json 里，``take_profit_plan`` /
    ``time_stop_state`` 全仓只有测试在调用，卖出计划完全走 strategy.take_profit
    的粗口径（例如"15% 减仓"）。这里把它们接上，让「+18% 卖一半」「满 8 周清」
    真正变成可执行的减仓指令。

    优先级：时间止损的硬清仓 > 时间止损减半 > 止盈阶梯。
    理由是时间止损是"无条件离场"，止盈是"兑现利润"，两者冲突时先离场。
    has_started 未提供时用"是否有浮盈"近似判断——有利润说明催化兑现过。
    """
    from . import allocation_service as alloc

    key = alloc.normalize_pool(pool)
    if not key:
        return {"applied": False, "orders": [], "warnings": [],
                "reason": "该票未归入四池，只按策略口径给目标价"}

    meta = decision_rules.pool(key) or {}
    label = str(meta.get("label") or key)
    started = (profit_pct or 0.0) > 0 if has_started is None else bool(has_started)

    tp = alloc.take_profit_plan(key, profit_pct or 0.0)
    time_stop = (alloc.time_stop_state(key, weeks_held, started)
                 if weeks_held is not None else None)

    held = int(shares or 0)
    orders: List[Dict[str, Any]] = []
    warnings: List[str] = []

    def _make(action: str, sell_pct: float, reason: str) -> Optional[Dict[str, Any]]:
        if action == "clear":
            sell_pct = 100.0
        sell_pct = max(0.0, min(100.0, float(sell_pct)))
        count = held if action == "clear" else _round_lot(held * sell_pct / 100.0)
        if count <= 0:
            return None
        return {
            "action": action,
            "sell_pct": round(sell_pct, 2),
            "shares": count,
            "amount": round(count * close, 2) if close else None,
            "reason": reason,
        }

    # 1) 时间止损优先：满硬止损周数 → 无条件清仓；观察节点无启动 → 减半
    if time_stop and time_stop.get("action") == "clear":
        order = _make("clear", 100.0, time_stop.get("note") or "时间止损：无条件离场")
        if order:
            orders.append(order)
    elif time_stop and time_stop.get("action") == "halve":
        order = _make("reduce", 50.0, time_stop.get("note") or "时间止损：减半释放资金")
        if order:
            orders.append(order)
    # 2) 否则看止盈阶梯
    elif tp.get("triggered") and _f(tp.get("sell_pct")) > 0:
        reason = f"{label} 已达 {tp.get('trigger_pct')}% 止盈档"
        if tp.get("recover_principal"):
            reason += "（收回本金口径）"
        if tp.get("note"):
            reason += f"：{tp['note']}"
        order = _make("reduce", _f(tp.get("sell_pct")), reason)
        if order:
            orders.append(order)

    # 提示：没到档时不打扰，只说还差多少
    if not orders:
        nxt = tp.get("next_trigger_pct")
        if nxt is not None and profit_pct is not None:
            gap = float(nxt) - float(profit_pct)
            if gap > 0:
                warnings.append(f"{label} 距下一档 {nxt}% 还差 {gap:.1f}pp（当前 {profit_pct:.1f}%）")
        elif tp.get("tail_drawdown_pct"):
            warnings.append(f"{label} 未到止盈档；已有利润仓，高点回撤 {tp['tail_drawdown_pct']}% 应清仓")
        if time_stop and time_stop.get("action") == "hold":
            warnings.append(f"{label} {time_stop.get('note')}")

    if not holding:
        warnings.append(f"未记录持股数与成本，只能给比例、算不出股数；{label} 的档位仍需按比例执行")

    return {
        "applied": True,
        "pool": key,
        "label": label,
        "take_profit": tp,
        "time_stop": time_stop,
        "orders": orders,
        "warnings": warnings,
    }


def _format_pool_exit(exit_plan: Dict[str, Any]) -> str:
    """把池规则结果拼成一句人能直接照做的话。"""
    label = exit_plan.get("label") or "四池"
    parts: List[str] = []
    for order in exit_plan.get("orders") or []:
        verb = "清仓" if order["action"] == "clear" else "减仓"
        seg = f"{verb} {order['sell_pct']:.0f}%"
        if order.get("shares"):
            seg += f" ≈ {order['shares']} 股"
        if order.get("amount"):
            seg += f"（约 {order['amount']:,.0f} 元）"
        if order.get("reason"):
            seg += f" —— {order['reason']}"
        parts.append(seg)
    if not parts:
        return f"【四池规则·{label}】暂不触发减仓"
    return f"【四池规则·{label}】" + "；".join(parts)


def build_sell_plan(
    code: str,
    bars: List[Dict[str, Any]],
    cost: float = 0.0,
    shares: int = 0,
    horizon: Any = "long",
    strategy: Optional[Dict[str, Any]] = None,
    price: Optional[float] = None,
    name: str = "",
    fundamentals: Optional[Dict[str, Any]] = None,
    valuation: Optional[Dict[str, Any]] = None,
    industry_stats: Optional[Dict[str, Any]] = None,
    industry_name: str = "",
    industry_cycle: Optional[Dict[str, Any]] = None,
    industry_valuation: Optional[Dict[str, Any]] = None,
    market: Optional[Dict[str, Any]] = None,
    leader_weight: Optional[float] = None,
    main_business: str = "",
    market_sentiment: Optional[Dict[str, Any]] = None,
    stock_sentiment: Optional[Dict[str, Any]] = None,
    pool: Any = None,
    weeks_held: Optional[float] = None,
    has_started: Optional[bool] = None,
) -> Dict[str, Any]:
    """生成多因子卖出计划。bars 为日 K 序列（升序，含 open/high/low/close/volume/pe/pb）。

    market 是大盘位置（250 日分位），market_sentiment 是全市场情绪快照，
    两者含义不同：前者回答"大盘在哪"，后者回答"今天资金敢不敢接力"。
    """
    if not bars:
        return {"ok": False, "error": f"{code} 无 K 线数据，无法计算卖出目标价"}

    style = normalize_style(horizon)
    params = _STYLE_PARAMS[style]
    strategy = strategy or {}
    valuation = valuation or {}

    last = bars[-1]
    close = _f(price) or _f(last.get("close"))
    if close is None or close <= 0:
        return {"ok": False, "error": f"{code} 最新价不可用"}

    cost = float(cost or 0.0)
    shares = int(shares or 0)
    holding = cost > 0 and shares > 0
    profit_pct = ((close - cost) / cost * 100.0) if holding else None

    # 基金/ETF 只给数据：不给三档目标价、不给分批方案、不给估值结论。
    # 个股这套（PE/PB 分位 + 财报质量 + 行业周期 + 题材情绪）对基金不成立，
    # 硬算出来的目标价会被当真，比不给更糟。
    from .market_gateway import is_data_only

    if is_data_only(code):
        highs = [v for v in (_f(b.get("high")) for b in bars) if v]
        lows = [v for v in (_f(b.get("low")) for b in bars) if v]
        hi = max(highs) if highs else None
        lo = min(lows) if lows else None
        lines = [f"{name or code}（ETF/场内基金·仅提供数据，不进策略体系）", f"现价 {close:.3f}"]
        if holding:
            lines.append(f"成本 {cost:.3f}｜持股 {shares}｜浮动盈亏 {profit_pct:+.2f}%")
        if hi and lo and hi > lo:
            pos = (close - lo) / (hi - lo)
            lines.append(f"近 {len(bars)} 根 K 线区间 {lo:.3f}~{hi:.3f}，"
                         f"当前位置 {pos:.0%}（距区间高点 {(close / hi - 1) * 100:+.1f}%）")
        lines.append("不做估值判断、不给目标价与减仓方案——基金没有自己的 PE/PB 与财报，"
                     "个股框架对它是错的尺子。买卖请按标的所处位置自行决定。")
        return {
            "ok": True,
            "code": code,
            "name": name,
            "data_only": True,
            "price": round(close, 4),
            "cost": cost or None,
            "shares": shares or None,
            "profit_pct": round(profit_pct, 2) if profit_pct is not None else None,
            "range_low": round(lo, 4) if lo else None,
            "range_high": round(hi, 4) if hi else None,
            "text": "\n".join(lines),
        }

    # PE/PB 优先取当日 K 线自带的（与价格同源、时效最好），退化到估值快照
    pe = _f(last.get("pe")) or _f(valuation.get("pe"))
    pb = _f(last.get("pb")) or _f(valuation.get("pb"))
    if pe is not None and pe <= 0:
        pe = None
    if pb is not None and pb <= 0:
        pb = None

    atr = _atr(bars)
    atr_value = atr or (close * 0.025)

    warnings: List[str] = []
    raw_horizon = str(horizon or "").strip().lower()
    if raw_horizon in ("short", "短线", "超短"):
        warnings.append("系统禁止短线/超短线建议，已按波段口径给出方案")
    if len(bars) < 60:
        warnings.append(f"K 线样本仅 {len(bars)} 根，长周期压力位（60/120/250 日）参考性不足")
    if atr is None:
        warnings.append("ATR 计算失败，目标价距离改用百分比估算")
    if pe is None and pb is None:
        warnings.append("缺 PE/PB，估值因子不参与，本方案退化为技术面方案")

    # ---------------- 多因子 ----------------
    analysis = compute_factors(
        bars=bars,
        pe=pe,
        pb=pb,
        industry_stats=industry_stats,
        industry_name=industry_name,
        main_business=main_business,
        fundamentals=fundamentals,
        industry_cycle=industry_cycle,
        industry_valuation=industry_valuation,
    )
    factors = analysis["factors"]
    if pe is not None:
        factors["valuation"]["pe"] = pe
    if pb is not None:
        factors["valuation"]["pb"] = pb
    fair = estimate_fair_value(
        close=close,
        factors=factors,
        industry_stats=industry_stats,
        leader_weight=leader_weight,
        style_band=params["band"],
        bars=bars,
    )

    # ---------------- 技术参考位与目标价 ----------------
    levels, refs = _collect_levels(bars, close, atr_value, params)
    targets, valuation_blocked = _build_targets(
        levels=levels, close=close, cost=cost, holding=holding,
        atr=atr_value, fair=fair, factors=factors, shares=shares,
        composite=analysis["composite"], style=style,
    )

    # ---------------- 低位价值视图（炒作史 + 相对位置 + 均值回归前提）----------------
    # 用户理念：近一年没炒作过 = 位置不高 = 迟早有一波；深跌 + 基本面没变 = 均值回归/戴维斯双击。
    # 这一块不参与目标价计算，但会改变"该不该在低位割肉"的退出模式判定。
    try:
        from .value_position_service import build_value_view
        _val_fac = factors.get("valuation") or {}
        value_view = build_value_view(
            code=code,
            bars=bars,
            market=market,
            industry_cycle=industry_cycle,
            market_sentiment=market_sentiment,
            quality_score=(factors.get("quality") or {}).get("score"),
            growth_score=(factors.get("growth") or {}).get("score"),
            pe_pct=_val_fac.get("pe_pct"),
            pb_pct=_val_fac.get("pb_pct"),
            pb_abs=_val_fac.get("pb"),
            industry_valuation=industry_valuation,
            pe=_val_fac.get("pe"),
            pb=_val_fac.get("pb"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("value_view_failed code=%s error=%r", code, exc)
        value_view = {}

    # ---------------- 情绪仓位策略（用户理念：低情绪留现金建仓，高情绪减仓兑现）----------------
    # 情绪不参与目标价计算，但决定"现在该持有多少仓位、该不该在高位追"。
    try:
        from .sentiment_position_service import build_sentiment_position_policy
        sentiment_policy = build_sentiment_position_policy(
            sentiment=market_sentiment or {},
            market=market or {},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("sentiment_policy_failed code=%s error=%r", code, exc)
        sentiment_policy = {}

    # ---------------- 个股情绪（没传就本地从 K 线反推，不额外请求接口）----------------
    if stock_sentiment is None:
        try:
            from .sentiment_service import sentiment_service as _sentiment
            stock_sentiment = _sentiment.get_stock_sentiment(
                code, bars, industry_name=industry_name, market=market_sentiment,
            )
        except Exception:  # noqa: BLE001
            stock_sentiment = {}

    theme = factors.get("theme") or {}
    heat = float(theme.get("heat") or 0.0)
    risk = float(theme.get("risk") or 0.0)
    composite = float(analysis["composite"])

    if valuation_blocked:
        warnings.append(
            f"现价已高于估值上沿 {fair['high']:.2f}，上方没有估值支撑的空间；"
            f"给出的档位只是技术反抽位，反弹到档位就应减仓，不宜期待趋势性上涨"
        )
    elif _f(fair.get("fair_repair")) is not None and float(fair["fair_repair"]) <= -0.06:
        warnings.append(
            f"多因子算出的估值中枢 {fair.get('price')} 低于现价 {abs(float(fair['fair_repair'])):.0%}，"
            f"说明当前价格已透支基本面；档位目标是减仓节奏，不是上涨空间"
        )
    if heat >= 0.65:
        warnings.append(
            f"情绪热度 {heat:.0%} 已进入过热区（退潮风险 {risk:.0%}）：这类位置的止盈要提前分档执行，别等目标价"
        )
    elif risk >= 0.5:
        warnings.append(f"情绪退潮风险 {risk:.0%}，反弹力度可能不及预期，减仓节奏宜快不宜慢")
    if targets and refs.get("high_60") and targets[0]["price"] > refs["high_60"]:
        warnings.append(f"第一档高于近 60 日高点 {refs['high_60']:.2f}，属于创新高后的位置，短期不必然触及")
    far = [t for t in targets if t["pct_from_now"] >= 40]
    if far:
        warnings.append(
            f"最高档距现价 {far[-1]['pct_from_now']:.0f}%，属于跨季度级别的目标，"
            f"需要业绩或行业景气继续兑现，中途每一档都要重新评估"
        )
    if market and _f(market.get("pos250")) is not None:
        mpos = float(market["pos250"])
        if mpos < 0.2:
            warnings.append(f"大盘处 250 日区间 {mpos:.0%} 低位，目标价容易受系统性拖累")
        elif mpos > 0.9:
            warnings.append(f"大盘处 250 日区间 {mpos:.0%} 高位，注意指数级回撤风险")

    # ---------------- 止损 ----------------
    tp_cfg = strategy.get("take_profit") if isinstance(strategy.get("take_profit"), dict) else {}
    sl_cfg = strategy.get("stop_loss") if isinstance(strategy.get("stop_loss"), dict) else {}
    sl_pct = _f(sl_cfg.get("pct"))
    if sl_pct is None:
        sl_pct = params["stop_loss_default"]
    if sl_pct > 0:
        sl_pct = -sl_pct

    atr_stop = close - params["atr_stop_k"] * atr_value
    cost_stop = cost * (1 + sl_pct / 100.0) if holding else None
    ma_anchor = refs.get("ma60") if params["ma_stop"] == "ma60" else refs.get("ma20")
    ma_anchor = _f(ma_anchor)
    ma_stop = ma_anchor * 0.97 if ma_anchor else None
    low_20 = _f(refs.get("low_20"))

    anchors = [v for v in (cost_stop, ma_stop) if v is not None and v > 0]
    # 已经跌穿所有防守锚（成本止损线、均线都跑到现价上方）时，那些价位只是
    # 「已经放弃的位置」，再报出来毫无意义。此时按当前结构给技术防守位。
    usable = [v for v in anchors if v < close * 0.99]
    stop_broken = bool(anchors) and not usable
    atr_capped = False
    if usable:
        stop = max(usable)
        if stop < atr_stop:
            stop = atr_stop
            atr_capped = True
        if holding and profit_pct is not None and profit_pct >= 10:
            keep = cost * 1.03
            if keep < close:
                stop = max(stop, keep)
    else:
        stop = min(atr_stop, close * 0.97)
        if low_20 and low_20 < close * 0.985:
            stop = min(stop, low_20 * 0.985)
    # 止损不能贴脸：离现价至少 1.2 倍 ATR 且不低于 2%，否则日常噪声就会打掉
    stop = min(stop, close - max(atr_value * 1.2, close * 0.02))

    # 基本面/估值恶化时收紧止损（把止损上移，不等它跌到原位置）
    tighten_notes: List[str] = []
    tighten = 0.0
    quality_score = float((factors.get("quality") or {}).get("score") or 0.0)
    growth_score = float((factors.get("growth") or {}).get("score") or 0.0)
    pe_pct = (factors.get("valuation") or {}).get("pe_pct")
    industry_score = float((factors.get("industry_cycle") or {}).get("score") or 0.0)
    if quality_score <= -0.3:
        tighten += 0.25
        tighten_notes.append("基本面偏弱")
    if growth_score <= -0.4:
        tighten += 0.25
        tighten_notes.append("业绩下滑")
    if pe_pct is not None and float(pe_pct) >= 0.9:
        tighten += 0.20
        tighten_notes.append(f"PE 处 {float(pe_pct):.0%} 极高历史分位")
    if industry_score <= -0.4:
        tighten += 0.15
        tighten_notes.append("行业景气下行")
    if risk >= 0.5:
        tighten += 0.15
        tighten_notes.append("情绪退潮风险高")
    tighten = min(tighten, 0.5)
    if tighten > 0:
        stop = stop + (close - stop) * tighten

    # 保护性约束：止损再紧也得留出 1.2 倍 ATR（至少 2%）的缓冲。
    # 否则「收紧保护利润」会变成「第二天开盘就被扫出去」——
    # 中国平安曾出现止损离现价只有 1.3%（不到 1 倍 ATR），那是随机出场不是止损。
    if not stop_broken:
        min_gap = max(atr_value * 1.2, close * 0.02)
        if stop > close - min_gap:
            stop = close - min_gap
            stop_basis_parts_gap = True
        else:
            stop_basis_parts_gap = False
    else:
        stop_basis_parts_gap = False

    stop = _round_price(stop)

    stop_basis_parts: List[str] = []
    if cost_stop:
        stop_basis_parts.append(f"成本 {sl_pct:.0f}% 止损线 {cost_stop:.2f}")
    if ma_stop:
        stop_basis_parts.append(f"{params['ma_stop'].upper()} 下方 3% 为 {ma_stop:.2f}")
    atr_desc = f"现价 - {params['atr_stop_k']:.1f} 倍 ATR = {atr_stop:.2f}"
    if stop_broken:
        stop_basis_parts.append(
            f"上述防守位都已在现价上方（已跌穿），改按当前结构给技术防守位 {stop:.2f}：{atr_desc}，并参考近 20 日低点"
        )
    elif atr_capped:
        stop_basis_parts.append(f"主锚 {max(usable):.2f} 离现价超过 {params['atr_stop_k']:.1f} 倍 ATR，收紧到{atr_desc}")
    elif usable:
        stop_basis_parts.append(f"{atr_desc}（仅作最远容忍上限）")
    else:
        stop_basis_parts.append(atr_desc)
    if tighten_notes:
        stop_basis_parts.append(f"因{'、'.join(tighten_notes)}，止损从原位置上移 {tighten:.0%}")
    if stop_basis_parts_gap:
        stop_basis_parts.append(
            f"但止损距现价至少保留 1.2 倍 ATR（{max(atr_value * 1.2, close * 0.02):.2f}）的缓冲，"
            f"避免正常日内波动就把仓位扫掉"
        )

    stop_plan: Dict[str, Any] = {
        "price": stop,
        "pct_from_now": round((stop - close) / close * 100.0, 2),
        "basis": "；".join(stop_basis_parts),
    }
    if holding:
        stop_plan["pct_from_cost"] = round((stop - cost) / cost * 100.0, 2)

    # ---------------- 三情景 + 动态退出策略 ----------------
    scenario_block: Dict[str, Any] = {}
    try:
        from .sell_scenarios import build_scenarios
        scenario_block = build_scenarios(
            close=close, cost=cost, holding=holding, shares=shares, atr=atr_value,
            bars=bars, levels=levels, refs=refs, fair=fair, factors=factors,
            market=market_sentiment or {}, stock_sent=stock_sentiment or {},
            base_targets=targets, base_stop=stop_plan,
            value_view=value_view,
            sentiment_policy=sentiment_policy,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("scenario_build_failed code=%s error=%r", code, exc)
        scenario_block = {}
    exit_policy = scenario_block.get("exit_policy") or {}

    # ---------------- 盈亏比与状态 ----------------
    risk_amount = close - stop
    risk_reward = None
    if risk_amount > 0:
        above = [t for t in targets if t["price"] > close * 1.002]
        if above:
            risk_reward = round((above[0]["price"] - close) / risk_amount, 2)

    status, status_text, advice = _judge_status(
        close=close, cost=cost, holding=holding, targets=targets, stop=stop,
        valuation_blocked=valuation_blocked, factors=factors, fair=fair,
    )

    # ---------------- 做 T 参考 ----------------
    t_trade = None
    if params["t_trade"] and holding and len(bars) >= 5:
        amps: List[float] = []
        for bar in bars[-5:]:
            high, low, prev = _f(bar.get("high")), _f(bar.get("low")), _f(bar.get("close"))
            if high and low and prev and prev > 0:
                amps.append((high - low) / prev)
        if amps:
            avg_amp = sum(amps) / len(amps)
            sell_px = close * (1 + avg_amp * 0.5)
            buy_px = close * (1 - avg_amp * 0.5)
            if refs.get("high_20"):
                sell_px = min(sell_px, refs["high_20"])
            if refs.get("low_20"):
                buy_px = max(buy_px, refs["low_20"])
            if stop:
                buy_px = max(buy_px, stop)
            if sell_px > buy_px:
                note = "仅用部分仓位（建议不超过 1/3）做 T，避免打光长线底仓"
                if risk >= 0.5:
                    note = "当前情绪退潮风险高，做 T 容易越做越亏，优先降仓而不是摊成本"
                t_trade = {
                    "avg_amplitude_pct": round(avg_amp * 100, 2),
                    "sell_price": _round_price(sell_px),
                    "buy_price": _round_price(buy_px),
                    "note": note,
                }

    # ---------------- 四池规则（止盈阶梯 + 时间止损）----------------
    # 先算再排版：warnings 才能和别的提示一起进正文，不用在末尾手工补一遍。
    pool_exit = _pool_exit_plan(
        pool=pool, profit_pct=profit_pct, shares=shares, close=close,
        holding=holding, weeks_held=weeks_held, has_started=has_started,
    )
    warnings.extend(pool_exit.get("warnings") or [])

    text = _format_text(
        code=code, name=name, style_label=params["label"],
        close=close, cost=cost, holding=holding, profit_pct=profit_pct,
        factors=factors, fair=fair, targets=targets, stop=stop_plan,
        risk_reward=risk_reward, status_text=status_text, advice=advice,
        t_trade=t_trade, warnings=warnings, refs=refs, atr=atr,
        valuation_blocked=valuation_blocked, market=market, analysis=analysis,
        scenarios=scenario_block.get("scenarios"), exit_policy=exit_policy,
        market_sentiment=market_sentiment, stock_sentiment=stock_sentiment,
        value_view=value_view,
        sentiment_policy=sentiment_policy,
    )
    # 减仓指令单独附在末尾，避免和多因子档位混在一起分不清该听哪个
    if pool_exit.get("applied"):
        text = text + "\n\n" + _format_pool_exit(pool_exit)

    return {
        "ok": True,
        "code": code,
        "name": name,
        "style": style,
        "style_label": params["label"],
        "price": close,
        "cost": cost if holding else None,
        "shares": shares if holding else None,
        "profit_pct": round(profit_pct, 2) if profit_pct is not None else None,
        "atr": round(atr, 3) if atr else None,
        "atr_pct": round(atr / close * 100, 2) if atr else None,
        "pe": pe,
        "pb": pb,
        "refs": refs,
        "factors": factors,
        "composite": analysis["composite"],
        "coverage": analysis["coverage"],
        "stance": analysis["stance"],
        "stance_text": analysis["stance_text"],
        "archetype": analysis["archetype"],
        "archetype_label": analysis["archetype_label"],
        "fair_value": fair,
        "valuation_blocked": valuation_blocked,
        "targets": targets,
        "stop_loss": stop_plan,
        "risk_reward": risk_reward,
        "status": status,
        "status_text": status_text,
        "advice": advice,
        "t_trade": t_trade,
        "market": market or {},
        "market_sentiment": market_sentiment or {},
        "stock_sentiment": stock_sentiment or {},
        "scenarios": scenario_block.get("scenarios") or [],
        "active_scenario": scenario_block.get("active"),
        "active_scenario_reason": scenario_block.get("active_reason"),
        "exit_policy": exit_policy,
        "value_view": value_view,
        "sentiment_policy": sentiment_policy,
        "pool_exit": pool_exit,
        "warnings": warnings,
        "text": text,
    }


def _judge_status(
    close: float,
    cost: float,
    holding: bool,
    targets: List[Dict[str, Any]],
    stop: Optional[float],
    valuation_blocked: bool = False,
    factors: Optional[Dict[str, Any]] = None,
    fair: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, str]:
    """返回 (status, status_text, advice)。advice 供推送/前端做动作标签。"""
    factors = factors or {}
    fair = fair or {}
    theme = factors.get("theme") or {}

    if stop and close <= stop:
        return ("below_stop", "现价已跌破止损位，按纪律应先减仓或离场", "reduce")

    if valuation_blocked:
        low = _f(fair.get("price")) if isinstance(fair, dict) else None
        extra = f"（估值中枢约 {low:.2f}）" if low else ""
        return (
            "valuation_blocked",
            f"估值已透支{extra}，现价高于合理区间上沿，上方只剩技术反抽空间——反弹到档位就减，别等更高价",
            "reduce",
        )

    if float(theme.get("risk") or 0) >= 0.6:
        return ("theme_overheat", "情绪过热且退潮风险高，逢高分批止盈，不要持有等目标", "reduce")

    repair = _f(fair.get("fair_repair")) if isinstance(fair, dict) else None
    if repair is not None and repair <= -0.06:
        return (
            "val_premium",
            f"现价高于多因子估值中枢 {fair.get('price')}（{repair:+.0%}），基本面不支持这个价格，"
            f"上方档位按反弹减仓对待而不是持有理由",
            "trim" if holding else "watch",
        )

    if targets:
        t1 = next((t for t in targets if t["level"] == "T1"), None)
        t2 = next((t for t in targets if t["level"] == "T2"), None)
        if t2 and close >= t2["price"]:
            return ("above_t2", "现价已达主目标区，建议分批了结大部分仓位", "reduce")
        if t1 and close >= t1["price"]:
            return ("above_t1", "现价已达第一目标，建议先减一部分锁定利润", "trim")

    if holding and cost > 0 and float(theme.get("heat") or 0) >= 0.65:
        pct = (close - cost) / cost * 100
        if pct >= 20:
            return ("theme_hot", f"情绪热度高且已有 {pct:+.0f}% 浮盈，先把利润落一部分比等更高价更实际", "trim")

    if holding and cost > 0:
        pct = (close - cost) / cost * 100
        if pct <= -10:
            return ("deep_loss", "浮亏已超 10%，需重点评估是否止损或换仓", "review")
        if pct < 0:
            return ("loss", "仍在成本下方，持有观察，反弹到第一目标先减亏", "hold")
        return ("profit", "已有浮盈，按目标价分批止盈", "hold")

    return ("no_position", "未记录持仓成本，以下价位为多因子参考位", "watch")


# ---------------------------------------------------------------------------
# 文案
# ---------------------------------------------------------------------------

def _fmt_pct(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "--"
    return f"{value:+.{digits}f}%"


def _format_text(
    *,
    code: str,
    name: str,
    style_label: str,
    close: float,
    cost: float,
    holding: bool,
    profit_pct: Optional[float],
    factors: Dict[str, Any],
    fair: Dict[str, Any],
    targets: List[Dict[str, Any]],
    stop: Dict[str, Any],
    risk_reward: Optional[float],
    status_text: str,
    advice: str,
    t_trade: Optional[Dict[str, Any]],
    warnings: List[str],
    refs: Dict[str, Any],
    atr: Optional[float],
    valuation_blocked: bool,
    market: Optional[Dict[str, Any]],
    analysis: Optional[Dict[str, Any]] = None,
    scenarios: Optional[List[Dict[str, Any]]] = None,
    exit_policy: Optional[Dict[str, Any]] = None,
    market_sentiment: Optional[Dict[str, Any]] = None,
    stock_sentiment: Optional[Dict[str, Any]] = None,
    value_view: Optional[Dict[str, Any]] = None,
    sentiment_policy: Optional[Dict[str, Any]] = None,
) -> str:
    head = f"{code} {name}".strip()
    lines = [f"{head}｜{style_label}｜现价 {close:.2f}"]
    if holding:
        lines[0] += f"｜成本 {cost:.2f}｜浮动 {profit_pct:+.2f}%"
    else:
        lines[0] += "｜无持仓成本（下方为多因子参考位）"

    val = factors.get("valuation") or {}
    qual = factors.get("quality") or {}
    grow = factors.get("growth") or {}
    ind = factors.get("industry_cycle") or {}
    stock = factors.get("stock_cycle") or {}
    theme = factors.get("theme") or {}

    val_bits = []
    if val.get("pe") is not None:
        pct = val.get("pe_pct")
        val_bits.append(f"PE {val['pe']:.1f}" + (f"（自身 {pct:.0%} 分位）" if pct is not None else ""))
    if val.get("pb") is not None:
        pct = val.get("pb_pct")
        val_bits.append(f"PB {val['pb']:.2f}" + (f"（自身 {pct:.0%} 分位）" if pct is not None else ""))
    if val.get("industry_pe_avg"):
        val_bits.append(f"行业{val.get('archetype_label')} PE均值 {val['industry_pe_avg']:.1f}")
    val_bits.append(f"→ {val.get('verdict')}")
    lines.append("估值：" + "｜".join(val_bits) + f"（{val.get('score'):+.2f}）")

    report = qual.get("report_date") or "--"
    fin_bits = [f"报告期 {report}", f"ROE {qual.get('roe') if qual.get('roe') is not None else '--'}%"]
    if grow.get("profit_growth") is not None:
        fin_bits.append(f"净利同比 {grow['profit_growth']:+.1f}%")
    if grow.get("rev_growth") is not None:
        fin_bits.append(f"营收同比 {grow['rev_growth']:+.1f}%")
    if qual.get("gross_margin") is not None:
        fin_bits.append(f"毛利率 {qual['gross_margin']:.1f}%")
    if qual.get("ocf_to_profit") is not None:
        fin_bits.append(f"现金流/净利 {qual['ocf_to_profit']:.0f}%")
    lines.append(f"财报：{'｜'.join(fin_bits)} → 质量{qual.get('verdict')}（{qual.get('score'):+.2f}）"
                 f"／成长{grow.get('verdict')}（{grow.get('score'):+.2f}）")

    cyc_bits = []
    if ind.get("industry_name"):
        cyc_bits.append(f"行业{ind['industry_name']}")
    if ind.get("pos250") is not None:
        cyc_bits.append(f"行业指数 250 日 {ind['pos250']:.0%} 位")
    if ind.get("trend"):
        cyc_bits.append(ind["trend"])
    cyc_bits.append(f"→ {ind.get('verdict')}（{ind.get('score'):+.2f}）")
    stock_bit = f"个股 250 日 {stock.get('pos250', 0):.0%} 位 → {stock.get('verdict')}"
    lines.append("周期：" + "｜".join(cyc_bits) + "｜" + stock_bit)

    # 低位价值视图：炒作史 + 相对位置 + 均值回归前提（用户理念的落地）
    vv = value_view or {}
    if vv.get("ok"):
        try:
            from .value_position_service import format_value_view
            lines.append("")
            lines.append(format_value_view(vv, name=name))
        except Exception as exc:  # noqa: BLE001
            logger.warning("value_view_format_failed code=%s error=%r", code, exc)

    # 情绪仓位策略：这条票在"当前市场情绪"下该怎么处置（用户理念：低情绪留钱，高情绪兑现）
    sp = sentiment_policy or {}
    if sp.get("ok"):
        try:
            from .sentiment_position_service import format_sentiment_policy
            lines.append("")
            lines.append(format_sentiment_policy(sp))
        except Exception as exc:  # noqa: BLE001
            logger.warning("sentiment_policy_format_failed code=%s error=%r", code, exc)

    th_bits = []
    if theme.get("amount_ratio"):
        th_bits.append(f"成交额 {theme['amount_ratio']:.2f}x")
    if theme.get("turnover_ratio"):
        th_bits.append(f"换手 {theme['turnover_ratio']:.2f}x")
    if theme.get("ret20") is not None:
        th_bits.append(f"20 日 {theme['ret20']:+.1f}%")
    if theme.get("bias20") is not None:
        th_bits.append(f"乖离 {theme['bias20']:+.1f}%")
    lines.append(f"题材：{'｜'.join(th_bits) if th_bits else '无明显量能异动'} → {theme.get('verdict')}"
                 f"（热度 {theme.get('heat', 0):.0%}、退潮风险 {theme.get('risk', 0):.0%}）")

    stance_map = {
        "bullish": "偏多", "lean_bullish": "偏多", "neutral": "中性",
        "lean_bearish": "偏空", "bearish": "偏空",
    }
    mkt_bit = ""
    if market and market.get("pos250") is not None:
        mkt_bit = f"｜大盘 250 日 {market['pos250']:.0%} 位"
    lines.append(f"综合：{(analysis or {}).get('composite', 0):+.2f}（{stance_map.get((analysis or {}).get('stance'), '中性')}，"
                 f"数据覆盖 {(analysis or {}).get('coverage', 0):.0%}）{mkt_bit}")

    ms = market_sentiment or {}
    if ms.get("ok"):
        metrics = ms.get("metrics") or {}
        lines.append(
            f"市场情绪：{ms.get('phase_label')}（温度 {float(ms.get('temperature') or 0):.0%}）｜"
            f"涨停 {metrics.get('zt_count')} 家 / 炸板 {metrics.get('zb_count')} 家 / "
            f"跌停 {metrics.get('dt_count')} 家｜封板率 {float(metrics.get('seal_rate') or 0):.0%}｜"
            f"最高 {metrics.get('max_board')} 板"
        )
        if ms.get("phase_reason"):
            lines.append(f"　阶段依据：{ms['phase_reason']}")
        hot = ms.get("hot_industries") or []
        if hot:
            lines.append("　涨停最集中：" + "、".join(
                f"{x['name']}({x['zt_count']}家/{x['max_board']}板)" for x in hot[:4]))
    ss = stock_sentiment or {}
    if ss.get("ok"):
        lines.append(
            f"个股情绪：{ss.get('verdict')}｜连板 {ss.get('limit_up_streak') or 0}"
            + ("｜今日炸板" if ss.get("zb_today") else "")
            + (f"｜所属板块今日涨停 {ss.get('sector_zt')} 家" if ss.get("sector_zt") else "")
            + (f"｜接力核心" if ss.get("relay") else "")
        )

    repair = fair.get("fair_repair")
    repair_bit = f"距现价 {repair:+.1%}" if isinstance(repair, (int, float)) else ""
    lines.append(
        f"估值中枢：{fair.get('price')}（{repair_bit}，合理区间 {fair.get('low')}~{fair.get('high')}，"
        f"情绪上限 {fair.get('ceiling')}，置信度 {fair.get('confidence')}）"
    )
    lines.append(f"　估值依据：{fair.get('basis')}")

    for t in targets:
        seg = f"{t['level']} {t['name']} {t['price']:.2f}（距现价 {t['pct_from_now']:+.2f}%"
        if t.get("pct_from_cost") is not None:
            seg += f"，距成本 {t['pct_from_cost']:+.2f}%"
        seg += "）"
        if t.get("trim_pct"):
            seg += f"｜减 {t['trim_pct']}%"
            if t.get("trim_shares"):
                seg += f"（约 {t['trim_shares']} 股）"
        seg += f"｜{t['basis']}"
        lines.append(seg)

    stop_seg = f"止损 {stop['price']:.2f}（距现价 {stop['pct_from_now']:+.2f}%"
    if stop.get("pct_from_cost") is not None:
        stop_seg += f"，距成本 {stop['pct_from_cost']:+.2f}%"
    stop_seg += f"）｜{stop['basis']}"
    lines.append(stop_seg)

    if risk_reward:
        verdict = "空间可接受" if risk_reward >= 2 else "性价比一般，不宜重仓博弈"
        lines.append(f"盈亏比 {risk_reward}:1 —— {verdict}")

    advice_map = {"reduce": "减仓", "trim": "部分止盈", "hold": "持有", "review": "复核", "watch": "观察"}
    lines.append(f"动作建议：{advice_map.get(advice, advice)}｜{status_text}")

    ep = exit_policy or {}
    if ep:
        lines.append("")
        lines.append(f"【当前退出策略：{ep.get('mode_label')}】")
        lines.append(f"　{ep.get('headline')}")
        for rule in ep.get("rules") or []:
            lines.append(f"　· {rule.get('if')} → {rule.get('then')}")
        hs = ep.get("hard_stop") or {}
        ss_ = ep.get("soft_stop") or {}
        ts = ep.get("trailing_stop") or {}
        stop_bits = [
            f"硬止损 {hs.get('price')}（{float(hs.get('pct_from_now') or 0):+.1f}%，逻辑破坏才用）",
        ]
        if ss_.get("available") is not False and ss_.get("price") is not None:
            stop_bits.append(
                f"结构止损 {ss_.get('price')}（{float(ss_.get('pct_from_now') or 0):+.1f}%，先减半）")
        else:
            stop_bits.append("结构止损：当前无可靠技术位")
        if ts.get("applies"):
            stop_bits.append(f"移动止盈 {ts.get('price')}（{float(ts.get('pct_from_now') or 0):+.1f}%，随涨上移）")
        lines.append("　" + "｜".join(stop_bits))
        if ss_.get("available") is False:
            lines.append(f"　{ss_.get('meaning')}")
        if ts.get("applies"):
            lines.append(f"　移动止盈依据：{ts.get('basis')}")
        if ep.get("keep_core_pct"):
            lines.append(f"　{ep.get('keep_core_note')}")

    if scenarios:
        lines.append("")
        lines.append("【三套情景（按触发条件切换，不是三选一）】")
        for sc in scenarios:
            tag = "← 当前适用" if sc.get("is_active") else ""
            prices = " / ".join(f"{t['price']:.2f}(减{t['trim_pct']}%)" for t in sc.get("targets") or [])
            lines.append(f"　{sc['label']}：{prices} {tag}")
            lines.append(f"　　{sc['assumption']}")
            lines.append(f"　　触发：{sc['trigger']}")
    if t_trade:
        lines.append(
            f"做 T 参考：近 5 日平均振幅 {t_trade['avg_amplitude_pct']:.2f}%，"
            f"高抛 {t_trade['sell_price']:.2f} / 低吸 {t_trade['buy_price']:.2f}（{t_trade['note']}）"
        )
    if warnings:
        lines.append("注意：" + "；".join(warnings))
    return "\n".join(lines)
