import argparse
import json

from app.tools.sources.stock_financial_analysis_tool import StockFinancialAnalysisTool


def main() -> None:
    parser = argparse.ArgumentParser(description="Get stock financial analysis indicators.")
    parser.add_argument("--symbol", required=True, help="e.g. 000895 or SZ000895")
    parser.add_argument("--indicator", default="按报告期", help="default: 按报告期")
    args = parser.parse_args()
    tool = StockFinancialAnalysisTool()
    print(json.dumps(tool.fetch(args.symbol, indicator=args.indicator), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
