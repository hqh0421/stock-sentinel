"""因子有效性纪律与组合级风控。"""

import unittest

from tests import assert_isolated


def _bars(n=160, pattern="flat"):
    rows = []
    for i in range(n):
        if pattern == "uptrend":
            close = 10 + i * 0.12
        elif pattern == "reversal":
            close = 10 + (i * 0.1 if i < 110 else 11 - (i - 110) * 0.1)
        else:
            close = 10 + (i % 5) * 0.02
        rows.append({
            "date": f"d{i:03d}", "close": close, "open": close,
            "high": close + 0.3, "low": close - 0.3,
            "FLAG": 10 if i % 5 == 0 else 0,
        })
    return rows


class TestFactorValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_isolated()

    def test_insufficient_data(self):
        from app.services.factor_validation_service import validate_factor

        r = validate_factor(_bars(20), "FLAG > 5")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "insufficient_data")

    def test_never_triggered_is_not_effective(self):
        from app.services.factor_validation_service import validate_factor

        r = validate_factor(_bars(), "close > 999999")
        self.assertTrue(r["ok"])
        self.assertIn("证据不足", r["verdict"])
        self.assertTrue(r["warnings"])

    def test_beta_is_not_mistaken_for_alpha(self):
        from app.services.factor_validation_service import validate_factor

        # 整体单边上涨，触发与不触发都赚钱，区分度极小 → 不能判「有效」
        r = validate_factor(_bars(pattern="uptrend"), "FLAG > 5", horizon_days=10)
        self.assertTrue(r["ok"])
        self.assertIn("区分度不足", r["verdict"])
        self.assertLess(r["metrics"]["layer_edge_pct"], 0.5)

    def test_overfit_detected_by_walk_forward(self):
        from app.services.factor_validation_service import validate_factor

        r = validate_factor(_bars(pattern="reversal"), "FLAG > 5", horizon_days=10)
        wf = r.get("walk_forward", {})
        self.assertTrue(r["ok"])
        self.assertGreater(wf.get("in_sample_return_pct") or 0, 0)
        self.assertLess(wf.get("out_sample_return_pct") or 0, 0)
        self.assertEqual(r["verdict"], "疑似过拟合")

    def test_empty_expression_rejected(self):
        from app.services.factor_validation_service import validate_factor

        r = validate_factor(_bars(), "")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "empty_entry_expr")


class TestRiskGuard(unittest.TestCase):
    def test_relative_position_bounds(self):
        from app.services import risk_guard_service as rg

        bars = [{"close": 10 + i * 0.1} for i in range(260)]
        p = rg.relative_position(bars, window=60, ma_long=200)
        self.assertTrue(p["available"])
        self.assertGreaterEqual(p["range_position"], 0.0)
        self.assertLessEqual(p["range_position"], 1.0)
        self.assertIn(p["label"], ("区间低位", "区间中部", "区间高位"))

    def test_vol_target_reduces_position_when_volatile(self):
        from app.services import risk_guard_service as rg

        calm = rg.realized_volatility([{"close": 10 + i * 0.01} for i in range(60)])
        wild = rg.realized_volatility([{"close": 10 + (5 if i % 2 else -5)} for i in range(60)])
        self.assertGreater(wild, calm)
        self.assertLess(rg.vol_target_position(wild)["suggested_position_pct"],
                        rg.vol_target_position(calm)["suggested_position_pct"])

    def test_drawdown_guard_breaches(self):
        from app.services import risk_guard_service as rg

        r = rg.portfolio_drawdown_guard([100, 105, 110, 95, 88, 92, 100])
        self.assertTrue(r["breached"])
        self.assertIn("暂停新增买入", r["action"])

    def test_concentration_flags_single_and_industry(self):
        from app.services import risk_guard_service as rg

        r = rg.concentration([
            {"name": "A", "market_value": 60, "industry": "半导体"},
            {"name": "B", "market_value": 30, "industry": "半导体"},
            {"name": "C", "market_value": 10, "industry": "银行"},
        ])
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["breaches"]), 2)

    def test_assess_aggregates_all(self):
        from app.services import risk_guard_service as rg

        r = rg.assess([{"close": 10 + i * 0.1} for i in range(60)],
                      equity=[100, 110, 99], positions=[])
        for key in ("relative_position", "vol_target", "drawdown_guard", "concentration", "rules"):
            self.assertIn(key, r)


if __name__ == "__main__":
    unittest.main()
