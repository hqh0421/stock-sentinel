from app.services.stock_service import StockService
import json

def disable_proxy_env() -> None:
    import os
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)

def get_service() -> StockService:
    disable_proxy_env()
    return StockService()

def print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
