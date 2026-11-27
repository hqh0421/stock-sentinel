"""新增 API 契约与安全回归（用最小 FastAPI app，避免启动生产 lifespan/调度器）。"""

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import assert_isolated


def _build_client():
    from app.routers.stock_router import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestNewEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_isolated()
        cls.client = _build_client()

    def test_decision_rules_endpoint(self):
        r = self.client.get("/api/stock/decision_rules")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["fee_model"], "ledger_aligned")
        self.assertIn("commission_rate", body["fees"])
        self.assertIn("max_portfolio_drawdown_pct", body["risk"])
        self.assertNotIn("api_key", body)

    def test_source_health_endpoint(self):
        r = self.client.get("/api/stock/source_health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreaterEqual(len(body["sources"]), 6)
        self.assertIn("checked_at", body)

    def test_risk_guard_endpoint(self):
        r = self.client.post("/api/stock/risk_guard", json={"equity": [100, 110, 90]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("relative_position", body)
        self.assertIn("drawdown_guard", body)
        self.assertTrue(body["drawdown_guard"]["available"])

    def test_factor_validation_requires_params(self):
        r = self.client.post("/api/stock/factor_validation", json={"code": "600519"})
        self.assertEqual(r.status_code, 400)


class TestStrategySaveSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_isolated()
        cls.client = _build_client()

    def test_malicious_expression_rejected_before_persist(self):
        for expr in ("__import__('os').system('id')", "(1).__class__.__mro__", "10**10**10"):
            r = self.client.post("/api/stock/strategy/save", json={
                "name": "malicious", "entry_expr": expr, "exit_expr": "close < 5",
            })
            self.assertEqual(r.status_code, 400, f"{expr} 应被拒绝")
            self.assertIn("不合法", r.json()["detail"])

    def test_exit_expression_also_validated(self):
        r = self.client.post("/api/stock/strategy/save", json={
            "name": "malicious", "entry_expr": "close > MA20",
            "exit_expr": "[x for x in range(3)]",
        })
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
