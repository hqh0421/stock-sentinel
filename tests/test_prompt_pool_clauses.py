"""四池口径提示词的闭环测试。

背景：曾经把「股息 20% / 催化 18% / 长赛道 15% / 低估 22% / 现金 25%」
硬编码进 BASE_RULES 12.1，界面上改了比例后 Agent 话术仍是旧值。
这里锁死：所有比例与阈值必须来自 decision_rules，改配置即改话术。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.agent import prompts
from app.core import decision_rules

_RULES_SOURCE = Path(__file__).resolve().parent.parent / "config" / "decision_rules.json"


class PoolClauseTestBase(unittest.TestCase):
    """每个用例都在一份临时规则文件上跑，绝不改生产配置。"""

    def setUp(self) -> None:
        self._orig_rules_file = os.environ.get("MONITOR_STOCK_RULES_FILE")
        self._tmpdir = tempfile.TemporaryDirectory(prefix="prompt_rules_")
        self._rules_path = Path(self._tmpdir.name) / "decision_rules.json"
        self._rules_path.write_text(_RULES_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
        os.environ["MONITOR_STOCK_RULES_FILE"] = str(self._rules_path)

    def tearDown(self) -> None:
        if self._orig_rules_file is None:
            os.environ.pop("MONITOR_STOCK_RULES_FILE", None)
        else:
            os.environ["MONITOR_STOCK_RULES_FILE"] = self._orig_rules_file
        self._tmpdir.cleanup()

    def patch_rules(self, patch: dict) -> None:
        decision_rules.update(patch, backup=False)


class TestPoolBrief(PoolClauseTestBase):
    def test_default_brief_comes_from_rules(self) -> None:
        brief = prompts.render_pool_brief()
        self.assertIn("股息 20%（18~22%）", brief)
        self.assertIn("催化波段成长 18%（15~21%）", brief)
        self.assertIn("长赛道产业成长 15%（12~18%）", brief)
        self.assertIn("低估价值 22%（19~25%）", brief)
        self.assertIn("现金 25%（下限 10%）", brief)

    def test_brief_follows_ratio_change(self) -> None:
        self.patch_rules({"pools": {"dividend": {
            "target_pct": 30.0, "min_pct": 28.0, "max_pct": 32.0,
        }}})
        brief = prompts.render_pool_brief()
        self.assertIn("股息 30%（28~32%）", brief)
        self.assertNotIn("股息 20%", brief)

    def test_cash_floor_follows_rules(self) -> None:
        self.patch_rules({"cash": {"target_pct": 30.0, "min_pct": 15.0}})
        brief = prompts.render_pool_brief()
        self.assertIn("现金 30%（下限 15%）", brief)

    def test_full_prompt_follows_ratio_change(self) -> None:
        """端到端：组装后的系统提示词不能残留旧比例。"""
        self.patch_rules({"pools": {"value": {
            "target_pct": 28.0, "min_pct": 26.0, "max_pct": 30.0,
        }}})
        prompt = prompts.build_system_prompt("copilot", "tool-a")
        self.assertIn("低估价值 28%", prompt)
        self.assertNotIn("低估价值 22%", prompt)


class TestPoolDiscipline(PoolClauseTestBase):
    def test_default_discipline_comes_from_rules(self) -> None:
        text = prompts.render_pool_discipline()
        self.assertIn("-25% 复盘", text)
        self.assertIn("4 周无启动减半", text)
        self.assertIn("满 8 周无条件清仓", text)
        self.assertIn("-18% 止损", text)
        self.assertIn("+18% 卖 50%", text)
        self.assertIn("-30% 复盘", text)
        self.assertIn("一次性建仓且禁止加仓", text)
        self.assertIn("禁止给长赛道/低估兜底", text)

    def test_time_stop_and_stop_loss_follow_rules(self) -> None:
        self.patch_rules({"pools": {"catalyst": {
            "time_rules": {
                "review_weeks": 6, "hard_stop_weeks": 10,
                "review_action": "halve_if_no_start", "hard_stop_action": "clear",
            },
            "stop_loss_pct": -20.0,
        }}})
        text = prompts.render_pool_discipline()
        self.assertIn("6 周无启动减半", text)
        self.assertIn("满 10 周无条件清仓", text)
        self.assertIn("-20% 止损", text)
        self.assertNotIn("4 周无启动", text)
        self.assertNotIn("-18% 止损", text)

    def test_take_profit_ladder_follows_rules(self) -> None:
        self.patch_rules({"pools": {"catalyst": {"take_profit": [
            {"trigger_pct": 25.0, "sell_pct": 40, "note": "T1"},
            {"trigger_pct": 45.0, "sell_pct": 100, "note": "T2"},
        ]}}})
        text = prompts.render_pool_discipline()
        self.assertIn("+25% 卖 40%", text)
        self.assertIn("+45% 卖 100%", text)
        self.assertNotIn("+18% 卖 50%", text)

    def test_add_rule_follows_rules(self) -> None:
        self.patch_rules({"pools": {"long_track": {"add_rules": {
            "mode": "gradient_on_validated_pullback", "max_adds": 3,
            "require": "订单验证",
        }}}})
        text = prompts.render_pool_discipline()
        self.assertIn("只在订单验证的回调加仓", text)
        self.assertNotIn("只在财报/订单验证的回调加仓", text)

    def test_full_prompt_follows_discipline_change(self) -> None:
        self.patch_rules({"pools": {"dividend": {"review_dd_pct": -15.0}}})
        prompt = prompts.build_system_prompt("sell", "tool-a")
        self.assertIn("-15% 复盘", prompt)


class TestPlaceholderWiring(PoolClauseTestBase):
    def test_no_hardcoded_ratio_in_base_rules(self) -> None:
        """防止以后有人把数字又写回提示词正文。"""
        self.assertIn("__POOL_BRIEF__", prompts.BASE_RULES)
        self.assertIn("__POOL_DISCIPLINE__", prompts.BASE_RULES)
        for stale in ("股息 20%", "现金 25%", "催化 4 周", "-18% 止损"):
            self.assertNotIn(stale, prompts.BASE_RULES, f"提示词正文又出现硬编码：{stale}")

    def test_all_scenarios_resolve_placeholders(self) -> None:
        for scenario in prompts._BODIES:
            with self.subTest(scenario=scenario):
                prompt = prompts.build_system_prompt(scenario, "tool-a")
                for marker in ("__PROTOCOL__", "__TOOLS__", "__RULES__",
                               "__POOL_BRIEF__", "__POOL_DISCIPLINE__"):
                    self.assertNotIn(marker, prompt)
                self.assertIn("四池", prompt)
                self.assertIn("股息", prompt)

    def test_rules_read_failure_falls_back_without_numbers(self) -> None:
        """规则读取失败时绝不能退回旧数字，只能让模型去查工具。"""
        with mock.patch.object(prompts.decision_rules, "pools", side_effect=RuntimeError("broken")):
            brief = prompts.render_pool_brief()
            discipline = prompts.render_pool_discipline()
        self.assertEqual(brief, prompts._FALLBACK_BRIEF)
        self.assertEqual(discipline, prompts._FALLBACK_DISCIPLINE)
        self.assertNotIn("20%", brief)
        self.assertNotIn("25%", discipline)


if __name__ == "__main__":
    unittest.main()
