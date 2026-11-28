import argparse
from app.tools.common import get_service, print_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Add one favorite stock.")
    parser.add_argument("--code", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--shares", type=int, default=0)
    parser.add_argument("--cost", type=float, default=0.0)
    parser.add_argument("--horizon", default="short")
    parser.add_argument("--buy-date", default="")
    args = parser.parse_args()
    service = get_service()
    service.add_favorite(
        args.code,
        shares=args.shares,
        cost=args.cost,
        horizon=args.horizon,
        buy_date=args.buy_date or None,
        name=args.name or None,
    )
    print_json({"ok": True, "code": args.code})


if __name__ == "__main__":
    main()



