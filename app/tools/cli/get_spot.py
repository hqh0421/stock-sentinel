import argparse
from app.tools.common import get_service, print_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Get realtime spot quote for one stock.")
    parser.add_argument("--code", required=True, help="e.g. SZ300476 / sh600519 / 000001")
    parser.add_argument("--force-refresh", action="store_true", help="force refresh when fallback snapshot is used")
    args = parser.parse_args()
    service = get_service()
    result = service.get_stock_spot_info(args.code, allow_full_market_fallback=True, force_refresh=args.force_refresh)
    print_json(result)


if __name__ == "__main__":
    main()



