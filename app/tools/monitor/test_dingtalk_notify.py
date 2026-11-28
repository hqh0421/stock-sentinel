import argparse
from app.tools.common import get_service, print_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Send dingtalk test notification.")
    parser.add_argument("--include-monitor-snapshot", action="store_true")
    args = parser.parse_args()
    service = get_service()
    monitor_data = service.monitor_favorites(force_refresh=False) if args.include_monitor_snapshot else []
    result = service.notify_monitor_results(monitor_data, force_send=True)
    print_json(result)


if __name__ == "__main__":
    main()



