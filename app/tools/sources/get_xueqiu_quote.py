import argparse
from app.tools.common import get_service, print_json
from app.tools.sources.xueqiu_market import XueqiuMarketDataTool


def main() -> None:
    parser = argparse.ArgumentParser(description="Get quote from Xueqiu source only.")
    parser.add_argument("--code", required=True, help="e.g. SZ300476 / sh600519 / 000001")
    args = parser.parse_args()
    service = get_service()
    settings = service.get_user_settings()
    tool = XueqiuMarketDataTool()
    result = tool.fetch_quote_by_code(
        args.code,
        cookie=str(settings.get("xueqiu_cookie") or "").strip() or None,
        user_agent=str(settings.get("xueqiu_user_agent") or "").strip() or None,
    )
    print_json(result)


if __name__ == "__main__":
    main()



