"""免打扰时段回归（PRD §12 G12）。

G5/G8 接入多渠道后，调度层只有按规则的冷却、没有按时间窗静默。
本测试锁死两件事：
1. 判定口径只有一处（跨午夜、非法值回退、终点不含）；
2. 监控告警与 Agent 定时推送都调用同一个判定函数，手动测试不受限制。
"""
from __future__ import annotations

import datetime
import sys
import unittest
from unittest import mock

from app.services.agent_schedule_service import AgentScheduleService
from app.services.alert_service import AlertService, in_quiet_hours, parse_hhmm, quiet_hours_window


def _settings(start: str = "23:00", end: str = "07:00", enabled: bool = True) -> dict:
    return {
        "quiet_hours_enabled": enabled,
        "quiet_hours_start": start,
        "quiet_hours_end": end,
    }


class TestQuietHoursRule(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(in_quiet_hours({}))
        self.assertIsNone(quiet_hours_window({}))

    def test_cross_midnight_window(self):
        settings = _settings()
        self.assertTrue(in_quiet_hours(settings, datetime.datetime(2026, 11, 28, 23, 0)))
        self.assertTrue(in_quiet_hours(settings, datetime.datetime(2026, 11, 29, 6, 59)))
        self.assertFalse(in_quiet_hours(settings, datetime.datetime(2026, 11, 29, 7, 0)))
        self.assertFalse(in_quiet_hours(settings, datetime.datetime(2026, 11, 28, 22, 59)))

    def test_same_day_window_end_exclusive(self):
        settings = _settings("12:00", "13:00")
        self.assertTrue(in_quiet_hours(settings, datetime.datetime(2026, 11, 28, 12, 30)))
        self.assertFalse(in_quiet_hours(settings, datetime.datetime(2026, 11, 28, 13, 0)))
        self.assertFalse(in_quiet_hours(settings, datetime.datetime(2026, 11, 28, 11, 59)))

    def test_equal_start_end_is_noop(self):
        """不提供全天静默：误填成同一时刻时视为未配置。"""
        self.assertFalse(in_quiet_hours(_settings("09:00", "09:00")))

    def test_invalid_times_fall_back_to_defaults(self):
        self.assertEqual(parse_hhmm("25:99", "23:00"), "23:00")
        self.assertEqual(parse_hhmm("garbage", "07:00"), "07:00")
        self.assertEqual(parse_hhmm("9:5", "00:00"), "09:05")
        settings = _settings("bad", "also bad")
        self.assertEqual(quiet_hours_window(settings), (23 * 60, 7 * 60))
        self.assertTrue(in_quiet_hours(settings, datetime.datetime(2026, 11, 28, 23, 30)))


class _Host:
    """AlertService 在免打扰判定之前只会用到这两个方法。"""

    def __init__(self, settings: dict):
        self._settings = settings
        self._alert_last_sent: dict = {}

    def get_user_settings(self):
        return dict(self._settings)

    def load_alert_rules_raw(self, defaults):
        return defaults


class TestAlertServiceQuiet(unittest.TestCase):
    def _service(self, settings):
        svc = AlertService.__new__(AlertService)
        svc.host = _Host(settings)
        return svc

    @mock.patch("app.services.alert_service.is_trading_now", return_value=True)
    def test_auto_alert_is_blocked_before_any_channel(self, _trading):
        settings = _settings("00:00", "23:59")
        result = self._service(settings).notify_monitor_results([], mode="auto")
        self.assertFalse(result.get("sent"))
        self.assertEqual(result.get("reason"), "quiet_hours")

    @mock.patch("app.services.alert_service.is_trading_now", return_value=True)
    def test_force_send_bypasses_quiet_hours(self, _trading):
        """显式测试绕过免打扰，但因为没有配置 webhook 会停在缺地址的既有分支。"""
        settings = _settings("00:00", "23:59")
        result = self._service(settings).notify_monitor_results([], force_send=True)
        self.assertNotEqual(result.get("reason"), "quiet_hours")
        self.assertEqual(result.get("reason"), "missing_webhook")


class TestAgentScheduleQuiet(unittest.TestCase):
    def _push(self, settings):
        # push() 内部才 import stock_service；用假模块顶替，避免宿主机缺 akshare。
        fake_stock = mock.Mock()
        fake_stock.stock_service.get_user_settings.return_value = settings
        with mock.patch("app.services.agent_schedule_service.set_current_user_id", return_value="t"), \
             mock.patch("app.services.agent_schedule_service.reset_current_user_id"), \
             mock.patch.dict(sys.modules, {"app.services.stock_service": fake_stock}), \
             mock.patch("app.services.agent_schedule_service.NotifyTool.send_dingtalk_markdown") as send:
            result = AgentScheduleService().push(1, "标题", "正文")
            return result, send

    def test_scheduled_report_is_blocked(self):
        settings = {**_settings("00:00", "23:59"),
                    "dingtalk_enabled": True,
                    "dingtalk_webhook_url": "https://example.invalid/hook"}
        result, send = self._push(settings)
        self.assertEqual(result.get("reason"), "quiet_hours")
        send.assert_not_called()

    def test_outside_window_still_sends(self):
        settings = {**_settings("00:00", "01:00", enabled=True),
                    "dingtalk_enabled": True,
                    "dingtalk_webhook_url": "https://example.invalid/hook"}
        quiet_now = datetime.datetime(2026, 11, 28, 12, 0)
        with mock.patch("app.services.alert_service.datetime.datetime") as dt_cls:
            dt_cls.now.return_value = quiet_now
            result, send = self._push(settings)
        self.assertTrue(result.get("ok"), msg=str(result))
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
