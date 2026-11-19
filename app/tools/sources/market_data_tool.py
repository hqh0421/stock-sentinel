import datetime
import os
from contextlib import contextmanager
from threading import Lock
from typing import Any, Dict, List, Optional

import akshare as ak
import baostock as bs
import pandas as pd
from .http_json_tool import HttpJsonTool


class MarketDataTool:
    def __init__(self, timeout_sec: float = 8.0) -> None:
        self._timeout = timeout_sec
        self._baostock_lock = Lock()
        self._http_json = HttpJsonTool(timeout_sec=timeout_sec)

    @contextmanager
    def _without_proxy_env(self):
        proxy_keys = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
        backup = {key: os.environ.get(key) for key in proxy_keys}
        try:
            for key in proxy_keys:
                os.environ.pop(key, None)
            yield
        finally:
            for key, value in backup.items():
                if value is not None:
                    os.environ[key] = value

    def _to_float(self, value: Any) -> Optional[float]:
        try:
            if value in [None, ""]:
                return None
            return float(value)
        except Exception:
            return None

    def fetch_eastmoney_quote(self, secid: str) -> Dict[str, Any]:
        params = {
            "secid": secid,
            "invt": "2",
            "fltt": "2",
            "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170",
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
        return self._http_json.get_json(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params=params,
            headers=headers,
        )

    def fetch_eastmoney_kline(self, secid: str, frequency: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        klt_map = {"1": "1", "5": "5", "15": "15", "30": "30", "60": "60", "d": "101", "w": "102", "m": "103"}
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": klt_map.get(frequency, "101"),
            "fqt": "1",
            "beg": (start_date or "0").replace("-", ""),
            "end": (end_date or "20500101").replace("-", ""),
            "lmt": "1000",
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
        payload = self._http_json.get_json(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params=params,
            headers=headers,
        )
        rows = (((payload or {}).get("data") or {}).get("klines")) or []
        result = []
        for row in rows:
            parts = str(row).split(",")
            if len(parts) < 11:
                continue
            result.append(
                {
                    "date": parts[0],
                    "open": self._to_float(parts[1]),
                    "close": self._to_float(parts[2]),
                    "high": self._to_float(parts[3]),
                    "low": self._to_float(parts[4]),
                    "volume": self._to_float(parts[5]),
                    "amount": self._to_float(parts[6]),
                    "amplitude": self._to_float(parts[7]),
                    "pctChg": self._to_float(parts[8]),
                    "change": self._to_float(parts[9]),
                    "turnover": self._to_float(parts[10]),
                }
            )
        return result

    def fetch_baostock_kline(self, bs_code: str, frequency: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        if frequency not in ["d", "w", "m"]:
            return []
        with self._baostock_lock, self._without_proxy_env():
            login_result = None
            try:
                login_result = bs.login()
                if getattr(login_result, "error_code", "0") != "0":
                    return []
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,high,low,close,preclose,volume,amount,turn,pctChg",
                    start_date=(start_date or "2020-01-01"),
                    end_date=(end_date or datetime.datetime.now().strftime("%Y-%m-%d")),
                    frequency=frequency,
                    adjustflag="2",
                )
                if getattr(rs, "error_code", "0") != "0":
                    return []
                rows = []
                while rs.error_code == "0" and rs.next():
                    data = rs.get_row_data()
                    if not data:
                        continue
                    preclose = self._to_float(data[5])
                    close = self._to_float(data[4])
                    change = (close - preclose) if close is not None and preclose is not None else None
                    rows.append(
                        {
                            "date": data[0],
                            "open": self._to_float(data[1]),
                            "high": self._to_float(data[2]),
                            "low": self._to_float(data[3]),
                            "close": close,
                            "prev_close": preclose,
                            "volume": self._to_float(data[6]),
                            "amount": self._to_float(data[7]),
                            "turnover": self._to_float(data[8]),
                            "pctChg": self._to_float(data[9]),
                            "change": change,
                        }
                    )
                return rows
            finally:
                if login_result is not None:
                    try:
                        bs.logout()
                    except Exception:
                        pass

    def get_stock_list(self) -> pd.DataFrame:
        with self._without_proxy_env():
            return ak.stock_info_a_code_name()

    def get_spot_df(self) -> pd.DataFrame:
        with self._without_proxy_env():
            return ak.stock_zh_a_spot_em()

    def get_company_profile(self, symbol: str) -> pd.DataFrame:
        with self._without_proxy_env():
            return ak.stock_profile_cninfo(symbol=symbol)

    def get_market_indices_df(self) -> pd.DataFrame:
        with self._without_proxy_env():
            # 注：akshare 旧接口 stock_zh_index_spot 已在新版移除，改用新浪实时指数接口
            return ak.stock_zh_index_spot_sina()

    def get_sw_index_realtime(self, symbol: str) -> pd.DataFrame:
        with self._without_proxy_env():
            return ak.index_realtime_sw(symbol=symbol)

    def get_sw_index_analysis_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        with self._without_proxy_env():
            return ak.index_analysis_daily_sw(symbol=symbol, start_date=start_date, end_date=end_date)

    def get_sw_index_component(self, symbol: str) -> pd.DataFrame:
        with self._without_proxy_env():
            return ak.index_component_sw(symbol=symbol)

    def get_money_flow_df(self, digits: str, market: str) -> pd.DataFrame:
        with self._without_proxy_env():
            return ak.stock_individual_fund_flow(stock=digits, market=market)

    def get_index_daily(self, symbol: str = "sh000001") -> pd.DataFrame:
        """大盘指数日线（新浪）。symbol 形如 sh000001 / sz399001 / sh000300。"""
        with self._without_proxy_env():
            return ak.stock_zh_index_daily(symbol=symbol)

    def get_sw_index_hist(self, industry_code: str) -> pd.DataFrame:
        """申万行业指数日线（含历史全量），用于判断行业周期位置。"""
        with self._without_proxy_env():
            return ak.index_hist_sw(symbol=str(industry_code), period="day")

    def get_akshare_kline(self, symbol: str, frequency: str, start_date: str, end_date: str) -> pd.DataFrame:
        with self._without_proxy_env():
            if frequency in ["1", "5", "15", "30", "60"]:
                return ak.stock_zh_a_hist_min_em(symbol=symbol, period=frequency, adjust="qfq")
            period = {"d": "daily", "w": "weekly", "m": "monthly"}.get(frequency, "daily")
            return ak.stock_zh_a_hist(
                symbol=symbol,
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
                period=period,
                adjust="qfq",
            )
