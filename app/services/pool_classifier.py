"""持仓四池分类建议（可解释打分，不写库、不产生交易）。

设计原则（避免用不可靠数据替你贴标签）：

1. **只给建议，不给结论**。返回值带 confidence 与 reasons，最终由人确认；
   前端必须逐只确认后才落库（human in the loop）。
2. **数据不足就说不足**。宁可 `needs_manual=True` + `suggested_pool=""`，
   也不猜。理由：分类错了会连带把止损/止盈/单票上限全部套错池。
3. **能可靠区分的只有三类倾向**：高股息、低估、成长。
   「催化波段成长」与「长赛道产业成长」的差别是**你的持有意图**（有没有赌某个
   短期事件），不是财报能读出来的——所以本模块只给成长倾向的默认落点
   `long_track`，并在 reasons 里提示「若你冲着短期事件买的，应改 catalytic」，
   同时把 confidence 降一档。

打分上限 3 分/池，命中 ≥2 分且领先 ≥1 分才给 medium 及以上。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from .allocation_service import normalize_pool

# 典型高股息行业（A 股口径，按申万/常见行业名做包含匹配）
_DIVIDEND_INDUSTRY_HINTS = (
    "银行", "保险", "煤炭", "电力", "公用事业", "水务", "燃气",
    "高速公路", "铁路", "港口", "交运", "交通运输",
    "石油石化", "石油", "炼化", "钢铁",
)

# 典型长赛道（渗透率提升型）行业提示，仅用于提高 long_track 的置信度，不作为否决
_LONG_TRACK_INDUSTRY_HINTS = (
    "半导体", "电子", "计算机", "通信", "电力设备", "新能源", "光伏",
    "储能", "医药", "生物", "军工", "机械", "汽车", "人工智能",
)

_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def _f(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _industry_hit(industry: str, hints) -> Optional[str]:
    text = str(industry or "")
    if not text:
        return None
    for hint in hints:
        if hint in text:
            return hint
    return None


def classify_one(
    *,
    code: str = "",
    name: str = "",
    industry: str = "",
    valuation: Optional[Dict[str, Any]] = None,
    fundamentals: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """对单只股票给出四池建议。

    参数都可以为 None/空——缺什么就少一个信号，不会因此硬猜。
    valuation: stock_service._evaluate_valuation 的结果（zone/pe/pb/roe）
    fundamentals: stock_service.get_fundamental_snapshot 的结果
    """
    valuation = valuation if isinstance(valuation, dict) else {}
    fundamentals = fundamentals if isinstance(fundamentals, dict) else {}

    scores = {"dividend": 0.0, "catalyst": 0.0, "long_track": 0.0, "value": 0.0}
    reasons: List[str] = []
    signals = 0

    # ---------- 信号 1：行业属性 ----------
    dividend_hint = _industry_hit(industry, _DIVIDEND_INDUSTRY_HINTS)
    if dividend_hint:
        scores["dividend"] += 2.0
        signals += 1
        reasons.append(f"行业属高股息常见板块（{dividend_hint}）")
    growth_hint = _industry_hit(industry, _LONG_TRACK_INDUSTRY_HINTS)
    if growth_hint:
        scores["long_track"] += 1.0
        signals += 1
        reasons.append(f"行业属成长型赛道（{growth_hint}）")

    # ---------- 信号 2：财务特征 ----------
    roe = _f(fundamentals.get("roe"))
    rev_growth = _f(fundamentals.get("rev_growth"))
    profit_growth = _f(fundamentals.get("profit_growth"))
    gross_margin = _f(fundamentals.get("gross_margin"))
    debt_ratio = _f(fundamentals.get("debt_ratio"))

    if roe is not None:
        if roe >= 12:
            scores["dividend"] += 1.0
            scores["long_track"] += 0.5
            signals += 1
            reasons.append(f"ROE {roe:.1f}% 属优质盈利水平")
        elif roe < 5:
            scores["dividend"] -= 1.0
            scores["long_track"] -= 0.5
            reasons.append(f"ROE 仅 {roe:.1f}%，不适合当股息底仓或长赛道核心")

    if rev_growth is not None or profit_growth is not None:
        rev = rev_growth if rev_growth is not None else 0.0
        profit = profit_growth if profit_growth is not None else 0.0
        best_growth = max(rev, profit)
        if best_growth >= 25:
            scores["long_track"] += 2.0
            scores["catalyst"] += 1.0
            signals += 1
            reasons.append(f"营收/净利增速最高 {best_growth:.1f}%，具备成长属性")
        elif best_growth <= 5:
            scores["dividend"] += 1.0
            scores["value"] += 0.5
            reasons.append(f"增速仅 {best_growth:.1f}%，更像成熟型企业")
            # 高股息行业 + 成熟增速 = 稳定的分红底仓，这是区分「便宜」与「该收股息」的关键组合
            if dividend_hint:
                scores["dividend"] += 1.0
                reasons.append("成熟增速叠加高股息行业，分红稳定性更强")
        if profit < -20:
            scores["long_track"] -= 1.5
            scores["dividend"] -= 1.0
            reasons.append(f"净利增速 {profit:.1f}%，成长逻辑存疑")

    if gross_margin is not None and gross_margin >= 40:
        scores["long_track"] += 0.5
        reasons.append(f"毛利率 {gross_margin:.1f}%，具备定价权特征")

    if debt_ratio is not None and debt_ratio <= 45 and dividend_hint:
        scores["dividend"] += 1.0
        reasons.append(f"资产负债率 {debt_ratio:.1f}%，分红可持续性较好")

    # ---------- 信号 3：估值水平 ----------
    zone = str(valuation.get("zone") or "").lower()
    pe = _f(valuation.get("pe"))
    pb = _f(valuation.get("pb"))
    if zone == "undervalued":
        # 低估只是「便宜」，便宜不等于「低估价值池」——股息票同样便宜。
        # 所以这里不给满权重，真正的区分靠上面的分红/成熟特征与下面的破净信号。
        scores["value"] += 1.5
        signals += 1
        reasons.append("估值处于低估区（破净/低 PE 或行业分位偏低）")
    elif zone == "overvalued":
        scores["value"] -= 1.5
        scores["long_track"] += 0.5
        reasons.append("估值偏高，不适合按低估价值建仓")
    if pe is not None and pe > 0:
        if pe <= 12:
            scores["dividend"] += 1.5
            scores["value"] += 0.5
            reasons.append(f"PE {pe:.1f} 偏低")
        elif pe >= 45:
            scores["value"] -= 1.0
            scores["long_track"] += 0.5
            reasons.append(f"PE {pe:.1f} 偏高，只能靠成长消化")
    if pb is not None and pb > 0 and pb <= 1.0:
        scores["value"] += 1.0
        reasons.append(f"PB {pb:.2f} 已破净，典型低估价值特征")

    # ---------- 汇总 ----------
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_pool, top_score = ordered[0]
    second_pool, second_score = ordered[1]

    # 成长倾向的默认落点选长赛道，并把「可能是催化」的判断权交回给人
    if top_pool == "catalyst" and scores["long_track"] >= scores["catalyst"] - 0.5:
        top_pool = "long_track"
        top_score = scores["long_track"]
        second_pool, second_score = "catalyst", scores["catalyst"]

    result: Dict[str, Any] = {
        "code": code,
        "name": name,
        "industry": industry,
        "suggested_pool": "",
        "suggested_label": "",
        "confidence": "low",
        "needs_manual": True,
        "reasons": reasons,
        "scores": {k: round(v, 2) for k, v in scores.items()},
        "note": "",
    }

    if signals == 0 or top_score < 1.5:
        result["note"] = "可用信号不足（行业/财务/估值都拿不到），请手动选择池"
        return result

    lead = top_score - second_score
    confidence = "low"
    if top_score >= 3.0 and lead >= 1.5 and signals >= 2:
        confidence = "high"
    elif top_score >= 2.0 and lead >= 1.0:
        confidence = "medium"

    result.update({
        "suggested_pool": top_pool,
        "suggested_label": {
            "dividend": "股息", "catalyst": "催化波段成长",
            "long_track": "长赛道产业成长", "value": "低估价值",
        }[top_pool],
        "confidence": confidence,
        "needs_manual": confidence == "low",
    })

    if top_pool == "long_track":
        result["note"] = (
            "判为成长倾向。若你买它是冲着某个短期事件（订单/季报/政策），"
            "应改选「催化波段成长」并接受 8 周时间止损。"
        )
    elif top_pool == "catalyst":
        result["note"] = "判为事件驱动。注意催化池有 4 周观察 / 8 周硬止损。"
    elif top_pool == "dividend":
        result["note"] = "判为股息底仓。若股息率已明显低于买入时水平，应改判为置换候选。"
    else:
        result["note"] = "判为低估价值。建仓后禁止加仓，防价值陷阱。"

    if confidence == "low":
        result["note"] = "信号偏弱，仅作参考。" + result["note"]
    return result


def apply_overrides(
    items: List[Dict[str, Any]],
    overrides: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """用人工给定的分类覆盖建议（overrides: {secID: pool}）。"""
    overrides = overrides or {}
    out: List[Dict[str, Any]] = []
    for item in items:
        pool = normalize_pool(overrides.get(str(item.get("code") or "")))
        if pool:
            item = dict(item)
            item["suggested_pool"] = pool
            item["confidence"] = "manual"
            item["needs_manual"] = False
            item["note"] = "已按人工分类"
        out.append(item)
    return out
