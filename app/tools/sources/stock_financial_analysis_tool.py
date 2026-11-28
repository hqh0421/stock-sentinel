from typing import Any, Dict

import akshare as ak


class StockFinancialAnalysisTool:
    def fetch(self, symbol: str, indicator: str = "按报告期") -> Dict[str, Any]:
        df = ak.stock_financial_analysis_indicator_em(symbol=symbol, indicator=indicator)
        rows = df.to_dict("records") if df is not None else []
        return {
            "symbol": symbol,
            "indicator": indicator,
            "rows": rows,
        }


class StockFinancialIndicatorTool:
    """新浪财务分析指标（按报告期，含同比增速/毛利率/负债率/现金流质量）。

    选它而不是东财接口的原因：实测东财 `stock_financial_analysis_indicator_em`
    在当前环境直接抛 TypeError 且受 push2 限流影响，新浪接口 0.9s 稳定返回，
    字段也够用（ROE、营收/净利增速、毛利率、资产负债率、经营现金流占净利比）。
    """

    def fetch(self, symbol: str, start_year: str) -> Dict[str, Any]:
        df = ak.stock_financial_analysis_indicator(symbol=symbol, start_year=start_year)
        rows = df.to_dict("records") if df is not None else []
        return {
            "symbol": symbol,
            "start_year": start_year,
            "rows": rows,
        }
