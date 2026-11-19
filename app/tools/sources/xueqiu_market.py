import datetime
from typing import Any, Dict, List, Optional

import httpx


class XueqiuMarketDataTool:
    def __init__(self, timeout_sec: float = 2.0) -> None:
        self._timeout_sec = timeout_sec

    def to_symbol(self, code: str) -> str:
        raw = str(code or "").strip().upper()
        if raw.startswith(("SH", "SZ", "BJ")) and len(raw) >= 8:
            return raw[:2] + raw[-6:]
        digits = "".join(ch for ch in raw if ch.isdigit())[-6:]
        market = "SH" if digits.startswith(("6", "9")) else ("BJ" if digits.startswith(("4", "8")) else "SZ")
        return f"{market}{digits}"

    def period_from_frequency(self, frequency: str) -> Optional[str]:
        return {
            "d": "day",
            "day": "day",
            "w": "week",
            "week": "week",
            "m": "month",
            "month": "month",
        }.get(str(frequency or "").lower())

    def begin_ms_from_end_date(self, end_date: Optional[str]) -> int:
        # Xueqiu web requests commonly use "now + 1 day" as begin when count is negative.
        return int((datetime.datetime.now() + datetime.timedelta(days=1)).timestamp() * 1000)

    def count_from_range(self, start_date: Optional[str], end_date: Optional[str], frequency: str) -> int:
        try:
            start = datetime.datetime.strptime(str(start_date), "%Y-%m-%d")
            end = datetime.datetime.strptime(str(end_date), "%Y-%m-%d")
            days = max(1, (end - start).days + 1)
        except Exception:
            days = 300
        if frequency in ["w", "week"]:
            count = max(60, int(days / 7) + 10)
        elif frequency in ["m", "month"]:
            count = max(36, int(days / 30) + 10)
        else:
            count = max(120, days)
        return -min(count, 300)

    def fetch_quote_by_code(
        self,
        code: str,
        cookie: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.fetch_quote(self.to_symbol(code), cookie=cookie, user_agent=user_agent)

    def fetch_kline_by_code(
        self,
        code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        frequency: str = "d",
        count: Optional[int] = None,
        cookie: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        period = self.period_from_frequency(frequency)
        if not period:
            return []
        symbol = self.to_symbol(code)
        begin_ms = self.begin_ms_from_end_date(end_date)
        resolved_count = count if count is not None else -284
        try:
            return self.fetch_kline(
                symbol,
                period=period,
                begin_ms=begin_ms,
                count=resolved_count,
                cookie=cookie,
                user_agent=user_agent,
                include_begin=True,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 400:
                return self.fetch_kline(
                    symbol,
                    period=period,
                    begin_ms=begin_ms,
                    count=resolved_count,
                    cookie=cookie,
                    user_agent=user_agent,
                    include_begin=False,
                )
            raise

    def fetch_minute_by_code(
        self,
        code: str,
        period: str = "1d",
        cookie: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self.fetch_minute(self.to_symbol(code), period=period, cookie=cookie, user_agent=user_agent)

    def fetch_trade_history_by_code(
        self,
        code: str,
        count: int = 10,
        cookie: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self.fetch_trade_history(self.to_symbol(code), count=count, cookie=cookie, user_agent=user_agent)

    def fetch_quote(
        self,
        symbol: str,
        cookie: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        candidates = [
            f"https://stock.xueqiu.com/v5/stock/realtime/quotec.json?symbol={symbol}",
            f"https://stock.xueqiu.com/v5/stock/realtime/pankou.json?symbol={symbol}",
        ]
        last_error = ""
        for url in candidates:
            try:
                parsed = self._parse_quote(symbol, self._get_json(url, symbol, cookie, user_agent))
                if parsed:
                    parsed["source"] = "xueqiu"
                    return parsed
            except Exception as exc:
                last_error = str(exc)
        if last_error:
            raise RuntimeError(last_error)
        return {}

    def fetch_kline(
        self,
        symbol: str,
        period: str = "day",
        begin_ms: Optional[int] = None,
        count: int = -300,
        cookie: Optional[str] = None,
        user_agent: Optional[str] = None,
        include_begin: bool = True,
    ) -> List[Dict[str, Any]]:
        begin_ms = begin_ms or int(datetime.datetime.now().timestamp() * 1000)
        indicators = "kline,pe,pb,ps,pcf,market_capital,agt,ggt,balance"
        if include_begin:
            url = (
                "https://stock.xueqiu.com/v5/stock/chart/kline.json"
                f"?symbol={symbol}&begin={begin_ms}&period={period}&type=before"
                f"&count={int(count)}&indicator={indicators}"
            )
        else:
            url = (
                "https://stock.xueqiu.com/v5/stock/chart/kline.json"
                f"?symbol={symbol}&period={period}&type=before"
                f"&count={int(count)}&indicator={indicators}"
            )
        payload = self._get_json(url, symbol, cookie, user_agent)
        data = payload.get("data") if isinstance(payload, dict) else {}
        columns = data.get("column") if isinstance(data, dict) else []
        rows = data.get("item") if isinstance(data, dict) else []
        if not isinstance(columns, list) or not isinstance(rows, list):
            return []
        records: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, list):
                continue
            item = self._row_from_columns(columns, row)
            timestamp = self._to_float(item.get("timestamp"))
            if timestamp is None:
                continue
            records.append({
                "date": datetime.datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d"),
                "open": self._to_float(item.get("open")),
                "close": self._to_float(item.get("close")),
                "high": self._to_float(item.get("high")),
                "low": self._to_float(item.get("low")),
                "volume": self._to_float(item.get("volume")),
                "amount": self._to_float(item.get("amount")),
                "pctChg": self._to_float(item.get("percent")),
                "change": self._to_float(item.get("chg")),
                "turnover": self._to_float(item.get("turnoverrate")),
                "market_capital": self._to_float(item.get("market_capital")),
                "pe": self._to_float(item.get("pe")),
                "pb": self._to_float(item.get("pb")),
            })
        return records

    def fetch_minute(
        self,
        symbol: str,
        period: str = "1d",
        cookie: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        url = f"https://stock.xueqiu.com/v5/stock/chart/minute.json?symbol={symbol}&period={period}"
        payload = self._get_json(url, symbol, cookie, user_agent)
        data = payload.get("data") if isinstance(payload, dict) else {}
        rows = data.get("items") or data.get("item") if isinstance(data, dict) else []
        if not isinstance(rows, list):
            return []
        records: List[Dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            normalized = self._row_from_columns(
                ["timestamp", "current", "price", "close", "volume", "amount", "percent", "chg", "avg_price"],
                [
                    item.get("timestamp") or item.get("time"),
                    item.get("current"),
                    item.get("price"),
                    item.get("close"),
                    item.get("volume"),
                    item.get("amount"),
                    item.get("percent"),
                    item.get("chg"),
                    item.get("avg_price"),
                ],
            )
            timestamp = self._to_float(normalized.get("timestamp"))
            price = self._to_float(normalized.get("current") or normalized.get("price") or normalized.get("close"))
            if timestamp is None or price is None:
                continue
            records.append({
                "date": datetime.datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d %H:%M:%S"),
                "open": price,
                "close": price,
                "high": price,
                "low": price,
                "volume": self._to_float(item.get("volume")),
                "amount": self._to_float(item.get("amount")),
                "pctChg": self._to_float(item.get("percent")),
                "change": self._to_float(item.get("chg")),
                "avg_price": self._to_float(item.get("avg_price")),
            })
        return records

    def fetch_trade_history(
        self,
        symbol: str,
        count: int = 10,
        cookie: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        url = f"https://stock.xueqiu.com/v5/stock/history/trade.json?symbol={symbol}&count={int(count)}"
        payload = self._get_json(url, symbol, cookie, user_agent)
        data = payload.get("data") if isinstance(payload, dict) else {}
        rows = data.get("items") or data.get("item") if isinstance(data, dict) else []
        return [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []

    def _headers(self, symbol: str, cookie: Optional[str], user_agent: Optional[str]) -> Dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://xueqiu.com",
            "Referer": f"https://xueqiu.com/S/{symbol}",
            "Priority": "u=1, i",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Sec-CH-UA": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "User-Agent": user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        }
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _get_json(self, url: str, symbol: str, cookie: Optional[str], user_agent: Optional[str]) -> Dict[str, Any]:
        with httpx.Client(timeout=self._timeout_sec, follow_redirects=True, trust_env=False) as client:
            response = client.get(url, headers=self._headers(symbol, cookie, user_agent))
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {}

    def _parse_quote(self, symbol: str, payload: Any) -> Dict[str, Any]:
        data = payload.get("data") or payload.get("quote") or payload if isinstance(payload, dict) else {}
        if isinstance(data, list):
            data = data[0] if data and isinstance(data[0], dict) else {}
        if not isinstance(data, dict):
            return {}
        price = self._to_float(data.get("current") or data.get("current_price") or data.get("last_close") or data.get("last"))
        prev_close = self._to_float(data.get("last_close") or data.get("prev_close"))
        change = self._to_float(data.get("chg") or data.get("change"))
        pct_chg = self._to_float(data.get("percent") or data.get("chg_pct") or data.get("percent_chg"))
        if change is None and price is not None and prev_close not in (None, 0):
            change = price - prev_close
        if pct_chg is None and change is not None and prev_close not in (None, 0):
            pct_chg = change / prev_close * 100
        code = symbol[-6:] if len(symbol) >= 6 else symbol
        return {
            "code": str(code),
            "name": str(data.get("name") or data.get("symbol_name") or symbol),
            "price": price,
            "pct_chg": pct_chg,
            "change": change,
            "amount": self._to_float(data.get("amount") or data.get("turnover")),
            "volume": self._to_float(data.get("volume") or data.get("vol")),
            "high": self._to_float(data.get("high")),
            "low": self._to_float(data.get("low")),
            "open": self._to_float(data.get("open")),
            "prev_close": prev_close,
        }

    def _to_float(self, value: Any) -> Optional[float]:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except Exception:
            return None

    def _row_from_columns(self, columns: List[Any], row: List[Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for idx in range(min(len(columns), len(row))):
            result[str(columns[idx])] = row[idx]
        return result
