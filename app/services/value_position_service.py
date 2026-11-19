"""低位价值分析服务（Value Position Service）。

这套服务承载用户的核心炒股经验，并把它变成**可计算、可复现、可回测**的指标：

1. **近一年有没有炒作过**：统计窗口内（默认 250 个交易日）单日最大涨幅、累计涨幅、
   连续大涨次数、"最后一次大涨距今多少天"。长期没大涨 = 缺乏炒作 = 位置不高
   = 从人性和筹码结构看，未来迟早有一波。
2. **相对低位**：当前价在窗口高低区间中的分位、距窗口高点的回撤幅度。
3. **深跌必有反弹**：区分"价格深跌但基本面未变"（均值回归/戴维斯双击的前提）
   与"基本面被证伪"（逻辑止损）。前者不该在地板上割肉。
4. **结合大盘**：大盘 250 日分位决定"低位"这件事值多少钱——大盘同样在低位时，
   均值回归的赔率更好；大盘在高位时，低位股也可能只是跟随杀跌。
5. **安全边际**：好公司股价太高不能碰；烂公司价格极低也可能有机会。
   输出「安全边际」维度而不是简单的好坏判断。

设计原则（与项目理念一致）：
- 只用可核实的数字说话，缺数据就明说"数据不可用"，不猜。
- 结论是"事实 + 判断"的分离，agent 在此基础上再叠加自己的推理。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

# 窗口长度：250 个交易日 ≈ 一年
DEFAULT_WINDOW = 250


def _f(value: Any) -> Optional[float]:
    """宽松地转 float，失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _closes(bars: Sequence[Dict[str, Any]]) -> List[float]:
    out: List[float] = []
    for bar in bars or []:
        c = _f(bar.get("close"))
        if c is not None and c > 0:
            out.append(c)
    return out


def _pct_chg(bars: Sequence[Dict[str, Any]]) -> List[float]:
    """逐根日涨跌幅（%）。首根用自身 pctChg 或 0。"""
    out: List[float] = []
    prev: Optional[float] = None
    for bar in bars or []:
        c = _f(bar.get("close"))
        if c is None or c <= 0:
            out.append(0.0)
            continue
        if prev is None:
            raw = _f(bar.get("pctChg"))
            out.append(raw if raw is not None else 0.0)
        else:
            out.append((c / prev - 1.0) * 100.0)
        prev = c
    return out


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# 行业坐标系：PE/PB 必须放回行业里比
# ---------------------------------------------------------------------------

def _industry_relative_view(
    pe: Optional[float] = None,
    pb: Optional[float] = None,
    industry_valuation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把个股估值放进行业坐标系，回答"在本行业里贵不贵"。

    这是用户明确要求的落地：**不能拿不同行业互相比较 PE/PB**。
    银行的 PB 0.8 和半导体的 PB 0.8 是两回事，只有放进各自的行业
    坐标系里，估值高低才有意义。

    `industry_valuation` 可以是：
      - IndustryValuationCenter.get_industry_stats() 的产物（含 peer 样本列表）
      - assess_industry_relative() 的产物（已算好分位）
    """
    data = industry_valuation or {}
    notes: List[str] = []

    if not isinstance(data, dict) or not data.get("ok"):
        return {
            "ok": False,
            "industry_name": "",
            "pe_median": None,
            "pb_median": None,
            "pe_percentile": None,
            "pb_percentile": None,
            "cheap_in_industry": False,
            "expensive_in_industry": False,
            "notes": ["行业估值中枢不可用，估值判断退回自身历史分位口径"],
        }

    industry_name = str(data.get("industry_name") or "") or "所属行业"
    pe_median = _f(data.get("pe_median"))
    pb_median = _f(data.get("pb_median"))

    peer_pe = [v for v in (data.get("peer_pe_list") or []) if v is not None]
    peer_pb = [v for v in (data.get("peer_pb_list") or []) if v is not None]

    def _pctile(value: Optional[float], population: List[float]) -> Optional[float]:
        if value is None or not population:
            return None
        below = sum(1 for v in population if v <= value)
        return below / len(population)

    pe_pctile = _f(data.get("pe_percentile"))
    pb_pctile = _f(data.get("pb_percentile"))
    if pe_pctile is None and pe is not None and peer_pe:
        pe_pctile = _pctile(pe, peer_pe)
    if pb_pctile is None and pb is not None and peer_pb:
        pb_pctile = _pctile(pb, peer_pb)

    pe_rel = None
    pb_rel = None
    if pe is not None and pe_median and pe_median > 0:
        pe_rel = (pe / pe_median - 1.0) * 100.0
    if pb is not None and pb_median and pb_median > 0:
        pb_rel = (pb / pb_median - 1.0) * 100.0

    # 便宜/贵的判定：优先用行业内分位（最直观），其次用相对中位的折溢价
    pctiles = [p for p in (pe_pctile, pb_pctile) if p is not None]
    rels = [r for r in (pe_rel, pb_rel) if r is not None]
    cheap = False
    expensive = False
    if pctiles:
        avg_pctile = sum(pctiles) / len(pctiles)
        cheap = avg_pctile <= 0.30
        expensive = avg_pctile >= 0.70
    elif rels:
        avg_rel = sum(rels) / len(rels)
        cheap = avg_rel <= -15.0
        expensive = avg_rel >= 15.0

    if pe_rel is not None:
        notes.append(
            f"PE {pe:.1f} 相对{industry_name}中位 {pe_median:.1f} "
            f"{'折价' if pe_rel < 0 else '溢价'} {abs(pe_rel):.0f}%"
        )
    if pb_rel is not None:
        notes.append(
            f"PB {pb:.2f} 相对{industry_name}中位 {pb_median:.2f} "
            f"{'折价' if pb_rel < 0 else '溢价'} {abs(pb_rel):.0f}%"
        )
    if pe_pctile is not None:
        notes.append(f"PE 在{industry_name}成分股中处 {pe_pctile:.0%} 分位")
    if pb_pctile is not None:
        notes.append(f"PB 在{industry_name}成分股中处 {pb_pctile:.0%} 分位")

    return {
        "ok": True,
        "industry_name": industry_name,
        "pe_median": pe_median,
        "pb_median": pb_median,
        "pe_percentile": round(pe_pctile, 4) if pe_pctile is not None else None,
        "pb_percentile": round(pb_pctile, 4) if pb_pctile is not None else None,
        "pe_vs_median_pct": round(pe_rel, 2) if pe_rel is not None else None,
        "pb_vs_median_pct": round(pb_rel, 2) if pb_rel is not None else None,
        "cheap_in_industry": bool(cheap),
        "expensive_in_industry": bool(expensive),
        "source": data.get("source"),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 核心：炒作史 + 相对低位
# ---------------------------------------------------------------------------

def analyze_hype_and_position(
    bars: Sequence[Dict[str, Any]],
    window: int = DEFAULT_WINDOW,
) -> Dict[str, Any]:
    """分析一只票在窗口内「有没有被炒作过」与「现在处在什么位置」。

    返回字段（缺失数据时对应字段为 None 并在 notes 里说明）：
      - pos_pct:      当前价在窗口高低区间的分位（0=最低, 1=最高）
      - high/low:     窗口最高/最低价
      - drawdown:     距窗口高点的回撤（%，负数）
      - ret_total:    窗口累计涨幅（%）
      - max_day_gain: 窗口内单日最大涨幅（%）——"炒作"的直接证据
      - surge_count:  窗口内涨幅 ≥ 阈值的天数（默认 9%）
      - days_since_last_surge: 距最后一次大涨（≥阈值）的交易日数；None=从未大涨
      - hype_level:   炒作程度标签（沉寂 / 温和 / 明显炒作 / 疯狂炒作）
      - position_level: 位置标签（极低 / 偏低 / 中位 / 偏高 / 极高）
      - dormant:      是否"很久没大涨过"（沉寂 ≥ 60 个交易日且累计涨幅 ≤ 25%）
    """
    notes: List[str] = []
    closes = _closes(bars)
    if len(closes) < 60:
        return {
            "ok": False,
            "error": f"K 线样本仅 {len(closes)} 根，不足以判断近一年炒作史（需要至少 60 根）",
            "notes": notes,
        }

    win = int(window) if int(window) > 0 else DEFAULT_WINDOW
    use = closes[-win:] if len(closes) >= win else closes
    use_bars = list(bars)[-len(use):] if bars else []
    span = len(use)
    span_days = f"近 {span} 个交易日"

    high = max(use)
    low = min(use)
    last = use[-1]
    rng = high - low
    pos_pct = (last - low) / rng if rng > 0 else 0.5

    ret_total = (last / use[0] - 1.0) * 100.0 if use[0] > 0 else 0.0
    drawdown = (last / high - 1.0) * 100.0 if high > 0 else 0.0

    # 单日涨幅（与窗口对齐）
    all_chg = _pct_chg(use_bars) if use_bars else _pct_chg(bars)[-span:]
    if len(all_chg) < span:
        all_chg = list(all_chg) + [0.0] * (span - len(all_chg))
    all_chg = all_chg[-span:]

    surge_threshold = 9.0
    max_day_gain = max(all_chg) if all_chg else 0.0
    surge_idx = [i for i, chg in enumerate(all_chg) if chg >= surge_threshold]
    surge_count = len(surge_idx)
    days_since_last_surge = (span - 1 - surge_idx[-1]) if surge_idx else None

    # 炒作程度：单日爆发力 + 累计涨幅 + 大涨频次，三个维度合成 0~1
    burst = _clip(max_day_gain / 18.0)                 # 单日 18% 视为满分
    trend = _clip(ret_total / 80.0)                     # 累计 80% 视为满分
    freq = _clip(surge_count / 12.0)                    # 12 次大涨视为满分
    hype_score = _clip(0.35 * burst + 0.40 * trend + 0.25 * freq, 0.0, 1.0)

    if hype_score >= 0.62:
        hype_level = "疯狂炒作"
    elif hype_score >= 0.42:
        hype_level = "明显炒作"
    elif hype_score >= 0.22:
        hype_level = "温和上涨"
    else:
        hype_level = "沉寂"

    if pos_pct <= 0.15:
        position_level = "极低位"
    elif pos_pct <= 0.35:
        position_level = "偏低位"
    elif pos_pct <= 0.65:
        position_level = "中位"
    elif pos_pct <= 0.85:
        position_level = "偏高位"
    else:
        position_level = "极高位"

    # 「很久没大涨过」：沉寂期长 + 累计涨幅低 = 缺乏炒作 = 位置通常不高
    dormant = bool(
        days_since_last_surge is not None
        and days_since_last_surge >= 60
        and ret_total <= 25.0
    ) or (surge_count == 0 and ret_total <= 15.0)

    if surge_count == 0:
        notes.append(f"{span_days}内没有出现过单日涨幅 ≥ {surge_threshold:.0f}% 的大涨")
    else:
        notes.append(
            f"{span_days}内单日最大涨幅 {max_day_gain:.1f}%，共 {surge_count} 次大涨"
            f"（≥ {surge_threshold:.0f}%），最后一次距今 {days_since_last_surge} 个交易日"
        )
    if dormant:
        notes.append("符合「很久没有大涨过」的特征：缺乏炒作，筹码未充分换手")
    notes.append(
        f"当前价 {last:.2f} 处 {span_days}区间 {low:.2f}~{high:.2f} 的 {pos_pct:.0%} 位置，"
        f"距区间高点 {drawdown:.1f}%，区间累计 {ret_total:+.1f}%"
    )

    return {
        "ok": True,
        "window_days": span,
        "last_close": round(last, 3),
        "high": round(high, 3),
        "low": round(low, 3),
        "pos_pct": round(pos_pct, 4),
        "drawdown": round(drawdown, 2),
        "ret_total": round(ret_total, 2),
        "max_day_gain": round(max_day_gain, 2),
        "surge_count": surge_count,
        "surge_threshold": surge_threshold,
        "days_since_last_surge": days_since_last_surge,
        "hype_score": round(hype_score, 3),
        "hype_level": hype_level,
        "position_level": position_level,
        "dormant": dormant,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 深跌 + 基本面：均值回归 / 戴维斯双击的前提
# ---------------------------------------------------------------------------

def assess_rebound_setup(
    bars: Sequence[Dict[str, Any]],
    hype: Optional[Dict[str, Any]] = None,
    quality_score: Optional[float] = None,
    growth_score: Optional[float] = None,
    pe_pct: Optional[float] = None,
    pb_pct: Optional[float] = None,
    pb_abs: Optional[float] = None,
    drawdown_threshold: float = -25.0,
    pe: Optional[float] = None,
    pb: Optional[float] = None,
    industry_valuation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """判断"深跌是否具备均值回归条件"。

    深跌 + 基本面未证伪 = 戴维斯双击的前提条件（盈利修复 × 估值回归）。
    深跌 + 基本面恶化 = 不是机会，是价值陷阱，该走逻辑止损。

    quality_score / growth_score 来自 sell_factors（-1~1，越高越好）。

    估值口径（用户明确要求）：**PE/PB 必须放回行业坐标系比较**。
    `pe_pct`/`pb_pct` 是"自身历史分位"（纵向，跟自己比），这是对的；
    但判断"贵不贵"不能只看纵向——一只银行 PE 处于自身历史 20% 分位，
    在银行同业里可能就是中位水平。所以这里额外接入 `industry_valuation`：
    行业中枢 + 个股在行业内的分位，两者共同决定"是否便宜"。
    """
    hype = hype or {}
    notes: List[str] = []
    closes = _closes(bars)
    if not closes:
        return {"ok": False, "error": "无 K 线数据", "notes": notes}

    last = closes[-1]
    win = closes[-DEFAULT_WINDOW:] if len(closes) >= DEFAULT_WINDOW else closes
    high = max(win)
    deep_drop = (last / high - 1.0) * 100.0 if high > 0 else 0.0

    is_deep = deep_drop <= drawdown_threshold

    # ---- 行业坐标系：这只票在本行业里到底贵不贵 ----
    industry_view = _industry_relative_view(
        pe=pe, pb=pb, industry_valuation=industry_valuation,
    )
    ind_cheap = bool(industry_view.get("cheap_in_industry"))
    ind_expensive = bool(industry_view.get("expensive_in_industry"))
    ind_pe_pctile = industry_view.get("pe_percentile")
    ind_pb_pctile = industry_view.get("pb_percentile")
    ind_pe_med = industry_view.get("pe_median")
    ind_pb_med = industry_view.get("pb_median")
    ind_name = str(industry_view.get("industry_name") or "") or "所属行业"

    # 周期底部豁免：PB 极低分位（≤25%）或绝对值破净（≤1.0），且业务质量未塌
    # 银行/保险/周期股常见「净利下滑 → 但 PB 已在地板上」，这是盈利周期问题
    # 而不是生意坏了。若用 growth 一票否决，会把戴维斯双击最典型的候选
    # （E 腿在低点、估值也在低点）判成价值陷阱，判反了。
    # 注意：PB 分位这里优先用**行业内分位**——银行的 PB 0.9 在银行里可能是地板，
    # 在半导体里可能是中位数；用错口径会把周期底部豁免判错。
    cheap_asset = bool(
        (ind_pb_pctile is not None and float(ind_pb_pctile) <= 0.25)
        or (pb_pct is not None and float(pb_pct) <= 0.25)
        or (pb_abs is not None and float(pb_abs) <= 1.0)
    )
    quality_ok = quality_score is None or float(quality_score) >= -0.3
    earnings_cycle_trough = bool(
        cheap_asset and quality_ok and (growth_score is None or float(growth_score) < -0.4)
    )

    fundamentals_intact = quality_ok and (
        growth_score is None
        or float(growth_score) >= -0.4
        or earnings_cycle_trough
    )

    # ---- 便宜判定：纵向分位 + 行业内相对，两条都成立才算便宜 ----
    # 只有纵向便宜而行业里是贵的 → 说明是"整个行业都在涨"，不是个股便宜；
    # 只有行业里便宜而纵向不便宜 → 说明是这个行业整体在跌（可能是行业基本面问题，
    #   需要靠"基本面未证伪"来过滤），这时不能单独当便宜证据。
    valuation_cheap = False
    cheap_reasons: List[str] = []
    if pe_pct is not None and pb_pct is not None:
        vertical_cheap = float(pe_pct) <= 0.5 or float(pb_pct) <= 0.3
    elif pe_pct is not None:
        vertical_cheap = float(pe_pct) <= 0.5
    elif pb_pct is not None:
        vertical_cheap = float(pb_pct) <= 0.3
    else:
        vertical_cheap = False

    if vertical_cheap and ind_cheap:
        valuation_cheap = True
        cheap_reasons.append("自身历史分位低 + 在本行业中位以下")
    elif vertical_cheap and not industry_view.get("ok"):
        # 没有行业中枢，只能用纵向口径（降级，但要说清楚）
        valuation_cheap = True
        cheap_reasons.append("自身历史分位低（行业中枢不可用，未做横向校验）")
    elif vertical_cheap and ind_expensive:
        cheap_reasons.append("自身历史分位低，但在本行业里并不便宜（行业整体抬升），打折采信")
    elif ind_cheap and not vertical_cheap:
        cheap_reasons.append("在本行业中位以下，但自身历史分位不低（行业整体承压），打折采信")

    # 破净（PB ≤ 1.0）是绝对低估，任何相对分位口径下都该算便宜
    if pb_abs is not None and float(pb_abs) <= 1.0:
        valuation_cheap = True
        cheap_reasons.append("PB 破净（≤1.0），绝对低估")
    # 纵向分位完全缺失时，行业内便宜可单独成立
    if not valuation_cheap and ind_cheap and pe_pct is None and pb_pct is None:
        valuation_cheap = True
        cheap_reasons.append("在行业中位以下")

    if is_deep and fundamentals_intact:
        verdict = "deep_value_candidate"
        verdict_text = "深跌但基本面未被证伪：符合均值回归/戴维斯双击的前提条件"
        if earnings_cycle_trough:
            verdict_text = (
                "深跌 + 业务质量未塌 + 估值在地板上（PB 极低分位或破净）："
                "单期利润下滑更像盈利周期低点而非生意坏了，属于戴维斯双击的典型起点"
            )
    elif is_deep and not fundamentals_intact:
        verdict = "value_trap_risk"
        verdict_text = "深跌且基本面已走弱：不属于均值回归机会，要防价值陷阱"
    elif not is_deep and fundamentals_intact:
        verdict = "not_deep_enough"
        verdict_text = "基本面未证伪但回撤未达深度（需 ≥ 25%），均值回归逻辑暂不成立"
    else:
        verdict = "no_setup"
        verdict_text = "既无深度回撤，基本面也偏弱，暂不构成低位机会"

    if is_deep:
        notes.append(f"距 {len(win)} 日高点 {high:.2f} 已回撤 {deep_drop:.1f}%")
    else:
        notes.append(f"距 {len(win)} 日高点 {high:.2f} 回撤 {deep_drop:.1f}%，未达深度回撤阈值 {drawdown_threshold:.0f}%")
    if quality_score is not None:
        notes.append(f"基本面质量分 {float(quality_score):+.2f}")
    if growth_score is not None:
        notes.append(f"成长分 {float(growth_score):+.2f}")
    if earnings_cycle_trough:
        _pb_parts = []
        if pb_pct is not None:
            _pb_parts.append(f"PB 分位 {float(pb_pct):.0%}")
        if pb_abs is not None:
            _pb_parts.append(f"PB 绝对值 {float(pb_abs):.2f}")
        notes.append(
            "认定当前利润下滑属于盈利周期底部而非基本面证伪"
            f"（{'／'.join(_pb_parts)}，业务质量分未塌）——"
            "戴维斯双击的 E 腿（盈利修复）就在这个位置"
        )
    if pe_pct is not None:
        notes.append(f"PE 处 {float(pe_pct):.0%} 历史分位")
    if pb_pct is not None:
        notes.append(f"PB 处 {float(pb_pct):.0%} 历史分位")
    if valuation_cheap:
        notes.append("估值仍在历史中低位区间，安全边际相对充足")
    # 行业坐标系说明：告诉用户这个"便宜"是跟谁比的
    for line in (industry_view.get("notes") or []):
        notes.append(line)
    for reason in cheap_reasons:
        notes.append(f"便宜判定依据：{reason}")
    if industry_view.get("ok") and ind_expensive and valuation_cheap:
        notes.append(
            f"注意：{ind_name}内该股估值并不便宜（行业整体抬升所致），"
            "「便宜」这一条的证据强度要打折"
        )

    # 戴维斯双击的必要条件：EPS 改善空间 + 估值修复空间同时存在
    davis = {
        "eps_inflection": growth_score is not None and float(growth_score) >= 0.0,
        "valuation_room": valuation_cheap,
        "double_play_ready": bool(
            (growth_score is not None and float(growth_score) >= 0.0) and valuation_cheap
        ),
    }
    if davis["double_play_ready"]:
        notes.append("戴维斯双击条件初步具备：业绩未恶化 + 估值处低位，等待盈利与估值共振")
    else:
        missing = []
        if not davis["eps_inflection"]:
            missing.append("盈利拐点未确认")
        if not davis["valuation_room"]:
            missing.append("估值修复空间不足")
        if missing:
            notes.append("戴维斯双击尚不成立：" + "、".join(missing))

    return {
        "ok": True,
        "deep_drop": round(deep_drop, 2),
        "is_deep_drop": bool(is_deep),
        "fundamentals_intact": bool(fundamentals_intact),
        "valuation_cheap": bool(valuation_cheap),
        "valuation_cheap_reasons": cheap_reasons,
        "industry_relative": industry_view,
        "verdict": verdict,
        "verdict_text": verdict_text,
        "davis_double_play": davis,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 大盘 + 行业：低位这件事值多少钱
# ---------------------------------------------------------------------------

def assess_market_context(
    market: Optional[Dict[str, Any]] = None,
    industry_cycle: Optional[Dict[str, Any]] = None,
    market_sentiment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """结合大盘与所属板块，判断"低位均值回归"这个逻辑的外部环境是否配合。

    - 大盘自身也在低位 → 均值回归赔率更好（系统性低估）
    - 大盘在高位 → 低位股可能只是跟随杀跌，别把"便宜"当"机会"
    - 板块有没有被炒作 → 板块炒作过 = 套牢盘重；板块沉寂 = 筹码干净
    """
    notes: List[str] = []
    market = market or {}
    industry_cycle = industry_cycle or {}
    sentiment = market_sentiment or {}

    mpos = _f(market.get("pos250"))
    if mpos is None:
        market_state = "未知"
        notes.append("大盘 250 日分位不可用，无法给出系统性位置判断")
    elif mpos <= 0.25:
        market_state = "大盘同处低位"
        notes.append(f"大盘处 250 日区间 {mpos:.0%} 位，系统性低估，均值回归赔率较好")
    elif mpos <= 0.5:
        market_state = "大盘中低位"
        notes.append(f"大盘处 250 日区间 {mpos:.0%} 位，环境中性偏友好")
    elif mpos <= 0.8:
        market_state = "大盘中高位"
        notes.append(f"大盘处 250 日区间 {mpos:.0%} 位，指数本身不便宜，低位股缺乏系统性支撑")
    else:
        market_state = "大盘高位"
        notes.append(f"大盘处 250 日区间 {mpos:.0%} 位，高位回落风险大，此时低位股容易变成接盘")

    ipos = _f(industry_cycle.get("pos250"))
    ind_state = "未知"
    if ipos is None:
        notes.append("行业 250 日分位不可用")
    elif ipos <= 0.3:
        ind_state = "板块低位沉寂"
        notes.append(f"所属板块处 250 日区间 {ipos:.0%} 位，板块未炒作、筹码相对干净")
    elif ipos <= 0.6:
        ind_state = "板块中位"
        notes.append(f"所属板块处 250 日区间 {ipos:.0%} 位，板块位置中性")
    elif ipos <= 0.85:
        ind_state = "板块偏热"
        notes.append(f"所属板块处 250 日区间 {ipos:.0%} 位，板块已有一轮上涨")
    else:
        ind_state = "板块高位"
        notes.append(f"所属板块处 250 日区间 {ipos:.0%} 位，板块在高位，回调风险大于机会")

    # 板块炒作史（用板块自身的动量近似：近 60/120 日涨幅越猛 = 炒得越凶）
    ind_ret60 = _f(industry_cycle.get("ret60"))
    ind_ret120 = _f(industry_cycle.get("ret120"))
    ind_hyped = bool(
        (ind_ret120 is not None and ind_ret120 >= 40.0)
        or (ind_ret60 is not None and ind_ret60 >= 25.0)
    )
    if ind_hyped:
        notes.append("板块近半年涨幅显著，属于炒作过的板块，注意上方套牢盘")
    elif ipos is not None and ipos <= 0.3:
        notes.append("板块涨幅有限，属于没怎么炒作过的方向")

    phase = str(sentiment.get("phase") or "unknown")
    phase_label = str(sentiment.get("phase_label") or "未知")
    if phase in ("ebb", "collapse"):
        env_ok = False
        notes.append(f"市场情绪处于{phase_label}，此阶段谈均值回归要先活下来，别在退潮期加码")
    elif phase in ("ice", "repair"):
        env_ok = True
        notes.append(f"市场情绪处于{phase_label}，是低位潜伏的相对友好窗口")
    else:
        env_ok = None
        notes.append(f"市场情绪处于{phase_label}，环境中性")

    return {
        "ok": True,
        "market_pos250": round(mpos, 4) if mpos is not None else None,
        "market_state": market_state,
        "industry_pos250": round(ipos, 4) if ipos is not None else None,
        "industry_state": ind_state,
        "industry_ret60": ind_ret60,
        "industry_ret120": ind_ret120,
        "industry_hyped": ind_hyped,
        "sentiment_phase": phase,
        "sentiment_label": phase_label,
        "env_supports_rebound": env_ok,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 安全边际：好公司太贵不能碰，烂公司极低也可能有机会
# ---------------------------------------------------------------------------

def assess_margin_of_safety(
    hype: Optional[Dict[str, Any]] = None,
    rebound: Optional[Dict[str, Any]] = None,
    quality_score: Optional[float] = None,
    pe_pct: Optional[float] = None,
    pb_pct: Optional[float] = None,
    pos_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """安全边际评估——用户在系统设计里的"生存根本"。

    好股 + 高价 + 高估值 = 安全边际不足，不碰；
    差股 + 极低价 = 有机会但要有纪律（这是烟蒂股逻辑）；
    深跌 + 基本面未证伪 = 安全边际相对充足。
    """
    hype = hype or {}
    rebound = rebound or {}
    notes: List[str] = []

    good_business = quality_score is not None and float(quality_score) >= 0.2
    cheap = bool(rebound.get("valuation_cheap"))
    deep = bool(rebound.get("is_deep_drop"))
    intact = bool(rebound.get("fundamentals_intact"))
    position_low = pos_pct is not None and float(pos_pct) <= 0.35

    # 行业坐标系补充证据：好公司在自己行业里也不便宜 → 安全边际更不足
    # （用户：好股价格太高也不能碰——"太高"要跟同行比，不是跟历史比）
    industry_view = rebound.get("industry_relative") or {}
    ind_expensive = bool(industry_view.get("expensive_in_industry"))
    ind_name = str(industry_view.get("industry_name") or "") or "所属行业"

    # 价值陷阱优先：深跌 + 基本面已走弱 = 低估的假象，不是安全边际。
    # 「价格便宜」本身不等于「安全边际充足」——前提是便宜的东西还有价值。
    value_trap = deep and not intact
    if value_trap:
        tier = "不足"
        tier_text = "深跌但基本面已走弱：这是价值陷阱而不是安全边际，价格便宜不等于值得持有"
        notes.append("价值陷阱优先于「低位」判断：先排除基本面证伪，再谈位置")
    elif good_business and not cheap:
        tier = "不足"
        tier_text = "好公司但估值不便宜：安全边际不足，不碰"
    elif good_business and cheap and ind_expensive:
        # 好公司 + 纵向便宜，但横向在本行业里偏贵 → 安全边际打折
        tier = "一般"
        tier_text = (
            f"好公司且纵向估值低，但在{ind_name}内并不便宜（行业整体抬升）："
            "安全边际一般，别把「比自己的过去便宜」当成「现在就该买」"
        )
    elif deep and intact and cheap:
        tier = "充足"
        tier_text = "深跌 + 基本面未证伪 + 估值仍在低位：安全边际相对充足"
    elif position_low and cheap:
        tier = "较充足"
        tier_text = "位置低 + 估值低：安全边际尚可（若基本面同时未恶化则更佳）"
    elif good_business and cheap:
        tier = "较充足"
        tier_text = "好公司 + 估值低：典型的安全边际组合"
    else:
        tier = "一般"
        tier_text = "安全边际一般，需要更严格的止损纪律"

    if tier == "不足":
        notes.append("宁可错过，不可买贵——这是生存原则，不是保守")
    if tier == "充足":
        notes.append("符合「越跌越兴奋」的适用条件：价格越低越接近安全边际，而非越跌越该恐慌")
    if industry_view.get("ok"):
        for line in (industry_view.get("notes") or []):
            notes.append(line)

    return {
        "ok": True,
        "tier": tier,
        "tier_text": tier_text,
        "good_business": good_business,
        "valuation_cheap": cheap,
        "position_low": position_low,
        "industry_expensive": ind_expensive,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------

def build_value_view(
    code: str,
    bars: Sequence[Dict[str, Any]],
    market: Optional[Dict[str, Any]] = None,
    industry_cycle: Optional[Dict[str, Any]] = None,
    market_sentiment: Optional[Dict[str, Any]] = None,
    quality_score: Optional[float] = None,
    growth_score: Optional[float] = None,
    pe_pct: Optional[float] = None,
    pb_pct: Optional[float] = None,
    pb_abs: Optional[float] = None,
    window: int = DEFAULT_WINDOW,
    pe: Optional[float] = None,
    pb: Optional[float] = None,
    industry_valuation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把上面三块合成一份完整的「低位价值视图」。"""
    hype = analyze_hype_and_position(bars, window=window)
    if not hype.get("ok"):
        return {
            "ok": False,
            "code": code,
            "error": hype.get("error"),
            "hype": hype,
        }

    rebound = assess_rebound_setup(
        bars=bars, hype=hype, quality_score=quality_score, growth_score=growth_score,
        pe_pct=pe_pct, pb_pct=pb_pct, pb_abs=pb_abs,
        pe=pe, pb=pb, industry_valuation=industry_valuation,
    )
    ctx = assess_market_context(
        market=market, industry_cycle=industry_cycle, market_sentiment=market_sentiment,
    )
    margin = assess_margin_of_safety(
        hype=hype, rebound=rebound, quality_score=quality_score,
        pe_pct=pe_pct, pb_pct=pb_pct, pos_pct=hype.get("pos_pct"),
    )

    # ---- 合成结论 ----
    dormant = bool(hype.get("dormant"))
    is_deep = bool(rebound.get("is_deep_drop"))
    intact = bool(rebound.get("fundamentals_intact"))
    market_low = (ctx.get("market_pos250") is not None and float(ctx["market_pos250"]) <= 0.4)
    env_bad = ctx.get("sentiment_phase") in ("ebb", "collapse")

    if is_deep and intact and bool(rebound.get("valuation_cheap")):
        # 深度价值的**必要条件**是「深跌 + 基本面未证伪 + 估值仍在低位」，
        # 炒作史只是**加分项**（长期没炒作 = 筹码干净、上面套牢盘轻）。
        # 绝不能因为「近一年涨过几次」就把一个 -56% 回撤且基本面完好的标的判成 neutral——
        # 那样会直接错过用户最看重的烟蒂股机会。
        stance = "deep_value"
        stance_text = (
            "深跌 + 基本面未证伪 + 估值仍在低位：符合「越跌越兴奋」的核心条件，"
            "是均值回归与戴维斯双击的候选标的"
        )
        if dormant:
            stance_text += (
                "；且近一年基本没炒作过，筹码未充分换手、上方套牢盘轻，反弹阻力更小"
            )
    elif is_deep and not intact:
        stance = "value_trap"
        stance_text = "深度回撤但基本面已走弱：不是均值回归机会，需防价值陷阱"
    elif dormant:
        stance = "dormant_watch"
        stance_text = (
            "长期没大涨过（缺乏炒作、位置不高），但还没跌出深度："
            "属于「迟早会有一波」的观察对象，等回撤或等信号"
        )
    elif hype.get("hype_score", 0) >= 0.42 and float(hype.get("pos_pct") or 0) >= 0.6:
        stance = "overheated"
        stance_text = (
            "近一年已被明显炒作且现价处在区间偏高位：这类票是「好股太贵不能碰」"
            "的典型，即使基本面不差，安全边际也不足"
        )
    else:
        stance = "neutral"
        stance_text = "位置与炒作史均无极端特征，按常规多因子框架处理"

    if env_bad:
        stance_text += "；但当前市场处于退潮/崩塌阶段，先控仓保命，低位不等于立刻买入"

    notes: List[str] = []
    notes.extend(hype.get("notes") or [])
    notes.extend(rebound.get("notes") or [])
    notes.extend(ctx.get("notes") or [])
    notes.extend(margin.get("notes") or [])

    return {
        "ok": True,
        "code": code,
        "stance": stance,
        "stance_text": stance_text,
        "hype": hype,
        "rebound": rebound,
        "context": ctx,
        "margin_of_safety": margin,
        "notes": notes,
    }


def format_value_view(view: Dict[str, Any], name: str = "") -> str:
    """把低位价值视图渲染成给 agent 读的文字。"""
    if not view or not view.get("ok"):
        return f"{name or view.get('code', '')} 低位价值视图不可用：{view.get('error') or '数据不足'}"

    hype = view.get("hype") or {}
    rebound = view.get("rebound") or {}
    ctx = view.get("context") or {}
    margin = view.get("margin_of_safety") or {}

    label = name or view.get("code", "")
    lines = [
        f"【{label}】低位价值视图（{hype.get('window_days')} 个交易日窗口）",
        f"炒作史：{hype.get('hype_level')}（炒作度 {float(hype.get('hype_score') or 0):.0%}）"
        f"｜区间累计 {float(hype.get('ret_total') or 0):+.1f}%"
        f"｜单日最大涨 {float(hype.get('max_day_gain') or 0):.1f}%"
        f"｜大涨 {hype.get('surge_count')} 次",
    ]
    if hype.get("days_since_last_surge") is not None:
        lines.append(
            f"距最后一次大涨：{hype.get('days_since_last_surge')} 个交易日"
            f"（{'长期沉寂，筹码未充分换手' if hype.get('dormant') else '仍有炒作记忆'}）"
        )
    else:
        lines.append(
            "距最后一次大涨：近一年从未出现单日大涨 ≥9% 的爆发"
            f"（{'长期沉寂，筹码未充分换手' if hype.get('dormant') else '缺乏炒作记忆'}）"
        )
    lines.append(
        f"位置：区间 {hype.get('low')} ~ {hype.get('high')}，现价 {hype.get('last_close')} "
        f"处 {float(hype.get('pos_pct') or 0):.0%} 分位（{hype.get('position_level')}），"
        f"距高点 {float(hype.get('drawdown') or 0):.1f}%"
    )
    lines.append(
        f"深跌判定：回撤 {float(rebound.get('deep_drop') or 0):.1f}%"
        f"｜{'深跌' if rebound.get('is_deep_drop') else '未达深度阈值'}"
        f"｜基本面{'未证伪' if rebound.get('fundamentals_intact') else '已走弱'}"
        f"｜{rebound.get('verdict_text')}"
    )
    davis = rebound.get("davis_double_play") or {}
    lines.append(
        f"戴维斯双击：{'条件初步具备' if davis.get('double_play_ready') else '尚不成立'}"
        f"（盈利拐点 {'有' if davis.get('eps_inflection') else '未确认'}／"
        f"估值空间 {'有' if davis.get('valuation_room') else '不足'}）"
    )
    # 行业坐标系：告诉用户"便宜"是跟谁比的
    ind_view = rebound.get("industry_relative") or {}
    if ind_view.get("ok"):
        bits = []
        if ind_view.get("pe_median") is not None:
            bits.append(f"行业 PE 中位 {float(ind_view['pe_median']):.1f}")
        if ind_view.get("pb_median") is not None:
            bits.append(f"PB 中位 {float(ind_view['pb_median']):.2f}")
        if ind_view.get("pe_percentile") is not None:
            bits.append(f"PE 处行业 {float(ind_view['pe_percentile']):.0%} 分位")
        if ind_view.get("pb_percentile") is not None:
            bits.append(f"PB 处行业 {float(ind_view['pb_percentile']):.0%} 分位")
        if bits:
            lines.append(
                f"行业坐标（{ind_view.get('industry_name') or '所属行业'}）：" + "｜".join(bits)
            )
    else:
        lines.append("行业坐标：行业估值中枢不可用，本次估值判断未做跨行业/同行业横向校验")
    lines.append(
        f"外部环境：{ctx.get('market_state')}（大盘 {ctx.get('market_pos250')}）"
        f"｜{ctx.get('industry_state')}"
        f"｜板块炒作过：{'是' if ctx.get('industry_hyped') else '否'}"
        f"｜情绪 {ctx.get('sentiment_label')}"
    )
    lines.append(f"安全边际：{margin.get('tier')}——{margin.get('tier_text')}")
    lines.append(f"综合：{view.get('stance_text')}")
    return "\n".join(lines)
