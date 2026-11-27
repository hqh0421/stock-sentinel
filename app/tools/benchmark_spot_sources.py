import argparse
from app.tools.common import get_service, print_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark A-share quote stability and latency.")
    parser.add_argument("--sample-size", type=int, default=80, help="number of symbols for single-quote benchmark")
    parser.add_argument("--timeout-sec", type=float, default=2.0, help="per-source timeout in seconds")
    args = parser.parse_args()
    service = get_service()
    result = service.benchmark_spot_sources(sample_size=args.sample_size, timeout_sec=args.timeout_sec)
    print_json(result)


if __name__ == "__main__":
    main()



