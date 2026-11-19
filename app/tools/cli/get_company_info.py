import argparse
from app.tools.common import get_service, print_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Get company profile info.")
    parser.add_argument("--code", required=True)
    args = parser.parse_args()
    service = get_service()
    result = service.get_company_info(args.code)
    print_json(result)


if __name__ == "__main__":
    main()



