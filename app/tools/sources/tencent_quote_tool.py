"""腾讯行情（qt.gtimg.cn）轻量实时报价工具。

为什么需要它
------------
服务器（云 IP）上三个原有现价来源会同时失效，而它们的失效原因互不相同：
- 星耀数智：SDK 需要登录+常驻子进程，容器 1GB 内存下 OOM（见 README 数据源章节）
- 雪球：cookie 过期后 302 跳登录，返回 HTML 解析失败
- 东财 push2：云 IP 被限流，直接断连（还会因为重试而延长封禁）

腾讯这个接口没有鉴权、单次请求可以批量拉几十只票、返回体只有几 KB（GBK 文本），
不占内存也不占带宽，是云上最稳的兜底。字段按 `~` 分割，位置固定：

    1 名称 / 2 代码 / 3 现价 / 4 昨收 / 5 今开 / 6 成交量(手)
    30 时间 / 31 涨跌 / 32 涨跌% / 33 最高 / 34 最低
    37 成交额(万元) / 38 换手率

只取前 40 个字段里语义明确的，其余不猜。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

_QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
# 当日分时（1 分钟一个点）。返回 JSON，取 data.<symbol>.data.data 里的
# "HHMM 价格 成交量(手) 成交额" 文本行；比分钟 K 线接口（mkline）稳，
# 后者在云服务器上会被 SSL 重置。
_MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
# 多日分时（默认 5 日），结构是 {"data": {code: {"data": [{"date": "20261102", "data": [...]}, ...]}}}
_MINUTE_DAYS_URL = "https://web.ifzq.gtimg.cn/appstock/app/day/query"


def _parse_minute_lines(lines: Any, day: str) -> List[Dict[str, Any]]:
    """把 "HHMM 价格 成交量 成交额" 文本行解析成统一的分钟点。"""
    if not isinstance(lines, list):
        return []
    if len(day) == 8:
        day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    out: List[Dict[str, Any]] = []
    for line in lines:
        parts = str(line or "").split()
        if len(parts) < 2:
            continue
        hhmm = parts[0].strip()
        price = _to_float(parts[1])
        if price is None or len(hhmm) != 4:
            continue
        out.append({
            "time": f"{day} {hhmm[:2]}:{hhmm[2:]}" if day else hhmm,
            "price": price,
            "volume": _to_float(parts[2]) if len(parts) > 2 else None,
            "amount": _to_float(parts[3]) if len(parts) > 3 else None,
        })
    return out


def _to_float(value: Any) -> Optional[float]:
    try:
        text = str(value).strip()
        if not text or text in ("-", "--"):
            return None
        return float(text)
    except Exception:
        return None


class TencentQuoteTool:
    """批量实时快照。一次请求多只票，返回 {6位代码: 报价}。"""

    def __init__(self, timeout_sec: float = 3.0) -> None:
        self._timeout = timeout_sec

    @staticmethod
    def to_symbol(code: str) -> str:
        """600519.SH / sh600519 / 600519 → sh600519（腾讯用小写市场前缀）。"""
        raw = str(code or "").strip().upper()
        if raw.startswith(("SH", "SZ", "BJ")) and len(raw) >= 8:
            return raw[:2].lower() + raw[-6:]
        digits = "".join(ch for ch in raw if ch.isdigit())[-6:]
        if len(digits) != 6:
            return ""
        market = "sh" if digits.startswith(("6", "9")) else ("bj" if digits.startswith(("4", "8")) else "sz")
        return f"{market}{digits}"

    def fetch_quotes(self, codes: List[str], batch_size: int = 60) -> Dict[str, Dict[str, Any]]:
        symbols = [s for s in (self.to_symbol(c) for c in codes) if s]
        if not symbols:
            return {}
        # 腾讯单次支持批量，但太长会被截断，按批分组
        # batch_size 可调：算「行业中位」时要拉全市场 ~4700 只，
        # 用 300 只一批能把请求数从 79 次降到 16 次。
        size = max(1, int(batch_size or 60))
        out: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(symbols), size):
            chunk = symbols[i:i + size]
            try:
                with httpx.Client(timeout=self._timeout, follow_redirects=False) as client:
                    resp = client.get(
                        _QUOTE_URL.format(codes=",".join(chunk)),
                        headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"},
                    )
                resp.raise_for_status()
                text = resp.content.decode("gbk", errors="ignore")
            except Exception:
                continue
            for line in text.split(";"):
                line = line.strip()
                if not line.startswith("v_") or "=" not in line:
                    continue
                left, _, right = line.partition("=")
                payload = right.strip().strip('"')
                if not payload:
                    continue
                parts = payload.split("~")
                if len(parts) < 35:
                    continue
                digits = parts[2].strip() or left[2:][-6:]
                price = _to_float(parts[3])
                if not digits or price is None:
                    continue
                prev = _to_float(parts[4])
                out[digits] = {
                    "code": digits,
                    "name": parts[1].strip() or None,
                    "price": price,
                    "prev_close": prev,
                    "open": _to_float(parts[5]),
                    "volume": _to_float(parts[6]),          # 手
                    "high": _to_float(parts[33]),
                    "low": _to_float(parts[34]),
                    "change": _to_float(parts[31]),
                    "pct_chg": _to_float(parts[32]),
                    "amount": (_to_float(parts[37]) or 0.0) * 10000.0 if _to_float(parts[37]) else None,
                    "turnover": _to_float(parts[38]),
                    # 估值字段：腾讯的 PE(idx39) 是动态市盈率、PB(idx46) 是市净率。
                    # 这是东财 push2 被限流时算「行业中位」的兜底数据源。
                    "pe": _to_float(parts[39]) if len(parts) > 39 else None,
                    "pb": _to_float(parts[46]) if len(parts) > 46 else None,
                    "float_mv": _to_float(parts[44]) if len(parts) > 44 else None,
                    "trade_time": parts[30].strip() or None,  # 20261102150330
                    "source": "tencent",
                }
        return out

    def fetch_minute(self, code: str, days: int = 1) -> List[Dict[str, Any]]:
        """分时曲线（1 分钟一个点），返回 [{time, price, volume, amount}]。

        为什么要单独一个方法：分时图只需要「每分钟收盘价」这一列，不需要完整
        OHLC。云服务器上雪球 cookie 会过期（返回登录页）且东财 push2 被限流，
        而腾讯这个接口实测 ~0.15 秒就能给全 240+ 个点。

        days=1 取当日；days=5 取最近 5 个交易日（两者是不同接口、不同结构）。
        """
        symbol = self.to_symbol(code)
        if not symbol:
            return []
        if days and int(days) > 1:
            return self._fetch_minute_multi(symbol, int(days))

        try:
            with httpx.Client(timeout=self._timeout, follow_redirects=False) as client:
                resp = client.get(
                    _MINUTE_URL,
                    params={"code": symbol},
                    headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"},
                )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            return []

        block = ((payload.get("data") or {}).get(symbol) or {})
        inner = block.get("data") or {}
        if not isinstance(inner, dict):
            return []
        return _parse_minute_lines(inner.get("data"), str(inner.get("date") or "").strip())

    def _fetch_minute_multi(self, symbol: str, days: int) -> List[Dict[str, Any]]:
        try:
            with httpx.Client(timeout=self._timeout, follow_redirects=False) as client:
                resp = client.get(
                    _MINUTE_DAYS_URL,
                    params={"code": symbol},
                    headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"},
                )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            return []

        block = ((payload.get("data") or {}).get(symbol) or {})
        day_list = block.get("data") or []
        if not isinstance(day_list, list):
            return []

        out: List[Dict[str, Any]] = []
        # 接口返回顺序是最新在前，按日期升序拼回去，否则曲线会倒着走
        for item in sorted(day_list, key=lambda x: str((x or {}).get("date") or "")):
            day = str((item or {}).get("date") or "").strip()
            out.extend(_parse_minute_lines((item or {}).get("data"), day))
        return out[-days * 300:] if days else out
