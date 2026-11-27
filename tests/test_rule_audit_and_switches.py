"""规则变更审计留痕 + 组合风控开关可配置。

补齐两处「实现了但没通电」的缺口：
- 波动率目标 / 回撤护栏此前只能在 JSON 里手改，界面只读；
- audit_log 早有 detail 列，但中间件从没写过，追溯不到「谁把哪一档改成多少」。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import decision_rules
from app.routers import stock_router

REAL_RULES = Path(__file__).resolve().parent.parent / "config" / "decision_rules.json"


class RuleTestBase(unittest.TestCase):
    """每个用例用独立的规则文件副本，绝不碰生产配置。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="rule_audit_")
        self._path = Path(self._tmpdir) / "decision_rules.json"
        shutil.copy(REAL_RULES, self._path)
        self._old_env = os.environ.get("MONITOR_STOCK_RULES_FILE")
        os.environ["MONITOR_STOCK_RULES_FILE"] = str(self._path)
        decision_rules.get_rules()

    def tearDown(self) -> None:
        if self._old_env is None:
            os.environ.pop("MONITOR_STOCK_RULES_FILE", None)
        else:
            os.environ["MONITOR_STOCK_RULES_FILE"] = self._old_env
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        decision_rules.get_rules()

    def client_with_audit_capture(self):
        """搭一个只挂 stock_router 的 mini app，捕获路由写到 request.state 的明细。"""
        captured: list = []
        app = FastAPI()
        app.include_router(stock_router.router)

        @app.middleware("http")
        async def capture(request, call_next):
            response = await call_next(request)
            captured.append(getattr(request.state, "audit_detail", ""))
            return response

        return TestClient(app), captured


class TestDiffHelpers(RuleTestBase):
    def test_diff_top_only_reports_changed_fields(self) -> None:
        diff = stock_router._diff_top(
            {"a": 1, "b": 2}, {"a": 1, "b": 9}, ["a", "b"])
        self.assertEqual(diff, {"b": {"from": 2, "to": 9}})

    def test_set_audit_detail_serializes_changes(self) -> None:
        request = SimpleNamespace(state=SimpleNamespace())
        stock_router._set_audit_detail(request, {"x": {"from": 1, "to": 2}})
        self.assertEqual(json.loads(request.state.audit_detail),
                         {"changes": {"x": {"from": 1, "to": 2}}})

    def test_set_audit_detail_skips_noop(self) -> None:
        request = SimpleNamespace(state=SimpleNamespace())
        stock_router._set_audit_detail(request, {})
        self.assertFalse(hasattr(request.state, "audit_detail"))

    def test_write_audit_forwards_detail(self) -> None:
        """中间件这条线必须真的把 detail 传给 store，否则前面全白做。"""
        from app import main

        request = SimpleNamespace(
            method="PUT", url=SimpleNamespace(path="/api/stock/pools"),
            state=SimpleNamespace(audit_detail='{"changes":{"x":1}}'),
            client=SimpleNamespace(host="127.0.0.1"),
        )
        with mock.patch("app.db.store.add_audit_log") as add:
            main._write_audit(request, 1, 200)
        self.assertEqual(add.call_args.kwargs["detail"], '{"changes":{"x":1}}')


class TestMonitorSwitches(RuleTestBase):
    def test_defaults_are_off(self) -> None:
        rules = decision_rules.monitor_rules()
        self.assertFalse(rules.get("vol_target_enabled"))
        self.assertFalse(rules.get("portfolio_drawdown_guard_enabled"))

    def test_legal_switch_writes_and_hot_reloads(self) -> None:
        from app.services.risk_guard_service import save_monitor_switches

        result = save_monitor_switches({"vol_target_enabled": True})
        self.assertTrue(result["vol_target_enabled"])
        self.assertTrue(decision_rules.get("monitor.vol_target_enabled"))
        # 另一个开关不受影响
        self.assertFalse(decision_rules.get("monitor.portfolio_drawdown_guard_enabled"))

    def test_unknown_switch_rejected(self) -> None:
        from app.services.risk_guard_service import save_monitor_switches

        with self.assertRaisesRegex(ValueError, "不支持的风控开关"):
            save_monitor_switches({"agent_enabled": True})

    def test_non_bool_rejected(self) -> None:
        from app.services.risk_guard_service import save_monitor_switches

        with self.assertRaisesRegex(ValueError, "必须是 true / false"):
            save_monitor_switches({"vol_target_enabled": "yes"})

    def test_empty_patch_rejected(self) -> None:
        from app.services.risk_guard_service import save_monitor_switches

        with self.assertRaises(ValueError):
            save_monitor_switches({})


class TestRouterWritesAudit(RuleTestBase):
    def test_risk_switch_records_before_after(self) -> None:
        client, captured = self.client_with_audit_capture()
        response = client.put("/api/stock/risk_switches",
                              json={"vol_target_enabled": True})
        self.assertEqual(response.status_code, 200, response.text)
        detail = json.loads(captured[-1])
        self.assertEqual(detail["changes"]["monitor"]["vol_target_enabled"],
                         {"from": False, "to": True})
        self.assertTrue(response.json()["monitor"]["vol_target_enabled"])

    def test_risk_switch_rejects_unknown_field(self) -> None:
        client, _ = self.client_with_audit_capture()
        response = client.put("/api/stock/risk_switches", json={"whatever": True})
        self.assertEqual(response.status_code, 400)

    def test_pool_change_records_only_changed_columns(self) -> None:
        client, captured = self.client_with_audit_capture()
        # 两个池对冲，四池+现金仍为 100%；持仓快照替换成空，避免测试外呼行情
        with mock.patch.object(stock_router.stock_service, "get_favorites_list", return_value=[]), \
             mock.patch.object(stock_router.stock_service, "get_user_settings", return_value={}):
            response = client.put("/api/stock/pools", json={"pools": {
                "dividend": {"target_pct": 19.0},
                "value": {"target_pct": 23.0},
            }})
        self.assertEqual(response.status_code, 200, response.text)
        detail = json.loads(captured[-1])
        changes = detail["changes"]["pools"]
        self.assertEqual(changes["dividend"]["target_pct"], {"from": 20.0, "to": 19.0})
        self.assertEqual(changes["value"]["target_pct"], {"from": 22.0, "to": 23.0})
        # 没改的字段不能混进审计
        self.assertNotIn("max_single_pct", changes["dividend"])

    def test_noop_pool_write_has_no_detail(self) -> None:
        client, captured = self.client_with_audit_capture()
        with mock.patch.object(stock_router.stock_service, "get_favorites_list", return_value=[]), \
             mock.patch.object(stock_router.stock_service, "get_user_settings", return_value={}):
            response = client.put("/api/stock/pools", json={"pools": {
                "dividend": {"target_pct": 20.0},
            }})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(captured[-1], "")

    def test_invalid_pool_write_records_no_detail(self) -> None:
        client, captured = self.client_with_audit_capture()
        response = client.put("/api/stock/pools", json={"pools": {
            "dividend": {"target_pct": 99.0},
        }})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(captured[-1], "")


if __name__ == "__main__":
    unittest.main()
