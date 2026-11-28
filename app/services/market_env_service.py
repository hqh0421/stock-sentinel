"""市场环境快照：大盘周期 + 市场情绪 + 隔夜外盘。

为什么独立成模块（而不是塞进 monitor_service 或让 agent 自己拼）：
- 时间止损的阈值要随环境缩放，需要一个统一、可缓存的入口；
- agent 做「豁免判断」时必须有可核对的依据，不能凭感觉；
- 逐票评估时若每只都去拉外盘，几分钟内就是几十次冗余请求 → 必须缓存。

⚠️ 本模块的职责边界：**只描述「环境是什么」，不回答「该不该买卖」**。
判断一旦写进代码就会变成死规则（这正是用户反对的）；但事实必须由代码算，
否则 agent 会自己编。分工是：代码给事实，agent 给解释。
"""

from __future__ import annotations

import logging
import time
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 新浪外盘：返回 GBK，格式随品种不同，见 _parse_line
_OVERSEAS_CODES = [
    ("int_dji", "道琼斯", "us"),
    ("int_nasdaq", "纳斯达克", "us"),
    ("int_sp500", "标普500", "us"),
    ("int_hangseng", "恒生指数", "asia"),
    ("int_nikkei", "日经225", "asia"),
    ("hf_CL", "纽约原油", "commodity"),
    ("hf_GC", "纽约黄金", "commodity"),
]

_OVERSEAS_TTL = 600.0     # 外盘快照缓存 10 分钟（盘中变动慢，够用）
_ENV_TTL = 180.0          # 完整环境快照缓存 3 分钟（逐票调用时命中）

_overseas_cache: Dict[str, Any] = {"at": 0.0, "data": None}
_env_cache: Dict[str, Any] = {"at": 0.0, "data": None}


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _parse_line(code: str, payload: str) -> Optional[Dict[str, Any]]:
    """解析新浪单行返回。

    指数形如 ``名称,点位,涨跌点,涨跌幅``；
    商品形如 ``当前价,,买,卖,高,低,时间,昨结,今开,...,日期,名称,0``（字段多且位置固定）。
    两者格式不同，不能套同一套下标——这是实测踩过的。
    """
    if not payload:
        return None
    parts = payload.split(",")
    if code.startswith("int_"):
        if len(parts) < 4:
            return None
        name, price, change, pct = parts[0], _f(parts[1]), _f(parts[2]), _f(parts[3])
        if price is None:
            return None
        return {"name": name, "price": price, "change": change, "pct": pct}
    if code.startswith("hf_"):
        if len(parts) < 8:
            return None
        price = _f(parts[0])
        prev_settle = _f(parts[7])
        name = parts[13] if len(parts) > 13 else code
        pct = None
        if price is not None and prev_settle:
            pct = round((price - prev_settle) / prev_settle * 100.0, 2)
        return {"name": name, "price": price, "change": None, "pct": pct}
    return None


def fetch_overseas(force: bool = False) -> Dict[str, Any]:
    """拉隔夜外盘。失败返回空结构——外盘是加分项，不能因为拿不到就阻断主流程。"""
    now = time.time()
    if not force and _overseas_cache["data"] is not None and now - _overseas_cache["at"] < _OVERSEAS_TTL:
        return _overseas_cache["data"]

    codes = ",".join(code for code, _, _ in _OVERSEAS_CODES)
    url = "https://hq.sinajs.cn/list=" + codes
    try:
        # 必须绕开容器代理：项目内所有行情源都是直连（httpx trust_env=False）。
        # 走代理会拿到 501 / 连接重置，看起来像「接口挂了」，实则是代理拒绝。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        )
        raw = opener.open(req, timeout=8).read().decode("gbk", "ignore")
    except Exception as exc:  # noqa: BLE001
        logger.warning("overseas_fetch_failed error=%r", exc)
        result = {"ok": False, "reason": "外盘接口不可用", "items": []}
        # 失败也写缓存，避免每只票都重试一次、把监控拖慢
        _overseas_cache.update({"at": now, "data": result})
        return result

    label_by_code = {code: (name, group) for code, name, group in _OVERSEAS_CODES}
    items: List[Dict[str, Any]] = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line.startswith("var hq_str_"):
            continue
        try:
            head, body = line.split("=", 1)
            code = head.replace("var hq_str_", "").strip()
            payload = body.strip().rstrip(";").strip('"')
        except ValueError:
            continue
        parsed = _parse_line(code, payload)
        if not parsed:
            continue
        fallback_name, group = label_by_code.get(code, (code, "other"))
        # 商品行的名称字段可能是空，回落到配置名称
        if not parsed.get("name"):
            parsed["name"] = fallback_name
        parsed["code"] = code
        parsed["group"] = group
        items.append(parsed)

    result = {"ok": True, "as_of": time.strftime("%Y-%m-%d %H:%M"), "items": items}
    _overseas_cache.update({"at": now, "data": result})
    return result


def _overseas_pressure(overseas: Dict[str, Any]) -> Dict[str, Any]:
    """把外盘涨跌折算成一个「外部压力」读数。

    只做算术，不下结论：跌 1% 记 1 分、跌 2% 记 2 分，取美股与亚太的均值。
    分档只是给 agent 一个起点，最终解释权在 agent。
    """
    items = overseas.get("items") or []
    us = [i for i in items if i.get("group") == "us" and i.get("pct") is not None]
    asia = [i for i in items if i.get("group") == "asia" and i.get("pct") is not None]
    if not us and not asia:
        return {"available": False}

    def _score(bucket: List[Dict[str, Any]]) -> Optional[float]:
        if not bucket:
            return None
        pcts = [float(i["pct"]) for i in bucket]
        avg = sum(pcts) / len(pcts)
        return round(-avg, 2)  # 跌为负 → 取反后正值代表压力

    us_score = _score(us)
    asia_score = _score(asia)
    pool = [s for s in (us_score, asia_score) if s is not None]
    score = round(sum(pool) / len(pool), 2) if pool else None
    level = "unknown"
    if score is not None:
        if score >= 1.5:
            level = "high"
        elif score >= 0.5:
            level = "medium"
        else:
            level = "low"
    return {
        "available": True,
        "us_score": us_score,
        "asia_score": asia_score,
        "score": score,
        "level": level,
    }


def build_env(force: bool = False) -> Dict[str, Any]:
    """聚合一份环境快照。任何一个子项失败都不阻断，缺失的部分显式标 available=False。"""
    now = time.time()
    if not force and _env_cache["data"] is not None and now - _env_cache["at"] < _ENV_TTL:
        return _env_cache["data"]

    from .stock_service import stock_service

    market: Dict[str, Any] = {}
    try:
        market = stock_service.get_market_regime() or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("env_market_regime_failed error=%r", exc)

    sentiment: Dict[str, Any] = {}
    try:
        raw = stock_service.get_market_sentiment() or {}
        if raw.get("ok"):
            sentiment = {
                "ok": True,
                "phase": raw.get("phase"),
                "phase_label": raw.get("phase_label"),
                "temperature": raw.get("temperature"),
                "detail": raw.get("detail"),
                "hot_industries": raw.get("hot_industries") or [],
            }
        else:
            sentiment = {"ok": False, "reason": raw.get("reason") or "情绪数据不可用"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("env_sentiment_failed error=%r", exc)
        sentiment = {"ok": False, "reason": "情绪计算异常"}

    overseas = fetch_overseas(force=force)
    pressure = _overseas_pressure(overseas)

    env = {
        "ok": bool(market.get("ok")) or bool(sentiment.get("ok")),
        "as_of": time.strftime("%Y-%m-%d %H:%M"),
        "market": {
            "available": bool(market.get("ok")),
            "pos250": market.get("pos250"),
            "close": market.get("close"),
            "ma60": market.get("ma60"),
            "ma250": market.get("ma250"),
            "ret20": market.get("ret20"),
            "ret60": market.get("ret60"),
        },
        "sentiment": sentiment,
        "overseas": overseas,
        "overseas_pressure": pressure,
    }
    _env_cache.update({"at": now, "data": env})
    return env


def env_phase(env: Dict[str, Any]) -> str:
    """取用于阈值缩放的情绪阶段。拿不到就返回 unknown，走中性阈值。"""
    sentiment = env.get("sentiment") or {}
    return str(sentiment.get("phase") or "unknown")


def format_env(env: Dict[str, Any]) -> str:
    """把环境渲染成 agent 可读的多行文本。"""
    if not env:
        return "环境快照不可用"
    lines: List[str] = []

    market = env.get("market") or {}
    if market.get("available"):
        bits = [f"上证 {market.get('close')}"]
        if market.get("pos250") is not None:
            bits.append(f"250日区间 {float(market['pos250']):.0%} 位")
        if market.get("ret20") is not None:
            bits.append(f"20日 {float(market['ret20']):+.1f}%")
        if market.get("ret60") is not None:
            bits.append(f"60日 {float(market['ret60']):+.1f}%")
        lines.append("【大盘】" + "，".join(bits))
    else:
        lines.append("【大盘】数据不可用")

    sentiment = env.get("sentiment") or {}
    if sentiment.get("ok"):
        lines.append(f"【题材活跃度】{sentiment.get('phase_label')}（温度 {sentiment.get('temperature')}）"
                     f" — {sentiment.get('detail') or ''}")
        hot = sentiment.get("hot_industries") or []
        if hot:
            names = "、".join(f"{h.get('name')}({h.get('zt_count')}家)" for h in hot[:3])
            lines.append(f"【涨停集中】{names}")
        lines.append("  ⚠️ 口径：只统计涨停/连板/封板率，**不含指数涨跌与成交量**——"
                     "这是「题材投机温度」，不是「市场好坏」，可能与指数方向背离")
    else:
        lines.append(f"【题材活跃度】不可用（{sentiment.get('reason')}）")

    overseas = env.get("overseas") or {}
    items = overseas.get("items") or []
    if items:
        segs = []
        for item in items:
            pct = item.get("pct")
            seg = f"{item.get('name')} {pct:+.2f}%" if pct is not None else str(item.get("name"))
            segs.append(seg)
        lines.append("【隔夜外盘】" + "，".join(segs))
        pressure = env.get("overseas_pressure") or {}
        if pressure.get("available"):
            lines.append(f"【外部压力】{pressure.get('level')}（读数 {pressure.get('score')}，"
                         f"正值代表普跌）")
    else:
        lines.append("【隔夜外盘】数据不可用")

    return "\n".join(lines)
