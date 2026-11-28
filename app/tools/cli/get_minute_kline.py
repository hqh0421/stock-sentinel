import argparse
from app.tools.common import get_service, print_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Get realtime minute kline data.")
    parser.add_argument("--code", required=True)
    args = parser.parse_args()
    service = get_service()
    result = service.get_realtime_minute_data(args.code)
    print_json({"code": args.code, "count": len(result), "data": result})


if __name__ == "__main__":
    main()



