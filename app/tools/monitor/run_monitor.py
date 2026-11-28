import argparse
from app.tools.common import get_service, print_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Run monitor for all favorites.")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    service = get_service()
    data = service.monitor_favorites(force_refresh=args.refresh)
    notify = service.notify_monitor_results(data, force_send=False)
    print_json({"data": data, "notify": notify})


if __name__ == "__main__":
    main()



