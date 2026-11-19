import logging
from typing import Dict, Optional

import httpx

logger = logging.getLogger("app.market_data")


class XueqiuCookieRefresher:
    def refresh_guest_cookie(
        self,
        user_agent: Optional[str] = None,
        refresh_url: Optional[str] = None,
        headless: bool = True,
        executable_path: Optional[str] = None,
    ) -> Dict[str, str]:
        ua = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        url = refresh_url or "https://xueqiu.com/S/SZ002230"
        cookie = self._from_playwright(ua, url, headless=headless, executable_path=executable_path)
        if cookie and self._is_usable(cookie):
            return {"cookie": cookie, "user_agent": ua, "source": "playwright", "refresh_url": url}
        cookie = self._from_httpx(ua, url)
        if cookie and self._is_usable(cookie):
            return {"cookie": cookie, "user_agent": ua, "source": "httpx", "refresh_url": url}
        return {"cookie": "", "user_agent": ua, "source": "none", "refresh_url": url}

    def _is_usable(self, cookie: str) -> bool:
        text = str(cookie or "")
        required = ["xq_a_token=", "xqat=", "device_id="]
        return all(part in text for part in required)

    def _from_playwright(self, ua: str, refresh_url: str, headless: bool, executable_path: Optional[str]) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            logger.warning("xueqiu_cookie_playwright_unavailable error=%r", exc)
            return ""
        try:
            with sync_playwright() as p:
                launch_options = {"headless": headless}
                if executable_path:
                    launch_options["executable_path"] = executable_path
                browser = p.chromium.launch(**launch_options)
                context = browser.new_context(user_agent=ua, locale="zh-CN")
                page = context.new_page()
                page.goto(refresh_url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2000)
                page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2000)
                cookies = context.cookies("https://xueqiu.com/")
                browser.close()
            logger.info("xueqiu_cookie_playwright_names names=%s", ",".join(c.get("name", "") for c in cookies))
            pairs = [f"{c.get('name')}={c.get('value')}" for c in cookies if c.get("name") and c.get("value")]
            return "; ".join(pairs)
        except Exception as exc:
            logger.warning("xueqiu_cookie_playwright_failed refresh_url=%s error=%r", refresh_url, exc)
            return ""

    def _from_httpx(self, ua: str, refresh_url: str) -> str:
        try:
            with httpx.Client(timeout=15, follow_redirects=True, trust_env=False, headers={"User-Agent": ua}) as client:
                client.get(refresh_url)
                client.get("https://xueqiu.com/")
                cookies = client.cookies
            logger.info("xueqiu_cookie_httpx_names names=%s", ",".join(cookies.keys()))
            pairs = [f"{k}={v}" for k, v in cookies.items() if k and v]
            return "; ".join(pairs)
        except Exception as exc:
            logger.warning("xueqiu_cookie_httpx_failed refresh_url=%s error=%r", refresh_url, exc)
            return ""
