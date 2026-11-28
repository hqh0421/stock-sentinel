"""三情景目标价与动态退出策略（纯计算，不做 IO）。

为什么需要情景而不是一条目标价
------------------------------
目标价本质是「未来某条路径上的一个点」。给一条路径，等于假装自己知道未来；
给三套，等于把「什么条件下该做什么」讲清楚，这才是可执行的交易计划。

    保守  —— 假设情绪不再扩张、估值不修复，只兑现技术反弹。目标贴着成本线和近端压力位。
    基线  —— 当前多因子算出的正常路径（T1/T2/T3 + 估值上沿）。
    激进  —— 假设题材接力延续，情绪把价格推到估值上限之外，目标锚定前高与整数关口。

三套同时给出，但**不是让用户选**：每套都带触发条件，达到某个情绪状态就用对应那套执行。

为什么退出不能是「到止损就割」
------------------------------
A 股的题材行情有两段完全不同的风险结构：
    鱼头鱼身：涨起来靠反复接力，越涨越安全（因为有新资金接），过早止盈是主要错误；
    鱼尾    ：情绪还在但结构已经坏了（炸板、涨停家数腰斩、连板断），越等越危险。
同一根 MA20，在鱼身跌破是「洗盘」，在鱼尾跌破是「崩盘起点」。所以止损必须分两类：

    hard_stop   逻辑止损：基本面或估值被证伪，执行不带商量（清仓）
    soft_stop   结构止损：跌破关键均线，先减一半观察，不是清仓
    trailing    移动止盈：接力行情里唯一正确的止盈方式（跟着涨，不预测顶）

退出模式（exit mode）由「市场情绪阶段 × 个股接力状态 × 因子结论」共同决定，
这是本模块的核心，也是用户要的"灵活判断"。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


def _f(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return None
    return fv if fv == fv else None


def _clip(value: Optional[float], low: float, high: float) -> float:
    if value is None:
        return 0.0
    return max(low, min(high, float(value)))


def _rp(price: float) -> float:
    """A 股价格统一保留两位小数。"""
    return round(float(price) + 1e-9, 2)


def _pct(price: float, base: float) -> float:
    if not base:
        return 0.0
    return round((price / base - 1.0) * 100.0, 2)


def _round_level(price: float) -> Optional[float]:
    """上方最近的整数关口。A 股对 10/20/50 这种整数位有天然的抛压与挂单聚集。"""
    if price <= 0:
        return None
    if price < 5:
        step = 0.5
    elif price < 20:
        step = 1.0
    elif price < 100:
        step = 5.0
    else:
        step = 10.0
    level = math.ceil(price / step) * step
    return round(level, 2)


def _avg_amount(bars: List[Dict[str, Any]], days: int) -> Optional[float]:
    vals = [_f(b.get("amount")) for b in bars[-days:] if _f(b.get("amount"))]
    return sum(vals) / len(vals) if vals else None


# ---------------------------------------------------------------------------
# 情绪溢价与接力判定
# ---------------------------------------------------------------------------

def sentiment_premium(market: Dict[str, Any], stock_sent: Dict[str, Any]) -> Tuple[float, List[str]]:
    """情绪能额外给出多少空间（只加在上限，不加在中枢）。

    注意方向：情绪溢价**只在情绪处于扩张期时为正**。退潮阶段溢价为负——
    这时候"情绪"是拖累而不是助力，很多人的错误是把亢奋当常态。
    """
    phase = str((market or {}).get("phase") or "unknown")
    temp = _f((market or {}).get("temperature")) or 0.5
    relay = bool((stock_sent or {}).get("relay"))
    accelerating = bool((stock_sent or {}).get("accelerating"))
    zb_today = bool((stock_sent or {}).get("zb_today"))
    streak = int((stock_sent or {}).get("limit_up_streak") or 0)

    notes: List[str] = []
    premium = 0.0
    # 市场层面的溢价只是"顺风"，个股不在接力核心时最多只能吃到三分之一。
    # 否则会出现「大盘亢奋 → 一只没人要的票也被算出 +50% 目标价」的荒谬结论。
    market_part = 0.0
    if phase in ("advance", "euphoria"):
        market_part = 0.10 if phase == "advance" else 0.16
        notes.append(f"市场处于{market.get('phase_label')}（温度 {temp:.0%}）")
    elif phase == "repair":
        market_part = 0.04
        notes.append("市场处于修复期")
    elif phase in ("ebb", "collapse"):
        market_part = -0.08
        notes.append(f"市场{market.get('phase_label')}，情绪转为拖累")
    elif phase == "freezing":
        market_part = -0.04
        notes.append("市场冰点")

    if relay:
        premium += market_part
        notes.append(f"个股在接力核心，市场情绪溢价全额计入 {market_part:+.0%}")
    else:
        discounted = market_part * 0.35 if market_part > 0 else market_part
        premium += discounted
        if market_part:
            notes.append(
                f"个股不在接力核心，市场情绪溢价由 {market_part:+.0%} 折为 {discounted:+.0%}"
                "——大盘热不等于这只票能跟着热"
            )

    if relay:
        premium += 0.10
        notes.append("接力核心额外 +10%")
    if streak >= 3:
        premium += 0.06
        notes.append(f"{streak} 连板打开空间 +6%")
    if accelerating:
        premium += 0.04
        notes.append("处于加速段 +4%")
    if zb_today:
        premium -= 0.08
        notes.append("今日炸板，接力资金不足 -8%")

    # 上限 0.25：再狂热的题材，也不该让"情绪"单独贡献 25% 以上的目标价空间
    return _clip(premium, -0.15, 0.25), notes


# ---------------------------------------------------------------------------
# 目标价构建
# ---------------------------------------------------------------------------

def _build_conservative(
    close: float,
    cost: float,
    holding: bool,
    atr: float,
    refs: Dict[str, Any],
    fair: Dict[str, Any],
    levels: List[Dict[str, Any]],
    base_targets: List[Dict[str, Any]],
    shares: int,
) -> Dict[str, Any]:
    """保守：只吃到「确定能到」的位置，不假设情绪与估值扩张。

    上限取 min(估值中枢, 基线第一档) —— 也就是说保守情景永远不高于基线。
    """
    fair_price = _f(fair.get("price")) or close
    cap = min(fair_price * 1.01, close * 1.15)
    if base_targets:
        cap = min(cap, base_targets[0]["price"] * 1.02)
    cap = max(cap, close * 1.02)

    candidates: List[Dict[str, Any]] = []
    # 成本线优先：浮亏时"回本减仓"是最现实的第一目标
    if holding and cost > close * 1.005:
        cost_line = cost * 1.005
        if cost_line <= cap * 1.02:
            candidates.append({
                "price": _rp(min(cost_line, cap)),
                "label": f"成本线 {cost:.2f} 附近（回本先减）",
                "kind": "成本",
            })
    # 两档之间至少留 1 倍 ATR 或 1.5% 的间距，否则两档挤在一起等于只有一档
    min_gap = max(atr * 1.0, close * 0.015)
    for item in levels:
        if item["price"] <= close * 1.008:
            continue
        if item["price"] > cap:
            break
        if candidates and item["price"] < candidates[-1]["price"] + min_gap:
            continue
        candidates.append({
            "price": _rp(item["price"]),
            "label": item["label"],
            "kind": item["kind"],
        })
        if len(candidates) >= 2:
            break

    if not candidates:
        step = max(atr * 1.0, close * 0.012)
        candidates.append({
            "price": _rp(close + step),
            "label": f"上方无有效压力位，按 1 倍 ATR 递推（{step:.2f}）",
            "kind": "波动率",
        })
    if len(candidates) == 1:
        nxt = candidates[0]["price"] + max(atr * 1.2, close * 0.015)
        if nxt <= cap * 1.03:
            candidates.append({
                "price": _rp(nxt),
                "label": "前一档之上再递推一档（仅在第一档达成后看）",
                "kind": "波动率",
            })

    targets: List[Dict[str, Any]] = []
    total = len(candidates)
    trims = [60, 40] if total >= 2 else [100]
    for idx, cand in enumerate(candidates):
        trim = trims[idx] if idx < len(trims) else 0
        item: Dict[str, Any] = {
            "level": f"C{idx + 1}",
            "name": "保守档" if idx == 0 else "保守加档",
            "price": cand["price"],
            "pct_from_now": _pct(cand["price"], close),
            "trim_pct": trim,
            "basis": cand["label"],
        }
        if holding and cost > 0:
            item["pct_from_cost"] = _pct(cand["price"], cost)
        if shares:
            item["trim_shares"] = int(shares * trim / 100)
        targets.append(item)

    return {
        "key": "conservative",
        "label": "保守（只吃确定的一段）",
        "assumption": "题材不再扩散、估值不修复，只兑现技术反弹与回本需求",
        "trigger": "市场退潮/冰点、或估值已透支、或基本面在恶化时，按这套执行",
        "targets": targets,
        "remaining_pct": 0 if total <= 1 else 0,
        "note": "两档打完即清。保守情景的价值是「不被套」，代价是错过后续所有行情。",
    }


def _build_aggressive(
    close: float,
    cost: float,
    holding: bool,
    atr: float,
    refs: Dict[str, Any],
    fair: Dict[str, Any],
    levels: List[Dict[str, Any]],
    base_targets: List[Dict[str, Any]],
    shares: int,
    premium: float,
    market: Dict[str, Any],
    stock_sent: Dict[str, Any],
) -> Dict[str, Any]:
    """激进：吃鱼尾。锚定前高、整数关口与「估值上限 × 情绪溢价」。

    这一档刻意给到「看起来太贵」的位置，因为它回答的问题是
    "如果散户接力真的把价格推上去，我最多看到哪"，不是"我认为它值多少"。
    配套的退出方式必须是移动止盈，而不是到价卖出——鱼尾段没人能精确逃顶。
    """
    ceiling = _f(fair.get("ceiling")) or _f(fair.get("high")) or close * 1.2
    upper = ceiling * (1.0 + premium)
    # 鱼尾也要有物理约束：情绪溢价叠加估值上沿后，最多看到现价 +75%。
    # 超过这个数的目标价不是"鱼尾"，是幻想——A 股一年翻倍的题材股有，但不能当作计划的基准。
    upper = min(upper, close * 1.75)

    base_top = base_targets[-1]["price"] if base_targets else close * 1.15
    # 激进档必须明显高于基线最高档，否则两套情景没区别（曾出现 +1.6% 的假激进档）
    lower_bound = max(base_top * 1.03, close * 1.08)

    candidates: List[Dict[str, Any]] = []
    for item in levels:
        if item["price"] < lower_bound:
            continue
        if item["price"] > upper * 1.005:
            break
        if candidates and item["price"] <= candidates[-1]["price"] * 1.02:
            continue
        candidates.append({"price": _rp(item["price"]), "label": item["label"], "kind": item["kind"]})
        if len(candidates) >= 2:
            break

    if len(candidates) < 2:
        for label, value in (
            ("250 日高点（前一轮高点，套牢盘与目标位重合）", _f(refs.get("high_250"))),
            ("整数关口（挂单与心理位聚集）", _round_level(upper * 0.97)),
        ):
            if not value or value < lower_bound or value > upper * 1.005:
                continue
            if any(abs(value - c["price"]) / max(c["price"], 0.01) < 0.015 for c in candidates):
                continue
            candidates.append({"price": _rp(value), "label": label, "kind": "前高"})
            if len(candidates) >= 2:
                break

    if not candidates:
        candidates.append({
            "price": _rp(lower_bound),
            "label": f"基线最高档之上，按估值上限 × 情绪溢价 {premium:+.0%} 递推",
            "kind": "情绪",
        })

    # 最后一档锚定上限：让"最多看到哪"有个明确数字
    top = _rp(upper)
    if all(abs(top - c["price"]) / max(c["price"], 0.01) > 0.02 for c in candidates):
        candidates.append({
            "price": top,
            "label": f"情绪上限 {top:.2f}（估值上限 {ceiling:.2f} × (1{premium:+.0%})）",
            "kind": "情绪",
        })

    targets: List[Dict[str, Any]] = []
    # 激进情景一共只卖 70%，留 30% 底仓跟移动止盈——鱼尾段靠"跟着跑"而不是"卖在顶上"
    trim_plans = {1: [50], 2: [35, 35], 3: [25, 25, 20]}
    trims = trim_plans.get(len(candidates)) or [25] * len(candidates)
    sold = sum(trims)
    for idx, cand in enumerate(candidates):
        trim = trims[idx] if idx < len(trims) else 0
        item: Dict[str, Any] = {
            "level": f"A{idx + 1}",
            "name": "激进档" if idx == 0 else ("情绪上限" if idx == len(candidates) - 1 else "激进加档"),
            "price": cand["price"],
            "pct_from_now": _pct(cand["price"], close),
            "trim_pct": trim,
            "basis": cand["label"],
        }
        if holding and cost > 0:
            item["pct_from_cost"] = _pct(cand["price"], cost)
        if shares:
            item["trim_shares"] = int(shares * trim / 100)
        targets.append(item)

    return {
        "key": "aggressive",
        "label": "激进（吃情绪鱼尾）",
        "assumption": (
            f"题材接力延续到{market.get('phase_label') or '亢奋'}阶段末端，"
            f"价格被推到估值上限之上 {max(0.0, premium) * 100:.0f}%"
        ),
        "trigger": "市场情绪处于主升/亢奋，且个股是接力核心（连板、板块同步涨停、量能持续放大）时启用",
        "targets": targets,
        "remaining_pct": 100 - sold,
        "note": (
            f"这一档不许用固定目标价止盈——鱼尾段没有顶，只有跌破才走。"
            f"打完还剩 {100 - sold}% 底仓跟移动止盈。"
        ),
    }


# ---------------------------------------------------------------------------
# 动态退出策略
# ---------------------------------------------------------------------------

def build_exit_policy(
    *,
    close: float,
    cost: float,
    holding: bool,
    shares: int,
    atr: float,
    bars: List[Dict[str, Any]],
    refs: Dict[str, Any],
    fair: Dict[str, Any],
    factors: Dict[str, Any],
    market: Dict[str, Any],
    stock_sent: Dict[str, Any],
    base_targets: List[Dict[str, Any]],
    base_stop: Dict[str, Any],
    scenarios: List[Dict[str, Any]],
    value_view: Optional[Dict[str, Any]] = None,
    sentiment_policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把「市场情绪 × 个股接力 × 因子结论」合成一个当前该执行的退出模式。

    返回的 mode 决定用户此刻该干什么，而不是等到某个价位才动作。
    """
    phase = str((market or {}).get("phase") or "unknown")
    phase_label = str((market or {}).get("phase_label") or "未知")
    temp = _f((market or {}).get("temperature")) or 0.5
    relay = bool((stock_sent or {}).get("relay"))
    streak = int((stock_sent or {}).get("limit_up_streak") or 0)
    zb_today = bool((stock_sent or {}).get("zb_today"))
    accelerating = bool((stock_sent or {}).get("accelerating"))
    sector_zt = int((stock_sent or {}).get("sector_zt") or 0)

    theme = (factors or {}).get("theme") or {}
    heat = float(theme.get("heat") or 0.0)
    theme_risk = float(theme.get("risk") or 0.0)
    growth_score = float(((factors or {}).get("growth") or {}).get("score") or 0.0)
    quality_score = float(((factors or {}).get("quality") or {}).get("score") or 0.0)
    val = (factors or {}).get("valuation") or {}
    pe_pct = _f(val.get("pe_pct"))
    pb_pct = _f(val.get("pb_pct"))
    repair = _f(fair.get("fair_repair"))

    # PE 高分位只有在 PB 也不便宜时才算"估值透支"。
    # 银行/保险/周期股经常出现「净利下滑 → PE 被动抬到历史高位，但 PB 在地板上」，
    # 这种是盈利周期问题而不是估值泡沫，按 PB 才是对的口径（中国平安就是典型：
    # PE 处 99% 分位、PB 却只有 11% 分位）。误判成清仓会把便宜资产割在最底部。
    pe_expensive = pe_pct is not None and float(pe_pct) >= 0.65
    pe_cheap_asset = pb_pct is not None and float(pb_pct) <= 0.25
    if pe_cheap_asset:
        pe_expensive = False

    closes = [_f(b.get("close")) for b in bars if _f(b.get("close"))]
    ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    recent_high = max(closes[-10:]) if len(closes) >= 10 else (max(closes) if closes else close)

    base_stop_price = _f((base_stop or {}).get("price")) or (close - 2 * atr)

    low20 = min(closes[-20:]) if len(closes) >= 20 else (min(closes) if closes else close)

    # --- 结构止损：跌破先减一半，不是清仓 ---
    # 均线在现价上方（下跌趋势）时不能用均线当结构位，改用近 20 日低点 / 1.5 倍 ATR
    soft_candidates: List[float] = []
    if ma20 and ma20 < close * 0.995:
        soft_candidates.append(ma20 * 0.985)
    if low20 and low20 < close * 0.995:
        soft_candidates.append(low20 * 0.99)
    soft_candidates.append(close - 1.5 * atr if atr else close * 0.94)
    soft_raw = max(soft_candidates)
    # 结构止损至少留 4% 缓冲：A 股日内 2% 的波动是常态，止损贴太紧等于随机出场
    soft_raw = min(soft_raw, close * 0.96)
    hard_stop = _rp(base_stop_price)
    # 结构止损必须比逻辑止损明显更远（至少 1.5%）才值得单独存在。
    # 逻辑止损已经很贴近现价时（保护浮盈的票），硬凑一条中间线只会变成噪音——
    # 那种情况就只给一条线，诚实地说"没有可用的结构位"。
    soft_stop: Optional[float] = None
    if soft_raw > hard_stop * 1.015 and soft_raw < close * 0.99:
        soft_stop = _rp(soft_raw)

    # --- 移动止盈：只有接力行情才用它，其它场景给了也是噪音 ---
    trail_base = [v for v in (ma5, (_f(recent_high) - 1.5 * atr) if atr else None) if v and v > 0]
    trail = max(trail_base) if trail_base else (close - 1.5 * atr if atr else close * 0.95)
    trail = min(trail, close * 0.995)
    # 结构止损可能不存在（技术位不可用），此时移动止盈只需高于逻辑止损
    trail_floor = (soft_stop or hard_stop) * 1.005
    trail = max(trail, trail_floor, close * 0.88)
    trail = _rp(min(trail, close * 0.995))

    soft_level = soft_stop if soft_stop else hard_stop
    soft_txt = f"{soft_level:.2f}"
    soft_desc = "结构位" if soft_stop else "逻辑止损"
    rules: List[Dict[str, str]] = []
    mode = "ladder"
    mode_label = "按阶梯执行"
    headline = ""
    keep_core_pct = 0
    trail_applies = False

    logic_broken = (
        (growth_score <= -0.6 and pe_expensive)
        or (growth_score <= -0.8 and quality_score <= -0.2 and not pe_cheap_asset)
        or (repair is not None and repair <= -0.25)
    )

    # ---------------- 低位价值视图：深跌但基本面未证伪 ----------------
    # 用户理念：越跌越兴奋；深跌必有反弹；基本面没变就会均值回归完成戴维斯双击；
    # 近一年没炒作过 = 位置不高 = 迟早有一波。
    # 但这套逻辑有三个前提，缺一不可：
    #   ① 基本面未被证伪（否则是价值陷阱，不是机会）
    #   ② 估值仍在相对低位（好公司价格太高也不能碰，安全边际是生存根本）
    #   ③ 大盘没有处在崩塌/退潮（退潮期低位照样能跌，先活命）
    vv = value_view or {}
    vv_hype = vv.get("hype") or {}
    vv_reb = vv.get("rebound") or {}
    vv_ctx = vv.get("context") or {}
    vv_margin = vv.get("margin_of_safety") or {}

    deep_value_candidate = bool(
        vv.get("ok")
        and vv_reb.get("is_deep_drop")
        and vv_reb.get("fundamentals_intact")
        and vv_reb.get("valuation_cheap")
    )
    market_not_collapsed = phase not in ("ebb", "collapse")
    # 板块没被炒过 + 个股位置低 = 筹码干净，反弹时阻力小
    clean_chips = (
        (vv_ctx.get("industry_hyped") is False)
        and (vv_hype.get("pos_pct") is not None and float(vv_hype.get("pos_pct") or 1) <= 0.35)
    )
    value_safety_ok = str(vv_margin.get("tier") or "") in ("充足", "较充足")

    if not holding:
        mode = "watch"
        mode_label = "观察（无持仓）"
        headline = "没有持仓成本，以下价位只作为多因子参考位，不构成交易指令。"
    elif logic_broken:
        mode = "exit_now"
        mode_label = "逻辑止损（清仓）"
        keep_core_pct = 0
        headline = (
            f"基本面或估值已被证伪（成长分 {growth_score:+.2f}、PE 分位 "
            f"{float(pe_pct or 0):.0%}），这种情况下不存在「等反弹」，"
            f"按现价分批退出，{hard_stop:.2f} 是最后一道线。"
        )
        rules.append({"if": "现价仍在成本上方", "then": "直接减到 1/3 以下，剩余挂 MA5 移动止盈"})
        rules.append({"if": f"跌破 {hard_stop:.2f}", "then": "全部清掉，不补不回本"})
    elif deep_value_candidate and market_not_collapsed and value_safety_ok:
        # ---- 低位均值回归：不在地板上割肉，等戴维斯双击 ----
        mode = "value_rebound"
        mode_label = "低位均值回归（越跌越兴奋，等基本面兑现）"
        keep_core_pct = 60
        _pos = float(vv_hype.get("pos_pct") or 0)
        _dd = float(vv_reb.get("deep_drop") or 0)
        _dormant = bool(vv_hype.get("dormant"))
        _davis = (vv_reb.get("davis_double_play") or {}).get("double_play_ready")
        _span = int(vv_hype.get("window_days") or 250)
        headline = (
            f"这只票已从区间高点回撤 {abs(_dd):.0f}%，现价处在近 {_span} 日区间的 {_pos:.0%} 低位，"
            f"且基本面未被证伪、估值仍在历史低位——这正是「深跌必有反弹」和均值回归成立的条件。"
        )
        if _dormant:
            headline += (
                f"而且近一年基本没有炒作过（{vv_hype.get('hype_level')}，"
                f"距最后一次大涨已 {vv_hype.get('days_since_last_surge')} 个交易日），"
                f"缺乏炒作意味着上方套牢盘轻、筹码没充分换手，未来迟早有一波。"
            )
        if _davis:
            headline += "戴维斯双击条件初步具备（业绩未恶化 + 估值处低位），等的是盈利与估值共振。"
        else:
            headline += "戴维斯双击尚需盈利拐点确认，先按持有观察处理，不加仓、不摊平。"
        headline += (
            f"结论：现在不是割肉的位置。保留 {keep_core_pct}% 底仓，"
            f"把卖出条件从「价格」换成「基本面是否被证伪」。"
        )
        rules.append({
            "if": "基本面没有实质变化（财报/经营未恶化）",
            "then": f"持有底仓不做卖出动作，等均值回归；反弹到成本附近先减 1/3 摊薄心理成本",
        })
        rules.append({
            "if": f"跌破 {hard_stop:.2f} 且同时出现基本面证伪（业绩下滑/逻辑改变）",
            "then": "这时才按逻辑止损离场——低位不割肉的前提是基本面还在，不是不认输",
        })
        rules.append({
            "if": "大盘进入退潮或崩塌（phase=ebb/collapse）",
            "then": "放弃均值回归的假设，转入退潮防御：先减到半仓以下，活着比赚反弹重要",
        })
        rules.append({
            "if": "板块开始被炒作（连续大涨、放量突破）",
            "then": "不追高不加仓；底仓持有，把减仓动作放到情绪亢奋期而不是启动初期",
        })
    elif phase in ("ebb", "collapse"):
        mode = "defend"
        mode_label = "退潮防御（快速降仓）"
        keep_core_pct = 10 if quality_score >= 0.2 else 0
        headline = (
            f"市场已进入{phase_label}（温度 {temp:.0%}），题材股的下跌往往三天就跌掉一个月的涨幅。"
            f"这时候不要把「反弹再卖」当成计划——先按现价减到半仓以下，剩下的用 {soft_txt} 兜底。"
        )
        rules.append({"if": "开盘或反弹即减", "then": "减到 50% 以下，不等目标价"})
        rules.append({"if": f"跌破 {soft_txt}（{soft_desc}）", "then": "再减一半，剩余只留底仓观察"})
        rules.append({"if": "涨停家数/最高连板回升到主升水平", "then": "才考虑把减掉的仓位买回，否则不加"})
    elif relay and phase in ("advance", "euphoria"):
        mode = "ride"
        mode_label = "接力跟随（不主动止盈）"
        keep_core_pct = 30
        trail_applies = True
        streak_bit = f"，已 {streak} 连板" if streak >= 2 else ""
        headline = (
            f"这是典型的接力行情{streak_bit}，板块今日 {sector_zt} 家涨停。"
            f"这种位置**不要按目标价止盈**——涨的时候没有顶，唯一正确的做法是跟着移动止盈跑："
            f"跌破 {trail:.2f}（MA5 或近 10 日高点 -1.5ATR）才减第一批，"
            f"剩余 30% 底仓留到跌破 {soft_txt} 再动。"
        )
        rules.append({"if": "价格继续上台阶", "then": f"止盈线随之上移到新的 MA5，只上移不下移"})
        agg_t1 = None
        for sc in scenarios or []:
            if sc.get("key") == "aggressive":
                ts_list = sc.get("targets") or []
                if ts_list:
                    agg_t1 = _f(ts_list[0].get("price"))
                break
        if agg_t1:
            rules.append({
                "if": f"涨到激进档第一档 {agg_t1:.2f}",
                "then": "减 30% 落袋（这一段涨幅已经吃到，先兑现一部分），剩余继续跟移动止盈——"
                        "既不错过后面的鱼尾，也不至于坐一趟电梯回去",
            })
        rules.append({"if": f"收盘跌破 {trail:.2f}", "then": "减 1/3（接力节奏放缓，先落一部分）"})
        rules.append({"if": "炸板 / 连板断板", "then": "立刻再减 1/3，不等反弹"})
        rules.append({"if": f"跌破 {soft_txt}", "then": "底仓也走，这波结束"})
        rules.append({"if": "板块涨停家数减半", "then": "视为题材退潮的第一信号，主动降到半仓"})
    elif zb_today or (heat >= 0.65 and theme_risk >= 0.45):
        mode = "trim_into_strength"
        mode_label = "高位转弱（逢高减）"
        keep_core_pct = 20
        trail_applies = True
        reason = "今日炸板" if zb_today else f"情绪热度 {heat:.0%} 但退潮风险已达 {theme_risk:.0%}"
        headline = (
            f"{reason}，说明接力资金开始不够了。这种时候不需要等目标价，"
            f"每一次冲高都是减仓机会——先减 1/3，剩下的把止盈上移到 {trail:.2f}。"
        )
        rules.append({"if": "盘中冲高到前一日高点上方", "then": "减 1/3，不追"})
        rules.append({"if": f"跌破 {trail:.2f}", "then": "再减 1/3"})
        rules.append({"if": "重新涨停且封板资金明显放大", "then": "可暂停减仓，但止盈线不再下移"})
    elif accelerating and heat >= 0.5:
        mode = "ride"
        mode_label = "加速段跟随"
        keep_core_pct = 30
        trail_applies = True
        headline = (
            f"个股在加速段（5 日涨 {stock_sent.get('ret5')}%、乖离 {stock_sent.get('bias20')}%），"
            f"但板块联动一般。加速段波动大，用 {trail:.2f} 跟，跌破再减 1/3，不要在加速途中主动卖。"
        )
        rules.append({"if": "单日涨幅超过 7%", "then": "不减，止盈线直接上移到当日低点"})
        rules.append({"if": f"跌破 {trail:.2f}", "then": f"减 1/3，剩余看 {soft_txt}"})
    else:
        mode = "ladder"
        mode_label = "按阶梯执行"
        keep_core_pct = 0
        t1 = (base_targets[0] if base_targets else None) or {}
        t1_price = _f(t1.get("price")) or close * 1.08
        headline = (
            f"没有情绪接力，也没有明显退潮，按正常阶梯走：到 {t1_price:.2f} 减第一档，"
            f"跌破 {soft_txt} 减半，两者之间不动。"
        )
        rules.append({"if": f"涨到 {t1_price:.2f}", "then": f"减 {t1.get('trim_pct') or 30}%"})
        rules.append({"if": f"跌破 {soft_txt}", "then": "减 50%（结构走坏，不是清仓）"})
        if soft_stop:
            rules.append({"if": f"跌破 {hard_stop:.2f}", "then": "剩余全部清掉（逻辑止损）"})
        else:
            rules.append({"if": f"连续两个交易日收在 {hard_stop:.2f} 下方",
                          "then": "剩余全部清掉（不是一天破位就砍，但也别一直等回本）"})

    # 鱼尾提醒：情绪高位但个股不在核心位，是最容易被"情绪"骗进去的位置
    warnings: List[str] = []
    if phase == "euphoria" and not relay and heat < 0.4:
        warnings.append(
            "市场亢奋但这只票不在接力核心位——这种错位最危险：情绪好的时候它不涨，"
            "情绪一退它跟着跌。不要因为「大盘很热」而放宽这只票的止盈纪律"
        )
    if phase == "euphoria" and relay:
        warnings.append(
            "情绪已到亢奋区，鱼尾段的特征是涨得快、跌得更快。可以跟着吃，"
            "但止盈线必须每天上移，且一旦炸板/断板立刻执行，不要重新商量"
        )
    if not relay and phase in ("advance", "euphoria") and heat < 0.35:
        warnings.append("市场在涨但个股量能不跟，说明资金没选它，目标价不宜按激进情景看")

    # 情绪仓位纪律（用户理念：低情绪留现金建仓、高情绪减仓兑现、绝对不参与博傻）
    sp = sentiment_policy or {}
    if sp.get("ok"):
        _sp_regime = str(sp.get("regime") or "")
        _sp_label = str(sp.get("regime_label") or "")
        _sp_pos_max = float(sp.get("target_position_max") or 0)
        _sp_pos_min = float(sp.get("target_position_min") or 0)
        if _sp_regime == "euphoria_trim":
            warnings.append(
                f"当前市场处于「{_sp_label}」（情绪温度 {float(sp.get('temperature') or 0):.0%}）："
                "亢奋期的涨幅是别人接力给的，不是自己赚的——这条票不追高、不加仓，"
                "浮盈优先兑现落袋（不参与博傻游戏）"
            )
        elif _sp_regime in ("panic_defend", "risk_off"):
            warnings.append(
                f"当前市场处于「{_sp_label}」：情绪转弱是撤退窗口而非抄底窗口——"
                "撤出接力与题材仓、保留价值底仓、现金为王；"
                "这个阶段要做的不是加仓，是活下来"
            )
        elif _sp_regime in ("cold_accumulate", "recovery_build"):
            warnings.append(
                f"当前市场处于「{_sp_label}」：赚钱效应正在修复，是留现金分批建仓的窗口"
                f"（目标总仓位 {_sp_pos_min:.0%}~{_sp_pos_max:.0%}）——"
                "只买位置低且有基本面支撑的，不满仓、不追高"
            )
        else:
            warnings.append(
                f"市场处于「{_sp_label}」，目标总仓位 {_sp_pos_min:.0%}~{_sp_pos_max:.0%}"
                f"（含大盘位置叠加：{sp.get('market_overlay') or '中性'}）"
            )

    # 低位价值视角的补充提醒（用户理念：越跌越兴奋，但好股太贵不能碰）
    if vv.get("ok"):
        _hype_score = float(vv_hype.get("hype_score") or 0)
        _pos_now = float(vv_hype.get("pos_pct") or 0)
        if _hype_score >= 0.42 and _pos_now >= 0.6:
            warnings.append(
                f"这只票近一年已被明显炒作（{vv_hype.get('hype_level')}，炒作度 {_hype_score:.0%}）"
                f"且现价处在区间 {_pos_now:.0%} 高位——即便基本面不差，安全边际也不足，"
                f"「好股价格太高也不能碰」，这种位置应把减仓放在前面"
            )
        if vv_hype.get("dormant") and _pos_now <= 0.4:
            _since = vv_hype.get("days_since_last_surge")
            _since_txt = (
                f"距最后一次大涨已 {_since} 个交易日"
                if _since is not None else "近一年从未出现单日大涨 ≥9% 的爆发"
            )
            warnings.append(
                f"近一年缺乏炒作（{_since_txt}）"
                f"且现价在区间 {_pos_now:.0%} 低位——从人性和筹码结构看，"
                f"这类标的未来迟早有一波，属于「等得起」而不是「等不及」"
            )
        if vv_reb.get("verdict") == "value_trap_risk":
            warnings.append(
                "深跌同时基本面已走弱：这不是均值回归机会而是价值陷阱，"
                "低位本身不构成买入或持有的理由"
            )
        if vv_ctx.get("sentiment_phase") in ("ebb", "collapse") and deep_value_candidate:
            warnings.append(
                "虽然满足低位均值回归条件，但当前市场处于退潮/崩塌阶段——"
                "低位在退潮期照样能深跌，先活命再谈均值回归"
            )

    return {
        "mode": mode,
        "mode_label": mode_label,
        "headline": headline,
        "rules": rules,
        "value_view": vv,
        "hard_stop": {
            "price": hard_stop,
            "pct_from_now": _pct(hard_stop, close),
            "meaning": "逻辑止损：基本面/估值被证伪时才用，跌到这里不再讨论",
        },
        "soft_stop": {
            "price": soft_stop,
            "pct_from_now": _pct(soft_stop, close) if soft_stop else None,
            "available": soft_stop is not None,
            "meaning": (
                "结构止损：跌破关键均线先减一半，留一半看能不能修复，不是清仓信号"
                if soft_stop else
                "当前没有可靠的技术结构位（均线都在上方且近期低点过远），只有一条逻辑止损线，"
                "跌破就减半观察，连续两个交易日收不回再清仓"
            ),
        },
        "trailing_stop": {
            "price": _rp(trail),
            "pct_from_now": _pct(trail, close),
            "applies": trail_applies,
            "basis": "MA5 与「近 10 日高点 - 1.5 ATR」取高者，随价格上移、永不下移",
            "meaning": (
                "接力行情唯一正确的止盈方式：跟着涨，不预测顶"
                if trail_applies else
                "当前不是接力行情，移动止盈不适用——用结构止损与阶梯目标就够了"
            ),
        },
        "keep_core_pct": keep_core_pct,
        "keep_core_note": (
            f"保留 {keep_core_pct}% 底仓不参与止盈，只在本模式被证伪（跌破止损线或情绪转为退潮）时处理"
            if keep_core_pct else "不保留底仓，按规则清干净"
        ),
        "warnings": warnings,
        "sentiment": {
            "market_phase": phase,
            "market_phase_label": phase_label,
            "market_temperature": round(temp, 3),
            "stock_verdict": (stock_sent or {}).get("verdict"),
            "relay": relay,
            "limit_up_streak": streak,
            "sector_zt": sector_zt,
        },
    }


# ---------------------------------------------------------------------------
# 对外主函数
# ---------------------------------------------------------------------------

def build_scenarios(
    *,
    close: float,
    cost: float,
    holding: bool,
    shares: int,
    atr: float,
    bars: List[Dict[str, Any]],
    levels: List[Dict[str, Any]],
    refs: Dict[str, Any],
    fair: Dict[str, Any],
    factors: Dict[str, Any],
    market: Dict[str, Any],
    stock_sent: Dict[str, Any],
    base_targets: List[Dict[str, Any]],
    base_stop: Dict[str, Any],
    value_view: Optional[Dict[str, Any]] = None,
    sentiment_policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """生成三套情景 + 当前退出策略。"""
    premium, premium_notes = sentiment_premium(market or {}, stock_sent or {})

    conservative = _build_conservative(
        close=close, cost=cost, holding=holding, atr=atr,
        refs=refs, fair=fair, levels=levels, base_targets=base_targets, shares=shares,
    )
    base = {
        "key": "base",
        "label": "基线（多因子正常路径）",
        "assumption": "估值中枢按当前多因子中枢兑现，技术位正常发挥",
        "trigger": "没有强情绪驱动、也没有退潮信号时的默认方案",
        "targets": base_targets,
        "remaining_pct": 0,
        "note": "三档打完清仓。这是「正常情况」的参考答案，不是最优解。",
    }
    aggressive = _build_aggressive(
        close=close, cost=cost, holding=holding, atr=atr,
        refs=refs, fair=fair, levels=levels, base_targets=base_targets,
        shares=shares, premium=premium, market=market or {}, stock_sent=stock_sent or {},
    )

    scenarios = [conservative, base, aggressive]

    # 选当前最该参照的那套：情绪接力 → 激进；退潮/透支 → 保守；其余基线
    phase = str((market or {}).get("phase") or "unknown")
    relay = bool((stock_sent or {}).get("relay"))
    repair = _f(fair.get("fair_repair"))
    if relay and phase in ("advance", "euphoria"):
        active = "aggressive"
        active_reason = "个股在接力核心且市场情绪处于扩张期，当前该看激进情景并配移动止盈"
    elif phase in ("ebb", "collapse") or (repair is not None and repair <= -0.08):
        active = "conservative"
        active_reason = "市场退潮或估值已透支，当前该看保守情景，先保利润/保本金"
    else:
        active = "base"
        active_reason = "无强情绪驱动也无退潮信号，按基线阶梯执行"
    for item in scenarios:
        item["is_active"] = item["key"] == active

    exit_policy = build_exit_policy(
        close=close, cost=cost, holding=holding, shares=shares, atr=atr, bars=bars,
        refs=refs, fair=fair, factors=factors, market=market or {},
        stock_sent=stock_sent or {}, base_targets=base_targets,
        base_stop=base_stop, scenarios=scenarios,
        value_view=value_view or {},
        sentiment_policy=sentiment_policy or {},
    )

    return {
        "scenarios": scenarios,
        "active": active,
        "active_reason": active_reason,
        "exit_policy": exit_policy,
        "sentiment_premium": round(premium, 4),
        "sentiment_notes": premium_notes,
    }
