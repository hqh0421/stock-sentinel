from typing import Any, Dict

import akshare as ak

from .market_data_tool import MarketDataTool


class StockSpotValuationTool:
    def __init__(self, timeout_sec: float = 8.0) -> None:
        self._market = MarketDataTool(timeout_sec=timeout_sec)

    def fetch(self) -> Dict[str, Any]:
        df = self._market.get_spot_df()
        if df is None or getattr(df, "empty", True):
            return {"rows": []}
        rows = df.to_dict("records")
        normalized = []
        for row in rows:
            normalized.append({
                "code": row.get("代码") or row.get("code") or "",
                "name": row.get("名称") or row.get("name") or "",
                "price": row.get("最新价") or row.get("最新") or row.get("price"),
                "pct_chg": row.get("涨跌幅") or row.get("pct_chg"),
                "pe": row.get("市盈率-动态") or row.get("市盈率") or row.get("pe"),
                "pb": row.get("市净率") or row.get("pb"),
                "total_mv": row.get("总市值") or row.get("total_mv"),
                "circ_mv": row.get("流通市值") or row.get("circ_mv"),
            })
        return {"rows": normalized}
