"""Agent 工具层：把平台已有能力封装成 LLM 可调用的语义工具。

设计要点
--------
1. **不重写业务逻辑**：所有 handler 都转调现有 `stock_service` / `backtest_service`，
   这里只做「参数归一化 + 结果摘要」。
2. **返回给 LLM 的是紧凑文本**，不是原始 JSON。原始 K 线有 284 根、筹码有上百个价位，
   直接塞进上下文会烧掉大量 token 且干扰判断。每个工具都配了摘要器。
3. **风险分级**：read 可自由调用；write 会改配置，runtime 默认拦截；
   external 会触发外部通知（钉钉），只能由系统流程调用。

handler 统一签名：`(args: dict) -> dict`
返回：{"ok": bool, "text": str, "data": Any, "source": str}
  - text: 给 LLM 看的紧凑摘要
  - data: 给前端渲染的结构化数据
  - source: 数据来源标识（诚信披露，前端会显示）
"""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.trading_time import market_clock, trading_status
from ..services.backtest_service import backtest_service
from ..services.sell_target_service import build_sell_plan, normalize_style
from ..services.stock_service import stock_service
from ..services.strategy_service import StrategyService
from ..services.trade_ledger import trade_ledger

logger = logging.getLogger(__name__)

_STYLE_LABEL = {"long": "长线", "mid_long": "中长线", "swing": "波段"}


@dataclass
class ToolSpec:
    name: str
    group: str
    desc: str
    params: Dict[str, str] = field(default_factory=dict)
    risk: str = "read"  # read | write | external
    handler: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# 通用工具函数
# ---------------------------------------------------------------------------

def _f(value: Any) -> Optional[float]:
    """安全转 float。"""
    if value is None or value == "":
        return None
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return None
    if fv != fv:  # NaN
        return None
    return fv


def _pct(value: Any, digits: int = 2) -> str:
    fv = _f(value)
    if fv is None:
        return "-"
    return f"{fv:+.{digits}f}"


def _num(value: Any, digits: int = 2) -> str:
    fv = _f(value)
    if fv is None:
        return "-"
    return f"{fv:.{digits}f}"


def _amount(value: Any) -> str:
    """成交额转亿/万。"""
    fv = _f(value)
    if fv is None:
        return "-"
    if abs(fv) >= 1e8:
        return f"{fv / 1e8:.2f}亿"
    if abs(fv) >= 1e4:
        return f"{fv / 1e4:.0f}万"
    return f"{fv:.0f}"


def _ok(text: str, data: Any = None, source: str = "") -> Dict[str, Any]:
    return {"ok": True, "text": text, "data": data, "source": source}


def _err(message: str, source: str = "") -> Dict[str, Any]:
    return {"ok": False, "text": message, "data": None, "source": source}


_CODE_LIKE = (
    re.compile(r"^(SH|SZ|BJ)\d{6}$"),
    re.compile(r"^\d{6}\.(SH|SZ|BJ)$"),
    re.compile(r"^\d{6}$"),
)


def _is_code_like(value: Any) -> bool:
    """判断名字字段是否其实是个代码（服务器上全市场名称表拿不到时会退化成这样）。"""
    text = str(value or "").strip().upper()
    if not text:
        return True
    return any(p.match(text) for p in _CODE_LIKE)


def _display_name(current: Any, secid: str, profile: Optional[Dict[str, Any]] = None) -> str:
    """名字解析：现成名字 -> 公司档案简称 -> 代码。

    服务器环境 AmazingData 无权限时 `_stock_list_cache` 为空，spot 也拿不到 name，
    上游会退化成 secID。这里用 company_info 的「股票简称」兜底，避免前端出现 SZ002230。
    """
    if not _is_code_like(current):
        return str(current).strip()
    profile = profile or {}
    for key in ("short_name", "股票简称", "A股简称", "name"):
        candidate = str(profile.get(key) or "").strip()
        if candidate and not _is_code_like(candidate):
            return candidate
    return str(current or secid)


def _match_favorite(code: str, favorites: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """按代码匹配自选记录：支持 600519 / 600519.SH / sh600519 三种写法。"""
    key = str(code or "").strip().upper()
    if not key:
        return None
    digits = re.sub(r"\D", "", key)
    for it in favorites:
        secid = str(it.get("secID") or "").strip().upper()
        if not secid:
            continue
        if secid == key:
            return it
        if digits and re.sub(r"\D", "", secid) == digits:
            return it
    return None


# ---------------------------------------------------------------------------
# 行情类
# ---------------------------------------------------------------------------

def tool_system_clock(args: Dict[str, Any]) -> Dict[str, Any]:
    """系统时钟 + A股交易日历快照：当前时间、星期、是否交易日、休市状态（周末/节假日）、下一交易日。"""
    info = market_clock()
    parts = [f"当前服务器时间 {info['now']}（{info['weekday']}），{info['status_label']}"]
    if info.get("holiday_name"):
        parts.append(f"休市原因：法定节假日「{info['holiday_name']}」")
    if info.get("next_trading_day"):
        parts.append(f"下一交易日：{info['next_trading_day']}")
    parts.append(f"交易时段：{info['sessions']}")
    verdict = "可以实时监控，行情为实时数据" if info["is_trading_now"] else "非交易时段，行情数据为最近一个交易日收盘"
    parts.append(verdict)
    text = "；".join(parts) + f"。[{info['calendar_note']}]"
    return _ok(text, info, f"系统时钟 + {info['calendar_source']}")


def tool_market_status(args: Dict[str, Any]) -> Dict[str, Any]:
    return tool_system_clock(args)


def tool_market_sentiment(args: Dict[str, Any]) -> Dict[str, Any]:
    """全市场情绪：涨停/炸板/跌停家数、封板率、最高连板、阶段判定、涨停最集中的行业。"""
    date = str(args.get("date") or "").strip() or None
    force = bool(args.get("force"))
    data = stock_service.get_market_sentiment(date=date, force=force) or {}
    if not data.get("ok"):
        return _err(str(data.get("detail") or "情绪数据不可用"), "market_sentiment")

    metrics = data.get("metrics") or {}
    lines = [
        f"市场情绪：{data.get('phase_label')}（温度 {float(data.get('temperature') or 0):.0%}）"
        f"｜{data.get('as_of')}",
        f"涨停 {metrics.get('zt_count')} 家 / 炸板 {metrics.get('zb_count')} 家 / "
        f"跌停 {metrics.get('dt_count')} 家｜封板率 {float(metrics.get('seal_rate') or 0):.0%}"
        f"（炸板率 {float(metrics.get('zb_rate') or 0):.0%}）",
        f"最高连板 {metrics.get('max_board')} 板，二板及以上 {metrics.get('lianban_count')} 家"
        + (f"，强势股 {metrics.get('strong_count')} 家" if metrics.get("strong_count") else ""),
    ]
    if metrics.get("zt_amount_yi"):
        lines.append(f"涨停股成交额合计 {metrics['zt_amount_yi']} 亿，封板资金合计 {metrics.get('seal_fund_yi')} 亿")
    hot = data.get("hot_industries") or []
    if hot:
        lines.append("涨停最集中：" + "、".join(
            f"{x['name']}（{x['zt_count']} 家，最高 {x['max_board']} 板）" for x in hot[:5]))
    if data.get("phase_reason"):
        lines.append(f"阶段判定依据：{data['phase_reason']}")
    trend = data.get("trend") or {}
    if trend.get("zt_change_pct") is not None:
        lines.append(f"涨停家数比上一交易日 {trend['zt_change_pct']:+.0f}%")
    return _ok("\n".join(lines), data, "东方财富涨停池/炸板池/跌停池/强势股池")


def tool_spot(args: Dict[str, Any]) -> Dict[str, Any]:
    code = str(args.get("code") or "").strip()
    if not code:
        return _err("缺少参数 code（如 600519 或 600519.SH）")
    info = stock_service.get_stock_spot_info(code)
    if not info or not _f(info.get("price")):
        return _err(f"取不到 {code} 的行情（可能代码错误或数据源不可用）", "spot")
    name = info.get("name") or code
    text = (
        f"{info.get('code') or code} {name}：最新 {_num(info.get('price'))}，"
        f"涨跌 {_pct(info.get('pct_chg'))}%（{_num(info.get('change'))}），"
        f"开 {_num(info.get('open'))} 高 {_num(info.get('high'))} 低 {_num(info.get('low'))} "
        f"昨收 {_num(info.get('prev_close'))}，量 {_amount(info.get('volume'))} 额 {_amount(info.get('amount'))}，"
        f"数据源 {info.get('source') or '-'}。"
    )
    return _ok(text, info, str(info.get("source") or ""))


def _sum_kline(code: str, bars: List[Dict[str, Any]], recent: int = 5, window: int = 60) -> Dict[str, Any]:
    """把 K 线序列压缩成一段话 + 结构化摘要。"""
    if not bars:
        return {"text": f"{code} 无 K 线数据", "data": None}

    last = bars[-1]
    closes = [_f(b.get("close")) for b in bars]
    closes = [c for c in closes if c is not None]
    win = bars[-window:] if len(bars) >= window else bars
    win_closes = [_f(b.get("close")) for b in win]
    win_closes = [c for c in win_closes if c is not None]

    close = _f(last.get("close"))
    ma5, ma10, ma20, ma60 = (_f(last.get(k)) for k in ("MA5", "MA10", "MA20", "MA60"))
    dif, dea, rsi6 = _f(last.get("DIF")), _f(last.get("DEA")), _f(last.get("RSI6"))

    # 均线关系
    ma_parts = []
    for label, ma in (("MA5", ma5), ("MA10", ma10), ("MA20", ma20), ("MA60", ma60)):
        if ma is None or close is None:
            continue
        diff_pct = (close - ma) / ma * 100 if ma else 0
        pos = "上方" if close >= ma else "下方"
        ma_parts.append(f"{label} {ma:.2f}（价在{pos} {abs(diff_pct):.1f}%）")
    ma_text = "｜".join(ma_parts) if ma_parts else "均线数据缺失"

    # MACD 状态
    macd_text = "MACD 数据缺失"
    if dif is not None and dea is not None:
        state = "DIF 在 DEA 上方（多头）" if dif > dea else "DIF 在 DEA 下方（空头）"
        cross_days = None
        for i in range(len(bars) - 1, max(len(bars) - 8, 0), -1):
            cur_d, cur_e = _f(bars[i].get("DIF")), _f(bars[i].get("DEA"))
            prev_d, prev_e = _f(bars[i - 1].get("DIF")), _f(bars[i - 1].get("DEA"))
            if None in (cur_d, cur_e, prev_d, prev_e):
                continue
            if (cur_d > cur_e) != (prev_d > prev_e):
                cross_days = len(bars) - 1 - i
                break
        if cross_days is None:
            macd_text = f"{state}，近 7 日无交叉"
        else:
            kind = "金叉" if dif > dea else "死叉"
            macd_text = f"{state}，{cross_days} 日前出现{kind}"

    # 量能
    vol_text = "量能数据缺失"
    vols = [_f(b.get("volume")) for b in win[-21:]]
    vols = [v for v in vols if v]
    cur_vol = _f(last.get("volume"))
    if cur_vol and len(vols) >= 5:
        avg = sum(vols) / len(vols)
        vol_text = f"最新量 / 20日均量 = {cur_vol / avg:.2f} 倍"

    # 区间位置
    pos_text = ""
    if win_closes:
        hi, lo = max(win_closes), min(win_closes)
        if hi > lo and close is not None:
            pct = (close - lo) / (hi - lo) * 100
            pos_text = f"近{len(win)}日区间 {lo:.2f} ~ {hi:.2f}，当前位置 {pct:.0f}%"

    recent_lines = []
    for b in bars[-recent:]:
        recent_lines.append(f"{b.get('date')} 收{_num(b.get('close'))}({_pct(b.get('pctChg'))}%)")

    text = (
        f"{code} 日线（共 {len(bars)} 根，{bars[0].get('date')} ~ {last.get('date')}）\n"
        f"最新：{last.get('date')} 收 {_num(close)}（{_pct(last.get('pctChg'))}%），"
        f"开{_num(last.get('open'))} 高{_num(last.get('high'))} 低{_num(last.get('low'))}\n"
        f"均线：{ma_text}\n"
        f"{macd_text}｜RSI6 {_num(rsi6, 1)}\n"
        f"{vol_text}\n"
        f"{pos_text}\n"
        f"近{len(recent_lines)}日：{' '.join(recent_lines)}"
    )
    data = {
        "code": code,
        "last_date": last.get("date"),
        "close": close,
        "pct_chg": _f(last.get("pctChg")),
        "ma": {"ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60},
        "macd": {"dif": dif, "dea": dea},
        "rsi6": rsi6,
        "bars": len(bars),
        "start_date": bars[0].get("date"),
        "end_date": last.get("date"),
    }
    return {"text": text, "data": data}


def tool_kline(args: Dict[str, Any]) -> Dict[str, Any]:
    code = str(args.get("code") or "").strip()
    if not code:
        return _err("缺少参数 code")
    days = int(_f(args.get("days")) or 120)
    days = max(20, min(days, 280))
    end = datetime.datetime.now().strftime("%Y-%m-%d")
    start = (datetime.datetime.now() - datetime.timedelta(days=int(days * 1.7) + 20)).strftime("%Y-%m-%d")
    bars = stock_service.get_k_line_data(code, start, end, "d", caller="agent")
    if not bars:
        return _err(f"取不到 {code} 的 K 线", "kline")
    bars = bars[-days:]
    summary = _sum_kline(code, bars)
    return _ok(summary["text"], summary["data"], "xueqiu 日线")


def tool_minute(args: Dict[str, Any]) -> Dict[str, Any]:
    code = str(args.get("code") or "").strip()
    if not code:
        return _err("缺少参数 code")
    data = stock_service.get_realtime_minute_data(code)
    if not data:
        return _err(f"取不到 {code} 的分时数据（可能未开盘或无权限）", "minute")

    # 分时结构：取收盘价序列，分析量能是否集中在尾盘
    closes = []
    for item in data:
        if isinstance(item, dict):
            c = _f(item.get("close") if "close" in item else item.get("price"))
            if c is not None:
                closes.append(c)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            c = _f(item[-1])
            if c is not None:
                closes.append(c)
    if len(closes) < 10:
        return _err(f"{code} 分时数据不足（{len(closes)} 点）", "minute")

    n = len(closes)
    head, tail = closes[: max(1, n // 3)], closes[-max(1, n // 10):]
    text = (
        f"{code} 分时共 {n} 个点：开盘 {closes[0]:.2f}，收盘 {closes[-1]:.2f}，"
        f"最高 {max(closes):.2f}，最低 {min(closes):.2f}，"
        f"日内振幅 {(max(closes) - min(closes)) / closes[0] * 100:.2f}%，"
        f"尾盘 10% 时段价格从 {tail[0]:.2f} 到 {tail[-1]:.2f}"
        f"（{(tail[-1] - tail[0]) / tail[0] * 100:+.2f}%）。"
    )
    return _ok(text, {"points": n, "open": closes[0], "close": closes[-1], "high": max(closes), "low": min(closes)}, "分时")


def tool_indices(args: Dict[str, Any]) -> Dict[str, Any]:
    payload = stock_service.get_market_indices()
    items = payload.get("data") if isinstance(payload, dict) else payload
    items = items or []
    if not items:
        return _err("取不到大盘指数", "indices")
    parts = []
    for it in items[:8]:
        name = it.get("name") or it.get("code") or "?"
        price = _f(it.get("price") if "price" in it else it.get("current"))
        pct = _f(it.get("pct_chg") if "pct_chg" in it else it.get("pctChg"))
        parts.append(f"{name} {_num(price)}（{_pct(pct)}%）")
    text = "大盘指数：" + "｜".join(parts)
    return _ok(text, items, "xueqiu")


# ---------------------------------------------------------------------------
# 持仓类
# ---------------------------------------------------------------------------

def tool_favorites(args: Dict[str, Any]) -> Dict[str, Any]:
    with_quotes = bool(args.get("with_quotes", True))
    items = stock_service.get_favorites_list(include_quotes=with_quotes)
    if not items:
        return _ok("自选股列表为空。", [], "config/favorites.json")

    lines = []
    total_cost = total_value = 0.0
    for it in items:
        secid = it.get("secID") or ""
        name = str(it.get("name") or it.get("secShortName") or "").strip()
        if _is_code_like(name):
            try:
                profile = stock_service.get_company_info(secid) or {}
            except Exception:  # noqa: BLE001
                profile = {}
            name = _display_name(name, secid, profile)
        name = name or secid
        shares = int(_f(it.get("shares")) or 0)
        cost = _f(it.get("cost")) or 0.0
        price = _f(it.get("price"))
        pct = _f(it.get("pctChg") if it.get("pctChg") is not None else it.get("pct_chg"))
        horizon = it.get("horizon") or "-"
        seg = f"{secid} {name}｜持仓{shares}股 成本{cost:.2f}"
        if price is not None:
            seg += f" 现价{price:.2f}（{_pct(pct)}%）"
            total_value += price * shares
        total_cost += cost * shares
        seg += f" 周期{horizon}"
        if it.get("profit_pct") is not None:
            seg += f" 浮盈亏{_pct(it.get('profit_pct'))}%"
        lines.append(seg)

    summary = f"自选股 {len(items)} 只"
    if total_cost:
        summary += f"，持仓成本合计 {total_cost:,.0f} 元"
    if total_value:
        summary += f"、市值合计 {total_value:,.0f} 元、浮动盈亏 {(total_value - total_cost):+,.0f} 元（{(total_value - total_cost) / total_cost * 100:+.2f}%）"
    text = summary + "\n" + "\n".join(lines)
    return _ok(text, items, "config + 行情")


def tool_position_summary(args: Dict[str, Any]) -> Dict[str, Any]:
    """组合视角汇总：市值、集中度、行业暴露、止损距离。纯计算，不依赖 LLM。"""
    items = stock_service.get_favorites_list(include_quotes=True)
    if not items:
        return _ok("自选股为空，无法计算组合风险。", None, "本地计算")

    # 账户口径统一走 get_account_snapshot：仓位 = 市值 ÷ 总资产（本金只是历史投入，不能当分母）
    account = stock_service.get_account_snapshot(items) or {}
    total_capital = _f(account.get("total_capital")) or 0.0
    available_cash = _f(account.get("available_cash")) or 0.0
    position_ratio = _f(account.get("position_ratio"))
    ammo_state = str(account.get("ammo_state") or "unknown")
    ammo_label = str(account.get("ammo_label") or "未知")

    rows = []
    total_value = 0.0
    total_cost = 0.0
    for it in items:
        shares = int(_f(it.get("shares")) or 0)
        cost = _f(it.get("cost")) or 0.0
        price = _f(it.get("price"))
        secid = it.get("secID") or ""
        value = (price or cost) * shares
        total_value += value
        total_cost += cost * shares

        industry = ""
        profile: Dict[str, Any] = {}
        try:
            profile = stock_service.get_company_info(secid) or {}
            industry = str(profile.get("industry") or profile.get("行业") or "").strip()
        except Exception:  # noqa: BLE001
            industry = ""
        name = _display_name(it.get("name") or it.get("secShortName"), secid, profile)

        rows.append({
            "secID": secid,
            "name": name,
            "shares": shares,
            "cost": cost,
            "price": price,
            "value": value,
            "pct": _f(it.get("pctChg") if it.get("pctChg") is not None else it.get("pct_chg")),
            "profit_pct": _f(it.get("profit_pct")),
            "industry": industry,
        })

    base = total_value or 1.0
    for r in rows:
        r["weight"] = r["value"] / base * 100
        if r["cost"] and r["price"]:
            pnl = (r["price"] / r["cost"] - 1) * 100
            r["stop_distance"] = pnl                     # 距成本
            r["stop_line_distance"] = pnl + 8.0          # 距 -8% 止损线的余量，负数=已击穿

    rows.sort(key=lambda x: x["value"], reverse=True)
    top = rows[0]
    industry_weight: Dict[str, float] = {}
    for r in rows:
        key = r["industry"] or "未分类"
        industry_weight[key] = industry_weight.get(key, 0.0) + r["weight"]

    lines = [f"组合市值 {total_value:,.0f} 元，成本 {total_cost:,.0f} 元，"
             f"浮动盈亏 {(total_value - total_cost):+,.0f} 元（{(total_value - total_cost) / (total_cost or 1) * 100:+.2f}%）"]
    total_assets = _f(account.get("total_assets"))
    if position_ratio is not None and total_assets:
        cash_ratio = _f(account.get("cash_ratio")) or 0.0
        lines.append(f"账户总资产 {total_assets:,.0f} 元（市值 {total_value:,.0f} + 现金 {available_cash:,.0f}），"
                     f"真实仓位 {position_ratio:.1%}，现金占比 {cash_ratio:.1%}")
    elif total_capital:
        lines.append(f"投入本金 {total_capital:,.0f} 元（未填可用资金，无法算真实仓位）")
    if total_capital:
        # 整体盈亏必须用「总资产 − 本金」。
        # 用「市值 − 本金」会把还没买成股票的现金当成亏损，虚增亏损，
        # 例如 27 万本金、25.25 万买入、1.7 万现金：市值口径显示 -4.5 万，真实只亏 2.7 万。
        pnl_base = total_assets if total_assets else total_value
        scope = "总资产" if total_assets else "市值（现金未知，可能高估亏损）"
        pnl_total = pnl_base - total_capital
        lines.append(f"整体盈亏（含已实现，= {scope} − 本金） {pnl_total:+,.0f} 元"
                     f"（{pnl_total / total_capital * 100:+.2f}%）")
    realized = _f(account.get("realized_pnl"))
    if realized is not None:
        lines.append(f"已实现盈亏（来自 {int(account.get('trade_count') or 0)} 笔交易流水） {realized:+,.0f} 元"
                     f"，累计手续费 {( _f(account.get('trade_fee_total')) or 0):,.0f} 元")
    lines.append(f"弹药状态：{ammo_label}｜{account.get('reason') or ''}")
    if account.get("buy_allowed") is False:
        lines.append("买入已禁止：账户满仓/弹药紧，任何买入或加仓建议都不要输出，只能给持有、减仓、换仓方案")
    elif available_cash <= 0:
        lines.append("可用资金为 0 或未知：禁止建议买入/加仓，只能建议持有、减仓、调仓或等待补充资金")
    lines.append("个股权重：" + "｜".join(
        f"{r['name']} {r['weight']:.1f}%" for r in rows))
    lines.append("行业暴露：" + "｜".join(
        f"{k} {v:.1f}%" for k, v in sorted(industry_weight.items(), key=lambda x: -x[1])))
    lines.append(f"最大单一标的：{top['name']} {top['weight']:.1f}%"
                 + ("（超过 35% 阈值，集中度偏高）" if top["weight"] > 35 else "（未超 35% 阈值）"))

    breached = [r for r in rows if r.get("stop_line_distance") is not None and r["stop_line_distance"] <= 0]
    if breached:
        lines.append("已跌破 -8% 止损线：" + "｜".join(
            f"{r['name']} 浮亏{r['stop_distance']:+.2f}%" for r in breached))
    near = [r for r in rows if r.get("stop_line_distance") is not None and 0 < r["stop_line_distance"] <= 3]
    if near:
        lines.append("距 -8% 止损线不足 3 个点：" + "｜".join(
            f"{r['name']} 还剩 {r['stop_line_distance']:.1f} 个点" for r in near))

    data = {
        "total_value": total_value,
        "total_cost": total_cost,
        "total_capital": total_capital,
        "total_pnl_incl_realized": round((total_assets or total_value) - total_capital, 2) if total_capital else None,
        "realized_pnl": round(realized, 2) if realized is not None else None,
        "trade_count": int(account.get("trade_count") or 0),
        "trade_fee_total": round(_f(account.get("trade_fee_total")) or 0.0, 2),
        "available_cash": round(available_cash, 2),
        "position_ratio": position_ratio,
        "ammo_state": ammo_state,
        "buy_allowed": account.get("buy_allowed"),
        "positions": rows,
        "industry_weight": industry_weight,
    }
    return _ok("\n".join(lines), data, "本地计算 + 行情")


def tool_account_status(args: Dict[str, Any]) -> Dict[str, Any]:
    """账户资金全景：持仓市值、可用现金、总资产、真实仓位、弹药状态。

    和 get_position_summary 的分工：这个只回答"我还有多少钱、仓位多少、还能不能买"，
    不拉单只票的行业与止损分析，所以快；要组合风险看 get_position_summary。
    """
    snap = stock_service.get_account_snapshot()
    if not snap.get("ok"):
        return _err("账户快照计算失败", "account_status")

    mv = _f(snap.get("market_value")) or 0.0
    cost = _f(snap.get("held_cost")) or 0.0
    pnl = _f(snap.get("position_pnl")) or 0.0
    pnl_pct = _f(snap.get("position_pnl_pct"))
    cash = _f(snap.get("available_cash"))
    n = int(snap.get("held_count") or 0)

    lines: List[str] = []
    if n:
        pct_text = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "-"
        lines.append(f"持仓 {n} 只，市值 {mv:,.0f} 元，成本 {cost:,.0f} 元，浮动盈亏 {pnl:+,.0f} 元（{pct_text}）")
    else:
        lines.append("当前没有记录持仓（favorites 里没有 shares>0 的标的）")

    source_text = {"explicit": "用户告知的真实账户资金",
                   "estimated": "按「本金−持仓成本」估算，卖出过会偏大",
                   "unknown": "未知"}.get(str(snap.get("cash_source") or ""), "未知")
    if cash is None:
        lines.append("可用现金：未知（未设置本金与可用资金），系统不做买入门控")
    else:
        lines.append(f"可用现金 {cash:,.0f} 元（来源：{source_text}）")
        ta = _f(snap.get("total_assets")) or 0.0
        pos = _f(snap.get("position_ratio")) or 0.0
        cash_ratio = _f(snap.get("cash_ratio")) or 0.0
        lines.append(f"账户总资产 {ta:,.0f} 元｜持仓仓位 {pos:.1%}｜现金占比 {cash_ratio:.1%}"
                     f"（仓位 = 市值 ÷ 总资产，不是 ÷ 本金）")

    state = str(snap.get("ammo_state") or "unknown")
    label = str(snap.get("ammo_label") or "未知")
    buy_allowed = snap.get("buy_allowed") is not False
    realized = _f(snap.get("realized_pnl"))
    if realized is not None:
        lines.append(f"已实现盈亏 {realized:+,.0f} 元（按 {int(snap.get('trade_count') or 0)} 笔交易流水计算，"
                     f"累计手续费 {(_f(snap.get('trade_fee_total')) or 0):,.0f} 元）")
    lines.append(f"弹药状态：{label}（{state}）｜{snap.get('reason') or ''}")
    lines.append("买入提醒：" + ("允许" if buy_allowed else "已关闭——满仓/弹药紧时钉钉只推卖出与风险，不推买入"))

    # 情绪维度的仓位纪律（用户理念：低情绪留现金建仓、高情绪减仓兑现、不博傻）
    _sp = snap.get("sentiment_policy") or {}
    if _sp.get("ok"):
        _pmin = _f(_sp.get("target_position_min")) or 0.0
        _pmax = _f(_sp.get("target_position_max")) or 0.0
        lines.append(
            f"市场情绪：{_sp.get('regime_label')}｜按情绪的目标总仓位 {_pmin:.0%}~{_pmax:.0%}"
            f"｜{_sp.get('gap_action')}"
        )

    if str(snap.get("cash_source") or "") == "estimated":
        lines.append("提示：现金是估算值。只要卖出过一次，这个数就会偏大（成本全额释放、实际只回收市值），"
                     "建议在设置页填一次券商里的「可用资金」，或直接告诉我金额。")
    return _ok("\n".join(lines), snap, "本地计算 + 持仓行情")


def tool_sentiment_policy(args: Dict[str, Any]) -> Dict[str, Any]:
    """市场情绪 → 仓位与现金纪律。

    用户理念：情绪低落时留有现金、开始建仓；情绪疯狂时卖出；绝不参与博傻游戏。
    这个工具把情绪翻译成可执行的仓位政策，而不是让 agent 自己拍脑袋。
    """
    try:
        from ..services.sentiment_position_service import (
            build_sentiment_position_policy,
            format_sentiment_policy,
        )
        sentiment = stock_service.get_market_sentiment() or {}
        market = stock_service.get_market_regime() or {}
        account: Dict[str, Any] = {}
        try:
            account = stock_service.get_account_snapshot() or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("sentiment_policy_account_failed error=%r", exc)
        policy = build_sentiment_position_policy(
            sentiment=sentiment,
            market=market,
            account={"position_ratio": account.get("position_ratio")},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("sentiment_policy_failed error=%r", exc)
        return _err(f"市场情绪策略计算失败：{exc}", "sentiment_policy")

    if not policy.get("ok"):
        return _err("市场情绪策略不可用（情绪数据缺失），按原有纪律常规管理", "sentiment_policy")

    text = format_sentiment_policy(policy)

    # 账户实际仓位与目标仓位的差距——这决定"现在到底该动多少"
    actual = _f(account.get("position_ratio"))
    if actual is not None:
        text += (
            f"\n实际仓位：{actual:.1%}"
            f"（目标 {float(policy.get('target_position_min') or 0):.0%}~"
            f"{float(policy.get('target_position_max') or 0):.0%}）"
            f"｜{policy.get('gap_action')}"
        )
    cash = _f(account.get("available_cash"))
    if cash is not None:
        text += f"\n可用现金：{cash:,.0f} 元"

    return _ok(text, policy, "东方财富涨停/炸板/跌停池 + 上证指数 250 日位置 + 账户快照")


def tool_sentiment_history(args: Dict[str, Any]) -> Dict[str, Any]:
    """市场情绪历史曲线与周期位置。

    光知道"今天情绪多少"不够，还要知道：
      - 当前温度在历史上处于什么位置（今天 49% 是刚从冰点起来，还是从亢奋掉下来）
      - 这一轮情绪走了多久（持续 3 天的亢奋 ≠ 持续 3 周的退潮）
      - 涨停家数是否腰斩（退潮最硬的证据）
      - 阶段转折点在哪（什么时候从亢奋切到退潮）

    这些决定了"现在该建仓、该持有、还是该兑现"。
    """
    from ..services.sentiment_history_service import sentiment_history_service

    try:
        current = stock_service.get_market_sentiment() or {}
        view = sentiment_history_service.get_history_view(current=current)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sentiment_history_failed error=%r", exc)
        return _err(f"情绪历史获取失败：{exc}", "sentiment_history")

    if not view.get("ok"):
        return _err(
            f"情绪历史不可用：{view.get('error') or '数据不足'}",
            "sentiment_history",
        )

    text = sentiment_history_service.format_history_view(view)
    cycle = view.get("cycle") or {}
    points = view.get("points") or []

    # 阶段转折点：这是判断"退潮中的反弹"还是"真反转"的关键
    transitions = cycle.get("transitions") or []
    if transitions:
        lines = ["近期情绪阶段转折："]
        for t in transitions[-5:]:
            lines.append(
                f"  {t.get('date')}：{t.get('from_label')} → {t.get('to_label')}"
                + (f"（温度 {float(t.get('temperature') or 0):.0%}）" if t.get("temperature") is not None else "")
            )
        text += "\n" + "\n".join(lines)

    return _ok(
        text,
        {
            "cycle": cycle,
            "count": view.get("count"),
            "points": points[-30:],
            "phase_days": view.get("phase_days"),
        },
        "config/sentiment_history.json（每日情绪快照）",
    )


def tool_update_account_cash(args: Dict[str, Any]) -> Dict[str, Any]:
    """用户在本轮对话里明确说出可用资金/本金时，写入设置。

    安全约束：只写用户给出的数值，不推断、不补零。
    available_cash 传 0 是有意义的（真的没现金了），不能把 0 当成"未提供"。
    """
    cash = _f(args.get("available_cash"))
    total = _f(args.get("total_capital"))
    if cash is None and total is None:
        return _err('需要 available_cash 或 total_capital 至少一个数值，例如 {"available_cash": 32000}',
                    "update_account_cash")
    if cash is not None and cash < 0:
        return _err("available_cash 不能为负数", "update_account_cash")
    if total is not None and total <= 0:
        total = None

    payload: Dict[str, Any] = {}
    if total is not None:
        payload["total_capital"] = float(total)
    if cash is not None:
        payload["available_cash"] = float(cash)
        payload["cash_updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not payload:
        return _err("没有可写入的字段", "update_account_cash")

    stock_service.save_user_settings(payload)
    snap = stock_service.get_account_snapshot(use_cache=False) or {}

    lines: List[str] = []
    if total is not None:
        lines.append(f"本金已更新为 {total:,.0f} 元")
    if cash is not None:
        lines.append(f"可用资金已更新为 {cash:,.0f} 元")
    cash_v = _f(snap.get("available_cash"))
    if cash_v is not None:
        lines.append(f"当前账户总资产 {(_f(snap.get('total_assets')) or 0):,.0f} 元，"
                     f"持仓仓位 {(_f(snap.get('position_ratio')) or 0):.1%}")
    lines.append(f"弹药状态：{snap.get('ammo_label') or '未知'}｜{snap.get('reason') or ''}")
    lines.append("钉钉提醒：" + ("会正常推买入信号" if snap.get("buy_allowed") is not False
                              else "已切为「只推卖出与风险」，不再提醒买入"))
    return _ok("\n".join(lines), snap, "本地写入")


def tool_record_trade(args: Dict[str, Any]) -> Dict[str, Any]:
    """把用户口语报的成交落成真实持仓 + 现金。

    这是「对话即记账」的唯一入口。只认用户说出口的事实：
    标的、买卖方向、股数、成交价。价格缺失时按现价记并明确标注，
    但股数和方向绝不猜。
    """
    code = str(args.get("code") or "").strip()
    if not code:
        return _err("需要 code：股票代码或名称", "record_trade")

    result = trade_ledger.apply_trade(
        code=code,
        side=str(args.get("side") or ""),
        shares=args.get("shares"),
        price=args.get("price"),
        fee=args.get("fee"),
        trade_date=args.get("date") or args.get("trade_date"),
        note=str(args.get("note") or ""),
    )
    if not result.get("ok"):
        return _err(result.get("error") or "记账失败", "record_trade")

    rec = result.get("record") or {}
    side_label = "买入" if rec.get("side") == "buy" else "卖出"
    lines = [
        f"已记账：{side_label} {rec.get('name')}（{rec.get('secID')}）"
        f"{rec.get('shares')} 股 @ {rec.get('price')}，成交额 {(_f(rec.get('amount')) or 0):,.2f} 元，"
        f"手续费 {(_f(rec.get('fee')) or 0):,.2f} 元（{'估算' if (rec.get('fee_detail') or {}).get('estimated') else '按实际'}）",
        f"持仓变化：{rec.get('shares_before')} 股 → {rec.get('shares_after')} 股，"
        f"成本 {(_f(rec.get('cost_before')) or 0):.4f} → {(_f(rec.get('cost_after')) or 0):.4f} 元/股",
    ]
    if rec.get("realized_pnl") is not None:
        rp = _f(rec.get("realized_pnl")) or 0.0
        lines.append(f"已实现盈亏 {rp:+,.2f} 元（(卖价 − 成本)×股数 − 费用）")
    if rec.get("cash_before") is not None:
        lines.append(f"可用现金 {(_f(rec.get('cash_before')) or 0):,.0f} → {(_f(rec.get('cash_after')) or 0):,.0f} 元")
    for warn in result.get("warnings") or []:
        lines.append(f"注意：{warn}")

    snap = stock_service.get_account_snapshot(use_cache=False) or {}
    if snap.get("available_cash") is not None:
        lines.append(f"账户：总资产 {(_f(snap.get('total_assets')) or 0):,.0f} 元，"
                     f"仓位 {(_f(snap.get('position_ratio')) or 0):.1%}，"
                     f"弹药 {snap.get('ammo_label') or '未知'}")
        if snap.get("buy_allowed") is False:
            lines.append("当前已满仓/弹药紧：只提醒卖出与风险，不再推买入")
    return _ok("\n".join(lines), {"record": rec, "account": snap}, "本地记账（config/trade_log.json）")


def tool_list_trades(args: Dict[str, Any]) -> Dict[str, Any]:
    """查交易流水：用于回答「我最近做了哪些操作 / 赚了多少已实现」。"""
    limit = args.get("limit") or 20
    code = str(args.get("code") or "").strip() or None
    since = str(args.get("since") or "").strip() or None
    rows = trade_ledger.list_trades(limit=limit, code=code, since=since, side=args.get("side"))
    summary = trade_ledger.summary(since=since)
    if not rows:
        return _ok("还没有任何交易记录。用户在对话里报成交后会自动记录。", {"items": [], "summary": summary},
                   "config/trade_log.json")
    lines = []
    for r in rows:
        side_label = {"buy": "买入", "sell": "卖出", "adjust": "纠正"}.get(str(r.get("side")), str(r.get("side")))
        pnl = r.get("realized_pnl")
        pnl_text = f"，已实现 {float(pnl):+,.2f}" if pnl is not None else ""
        fee = _f(r.get("fee")) or 0.0
        lines.append(f"{r.get('trade_date')} {side_label} {r.get('name')} {r.get('shares')} 股"
                     f" @ {r.get('price') or '-'}，费用 {fee:.2f}{pnl_text}"
                     f"（记后持股 {r.get('shares_after')}，成本 {r.get('cost_after')}）")
    head = f"最近 {len(rows)} 笔交易："
    tail = []
    if summary.get("realized_pnl") is not None:
        tail.append(f"期间已实现盈亏合计 {summary['realized_pnl']:+,.2f} 元")
    tail.append(f"累计手续费 {summary.get('total_fee', 0):,.2f} 元")
    return _ok("\n".join([head] + lines + ["；".join(tail)]),
               {"items": rows, "summary": summary}, "config/trade_log.json")


def tool_sync_position(args: Dict[str, Any]) -> Dict[str, Any]:
    """纠正持股数/成本（送转股、分红再投、或以前记错了）。不改现金。"""
    code = str(args.get("code") or "").strip()
    if not code:
        return _err("需要 code：股票代码或名称", "sync_position")
    shares = args.get("shares")
    if shares is None:
        return _err("需要 shares：纠正后的实际持股数（可以是 0）", "sync_position")
    result = trade_ledger.sync_position(code=code, shares=shares, cost=args.get("cost"),
                                        note=str(args.get("note") or ""))
    if not result.get("ok"):
        return _err(result.get("error") or "纠正失败", "sync_position")
    rec = result.get("record") or {}
    lines = [
        f"已纠正 {rec.get('name')}（{rec.get('secID')}）：持股 {rec.get('shares_before')} → {rec.get('shares_after')} 股，"
        f"成本 {(_f(rec.get('cost_before')) or 0):.4f} → {(_f(rec.get('cost_after')) or 0):.4f} 元/股",
        "本次是直接覆盖，不产生现金变动；如果这次变化对应真实成交，应该用 record_trade 记账而不是纠正。",
    ]
    return _ok("\n".join(lines), rec, "本地记账（config/trade_log.json）")


def tool_sell_target(args: Dict[str, Any]) -> Dict[str, Any]:
    """多因子卖出目标价：估值（PE/PB 分位+行业比较）+ 财报 + 行业/个股周期 + 题材情绪，
    再与技术压力位合成 T1/T2/T3。不传 code 时对全部持仓批量计算（上限 6 只）。
    """
    code = str(args.get("code") or "").strip()
    style_arg = str(args.get("style") or "").strip().lower()

    settings = stock_service.get_user_settings() or {}
    default_style = style_arg or str(settings.get("investment_style") or "").strip().lower() or "long"

    favorites = stock_service.get_favorites_list(include_quotes=False) or []
    holdings = [it for it in favorites if int(_f(it.get("shares")) or 0) > 0]

    targets: List[Dict[str, Any]] = []
    if code:
        matched = _match_favorite(code, favorites)
        if matched is not None:
            targets = [matched]
        else:
            targets = [{"secID": code, "shares": 0, "cost": 0.0, "horizon": default_style}]
    else:
        targets = holdings[:6]

    if not targets:
        return _ok("当前没有持仓，无法生成卖出计划。可以先在自选页录入持股数与成本。", None, "本地计算")

    end = datetime.datetime.now().strftime("%Y-%m-%d")
    # 拉够 800 天：PE/PB 分位与 250 日均线都靠这段窗口，太短会让估值分位失真
    start = (datetime.datetime.now() - datetime.timedelta(days=800)).strftime("%Y-%m-%d")

    plans: List[Dict[str, Any]] = []
    texts: List[str] = []
    for it in targets:
        secid = str(it.get("secID") or "").strip()
        if not secid:
            continue
        shares = int(_f(it.get("shares")) or 0)
        cost = _f(it.get("cost")) or 0.0
        # 四池是用户口径：有池按池映射到内部风格（股息/低估→long、长赛道→mid_long、催化→swing），
        # 显式 style 参数优先（供 Agent 做假设推演），未分类才看旧 horizon。
        horizon = style_arg or stock_service.effective_horizon(it) or default_style

        bars = stock_service.get_k_line_data(secid, start, end, "d", caller="agent")
        if not bars:
            texts.append(f"{secid} 取不到日 K 线，跳过。")
            continue

        strategy = {}
        try:
            strategy = stock_service.get_trade_strategy_config(horizon) or {}
        except Exception:  # noqa: BLE001
            strategy = {}

        profile: Dict[str, Any] = {}
        try:
            profile = stock_service.get_company_info(secid) or {}
        except Exception:  # noqa: BLE001
            profile = {}
        name = _display_name(it.get("name"), secid, profile)

        # 估值/财报/行业周期/大盘周期：内部都有磁盘缓存，失败降级为空 dict
        context: Dict[str, Any] = {}
        try:
            context = stock_service.get_valuation_context(secid) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("sell_context_failed secid=%s error=%r", secid, exc)
            context = {}

        # 四池：把池标签与持仓周数带进卖出计划，止盈阶梯与时间止损才有依据。
        # 没记买入日期就只能跳过时间止损（宁可不判，也不按 0 周瞎判）。
        weeks_held = None
        buy_date = str(it.get("buy_date") or "").strip()[:10]
        if buy_date:
            try:
                bought = datetime.datetime.strptime(buy_date, "%Y-%m-%d").date()
                weeks_held = max(0.0, (datetime.date.today() - bought).days / 7.0)
            except ValueError:
                weeks_held = None

        plan = build_sell_plan(
            code=secid,
            bars=bars,
            cost=cost,
            shares=shares,
            horizon=horizon,
            strategy=strategy,
            name=name,
            fundamentals=context.get("fundamentals") or {},
            valuation=context.get("valuation") or {},
            industry_stats=context.get("industry_stats") or {},
            industry_name=context.get("industry_name") or str(profile.get("industry") or ""),
            industry_cycle=context.get("industry_cycle") or {},
            industry_valuation=context.get("industry_valuation") or {},
            market=context.get("market") or {},
            leader_weight=context.get("leader_weight"),
            main_business=str(profile.get("main_business") or ""),
            market_sentiment=context.get("sentiment") or {},
            pool=it.get("pool"),
            weeks_held=weeks_held,
        )
        if not plan.get("ok"):
            texts.append(f"{secid} {plan.get('error') or '计算失败'}")
            continue
        plans.append(plan)
        texts.append(plan["text"])

    if not plans:
        return _err("全部标的都算不出卖出计划（K 线数据不可用）", "sell_target")

    header = (f"卖出计划（投资风格：{_STYLE_LABEL.get(normalize_style(default_style), default_style)}｜"
              f"多因子：估值+财报+行业周期+题材情绪）")
    if len(plans) > 1:
        header += f"，共 {len(plans)} 只持仓"
    return _ok(header + "\n\n" + "\n\n".join(texts), plans, "本地计算 + 日K线 + 财报 + 申万行业指数")


def tool_value_position(args: Dict[str, Any]) -> Dict[str, Any]:
    """低位价值视图：近一年有没有炒作过、现在处在什么位置、是不是均值回归机会。

    这是用户炒股理念的结构化载体：
      - 炒作史（单日最大涨幅、大涨次数、距最后一次大涨多久）→ 判断「有没有被炒作过」
      - 相对位置（250 日分位、距高点回撤）→ 判断「现在是不是不高」
      - 深跌 + 基本面未证伪 → 均值回归 / 戴维斯双击的前提
      - 大盘与板块位置 → 判断外部环境配不配合
      - 安全边际 → 好公司太贵不能碰，烂公司极低也是机会
    """
    from ..services.value_position_service import build_value_view, format_value_view

    code = str(args.get("code") or "").strip()
    window = int(_f(args.get("window")) or 250)
    window = max(120, min(window, 250))

    favorites = stock_service.get_favorites_list(include_quotes=False) or []
    holdings = [it for it in favorites if int(_f(it.get("shares")) or 0) > 0]

    if code:
        matched = _match_favorite(code, favorites)
        targets = [matched] if matched is not None else [{"secID": code, "shares": 0, "cost": 0.0}]
    else:
        targets = holdings[:6]
    if not targets:
        return _err("没有可分析的标的：请传 code，或先在自选页录入持仓", "value_position")

    end = datetime.datetime.now().strftime("%Y-%m-%d")
    start = (datetime.datetime.now() - datetime.timedelta(days=800)).strftime("%Y-%m-%d")

    views = []
    texts = []
    for it in targets:
        secid = str(it.get("secID") or "").strip()
        if not secid:
            continue
        try:
            bars = stock_service.get_k_line_data(secid, start, end, "d", caller="agent")
        except Exception as exc:  # noqa: BLE001
            texts.append(f"{secid} 取 K 线失败：{exc}")
            continue
        if not bars:
            texts.append(f"{secid} 取不到日 K 线，跳过。")
            continue

        context: Dict[str, Any] = {}
        try:
            context = stock_service.get_valuation_context(secid) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("value_context_failed secid=%s error=%r", secid, exc)
            context = {}

        valuation = context.get("valuation") or {}
        industry_stats = context.get("industry_stats") or {}
        industry_name = str(context.get("industry_name") or "")
        try:
            from ..services.sell_factors import classify_archetype
            archetype = classify_archetype(industry_name, "")
        except Exception:  # noqa: BLE001
            archetype = "default"
        pe_pct = None
        pb_pct = None
        try:
            from ..services.sell_factors import factor_valuation

            vf = factor_valuation(bars, _f(valuation.get("pe")), _f(valuation.get("pb")),
                                  industry_stats, archetype,
                                  context.get("industry_valuation") or {})
            pe_pct = vf.get("pe_pct")
            pb_pct = vf.get("pb_pct")
        except Exception as exc:  # noqa: BLE001
            logger.warning("value_valuation_failed secid=%s error=%r", secid, exc)

        quality_score = None
        growth_score = None
        try:
            from ..services.sell_factors import factor_quality, factor_growth
            q = factor_quality(context.get("fundamentals") or {}, archetype)
            g = factor_growth(context.get("fundamentals") or {}, _f(valuation.get("pe")))
            quality_score = q.get("score")
            growth_score = g.get("score")
        except Exception as exc:  # noqa: BLE001
            logger.warning("value_quality_failed secid=%s error=%r", secid, exc)

        name = _display_name(it.get("name"), secid, {})
        view = build_value_view(
            code=secid,
            bars=bars,
            market=context.get("market") or {},
            industry_cycle=context.get("industry_cycle") or {},
            market_sentiment=context.get("sentiment") or {},
            quality_score=quality_score,
            growth_score=growth_score,
            pe_pct=pe_pct,
            pb_pct=pb_pct,
            pb_abs=_f(valuation.get("pb")),
            window=window,
            pe=_f(valuation.get("pe")),
            pb=_f(valuation.get("pb")),
            industry_valuation=context.get("industry_valuation") or {},
        )
        if not view.get("ok"):
            texts.append(f"{name} 低位价值视图不可用：{view.get('error')}")
            continue
        views.append(view)
        texts.append(format_value_view(view, name=name))

    if not views:
        return _err("没有可用的低位价值视图（K 线数据不可用）", "value_position")

    header = "低位价值视图（炒作史 + 相对位置 + 均值回归前提 + 安全边际）"
    if len(views) > 1:
        header += f"，共 {len(views)} 只持仓"
    return _ok(header + "\n\n" + "\n\n".join(texts), views, "本地计算 + 日K线 + 财报 + 申万行业指数")



def tool_industry_valuation(args: Dict[str, Any]) -> Dict[str, Any]:
    """行业估值中枢：这只票在**自己行业**里贵不贵。

    用户明确要求：**「你算 pe、pb 的时候得看行业均位；不能不同行业进行比较，
    每个行业商业模式都不一样。」**

    本工具给出的不是"PE 是 30 倍所以贵"，而是：
      - PE/PB 相对**本行业成分股中位数**的溢价/折价；
      - PE/PB 在**本行业成分股**里的分位（0=本行业最便宜）；
      - 行业中枢本身（成分股中位 + 截尾均值 + 官方指数口径交叉校验）。

    这样银行 PB 0.76 是"银行里的中位水平"，汽车零部件 PB 0.76 是"行业最便宜的 3%"——
    同一把尺子量不同的行业才是对的。
    """
    from ..services.industry_valuation_service import assess_industry_relative

    code = str(args.get("code") or "").strip()
    if not code:
        return _err("需要参数 code（股票代码）", "industry_valuation")

    try:
        context = stock_service.get_valuation_context(code) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("industry_valuation_context_failed code=%s error=%r", code, exc)
        return _err(f"取估值上下文失败：{exc}", "industry_valuation")

    industry = context.get("industry_valuation") or {}
    valuation = context.get("valuation") or {}
    pe = _f(valuation.get("pe"))
    pb = _f(valuation.get("pb"))
    relative = assess_industry_relative(pe, pb, industry)

    industry_name = str(context.get("industry_name") or "") or str(industry.get("industry_name") or "")
    lines = [
        f"【{code}】行业估值坐标系（{industry_name or '所属行业未知'}）",
    ]
    if not relative.get("ok"):
        lines.append(f"行业估值中枢不可用：{relative.get('error')}")
        lines.append("无法做行业内相对比较——请在结论里说明这一点，不要用跨行业的绝对 PE/PB 阈值代替。")
        return _ok("\n".join(lines), relative, "申万二级行业估值")

    lines.append(
        f"本股：PE {pe if pe is not None else '-'}｜PB {pb if pb is not None else '-'}"
    )
    lines.append(
        f"行业中位：PE {relative.get('pe_median')}｜PB {relative.get('pb_median')}"
        + (
            f"（成分股样本 {industry.get('pe_count')}/{industry.get('pb_count')}）"
            if industry.get("pe_count") else "（申万官方行业口径，加权）"
        )
    )
    if relative.get("pe_vs_median_pct") is not None:
        lines.append(
            f"相对行业中位：PE {relative['pe_vs_median_pct']:+.0f}%｜"
            f"PB {relative.get('pb_vs_median_pct'):+.0f}%"
        )
    if relative.get("pe_percentile") is not None:
        lines.append(
            f"行业内分位：PE {float(relative['pe_percentile']):.0%}｜"
            f"PB {float(relative.get('pb_percentile') or 0):.0%}"
            "（0% = 本行业最便宜）"
        )
    zone_label = {"cheap": "相对本行业便宜", "fair": "相对本行业居中", "expensive": "相对本行业偏贵"}.get(
        str(relative.get("valuation_zone")), "无法判定"
    )
    lines.append(f"结论：{zone_label}")
    for note in relative.get("notes") or []:
        lines.append(f"  - {note}")

    return _ok(
        "\n".join(lines),
        {
            "relative": relative,
            "industry_stats": {k: v for k, v in industry.items() if k not in ("peer_pe_list", "peer_pb_list")},
        },
        "申万二级行业成分股估值中枢",
    )


def tool_trade_history(args: Dict[str, Any]) -> Dict[str, Any]:
    code = str(args.get("code") or "").strip()
    count = int(_f(args.get("count")) or 10)
    if not code:
        return _err("缺少参数 code")
    rows = stock_service.get_trade_history(code, count=count)
    if not rows:
        return _ok(f"{code} 无历史成交明细（数据源可能不提供）。", [], "xueqiu")
    text = f"{code} 最近 {len(rows)} 笔成交：" + "｜".join(
        f"{r.get('date') or r.get('time')} {_num(r.get('price'))}×{r.get('volume') or r.get('shares')}" for r in rows[:10])
    return _ok(text, rows, "xueqiu")


# ---------------------------------------------------------------------------
# 分析类
# ---------------------------------------------------------------------------

def tool_chip(args: Dict[str, Any]) -> Dict[str, Any]:
    code = str(args.get("code") or "").strip()
    if not code:
        return _err("缺少参数 code")
    payload = stock_service.get_chip_distribution(code)
    if not payload or payload.get("error"):
        reason = str((payload or {}).get("error") or "空")
        if "no data" in reason.lower():
            reason = "没有可用的 K 线数据"
        return _err(f"{code} 筹码数据不可用：{reason}", "chip")
    cur = _f(payload.get("current_price"))
    avg = _f(payload.get("average_cost"))
    profit_ratio = _f(payload.get("profit_ratio"))
    trapped = _f(payload.get("trapped_ratio"))
    conc = _f(payload.get("concentration"))
    rng = payload.get("cost_range_90") or {}

    # 找最密集的价位区间（上方抛压）
    dist = payload.get("distribution") or []
    dense = ""
    if dist:
        buckets: Dict[int, float] = {}
        for d in dist:
            p = _f(d.get("price"))
            v = _f(d.get("volume")) or 0
            if p is None:
                continue
            buckets[int(p // max(1, (cur or 100) * 0.01))] = buckets.get(int(p // max(1, (cur or 100) * 0.01)), 0) + v
        if buckets:
            top_bucket = max(buckets.items(), key=lambda kv: kv[1])
            dense = f"，最密集价位带约 {top_bucket[0] * max(1, (cur or 100) * 0.01):.1f} 附近"

    text = (
        f"{code} 筹码：现价 {_num(cur)}，平均成本 {_num(avg)}，"
        f"获利盘 {_num(profit_ratio, 1)}%，套牢盘 {_num(trapped, 1)}%，集中度 {_num(conc, 3)}"
        + (f"，90% 成本区间 {_num(_f(rng.get('low')))} ~ {_num(_f(rng.get('high')))}" if isinstance(rng, dict) and rng else "")
        + dense
    )
    return _ok(text, {"current_price": cur, "average_cost": avg, "profit_ratio": profit_ratio,
                      "trapped_ratio": trapped, "concentration": conc, "cost_range_90": rng}, "amazingdata")


def tool_money_flow(args: Dict[str, Any]) -> Dict[str, Any]:
    code = str(args.get("code") or "").strip()
    if not code:
        return _err("缺少参数 code")
    payload = stock_service.get_money_flow(code)
    if not payload or payload.get("error"):
        return _err(f"{code} 资金流数据不可用：{(payload or {}).get('error', '空')}", "money_flow")
    rows = payload.get("data") or []
    if not rows:
        return _err(f"{code} 无资金流记录", "money_flow")
    last = rows[-1]
    # 兼容不同字段命名
    def pick(d: Dict[str, Any], *keys: str) -> Optional[float]:
        for k in keys:
            if k in d:
                v = _f(d.get(k))
                if v is not None:
                    return v
        return None

    main_net = pick(last, "main_net", "主力净流入", "main_net_inflow", "net_amount")
    text = f"{code} 资金流（{len(rows)} 条记录），最新："
    if main_net is not None:
        text += f"主力净流入 {_amount(main_net)}"
    else:
        text += "字段：" + "、".join(list(last.keys())[:10])
    # 连续净流出天数
    net_series = []
    for r in rows[-6:]:
        v = pick(r, "main_net", "主力净流入", "main_net_inflow", "net_amount")
        net_series.append(v)
    if net_series and all(v is not None for v in net_series):
        neg_days = 0
        for v in reversed(net_series):
            if v is not None and v < 0:
                neg_days += 1
            else:
                break
        if neg_days >= 2:
            text += f"；已连续 {neg_days} 日主力净流出"
    return _ok(text, rows[-5:], "eastmoney / amazingdata")


def tool_company(args: Dict[str, Any]) -> Dict[str, Any]:
    code = str(args.get("code") or "").strip()
    if not code:
        return _err("缺少参数 code")
    info = stock_service.get_company_info(code)
    if not info:
        return _err(f"{code} 无公司信息", "company")
    name = info.get("股票简称") or info.get("org_name") or code
    industry = info.get("industry") or info.get("行业") or "-"
    text = (
        f"{code} {name}｜行业：{industry}｜总市值 {_amount(info.get('总市值'))}｜"
        f"流通市值 {_amount(info.get('流通市值'))}｜上市 {info.get('上市时间') or '-'}"
    )
    profile = str(info.get("main_business") or info.get("profile") or "").strip()
    if profile:
        text += f"\n主营：{profile[:200]}"
    data = {k: info.get(k) for k in ("股票简称", "industry", "行业", "总市值", "流通市值", "上市时间") if k in info}
    return _ok(text, data, "本地缓存")


def tool_backtest(args: Dict[str, Any]) -> Dict[str, Any]:
    code = str(args.get("code") or "").strip()
    if not code:
        return _err("缺少参数 code")
    entry = str(args.get("entry_expr") or "").strip() or "CROSS(DIF, DEA)"
    exit_expr = str(args.get("exit_expr") or "").strip() or "CROSS(DEA, DIF)"
    capital = _f(args.get("capital")) or 100000.0
    benchmark = str(args.get("benchmark") or "hs300")

    result = backtest_service.run(
        code, start_date=args.get("start_date"), end_date=args.get("end_date"),
        capital=capital, entry_expr=entry, exit_expr=exit_expr, benchmark=benchmark,
    )
    if not result.get("ok"):
        return _err(f"回测失败：{result.get('error') or result.get('warnings')}", "backtest")

    m = result.get("metrics") or {}
    text = (
        f"{code} 回测（{result.get('start_date')} ~ {result.get('end_date')}，{result.get('bars')} 根K线）\n"
        f"策略：入场 {entry}；出场 {exit_expr}\n"
        f"总收益 {_pct(result.get('return_pct'))}%｜年化 {_pct(m.get('annual_return_pct'))}%｜"
        f"最大回撤 {_num(m.get('max_drawdown_pct'))}%｜夏普 {_num(m.get('sharpe'))}｜"
        f"胜率 {_num(m.get('win_rate_pct'))}%｜交易 {result.get('trades') and len(result.get('trades'))} 笔｜"
        f"平均持有 {_num(m.get('avg_hold_days'), 0)} 天\n"
        f"基准 {result.get('benchmark', {}).get('name', benchmark)} {_pct((result.get('benchmark') or {}).get('return_pct'))}%｜"
        f"超额 {_pct(result.get('excess_pct'))}%"
    )
    if result.get("warnings"):
        text += f"\n注意：{'；'.join(str(w) for w in result['warnings'])}"
    return _ok(text, result, "xueqiu 日线")


# ---------------------------------------------------------------------------
# 监控 / 策略 / 设置（只读部分）
# ---------------------------------------------------------------------------

def tool_monitor(args: Dict[str, Any]) -> Dict[str, Any]:
    rows = stock_service.monitor_favorites(force_refresh=False, respect_trading_hours=True)
    if not rows:
        return _ok(
            f"本次监控无结果（当前状态：{trading_status()}）。非交易时段不会产生监控结果，"
            "这是预期行为，不是故障。", [], "monitor")
    lines = []
    for r in rows[:15]:
        lines.append(
            f"{r.get('secID') or r.get('code')} {r.get('name') or ''}："
            f"{r.get('signal') or r.get('reason') or '-'}（{to_action_text(r.get('action'))}）"
        )
    return _ok(f"监控命中 {len(rows)} 条：\n" + "\n".join(lines), rows, "monitor")


def to_action_text(v: Any) -> str:
    mapping = {
        "watch": "观望", "hold": "持有", "hold_add": "持有/加仓", "hold_watch": "持有/观察",
        "buy": "买入", "buy_dip": "低吸", "probe_buy": "试探买入", "trend_follow": "顺势跟进",
        "accumulate": "逢低布局", "reduce": "减仓", "stop_loss": "止损", "exit": "清仓",
        "focus": "重点关注",
    }
    return mapping.get(str(v or ""), str(v or "-"))


def tool_alert_rules(args: Dict[str, Any]) -> Dict[str, Any]:
    rules = stock_service.get_alert_rules() or {}
    keys = list(rules.keys())[:15]
    return _ok(f"预警规则字段：{'、'.join(keys)}；内容 {str(rules)[:600]}", rules, "config/alert_rules.json")


def tool_strategies(args: Dict[str, Any]) -> Dict[str, Any]:
    rows = stock_service.list_strategies() or []
    if not rows:
        return _ok("策略列表为空（用户还没有保存任何表达式因子）。", [], "config/strategies.json")
    lines = []
    for r in rows:
        seg = f"- {r.get('name')}｜入场 {r.get('entry_expr')}｜出场 {r.get('exit_expr')}"
        if r.get("description"):
            seg += f"｜说明 {r.get('description')}"
        bench = r.get("benchmark") or "hs300"
        seg += f"｜基准 {bench}｜初始资金 {_num(r.get('capital'), 0)} 元"
        m = r.get("metrics")
        if isinstance(m, dict):
            seg += (f"｜最近回测 收益{_pct(m.get('total_return_pct'))}% 回撤{_num(m.get('max_drawdown_pct'))}%"
                     f" 夏普{_num(m.get('sharpe'))} 胜率{_num(m.get('win_rate_pct'))}% 交易{m.get('trade_count')}笔")
        lines.append(seg)
    text = f"表达式因子（回测策略）{len(rows)} 套：\n" + "\n".join(lines)
    return _ok(text, rows, "config/strategies.json")


def tool_run_factor_backtest(args: Dict[str, Any]) -> Dict[str, Any]:
    """用已保存的表达式因子（回测策略）对个股做历史回测。

    让用户说的『用我保存的某某因子回测一下 600519』『拿金叉因子在平安上跑一遍』之类的话能直接落地：
    不再需要用户口述表达式，直接从因子库取入场/出场与回测参数。
    """
    factor_name = str(args.get("factor_name") or "").strip()
    if not factor_name:
        return _err("缺少参数 factor_name（已保存的因子名称），可用 list_strategies 查看")
    factor = stock_service.get_strategy(factor_name)
    if not factor:
        names = "、".join(r.get("name") for r in (stock_service.list_strategies() or []) if r.get("name"))
        return _err(f"找不到因子「{factor_name}」。现有因子：{names or '（无）'}", "factor_backtest")
    code = str(args.get("code") or "").strip()
    if not code:
        return _err("缺少参数 code（如 600519 或 600519.SH）")
    result = backtest_service.run(
        code,
        start_date=args.get("start_date"),
        end_date=args.get("end_date"),
        capital=_f(factor.get("capital")) or 100000.0,
        entry_expr=str(factor.get("entry_expr") or ""),
        exit_expr=str(factor.get("exit_expr") or ""),
        benchmark=str(factor.get("benchmark") or "hs300"),
        fee_rate=_f(factor.get("fee_rate")) or 0.00075,
        stamp_tax_rate=_f(factor.get("stamp_tax_rate")) or 0.0005,
        slippage_pct=_f(factor.get("slippage_pct")) or 0.001,
        position_pct=_f(factor.get("position_pct")) or 1.0,
        strategy_name=factor_name,
    )
    if not result.get("ok"):
        return _err(f"因子「{factor_name}」回测 {code} 失败：{result.get('error') or result.get('warnings')}",
                    "factor_backtest")
    m = result.get("metrics") or {}
    text = (
        f"因子「{factor_name}」回测 {code}（{result.get('start_date')} ~ {result.get('end_date')}，"
        f"{result.get('bars')} 根K线）\n"
        f"总收益 {_pct(result.get('return_pct'))}%｜年化 {_pct(m.get('annual_return_pct'))}%｜"
        f"最大回撤 {_num(m.get('max_drawdown_pct'))}%｜夏普 {_num(m.get('sharpe'))}｜"
        f"胜率 {_num(m.get('win_rate_pct'))}%｜交易 "
        f"{result.get('trades') and len(result.get('trades'))} 笔｜"
        f"平均持有 {_num(m.get('avg_hold_days'), 0)} 天\n"
        f"基准 {result.get('benchmark', {}).get('name', 'hs300')} "
        f"{_pct((result.get('benchmark') or {}).get('return_pct'))}%｜"
        f"超额 {_pct(result.get('excess_pct'))}%"
    )
    if result.get("warnings"):
        text += f"\n注意：{'；'.join(str(w) for w in result['warnings'])}"
    return _ok(text, result, "xueqiu 日线 + 已保存因子")


def tool_evaluate_factor(args: Dict[str, Any]) -> Dict[str, Any]:
    """用已保存因子的入场/出场表达式，判定某只股票最新一根日线是否触发买入/卖出信号。

    让 agent 能『调我们的因子』做实时判定：用户问『我那个金叉因子现在对 600519 发出信号了吗』时直接查，
    不用用户复述表达式。
    """
    factor_name = str(args.get("factor_name") or "").strip()
    if not factor_name:
        return _err("缺少参数 factor_name（已保存的因子名称），可用 list_strategies 查看")
    factor = stock_service.get_strategy(factor_name)
    if not factor:
        names = "、".join(r.get("name") for r in (stock_service.list_strategies() or []) if r.get("name"))
        return _err(f"找不到因子「{factor_name}」。现有因子：{names or '（无）'}", "evaluate_factor")
    code = str(args.get("code") or "").strip()
    if not code:
        return _err("缺少参数 code（如 600519 或 600519.SH）")
    entry = str(factor.get("entry_expr") or "").strip()
    exit_expr = str(factor.get("exit_expr") or "").strip()
    if not entry:
        return _err(f"因子「{factor_name}」没有入场表达式，无法判定", "evaluate_factor")
    try:
        sig = StrategyService(stock_service).eval_signal(code, entry, exit_expr)
    except Exception as exc:  # noqa: BLE001
        return _err(f"因子信号判定失败：{exc}", "evaluate_factor")
    if not sig.get("ok"):
        return _err(f"{code} 判定失败：{sig.get('error')}（K线需≥2根）", "evaluate_factor")
    verdict = []
    verdict.append("当前触发【入场/买入】信号" if sig.get("entry_hit") else "当前未触发入场信号")
    verdict.append("当前触发【出场/卖出】信号" if sig.get("exit_hit") else "当前未触发出场信号")
    text = f"因子「{factor_name}」对 {code} 的当日信号：{'；'.join(verdict)}。"
    if sig.get("entry_hit") and sig.get("exit_hit"):
        text += "（入场与出场同时命中，通常说明表达式逻辑重叠，需要检查）"
    return _ok(text, {"code": code, "factor": factor_name, **sig}, "表达式判定 + 最新日线")


def tool_trend_strategy(args: Dict[str, Any]) -> Dict[str, Any]:
    horizon = str(args.get("horizon") or "long")
    cfg = stock_service.get_trade_strategy_config(horizon) or {}
    entry = cfg.get("entry") or {}
    text = (
        f"{cfg.get('name') or horizon}：K线周期 {cfg.get('kline_frequency')}，"
        f"允许阶段 {entry.get('allowed_stages')}，"
        f"止损 {((cfg.get('stop_loss') or {}).get('pct'))}%，"
        f"止盈 {((cfg.get('take_profit') or {}).get('pct'))}%，"
        f"初始仓位 {((cfg.get('position') or {}).get('initial_pct'))}%"
    )
    return _ok(text, cfg, "config/trade_strategy_{}.json".format(horizon))


def tool_settings(args: Dict[str, Any]) -> Dict[str, Any]:
    st = stock_service.get_user_settings() or {}
    safe = {k: v for k, v in st.items() if k not in ("dingtalk_webhook_url", "dingtalk_secret", "xueqiu_cookie")}
    text = (
        f"总资金 {_num(safe.get('total_capital'), 0)} 元｜可用弹药 {_num(safe.get('available_cash'), 0)} 元"
        f"｜投资风格 {_STYLE_LABEL.get(normalize_style(safe.get('investment_style')), safe.get('investment_style') or '长线')}"
        f"｜监控策略 {safe.get('monitor_strategy') or '-'}"
        f"｜监控开关 {safe.get('monitor_enabled')}｜钉钉开关 {safe.get('dingtalk_enabled')}"
        f"｜主数据源 {safe.get('prefer_source')}｜K线周期 {safe.get('monitor_kline_frequency')}"
    )
    return _ok(text, safe, "config/user_settings.json")


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

def tool_allocation_advice(args: Dict[str, Any]) -> Dict[str, Any]:
    """四池组合体检：目标 vs 实际、偏离带宽、越界、再平衡建议、全局回撤风控。

    只给分析和建议，不产生交易。任何「要不要调仓 / 加仓 / 减仓 / 比例怎么调」
    的结论都必须以本工具为准，禁止模型自己拍比例。
    """
    try:
        from ..services import allocation_service as alloc

        items = stock_service.get_favorites_list(include_quotes=True) or []
        settings = stock_service.get_user_settings() or {}
        total = _f(settings.get("total_capital")) or 0.0
        holdings = [
            {
                "secID": item.get("secID"),
                "name": item.get("name"),
                "shares": item.get("shares"),
                "cost": item.get("cost"),
                "pool": item.get("pool"),
                "price": item.get("price"),
                "industry": item.get("industry") or "",
            }
            for item in items
        ]
        result = alloc.compute_allocation(holdings, total)
        if not result.get("ok"):
            return _err(f"四池体检不可用：{result.get('error')}", "four_pools")

        plan = alloc.rebalance_plan(result)
        drawdown = _f(args.get("drawdown_pct"))
        if drawdown is None:
            try:
                snapshot = stock_service.get_account_snapshot() or {}
                drawdown = _f(snapshot.get("drawdown_pct") or snapshot.get("max_drawdown_pct"))
            except Exception:  # noqa: BLE001
                drawdown = None
        risk = alloc.global_risk_state(drawdown if drawdown is not None else 0.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("allocation_advice_failed error=%r", exc)
        return _err(f"四池体检失败：{exc}", "four_pools")

    status_label = {"under": "低配", "over": "超配", "in_band": "区间内"}
    lines = ["四池配置（目标 / 实际 / 偏离）"]
    for item in result["pools"]:
        lines.append(
            f"- {item['label']}：目标 {item['target_pct']:.0f}%（{item['min_pct']:.0f}~{item['max_pct']:.0f}%），"
            f"实际 {item['actual_pct']:.2f}%，偏离 {item['diff_pp']:+.2f}pp（{status_label.get(item['status'], item['status'])}），"
            f"单票最大 {item['largest_single_pct']:.2f}% / 上限 {item['max_single_pct']:.2f}%"
        )
    cash = result["cash"]
    lines.append(f"- 现金：实际 {cash['actual_pct']:.2f}%（目标 {cash['target_pct']:.0f}%，下限 {cash['min_pct']:.0f}%）")
    if result["unclassified"]["count"]:
        lines.append(f"- 未分类：{result['unclassified']['count']} 只（{result['unclassified']['pct']:.2f}%），请先分类再调仓")

    if result["issues"]:
        lines.append("越界 / 风险：")
        lines.extend(f"- {item['message']}" for item in result["issues"])
    else:
        lines.append("越界 / 风险：无")

    if plan:
        lines.append("再平衡建议（只在 ±带宽外提示，不自动执行）：")
        for item in plan:
            verb = "减" if item["action"] == "trim" else "加"
            lines.append(
                f"- {item['label']}：{verb} {abs(item['diff_pp']):.2f}pp（约 {item['amount']:,.0f} 元）回到目标；"
                f"资金只在本池内调拨"
            )
    else:
        lines.append("再平衡建议：四池均在带宽内，无需调整")

    lines.append(f"全局风控：{risk['level']}（账户自新高回撤 {risk['drawdown_pct']:+.2f}%）")
    lines.extend(f"- {action}" for action in risk["actions"])

    return _ok("\n".join(lines), {"allocation": result, "rebalance": plan,
                                  "global_risk": risk}, "四池规则 + 账户快照")


def tool_suggest_pool_classification(args: Dict[str, Any]) -> Dict[str, Any]:
    """四池分类建议：给未分类持仓推荐属于哪个池，带依据与置信度。

    只给建议，不落库。用户确认后才允许在前端调 /stock/allocation/classify。
    """
    try:
        from ..services import allocation_service as alloc
        from ..services import pool_classifier
    except Exception as exc:  # noqa: BLE001
        return _err(f"分类器不可用：{exc}", "four_pools")

    only_unclassified = args.get("only_unclassified", True)
    only_unclassified = only_unclassified if isinstance(only_unclassified, bool) else True
    try:
        items = stock_service.get_favorites_list(include_quotes=True) or []
    except Exception as exc:  # noqa: BLE001
        return _err(f"取持仓失败：{exc}", "four_pools")

    results: List[Dict[str, Any]] = []
    for item in items:
        secid = str(item.get("secID") or "").strip()
        if not secid:
            continue
        current = alloc.normalize_pool(item.get("pool"))
        if only_unclassified and current:
            continue
        industry = str(item.get("industry") or "").strip()
        valuation: Dict[str, Any] = {}
        fundamentals: Dict[str, Any] = {}
        try:
            valuation = stock_service._evaluate_valuation(secid) or {}
        except Exception:  # noqa: BLE001
            valuation = {}
        if not industry:
            try:
                profile = stock_service.get_company_info(secid) or {}
                industry = str(profile.get("industry") or profile.get("行业") or "").strip()
            except Exception:  # noqa: BLE001
                industry = ""
        try:
            fundamentals = stock_service.get_fundamental_snapshot(secid) or {}
        except Exception:  # noqa: BLE001
            fundamentals = {}
        suggestion = pool_classifier.classify_one(
            code=secid, name=str(item.get("name") or ""), industry=industry,
            valuation=valuation, fundamentals=fundamentals,
        )
        suggestion["current_pool"] = current
        results.append(suggestion)

    if not results:
        return _ok("没有需要分类的持仓：要么都归池了，要么自选列表为空。", {"items": []}, "持仓列表")

    lines = ["四池分类建议（只给建议，采纳需人工确认）"]
    for s in results:
        name = s.get("name") or s.get("code")
        if s.get("suggested_pool"):
            lines.append(
                f"- {name}（{s['code']}）：建议 {s['suggested_label']}，"
                f"置信度 {s['confidence']}；依据：{'；'.join(s['reasons'][:3]) or '—'}"
            )
        else:
            lines.append(f"- {name}（{s['code']}）：信号不足，请手动选择池")
        if s.get("note"):
            lines.append(f"  提示：{s['note']}")
    lines.append("提醒：催化波段与长赛道的差别是持有意图，不能只靠财报判断，需用户确认。")
    return _ok("\n".join(lines), {"items": results}, "估值 + 财务快照")


TOOL_SPECS: List[ToolSpec] = [
    # 行情
    ToolSpec("get_system_clock", "行情", "系统时钟 + A股交易日历：当前时间、星期、是否交易日、休市状态（周末/法定节假日及名称）、下一交易日、交易时段。回答任何『今天/现在/明天能不能交易』或涉及时间的问题前必调", {}, "read", tool_system_clock),
    ToolSpec("get_market_status", "行情", "判断当前是否交易时段（周末/节假日/盘前/盘中/午休/盘后），内部同 get_system_clock", {}, "read", tool_market_status),
    ToolSpec("get_market_sentiment", "情绪", "全市场情绪仪表盘：涨停/炸板/跌停家数、封板率、最高连板、二板以上家数、涨停最集中的行业，并给出阶段判定（冰点/修复/主升/亢奋/退潮/崩塌）。判断『现在能不能追、要不要撤』以及任何涉及题材接力、市场热度的问题前必调", {"date": "可选，YYYY-MM-DD；不传取最新交易日", "force": "可选，true 强制刷新缓存"}, "read", tool_market_sentiment),
    ToolSpec("get_spot", "行情", "取单只股票的实时/最新报价：价格、涨跌幅、开高低、成交量额", {"code": "股票代码，如 600519 或 002230.SZ"}, "read", tool_spot),
    ToolSpec("get_kline", "行情", "取日 K 线并计算均线、MACD、RSI、量能倍数、区间位置。判断趋势与形态必用", {"code": "股票代码", "days": "取最近多少个交易日，默认 120，范围 20-280"}, "read", tool_kline),
    ToolSpec("get_minute", "行情", "取当日分时，用于判断量能与价格是否集中在尾盘（识别集合竞价造成的假放量）", {"code": "股票代码"}, "read", tool_minute),
    ToolSpec("get_indices", "行情", "取上证/深证/创业板等大盘指数涨跌", {}, "read", tool_indices),
    # 持仓
    ToolSpec("list_favorites", "持仓", "取自选股与持仓：代码、名称、持股数、成本、现价、浮盈亏、所属池", {"with_quotes": "是否附实时行情，默认 true；只想看列表时传 false 更快"}, "read", tool_favorites),
    ToolSpec("get_position_summary", "持仓", "组合视角汇总：总市值、浮动盈亏、个股权重、行业暴露、最大单一标的、接近止损的标的。做风险判断必用", {}, "read", tool_position_summary),
    ToolSpec("get_sell_target", "持仓", "多因子卖出方案：① 三套情景目标价（保守/基线/激进，各自带目标阶梯、减仓比例、触发条件）② 当前该用的退出策略（接力跟随/退潮防御/逻辑止损/低位均值回归/逢高减/按阶梯，含硬止损、结构止损、移动止盈三条线）③ 因子明细（估值 PE/PB 分位+申万行业比较、财报 ROE/增速、行业与个股周期、题材情绪）④ 市场情绪与个股情绪定位（连板/炸板/板块联动）⑤ **低位价值视图（近一年有没有炒作过、现在是不是低位、深跌是否构成均值回归机会、安全边际）**。**给任何卖出价位或止盈止损建议前必调本工具**，禁止自己拍价位", {"code": "股票代码；不传则对全部持仓批量计算", "style": "仅供假设推演的内部风格覆盖：long | mid_long | swing；默认按持仓所属池自动映射，不要向用户提这些词"}, "read", tool_sell_target),
    ToolSpec("get_value_position", "分析", "低位价值视图：判断这只票/这个板块**近一年有没有炒作过、有没有大涨过**、现在处在什么位置、是不是「深跌必有反弹」的均值回归机会。给出：炒作史（单日最大涨幅/大涨次数/距最后一次大涨多少天/炒作程度）、相对位置（250日分位/距高点回撤）、深跌与基本面是否被证伪、戴维斯双击条件是否具备、所属板块是否被炒过、大盘位置、环境是否配合、安全边际评估（好股太贵不能碰）。**分析卖出该不该卖、该不该在低位割肉、或判断一只票是不是「久未大涨迟早有一波」时必调**", {"code": "股票代码；不传则对全部持仓批量计算", "window": "可选，观察窗口交易日数，默认 250（约一年），范围 120-250"}, "read", tool_value_position),
    ToolSpec("get_account_status", "持仓", "账户资金全景：持仓市值、可用现金、账户总资产、真实仓位（市值÷总资产）、弹药状态（充足/偏紧/满仓）以及当前是否允许推买入提醒。用户问『我还有多少钱/仓位多少/还有没有弹药/满仓了吗/能不能加仓』时必调", {}, "read", tool_account_status),
    ToolSpec("get_sentiment_policy", "市场", "市场情绪→仓位与现金纪律：把情绪阶段（崩塌/退潮/冰点/修复/主升/亢奋）+情绪温度+大盘250日位置翻译成①目标总仓位区间与目标现金比例②当前该做什么（留现金攒子弹/分批建仓/持有不加/减仓兑现/保命撤退）③建仓节奏与分批笔数④是否允许开仓、是否允许追高⑤不博傻清单（不接最后一棒、不买无基本面支撑的纯题材、不追高、不因感觉下单）⑥实际仓位与目标仓位的差距。**给任何买入或加仓建议前必调本工具**；情绪低迷时不要只喊跌，要讲清楚该留多少现金、什么时候分批建", {}, "read", tool_sentiment_policy),
    ToolSpec("get_sentiment_history", "市场", "市场情绪历史曲线与周期位置：把每日情绪快照加工成①当前温度在历史样本中的分位（今天49%是从冰点起来还是从亢奋掉下来）②当前阶段已持续几个交易日（持续3天的亢奋≠持续3周的退潮）③涨停家数是否较峰值腰斩（退潮最硬的证据）④近期阶段转折点（判断退潮中的反弹还是真反转）⑤周期位置结论（该建仓/该持有/该兑现）。**判断『现在情绪处在什么位置、该不该动手、这个退潮持续多久了』时必调**", {}, "read", tool_sentiment_history),
    ToolSpec("get_industry_valuation", "分析", "行业估值中枢：把这只票的 PE/PB 放回**它自己行业**里比较——①本行业成分股 PE/PB 中位数与截尾均值②本股相对行业中位的溢价/折价③PE/PB 在本行业成分股里的分位（0%=本行业最便宜）④申万官方行业指数口径交叉校验。**用户明确要求：算 PE/PB 必须看行业均位，不能跨行业比较（每个行业商业模式都不一样）**。判断『这只票贵不贵、是低估还是高估』时必调，不要自己拿固定 PE/PB 阈值横跨所有行业", {"code": "股票代码"}, "read", tool_industry_valuation),
    ToolSpec("update_account_cash", "持仓", "把用户在本轮对话中明确说出的账户可用资金/本金写入设置（如『我现金还剩 3 万』『现在满仓了没现金』）。**只有用户本轮明确给出金额时才能调用**，禁止自己推断或补零；available_cash=0 表示真的没现金（满仓）", {"available_cash": "账户可用资金（元）；0 = 已满仓无现金", "total_capital": "可选，投入本金（元）"}, "write", tool_update_account_cash),
    ToolSpec("get_trade_history", "持仓", "取个股最近成交明细", {"code": "股票代码", "count": "条数，默认 10"}, "read", tool_trade_history),
    ToolSpec("record_trade", "记账",
             "用户口述成交时记一笔并同步持仓与现金（『今天卖了巨人网络 500 股，27.35』『加仓平安 300 股』『清掉华域』）。"
             "**只记已经成交的事实，不记计划与意愿**。买入必须 100 股整数倍；卖出超过记录持股会被拒绝；"
             "不给成交价会按现价记并明确标注。手续费可传实际值，不传按佣金万2.5（最低5元）+印花税0.05%(卖出)+过户费0.001%估",
             {"code": "股票代码或名称，如 002558 / 巨人网络",
              "side": "buy（买入/加仓/补仓）或 sell（卖出/减仓/清仓/止盈/止损）",
              "shares": "成交股数（正整数）；清仓填当前持股数",
              "price": "成交均价；没有就留空，按现价记",
              "fee": "可选，实际手续费合计（元）；不传则估算",
              "date": "可选，成交日期 YYYY-MM-DD，默认今天",
              "note": "可选，备注，如『做T卖出』『止盈T1』"},
             "write", tool_record_trade),
    ToolSpec("list_trades", "记账", "查交易流水与已实现盈亏：回答『我最近做了哪些操作』『这波赚了多少已经落袋的』『手续费花了多少』时必调",
             {"limit": "条数，默认 20", "code": "可选，只看某只票", "since": "可选，起始日期 YYYY-MM-DD", "side": "可选，buy/sell"},
             "read", tool_list_trades),
    ToolSpec("sync_position", "记账",
             "直接纠正某只票的持股数与成本（送转股、分红再投、或以前记错了）。**只在用户明确给出『实际持有 X 股、成本 Y』时用**，"
             "真实买卖一律走 record_trade。本工具不改现金",
             {"code": "股票代码或名称", "shares": "纠正后的实际持股数，可为 0", "cost": "可选，纠正后的每股成本",
              "note": "可选，纠正原因，如『10转5送股』"},
             "write", tool_sync_position),
    # 分析
    ToolSpec("get_chip_distribution", "分析", "取筹码分布：平均成本、获利盘/套牢盘比例、集中度、90% 成本区间。判断上方抛压用", {"code": "股票代码"}, "read", tool_chip),
    ToolSpec("get_money_flow", "分析", "取主力资金流向，含连续净流出天数", {"code": "股票代码"}, "read", tool_money_flow),
    ToolSpec("get_company_info", "分析", "取公司概况与所属行业（用于判断板块联动与行业暴露）", {"code": "股票代码"}, "read", tool_company),
    ToolSpec("run_backtest", "分析", "用表达式策略对个股做历史回测，返回收益/年化/回撤/夏普/胜率/基准对比。评估策略有效性必用", {
        "code": "股票代码",
        "entry_expr": "入场表达式，如 CROSS(DIF, DEA)",
        "exit_expr": "出场表达式，如 CROSS(DEA, DIF)",
        "capital": "初始资金，默认 100000",
        "benchmark": "基准：hs300/zz500/sh/sz",
    }, "read", tool_backtest),
    # 监控
    ToolSpec("run_monitor", "监控", "执行一次监控扫描，返回命中的信号。非交易时段会返回空，这是预期行为", {}, "read", tool_monitor),
    ToolSpec("get_alert_rules", "监控", "读取预警规则配置", {}, "read", tool_alert_rules),
    # 策略
    ToolSpec("list_strategies", "策略", "列出所有已保存的表达式因子（回测策略）：名称、说明、入场/出场表达式、基准、回测参数与最近一次回测指标。agent 想『用某个因子』前先调它取名称", {}, "read", tool_strategies),
    ToolSpec("run_factor_backtest", "策略", "用已保存的表达式因子（回测策略）对个股做历史回测，返回收益/年化/回撤/夏普/胜率/基准对比。用户说『用我保存的某某因子回测一下 600519』『拿金叉因子在平安上跑一遍』时必调，不要再让用户口头复述表达式", {"factor_name": "已保存因子名称（先 list_strategies 取名称）", "code": "股票代码", "start_date": "可选，开始日期 YYYY-MM-DD", "end_date": "可选，结束日期 YYYY-MM-DD"}, "read", tool_run_factor_backtest),
    ToolSpec("evaluate_factor", "策略", "用已保存因子的入场/出场表达式，判定某只股票最新一根日线是否触发买入/卖出信号。用户问『我那个某某因子现在对 600519 发出信号了吗』时必调", {"factor_name": "已保存因子名称", "code": "股票代码"}, "read", tool_evaluate_factor),
    ToolSpec("get_trend_strategy", "策略", "读取内部监控/卖出参数档（short=波段 / mid_long / long）：允许阶段、止损止盈、仓位。这是四池映射后的底层参数，回答用户时请换算成池口径，不要直接抛 short/long 这些词", {"horizon": "short | mid_long | long，默认 long"}, "read", tool_trend_strategy),
    # 系统
    ToolSpec("get_settings", "系统", "读取用户设置：总资金、监控策略、开关、主数据源（不含密钥）", {}, "read", tool_settings),
    # 四池
    ToolSpec("get_allocation_advice", "仓位", "四池组合体检：每池目标/实际占比、偏离pp、带宽、单票与只数越界、行业集中度（≤30%）、现金下限（≥10%）、再平衡建议、全局回撤风控级别（-15 停新开/-20 各减半/-25 只留股息+现金）。给任何『要不要调仓/加仓/减仓/比例怎么调』建议前必调本工具，禁止自己拍比例", {"drawdown_pct": "可选，账户自新高回撤百分比（负值）；不传则从账户快照取"}, "read", tool_allocation_advice),
    ToolSpec("suggest_pool_classification", "仓位", "四池分类建议：对未分类持仓推荐股息/催化波段/长赛道/低估中的一个，附打分依据与置信度。**只给建议、不落库**，用户明确同意后由用户在「四池配置」页采纳。用户问『这只票该放哪个池』或检出不分类持仓时使用", {"only_unclassified": "是否只处理未分类持仓，默认 true"}, "read", tool_suggest_pool_classification),
]

TOOL_MAP: Dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

# 允许 Agent 直接执行的写工具。
# 只放"用户在对话里报一个数、写错也能一句话改回来"的账户类字段，
# 其余写工具（改策略、改提醒规则等）仍然要求用户显式确认。
# record_trade / sync_position 进白名单的理由：用户口述成交就是要系统自己落账，
# 每笔都要二次确认会让"对话即记账"变成"对话即填表"。它们只动持仓/现金，
# 且流水留痕（config/trade_log.json）可追溯、可反向修正。
_AUTO_WRITE_TOOLS = {"update_account_cash", "record_trade", "sync_position"}


def get_tool(name: str) -> Optional[ToolSpec]:
    return TOOL_MAP.get(name)


def execute_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """执行工具，异常一律转成可读错误（让模型自己决定怎么处理）。"""
    spec = TOOL_MAP.get(name)
    if not spec:
        return _err(f"未知工具 {name}。可用工具：{', '.join(TOOL_MAP.keys())}")
    if spec.risk == "write" and name not in _AUTO_WRITE_TOOLS:
        return _err(f"工具 {name} 会修改配置，需要用户明确确认后才能执行。请先向用户说明改动内容。")
    if spec.risk == "external":
        return _err(f"工具 {name} 会触发外部通知，不允许自行调用。")
    try:
        return spec.handler(args or {})
    except Exception as exc:  # noqa: BLE001
        logger.exception("工具 %s 执行失败", name)
        detail = str(exc)
        if len(detail) > 160:
            detail = detail[:160] + "…"
        return _err(f"工具 {name} 执行异常：{type(exc).__name__}: {detail}", "error")


def describe_tools(names: Optional[List[str]] = None) -> str:
    """生成给 LLM 的工具说明（只列出指定工具，控制上下文长度）。"""
    specs = [TOOL_MAP[n] for n in names if n in TOOL_MAP] if names else TOOL_SPECS
    lines = []
    for spec in specs:
        params = "、".join(f"{k}（{v}）" for k, v in spec.params.items()) if spec.params else "无参数"
        lines.append(f"- {spec.name}：{spec.desc}\n  参数：{params}")
    return "\n".join(lines)
