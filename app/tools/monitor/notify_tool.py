import base64
import hashlib
import hmac
import time
import urllib.parse
from typing import Any, Dict

import httpx


class NotifyTool:
    def __init__(self, timeout_sec: float = 8.0) -> None:
        self._timeout = timeout_sec

    def _build_dingtalk_sign(self, secret: str) -> Dict[str, str]:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code).decode("utf-8"))
        return {"timestamp": timestamp, "sign": sign}

    def _signed_webhook_url(self, webhook_url: str, secret: str = "") -> str:
        if not secret:
            return webhook_url
        sign_params = self._build_dingtalk_sign(secret)
        separator = "&" if "?" in webhook_url else "?"
        return f"{webhook_url}{separator}timestamp={sign_params['timestamp']}&sign={sign_params['sign']}"

    def send_dingtalk_markdown(self, webhook_url: str, title: str, text: str, secret: str = "") -> Dict[str, Any]:
        if not webhook_url:
            return {"ok": False, "error": "webhook is empty"}
        url = self._signed_webhook_url(webhook_url.strip(), secret.strip())
        payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
        try:
            with httpx.Client(timeout=self._timeout, follow_redirects=True, trust_env=False) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
                data = response.json() if response.text else {}
                if isinstance(data, dict) and int(data.get("errcode", 0) or 0) != 0:
                    return {"ok": False, "error": data.get("errmsg") or str(data)}
                return {"ok": True, "response": data}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
