"""通知发送器（薄适配层）。

具体渠道实现见 app/services/notification_channels.py。这里保留
``send_dingtalk_markdown`` 的签名与返回结构，确保既有 100+ 调用点和测试零改动；
新渠道通过 ``send(channel, ...)`` 与 ``send_to_enabled()`` 接入，调度 / 冷却 /
去重 / alert_log 仍由 AlertService 统一负责，本层只负责「发到哪」。
"""
from typing import Any, Dict, List

from ...services import notification_channels


class NotifyTool:
    def __init__(self, timeout_sec: float = 8.0) -> None:
        self._timeout = timeout_sec

    # ---- 既有接口：行为不变 --------------------------------------------------
    def send_dingtalk_markdown(self, webhook_url: str, title: str, text: str,
                               secret: str = "") -> Dict[str, Any]:
        return notification_channels.send_dingtalk(
            webhook_url, title, text, secret=secret, timeout=self._timeout
        )

    # ---- 新接口：可插拔渠道 --------------------------------------------------
    def send(self, channel: str, webhook_url: str, title: str, text: str,
             secret: str = "", **opts: Any) -> Dict[str, Any]:
        """发到单个指定渠道。"""
        return notification_channels.send(
            channel, webhook_url, title, text, secret=secret,
            timeout=self._timeout, **opts
        )

    def send_to_enabled(self, settings: Dict[str, Any], title: str, text: str) -> List[Dict[str, Any]]:
        """把同一条消息发到所有在设置里启用的渠道。

        只读 settings 里的 ``<channel>_enabled`` / ``<channel>_webhook_url`` /
        ``<channel>_secret``；新渠道未配置或默认关闭时不会发。返回结果列表供
        AlertService 记 alert_log，本方法不写库、不做冷却。
        """
        results: List[Dict[str, Any]] = []
        for channel in notification_channels.SUPPORTED_CHANNELS:
            if not bool(settings.get(f"{channel}_enabled", False)):
                continue
            webhook = str(settings.get(notification_channels.recipient_key(channel)) or "").strip()
            secret = str(settings.get(f"{channel}_secret") or "").strip()
            if not webhook:
                results.append({"channel": channel, "ok": False, "error": "recipient is empty"})
                continue
            opts = {}
            if channel == "email":
                for key in ("smtp_host", "smtp_port", "smtp_user", "smtp_password",
                            "smtp_use_tls", "mail_from"):
                    if settings.get(key) is not None:
                        opts[key] = settings[key]
            result = self.send(channel, webhook, title, text, secret=secret, **opts)
            results.append({"channel": channel, **result})
        return results
