"""交易行为诊断：把「已经发生的成交」和「市场实际走势」对照，找出打法的漏洞。

为什么单独一个模块，而不是塞进 agent 的通用对话
------------------------------------------------
agent 能读数字，但**没有能力做反事实计算**：它没法自己去拉每笔卖出之后 20 个
交易日的行情，也不会主动去算「如果当时止损设得更紧会省下多少钱」。
而这几件事恰恰是诊断的全部价值。

更现实的原因是可复现：如果让模型自由发挥，同一个账户问两次可能得到两套结论，
用户没法验证、也没法在不同时间点比较改进效果。这里把口径写死在代码里，
每次诊断都按同一套规则算，数字对得上才谈得上「改进」。

四项诊断，各自回答一个具体问题
--------------------------------
1. **出场是否偏早**：卖出之后继续涨了多少？卖早了的比例有多高？
2. **是否截断利润、放任亏损**：盈利单和亏损单分别持有了多久？
3. **止损有没有真的执行**：亏损卖出的幅度分布，有多少笔明显该更早砍。
4. **亏损是否过度集中**：是不是一只票/一个月吃掉了大半亏损。

另外给一条反事实测算：把止损线收紧到某个位置，今年的已实现亏损会变成多少。
⚠️ 反事实是简化估算（假设止损单能成交、忽略滑点与后续反弹），必须标注清楚，
它只用来判断「方向对不对」，不能当成「照做就能省这么多」。
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


def _f(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    return round(float(value), digits) if value is not None else None


# ---------------- 行情辅助 ----------------

def _load_bar_index(sec_ids: Sequence[str], start_date: str) -> Dict[str, Dict[str, Any]]:
    """给每只票取一次日线，建「日期 → 下标」索引。按票去重，避免一票一请求几百次。"""
    from .stock_service import stock_service

    out: Dict[str, Dict[str, Any]] = {}
    for sec in dict.fromkeys([s for s in sec_ids if s]):
        try:
            bars = stock_service.get_k_line_data(sec, start_date=start_date,
                                                 frequency="d", caller="trade_diagnosis")
        except Exception as exc:  # noqa: BLE001
            logger.warning("diagnosis_kline_failed sec=%s error=%r", sec, exc)
            bars = []
        if not bars:
            out[sec] = {"bars": [], "index": {}}
            continue
        out[sec] = {
            "bars": bars,
            "index": {str(b.get("date"))[:10]: i for i, b in enumerate(bars)},
        }
    return out


def _forward_return(bar_map: Dict[str, Any], sec: str, trade_date: str,
                    price: float, days: int) -> Optional[float]:
    """从卖出日往后数 days 个交易日，返回相对卖出价的涨跌幅（%）。"""
    node = bar_map.get(sec) or {}
    bars = node.get("bars") or []
    idx = (node.get("index") or {}).get(str(trade_date)[:10])
    if idx is None or not price:
        return None
    target = idx + days
    if target >= len(bars):
        return None
    try:
        return (float(bars[target]["close"]) - price) / price * 100.0
    except (TypeError, ValueError, KeyError):
        return None


def _max_favorable(bar_map: Dict[str, Any], sec: str, trade_date: str,
                   price: float, days: int) -> Optional[float]:
    """卖出后 days 个交易日内的最高价相对卖出价的涨幅（%）——「本来还能赚多少」。"""
    node = bar_map.get(sec) or {}
    bars = node.get("bars") or []
    idx = (node.get("index") or {}).get(str(trade_date)[:10])
    if idx is None or not price:
        return None
    window = bars[idx + 1: idx + 1 + days]
    highs = []
    for bar in window:
        value = _f(bar.get("high"))
        if value:
            highs.append(value)
    if not highs:
        return None
    return (max(highs) - price) / price * 100.0


# ---------------- 持有期（FIFO 配对） ----------------

def _fifo_holding_days(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 FIFO 把卖出和买入配对，算每笔卖出的加权持有天数。

    流水可能不从建仓开始（2026 年之前就有仓位），配不上的卖出直接跳过——
    用「假设持有 0 天」去凑数会把持有期结论整体拉偏。
    """
    from collections import defaultdict, deque

    lots: Dict[str, deque] = defaultdict(deque)
    out: List[Dict[str, Any]] = []
    ordered = sorted([r for r in records if r.get("side") in ("buy", "sell")],
                     key=lambda r: (str(r.get("trade_date") or ""), str(r.get("id") or "")))

    def to_date(text: Any) -> Optional[datetime.date]:
        try:
            return datetime.date.fromisoformat(str(text)[:10])
        except (TypeError, ValueError):
            return None

    for rec in ordered:
        sec = str(rec.get("secID") or "")
        shares = int(_f(rec.get("shares"), 0) or 0)
        day = to_date(rec.get("trade_date"))
        if not sec or shares <= 0 or day is None:
            continue
        if rec.get("side") == "buy":
            cost = _f(rec.get("price")) or _f(rec.get("cost_after")) or 0.0
            lots[sec].append({"shares": shares, "date": day, "price": cost})
            continue

        pnl = rec.get("realized_pnl")
        remaining = shares
        matched: List[Tuple[int, datetime.date, float]] = []
        while remaining > 0 and lots[sec]:
            lot = lots[sec][0]
            take = min(remaining, lot["shares"])
            matched.append((take, lot["date"], lot["price"]))
            lot["shares"] -= take
            remaining -= take
            if lot["shares"] <= 0:
                lots[sec].popleft()
        if not matched:
            # 没有对应买入 → 流水起点之前的老仓位，持有期不可知，跳过
            continue
        total = sum(m[0] for m in matched) or 1
        weighted_days = sum(m[0] * (day - m[1]).days for m in matched) / total
        out.append({
            "secID": sec,
            "name": rec.get("name"),
            "trade_date": str(rec.get("trade_date"))[:10],
            "shares": sum(m[0] for m in matched),
            "holding_days": round(weighted_days, 1),
            "realized_pnl": _f(pnl),
        })
    return out


# ---------------- 诊断主体 ----------------

def diagnose(since: Optional[str] = None,
             stop_pct: float = 8.0,
             forward_days: int = 20) -> Dict[str, Any]:
    """跑一遍完整诊断。``stop_pct`` 用于反事实测算，默认 -8%。"""
    from .trade_ledger import trade_ledger

    records = trade_ledger.list_trades(limit=20000, since=since)
    if not records:
        return {"ok": True, "empty": True,
                "hint": "台账里还没有成交记录，先同步账本：sync_trade_ledger()"}

    sells = [r for r in records if r.get("side") == "sell"]
    judged = [r for r in sells if r.get("realized_pnl") is not None]
    dates = [str(r.get("trade_date") or "")[:10] for r in records if r.get("trade_date")]
    start_date = min(dates) if dates else "2025-01-01"
    # 往前多取 60 个自然日，保证卖出日必定落在 K 线窗口里
    lookback = (datetime.date.fromisoformat(start_date)
                - datetime.timedelta(days=60)).isoformat()

    bar_map = _load_bar_index([r.get("secID") for r in sells], lookback)

    findings: List[Dict[str, Any]] = []
    actions: List[Dict[str, Any]] = []

    # ---- 1. 出场时机（必须按「卖出时是盈是亏」分组） ----
    # ⚠️ 混算会产生统计陷阱：盈利单卖掉后继续涨（卖早）会与亏损单卖掉后继续跌
    # （卖对了）互相抵消，得出「整体出场还行」的假象，恰好掩盖两个相反的真问题。
    # 「抗单」和「拿不住」必须分开归因，否则改错了方向。
    fwd_values: List[float] = []
    mfe_values: List[float] = []
    win_fwd: List[float] = []
    win_mfe: List[float] = []
    loss_fwd: List[float] = []
    loss_mfe: List[float] = []
    win_missed_amount = 0.0   # 盈利单卖出后 20 日最高价折算的「少赚金额」（事后视角）
    loss_avoided_amount = 0.0  # 亏损单卖出后继续下跌折算的「躲过的金额」
    premature = 0
    good_exit = 0
    for rec in sells:
        sec = str(rec.get("secID") or "")
        day = str(rec.get("trade_date") or "")[:10]
        price = _f(rec.get("price"))
        if not sec or not day or not price:
            continue
        fwd = _forward_return(bar_map, sec, day, price, forward_days)
        mfe = _max_favorable(bar_map, sec, day, price, forward_days)
        pnl = _f(rec.get("realized_pnl"))
        is_win = pnl is not None and pnl > 0
        amount = price * int(_f(rec.get("shares"), 0) or 0)
        if fwd is not None:
            fwd_values.append(fwd)
            (win_fwd if is_win else loss_fwd).append(fwd)
            if fwd > 5:
                premature += 1
            elif fwd < 0:
                good_exit += 1
        if mfe is not None:
            mfe_values.append(mfe)
            (win_mfe if is_win else loss_mfe).append(mfe)
            if amount > 0:
                if is_win and mfe > 0:
                    win_missed_amount += amount * mfe / 100.0
                elif not is_win and fwd is not None and fwd < 0:
                    loss_avoided_amount += amount * (-fwd) / 100.0

    exit_analysis: Dict[str, Any] = {"sample": len(fwd_values)}
    if fwd_values:
        avg_fwd = sum(fwd_values) / len(fwd_values)
        avg_mfe = sum(mfe_values) / len(mfe_values) if mfe_values else None
        premature_ratio = premature / len(fwd_values)
        exit_analysis.update({
            "avg_forward_pct": _round(avg_fwd),
            "avg_max_favorable_pct": _round(avg_mfe),
            "premature_count": premature,
            "premature_ratio": _round(premature_ratio, 3),
            "good_exit_ratio": _round(good_exit / len(fwd_values), 3),
            # 分组口径：这两个数才是归因依据，上面整体平均只作对照
            "win_sample": len(win_fwd),
            "loss_sample": len(loss_fwd),
            "win_avg_forward_pct": _round(sum(win_fwd) / len(win_fwd)) if win_fwd else None,
            "win_avg_mfe_pct": _round(sum(win_mfe) / len(win_mfe)) if win_mfe else None,
            "loss_avg_forward_pct": _round(sum(loss_fwd) / len(loss_fwd)) if loss_fwd else None,
            "loss_avg_mfe_pct": _round(sum(loss_mfe) / len(loss_mfe)) if loss_mfe else None,
            "win_missed_amount": _round(win_missed_amount),
            "loss_avoided_amount": _round(loss_avoided_amount),
        })

        win_avg = exit_analysis["win_avg_forward_pct"]
        win_mfe_avg = exit_analysis["win_avg_mfe_pct"]
        loss_avg = exit_analysis["loss_avg_forward_pct"]
        loss_mfe_avg = exit_analysis["loss_avg_mfe_pct"]

        # ① 盈利单：必须分清两种完全不同的情形，否则会把人误导成「死拿」
        #    A. win_avg > 0 → 卖出后续还在涨，真的是卖早了
        #    B. win_avg <= 0 但 mfe 高 → 卖点其实很准，只是「一笔清仓」吃不到冲高段。
        #       这种情况改成死拿会更差（20 日后是跌的），只能改「分批」。
        if win_fwd and win_avg is not None:
            if win_avg > 2:
                findings.append({
                    "key": "winners_sold_too_early",
                    "severity": "high",
                    "title": "盈利单卖太早（拿不住）",
                    "evidence": (f"{len(win_fwd)} 笔盈利卖出，卖出后 {forward_days} 个交易日"
                                 f"平均还涨 {win_avg:.2f}%"
                                 + (f"，期间最高平均还有 {win_mfe_avg:.2f}% 的空间" if win_mfe_avg else "")),
                    "advice": ("盈利单不该一笔清仓：改用移动止盈（从最高点回撤 X% 才卖），"
                               "或分批卖（+10% 卖 1/3、+20% 卖 1/3、剩余跟移动止盈）。"),
                })
                actions.append({
                    "area": "exits",
                    "change": "盈利单改移动止盈 + 分批止盈",
                    "target": "不再一有浮盈就整笔卖出，按最高点回撤触发",
                    "expected": (f"事后视角上限约 {win_missed_amount:,.0f} 元"
                                 f"（卖出后 {forward_days} 日最高价口径，不是可实现收益）"),
                    "how_to_verify": "下次诊断看 win_avg_forward_pct 是否回落",
                })
            elif (win_mfe_avg or 0) > 8:
                findings.append({
                    "key": "winners_exit_right_but_all_at_once",
                    "severity": "medium",
                    "title": "盈利单：卖点是对的，问题是一笔清仓",
                    "evidence": (f"{len(win_fwd)} 笔盈利卖出，卖出后 {forward_days} 个交易日"
                                 f"平均反而跌 {abs(win_avg):.2f}%——说明卖点选得准；"
                                 f"但同期最高还平均冲高 {win_mfe_avg:.2f}%，"
                                 f"这段没吃到（事后折算约 {win_missed_amount:,.0f} 元）"),
                    "advice": ("⚠️ 别把这条读成「要拿更久」：如果一直不卖，20 日后是亏 {:.1f}%。"
                               "正确的改法是「分批」而不是「死拿」——先卖一半锁住利润，"
                               "剩下一半挂移动止盈去吃冲高段。").format(abs(win_avg)),
                })
                actions.append({
                    "area": "exits",
                    "change": "盈利单改「分批卖」而非一次性清仓",
                    "target": "首笔止盈时只卖 1/2~1/3，剩余用移动止盈跟",
                    "expected": ("既保留当前卖点的准确率，又能吃到部分冲高段"
                                 f"（上限约 {win_missed_amount:,.0f} 元，事后口径）"),
                    "how_to_verify": "下次诊断看 win_avg_mfe_pct 与 win_avg_forward_pct 的差值是否收窄",
                })

        # ② 亏损单卖出后继续跌 → 卖点本身不差，代价全在「拖到那时候才卖」
        if loss_fwd and loss_avg is not None and loss_avg < -2:
            findings.append({
                "key": "loss_exit_late_not_wrong",
                "severity": "high",
                "title": "亏损单：卖点不差，只是拖得太久",
                "evidence": (f"{len(loss_fwd)} 笔亏损卖出，卖出后 {forward_days} 个交易日"
                             f"平均继续跌 {abs(loss_avg):.2f}%，说明卖出这个动作本身是对的；"
                             f"问题在于已经亏了一大截才卖"),
                "advice": ("别去优化「怎么卖得更好」——卖点已经对了。要改的是「什么时候必须卖」："
                           "把止损前置成条件单，触线自动走，不给自己「再看看」的机会。"),
            })
            actions.append({
                "area": "risk",
                "change": "止损前置为条件单（而非盘中判断）",
                "target": "买入即挂止损，触线自动执行",
                "expected": "亏损单的平均持有天数向盈利单靠拢，深亏笔数归零",
                "how_to_verify": "下次诊断看 avg_holding_days_loss 与 deep_loss_ratio",
            })

        # ③ 抗单的心理根源：亏损卖出后仍平均反弹一大截，正是「再等等会回来」被强化的原因
        #    ——间歇性强化最难戒。数据上它只是诱多（20 日后净跌），必须用规则对抗直觉。
        if loss_fwd and (loss_mfe_avg or 0) > 6 and loss_avg is not None and loss_avg < 0:
            findings.append({
                "key": "bounce_traps_holding",
                "severity": "medium",
                "title": "「再等等会反弹」是被真实数据强化的错觉",
                "evidence": (f"{len(loss_fwd)} 笔亏损卖出后，{forward_days} 个交易日内"
                             f"最高平均还有 {loss_mfe_avg:.2f}% 的反弹，"
                             f"但到第 {forward_days} 日净收益是 {loss_avg:.2f}%"),
                "advice": ("深跌确实常有反弹，这让你每次扛单都能「验证」自己是对的——"
                           "但反弹之后是继续跌。这种间歇性强化最难戒，靠意志力没用，"
                           "只能靠事前挂好的条件单，让它在你情绪介入之前就成交。"),
            })

        if not findings:
            findings.append({
                "key": "exit_timing_ok",
                "severity": "ok",
                "title": "出场时机无明显偏差",
                "evidence": f"只有 {premature} 笔卖出后继续大涨，比例 {premature_ratio:.0%}",
                "advice": "出场不是当前主要问题，把注意力放在入场和仓位",
            })

    # ---- 2. 持有期结构 ----
    holding = _fifo_holding_days(records)
    win_lots = [h for h in holding if (_f(h.get("realized_pnl")) or 0) > 0]
    loss_lots = [h for h in holding if (_f(h.get("realized_pnl")) or 0) < 0]
    win_days = [h["holding_days"] for h in win_lots]
    loss_days = [h["holding_days"] for h in loss_lots]
    hold_analysis: Dict[str, Any] = {
        "matched": len(holding),
        "win_sample": len(win_days),
        "loss_sample": len(loss_days),
    }

    # 「抗单」的直接量化：亏损单按持有天数分桶，看亏损压在哪个区间
    # ⚠️ 用中位数而不只是平均值——一两笔拿了三年的老仓会把均值拉到没有意义。
    def _median(values: List[float]) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    buckets = [(0, 15), (15, 30), (30, 60), (60, 120), (120, 10 ** 6)]
    loss_buckets = []
    for low, high in buckets:
        picked = [h for h in loss_lots if low <= h["holding_days"] < high]
        if not picked:
            continue
        label = (f"{low}~{high}天" if high < 10 ** 6 else f">{low}天")
        loss_buckets.append({
            "bucket": label,
            "count": len(picked),
            "pnl": _round(sum(_f(h.get("realized_pnl")) or 0 for h in picked)),
        })
    if loss_buckets:
        hold_analysis["loss_buckets"] = loss_buckets
        hold_analysis["loss_days_median"] = _round(_median(loss_days), 1)
    if win_days:
        hold_analysis["win_days_median"] = _round(_median(win_days), 1)

    if win_days and loss_days:
        avg_win_days = sum(win_days) / len(win_days)
        avg_loss_days = sum(loss_days) / len(loss_days)
        hold_analysis.update({
            "avg_holding_days_win": _round(avg_win_days, 1),
            "avg_holding_days_loss": _round(avg_loss_days, 1),
            "ratio": _round(avg_loss_days / avg_win_days, 2) if avg_win_days else None,
        })
        if avg_loss_days > avg_win_days * 1.3:
            stale = [h for h in loss_lots if h["holding_days"] >= 60]
            stale_pnl = sum(_f(h.get("realized_pnl")) or 0 for h in stale)
            findings.append({
                "key": "cut_winners_ride_losers",
                "severity": "high",
                "title": "截断利润、放任亏损（抗单）",
                "evidence": (f"盈利单平均持有 {avg_win_days:.0f} 天，"
                             f"亏损单平均持有 {avg_loss_days:.0f} 天，"
                             f"亏损单持有时长是盈利单的 {avg_loss_days/avg_win_days:.1f} 倍"
                             + (f"；其中持有超 60 天的亏损单 {len(stale)} 笔，"
                                f"合计 {stale_pnl:,.0f} 元" if stale else "")),
                "advice": "这是盈亏比被压到 1 以下最直接的原因：赚了就跑、亏了死扛。"
                          "先改这条，比换股票管用",
            })
            actions.append({
                "area": "holding",
                "change": "给亏损单设硬性时间止损",
                "target": "持有超过 N 天仍未创新高就减半（N 取盈利单均持天数附近）",
                "expected": f"把持仓天数分布拉回对称（目标 {avg_win_days:.0f} 天量级）",
                "how_to_verify": "下次诊断看 avg_holding_days_loss / avg_holding_days_win 是否降到 1.0 附近",
            })

    # ---- 3. 止损执行（反事实） ----
    loss_sells = [r for r in judged if (_f(r.get("realized_pnl")) or 0) < 0]
    actual_loss = sum(_f(r.get("realized_pnl")) or 0 for r in loss_sells)
    deep = 0
    hypothetical = 0.0
    covered = 0
    for rec in loss_sells:
        cost = _f(rec.get("cost_before"))
        price = _f(rec.get("price"))
        shares = int(_f(rec.get("shares"), 0) or 0)
        pnl = _f(rec.get("realized_pnl")) or 0.0
        fee = _f(rec.get("fee")) or 0.0
        if not cost or not price or shares <= 0:
            continue
        drop_pct = (price - cost) / cost * 100.0
        covered += 1
        if drop_pct <= -15:
            deep += 1
        if drop_pct < -stop_pct:
            # 假设在 -stop_pct 处止损：亏损 = 成本 × 股数 × stop_pct + 费用
            hypothetical += -(cost * shares * stop_pct / 100.0) - fee
        else:
            hypothetical += pnl
    stop_analysis: Dict[str, Any] = {
        "loss_sells": len(loss_sells),
        "actual_loss": _round(actual_loss),
        "deep_loss_count": deep,
        "deep_loss_ratio": _round(deep / covered, 3) if covered else None,
    }
    if covered:
        stop_analysis.update({
            "hypothetical_stop_pct": stop_pct,
            "hypothetical_loss": _round(hypothetical),
            "saved": _round(actual_loss - hypothetical),
        })
        if deep > 0:
            findings.append({
                "key": "stop_loss_not_enforced",
                "severity": "high",
                "title": "止损没有真正执行",
                "evidence": (f"{covered} 笔有成本可考的亏损卖出里，{deep} 笔亏损超过 15%；"
                             f"今年已实现亏损合计 {actual_loss:,.0f} 元"),
                "advice": (f"如果在 -{stop_pct:.0f}% 无条件止损，同期亏损约 "
                           f"{hypothetical:,.0f} 元，差额约 {(actual_loss - hypothetical):,.0f} 元。"
                           f"注意这是简化测算：假设止损单能成交、忽略滑点，也不考虑止损后反弹"),
            })
            actions.append({
                "area": "risk",
                "change": f"止损线收紧到 -{stop_pct:.0f}% 并前置为条件单",
                "target": "买入时就挂好止损，不等盘中看",
                "expected": f"按今年数据测算，可减少约 {(actual_loss - hypothetical):,.0f} 元亏损（简化口径）",
                "how_to_verify": "下次诊断看 deep_loss_ratio 是否降到 0",
            })

    # ---- 4. 亏损集中度 ----
    by_stock: Dict[str, Dict[str, Any]] = {}
    by_month: Dict[str, float] = {}
    total_neg = 0.0
    for rec in judged:
        pnl = _f(rec.get("realized_pnl")) or 0.0
        sec = str(rec.get("secID") or "")
        slot = by_stock.setdefault(sec, {"secID": sec, "name": rec.get("name") or sec,
                                         "pnl": 0.0, "count": 0})
        slot["pnl"] += pnl
        slot["count"] += 1
        month = str(rec.get("trade_date") or "")[:7]
        by_month[month] = by_month.get(month, 0.0) + pnl
        if pnl < 0:
            total_neg += pnl
    worst_stock = min(by_stock.values(), key=lambda x: x["pnl"], default=None)
    worst_month = min(by_month.items(), key=lambda kv: kv[1], default=None)
    conc_analysis: Dict[str, Any] = {
        "worst_stock": ({"name": worst_stock["name"], "pnl": _round(worst_stock["pnl"]),
                         "count": worst_stock["count"]} if worst_stock else None),
        "worst_month": ({"month": worst_month[0], "pnl": _round(worst_month[1])}
                        if worst_month else None),
        "total_loss": _round(total_neg),
    }
    if worst_stock and total_neg < 0:
        share = worst_stock["pnl"] / total_neg
        conc_analysis["worst_stock_share_of_loss"] = _round(share, 3)
        if share >= 0.5:
            findings.append({
                "key": "loss_concentrated",
                "severity": "medium",
                "title": "亏损集中在单一标的",
                "evidence": (f"{worst_stock['name']} 一只票贡献了 {share:.0%} 的亏损"
                             f"（{worst_stock['pnl']:,.0f} 元，{worst_stock['count']} 笔卖出）"),
                "advice": ("单票亏损占比过半，说明问题不是「选错很多票」，而是"
                           "「在一只票上反复加码又不止损」。先限制单票累计亏损上限"
                           "（例如总资金的 3%），触线就强制离场冷却"),
            })
            actions.append({
                "area": "position",
                "change": "设单票累计亏损上限",
                "target": "单票累计亏损达总资金 3% 即强制离场并冷却",
                "expected": "避免在一只票上把多次小亏滚成大亏",
                "how_to_verify": "下次诊断看 worst_stock_share_of_loss 是否低于 0.3",
            })

    findings.sort(key=lambda x: {"high": 0, "medium": 1, "ok": 2}.get(x["severity"], 3))

    return {
        "ok": True,
        "empty": False,
        "since": since,
        "date_range": [min(dates), max(dates)] if dates else None,
        "sample": {
            "records": len(records),
            "sells": len(sells),
            "judged_sells": len(judged),
            "holding_matched": len(holding),
        },
        "exit_analysis": exit_analysis,
        "holding_analysis": hold_analysis,
        "stop_analysis": stop_analysis,
        "concentration": conc_analysis,
        "findings": findings,
        "actions": actions,
        "caveats": [
            f"「卖出后 {forward_days} 日表现」只对有完整 K 线的卖出有效，"
            f"越接近今天的卖出越可能样本不足",
            "持有期用 FIFO 配对，配对不上的（流水起点前的老仓位）已跳过",
            f"反事实测算假设在 -{stop_pct:.0f}% 处能顺利成交，忽略滑点与止损后反弹",
        ],
    }
