from typing import Any, Dict

import akshare as ak


class StockDupontTool:
    def fetch(self, symbol: str) -> Dict[str, Any]:
        df = ak.stock_zh_dupont_comparison_em(symbol=symbol)
        rows = df.to_dict("records") if df is not None else []
        return {
            "symbol": symbol,
            "rows": rows,
        }
