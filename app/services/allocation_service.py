"""四池组合分配与组合级风控（纯计算，不联网、不写库）。

产品口径（唯一事实源在 config/decision_rules.json 的 pools / cash / global_risk）：
- 四池：dividend 股息 / catalyst 催化波段成长 / long_track 长赛道产业成长 / value 低估价值
- 现金为余量，有下限；四池目标 + 现金 = 100%
- 池子资金闭环隔离：兑现资金优先回流本池，不做跨池调拨
- 全局风控按「账户自新高回撤」分级：-15 停新开/加仓，-20 成长与低估各减半，-25 只留股息+现金

本模块只回答「现在偏了多少、能不能买、该卖多少」，不产生交易、不写台账。
真正的执行必须经过 proposal_service 的两道确认。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from ..core import decision_rules

UNCLASSIFIED = ""

_POOL_ALIASES = {
    # 英文键
    "dividend": "dividend",
    "catalyst": "catalyst",
    "long_track": "long_track",
    "value": "value",
    # 中文/口语别名
    "股息": "dividend", "股息股": "dividend", "红利": "dividend", "高股息": "dividend",
    "催化": "catalyst", "催化波段": "catalyst", "催化波段成长": "catalyst",
    "波段成长": "catalyst", "事件驱动": "catalyst",
    "长赛道": "long_track", "长赛道产业成长": "long_track", "产业成长": "long_track",
    "长线成长": "long_track",
    "低估": "value", "低估价值": "value", "低估股": "value", "价值": "value",
}


def normalize_pool(value: Any) -> str:
    """把任意写法归一到四池键；无法识别返回空串（未分类）。"""
    text = str(value or "").strip()
    if not text:
        return UNCLASSIFIED
    return _POOL_ALIASES.get(text.lower(), _POOL_ALIASES.get(text, UNCLASSIFIED))


def pool_label(name: Any) -> str:
    key = normalize_pool(name)
    if not key:
        return "未分类"
    return decision_rules.pool_labels().get(key, key)


def holdings_from_items(items: Any, settings: Any = None) -> tuple:
    """把自选持仓 + 用户设置组装成 compute_allocation 的输入（唯一出处）。

    路由、告警、复盘都要同一份形状；此前只在 stock_router 里私有实现，
    第二处调用再抄一遍必然漂移，故上收到这里。
    """
    rows = []
    for item in (items or []):
        if not isinstance(item, dict):
            continue
        rows.append({
            "secID": item.get("secID"),
            "name": item.get("name"),
            "shares": item.get("shares"),
            "cost": item.get("cost"),
            "pool": item.get("pool"),
            "price": item.get("price"),
            "industry": item.get("industry") or "",
        })
    total = _f((settings or {}).get("total_capital")) if isinstance(settings, dict) else 0.0
    return rows, total


# 池 → 卖出/监控风格映射（唯一出处）。
# 产品口径已切到四池，`long/mid_long/swing` 只作为底层卖出参数与监控阈值的内部风格，
# 不再由用户选择；这样页面删掉「周期」概念后，监控与卖出目标价依然有确定口径。
POOL_HORIZON = {
    "dividend": "long",      # 吃分红 + 慢修复，对应长线参数
    "value": "long",         # 估值修复，长期等待
    "long_track": "mid_long",  # 产业趋势，数周至数月
    "catalyst": "swing",     # 事件驱动，数日到数周
}


def horizon_for_pool(pool: Any, fallback: Any = "long") -> str:
    """按池推导内部卖出风格；未分类时回退到 fallback（保持旧数据行为）。"""
    key = normalize_pool(pool)
    if key:
        return POOL_HORIZON[key]
    text = str(fallback or "").strip().lower()
    return text if text in ("long", "mid_long", "swing", "short") else "long"


# 违规码的唯一事实源：判定、落盘、聚合、前端标签全部从这里取。
# 绝不能靠中文文案做字符串匹配去分类——文案会随规则文案调整而漂移。
VIOLATION_LABELS = {
    "unknown_pool": "未知池",
    "max_single": "单票上限",
    "max_positions": "持仓只数",
    "add_one_shot": "禁止加仓",
    "add_closed": "未开放加仓",
    "cash_floor": "现金下限",
}


def make_violation(code: str, message: str) -> Dict[str, str]:
    """一条违规：code 稳定可聚合，message 给人看，label 供前端直接显示。"""
    return {"code": code, "label": VIOLATION_LABELS.get(code, code),
            "message": str(message)}


def violation_label(code: Any) -> str:
    """历史台账里的 code 也要能显示中文；未知 code 原样返回，不假装认识。"""
    text = str(code or "").strip()
    return VIOLATION_LABELS.get(text, text or "未知规则")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _r(value: Any, digits: int = 2) -> float:
    return round(_f(value), digits)


# ---------------------------------------------------------------------------
# 占比计算
# ---------------------------------------------------------------------------

def compute_allocation(
    holdings: List[Dict[str, Any]],
    total_capital: float,
    prices: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """计算四池实际占比、偏离、越界与集中度。

    holdings 每项：{secID, name?, shares, cost?, pool?, industry?, price?}
    prices 可覆盖实时价；缺失时退化用 cost 计价并标 valued_by_cost（提示估值失真）。
    """
    total = _f(total_capital)
    if total <= 0:
        return {"ok": False, "error": "总资金未设置或为 0，无法计算占比"}

    prices = prices or {}
    pools = decision_rules.pools()
    if not pools:
        return {"ok": False, "error": "规则文件缺少 pools 段"}

    by_pool: Dict[str, Dict[str, Any]] = {
        key: {"value": 0.0, "count": 0, "holdings": []} for key in pools
    }
    unclassified = {"value": 0.0, "count": 0, "holdings": []}
    rows: List[Dict[str, Any]] = []

    for item in holdings or []:
        sec = str(item.get("secID") or item.get("code") or "").strip()
        shares = int(_f(item.get("shares")))
        if not sec or shares <= 0:
            continue
        price = _f(prices.get(sec, item.get("price")), 0.0)
        valued_by_cost = False
        if price <= 0:
            price = _f(item.get("cost"), 0.0)
            valued_by_cost = True
        value = shares * price
        key = normalize_pool(item.get("pool"))
        row = {
            "secID": sec,
            "name": str(item.get("name") or ""),
            "shares": shares,
            "price": _r(price, 4),
            "value": _r(value),
            "valued_by_cost": valued_by_cost,
            "pool": key or UNCLASSIFIED,
            "pool_label": pool_label(key) if key else "未分类",
            "industry": str(item.get("industry") or ""),
        }
        rows.append(row)
        bucket = by_pool[key] if key in by_pool else unclassified
        bucket["value"] += value
        bucket["count"] += 1
        bucket["holdings"].append(row)

    pool_rows: List[Dict[str, Any]] = []
    for key, meta in pools.items():
        bucket = by_pool[key]
        pct = bucket["value"] / total * 100.0
        target = _f(meta.get("target_pct"))
        low = _f(meta.get("min_pct"), target)
        high = _f(meta.get("max_pct"), target)
        largest = max((r["value"] for r in bucket["holdings"]), default=0.0) / total * 100.0
        if pct < low - 1e-9:
            status = "under"
        elif pct > high + 1e-9:
            status = "over"
        else:
            status = "in_band"
        pool_rows.append({
            "pool": key,
            "label": str(meta.get("label") or key),
            "target_pct": _r(target),
            "min_pct": _r(low),
            "max_pct": _r(high),
            "actual_pct": _r(pct),
            "value": _r(bucket["value"]),
            "diff_pp": _r(pct - target),
            "status": status,
            "count": bucket["count"],
            "max_positions": int(_f(meta.get("max_positions"), 0)),
            "max_single_pct": _r(meta.get("max_single_pct")),
            "largest_single_pct": _r(largest),
            "holdings": bucket["holdings"],
        })

    unclassified_pct = unclassified["value"] / total * 100.0
    invested_pct = sum(p["actual_pct"] for p in pool_rows) + unclassified_pct
    cash_pct = max(0.0, 100.0 - invested_pct)
    cash_rule = decision_rules.cash_rules()

    issues: List[Dict[str, Any]] = []
    for p in pool_rows:
        if p["count"] > p["max_positions"] > 0:
            issues.append({"kind": "max_positions", "pool": p["pool"],
                           "message": f"{p['label']}持有 {p['count']} 只，超过上限 {p['max_positions']} 只"})
        if p["largest_single_pct"] > p["max_single_pct"] + 1e-9 > 0:
            issues.append({"kind": "max_single", "pool": p["pool"],
                           "message": f"{p['label']}单票占 {p['largest_single_pct']:.2f}%，超过上限 {p['max_single_pct']:.2f}%"})
    if cash_pct < _f(cash_rule.get("min_pct"), 10.0) - 1e-9:
        issues.append({"kind": "cash_floor",
                       "message": f"现金 {cash_pct:.2f}% 低于下限 {_f(cash_rule.get('min_pct'), 10.0):.2f}%"})
    if unclassified_pct > 1e-9:
        issues.append({"kind": "unclassified",
                       "message": f"有 {unclassified['count']} 只持仓（{unclassified_pct:.2f}%）未分类到四池，分配与风控会失真"})

    global_rule = decision_rules.global_risk()
    max_industry = _f(global_rule.get("max_industry_pct"), 30.0)
    industry_value: Dict[str, float] = {}
    for row in rows:
        if row["industry"]:
            industry_value[row["industry"]] = industry_value.get(row["industry"], 0.0) + row["value"]
    industry_rows = sorted(
        ({"industry": name, "pct": _r(value / total * 100.0)} for name, value in industry_value.items()),
        key=lambda item: -item["pct"],
    )
    for item in industry_rows:
        if item["pct"] > max_industry + 1e-9:
            issues.append({"kind": "industry_concentration", "industry": item["industry"],
                           "message": f"行业「{item['industry']}」占 {item['pct']:.2f}%，超过上限 {max_industry:.2f}%"})

    return {
        "ok": True,
        "total_capital": _r(total),
        "pools": pool_rows,
        "cash": {
            "target_pct": _r(_f(cash_rule.get("target_pct"), 25.0)),
            "min_pct": _r(_f(cash_rule.get("min_pct"), 10.0)),
            "max_pct": _r(_f(cash_rule.get("max_pct"), 30.0)),
            "actual_pct": _r(cash_pct),
            "value": _r(total * cash_pct / 100.0),
            "status": ("below_floor" if cash_pct < _f(cash_rule.get("min_pct"), 10.0) - 1e-9
                       else "above_target" if cash_pct > _f(cash_rule.get("target_pct"), 25.0) + 1e-9
                       else "in_band"),
        },
        "unclassified": {
            "count": unclassified["count"],
            "value": _r(unclassified["value"]),
            "pct": _r(unclassified_pct),
            "holdings": unclassified["holdings"],
        },
        "invested_pct": _r(invested_pct),
        "industry": industry_rows,
        "issues": issues,
        "holdings": rows,
    }


# ---------------------------------------------------------------------------
# 再平衡（只在带宽外提示，且不跨池调拨）
# ---------------------------------------------------------------------------

def rebalance_plan(allocation: Dict[str, Any]) -> List[Dict[str, Any]]:
    """偏离超过 ±band 时给出「回到目标」的建议；金额用于生成建议单，不自动执行。"""
    if not allocation.get("ok"):
        return []
    band = _f(decision_rules.global_risk().get("rebalance_band_pp"), 3.0)
    total = _f(allocation.get("total_capital"))
    plan: List[Dict[str, Any]] = []
    for item in allocation.get("pools", []):
        diff = _f(item.get("diff_pp"))
        if abs(diff) <= band + 1e-9:
            continue
        plan.append({
            "pool": item["pool"],
            "label": item["label"],
            "action": "trim" if diff > 0 else "add",
            "diff_pp": _r(diff),
            "delta_pp": _r(-diff),
            "amount": _r(abs(diff) / 100.0 * total),
            "reason": f"相对目标偏离 {diff:+.2f}pp，超过 ±{band:.0f}pp 带宽",
            "constraint": "资金只在本池内调拨，不跨池挪用",
        })
    return plan


# ---------------------------------------------------------------------------
# 说人话：把四池数字翻译成「现在怎么样、下一步做什么」
# ---------------------------------------------------------------------------

_PLAIN_ACTIONS = {
    "settings": ("去设置本金", "settings"),
    "watchlist": ("去添加自选", "watchlist"),
    "pools": ("打开四池配置", "pools"),
}


def plain_summary(allocation: Dict[str, Any],
                  plan: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """把分配结果翻译成一句人话 + 一个下一步动作（给不懂金融的人看）。

    只做翻译，不新增判断口径：数字仍来自 compute_allocation / rebalance_plan。
    前端不再自己写一套「什么时候该提醒什么」，避免两处口径漂移。
    """
    if not isinstance(allocation, dict):
        allocation = {}
    plan = plan if plan is not None else rebalance_plan(allocation)

    def _say(level: str, headline: str, detail: str = "", action_key: str = "pools") -> Dict[str, Any]:
        label, view = _PLAIN_ACTIONS.get(action_key, _PLAIN_ACTIONS["pools"])
        return {"level": level, "headline": headline, "detail": detail,
                "action": {"label": label, "view": view}}

    if not allocation.get("ok"):
        error = str(allocation.get("error") or "")
        if "总资金" in error:
            return _say("setup", "先告诉我你有多少本金",
                        "有了本金，我才算得出每只股票最多能占多少钱。", "settings")
        return _say("setup", "四池暂时算不出来", error or "请先完成基础设置。", "settings")

    pools = allocation.get("pools") or []
    held = sum(int(_f(p.get("count"))) for p in pools)
    unclassified = allocation.get("unclassified") or {}
    cash = allocation.get("cash") or {}
    unclassified_count = int(_f(unclassified.get("count")))

    if held <= 0 and unclassified_count <= 0:
        return _say("empty", "还没有持仓，先加一只你在意的股票",
                    "在「自选股」里填代码就能加，之后每一步我都会按你的纪律提醒。", "watchlist")

    if unclassified_count > 0:
        return _say("unclassified", f"有 {unclassified_count} 只还没分类",
                    "把它们放进四个池子之一，我才能判断该不该加减仓。", "pools")

    if str(cash.get("status")) == "below_floor":
        floor = _f(cash.get("min_pct"), 10.0)
        return _say("risk", f"现金只剩 {_f(cash.get('actual_pct')):.0f}%，低于 {floor:.0f}% 底线",
                    "先别再加仓，留一点现金应付下跌，本身就是纪律。", "pools")

    if plan:
        worst = max(plan, key=lambda x: abs(_f(x.get("diff_pp"))))
        diff = _f(worst.get("diff_pp"))
        amount = abs(_f(worst.get("amount")))
        over = diff > 0
        name = str(worst.get("label") or "某个池")
        return _say(
            "drift",
            f"{name}{'多了' if over else '少了'} {abs(diff):.1f} 个百分点，约 {amount:,.0f} 元",
            ("超出目标就该减一点，把钱放回更便宜的地方。" if over
             else "低于目标可以慢慢补，但别一次买满。"),
            "pools",
        )

    return _say("calm", "四池都在目标附近，今天不用动手",
                "没有偏离就不交易，少动手本身就是收益。", "pools")


# ---------------------------------------------------------------------------
# 全局风控
# ---------------------------------------------------------------------------

def global_risk_state(drawdown_pct: float) -> Dict[str, Any]:
    """按账户自新高回撤（负值）判定风控级别。"""
    rules = decision_rules.global_risk()
    dd = _f(drawdown_pct)
    tiers = [
        ("defensive", _f(rules.get("dd_defensive_pct"), -25.0),
         ["只保留股息底仓 + 现金", "清掉成长与低估的剩余仓位", "停止一切新开仓与加仓"]),
        ("halve", _f(rules.get("dd_halve_growth_value_pct"), -20.0),
         ["成长（催化/长赛道）与低估各减半", "停止新开仓与加仓", "只执行原定止盈止损"]),
        ("stop_new", _f(rules.get("dd_stop_new_pct"), -15.0),
         ["停止新开仓", "停止一切加仓", "只执行原定止盈止损"]),
    ]
    for level, threshold, actions in tiers:
        if dd <= threshold + 1e-9:
            return {"level": level, "drawdown_pct": _r(dd), "threshold_pct": _r(threshold),
                    "actions": actions, "allow_new": False, "allow_add": False}
    return {"level": "normal", "drawdown_pct": _r(dd), "threshold_pct": None,
            "actions": ["按四池纪律正常执行"], "allow_new": True, "allow_add": True}


def pool_drawdown_state(pool_name: str, drawdown_pct: float) -> Dict[str, Any]:
    """池级回撤护栏：单池回撤到阈值触发复盘（不是自动止损）。"""
    threshold = _f(decision_rules.global_risk().get("pool_dd_review_pct"), -15.0)
    dd = _f(drawdown_pct)
    triggered = dd <= threshold + 1e-9
    return {"pool": normalize_pool(pool_name), "drawdown_pct": _r(dd),
            "threshold_pct": _r(threshold), "review_required": triggered,
            "action": "触发池级复盘：核对逻辑是否破坏，不自动清仓" if triggered else "正常"}


# ---------------------------------------------------------------------------
# 单笔交易 / 止盈 / 时间止损
# ---------------------------------------------------------------------------

def cash_floor_violation(cash_after_pct: Optional[float]) -> Optional[Dict[str, str]]:
    """现金破下限的结构化违规项；判定逻辑只在这里写一次。

    与池无关：新建仓的票还没归池，池规则无从判断，但"买完现金不够"
    任何池都成立，闸门必须照样拦。
    """
    if cash_after_pct is None:
        return None
    floor = _f(decision_rules.cash_rules().get("min_pct"), 10.0)
    if _f(cash_after_pct) < floor - 1e-9:
        return make_violation("cash_floor",
                              f"交易后现金 {_f(cash_after_pct):.2f}% 低于下限 {floor:.2f}%")
    return None


def cash_floor_issue(cash_after_pct: Optional[float]) -> Optional[str]:
    """兼容旧调用方：只要一句中文描述。判定仍走 cash_floor_violation。"""
    item = cash_floor_violation(cash_after_pct)
    return item["message"] if item else None


def validate_pool_trade(
    pool_name: str,
    post_weight_pct: float,
    post_positions: int,
    is_add: bool = False,
    cash_after_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """校验一笔拟议交易是否违反池规则。

    返回 ``violations``（结构化，唯一真源）、``codes`` / ``issues``（由它派生）。
    ``issues`` 保持原样，既有调用方与测试不受影响。
    """
    key = normalize_pool(pool_name)
    meta = decision_rules.pool(key)
    if not meta:
        item = make_violation("unknown_pool", f"未知池：{pool_name}")
        return {"ok": False, "pool": key, "violations": [item],
                "codes": [item["code"]], "issues": [item["message"]]}

    violations: List[Dict[str, str]] = []
    max_single = _f(meta.get("max_single_pct"))
    if max_single > 0 and _f(post_weight_pct) > max_single + 1e-9:
        violations.append(make_violation(
            "max_single",
            f"单票将达 {_f(post_weight_pct):.2f}%，超过 {meta.get('label')} 单票上限 {max_single:.2f}%"))

    max_positions = int(_f(meta.get("max_positions"), 0))
    if max_positions > 0 and int(post_positions) > max_positions:
        violations.append(make_violation(
            "max_positions",
            f"持仓将达 {int(post_positions)} 只，超过 {meta.get('label')} 上限 {max_positions} 只"))

    if is_add:
        add_rule = meta.get("add_rules") or {}
        mode = str(add_rule.get("mode") or "")
        if mode == "one_shot":
            violations.append(make_violation(
                "add_one_shot", f"{meta.get('label')} 规定一次性建仓、禁止加仓"))
        max_adds = int(_f(add_rule.get("max_adds"), 0))
        if mode != "one_shot" and max_adds <= 0:
            violations.append(make_violation(
                "add_closed", f"{meta.get('label')} 未开放加仓"))

    cash_item = cash_floor_violation(cash_after_pct)
    if cash_item:
        violations.append(cash_item)

    return {
        "ok": not violations,
        "pool": key,
        "violations": violations,
        "codes": [v["code"] for v in violations],
        "issues": [v["message"] for v in violations],
    }


def take_profit_plan(pool_name: str, gain_pct: float) -> Dict[str, Any]:
    """按池的止盈阶梯给出「该卖多少」。recover_principal 时用 f=1/(1+r) 动态算。"""
    key = normalize_pool(pool_name)
    meta = decision_rules.pool(key)
    gain = _f(gain_pct)
    tiers = meta.get("take_profit") or []
    matched: Optional[Dict[str, Any]] = None
    for tier in tiers:
        trigger = tier.get("trigger_pct")
        if trigger is None:
            continue
        if gain < _f(trigger) - 1e-9:
            continue
        # 阶梯是顺序执行的：取「已达到的最高一档」，前序档视为已执行
        matched = tier
    if matched is not None:
        trigger = _f(matched.get("trigger_pct"))
        sell_pct = _f(matched.get("sell_pct"), 100.0)
        if matched.get("recover_principal"):
            sell_pct = decision_rules.recover_principal_sell_pct(gain)
        return {
            "pool": key, "label": meta.get("label"), "triggered": True,
            "trigger_pct": _r(trigger), "gain_pct": _r(gain),
            "sell_pct": _r(sell_pct),
            "recover_principal": bool(matched.get("recover_principal")),
            "note": str(matched.get("note") or ""),
        }
    tail = _f(meta.get("tail_drawdown_pct"), 0.0)
    return {
        "pool": key, "label": meta.get("label"), "triggered": False,
        "gain_pct": _r(gain), "sell_pct": 0.0,
        "next_trigger_pct": next((_f(t.get("trigger_pct")) for t in tiers if t.get("trigger_pct") is not None
                                  and _f(t.get("trigger_pct")) > gain), None),
        "tail_drawdown_pct": _r(tail) if tail else None,
        "note": f"未到止盈档；若已有利润仓，高点回撤 {tail:.0f}% 清仓" if tail else "未到止盈档",
    }


def time_stop_state(pool_name: str, weeks_held: float, has_started: bool = False) -> Dict[str, Any]:
    """催化池的硬时间规则：4 周无启动减半、8 周无条件清仓；其余池无时间止损。"""
    key = normalize_pool(pool_name)
    meta = decision_rules.pool(key)
    rules = meta.get("time_rules") or {}
    if not rules:
        return {"pool": key, "applies": False, "action": "hold",
                "note": f"{meta.get('label') or pool_name} 无时间止损"}

    weeks = _f(weeks_held)
    review_weeks = _f(rules.get("review_weeks"), 4.0)
    hard_weeks = _f(rules.get("hard_stop_weeks"), 8.0)
    if weeks >= hard_weeks - 1e-9:
        return {"pool": key, "applies": True, "weeks": _r(weeks), "action": "clear",
                "note": f"已满 {hard_weeks:.0f} 周，未兑现催化 → 无条件离场"}
    if weeks >= review_weeks - 1e-9 and not has_started:
        return {"pool": key, "applies": True, "weeks": _r(weeks), "action": "halve",
                "note": f"已满 {review_weeks:.0f} 周且无启动迹象 → 减半释放资金"}
    return {"pool": key, "applies": True, "weeks": _r(weeks), "action": "hold",
            "next_review_weeks": review_weeks, "hard_stop_weeks": hard_weeks,
            "note": "未到观察节点"}


def pool_rules_summary() -> List[Dict[str, Any]]:
    """给前端 / Agent 用的四池规则摘要。"""
    out: List[Dict[str, Any]] = []
    for key, meta in decision_rules.pools().items():
        out.append({
            "pool": key,
            "label": meta.get("label") or key,
            "role": meta.get("role") or "",
            "target_pct": _r(meta.get("target_pct")),
            "band": [_r(meta.get("min_pct")), _r(meta.get("max_pct"))],
            "max_single_pct": _r(meta.get("max_single_pct")),
            "max_positions": int(_f(meta.get("max_positions"), 0)),
            "time_rules": meta.get("time_rules"),
            "stop_loss_pct": meta.get("stop_loss_pct"),
            "review_dd_pct": meta.get("review_dd_pct"),
            "take_profit": meta.get("take_profit") or [],
            "add_rules": meta.get("add_rules") or {},
            "prohibited": meta.get("prohibited") or [],
        })
    return out


# ---------------------------------------------------------------------------
# 四池占比可编辑（写接口用）
# ---------------------------------------------------------------------------

# 只开放这几个字段：止盈/止损/加仓/时间止损属于策略内容，改动影响面大，
# 仍只走 JSON 手工编辑，不在界面里放开。
POOL_EDITABLE_FIELDS = ("target_pct", "min_pct", "max_pct", "max_single_pct", "max_positions")
CASH_EDITABLE_FIELDS = ("target_pct", "min_pct", "max_pct")
# 四池目标 + 现金目标必须凑满 100%，否则占比/偏离/越界全部失真
TOTAL_PCT_TOLERANCE = 0.5


def _pick(patch: Any, allowed: tuple, where: str, issues: List[str]) -> Dict[str, Any]:
    """从 patch 里挑出白名单字段并校验类型；未知字段直接报错（防手误写坏配置）。"""
    if patch is None:
        return {}
    if not isinstance(patch, dict):
        issues.append(f"{where} 必须是对象")
        return {}
    out: Dict[str, Any] = {}
    for key, value in patch.items():
        if key not in allowed:
            issues.append(f"{where} 不支持修改字段「{key}」，只允许 {', '.join(allowed)}")
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            issues.append(f"{where}.{key} 必须是数字，收到 {value!r}")
            continue
        if num != num or num in (float("inf"), float("-inf")):  # NaN / Inf
            issues.append(f"{where}.{key} 不是有效数字")
            continue
        out[key] = int(round(num)) if key == "max_positions" else round(num, 2)
    return out


def validate_pool_config(
    pools_patch: Optional[Dict[str, Any]] = None,
    cash_patch: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """校验四池占比的修改，返回 ``{'ok', 'issues', 'pools', 'cash'}``。

    ``pools`` / ``cash`` 是**合并后**的完整取值，router 只把这些字段写回文件，
    不会把请求里夹带的其它键带进配置。

    校验的不只是"单个数字合法"，还有三条容易踩的组合约束：
    1. ``min ≤ target ≤ max``，越界后这套池自己就对不上；
    2. ``单票上限 × 最大持仓数 ≥ 区间下限``，否则该池永远无法达标（例如
       下限 18% 但单票 5% × 2 只 = 10%，配置本身就是矛盾的）；
    3. 四池目标合计 + 现金目标 = 100%，代码不会自动归一化，必须挡在入口。
    """
    issues: List[str] = []
    current_pools = decision_rules.pools()
    current_cash = decision_rules.cash_rules()
    if not current_pools:
        return {"ok": False, "issues": ["规则文件缺少 pools 段"], "pools": {}, "cash": {}}

    clean_pools: Dict[str, Dict[str, Any]] = {}
    for name, patch in (pools_patch or {}).items():
        if name not in current_pools:
            issues.append(f"未知池「{name}」，可选 {', '.join(current_pools)}")
            continue
        clean_pools[name] = _pick(patch, POOL_EDITABLE_FIELDS, f"pools.{name}", issues)

    clean_cash = _pick(cash_patch, CASH_EDITABLE_FIELDS, "cash", issues)

    merged_pools: Dict[str, Dict[str, Any]] = {}
    for name, meta in current_pools.items():
        row = {f: _f(meta.get(f)) for f in POOL_EDITABLE_FIELDS}
        row["max_positions"] = int(_f(meta.get("max_positions")))
        row.update(clean_pools.get(name) or {})
        merged_pools[name] = row

    merged_cash = {f: _f(current_cash.get(f)) for f in CASH_EDITABLE_FIELDS}
    merged_cash.update(clean_cash)

    for name, row in merged_pools.items():
        label = current_pools[name].get("label") or name
        target, low, high = row["target_pct"], row["min_pct"], row["max_pct"]
        single, positions = row["max_single_pct"], row["max_positions"]
        for fname, value in (("target_pct", target), ("min_pct", low), ("max_pct", high)):
            if not (0 < value <= 100):
                issues.append(f"{label}.{fname} 必须在 0~100 之间，当前 {value}")
        if not (0 < single <= 100):
            issues.append(f"{label}.max_single_pct 必须在 0~100 之间，当前 {single}")
        if positions < 1:
            issues.append(f"{label}.max_positions 至少为 1 只，当前 {positions}")
        if low > target:
            issues.append(f"{label} 区间下限 {low}% 高于目标 {target}%")
        if target > high:
            issues.append(f"{label} 目标 {target}% 高于区间上限 {high}%")
        capacity = single * positions
        if positions >= 1 and single > 0 and capacity + 1e-9 < low:
            issues.append(
                f"{label} 单票上限 {single}% × 最多 {positions} 只 = {capacity:.2f}%，"
                f"低于区间下限 {low}%，该池永远无法达标"
            )

    cash_target, cash_low, cash_high = (merged_cash["target_pct"], merged_cash["min_pct"], merged_cash["max_pct"])
    if not (0 <= cash_low <= cash_target <= cash_high <= 100):
        issues.append(f"现金需满足 0 ≤ 下限 {cash_low} ≤ 目标 {cash_target} ≤ 上限 {cash_high} ≤ 100")

    total_target = sum(row["target_pct"] for row in merged_pools.values()) + cash_target
    if abs(total_target - 100.0) > TOTAL_PCT_TOLERANCE + 1e-9:
        detail = " + ".join(
            f"{current_pools[k].get('label') or k} {v['target_pct']}%" for k, v in merged_pools.items()
        )
        issues.append(
            f"四池目标 + 现金目标 = {total_target:.2f}%，必须等于 100%（{detail} + 现金 {cash_target}%）"
        )

    return {"ok": not issues, "issues": issues, "pools": merged_pools, "cash": merged_cash}


def save_pool_config(
    pools_patch: Optional[Dict[str, Any]] = None,
    cash_patch: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """校验并落盘；不合法时抛 ValueError（由 router 转 400）。"""
    result = validate_pool_config(pools_patch, cash_patch)
    if not result["ok"]:
        raise ValueError("；".join(result["issues"]))
    patch: Dict[str, Any] = {"pools": result["pools"]}
    if cash_patch:
        patch["cash"] = result["cash"]
    decision_rules.update(patch)
    return {
        "pools": pool_rules_summary(),
        "cash": decision_rules.cash_rules(),
    }
