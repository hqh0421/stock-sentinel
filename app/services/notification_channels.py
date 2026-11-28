"""可插拔通知渠道发送层（G5 / G8）。

设计约束（roadmap G5 / PRD §12）：
- 只替换「发送目的地」，不复制调度、冷却、去重、alert_log——那套逻辑全在 AlertService，
  一个都不在这里重写。
- 钉钉实现从 NotifyTool 迁入，行为（签名、返回结构、异常吞噬）保持不变。
- 新渠道默认不启用；是否启用由 user_settings 决定，这里只读传入的开关。
- 失败一律返回 {"ok": False, "error": ...}，绝不抛异常打断通知主链路。

每个渠道函数签名统一：
    send(channel, webhook_url, title, text, secret="", **opts) -> dict
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import smtplib
import time
import urllib.parse
from email.message import EmailMessage
from typing import Any, Dict

import httpx

SUPPORTED_CHANNELS = ("dingtalk", "feishu", "wecom", "email")


def recipient_key(channel: str) -> str:
    """渠道收件地址在 settings 里的键名。默认 <channel>_webhook_url。"""
    return "email_recipients" if channel == "email" else f"{channel}_webhook_url"


def _dingtalk_sign(secret: str) -> Dict[str, str]:
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code).decode("utf-8"))
    return {"timestamp": timestamp, "sign": sign}


def _signed_webhook_url(webhook_url: str, secret: str = "") -> str:
    if not secret:
        return webhook_url
    sign = _dingtalk_sign(secret)
    separator = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{separator}timestamp={sign['timestamp']}&sign={sign['sign']}"


def _post_json(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json() if response.text else {}
            return {"ok": True, "response": data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def send_dingtalk(webhook_url: str, title: str, text: str, secret: str = "",
                  timeout: float = 8.0) -> Dict[str, Any]:
    """迁移自 NotifyTool.send_dingtalk_markdown，行为逐字保持一致。"""
    if not webhook_url:
        return {"ok": False, "error": "webhook is empty"}
    url = _signed_webhook_url(webhook_url.strip(), secret.strip())
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    result = _post_json(url, payload, timeout)
    if not result.get("ok"):
        return result
    data = result.get("response") if isinstance(result.get("response"), dict) else {}
    if int(data.get("errcode", 0) or 0) != 0:
        return {"ok": False, "error": data.get("errmsg") or str(data)}
    return {"ok": True, "response": data}


def send_feishu(webhook_url: str, title: str, text: str, secret: str = "",
                timeout: float = 8.0) -> Dict[str, Any]:
    """飞书自定义机器人：interactive 卡片，text 作为 markdown 内容。"""
    if not webhook_url:
        return {"ok": False, "error": "webhook is empty"}
    # 飞书机器人 Security setting 的签名校验（可选）
    url = webhook_url.strip()
    if secret:
        timestamp = str(round(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        sign = base64.b64encode(hmac_code).decode("utf-8")
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}timestamp={timestamp}&sign={urllib.parse.quote_plus(sign)}"
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
            "elements": [{"tag": "markdown", "content": text}],
        },
    }
    result = _post_json(url, payload, timeout)
    if not result.get("ok"):
        return result
    data = result.get("response") if isinstance(result.get("response"), dict) else {}
    # 飞书成功响应 {"StatusCode": 0, ...} 或 {"code": 0,...}
    code = data.get("StatusCode", data.get("code", 0))
    if int(code or 0) != 0:
        return {"ok": False, "error": data.get("StatusMessage") or data.get("msg") or str(data)}
    return {"ok": True, "response": data}


def send_wecom(webhook_url: str, title: str, text: str, secret: str = "",
               timeout: float = 8.0) -> Dict[str, Any]:
    """企业微信群机器人：markdown 消息；secret 预留（群机器人通常无需签名）。"""
    if not webhook_url:
        return {"ok": False, "error": "webhook is empty"}
    payload = {"msgtype": "markdown", "markdown": {"content": f"### {title}\n{text}"}}
    result = _post_json(webhook_url.strip(), payload, timeout)
    if not result.get("ok"):
        return result
    data = result.get("response") if isinstance(result.get("response"), dict) else {}
    if int(data.get("errcode", 0) or 0) != 0:
        return {"ok": False, "error": data.get("errmsg") or str(data)}
    return {"ok": True, "response": data}


def send_email(webhook_url: str, title: str, text: str, secret: str = "",
               timeout: float = 8.0, **opts: Any) -> Dict[str, Any]:
    """G8：标准库 smtplib。收件人走 webhook_url 参数位（用 '|' 分隔多个地址）。

    其余参数通过 opts 传入：smtp_host / smtp_port / smtp_user / smtp_password /
    smtp_use_tls / mail_from。全部缺省时不发送。
    """
    host = str(opts.get("smtp_host") or "").strip()
    if not host or not webhook_url:
        return {"ok": False, "error": "smtp not configured"}
    recipients = [a.strip() for a in str(webhook_url).split("|") if a.strip()]
    if not recipients:
        return {"ok": False, "error": "no recipient"}
    try:
        port = int(opts.get("smtp_port") or 465)
        use_tls = bool(opts.get("smtp_use_tls", True))
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = str(opts.get("mail_from") or opts.get("smtp_user") or host)
        msg["To"] = ", ".join(recipients)
        msg.set_content(text)
        if use_tls:
            with smtplib.SMTP_SSL(host, port, timeout=timeout) as client:
                if opts.get("smtp_user"):
                    client.login(str(opts["smtp_user"]), str(opts.get("smtp_password") or ""))
                client.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as client:
                if opts.get("smtp_user"):
                    client.starttls()
                    client.login(str(opts["smtp_user"]), str(opts.get("smtp_password") or ""))
                client.send_message(msg)
        return {"ok": True, "response": {"recipients": recipients}}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


_SENDERS = {
    "dingtalk": send_dingtalk,
    "feishu": send_feishu,
    "wecom": send_wecom,
    "email": send_email,
}


def send(channel: str, webhook_url: str, title: str, text: str, secret: str = "",
         timeout: float = 8.0, **opts: Any) -> Dict[str, Any]:
    """统一发送入口。未知渠道返回结构化失败，不抛异常。"""
    fn = _SENDERS.get(str(channel or "").strip().lower())
    if fn is None:
        return {"ok": False, "error": f"unsupported channel: {channel}"}
    return fn(webhook_url, title, text, secret=secret, timeout=timeout, **opts)
