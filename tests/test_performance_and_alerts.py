"""补齐对标能力的两处回归：资金加权收益（XIRR）与四池偏离告警。

对标依据：Portfolio Performance / Ghostfolio 的资金加权收益，Passiv 的 drift 通知。
两处都只补「确实缺的那一块」：
- 绩效模块已有时间加权净值曲线，缺 XIRR；不新建模块，加纯函数并接进 summary。
- 告警模块已有钉钉通道与冷却，缺四池口径；不新建通道，加判定源，默认关闭。
"""
from __future__ import annotations

import datetime
import unittest
from types import SimpleNamespace
from unittest import mock

from app.services import alert_service
from app.services.alert_service import AlertService
from app.services.performance_service import _merge_flows, money_weighted, xirr


def d(text: str) -> datetime.date:
    return datetime.date.fromisoformat(text)


class TestXirr(unittest.TestCase):
    def test_one_year_ten_percent(self):
        flows = [(d("2025-01-01"), -1000.0), (d("2026-01-01"), 1100.0)]
        self.assertAlmostEqual(xirr(flows), 10.0, places=1)

    def test_two_year_double(self):
        flows = [(d("2024-01-01"), -1000.0), (d("2026-01-01"), 2000.0)]
        # 731 天翻倍：2^(365/731)-1 ≈ 41.4%
        self.assertAlmostEqual(xirr(flows), 41.4, places=1)

    def test_negative_when_losing(self):
        flows = [(d("2025-01-01"), -1000.0), (d("2026-01-01"), 800.0)]
        self.assertLess(xirr(flows), 0)

    def test_all_same_sign_returns_none(self):
        self.assertIsNone(xirr([(d("2025-01-01"), -1000.0), (d("2026-01-01"), -500.0)]))

    def test_same_day_returns_none(self):
        self.assertIsNone(xirr([(d("2025-01-01"), -1000.0), (d("2025-01-01"), 1100.0)]))

    def test_single_flow_returns_none(self):
        self.assertIsNone(xirr([(d("2025-01-01"), -1000.0)]))
        self.assertIsNone(xirr([]))


class TestMoneyWeighted(unittest.TestCase):
    def test_buy_then_terminal_value(self):
        trades = [
            {"side": "buy", "trade_date": "2025-01-01", "amount": 1000.0, "fee": 0.0},
            {"side": "buy", "trade_date": "2025-07-01", "amount": 1000.0, "fee": 0.0},
        ]
        value = money_weighted(trades, 2200.0, d("2026-01-01"))
        self.assertIsNotNone(value)
        # 两笔各持有一年/半年，终值 2200，年化必然为正且低于 20%
        self.assertGreater(value, 0)
        self.assertLess(value, 20)

    def test_sell_is_positive_flow(self):
        trades = [
            {"side": "buy", "trade_date": "2025-01-01", "amount": 1000.0, "fee": 0.0},
            {"side": "sell", "trade_date": "2026-01-01", "amount": 1200.0, "fee": 0.0},
        ]
        value = money_weighted(trades, 0.0, d("2026-01-01"))
        self.assertAlmostEqual(value, 20.0, places=1)

    def test_no_trades_returns_none(self):
        self.assertIsNone(money_weighted([], 10000.0, d("2026-01-01")))

    def test_buy_without_terminal_is_unsolvable(self):
        trades = [{"side": "buy", "trade_date": "2025-01-01", "amount": 1000.0, "fee": 0.0}]
        self.assertIsNone(money_weighted(trades, 0.0, d("2026-01-01")))

    def test_adjust_records_do_not_move_cash(self):
        trades = [
            {"side": "buy", "trade_date": "2025-01-01", "amount": 1000.0, "fee": 0.0},
            {"side": "adjust", "trade_date": "2025-06-01", "amount": 0.0, "fee": 0.0},
        ]
        self.assertIsNotNone(money_weighted(trades, 1100.0, d("2026-01-01")))


class TestMergeFlows(unittest.TestCase):
    def test_same_day_is_merged(self):
        merged = _merge_flows([("2025-01-01", -100.0), ("2025-01-01", -50.0),
                               ("2025-02-01", 200.0)])
        self.assertEqual(merged[0], (d("2025-01-01"), -150.0))
        self.assertEqual(len(merged), 2)

    def test_bad_date_and_zero_are_dropped(self):
        self.assertEqual(_merge_flows([("", -100.0), ("not-a-date", 50.0),
                                       ("2025-01-01", 0.0)]), [])


class FakeHost:
    """只实现四池告警会用到的方法。"""

    def __init__(self, holdings, total=100000.0):
        self._holdings = holdings
        self._total = total
        self._alert_last_sent = {}
        self.sent = []
        self._notify_tool = SimpleNamespace(send_dingtalk_markdown=self._send)

    def _send(self, url, title, body, secret=""):
        self.sent.append({"title": title, "body": body})
        return {"ok": True}

    def get_favorites_list(self, include_quotes=False):
        return [dict(x) for x in self._holdings]

    def get_user_settings(self):
        return {"total_capital": self._total}


class TestPoolDriftAlert(unittest.TestCase):
    def _service(self, holdings, total=100000.0, enabled=True, cooldown=14400):
        svc = AlertService.__new__(AlertService)
        svc.host = FakeHost(holdings, total)
        svc._log_alert = lambda *a, **k: None

        def rules_get(key, default=None):
            if key == "monitor.pool_drift_alert_enabled":
                return enabled
            if key == "monitor.pool_drift_cooldown_sec":
                return cooldown
            return default

        patcher = mock.patch.object(alert_service, "decision_rules",
                                    SimpleNamespace(get=rules_get))
        patcher.start()
        self.addCleanup(patcher.stop)
        return svc

    def _over_band(self):
        # 总资金 10 万，股息池 2600 股 @10 = 2.6 万 → 26%，目标 20%，偏离 +6pp > 3pp
        return [{"secID": "600000.SH", "name": "测试A", "shares": 2600,
                 "cost": 10, "pool": "dividend", "price": 10}]

    def test_disabled_by_default_does_not_send(self):
        svc = self._service(self._over_band(), enabled=False)
        result = svc._notify_pool_drift("https://hook", "", "auto", False)
        self.assertEqual(result["reason"], "disabled")
        self.assertEqual(svc.host.sent, [])

    def test_sends_when_over_band(self):
        svc = self._service(self._over_band())
        result = svc._notify_pool_drift("https://hook", "", "auto", False)
        self.assertTrue(result["sent"], result)
        self.assertEqual(len(svc.host.sent), 1)
        body = svc.host.sent[0]["body"]
        self.assertIn("股息", body)
        self.assertIn("不跨池挪用", body)
        self.assertIn("两道确认", body)

    def test_cooldown_blocks_repeat(self):
        svc = self._service(self._over_band())
        first = svc._notify_pool_drift("https://hook", "", "auto", False)
        second = svc._notify_pool_drift("https://hook", "", "auto", False)
        self.assertTrue(first["sent"])
        self.assertEqual(second["reason"], "cooldown")
        self.assertEqual(len(svc.host.sent), 1)

    def test_force_send_ignores_cooldown(self):
        svc = self._service(self._over_band())
        svc._notify_pool_drift("https://hook", "", "auto", False)
        again = svc._notify_pool_drift("https://hook", "", "auto", True)
        self.assertTrue(again["sent"])
        self.assertEqual(len(svc.host.sent), 2)

    def test_in_band_does_not_send(self):
        """四池都贴目标（20/18/15/22 + 现金 25）时不该推。空池也算低配，所以要全配齐。"""
        holdings = [
            {"secID": "600000.SH", "shares": 2000, "cost": 10, "pool": "dividend", "price": 10},
            {"secID": "600001.SH", "shares": 1800, "cost": 10, "pool": "catalyst", "price": 10},
            {"secID": "600002.SH", "shares": 1500, "cost": 10, "pool": "long_track", "price": 10},
            {"secID": "600003.SH", "shares": 2200, "cost": 10, "pool": "value", "price": 10},
        ]
        svc = self._service(holdings)
        result = svc._notify_pool_drift("https://hook", "", "auto", False)
        self.assertEqual(result["reason"], "in_band")
        self.assertEqual(svc.host.sent, [])

    def test_missing_total_capital(self):
        svc = self._service(self._over_band(), total=0.0)
        result = svc._notify_pool_drift("https://hook", "", "auto", False)
        self.assertEqual(result["reason"], "no_total_capital")

    def test_missing_webhook(self):
        svc = self._service(self._over_band())
        result = svc._notify_pool_drift("", "", "auto", False)
        self.assertEqual(result["reason"], "missing_webhook")


class TestSwitchWhitelist(unittest.TestCase):
    def test_pool_drift_switch_is_whitelisted(self):
        from app.services.risk_guard_service import MONITOR_SWITCHES

        self.assertIn("pool_drift_alert_enabled", MONITOR_SWITCHES)


if __name__ == "__main__":
    unittest.main()
