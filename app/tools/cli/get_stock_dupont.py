import argparse
import json

from app.tools.sources.stock_dupont_tool import StockDupontTool


def main() -> None:
    parser = argparse.ArgumentParser(description="Get stock Dupont comparison indicators.")
    parser.add_argument("--symbol", required=True, help="e.g. SZ000895")
    args = parser.parse_args()
    tool = StockDupontTool()
    print(json.dumps(tool.fetch(args.symbol), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
