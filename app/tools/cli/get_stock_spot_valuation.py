import json

from app.tools.sources.stock_spot_valuation_tool import StockSpotValuationTool


def main() -> None:
    tool = StockSpotValuationTool()
    print(json.dumps(tool.fetch(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
