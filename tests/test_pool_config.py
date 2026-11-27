"""四池占比可编辑：校验规则 + 落盘热生效。

覆盖三类容易踩的组合约束：
- 未知字段/未知池直接拒（防手误写坏配置）
- min ≤ target ≤ max，且单票上限 × 持仓数 ≥ 区间下限
- 四池目标 + 现金目标 = 100%
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.core import decision_rules
from app.services import allocation_service


REAL_RULES = Path(__file__).resolve().parent.parent / "config" / "decision_rules.json"


class PoolConfigTestBase(unittest.TestCase):
    """每个用例用独立的规则文件副本，绝不碰生产配置。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="rules_")
        self._path = Path(self._tmpdir) / "decision_rules.json"
        shutil.copy(REAL_RULES, self._path)
        self._old_env = os.environ.get("MONITOR_STOCK_RULES_FILE")
        os.environ["MONITOR_STOCK_RULES_FILE"] = str(self._path)
        # 强制重载（路径变了，get_rules 会自己失效，这里显式刷新以稳妥）
        decision_rules.get_rules()

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("MONITOR_STOCK_RULES_FILE", None)
        else:
            os.environ["MONITOR_STOCK_RULES_FILE"] = self._old_env
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        decision_rules.get_rules()


class TestValidatePoolConfig(PoolConfigTestBase):
    def test_legal_change_passes(self):
        # 股息 20→19、低估 22→23，现金 25 不变，合计仍 100
        result = allocation_service.validate_pool_config(
            {"dividend": {"target_pct": 19}, "value": {"target_pct": 23}}
        )
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["pools"]["dividend"]["target_pct"], 19.0)
        self.assertEqual(result["pools"]["value"]["target_pct"], 23.0)

    def test_total_must_equal_100(self):
        result = allocation_service.validate_pool_config({"dividend": {"target_pct": 30}})
        self.assertFalse(result["ok"])
        self.assertTrue(any("100%" in i for i in result["issues"]), result["issues"])

    def test_cash_change_compensates_total(self):
        # 股息 +5 → 合计 105；现金 -5 拉回 100 应通过
        result = allocation_service.validate_pool_config(
            {"dividend": {"target_pct": 25, "max_pct": 26}},
            {"target_pct": 20, "min_pct": 10, "max_pct": 30},
        )
        self.assertTrue(result["ok"], result["issues"])

    def test_target_outside_band_rejected(self):
        # 目标 25 高于股息区间上限 22
        result = allocation_service.validate_pool_config({"dividend": {"target_pct": 25}})
        self.assertFalse(result["ok"])
        self.assertTrue(any("高于区间上限" in i for i in result["issues"]), result["issues"])

    def test_min_above_target_rejected(self):
        result = allocation_service.validate_pool_config({"dividend": {"min_pct": 21}})
        self.assertFalse(result["ok"])
        self.assertTrue(any("高于目标" in i for i in result["issues"]), result["issues"])

    def test_unknown_field_rejected(self):
        result = allocation_service.validate_pool_config({"dividend": {"stop_loss_pct": -10}})
        self.assertFalse(result["ok"])
        self.assertTrue(any("不支持修改字段" in i for i in result["issues"]), result["issues"])

    def test_unknown_pool_rejected(self):
        result = allocation_service.validate_pool_config({"growth": {"target_pct": 10}})
        self.assertFalse(result["ok"])
        self.assertTrue(any("未知池" in i for i in result["issues"]), result["issues"])

    def test_infeasible_capacity_rejected(self):
        # 单票 3% × 2 只 = 6% < 区间下限 18%
        result = allocation_service.validate_pool_config({"dividend": {"max_single_pct": 3}})
        self.assertFalse(result["ok"])
        self.assertTrue(any("永远无法达标" in i for i in result["issues"]), result["issues"])

    def test_bad_types_rejected(self):
        for bad in ("abc", None, float("nan")):
            with self.subTest(value=bad):
                result = allocation_service.validate_pool_config({"dividend": {"target_pct": bad}})
                self.assertFalse(result["ok"])

    def test_merged_values_are_complete(self):
        """返回的 pools 应包含全部可编辑字段，router 只回写这些键。"""
        result = allocation_service.validate_pool_config({"dividend": {"target_pct": 19}})
        for field in allocation_service.POOL_EDITABLE_FIELDS:
            self.assertIn(field, result["pools"]["dividend"])
        for field in allocation_service.CASH_EDITABLE_FIELDS:
            self.assertIn(field, result["cash"])


class TestSavePoolConfig(PoolConfigTestBase):
    def test_save_persists_and_hot_reloads(self):
        before = decision_rules.pools()["dividend"]["target_pct"]
        self.assertAlmostEqual(before, 20.0)

        allocation_service.save_pool_config(
            {"dividend": {"target_pct": 19}, "value": {"target_pct": 23}}
        )

        # 不重启、不手动 reload，直接读到的就是新值（mtime 热更新）
        self.assertAlmostEqual(decision_rules.pools()["dividend"]["target_pct"], 19.0)
        saved = json.loads(self._path.read_text(encoding="utf-8"))
        self.assertAlmostEqual(saved["pools"]["dividend"]["target_pct"], 19.0)

    def test_save_keeps_other_sections(self):
        original = json.loads(self._path.read_text(encoding="utf-8"))
        allocation_service.save_pool_config(
            {"dividend": {"target_pct": 19}, "value": {"target_pct": 23}}
        )
        saved = json.loads(self._path.read_text(encoding="utf-8"))
        self.assertEqual(saved["fees"], original["fees"])
        self.assertEqual(saved["risk"], original["risk"])
        self.assertEqual(saved["pools"]["catalyst"], original["pools"]["catalyst"])

    def test_save_creates_backup(self):
        allocation_service.save_pool_config(
            {"dividend": {"target_pct": 19}, "value": {"target_pct": 23}}
        )
        bak = self._path.with_suffix(self._path.suffix + ".bak")
        self.assertTrue(bak.exists(), "写入前应留一份 .bak 便于回滚")
        self.assertAlmostEqual(
            json.loads(bak.read_text(encoding="utf-8"))["pools"]["dividend"]["target_pct"], 20.0
        )

    def test_save_does_not_inject_defaults(self):
        """只写改动过的键，不能把 DEFAULT_RULES 兜底固化进文件。"""
        allocation_service.save_pool_config({"dividend": {"target_pct": 19}, "value": {"target_pct": 23}})
        saved = json.loads(self._path.read_text(encoding="utf-8"))
        self.assertNotIn("_cached", saved)
        # 原本没有的顶层键不应该冒出来
        original_keys = set(json.loads(REAL_RULES.read_text(encoding="utf-8")).keys())
        self.assertEqual(set(saved.keys()), original_keys)

    def test_invalid_save_raises_and_leaves_file_untouched(self):
        original_text = self._path.read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            allocation_service.save_pool_config({"dividend": {"target_pct": 30}})
        self.assertEqual(self._path.read_text(encoding="utf-8"), original_text)

    def test_save_recomputes_allocation_with_new_targets(self):
        """改目标后，同一批持仓的偏离应立即按新目标算。"""
        holdings = [{"secID": "600000.SH", "shares": 1000, "cost": 10, "price": 10, "pool": "dividend"}]
        before = allocation_service.compute_allocation(holdings, 100000.0)
        # 股息 20→19、低估 22→23，合计仍 75%，现金 25% 不变
        allocation_service.save_pool_config(
            {"dividend": {"target_pct": 19}, "value": {"target_pct": 23}}
        )
        after = allocation_service.compute_allocation(holdings, 100000.0)
        b = next(p for p in before["pools"] if p["pool"] == "dividend")
        a = next(p for p in after["pools"] if p["pool"] == "dividend")
        self.assertEqual(b["target_pct"], 20.0)
        self.assertEqual(a["target_pct"], 19.0)
        self.assertNotAlmostEqual(b["diff_pp"], a["diff_pp"])


if __name__ == "__main__":
    unittest.main()
