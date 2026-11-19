import time
from typing import Any, Dict, Optional

import httpx


class HttpJsonTool:
    def __init__(self, timeout_sec: float = 8.0) -> None:
        self._timeout = timeout_sec

    def get_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        retries: int = 2,
        retry_sleep_sec: float = 0.4,
    ) -> Dict[str, Any]:
        last_exc: Optional[Exception] = None
        for _ in range(max(1, retries)):
            try:
                with httpx.Client(timeout=self._timeout, follow_redirects=True, trust_env=False) as client:
                    response = client.get(url, params=params or {}, headers=headers or {})
                    response.raise_for_status()
                    data = response.json()
                    return data if isinstance(data, dict) else {}
            except Exception as exc:
                last_exc = exc
                time.sleep(retry_sleep_sec)
        if last_exc:
            raise last_exc
        return {}
