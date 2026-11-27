import argparse
from app.tools.common import get_service, print_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Get stock money flow data.")
    parser.add_argument("--code", required=True)
    args = parser.parse_args()
    service = get_service()
    result = service.get_money_flow(args.code)
    print_json(result)


if __name__ == "__main__":
    main()



