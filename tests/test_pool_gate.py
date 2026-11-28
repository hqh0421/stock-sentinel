"""四池闸门：买入前按池规则拦截。

三道入口（手工记账 / 对话记账 / 建议单执行）都汇到 trade_ledger.apply_trade，
所以这里测的就是全链路。卖出永远放行——仓位只会变小，不存在越过上限的问题。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from app.core.secid import split_secid
from app.services.trade_ledger import TradeLedger


class FakeSvc:
    """只实现 trade_ledger 会用到的那几个方法。"""

    def __init__(self, favorites, total_capital=100000.0, cash=100000.0, prices=None):
        self._favorite_cache = [dict(f) for f in favorites]
        self._settings = {"total_capital": total_capital, "available_cash": cash}
        self._prices = prices or {}

    # --- 记账依赖 ---
    def _normalize_stock_code(self, code):
        return split_secid(code)

    def _get_cached_stock_name(self, digits):
        return f"测试{digits}"

    def get_stock_spot_info(self, sec_id):
        return {"price": self._prices.get(sec_id, 10.0)}

    def _cash_detail(self):
        return {"value": self._settings["available_cash"], "source": "manual"}

    def save_user_settings(self, patch):
        self._settings.update(patch or {})

    def add_favorite(self, sec_id, shares=None, cost=None, **kwargs):
        self._favorite_cache.append({"secID": sec_id, "shares": shares, "cost": cost,
                                     "pool": kwargs.get("pool")})

    def update_favorite_position(self, sec_id, **kwargs):
        for item in self._favorite_cache:
            if item.get("secID") == sec_id:
                for k, v in kwargs.items():
                    if v is not None:
                        item[k] = v

    # --- 闸门依赖 ---
    def get_favorites_list(self, include_quotes=False):
        return [dict(f) for f in self._favorite_cache]

    def get_user_settings(self):
        return dict(self._settings)

    def get_favorites(self):
        return [dict(f) for f in self._favorite_cache]


class PoolGateTestBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="ledger_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def make(self, favorites, total=100000.0, cash=100000.0):
        self.svc = FakeSvc(favorites, total_capital=total, cash=cash)
        return TradeLedger(config_dir=self._tmpdir, svc=self.svc)


class TestPoolGateBlocks(PoolGateTestBase):
    def test_single_stock_cap_blocks_buy(self):
        """股息池单票上限 10%，买到 29% 必须拦下。"""
        ledger = self.make([{"secID": "600000.SH", "shares": 2000, "cost": 10, "pool": "dividend"}])
        result = ledger.apply_trade("600000", "buy", 900, 10)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result.get("gate"), "pool_rules")
        self.assertTrue(any("单票" in i for i in result["issues"]), result["issues"])
        # 拦下时不能写台账
        self.assertEqual(ledger._load(), [])

    def test_max_positions_blocks_new_stock(self):
        """催化池最多 2 只；已归池但持股为 0 的票买进来会成为第 3 只，必须拦下。

        新建仓必须先归池才判池规则——所以这里用「已在自选、已选池、持股 0」
        的真实流程，而不是买一只完全陌生的票（那种情况池未知，只能给 warning）。
        """
        ledger = self.make([
            {"secID": "600000.SH", "shares": 100, "cost": 10, "pool": "catalyst"},
            {"secID": "000001.SZ", "shares": 100, "cost": 10, "pool": "catalyst"},
            {"secID": "300750.SZ", "shares": 0, "cost": 10, "pool": "catalyst"},
        ])
        result = ledger.apply_trade("300750", "buy", 100, 10)
        self.assertFalse(result["ok"], result)
        self.assertTrue(any("上限" in i and "只" in i for i in result["issues"]), result["issues"])

    def test_add_rules_block_extra_add(self):
        """股息池 max_adds=0，加仓应被拦（mode 不是 one_shot）。"""
        ledger = self.make([{"secID": "600000.SH", "shares": 500, "cost": 10, "pool": "dividend"}])
        result = ledger.apply_trade("600000", "buy", 100, 10)
        self.assertFalse(result["ok"], result)
        self.assertTrue(any("加仓" in i for i in result["issues"]), result["issues"])

    def test_cash_floor_blocks_buy(self):
        """买入后现金低于下限 10% 必须拦下。"""
        # 总资金 10 万，已持仓 8.5 万，现金 1.5 万；再买 1 万 → 现金 5% < 10%
        ledger = self.make(
            [{"secID": "600519.SH", "shares": 850, "cost": 100, "pool": "value"}],
            total=100000.0, cash=15000.0,
        )
        result = ledger.apply_trade("000001", "buy", 1000, 10)
        self.assertFalse(result["ok"], result)
        self.assertTrue(any("现金" in i for i in result["issues"]), result["issues"])


class TestPoolGateAllows(PoolGateTestBase):
    def test_first_buy_within_limits_passes(self):
        """低估池单票上限 11%，首次建仓 5% 应放行。"""
        ledger = self.make([])
        result = ledger.apply_trade("000001", "buy", 500, 10)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(ledger._load()), 1)

    def test_sell_always_allowed(self):
        """已超单票上限的持仓，卖出不该被拦。"""
        ledger = self.make([{"secID": "600000.SH", "shares": 2000, "cost": 10, "pool": "dividend"}])
        result = ledger.apply_trade("600000", "sell", 500, 10)
        self.assertTrue(result["ok"], result)

    def test_unclassified_pool_warns_but_allows(self):
        ledger = self.make([{"secID": "600000.SH", "shares": 100, "cost": 10, "pool": None}])
        result = ledger.apply_trade("600000", "buy", 100, 10)
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("未归入四池" in w for w in result["warnings"]), result["warnings"])

    def test_force_overrides_but_leaves_warning(self):
        ledger = self.make([{"secID": "600000.SH", "shares": 2000, "cost": 10, "pool": "dividend"}])
        result = ledger.apply_trade("600000", "buy", 900, 10, force=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("越过四池规则" in w for w in result["warnings"]), result["warnings"])
        self.assertEqual(len(ledger._load()), 1)

    def test_zero_total_capital_skips_gate(self):
        """总资金未设置时无从算占比，放行但提示，不能把用户彻底锁死。"""
        ledger = self.make([], total=0.0)
        result = ledger.apply_trade("000001", "buy", 500, 10)
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("总资金未设置" in w for w in result["warnings"]), result["warnings"])


if __name__ == "__main__":
    unittest.main()
