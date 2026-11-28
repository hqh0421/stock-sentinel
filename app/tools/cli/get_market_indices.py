from app.tools.common import get_service, print_json


def main() -> None:
    service = get_service()
    result = service.get_market_indices()
    print_json(result)


if __name__ == "__main__":
    main()



