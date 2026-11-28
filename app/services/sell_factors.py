"""卖出目标价的多因子计算层（纯函数，不做任何 IO）。

为什么要有这一层
----------------
只看 K 线 + ATR 算目标价，同一个形态在 PE 90 分位和 PE 20 分位会给出完全一样的
答案，这显然不对。真实的目标价取决于「这家公司值多少钱」与「市场愿意给多少」：

    估值中枢（PE/PB 纵向分位 + 横向行业比较 + PB-ROE）
      × 质量与成长溢价（ROE、现金流含量、增速、PEG）
      × 周期位置（行业指数 250 日分位与均线排列）
      + 题材情绪溢价（量能/换手/涨幅/涨停，只加在上限，不加在中枢）

六个因子统一口径：取值 [-1, +1]，**正数表示对更高股价有利**，负数表示压制。
这样既能加权成综合分，也能单独拿出来给用户解释「为什么这只票我给的低」。

刻意不含的东西
--------------
- 不预测业绩，只读已披露财报（新浪财务分析指标，按报告期）。
- 不猜题材是什么，只用价量特征度量「情绪热度」和「退潮风险」。
- 不做 IO、不读配置，所有输入由调用方传进来，便于单测与复现。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _f(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return None
    if fv != fv:  # NaN
        return None
    return fv


def _clip(value: Optional[float], low: float, high: float) -> float:
    if value is None:
        return 0.0
    return max(low, min(high, float(value)))


def _mean(values: List[float]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def _median(values: List[float]) -> Optional[float]:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2


def _pct_rank(values: List[float], target: Optional[float]) -> Optional[float]:
    """target 在 values 里的分位（0~1）。"""
    clean = [v for v in values if v is not None]
    if not clean or target is None:
        return None
    below = sum(1 for v in clean if v <= target)
    return below / len(clean)


def moving_average(closes: List[float], days: int) -> Optional[float]:
    if not closes or len(closes) < days or days <= 0:
        return None
    return sum(closes[-days:]) / days


def _series(bars: List[Dict[str, Any]], key: str, upper: float) -> List[float]:
    out: List[float] = []
    for bar in bars:
        value = _f(bar.get(key))
        if value is None or value <= 0 or value > upper:
            continue
        out.append(value)
    return out


# ---------------------------------------------------------------------------
# 行业原型：决定用哪个估值通道说话
# ---------------------------------------------------------------------------

# 同一套 PE/PB 阈值套在银行和半导体上是灾难。按行业原型切换权重：
#   金融  PE 长期低位失真，PB 与股息才是锚
#   周期  PE 在盈利底部会虚高（分母塌了），必须 PE/PB 并用
#   成长  PE 可以很高，看的是增速和 PEG
#   稳定  PB + 股息，PE 中等权重
_ARCHETYPE_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "financial": ("银行", "保险", "证券", "多元金融", "信托", "租赁", "金融服务"),
    "cyclical": (
        "钢铁", "煤炭", "有色", "金属", "化工", "石油", "石化", "建材", "水泥",
        "航运", "港口", "航空", "造纸", "房地产", "地产", "工程机械", "养殖",
        "种植", "农副", "玻璃", "化纤", "橡胶", "塑料", "工业金属", "小金属",
    ),
    "growth": (
        "软件", "计算机", "半导体", "电子", "元件", "光学", "通信", "互联网",
        "传媒", "游戏", "医药", "生物", "医疗", "电池", "光伏", "风电", "军工",
        "航空装备", "航天", "机器人", "人工智能", "IT服务", "消费电子",
    ),
    "stable": (
        "电力", "公用", "燃气", "水务", "高速", "铁路", "港口", "食品", "饮料",
        "白酒", "家电", "家用", "纺织", "服装", "零售", "商业", "农林牧渔",
        "环保", "建筑装饰", "交通运输",
    ),
}

_ARCHETYPE_LABEL = {
    "financial": "金融",
    "cyclical": "周期",
    "growth": "成长",
    "stable": "稳定",
    "default": "均衡",
}

# 估值通道权重（纵向分位 / 横向行业 / 行业分位 / 绝对水平）
# ind_pct 是**行业内分位**的权重：这条通道回答"这只票在自己行业里贵不贵"，
# 是用户明确要求的口径（"不能不同行业进行比较"）。
# abs（绝对水平）刻意压到 0.06~0.10：跨行业的绝对 PE/PB 阈值只在
# 没有行业中位时才起主导作用。
_VALUATION_WEIGHTS: Dict[str, Dict[str, float]] = {
    "financial": {"pe_pct": 0.20, "pb_pct": 0.28, "pe_rel": 0.12, "pb_rel": 0.14, "ind_pct": 0.20, "abs": 0.06},
    "cyclical": {"pe_pct": 0.22, "pb_pct": 0.22, "pe_rel": 0.15, "pb_rel": 0.12, "ind_pct": 0.22, "abs": 0.07},
    "growth": {"pe_pct": 0.28, "pb_pct": 0.10, "pe_rel": 0.16, "pb_rel": 0.08, "ind_pct": 0.26, "abs": 0.12},
    "stable": {"pe_pct": 0.22, "pb_pct": 0.18, "pe_rel": 0.14, "pb_rel": 0.12, "ind_pct": 0.24, "abs": 0.10},
    "default": {"pe_pct": 0.24, "pb_pct": 0.18, "pe_rel": 0.15, "pb_rel": 0.10, "ind_pct": 0.24, "abs": 0.09},
}

# PB/PE 合理性参考带（"绝对水平"因子的分档）
_PE_BANDS = ((12.0, 1.0), (18.0, 0.7), (25.0, 0.35), (35.0, 0.0), (50.0, -0.35), (80.0, -0.7))
_PB_BANDS = ((1.0, 1.0), (1.8, 0.6), (3.0, 0.2), (5.0, -0.2), (8.0, -0.55))


def classify_archetype(industry_name: str = "", main_business: str = "") -> str:
    text = f"{industry_name or ''} {main_business or ''}"
    for archetype, keywords in _ARCHETYPE_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return archetype
    return "default"


def archetype_label(archetype: str) -> str:
    return _ARCHETYPE_LABEL.get(archetype or "default", "均衡")


def _band_score(value: Optional[float], bands: Tuple[Tuple[float, float], ...]) -> Optional[float]:
    if value is None or value <= 0:
        return None
    for limit, score in bands:
        if value <= limit:
            return score
    return -1.0


def _industry_center(industry_valuation: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """规整行业估值中枢输入，并补上个股在行业内的分位。

    输入可以是两种形态：
      ① 行业中枢（含 peer 样本列表）——由 IndustryValuationCenter 提供；
      ② 已经算好相对估值的结构——直接透传。

    返回统一结构；ok=False 表示行业中枢不可用（调用方需降级）。
    """
    data = industry_valuation or {}
    if not isinstance(data, dict):
        return {"ok": False}

    # 已经算过相对估值的（assess_industry_relative 的产物）
    if data.get("pe_percentile") is not None or data.get("pb_percentile") is not None:
        return {
            "ok": True,
            "industry_name": data.get("industry_name"),
            "pe_median": data.get("pe_median"),
            "pb_median": data.get("pb_median"),
            "pe_percentile": data.get("pe_percentile"),
            "pb_percentile": data.get("pb_percentile"),
            "source": data.get("source"),
        }

    pe_median = _f(data.get("pe_median"))
    pb_median = _f(data.get("pb_median"))
    if not pe_median and not pb_median:
        return {"ok": False}

    return {
        "ok": True,
        "industry_name": data.get("industry_name"),
        "pe_median": pe_median,
        "pb_median": pb_median,
        "pe_percentile": _f(data.get("pe_percentile")),
        "pb_percentile": _f(data.get("pb_percentile")),
        "source": data.get("source"),
    }


# ---------------------------------------------------------------------------
# 因子 1：估值
# ---------------------------------------------------------------------------

def factor_valuation(
    bars: List[Dict[str, Any]],
    pe: Optional[float],
    pb: Optional[float],
    industry_stats: Optional[Dict[str, Any]],
    archetype: str = "default",
    industry_valuation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """估值因子：纵向（自身历史分位）+ 横向（行业内）+ 绝对水平。

    口径原则（用户明确要求）：**PE/PB 必须在行业坐标系里比较**。
    银行的 PB 0.8 和半导体的 PB 0.8 是两回事，用同一套绝对阈值量它们
    必然对一半错一半。所以绝对水平这一路只在"没有行业中位"时才作为
    兜底使用，且权重压低；一旦拿到行业中位，主判据是行业内相对位置。
    """
    industry_stats = industry_stats or {}
    industry_valuation = industry_valuation or {}
    # PE 序列上限 150：超过这个数不是"估值贵"，而是净利润接近 0 导致 PE 无意义
    # （科大讯飞这类微利股历史 PE 中枢 130+，若不过滤会被当成价值锚反推出荒谬高价）
    pe_series = _series(bars, "pe", 150.0)
    pb_series = _series(bars, "pb", 60.0)

    pe_pct = _pct_rank(pe_series, pe) if (pe and pe > 0) else None
    pb_pct = _pct_rank(pb_series, pb) if (pb and pb > 0) else None
    pe_median = _median(pe_series)
    pb_median = _median(pb_series)
    pe_p80 = None
    if len(pe_series) >= 20:
        ordered = sorted(pe_series)
        pe_p80 = ordered[int(len(ordered) * 0.8)]

    ind_pe = _f(industry_stats.get("pe_avg"))
    ind_pb = _f(industry_stats.get("pb_avg"))
    pe_vs_ind = (pe / ind_pe - 1.0) * 100.0 if (pe and pe > 0 and ind_pe and ind_pe > 0) else None
    pb_vs_ind = (pb / ind_pb - 1.0) * 100.0 if (pb and pb > 0 and ind_pb and ind_pb > 0) else None

    # ---- 行业估值中枢：本次改造的核心 ----
    # 行业中位数（成分股口径）+ 个股在行业内的分位。
    industry_center = _industry_center(industry_valuation)
    has_center = bool(industry_center.get("ok"))

    weights = dict(_VALUATION_WEIGHTS.get(archetype, _VALUATION_WEIGHTS["default"]))
    parts: List[Tuple[float, float, str]] = []
    unclear_pe = pe is None or pe <= 0 or pe > 300

    if pe_pct is not None and not unclear_pe:
        parts.append((weights["pe_pct"], (0.5 - pe_pct) * 2.0,
                      f"PE {pe:.1f} 处近 {len(pe_series)} 个交易日 {pe_pct:.0%} 分位"))
    if pb_pct is not None:
        parts.append((weights["pb_pct"], (0.5 - pb_pct) * 2.0,
                      f"PB {pb:.2f} 处近 {len(pb_series)} 个交易日 {pb_pct:.0%} 分位"))

    # 行业内相对（主口径）：个股相对本行业中位的溢价/折价 + 行业内分位
    industry_pe_rel = industry_pb_rel = None
    industry_pe_pctile = industry_pb_pctile = None
    if has_center:
        industry_name = str(industry_center.get("industry_name") or "") or "本行业"
        center_pe = _f(industry_center.get("pe_median"))
        center_pb = _f(industry_center.get("pb_median"))
        if pe and pe > 0 and center_pe and center_pe > 0:
            industry_pe_rel = (pe / center_pe - 1.0) * 100.0
            parts.append((weights["pe_rel"], -_clip(industry_pe_rel / 40.0, -1.0, 1.0),
                          f"PE {pe:.1f} 较{industry_name}中位 {center_pe:.1f} "
                          f"{'溢价' if industry_pe_rel >= 0 else '折价'} {abs(industry_pe_rel):.0f}%"))
        if pb and pb > 0 and center_pb and center_pb > 0:
            industry_pb_rel = (pb / center_pb - 1.0) * 100.0
            parts.append((weights["pb_rel"], -_clip(industry_pb_rel / 40.0, -1.0, 1.0),
                          f"PB {pb:.2f} 较{industry_name}中位 {center_pb:.2f} "
                          f"{'溢价' if industry_pb_rel >= 0 else '折价'} {abs(industry_pb_rel):.0f}%"))
        # 行业内分位：比"相对中位溢价多少"更直观（0=本行业最便宜）
        industry_pe_pctile = _f(industry_center.get("pe_percentile"))
        industry_pb_pctile = _f(industry_center.get("pb_percentile"))
        for pctile, label, raw in (
            (industry_pe_pctile, "PE", pe),
            (industry_pb_pctile, "PB", pb),
        ):
            if pctile is not None and raw is not None and raw > 0:
                # 分位越低越便宜 → 分数越高（对更高股价有利）
                parts.append((weights["ind_pct"], (0.5 - pctile) * 2.0,
                              f"{label} 在本行业成分股中处 {pctile:.0%} 分位"))
    else:
        # 没有行业中位时的降级：用官方行业均值（仍是行业口径，只是加权）
        if pe_vs_ind is not None and not unclear_pe:
            parts.append((weights["pe_rel"], -_clip(pe_vs_ind / 40.0, -1.0, 1.0),
                          f"PE 较申万行业均值 {ind_pe:.1f} {'溢价' if pe_vs_ind >= 0 else '折价'} {abs(pe_vs_ind):.0f}%"))
        if pb_vs_ind is not None:
            parts.append((weights["pb_rel"], -_clip(pb_vs_ind / 40.0, -1.0, 1.0),
                          f"PB 较申万行业均值 {ind_pb:.2f} {'溢价' if pb_vs_ind >= 0 else '折价'} {abs(pb_vs_ind):.0f}%"))

    # 绝对水平：**只在没有行业内分位时**才作为主判据。
    # 有了行业内分位后它只做微调——因为跨行业的绝对 PE/PB 阈值
    # 本来就是用户要否掉的东西（"每个行业商业模式都不一样"）。
    abs_scale = 1.6 if archetype == "growth" else 1.0
    abs_notes: List[str] = []
    abs_scores: List[float] = []
    pe_abs = _band_score(pe / abs_scale if (pe and pe > 0) else None, _PE_BANDS)
    pb_abs = _band_score(pb, _PB_BANDS)
    if pe_abs is not None and not unclear_pe:
        abs_scores.append(pe_abs)
        abs_notes.append(f"PE {pe:.1f} 绝对值{'偏低' if pe_abs > 0 else ('偏高' if pe_abs < 0 else '中性')}")
    if pb_abs is not None:
        abs_scores.append(pb_abs)
        abs_notes.append(f"PB {pb:.2f} 绝对值{'偏低' if pb_abs > 0 else ('偏高' if pb_abs < 0 else '中性')}")
    if abs_scores:
        abs_weight = weights["abs"] if has_center else weights["abs"] * 1.6
        abs_label = "；".join(abs_notes)
        if has_center:
            abs_label += "（绝对水平已降权，以行业内相对位置为准）"
        parts.append((abs_weight, _mean(abs_scores) or 0.0, abs_label))
        abs_note = None
    else:
        abs_note = None

    score = 0.0
    if parts:
        total_weight = sum(w for w, _, _ in parts)
        score = sum(w * s for w, s, _ in parts) / total_weight if total_weight > 0 else 0.0

    notes = [note for _, _, note in parts]

    # 完全没有估值依据：ETF/LOF 没有 PE/PB，停牌或行情源缺字段也会全空。
    # 原逻辑会走到 unclear_pe 再扣 0.30，于是「没有数据」被判成「偏高」——
    # 把惩罚项当成了结论。惩罚的本意是「PE 不可用就改用 PB」，不是「估值贵」。
    # 对一个黄金 ETF 输出「估值偏高」，比直接说「不适用」危险得多。
    no_basis = (not parts) and pe is None and pb is None

    if no_basis:
        notes.append("无 PE/PB 数据（基金/ETF 或行情源未提供），估值高低判断不成立，不要引用本项")
        score = 0.0
        verdict = "不适用"
    else:
        if unclear_pe:
            score -= 0.30
            notes.append("PE 不可用（亏损或微利），估值判断以 PB 为主")
            if pb_pct is not None:
                score = 0.65 * ((0.5 - pb_pct) * 2.0) - 0.3
        if not has_center:
            notes.append("行业估值中枢不可用，本次未做行业内相对比较（跨行业口径参考性有限）")

        score = _clip(score, -1.0, 1.0)
        if score >= 0.45:
            verdict = "低估"
        elif score >= 0.15:
            verdict = "偏低"
        elif score > -0.15:
            verdict = "合理"
        elif score > -0.45:
            verdict = "偏高"
        else:
            verdict = "高估"

    return {
        "score": round(score, 4),
        "label": "估值",
        "verdict": verdict,
        "archetype": archetype,
        "archetype_label": archetype_label(archetype),
        "pe": pe,
        "pb": pb,
        "pe_pct": round(pe_pct, 4) if pe_pct is not None else None,
        "pb_pct": round(pb_pct, 4) if pb_pct is not None else None,
        "pe_median": round(pe_median, 2) if pe_median else None,
        "pb_median": round(pb_median, 3) if pb_median else None,
        "pe_p80": round(pe_p80, 2) if pe_p80 else None,
        "pe_samples": len(pe_series),
        "pb_samples": len(pb_series),
        # 行业口径（本次改造新增）
        "industry_pe_median": _f(industry_center.get("pe_median")),
        "industry_pb_median": _f(industry_center.get("pb_median")),
        "industry_pe_percentile": round(industry_pe_pctile, 4) if industry_pe_pctile is not None else None,
        "industry_pb_percentile": round(industry_pb_pctile, 4) if industry_pb_pctile is not None else None,
        "pe_vs_industry_median_pct": round(industry_pe_rel, 2) if industry_pe_rel is not None else None,
        "pb_vs_industry_median_pct": round(industry_pb_rel, 2) if industry_pb_rel is not None else None,
        "industry_center_available": has_center,
        "industry_valuation_source": industry_center.get("source"),
        "industry_pe_avg": ind_pe,
        "industry_pb_avg": ind_pb,
        "pe_vs_industry_pct": round(pe_vs_ind, 2) if pe_vs_ind is not None else None,
        "pb_vs_industry_pct": round(pb_vs_ind, 2) if pb_vs_ind is not None else None,
        "detail": "；".join(notes) if notes else "无估值数据（行情源未返回 PE/PB）",
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 因子 2：基本面质量
# ---------------------------------------------------------------------------

def factor_quality(fundamentals: Optional[Dict[str, Any]], archetype: str = "default") -> Dict[str, Any]:
    """质量因子：ROE 水平、毛利率趋势、现金流含金量、杠杆。

    ⚠️ 季节性校正（重要，勿删）：
    非年报报告期（Q1/Q3）的 ROE 是**累计值**，而 ToG / 工程 / 软件项目类公司的
    收入与回款高度集中在 Q4。拿「三季度累计 ROE」套全年阈值，会把「季节性微利」
    误判成「质量差 → 基本面已走弱 → 价值陷阱」。
    实测科大讯飞 2024/2025 两年：Q3 累计 EPS 分别为 -0.18/-0.04，Q4 单季补回
    +0.40，全年转正——若只看三季报，每年都会误判一次。
    所以非年报时改用**最近一期年报 ROE** 作参考，并在 notes 里标明口径。
    """
    f = fundamentals or {}
    roe = _f(f.get("roe"))
    gm = _f(f.get("gross_margin"))
    gm_prev = _f(f.get("gross_margin_prev"))
    ocf = _f(f.get("ocf_to_profit"))
    debt = _f(f.get("debt_ratio"))
    report_date = str(f.get("report_date") or "")
    is_annual = report_date.endswith("12-31")
    notes: List[str] = []
    roe_used = roe

    parts: List[Tuple[float, float, str]] = []

    if roe is not None:
        roe_used = roe
        roe_note = f"ROE {roe:.1f}%"
        if not is_annual and report_date:
            # 找最近一期年报的 ROE 作为「完整年度」参考
            annual_roe = None
            annual_date = ""
            for item in reversed(f.get("history") or []):
                if str(item.get("report_date") or "").endswith("12-31"):
                    annual_roe = _f(item.get("roe"))
                    annual_date = str(item.get("report_date"))[:10]
                    break
            if annual_roe is not None:
                roe_used = annual_roe
                roe_note = (f"ROE {annual_roe:.1f}%（{annual_date} 年报口径；"
                            f"{report_date} 为季度累计 {roe:.1f}%，未含 Q4）")
            else:
                roe_note = f"ROE {roe:.1f}%（季度累计，未含 Q4）"
                notes.append("报告期为季度累计且无年报可比，ROE 可能因季节性偏低")

        if roe_used >= 20:
            s = 1.0
        elif roe_used >= 15:
            s = 0.75
        elif roe_used >= 10:
            s = 0.45
        elif roe_used >= 6:
            s = 0.10
        elif roe_used >= 3:
            s = -0.30
        elif roe_used >= 0:
            s = -0.55
        else:
            s = -0.90
        parts.append((0.50, s, roe_note))

    if gm is not None and gm_prev is not None:
        delta = gm - gm_prev
        parts.append((0.18, _clip(delta / 3.0, -1.0, 1.0) * 0.8,
                      f"毛利率 {gm:.1f}%（上期 {gm_prev:.1f}%，{delta:+.1f}pt）"))

    if ocf is not None and archetype != "financial":
        if ocf >= 120:
            s = 0.6
        elif ocf >= 80:
            s = 0.3
        elif ocf >= 40:
            s = -0.1
        elif ocf >= 0:
            s = -0.5
        else:
            s = -0.8
        parts.append((0.17, s, f"经营现金流/净利润 {ocf:.0f}%"))

    if debt is not None and archetype != "financial":
        limit = 65.0 if archetype == "cyclical" else 70.0
        if debt < 50:
            s = 0.3
        elif debt < limit:
            s = 0.0
        else:
            s = -0.5
        parts.append((0.15, s, f"资产负债率 {debt:.1f}%"))

    score = 0.0
    if parts:
        total_weight = sum(w for w, _, _ in parts)
        score = sum(w * s for w, s, _ in parts) / total_weight if total_weight > 0 else 0.0
    score = _clip(score, -1.0, 1.0)

    if not parts:
        verdict = "无财报"
    elif score >= 0.5:
        verdict = "优秀"
    elif score >= 0.15:
        verdict = "良好"
    elif score > -0.2:
        verdict = "一般"
    elif score > -0.55:
        verdict = "偏弱"
    else:
        verdict = "差"

    return {
        "score": round(score, 4),
        "label": "基本面",
        "verdict": verdict,
        "roe": roe,
        "roe_used": roe_used,
        "is_annual_report": is_annual,
        "gross_margin": gm,
        "debt_ratio": debt,
        "ocf_to_profit": ocf,
        "report_date": f.get("report_date"),
        "detail": "；".join(note for _, _, note in parts) if parts else "无财报数据（数据源不可用）",
        "notes": [note for _, _, note in parts] + notes,
        "seasonality_caveat": (
            "" if is_annual
            else "报告期非年报，Q1/Q3 为累计口径；ToG/工程/软件类公司 Q4 才集中确认收入与回款，"
                 "此时的 ROE 与净利率不代表全年，需等年报验证。"
        ),
    }


# ---------------------------------------------------------------------------
# 因子 3：成长
# ---------------------------------------------------------------------------

def factor_growth(fundamentals: Optional[Dict[str, Any]], pe: Optional[float]) -> Dict[str, Any]:
    """成长因子：净利润/营收增速、增速加速度、PEG。"""
    f = fundamentals or {}
    g = _f(f.get("profit_growth"))
    g_prev = _f(f.get("profit_growth_prev"))
    rg = _f(f.get("rev_growth"))

    notes: List[str] = []
    parts: List[Tuple[float, float]] = []

    if g is not None:
        if g >= 100:
            s = 1.0
        elif g >= 50:
            s = 0.85
        elif g >= 30:
            s = 0.65
        elif g >= 15:
            s = 0.40
        elif g >= 5:
            s = 0.15
        elif g >= 0:
            s = 0.0
        elif g >= -15:
            s = -0.45
        elif g >= -30:
            s = -0.70
        else:
            s = -1.0
        notes.append(f"净利润同比 {g:+.1f}%")
        if g_prev is not None:
            delta = g - g_prev
            s += _clip(delta / 40.0, -1.0, 1.0) * 0.20
            notes.append(f"上期 {g_prev:+.1f}%（{'加速' if delta > 5 else ('减速' if delta < -5 else '持平')}）")
        parts.append((0.70, s))
    elif rg is not None:
        notes.append(f"净利润增速缺失，用营收同比 {rg:+.1f}% 代理")
        parts.append((0.70, _clip(rg / 30.0, -1.0, 1.0) * 0.6))

    if rg is not None:
        parts.append((0.20, _clip(rg / 25.0, -1.0, 1.0) * 0.8))
        notes.append(f"营收同比 {rg:+.1f}%")

    # 增收不增利：营收正增长而利润负增长 → 说明成本/减值在恶化
    if rg is not None and g is not None and rg > 5 and g < -5:
        notes.append("增收不增利，利润端在恶化")

    score = 0.0
    if parts:
        total_weight = sum(w for w, _ in parts)
        score = sum(w * s for w, s in parts) / total_weight if total_weight > 0 else 0.0

    # PEG：只有增速为正时才有意义
    peg = None
    if pe and pe > 0 and g is not None and g > 0:
        peg = pe / g
        if peg < 0.6:
            score += 0.25
            notes.append(f"PEG {peg:.2f}，成长性价比高")
        elif peg < 1.0:
            score += 0.12
            notes.append(f"PEG {peg:.2f}，成长与估值匹配")
        elif peg > 3.0:
            score -= 0.30
            notes.append(f"PEG {peg:.2f}，估值透支成长")
        elif peg > 2.0:
            score -= 0.15
            notes.append(f"PEG {peg:.2f}，偏贵")

    score = _clip(score, -1.0, 1.0)
    if g is None and rg is None:
        verdict = "无财报"
    elif score >= 0.5:
        verdict = "高增长"
    elif score >= 0.15:
        verdict = "稳健增长"
    elif score > -0.2:
        verdict = "停滞"
    elif score > -0.55:
        verdict = "下滑"
    else:
        verdict = "恶化"

    return {
        "score": round(score, 4),
        "label": "成长",
        "verdict": verdict,
        "profit_growth": g,
        "rev_growth": rg,
        "profit_growth_prev": g_prev,
        "peg": round(peg, 2) if peg is not None else None,
        "detail": "；".join(notes) if notes else "无财报数据（数据源不可用）",
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 因子 4：行业周期
# ---------------------------------------------------------------------------

def factor_industry_cycle(
    industry_cycle: Optional[Dict[str, Any]],
    stock_bars: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """行业周期：行业指数在 250 日区间的位置（还有多少空间）+ 趋势 + 相对强弱。

    逻辑要点：**位置取反**。行业在 250 日低位意味着估值修复空间大（+），
    在 250 日高位意味着已经涨过一轮、再扩张空间有限（−）。趋势单独加项，
    所以「低位+刚转强」是最有利组合，「高位+转弱」是最差组合。
    """
    ic = industry_cycle or {}
    pos = _f(ic.get("pos250"))
    ma60 = _f(ic.get("ma60"))
    ma250 = _f(ic.get("ma250"))
    ind_ret60 = _f(ic.get("ret60"))
    stock_ret60 = _return_pct(stock_bars, 60)

    if pos is None and ma60 is None:
        return {
            "score": 0.0, "label": "行业周期", "verdict": "无数据",
            "detail": "未取到行业指数数据，行业周期不参与判断", "notes": [],
        }

    notes: List[str] = []
    score = 0.0
    if pos is not None:
        score += (0.6 - pos) * 2.0 * 0.45
        notes.append(f"行业指数处 250 日区间 {pos:.0%} 位（{ic.get('low250')}~{ic.get('high250')}）")

    trend_text = "不明"
    if ma60 is not None and ma250 is not None and ma250 > 0:
        if ma60 > ma250 * 1.02:
            score += 0.35
            trend_text = "多头（MA60 在年线上方）"
        elif ma60 < ma250 * 0.98:
            score -= 0.35
            trend_text = "空头（MA60 在年线下方）"
        else:
            trend_text = "走平（MA60 贴近年线）"
        notes.append(f"行业趋势{trend_text}")

    rs = None
    if ind_ret60 is not None and stock_ret60 is not None:
        rs = stock_ret60 - ind_ret60
        score += _clip(rs / 15.0, -1.0, 1.0) * 0.20
        notes.append(f"近 60 日个股{stock_ret60:+.1f}% vs 行业{ind_ret60:+.1f}%（超额 {rs:+.1f}pt）")

    score = _clip(score, -1.0, 1.0)
    if score >= 0.4:
        verdict = "景气上行"
    elif score >= 0.1:
        verdict = "温和修复"
    elif score > -0.1:
        verdict = "中性"
    elif score > -0.4:
        verdict = "走弱"
    else:
        verdict = "景气下行"

    return {
        "score": round(score, 4),
        "label": "行业周期",
        "verdict": verdict,
        "industry_name": ic.get("industry_name"),
        "industry_code": ic.get("industry_code"),
        "pos250": round(pos, 4) if pos is not None else None,
        "trend": trend_text,
        "ret60": ind_ret60,
        "relative_strength_60": round(rs, 2) if rs is not None else None,
        "detail": "；".join(notes),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 因子 5：个股周期
# ---------------------------------------------------------------------------

def _return_pct(bars: List[Dict[str, Any]], days: int) -> Optional[float]:
    closes = [_f(b.get("close")) for b in bars]
    closes = [c for c in closes if c and c > 0]
    if len(closes) < days + 1:
        return None
    return (closes[-1] / closes[-(days + 1)] - 1.0) * 100.0


def factor_stock_cycle(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """个股周期：价格在 250 日区间的位置 + 长周期均线排列。

    和「行业周期」的方向**故意相反**：
      行业周期问的是"行业还有多少估值扩张空间"（低位=空间大，故位置取反）；
      个股周期问的是"个股趋势是否成立"（创新高=上方无套牢盘，故位置取正）。
    两者合起来才能表达"行业在底部修复 + 个股已经走出来"这种最好的组合。
    """
    closes = [_f(b.get("close")) for b in bars]
    closes = [c for c in closes if c and c > 0]
    if len(closes) < 60:
        return {"score": 0.0, "label": "个股周期", "verdict": "无数据",
                "detail": "K 线不足 60 根", "notes": []}

    window = closes[-250:] if len(closes) >= 250 else closes
    last = closes[-1]
    high, low = max(window), min(window)
    span = high - low
    pos = (last - low) / span if span > 0 else 0.5
    ma60 = moving_average(closes, 60)
    ma120 = moving_average(closes, 120)
    ma250 = moving_average(closes, 250)
    ret60 = _return_pct(bars, 60)

    notes = [f"股价处 {len(window)} 日区间 {pos:.0%} 位（{low:.2f}~{high:.2f}）"]
    score = (pos - 0.5) * 2.0 * 0.40

    if ma60 and ma120 and ma250 and ma250 > 0:
        if ma60 > ma120 > ma250:
            score += 0.30
            notes.append("MA60>MA120>MA250，长期多头排列")
        elif ma60 < ma120 < ma250:
            score -= 0.30
            notes.append("MA60<MA120<MA250，长期空头排列")
        elif ma60 > ma250:
            score += 0.08
            notes.append("MA60 在年线上方")
        else:
            score -= 0.08
            notes.append("MA60 在年线下方")

    # 远离年线太多时，趋势再强也不适合当"起点"，扣一点分避免追高
    if ma250 and ma250 > 0:
        bias250 = (last / ma250 - 1.0) * 100.0
        if abs(bias250) >= 40:
            score -= 0.15
            notes.append(f"距年线 {bias250:+.0f}%，已明显偏离长期中枢")

    score = _clip(score, -1.0, 1.0)
    if score >= 0.35:
        verdict = "上行"
    elif score >= 0.05:
        verdict = "震荡偏强"
    elif score > -0.2:
        verdict = "震荡偏弱"
    else:
        verdict = "下行"

    return {
        "score": round(score, 4),
        "label": "个股周期",
        "verdict": verdict,
        "pos250": round(pos, 4),
        "ma60": round(ma60, 2) if ma60 else None,
        "ma120": round(ma120, 2) if ma120 else None,
        "ma250": round(ma250, 2) if ma250 else None,
        "ret60": round(ret60, 2) if ret60 is not None else None,
        "detail": "；".join(notes),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 因子 6：题材 / 情绪
# ---------------------------------------------------------------------------

def factor_theme(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """题材炒作因子：只用价量度量热度与退潮风险，不猜题材名称。

    输出两个独立的量：
      heat（0~1） 情绪热度 —— 决定目标上限可以放宽多少
      risk（0~1） 退潮风险 —— 决定止盈是否要提前、止损是否要收紧
    热度高但风险也高（加速末端、乖离过大）时，score 转为负：
    「已经很热了」不是买入理由，而是卖出理由。
    """
    if len(bars) < 30:
        return {"score": 0.0, "label": "题材情绪", "verdict": "无数据", "heat": 0.0,
                "risk": 0.0, "detail": "K 线不足 30 根", "notes": []}

    def avg_amount(subset: List[Dict[str, Any]]) -> Optional[float]:
        return _mean([_f(b.get("amount")) for b in subset if _f(b.get("amount"))])

    def avg_turnover(subset: List[Dict[str, Any]]) -> Optional[float]:
        return _mean([_f(b.get("turnover")) for b in subset if _f(b.get("turnover"))])

    recent5, base60 = bars[-5:], bars[-60:] if len(bars) >= 60 else bars
    amount_ratio = None
    a5, a60 = avg_amount(recent5), avg_amount(base60)
    if a5 and a60 and a60 > 0:
        amount_ratio = a5 / a60
    turnover_ratio = None
    t5, t60 = avg_turnover(recent5), avg_turnover(base60)
    if t5 and t60 and t60 > 0:
        turnover_ratio = t5 / t60

    closes = [_f(b.get("close")) for b in bars]
    closes = [c for c in closes if c and c > 0]
    last = closes[-1] if closes else None
    ma20 = _f(bars[-1].get("MA20")) or moving_average(closes, 20)
    bias20 = ((last / ma20 - 1.0) * 100.0) if (last and ma20 and ma20 > 0) else None
    ret20 = _return_pct(bars, 20)
    ret5 = _return_pct(bars, 5)

    amps: List[float] = []
    for bar in bars[-5:]:
        high, low, prev = _f(bar.get("high")), _f(bar.get("low")), _f(bar.get("close"))
        if high and low and prev and prev > 0:
            amps.append((high - low) / prev * 100.0)
    amp5 = _mean(amps)

    limit_ups = 0
    for bar in bars[-20:]:
        pct = _f(bar.get("pctChg"))
        if pct is not None and pct >= 9.7:
            limit_ups += 1

    heat = 0.0
    heat += 0.30 * _clip(((amount_ratio or 1.0) - 1.0) / 2.0, 0.0, 1.0)
    heat += 0.20 * _clip(((turnover_ratio or 1.0) - 1.0) / 1.5, 0.0, 1.0)
    heat += 0.20 * _clip((ret20 or 0.0) / 30.0, 0.0, 1.0)
    heat += 0.15 * _clip((bias20 or 0.0) / 15.0, 0.0, 1.0)
    heat += 0.15 * _clip(limit_ups / 3.0, 0.0, 1.0)
    heat = _clip(heat, 0.0, 1.0)

    # 退潮风险的标定以「20 日涨幅 15%~45%」「乖离 10%~20%」为满格区间：
    # 这是 A 股题材股常见的加速末端特征，超过就属于情绪透支
    risk = 0.0
    risk += _clip(((ret20 or 0.0) - 15.0) / 30.0, 0.0, 1.0) * 0.30
    risk += _clip(((bias20 or 0.0) - 10.0) / 10.0, 0.0, 1.0) * 0.30
    risk += _clip(((turnover_ratio or 1.0) - 1.8) / 1.5, 0.0, 1.0) * 0.20
    risk += _clip(((amp5 or 0.0) - 5.0) / 5.0, 0.0, 1.0) * 0.15
    if (ret5 or 0.0) < -5.0 and ma20 and last and last < ma20 * 0.97:
        risk += 0.25
    risk = _clip(risk, 0.0, 1.0)

    score = _clip(1.1 * heat * (1.0 - risk) - 0.7 * risk, -1.0, 1.0)

    notes = []
    if amount_ratio:
        notes.append(f"近 5 日成交额是 60 日均值的 {amount_ratio:.2f} 倍")
    if turnover_ratio:
        notes.append(f"换手率放大 {turnover_ratio:.2f} 倍")
    if ret20 is not None:
        notes.append(f"20 日涨幅 {ret20:+.1f}%")
    if bias20 is not None:
        notes.append(f"距 MA20 乖离 {bias20:+.1f}%")
    if limit_ups:
        notes.append(f"近 20 日涨停 {limit_ups} 次")

    if heat >= 0.7 and risk >= 0.5:
        verdict = "过热"
    elif heat >= 0.65:
        verdict = "过热"
    elif heat >= 0.45:
        verdict = "活跃"
    elif risk >= 0.5:
        verdict = "退潮"
    elif heat >= 0.25:
        verdict = "温和放量"
    else:
        verdict = "清淡"

    return {
        "score": round(score, 4),
        "label": "题材情绪",
        "verdict": verdict,
        "heat": round(heat, 4),
        "risk": round(risk, 4),
        "amount_ratio": round(amount_ratio, 2) if amount_ratio else None,
        "turnover_ratio": round(turnover_ratio, 2) if turnover_ratio else None,
        "bias20": round(bias20, 2) if bias20 is not None else None,
        "ret20": round(ret20, 2) if ret20 is not None else None,
        "limit_ups_20d": limit_ups,
        "amp5": round(amp5, 2) if amp5 else None,
        "detail": "；".join(notes) if notes else "无明显量能异动",
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 汇总：综合分
# ---------------------------------------------------------------------------

_FACTOR_WEIGHTS = {
    "valuation": 0.30,
    "growth": 0.25,
    "quality": 0.20,
    "industry_cycle": 0.15,
    "stock_cycle": 0.05,
    "theme": 0.05,
}

# 视为"没有数据"的因子：不参与加权，避免用 0 分污染结果
_MISSING_VERDICTS = {"无数据", "无财报"}


def compute_factors(
    bars: List[Dict[str, Any]],
    pe: Optional[float] = None,
    pb: Optional[float] = None,
    industry_stats: Optional[Dict[str, Any]] = None,
    industry_name: str = "",
    main_business: str = "",
    fundamentals: Optional[Dict[str, Any]] = None,
    industry_cycle: Optional[Dict[str, Any]] = None,
    industry_valuation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    archetype = classify_archetype(industry_name, main_business)
    valuation = factor_valuation(bars, pe, pb, industry_stats, archetype, industry_valuation)
    quality = factor_quality(fundamentals, archetype)
    growth = factor_growth(fundamentals, pe)
    industry = factor_industry_cycle(industry_cycle, bars)
    stock = factor_stock_cycle(bars)
    theme = factor_theme(bars)

    factors = {
        "valuation": valuation,
        "quality": quality,
        "growth": growth,
        "industry_cycle": industry,
        "stock_cycle": stock,
        "theme": theme,
    }

    total_weight = 0.0
    weighted = 0.0
    for key, weight in _FACTOR_WEIGHTS.items():
        item = factors.get(key) or {}
        if str(item.get("verdict") or "") in _MISSING_VERDICTS:
            continue
        total_weight += weight
        weighted += weight * float(item.get("score") or 0.0)
    composite = (weighted / total_weight) if total_weight > 0 else 0.0
    # 数据覆盖率：既表示置信度，也用于让文案说明"这结论靠不靠得住"
    coverage = round(total_weight / sum(_FACTOR_WEIGHTS.values()), 3)

    if composite >= 0.35:
        stance, stance_text = "bullish", "多因子共振偏多：估值/成长/周期总体支持更高价格"
    elif composite >= 0.1:
        stance, stance_text = "lean_bullish", "多因子里偏正面，但缺乏共振"
    elif composite > -0.1:
        stance, stance_text = "neutral", "多因子中性：价格主要由技术面与情绪决定"
    elif composite > -0.35:
        stance, stance_text = "lean_bearish", "多因子偏空：估值或基本面不支持继续上行"
    else:
        stance, stance_text = "bearish", "多因子明显偏空：估值透支且基本面/周期不配合"

    return {
        "archetype": archetype,
        "archetype_label": archetype_label(archetype),
        "factors": factors,
        "composite": round(composite, 4),
        "coverage": coverage,
        "stance": stance,
        "stance_text": stance_text,
    }


# ---------------------------------------------------------------------------
# 估值中枢（合理价）
# ---------------------------------------------------------------------------

def _vwap(bars: List[Dict[str, Any]], days: int = 250) -> Optional[float]:
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


# 单通道隐含空间的压缩上限：超过 +55% 的"修复空间"一律按对数式压缩，
# 因为「现价 × 1.5 以上」在一年维度里已经属于重估级别，不能靠一个倍数通道就推出来。
_CHANNEL_DRAWUP_CAP = 0.55
# 中枢相对现价的硬边界。现价本身就是全市场共识，任何模型都不该给出
# "比市场共识低 38% / 高 40% 以上"的中枢，除非是明确的价值陷阱或重估。
_FAIR_MIN_RATIO = 0.62
_FAIR_MAX_RATIO = 1.40


def _compress_drawup(drawup: float, cap: float = _CHANNEL_DRAWUP_CAP) -> float:
    """把单个通道的隐含空间做尾部压缩，抑制"修复到行业平均"式的线性外推。

    cap 由公司自身质地决定：基本面越差，"均值回归"越不可信（净资产不产生收益时，
    PB 回到历史中枢只是幻想），所以低质量公司的上行通道最多只认 20~28% 空间。
    """
    if drawup <= cap:
        return drawup
    return cap + (drawup - cap) * 0.20


def estimate_fair_value(
    close: float,
    factors: Dict[str, Any],
    industry_stats: Optional[Dict[str, Any]] = None,
    leader_weight: Optional[float] = None,
    style_band: float = 0.18,
    bars: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """多通道估值中枢：每个通道先自证可信度，再按可信度加权隐含空间。

    为什么不能直接把"倍数 × 每股指标"当成目标价
    ---------------------------------------------
    早期版本直接算 ``eps × 行业 PE 均值``，结果在真实持仓上给出荒谬答案：
      - 华域汽车 PB 0.75、ROE 6.35%、净利 -10%，行业 PB 均值 2.10，
        线性推得 43 元（现价 16 元，+166%）。但市场给 0.75 倍 PB 正是因为
        ROE 只有行业一半——**折价是有原因的，不是错误定价**。
      - 科大讯飞历史 PE 中枢 134 倍（微利期 PE 被撑爆），乘当前 EPS 得 52 元，
        等于把上一轮泡沫当成价值锚。

    所以改成三步：
      1. **给每个通道算可信度**（0~1），不可信的通道直接不发声：
         PE 中枢 > 60 倍、净利增速绝对值 > 100%（分母刚剧变）、PB 中枢 > 10 倍，
         这些情况下的"均值回归"都是伪信号。
      2. **横向通道只修一半**：行业倍数与自身倍数取中值，而不是假设
         公司会一步修复到行业平均——那个假设在 A 股几乎从不成立。
      3. **隐含空间先压缩再加权**：单通道最多认 +55% 空间，超出部分打两折。
         加权后再乘 0.55 阻尼，最后裁剪到现价的 0.62~1.40 倍。

    质量/成长/周期溢价（premium）只在中枢上体现一次，不再重复乘进每个通道价，
    避免"溢价被通道数放大"的隐性杠杆。
    """
    industry_stats = industry_stats or {}
    val = factors.get("valuation") or {}
    growth = factors.get("growth") or {}
    quality = factors.get("quality") or {}
    pe = _f(val.get("pe"))
    pb = _f(val.get("pb"))
    notes: List[str] = []

    eps = close / pe if (pe and pe > 0) else None
    bps = close / pb if (pb and pb > 0) else None

    quality_score = float(quality.get("score") or 0.0)
    growth_score = float(growth.get("score") or 0.0)
    industry_score = float((factors.get("industry_cycle") or {}).get("score") or 0.0)
    stock_score = float((factors.get("stock_cycle") or {}).get("score") or 0.0)
    profit_growth = _f(growth.get("profit_growth"))

    premium = 0.20 * quality_score + 0.26 * growth_score + 0.12 * industry_score + 0.05 * stock_score
    is_leader = bool(leader_weight and leader_weight >= 8.0)
    if is_leader:
        premium += 0.08
        notes.append(f"行业权重 {leader_weight:.1f}%，按龙头给质量溢价")
    premium = _clip(premium, -0.42, 0.55)

    # 通道格式：(名称, 目标价, 可信度权重)
    channels: List[Tuple[str, float, float]] = []

    def add_channel(name: str, price: Optional[float], weight: float) -> None:
        if price and price > 0 and weight > 0:
            channels.append((name, float(price), float(weight)))

    # --- 通道 1：自身 PE 中枢 ---
    pe_median = _f(val.get("pe_median"))
    if eps and pe_median and pe_median > 0:
        conf = 0.30
        if pe_median > 60:
            conf = 0.0
            notes.append(f"自身 PE 中枢 {pe_median:.0f} 倍过高（微利期撑大的历史值），不作为估值锚")
        elif profit_growth is not None and abs(profit_growth) >= 100:
            conf = 0.12
            notes.append(f"净利同比 {profit_growth:+.0f}%，PE 中枢均值回归易高估（分母刚剧变）")
        elif pe_median > 45:
            conf = 0.18
        add_channel(f"自身 PE 中枢 {pe_median:.1f}", eps * pe_median, conf)

    # --- 通道 2：自身 PB 中枢（比 PE 稳定得多，微利期也不失真）---
    pb_median = _f(val.get("pb_median"))
    if bps and pb_median and pb_median > 0:
        conf = 0.26 if pb_median <= 10 else 0.12
        add_channel(f"自身 PB 中枢 {pb_median:.2f}", bps * pb_median, conf)

    # --- 通道 3/4：横向行业比较，只修一半 ---
    ind_pb = _f(industry_stats.get("pb_avg"))
    ind_pe = _f(industry_stats.get("pe_avg"))
    if bps and ind_pb and ind_pb > 0 and pb and pb > 0:
        blend_pb = (pb + ind_pb) / 2.0
        add_channel(f"行业 PB {ind_pb:.2f}（自身 {pb:.2f}，按中值修复一半）", bps * blend_pb, 0.18)
    if eps and ind_pe and ind_pe > 0 and pe and pe > 0:
        blend_pe = (pe + ind_pe) / 2.0
        conf = 0.15
        if pe > 60:
            conf = 0.10
        add_channel(f"行业 PE {ind_pe:.1f}（自身 {pe:.1f}，按中值修复一半）", eps * blend_pe, conf)

    # --- 通道 5：近一年成交均价（筹码成本，跌破后是最直接的价值参照）---
    vwap = _vwap(bars or [], 250)
    if vwap:
        add_channel("近一年成交均价", vwap, 0.20)

    if not channels:
        band = style_band
        return {
            "price": round(close, 2),
            "low": round(close * (1 - band), 2),
            "high": round(close * (1 + band), 2),
            "ceiling": round(close * (1 + band * 1.3), 2),
            "premium": 0.0,
            "channels": [],
            "confidence": 0.15,
            "fair_repair": 0.0,
            "basis": "无估值数据（缺 PE/PB），中枢退化为现价，仅作技术参考",
            "theme_premium": 0.0,
            "band_pct": round(band * 100, 1),
            "up_band_pct": round(band * 100, 1),
            "down_band_pct": round(band * 100, 1),
            "up_factor": 1.0,
        }

    # 每个通道先换算成"相对现价的隐含空间"，压缩后再按可信度加权
    # 上行空间的认可上限与公司质地绑定：烂公司的"估值修复"只给很小额度
    up_cap = _CHANNEL_DRAWUP_CAP
    weak_quality = quality_score <= -0.2
    weak_growth = growth_score <= -0.3
    if weak_quality and weak_growth:
        up_cap = 0.20
    elif weak_quality or weak_growth:
        up_cap = 0.28
    if up_cap < _CHANNEL_DRAWUP_CAP:
        notes.append(
            f"基本面{'偏弱' if weak_quality else ''}{'、业绩下滑' if weak_growth else ''}，"
            f"任何估值修复通道的上行空间最多只认 {up_cap:.0%}"
        )

    weighted = 0.0
    total_weight = 0.0
    channel_detail: List[Dict[str, Any]] = []
    for name, price, weight in channels:
        drawup = price / close - 1.0
        compressed = _compress_drawup(drawup, up_cap)
        weighted += compressed * weight
        total_weight += weight
        channel_detail.append({
            "name": name,
            "price": round(price, 2),
            "weight": weight,
            "drawup_pct": round(drawup * 100, 1),
            "used_pct": round(compressed * 100, 1),
        })

    implied_drawup = weighted / total_weight if total_weight > 0 else 0.0
    # 0.55 阻尼 + 基本面溢价只在中枢体现一次
    repair = implied_drawup * 0.55 + premium * 0.55

    theme = factors.get("theme") or {}
    heat = float(theme.get("heat") or 0.0)
    risk = float(theme.get("risk") or 0.0)
    theme_premium = 0.28 * heat * (1.0 - risk)
    # 情绪过热时，估值中枢本身也该往下调（不是往上），因为接下来的抛压来自估值
    if risk >= 0.5:
        repair -= 0.04 * risk
    repair = _clip(repair, math.log(_FAIR_MIN_RATIO), math.log(_FAIR_MAX_RATIO))
    fair = close * (1.0 + repair)

    val_score = float(val.get("score") or 0.0)
    # 估值分已经在 repair 里通过通道价体现，这里只做微弱的方向微调，避免双重放大
    up_factor = _clip(1.0 + 0.45 * val_score, 0.35, 1.55)
    down_factor = _clip(1.0 - 0.40 * val_score, 0.60, 1.50)
    base_band = _clip(style_band * (1.0 + 0.5 * risk), 0.08, 0.32)
    up_band = base_band * up_factor
    down_band = base_band * down_factor

    low = fair * (1 - down_band)
    high = fair * (1 + up_band)
    ceiling = high * (1 + theme_premium)

    # 串联放大护栏：中枢修复 + 区间上沿 + 题材溢价三者相乘会失控，
    # 用"总空间 = repair 的 1.7 倍 + 12%"封顶，防止出现 +100% 的延伸目标。
    total_cap = _clip(repair * 1.7 + 0.12, 0.18, 0.62)
    high = min(high, close * (1 + total_cap))
    ceiling = min(ceiling, close * (1 + total_cap) * 1.12)
    low = max(low, close * (1 - min(0.45, abs(repair) * 1.5 + 0.14)))

    detail = "；".join(f"{item['name']}={item['price']:.2f}" for item in channel_detail)
    basis = (f"中枢 {fair:.2f}（{detail}），"
             f"各通道隐含空间 {implied_drawup:+.0%} → 阻尼后 {implied_drawup * 0.55:+.0%}"
             f"；质量/成长/周期溢价 {premium:+.0%}")
    if notes:
        basis += "；" + "；".join(notes)
    if theme_premium > 0.02:
        basis += f"；题材热度 {heat:.0%} 允许上限再上浮 {theme_premium:.0%}"

    confidence = 0.30
    confidence += 0.15 if eps else 0.0
    confidence += 0.15 if bps else 0.0
    confidence += 0.15 if len(channels) >= 3 else 0.0
    confidence += 0.10 if (factors.get("quality") or {}).get("verdict") not in _MISSING_VERDICTS else 0.0
    confidence += 0.10 if (factors.get("industry_cycle") or {}).get("verdict") not in _MISSING_VERDICTS else 0.0
    # 通道之间分歧大时降低置信度：超过 3 倍极差说明这套估值本身不稳
    prices = [c["price"] for c in channel_detail]
    if prices and min(prices) > 0 and max(prices) / min(prices) > 3.0:
        confidence -= 0.15
        notes.append("各通道估值分歧超过 3 倍，中枢置信度下调")
    confidence = _clip(confidence, 0.15, 0.95)

    return {
        "price": round(fair, 2),
        "low": round(low, 2),
        "high": round(high, 2),
        "ceiling": round(ceiling, 2),
        "band_pct": round(base_band * 100, 1),
        "up_band_pct": round(up_band * 100, 1),
        "down_band_pct": round(down_band * 100, 1),
        "up_factor": round(up_factor, 3),
        "premium": round(premium, 4),
        "theme_premium": round(theme_premium, 4),
        "fair_repair": round(repair, 4),
        "implied_drawup": round(implied_drawup, 4),
        "channels": channel_detail,
        "confidence": round(confidence, 2),
        "basis": basis,
    }
