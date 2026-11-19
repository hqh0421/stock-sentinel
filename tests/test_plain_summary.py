"""说人话层：四池数字 → 一句现状 + 一个下一步动作。

小白友好的关键不是少功能，而是「打开就知道现在怎么样、接下来点哪里」。
这里锁死分支优先级与文案口径，避免前端再写一套判断。
"""
from __future__ import annotations

import unittest

from app.services.allocation_service import plain_summary

POOL_ZERO = [
    {"pool": "dividend", "label": "股息", "count": 0, "actual_pct": 0, "target_pct": 20,
     "diff_pp": -20, "status": "under", "max_positions": 5, "largest_single_pct": 0},
    {"pool": "catalyst", "label": "催化波段成长", "count": 0, "actual_pct": 0, "target_pct": 18,
     "diff_pp": -18, "status": "under", "max_positions": 3, "largest_single_pct": 0},
    {"pool": "long_track", "label": "长赛道产业成长", "count": 0, "actual_pct": 0, "target_pct": 15,
     "diff_pp": -15, "status": "under", "max_positions": 3, "largest_single_pct": 0},
    {"pool": "value", "label": "低估价值", "count": 0, "actual_pct": 0, "target_pct": 22,
     "diff_pp": -22, "status": "under", "max_positions": 5, "largest_single_pct": 0},
]


def alloc(**overrides):
    base = {
        "ok": True,
        "total_capital": 100000,
        "pools": [dict(x) for x in POOL_ZERO],
        "cash": {"actual_pct": 100, "target_pct": 25, "min_pct": 10, "status": "above_target"},
        "unclassified": {"count": 0, "pct": 0},
        "issues": [],
    }
    base.update(overrides)
    return base


def with_holding():
    pools = [dict(x) for x in POOL_ZERO]
    pools[0]["count"] = 1
    pools[0]["actual_pct"] = 20
    pools[0]["diff_pp"] = 0
    pools[0]["status"] = "in_band"
    return alloc(pools=pools, cash={"actual_pct": 80, "target_pct": 25,
                                   "min_pct": 10, "status": "above_target"})


class TestPlainSummary(unittest.TestCase):
    def test_missing_capital_points_to_settings(self):
        p = plain_summary({"ok": False, "error": "总资金未设置或为 0，无法计算占比"})
        self.assertEqual(p["level"], "setup")
        self.assertIn("本金", p["headline"])
        self.assertEqual(p["action"]["view"], "settings")

    def test_empty_portfolio_points_to_watchlist(self):
        p = plain_summary(alloc(), [])
        self.assertEqual(p["level"], "empty")
        self.assertEqual(p["action"]["view"], "watchlist")
        self.assertIn("还没有持仓", p["headline"])

    def test_unclassified_beats_drift(self):
        """有未分类时先让人分类：没分类就没法判断该不该加减仓。"""
        p = plain_summary(alloc(unclassified={"count": 2, "pct": 8}),
                          [{"label": "股息", "diff_pp": 9, "amount": 9000, "pool": "dividend"}])
        self.assertEqual(p["level"], "unclassified")
        self.assertIn("2 只", p["headline"])
        self.assertEqual(p["action"]["view"], "pools")

    def test_cash_below_floor_is_risk(self):
        base = with_holding()
        base["cash"] = {"actual_pct": 7, "target_pct": 25, "min_pct": 10, "status": "below_floor"}
        p = plain_summary(base, [])
        self.assertEqual(p["level"], "risk")
        self.assertIn("低于", p["headline"])
        self.assertIn("10%", p["headline"])

    def test_empty_beats_cash_because_no_holding_means_no_floor_issue(self):
        """没有持仓时先引导加票；这时现金 100%，谈下限没有意义。"""
        p = plain_summary(alloc(cash={"actual_pct": 5, "min_pct": 10, "status": "below_floor"}), [])
        self.assertEqual(p["level"], "empty")

    def test_drift_uses_worst_item_and_plain_words(self):
        plan = [
            {"label": "股息", "diff_pp": 2.1, "amount": 2100, "pool": "dividend"},
            {"label": "低估价值", "diff_pp": -6.2, "amount": 6200, "pool": "value"},
        ]
        p = plain_summary(with_holding(), plan)
        self.assertEqual(p["level"], "drift")
        self.assertIn("低估价值", p["headline"])
        self.assertIn("少了 6.2 个百分点", p["headline"])
        self.assertIn("6,200 元", p["headline"])
        self.assertNotIn("pp", p["headline"])

    def test_over_target_says_reduce(self):
        p = plain_summary(with_holding(),
                          [{"label": "股息", "diff_pp": 6.2, "amount": 6200, "pool": "dividend"}])
        self.assertIn("多了 6.2 个百分点", p["headline"])
        self.assertIn("减一点", p["detail"])

    def test_calm_when_no_plan(self):
        p = plain_summary(with_holding(), [])
        self.assertEqual(p["level"], "calm")
        self.assertIn("不用动手", p["headline"])

    def test_action_always_runnable(self):
        """每个分支都必须给出可跳转的动作，否则小白不知道下一步点哪。"""
        risk_base = with_holding()
        risk_base["cash"] = {"actual_pct": 5, "min_pct": 10, "status": "below_floor"}
        cases = [
            plain_summary({"ok": False, "error": "总资金未设置"}),
            plain_summary(alloc(), []),
            plain_summary(alloc(unclassified={"count": 1, "pct": 3}), []),
            plain_summary(risk_base, []),
            plain_summary(with_holding(), [{"label": "股息", "diff_pp": 5, "amount": 5000}]),
            plain_summary(with_holding(), []),
        ]
        for item in cases:
            self.assertTrue(item["action"]["label"])
            self.assertIn(item["action"]["view"], {"settings", "watchlist", "pools"})


if __name__ == "__main__":
    unittest.main()
