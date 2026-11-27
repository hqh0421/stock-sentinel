import argparse
import json

from app.tools.sources.fund_portfolio_hold_tool import FundPortfolioHoldTool


def main() -> None:
    parser = argparse.ArgumentParser(description="Get fund portfolio holdings.")
    parser.add_argument("--symbol", required=True, help="fund code, e.g. 000001")
    parser.add_argument("--year", required=True, help="year, e.g. 2024")
    args = parser.parse_args()
    tool = FundPortfolioHoldTool()
    print(json.dumps(tool.fetch(args.symbol, args.year), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
