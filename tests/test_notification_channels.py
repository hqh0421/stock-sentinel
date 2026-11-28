"""可插拔通知渠道（G5/G8）单测：全部 mock，不碰真实网络 / SMTP。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from app.services import notification_channels as nc
from app.tools.monitor.notify_tool import NotifyTool


class FakeResponse:
    def __init__(self, data, text='{}', raise_error=None):
        self._data = data
        self.text = text
        self._raise = raise_error

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def json(self):
        return self._data


class FakeClient:
    """捕获 post 调用，返回预置响应。"""
    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json):
        FakeClient.last = {"url": url, "json": json}
        return FakeResponse(FakeClient.response)


class ChannelSendTest(unittest.TestCase):
    def setUp(self):
        FakeClient.last = None
        FakeClient.response = {"errcode": 0, "errmsg": "ok"}

    def test_unsupported_channel_fails_softly(self):
        r = nc.send("sms", "x", "t", "b")
        self.assertFalse(r["ok"])
        self.assertIn("unsupported", r["error"])

    def test_dingtalk_payload_and_sign_unchanged(self):
        with mock.patch.object(nc.httpx, "Client", FakeClient):
            r = nc.send_dingtalk("https://hook", "标题", "正文", secret="SEC123")
        self.assertTrue(r["ok"])
        self.assertIn("timestamp=", FakeClient.last["url"])
        self.assertIn("sign=", FakeClient.last["url"])
        self.assertEqual(FakeClient.last["json"]["msgtype"], "markdown")
        self.assertEqual(FakeClient.last["json"]["markdown"]["title"], "标题")

    def test_dingtalk_errcode_nonzero_is_failure(self):
        FakeClient.response = {"errcode": 310000, "errmsg": "sign not match"}
        with mock.patch.object(nc.httpx, "Client", FakeClient):
            r = nc.send_dingtalk("https://hook", "t", "b")
        self.assertFalse(r["ok"])
        self.assertIn("sign not match", str(r["error"]))

    def test_dingtalk_empty_webhook(self):
        r = nc.send_dingtalk("", "t", "b")
        self.assertFalse(r["ok"])
        self.assertIn("empty", r["error"])

    def test_feishu_interactive_card(self):
        FakeClient.response = {"StatusCode": 0, "StatusMessage": "success"}
        with mock.patch.object(nc.httpx, "Client", FakeClient):
            r = nc.send_feishu("https://feishu-hook", "标题", "正文")
        self.assertTrue(r["ok"])
        card = FakeClient.last["json"]["card"]
        self.assertEqual(card["header"]["title"]["content"], "标题")
        self.assertEqual(card["elements"][0]["tag"], "markdown")

    def test_feishu_nonzero_status_is_failure(self):
        FakeClient.response = {"StatusCode": 1, "StatusMessage": "bad token"}
        with mock.patch.object(nc.httpx, "Client", FakeClient):
            r = nc.send_feishu("https://feishu-hook", "t", "b")
        self.assertFalse(r["ok"])
        self.assertIn("bad token", str(r["error"]))

    def test_wecom_markdown(self):
        FakeClient.response = {"errcode": 0, "errmsg": "ok"}
        with mock.patch.object(nc.httpx, "Client", FakeClient):
            r = nc.send_wecom("https://wecom-hook", "标题", "正文")
        self.assertTrue(r["ok"])
        self.assertEqual(FakeClient.last["json"]["msgtype"], "markdown")
        self.assertIn("标题", FakeClient.last["json"]["markdown"]["content"])

    def test_network_error_is_swallowed(self):
        def boom(*a, **k):
            raise RuntimeError("connect timeout")
        with mock.patch.object(nc.httpx, "Client", side_effect=boom):
            r = nc.send("feishu", "https://x", "t", "b")
        self.assertFalse(r["ok"])
        self.assertIn("timeout", r["error"])


class EmailChannelTest(unittest.TestCase):
    def test_no_smtp_config_fails_softly(self):
        r = nc.send_email("a@b.com", "t", "b")
        self.assertFalse(r["ok"])
        self.assertIn("not configured", r["error"])

    def test_email_success(self):
        captured = {}

        class Ctx:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def login(self, u, p): captured["login"] = (u, p)
            def send_message(self, msg): captured["msg"] = msg

        with mock.patch.object(nc.smtplib, "SMTP_SSL", return_value=Ctx()) as ctor:
            r = nc.send_email("a@b.com|c@d.com", "标题", "正文",
                              smtp_host="smtp.example.com", smtp_user="u", smtp_password="p")
        self.assertTrue(r["ok"])
        self.assertEqual(captured["login"], ("u", "p"))
        self.assertEqual(captured["msg"]["Subject"], "标题")
        self.assertEqual(captured["msg"]["To"], "a@b.com, c@d.com")
        ctor.assert_called_once()

    def test_email_auth_failure_swallowed(self):
        class Ctx:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def login(self, u, p): raise RuntimeError("535 auth failed")
        with mock.patch.object(nc.smtplib, "SMTP_SSL", return_value=Ctx()):
            r = nc.send_email("a@b.com", "t", "b", smtp_host="h", smtp_user="u", smtp_password="bad")
        self.assertFalse(r["ok"])
        self.assertIn("auth failed", r["error"])

    def test_email_timeout_swallowed(self):
        with mock.patch.object(nc.smtplib, "SMTP_SSL", side_effect=TimeoutError("timed out")):
            r = nc.send_email("a@b.com", "t", "b", smtp_host="h")
        self.assertFalse(r["ok"])
        self.assertIn("timed out", r["error"])


class FanoutTest(unittest.TestCase):
    def test_new_channels_off_by_default(self):
        tool = NotifyTool()
        results = tool.send_to_enabled({}, "t", "b")
        self.assertEqual(results, [], "默认设置不应发给任何渠道")

    def test_fanout_reads_settings_and_skips_empty_recipient(self):
        tool = NotifyTool()
        settings = {
            "feishu_enabled": True, "feishu_webhook_url": "https://fs",
            "wecom_enabled": True, "wecom_webhook_url": "",
        }
        with mock.patch.object(tool, "send", return_value={"ok": True}) as send:
            results = tool.send_to_enabled(settings, "标题", "正文")
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"], "空地址应结构化失败")
        self.assertEqual(send.call_count, 1, "空地址不应真正发起请求")

    def test_fanout_email_uses_recipients_key(self):
        tool = NotifyTool()
        settings = {
            "email_enabled": True, "email_recipients": "a@b.com",
            "smtp_host": "smtp.x.com", "smtp_user": "u", "smtp_password": "p",
        }
        with mock.patch.object(tool, "send", return_value={"ok": True}) as send:
            results = tool.send_to_enabled(settings, "t", "b")
        self.assertTrue(results[0]["ok"])
        self.assertEqual(send.call_args.kwargs.get("smtp_host"), "smtp.x.com")


class AlertServiceFanoutTest(unittest.TestCase):
    """验证 AlertService._fanout_channels 复用 alert_log、跳过钉钉、默认零副作用。"""

    def _service(self):
        from app.services.alert_service import AlertService
        svc = AlertService.__new__(AlertService)
        svc.host = SimpleNamespace(_notify_tool=NotifyTool(), _uid_safe=lambda: 1)
        logged = []
        svc._log_alert = lambda *a, **k: logged.append(a)
        return svc, logged

    def test_fanout_skips_dingtalk_and_logs_each_channel(self):
        svc, logged = self._service()
        settings = {"feishu_enabled": True, "feishu_webhook_url": "https://fs"}
        results = []
        with mock.patch.object(svc.host._notify_tool, "send_to_enabled", return_value=[
            {"channel": "dingtalk", "ok": True},
            {"channel": "feishu", "ok": True},
        ]):
            any_ok = svc._fanout_channels("标题", "正文", 2, "auto", settings, results)
        self.assertTrue(any_ok)
        self.assertEqual(len(results), 1, "钉钉由主链路负责，不能重复记")
        self.assertEqual(results[0]["channel"], "feishu")
        self.assertEqual(len(logged), 1, "新渠道必须写 alert_log")

    def test_fanout_failure_does_not_break(self):
        svc, logged = self._service()
        with mock.patch.object(svc.host._notify_tool, "send_to_enabled", side_effect=RuntimeError("boom")):
            any_ok = svc._fanout_channels("t", "b", 1, "auto", {}, [])
        self.assertFalse(any_ok)
        self.assertEqual(logged, [])

    def test_no_channels_enabled_is_noop(self):
        svc, logged = self._service()
        with mock.patch.object(svc.host._notify_tool, "send_to_enabled", return_value=[]):
            any_ok = svc._fanout_channels("t", "b", 1, "auto", {}, [])
        self.assertFalse(any_ok)


if __name__ == "__main__":
    unittest.main()
