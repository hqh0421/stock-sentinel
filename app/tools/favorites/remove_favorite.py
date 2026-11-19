import argparse
from app.tools.common import get_service, print_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove one favorite stock.")
    parser.add_argument("--code", required=True)
    args = parser.parse_args()
    service = get_service()
    service.remove_favorite(args.code)
    print_json({"ok": True, "code": args.code})


if __name__ == "__main__":
    main()



