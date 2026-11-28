"""亏损单时间止损：持有超过「自适应天数」仍未创新高 → 建议减半。

为什么要有这个模块（实证依据，不是拍脑袋）：
2026 全年 136 笔成交的诊断显示——盈利单持有中位数 4 天、亏损单 45 天；
持有超 60 天的 9 笔亏掉全部已实现亏损的 69.7%。抗单是账户唯一的结构性漏洞。

⚠️ 三个设计原则，改动前务必先读：

1. **触发必须由代码保证，不能让 agent 决定「这次要不要提醒」**。
   人（和 agent）永远能找到「再等等」的理由——那正是抗单的心理机制。
   所以「算不算触发」是确定性计算，同一个输入永远同一结果。

2. **阈值随环境缩放，但缩放规则本身写死、可审计**（config/decision_rules.json）。
   「自适应」不等于「每次临时判断」：前者可复现，后者是把抗单制度化。

3. **agent 可以豁免，但必须留痕**（见 time_stop_override 记录）。
   允许判断力，不允许无声跳过。

反直觉但故意的设计：主升期（advance）阈值反而收紧到 15 天。
理由——环境最好时你还在浮亏，大概率是选错了，不是市场不给时间。
反过来修复期（repair）放宽到 30 天，因为均值回归本来就需要时间。
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Sequence

from ..core import decision_rules

logger = logging.getLogger(__name__)

_DEFAULT_RULES: Dict[str, Any] = {
    "enabled": True,
    "base_days": 20,
    "new_high_lookback_bars": 10,
    "new_high_tolerance_pct": 0.5,
    "min_bars_since_entry": 10,
    "phase_scale": {
        "collapse": 0.5,
        "ebb": 0.75,
        "freezing": 1.0,
        "repair": 1.5,
        "advance": 0.75,
        "euphoria": 0.5,
        "unknown": 1.0,
    },
    # 炒作度修正：用户理念「近一年没炒作过 → 价位不高，迟早起一波」的量化。
    # 沉寂的票给时间等均值回归；刚炒过的票退潮后容易长期阴跌，要快走。
    "hype_dormant_scale": 1.6,
    "hype_hot_scale": 0.6,
    "hype_hot_days": 60,
    "hype_hot_score": 0.5,
    "hype_dormant_score": 0.2,
}


def _rules() -> Dict[str, Any]:
    cfg = decision_rules.section("time_stop")
    if not cfg:
        return dict(_DEFAULT_RULES)
    merged = dict(_DEFAULT_RULES)
    merged.update(cfg)
    if not isinstance(merged.get("phase_scale"), dict):
        merged["phase_scale"] = dict(_DEFAULT_RULES["phase_scale"])
    return merged


# ---------------- 豁免留痕 ----------------
# 允许 agent 判「周期底部豁免」，但必须满足三条，缺一不可：
#   1. 写明依据（不是「我觉得会涨」）
#   2. 有期限（最长 _MAX_OVERRIDE_DAYS 天），到期自动恢复提醒
#   3. 落盘留痕，可查询、可复盘
# 没有期限的豁免等于把抗单合法化——那正是这个机制要防的东西。

_OVERRIDE_FILE = "time_stop_overrides.json"
_MAX_OVERRIDE_DAYS = 30
_OVERRIDE_LOCK = threading.Lock()
_OV_CACHE_TTL = 5.0
_ov_cache = None  # (monotonic_ts, {secID: record})


def _override_path():
    return decision_rules.BASE_DIR / "config" / _OVERRIDE_FILE


def _load_overrides() -> Dict[str, Any]:
    """读全部豁免记录（真源是 SQLite）。返回 {secID: record} 便于按票查。

    带 5 秒内存缓存：每轮监控对每只持仓都会问一次「这只票被豁免了吗」，
    直接查库 0.4ms × 六只票 ≈ 2.4ms。豁免是人为决定的，秒级延迟没有影响，
    登记/撤销时立刻失效缓存。
    """
    global _ov_cache
    now = time.monotonic()
    if _ov_cache and (now - _ov_cache[0]) < _OV_CACHE_TTL:
        return _ov_cache[1]

    from ..db import store

    try:
        rows = store.list_time_stop_overrides()
    except Exception as exc:  # noqa: BLE001
        logger.warning("time_stop_override_read_failed error=%r", exc)
        return _load_overrides_from_json()
    if rows:
        data = {str(it.get("secID")): it for it in rows if it.get("secID")}
    else:
        data = _seed_overrides_from_json()
    _ov_cache = (now, data)
    return data


def _load_overrides_from_json() -> Dict[str, Any]:
    path = _override_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("time_stop_override_load_failed error=%r", exc)
    return {}


def _seed_overrides_from_json() -> Dict[str, Any]:
    """把旧的 time_stop_overrides.json 一次性导入表。"""
    from ..db import store

    data = _load_overrides_from_json()
    if not data:
        return {}
    now_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for sec, slot in data.items():
        if not isinstance(slot, dict):
            continue
        try:
            store.upsert_time_stop_override({**slot, "secID": sec, "updated_at": now_text})
        except Exception as exc:  # noqa: BLE001
            logger.warning("time_stop_override_seed_failed sec=%s error=%r", sec, exc)
    logger.info("time_stop_overrides_seeded rows=%s", len(data))
    try:
        return {str(it.get("secID")): it for it in store.list_time_stop_overrides()}
    except Exception:  # noqa: BLE001
        return data


def active_override(sec_id: str, today: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """取当前有效的豁免。过期的自动作废（不删记录，只不再生效）。"""
    sec = str(sec_id or "")
    if not sec:
        return None
    data = _load_overrides()
    slot = data.get(sec)
    if not isinstance(slot, dict):
        return None
    anchor = _to_date(today) or datetime.date.today()
    expires = _to_date(slot.get("expires_on"))
    if expires is None or anchor > expires:
        return None
    return slot


def record_override(sec_id: str, reason: str, basis: str,
                    days: int = 20, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """登记一次豁免。理由或依据为空直接拒绝——不允许无据跳过。"""
    sec = str(sec_id or "").strip()
    reason = str(reason or "").strip()
    basis = str(basis or "").strip()
    if not sec:
        return {"ok": False, "error": "缺少股票代码"}
    if not reason or not basis:
        return {"ok": False,
                "error": "豁免必须同时给出 reason（为什么）和 basis（依据是什么），否则不予登记"}
    try:
        days = max(1, min(int(days), _MAX_OVERRIDE_DAYS))
    except (TypeError, ValueError):
        days = 20

    today = datetime.date.today()
    record = {
        "secID": sec,
        "reason": reason[:400],
        "basis": basis[:400],
        "created_on": today.isoformat(),
        "expires_on": (today + datetime.timedelta(days=days)).isoformat(),
        "days": days,
    }
    if isinstance(meta, dict):
        record["meta"] = {k: meta[k] for k in ("holding_days", "pnl_pct", "phase") if k in meta}

    from ..db import store

    global _ov_cache
    with _OVERRIDE_LOCK:
        _ov_cache = None
        store.upsert_time_stop_override({
            **record,
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    return {"ok": True, "override": record}


def clear_override(sec_id: str) -> Dict[str, Any]:
    from ..db import store

    sec = str(sec_id or "").strip()
    global _ov_cache
    with _OVERRIDE_LOCK:
        _ov_cache = None
        existed = store.delete_time_stop_override(sec)
    return {"ok": True, "cleared": bool(existed)}


def list_overrides(today: Optional[str] = None) -> List[Dict[str, Any]]:
    data = _load_overrides()
    anchor = _to_date(today) or datetime.date.today()
    out: List[Dict[str, Any]] = []
    for sec, slot in data.items():
        if not isinstance(slot, dict):
            continue
        expires = _to_date(slot.get("expires_on"))
        item = dict(slot)
        item["expired"] = bool(expires is None or anchor > expires)
        if expires:
            item["days_left"] = max(0, (expires - anchor).days)
        out.append(item)
    return out


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _to_date(text: Any) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


def resolve_entry(records: Sequence[Dict[str, Any]], sec_id: str,
                  shares: int) -> Dict[str, Any]:
    """从流水 FIFO 反推当前持仓的建仓起点。

    `favorites` 表里没有 buy_date 字段（只有 secID/shares/cost/horizon/pool），
    所以持有天数只能从成交流水倒推。

    覆盖度不足时的处理（关键）：流水起点之前就有的老仓位，FIFO 扣完可能剩不下
    任何批次。这时**不猜一个日期**，而是返回 available=False 让调用方跳过——
    用「假设持有 0 天」去凑数会把结论整体拉偏（与 trade_diagnosis 同口径）。
    """
    sec = str(sec_id or "")
    if not sec or shares <= 0:
        return {"available": False, "reason": "no_position"}

    lots: deque = deque()
    ordered = sorted(
        [r for r in records if str(r.get("secID") or "") == sec
         and r.get("side") in ("buy", "sell")],
        key=lambda r: (str(r.get("trade_date") or ""), str(r.get("id") or "")),
    )
    for rec in ordered:
        qty = int(_f(rec.get("shares")) or 0)
        day = _to_date(rec.get("trade_date"))
        if qty <= 0 or day is None:
            continue
        if rec.get("side") == "buy":
            price = _f(rec.get("price")) or _f(rec.get("cost_after")) or 0.0
            lots.append({"shares": qty, "date": day, "price": price})
            continue
        remaining = qty
        while remaining > 0 and lots:
            lot = lots[0]
            take = min(remaining, lot["shares"])
            lot["shares"] -= take
            remaining -= take
            if lot["shares"] <= 0:
                lots.popleft()

    open_lots = [lot for lot in lots if lot["shares"] > 0]
    if not open_lots:
        return {"available": False, "reason": "no_open_lot",
                "hint": "流水没有覆盖当前持仓（建仓早于流水起点）"}

    total = sum(lot["shares"] for lot in open_lots)
    coverage = round(min(1.0, total / max(shares, 1)), 3)
    earliest = min(lot["date"] for lot in open_lots)
    weighted = sum(lot["shares"] * (lot["date"] - earliest).days for lot in open_lots) / total
    return {
        "available": True,
        "entry_date": earliest.isoformat(),
        "weighted_entry_date": (earliest + datetime.timedelta(days=round(weighted))).isoformat(),
        "matched_shares": total,
        "coverage": coverage,
        "lots": len(open_lots),
        # 覆盖度低说明还有流水之外的老仓，持有天数只是「至少这么久」
        "partial": coverage < 0.95,
    }


def evaluate(sec_id: str,
             name: str,
             shares: int,
             cost: float,
             klines: Sequence[Dict[str, Any]],
             records: Optional[Sequence[Dict[str, Any]]] = None,
             env: Optional[Dict[str, Any]] = None,
             today: Optional[str] = None,
             kline_frequency: str = "d",
             hype: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """时间止损评估。纯计算：K 线与流水由调用方给，本函数不联网。

    触发条件（全部满足）：浮亏 + 持有够久 + 近期没创新高。
    三个条件的含义：只砍「亏着 + 拖很久 + 明显走弱」的，不砍正在突破的。

    ⚠️ **直接用调用方传入的 K 线，不再额外取日线**。
    长线持仓的 K 线是周线（见 trade_strategy_long.json），额外取日线会让监控
    多跑一遍源降级链——行情源限流时单票能拖到分钟级，监控等于瘫痪。
    所以「最近 N 根」按频率换算：日线 10 根 ≈ 周线 4 根 ≈ 月线 1 根。
    """
    cfg = _rules()
    result: Dict[str, Any] = {
        "ok": True,
        "enabled": bool(cfg.get("enabled", True)),
        "secID": str(sec_id or ""),
        "name": name or sec_id,
        "triggered": False,
        "signal": "",
        "kline_frequency": str(kline_frequency or "d").lower(),
    }
    if shares <= 0:
        result.update({"available": False, "reason": "no_position"})
        return result
    # 基金/ETF 不进策略体系：持有天数、创新高、浮亏这些概念对基金没有意义，
    # 硬算只会得出「该减半」这种误导性结论。行情数据照常可用。
    from .market_gateway import is_data_only

    if is_data_only(sec_id):
        result.update({
            "available": False,
            "reason": "fund_data_only",
            "hint": "ETF/场内基金只提供行情数据，不参与时间止损判断",
            "applicable": False,
            "data_only": True,
        })
        return result
    if not klines:
        result.update({"available": False, "reason": "no_kline"})
        return result

    # 频率换算系数：日线为基准。周线一根≈5 个交易日，月线一根≈20 个。
    freq_scale = {"d": 1.0, "w": 0.4, "m": 0.15}.get(result["kline_frequency"], 1.0)
    min_bars = max(3, int(round(int(cfg.get("min_bars_since_entry", 10)) * freq_scale)))

    bars = [b for b in klines if _f(b.get("close")) is not None]
    if len(bars) < min_bars:
        result.update({"available": False, "reason": "insufficient_bars"})
        return result

    entry = resolve_entry(records or [], sec_id, shares)
    result["entry"] = entry
    if not entry.get("available"):
        result.update({"available": False,
                       "reason": entry.get("reason"),
                       "hint": entry.get("hint")})
        return result

    entry_date = _to_date(entry.get("entry_date"))
    if entry_date is None:
        result.update({"available": False, "reason": "bad_entry_date"})
        return result

    last_bar = bars[-1]
    last_day = _to_date(last_bar.get("date")) or datetime.date.today()
    holding_days = max(0, (last_day - entry_date).days)
    price = _f(last_bar.get("close"))
    cost_value = _f(cost) or 0.0
    pnl_pct = round((price - cost_value) / cost_value * 100.0, 2) if cost_value > 0 else None

    # 建仓日之后的 K 线：创新高判定只能看这段，用全量会把建仓前的高点算进来
    since_entry = [b for b in bars if (_to_date(b.get("date")) or entry_date) >= entry_date]
    if not since_entry:
        result.update({"available": False, "reason": "no_bars_since_entry"})
        return result
    highs = [(b, _f(b.get("high")) or _f(b.get("close"))) for b in since_entry]
    peak_bar, peak_price = max(highs, key=lambda pair: pair[1] or 0.0)
    # 「最近 N 根」按 K 线频率换算：日线 10 根 ≈ 周线 4 根 ≈ 月线 1 根
    lookback = max(1, int(round(int(cfg.get("new_high_lookback_bars", 10)) * freq_scale)))
    recent = since_entry[-lookback:] if len(since_entry) >= lookback else since_entry
    recent_high = max((_f(b.get("high")) or _f(b.get("close")) or 0.0) for b in recent)
    tolerance = float(cfg.get("new_high_tolerance_pct", 0.5)) / 100.0
    peak_price = peak_price or 0.0
    made_new_high = bool(peak_price > 0 and recent_high >= peak_price * (1 - tolerance))
    drawdown_from_peak = (round((price - peak_price) / peak_price * 100.0, 2)
                          if peak_price > 0 else None)

    phase = str(((env or {}).get("sentiment") or {}).get("phase") or "unknown")
    scale_map = cfg.get("phase_scale") or _DEFAULT_RULES["phase_scale"]
    scale = float(scale_map.get(phase, scale_map.get("unknown", 1.0)) or 1.0)
    base_days = float(cfg.get("base_days", 20))

    # 炒作度修正（用户理念的量化）：近一年没炒过 → 价位不高，值得多等；
    # 刚炒过 → 退潮后容易长期阴跌，反而要快走。两者性质不同，不能用同一个阈值。
    hype_info = hype or {}
    if not hype_info and result["kline_frequency"] == "d":
        try:
            from .value_position_service import analyze_hype_and_position

            hype_info = analyze_hype_and_position(klines, window=250)
        except Exception:  # noqa: BLE001
            hype_info = {}
    hype_score = _f(hype_info.get("hype_score"))
    days_since = _f(hype_info.get("days_since_last_surge"))
    dormant = bool(hype_info.get("dormant"))
    hype_scale = 1.0
    hype_label = "中性"
    if dormant or (hype_score is not None and hype_score <= float(cfg.get("hype_dormant_score", 0.2))):
        hype_scale = float(cfg.get("hype_dormant_scale", 1.6))
        hype_label = "沉寂（近一年未炒作，给时间等回归）"
    elif ((days_since is not None and days_since <= float(cfg.get("hype_hot_days", 60)))
          or (hype_score is not None and hype_score >= float(cfg.get("hype_hot_score", 0.5)))):
        hype_scale = float(cfg.get("hype_hot_scale", 0.6))
        hype_label = "刚炒作过（退潮后易长期阴跌，快走）"

    threshold_days = max(1, int(round(base_days * scale * hype_scale)))

    reasons: List[str] = []
    applicable: List[str] = []
    if pnl_pct is not None and pnl_pct < 0:
        applicable.append("亏损")
    if holding_days >= threshold_days:
        applicable.append(f"持有 {holding_days} 天 ≥ 阈值 {threshold_days} 天")
    if not made_new_high:
        applicable.append(f"最近 {lookback} 根未创新高（高点回撤 {abs(drawdown_from_peak or 0):.1f}%）")
    if len(applicable) == 3:
        reasons = applicable

    result.update({
        "available": True,
        "entry_date": entry.get("entry_date"),
        "coverage": entry.get("coverage"),
        "partial": entry.get("partial"),
        "holding_days": holding_days,
        "holding_bars": len(since_entry),
        "shares": shares,
        "cost": round(cost_value, 4),
        "price": round(price, 4) if price else None,
        "pnl_pct": pnl_pct,
        "pnl_amount": round((price - cost_value) * shares, 2) if price and cost_value else None,
        "peak_price": round(peak_price, 4) if peak_price else None,
        "peak_date": str((peak_bar or {}).get("date"))[:10],
        "drawdown_from_peak_pct": drawdown_from_peak,
        "recent_high": round(recent_high, 4) if recent_high else None,
        "lookback_bars": lookback,
        "made_new_high": made_new_high,
        "phase": phase,
        "phase_scale": scale,
        "base_days": base_days,
        "threshold_days": threshold_days,
        "hype_score": hype_score,
        "hype_label": hype_label,
        "hype_scale": hype_scale,
        "days_since_last_surge": days_since,
        "dormant": dormant,
        "applicable": applicable,
        "triggered": bool(reasons),
        "signal": "time_stop" if reasons else "",
        "severity": "medium" if reasons else "",
    })
    # 已豁免：事实仍是「触发」，但不再推送；到期自动恢复。
    # 注意不要清空 signal —— 要让报告里能看出「这是被豁免的触发」，
    # 否则看起来就像从没触发过，复盘时查不到。
    if reasons:
        override = active_override(sec_id, today=today)
        if override:
            result["overridden"] = True
            result["override"] = override
            result["severity"] = "info"
    return result


def format_result(item: Dict[str, Any]) -> str:
    """渲染成 agent 可读文字。"""
    name = item.get("name") or item.get("secID")
    if not item.get("available"):
        hint = item.get("hint") or ""
        return f"{name}：时间止损无法评估（{item.get('reason')}{'，' + hint if hint else ''}）"

    bits = [f"{name} 持有 {item.get('holding_days')} 天"]
    if item.get("pnl_pct") is not None:
        bits.append(f"浮亏 {item['pnl_pct']:.2f}%" if item["pnl_pct"] < 0
                    else f"浮盈 {item['pnl_pct']:.2f}%")
    if item.get("drawdown_from_peak_pct") is not None:
        bits.append(f"距持有期高点 {item['drawdown_from_peak_pct']:.1f}%")
    bits.append(f"阈值 {item.get('threshold_days')} 天"
                f"（{item.get('phase')} × {item.get('phase_scale')}"
                f"，{item.get('hype_label')} × {item.get('hype_scale')}）")
    head = "【触发时间止损】" if item.get("triggered") else "【未触发】"
    line = head + "，".join(bits)
    if item.get("triggered"):
        line += f"\n  依据：{' / '.join(item.get('applicable') or [])}"
        line += "\n  建议：减半观察。若判为周期底部豁免，必须写明依据（估值分位/基本面/环境），不得口头跳过。"
    if item.get("partial"):
        line += f"\n  ⚠️ 流水只覆盖 {(item.get('coverage') or 0):.0%} 的持仓，实际持有天数可能更久"
    return line
