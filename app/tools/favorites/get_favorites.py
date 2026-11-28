import argparse
from app.tools.common import get_service, print_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Get favorites list with spot data.")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    service = get_service()
    result = service.get_favorites_list(force_refresh=args.refresh)
    print_json({"data": result})


if __name__ == "__main__":
    main()



