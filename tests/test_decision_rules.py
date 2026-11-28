"""决策规则单一事实源：费率口径一致、阈值收敛、规则热更新。"""

import json
import unittest

from app.core import decision_rules as dr
from app.core import secrets
from tests import TEST_ROOT, assert_isolated


class TestFeeAlignment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_isolated()

    def test_backtest_and_ledger_share_one_fee_implementation(self):
        from app.services.trade_ledger import calc_fee

        for side in ("buy", "sell"):
            for amount in (0.0, 5000.0, 100000.0, 1234567.89):
                self.assertEqual(calc_fee(side, amount), dr.calc_trade_fee(side, amount))

    def test_stamp_only_on_sell(self):
        buy = dr.calc_trade_fee("buy", 100000.0)
        sell = dr.calc_trade_fee("sell", 100000.0)
        self.assertEqual(buy["stamp"], 0.0)
        self.assertGreater(sell["stamp"], 0.0)
        self.assertGreater(sell["total"], buy["total"])

    def test_minimum_commission_applies(self):
        small = dr.calc_trade_fee("buy", 100.0)
        self.assertEqual(small["commission"], dr.fee_rules().get("commission_min"))

    def test_user_supplied_fee_not_stacked(self):
        self.assertEqual(dr.calc_trade_fee("sell", 100000.0, fee=9.9)["total"], 9.9)

    def test_effective_rate_not_the_old_hardcoded_value(self):
        # 旧口径是拍脑袋的 0.00075，现在必须由台账口径推导
        self.assertNotEqual(round(dr.effective_fee_rate(), 5), 0.00075)


class TestRuleConvergence(unittest.TestCase):
    def test_regime_table_comes_from_rules(self):
        from app.services.sentiment_position_service import _REGIME_TABLE, _STYLE_TILT

        self.assertEqual(_REGIME_TABLE, dr.regime_table())
        self.assertEqual(_STYLE_TILT, dr.style_tilt())
        self.assertIn("euphoria", _REGIME_TABLE)
        self.assertFalse(_REGIME_TABLE["euphoria"]["allow_new_buy"])

    def test_sell_style_matches_rules_and_keeps_tuple(self):
        from app.services.sell_target_service import _STYLE_PARAMS

        self.assertEqual(_STYLE_PARAMS, dr.sell_style_params())
        self.assertIsInstance(_STYLE_PARAMS["long"]["atr_targets"], tuple)

    def test_scheduler_constants_come_from_rules(self):
        from app.services.scheduler import TICK_SECONDS, DEFAULT_INTERVAL_SEC

        self.assertEqual(TICK_SECONDS, int(dr.get("scheduler.tick_seconds")))
        self.assertEqual(DEFAULT_INTERVAL_SEC, int(dr.get("scheduler.default_interval_sec")))

    def test_rules_hot_reload(self):
        path = TEST_ROOT / "rules_hot.json"
        path.write_text(json.dumps({"risk": {"max_single_weight_pct": 42.0}}), encoding="utf-8")
        import os
        old = os.environ.get("MONITOR_STOCK_RULES_FILE")
        os.environ["MONITOR_STOCK_RULES_FILE"] = str(path)
        try:
            self.assertEqual(dr.get("risk.max_single_weight_pct"), 42.0)
            # 缺省字段仍由内置兜底补齐
            self.assertIsNotNone(dr.get("fees.commission_min"))
        finally:
            if old is None:
                os.environ.pop("MONITOR_STOCK_RULES_FILE", None)
            else:
                os.environ["MONITOR_STOCK_RULES_FILE"] = old
            dr.get_rules()


class TestSecrets(unittest.TestCase):
    def test_env_overlay_takes_priority(self):
        import os
        os.environ["MONITOR_STOCK_DINGTALK_SECRET"] = "env-value"
        try:
            out = secrets.overlay_secret_settings({"dingtalk_secret": "file-value"})
            self.assertEqual(out["dingtalk_secret"], "env-value")
        finally:
            os.environ.pop("MONITOR_STOCK_DINGTALK_SECRET", None)

    def test_sources_never_leak_values(self):
        report = secrets.secret_sources({"dingtalk_secret": "super-secret"})
        self.assertIn("dingtalk_secret", report)
        self.assertNotIn("super-secret", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
