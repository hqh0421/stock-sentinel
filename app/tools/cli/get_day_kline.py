import argparse
from app.tools.common import get_service, print_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Get daily kline data.")
    parser.add_argument("--code", required=True)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    args = parser.parse_args()
    service = get_service()
    result = service.get_k_line_payload(args.code, start_date=args.start or None, end_date=args.end or None, frequency="d")
    print_json(result)


if __name__ == "__main__":
    main()



