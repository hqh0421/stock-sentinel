"""纪律闭环：工具可达、必调前置、trigger 真正落地。"""

import datetime
import re
import unittest
from pathlib import Path

from tests import assert_isolated

ROOT = Path(__file__).resolve().parent.parent


def _registered_tool_names():
    text = (ROOT / "app" / "agent" / "tools.py").read_text(encoding="utf-8")
    return set(re.findall(r'ToolSpec\(\s*"([a-z_]+)"', text))


class TestScenarioTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_isolated()

    def test_every_registered_tool_is_reachable(self):
        from app.agent.scenarios import validate_scenario_tools

        report = validate_scenario_tools(_registered_tool_names())
        self.assertTrue(report["ok"], f"一致性失败：{report}")
        self.assertEqual(report["unknown_tools"], {})
        self.assertEqual(report["unreachable_tools"], [])

    def test_prompts_required_tools_are_in_relevant_scenarios(self):
        from app.agent.scenarios import SCENARIOS

        prompts = (ROOT / "app" / "agent" / "prompts.py").read_text(encoding="utf-8")
        for tool in ("get_sentiment_policy", "get_value_position",
                     "get_industry_valuation", "get_sentiment_history", "get_sell_target"):
            self.assertIn(tool, prompts)
            reachable = [n for n, cfg in SCENARIOS.items() if tool in cfg["tools"]]
            self.assertTrue(reachable, f"{tool} 提示词要求必调却无任何场景可达")

    def test_triggers_are_no_longer_just_text(self):
        from app.agent.scenarios import SCENARIOS

        for key in ("daily", "risk", "review", "doctor"):
            text = SCENARIOS[key]["trigger"]
            # 每个 trigger 都必须指向一个真实执行机制，而不是「每日一次」这类空文案
            self.assertTrue(any(k in text for k in ("MonitorScheduler", "调度器", "回测接口", "自动调用")),
                            f"{key} 的 trigger 仍是文案：{text}")


class TestPreflight(unittest.TestCase):
    def test_buy_advice_forces_sentiment_policy(self):
        from app.agent.runtime import missing_preflight_tools

        tools = ["get_sentiment_policy", "get_sell_target"]
        buy = {"headline": "可以分批加仓", "items": [{"body": "跌到位可以买", "action": "分2批买入"}]}
        self.assertEqual(missing_preflight_tools(buy, set(), tools), ["get_sentiment_policy"])
        self.assertEqual(missing_preflight_tools(buy, {"get_sentiment_policy"}, tools), [])

    def test_sell_price_forces_target(self):
        from app.agent.runtime import missing_preflight_tools

        tools = ["get_sentiment_policy", "get_sell_target"]
        sell = {"headline": "目标价 45.10", "items": [{"body": "T1 止盈减仓"}]}
        self.assertEqual(missing_preflight_tools(sell, set(), tools), ["get_sell_target"])

    def test_neutral_answer_is_untouched(self):
        from app.agent.runtime import missing_preflight_tools

        tools = ["get_sentiment_policy", "get_sell_target"]
        neutral = {"headline": "今日无显著异动", "items": [{"body": "持有观察"}]}
        self.assertEqual(missing_preflight_tools(neutral, set(), tools), [])


class TestScheduledTriggers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_isolated()

    def setUp(self):
        from app.services.agent_schedule_service import agent_schedule_service
        self.svc = agent_schedule_service

    def test_daily_and_risk_fire_at_configured_times(self):
        monday = datetime.datetime(2026, 11, 16)
        # 触发时刻起 30 分钟内都算补跑窗口（tick 30s，且允许重启后补跑）
        cases = {
            (9, 5): ["risk"], (9, 30): ["risk"], (9, 36): [], (11, 0): [],
            (15, 8): ["daily"], (15, 30): ["daily"], (15, 40): [], (23, 0): [],
        }
        for (h, m), expected in cases.items():
            tasks = [t for t, _ in self.svc.due_scheduled(monday.replace(hour=h, minute=m))]
            self.assertEqual(tasks, expected, f"{h:02d}:{m:02d}")

    def test_weekend_does_not_fire(self):
        sunday = datetime.datetime(2026, 11, 15, 15, 8)
        self.assertEqual(self.svc.due_scheduled(sunday), [])

    def test_format_markdown_contains_headline_and_items(self):
        text = self.svc.format_markdown({
            "headline": "有一件必须处理",
            "items": [{"tag": "立即卖出", "title": "A 股", "body": "已破位", "action": "减 1/3"}],
        })
        self.assertIn("有一件必须处理", text)
        self.assertIn("立即卖出", text)
        self.assertIn("减 1/3", text)

    def test_review_allows_when_disabled_or_agent_fails(self):
        import os
        os.environ["MONITOR_STOCK_RULES_FILE"] = str(ROOT / "config" / "decision_rules.json")
        # 关闭开关时应直接放行，不调用任何模型
        old = self.svc._rules()
        from unittest.mock import patch
        with patch.object(self.svc, "_rules", return_value={"review_before_notify": False}):
            r = self.svc.review_monitor(1, [{"name": "A", "price": 10}])
        self.assertEqual(r["decision"], "pass")
        self.assertEqual(r["reason"], "disabled")
        self.assertIsInstance(old, dict)

    def test_status_exposes_tasks(self):
        st = self.svc.status()
        self.assertIn("daily", st["tasks"])
        self.assertIn("risk", st["tasks"])
        self.assertIn("review_before_notify", st)
        self.assertIn("source_failure_streak", st)

    def test_source_failure_escalates_only_after_streak(self):
        from unittest.mock import patch
        from app.services.stock_service import stock_service

        self.svc._source_streak.clear()
        with patch.object(stock_service, "source_health_snapshot",
                          return_value={"xueqiu": {"cooldown": True}}), \
             patch.object(self.svc, "push", return_value={"ok": True}), \
             patch.object(self.svc, "_is_done", return_value=False), \
             patch.object(self.svc, "_mark_done"):
            first = self.svc.check_source_health([1], trading_now=True)
            second = self.svc.check_source_health([1], trading_now=True)
            third = self.svc.check_source_health([1], trading_now=True)
        self.assertEqual(len(first), 0)
        self.assertEqual(len(second), 0)
        self.assertGreater(len(third), 0)
        self.assertEqual(third[0]["task"], "source_alert")

    def test_source_health_skipped_outside_trading_hours(self):
        self.assertEqual(self.svc.check_source_health([1], trading_now=False), [])


if __name__ == "__main__":
    unittest.main()
