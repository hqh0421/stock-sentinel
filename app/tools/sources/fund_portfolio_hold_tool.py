from typing import Any, Dict

import akshare as ak


class FundPortfolioHoldTool:
    def fetch(self, symbol: str, year: str) -> Dict[str, Any]:
        df = ak.fund_portfolio_hold_em(symbol=symbol, date=year)
        rows = df.to_dict("records") if df is not None else []
        return {
            "symbol": symbol,
            "year": year,
            "rows": rows,
        }
