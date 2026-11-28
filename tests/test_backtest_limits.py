"""G6 回测口径诚实化：涨跌停不成交 + 约束声明 + 无未来函数回归。

行情数据全部合成，不碰生产库、不发真实请求。
"""

from __future__ import annotations

import datetime
import unittest
from unittest import mock

from app.services import backtest_service as bt
from app.services.backtest_service import _at_limit, _limit_pct


def _series(prices, *, dates=None):
    """构造日线序列：open/close/high/low 都用给定价格（一字板）。"""
    out = []
    for i, p in enumerate(prices):
        d = dates[i] if dates else (datetime.date(2024, 1, 1) + datetime.timedelta(days=i)).isoformat()
        out.append({"date": d, "open": p, "close": p, "high": p, "low": p, "volume": 1000})
    return out


class LimitHelpersTest(unittest.TestCase):
    def test_board_limits(self):
        self.assertEqual(_limit_pct("600000.SH"), 10.0)
        self.assertEqual(_limit_pct("000001.SZ"), 10.0)
        self.assertEqual(_limit_pct("300750.SZ"), 20.0)
        self.assertEqual(_limit_pct("688981.SH"), 20.0)
        self.assertEqual(_limit_pct("920001.BJ"), 30.0)
        self.assertEqual(_limit_pct("unknown"), 10.0)

    def test_at_limit_up_and_down(self):
        self.assertTrue(_at_limit(11.0, 10.0, 10.0, "buy"))
        self.assertFalse(_at_limit(10.8, 10.0, 10.0, "buy"))
        self.assertTrue(_at_limit(9.0, 10.0, 10.0, "sell"))
        self.assertFalse(_at_limit(9.5, 10.0, 10.0, "sell"))

    def test_missing_preclose_never_blocks(self):
        self.assertFalse(_at_limit(11.0, None, 10.0, "buy"))
        self.assertFalse(_at_limit(0, 10.0, 10.0, "sell"))


class BacktestLimitFillTest(unittest.TestCase):
    def _run(self, prices, code="600000.SH"):
        fake = mock.Mock()
        fake.get_k_line_data.return_value = _series(prices)
        fake._normalize_stock_code.return_value = {"digits": "".join(c for c in code if c.isdigit()),
                                                    "market": code.split(".")[-1]}
        fake._favorite_cache = []
        with mock.patch.object(bt, "stock_service", fake, create=True), \
             mock.patch("app.services.stock_service.stock_service", fake):
            return bt.backtest_service.run(
                code=code, start_date="2024-01-01", end_date="2024-01-30",
                capital=100000.0, entry_expr="close > 10", exit_expr="close < 5",
                benchmark="none",
            )

    def test_one_word_limit_up_blocks_buy_and_counts(self):
        # 第 28 根 close=11 触发买入信号；第 29 根（最后一根）open=12.1
        # = 前收 11 × 1.10，正好涨停 → 买入信号被封板挡住，不能成交
        prices = [10.0] * 28 + [11.0] + [12.1]
        r = self._run(prices)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["constraints"]["blocked_buy_count"], 1)
        self.assertEqual(r["trades"], [], "涨停买入不应成交")
        self.assertIn("涨停不买入", r["constraints"]["limit_up_down"])

    def test_normal_open_fills_next_day(self):
        # 第 24 根 close=11 触发信号；第 25 根 open=11（相对前收 11 未封板）→ 次日成交
        prices = [10.0] * 24 + [11.0] + [11.0] * 6
        r = self._run(prices)
        self.assertTrue(r["ok"], r)
        buys = [t for t in r["trades"] if t["side"] == "buy"]
        self.assertEqual(len(buys), 1, "正常开盘应成交")
        self.assertEqual(buys[0]["date"], "2024-01-26", "T 日信号、T+1 开盘成交")
        self.assertEqual(r["constraints"]["blocked_buy_count"], 0)

    def test_constraints_declare_t1_and_adjustment(self):
        r = self._run([10.0] * 30)
        c = r["constraints"]
        self.assertTrue(c["t_plus_1"])
        self.assertIn("开盘", c["fill_at"])
        self.assertIn("前复权", c["adjustment"])
        self.assertTrue(any("ST" in w for w in r["warnings"]))

    def test_st_estimate_declared_for_gem(self):
        r = self._run([10.0] * 30, code="300750.SZ")
        self.assertIn("20%", r["constraints"]["limit_up_down"])


if __name__ == "__main__":
    unittest.main()
