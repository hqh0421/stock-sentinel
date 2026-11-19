"""四池止盈阶梯 + 时间止损 → 可执行的减仓指令。

以前 take_profit_plan / time_stop_state 只有测试在调，规则停在配置里；
现在通过 _pool_exit_plan 接进卖出计划，这里锁死优先级与边界。
"""
import unittest

from app.services.sell_target_service import _format_pool_exit, _pool_exit_plan


class TestPoolExitTakeProfit(unittest.TestCase):
    def test_catalyst_hits_first_tier(self):
        """催化 +20% 达到 T1(+18%)，卖 50%，1000 股 → 500 股。"""
        plan = _pool_exit_plan("catalyst", 20.0, 1000, 10.0, True)
        self.assertTrue(plan["applied"])
        self.assertEqual(len(plan["orders"]), 1)
        order = plan["orders"][0]
        self.assertEqual(order["action"], "reduce")
        self.assertAlmostEqual(order["sell_pct"], 50.0)
        self.assertEqual(order["shares"], 500)
        self.assertAlmostEqual(order["amount"], 5000.0)

    def test_catalyst_hits_second_tier_sells_all(self):
        plan = _pool_exit_plan("catalyst", 35.0, 1000, 10.0, True)
        self.assertEqual(len(plan["orders"]), 1)
        self.assertAlmostEqual(plan["orders"][0]["sell_pct"], 100.0)
        self.assertEqual(plan["orders"][0]["shares"], 1000)

    def test_below_tier_gives_distance_hint(self):
        plan = _pool_exit_plan("catalyst", 5.0, 1000, 10.0, True)
        self.assertEqual(plan["orders"], [])
        self.assertTrue(any("还差" in w for w in plan["warnings"]), plan["warnings"])

    def test_round_lot_never_oversells(self):
        """150 股按 50% 减，应取整到 0 手（宁可少卖，不报错单）。"""
        plan = _pool_exit_plan("catalyst", 20.0, 150, 10.0, True)
        self.assertEqual(plan["orders"], [])

    def test_recover_principal_uses_ratio(self):
        """长赛道 +50% 档是「卖 2/3 收回本金」，比例由公式算，不是写死。"""
        plan = _pool_exit_plan("long_track", 60.0, 1000, 10.0, True)
        self.assertEqual(len(plan["orders"]), 1)
        self.assertGreater(plan["orders"][0]["sell_pct"], 0)
        self.assertLessEqual(plan["orders"][0]["sell_pct"], 100)


class TestPoolExitTimeStop(unittest.TestCase):
    def test_hard_stop_weeks_clears(self):
        """催化满 8 周无条件清仓，优先级高于止盈。"""
        plan = _pool_exit_plan("catalyst", 20.0, 1000, 10.0, True, weeks_held=8.5)
        self.assertEqual(len(plan["orders"]), 1)
        self.assertEqual(plan["orders"][0]["action"], "clear")
        self.assertEqual(plan["orders"][0]["shares"], 1000)

    def test_review_weeks_halves_when_not_started(self):
        plan = _pool_exit_plan("catalyst", 0.0, 1000, 10.0, True, weeks_held=4.5)
        self.assertEqual(len(plan["orders"]), 1)
        self.assertEqual(plan["orders"][0]["action"], "reduce")
        self.assertAlmostEqual(plan["orders"][0]["sell_pct"], 50.0)

    def test_review_weeks_holds_when_started(self):
        """满观察周数但催化已启动 → 不减，避免砍掉正在走的行情。"""
        plan = _pool_exit_plan("catalyst", 8.0, 1000, 10.0, True,
                               weeks_held=4.5, has_started=True)
        self.assertEqual(plan["orders"], [])

    def test_no_buy_date_skips_time_stop(self):
        """没记买入日期就不判时间止损，不能按 0 周瞎判。"""
        plan = _pool_exit_plan("catalyst", 20.0, 1000, 10.0, True, weeks_held=None)
        self.assertIsNone(plan["time_stop"])
        # 仍然按止盈档给指令
        self.assertEqual(len(plan["orders"]), 1)

    def test_pool_without_time_rules_ignores_weeks(self):
        plan = _pool_exit_plan("dividend", 1.0, 1000, 10.0, True, weeks_held=50.0)
        self.assertFalse(plan["time_stop"]["applies"])
        self.assertEqual(plan["orders"], [])


class TestPoolExitGuards(unittest.TestCase):
    def test_unclassified_stock_skips(self):
        plan = _pool_exit_plan(None, 30.0, 1000, 10.0, True)
        self.assertFalse(plan["applied"])
        self.assertEqual(plan["orders"], [])
        self.assertTrue(plan["reason"])

    def test_no_holding_warns_but_keeps_ratio(self):
        plan = _pool_exit_plan("catalyst", 20.0, 0, 10.0, False)
        self.assertTrue(any("未记录持股数" in w for w in plan["warnings"]), plan["warnings"])

    def test_format_text_is_actionable(self):
        plan = _pool_exit_plan("catalyst", 20.0, 1000, 10.0, True)
        text = _format_pool_exit(plan)
        self.assertIn("减仓", text)
        self.assertIn("500 股", text)

    def test_format_without_orders(self):
        plan = _pool_exit_plan("catalyst", 1.0, 1000, 10.0, True)
        self.assertIn("暂不触发", _format_pool_exit(plan))


if __name__ == "__main__":
    unittest.main()
