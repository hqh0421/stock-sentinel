"""四池分配、组合风控、两道确认建议单的回归测试。"""

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import assert_isolated


def _ensure_user() -> int:
    from app.db import database, store
    from app.core.context import set_current_user_id

    database.init_db(force=True)
    if store.count_users() == 0:
        store.create_user("pools_test", "salt", "hash", "pools", "2026-01-01")
    uid = int(store.first_user_id())
    set_current_user_id(uid)
    return uid


def _client() -> TestClient:
    from app.routers.stock_router import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestAllocationMath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_isolated()

    def test_four_pools_and_cash(self):
        from app.services.allocation_service import compute_allocation

        holdings = [
            {"secID": "601318.SH", "shares": 100, "cost": 50, "pool": "股息", "industry": "保险"},
            {"secID": "002230.SZ", "shares": 100, "cost": 50, "pool": "催化", "industry": "计算机"},
            {"secID": "002558.SZ", "shares": 100, "cost": 50, "pool": "低估", "industry": "传媒"},
            {"secID": "600741.SH", "shares": 100, "cost": 50, "pool": "长赛道", "industry": "汽车"},
        ]
        result = compute_allocation(holdings, 100000)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["pools"]), 4)
        self.assertAlmostEqual(sum(p["actual_pct"] for p in result["pools"]), 20.0, places=2)
        self.assertAlmostEqual(result["cash"]["actual_pct"], 80.0, places=2)
        self.assertEqual(result["cash"]["status"], "above_target")

    def test_max_single_and_cash_floor_violations(self):
        from app.services.allocation_service import compute_allocation

        holdings = [{"secID": "601318.SH", "shares": 400, "cost": 50, "pool": "dividend"}]
        result = compute_allocation(holdings, 20000)  # 单票 100%，现金 0
        self.assertTrue(result["ok"])
        kinds = {item["kind"] for item in result["issues"]}
        self.assertIn("max_single", kinds)
        self.assertIn("cash_floor", kinds)

    def test_unclassified_is_reported(self):
        from app.services.allocation_service import compute_allocation

        holdings = [
            {"secID": "601318.SH", "shares": 100, "cost": 50, "pool": "dividend"},
            {"secID": "000001.SZ", "shares": 100, "cost": 10, "pool": ""},
        ]
        result = compute_allocation(holdings, 100000)
        self.assertEqual(result["unclassified"]["count"], 1)
        self.assertIn("unclassified", {item["kind"] for item in result["issues"]})

    def test_max_positions(self):
        from app.services.allocation_service import compute_allocation

        holdings = [
            {"secID": f"60{i:04d}.SH", "shares": 10, "cost": 10, "pool": "dividend"}
            for i in range(3)
        ]
        result = compute_allocation(holdings, 100000)
        self.assertIn("max_positions", {item["kind"] for item in result["issues"]})

    def test_rebalance_only_outside_band(self):
        from app.services.allocation_service import compute_allocation, rebalance_plan

        holdings = [{"secID": "601318.SH", "shares": 600, "cost": 50, "pool": "dividend"}]
        result = compute_allocation(holdings, 100000)  # 30% vs 目标 20%
        plan = rebalance_plan(result)
        self.assertTrue(plan)
        self.assertEqual(plan[0]["pool"], "dividend")
        self.assertEqual(plan[0]["action"], "trim")


class TestPoolRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_isolated()

    def test_take_profit_catalyst_tiers(self):
        from app.services.allocation_service import take_profit_plan

        self.assertEqual(take_profit_plan("catalyst", 10)["triggered"], False)
        t1 = take_profit_plan("catalyst", 20)
        self.assertEqual(t1["trigger_pct"], 18.0)
        self.assertEqual(t1["sell_pct"], 50.0)
        t2 = take_profit_plan("catalyst", 35)
        self.assertEqual(t2["trigger_pct"], 32.0)
        self.assertEqual(t2["sell_pct"], 100.0)

    def test_recover_principal_math(self):
        from app.services.allocation_service import take_profit_plan

        value = take_profit_plan("value", 50)
        self.assertTrue(value["recover_principal"])
        self.assertAlmostEqual(value["sell_pct"], 66.67, places=2)
        long_track = take_profit_plan("long_track", 50)
        self.assertAlmostEqual(long_track["sell_pct"], 66.67, places=2)

    def test_long_track_single_cap_is_75(self):
        from app.core import decision_rules

        self.assertEqual(float(decision_rules.pool("long_track")["max_single_pct"]), 7.5)

    def test_time_stop_only_for_catalyst(self):
        from app.services.allocation_service import time_stop_state

        self.assertEqual(time_stop_state("catalyst", 5, False)["action"], "halve")
        self.assertEqual(time_stop_state("catalyst", 9, False)["action"], "clear")
        self.assertFalse(time_stop_state("dividend", 100)["applies"])
        self.assertFalse(time_stop_state("value", 100)["applies"])

    def test_validate_trade(self):
        from app.services.allocation_service import validate_pool_trade

        self.assertIn("一次性建仓", "".join(validate_pool_trade("value", 5, 1, is_add=True)["issues"]))
        self.assertIn("单票上限", "".join(validate_pool_trade("long_track", 10, 1)["issues"]))
        self.assertTrue(validate_pool_trade("catalyst", 5, 1)["ok"])

    def test_global_risk_levels(self):
        from app.services.allocation_service import global_risk_state

        self.assertEqual(global_risk_state(-10)["level"], "normal")
        self.assertEqual(global_risk_state(-16)["level"], "stop_new")
        self.assertEqual(global_risk_state(-21)["level"], "halve")
        self.assertEqual(global_risk_state(-26)["level"], "defensive")


class TestProposalStateMachine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_isolated()
        cls.uid = _ensure_user()

    def test_two_confirmations_required(self):
        from app.services import proposal_service

        proposal = proposal_service.create(self.uid, "rebalance", "再平衡", {"actions": []}, "分析")
        self.assertEqual(proposal["status"], "proposed")

        accepted = proposal_service.accept(self.uid, proposal["id"])
        self.assertEqual(accepted["status"], "accepted")

        with self.assertRaises(ValueError):
            proposal_service.execute(self.uid, proposal["id"])

        executed = proposal_service.execute(self.uid, proposal["id"], confirm=True)
        self.assertEqual(executed["status"], "executed")

        with self.assertRaises(ValueError):
            proposal_service.execute(self.uid, proposal["id"], confirm=True)

    def test_cannot_execute_before_accept(self):
        from app.services import proposal_service

        proposal = proposal_service.create(self.uid, "buy", "买入", {}, "分析")
        with self.assertRaises(ValueError):
            proposal_service.execute(self.uid, proposal["id"], confirm=True)

    def test_ignore_keeps_trace(self):
        from app.services import proposal_service

        proposal = proposal_service.create(self.uid, "sell", "卖出", {}, "分析")
        ignored = proposal_service.ignore(self.uid, proposal["id"], "不跟随")
        self.assertEqual(ignored["status"], "ignored")
        self.assertTrue(ignored["ignored_at"])


class TestPoolApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_isolated()
        cls.uid = _ensure_user()
        cls.client = _client()

    def test_pools_endpoint(self):
        response = self.client.get("/api/stock/pools")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["pools"]), 4)
        self.assertEqual(body["cash"]["target_pct"], 25.0)
        self.assertEqual(body["global_risk"]["max_industry_pct"], 30.0)

    def test_proposal_flow_via_api(self):
        created = self.client.post("/api/stock/proposals", json={
            "kind": "rebalance", "title": "API 再平衡", "analysis": "测试",
            "payload": {"actions": [{"pool": "catalyst", "action": "trim"}]},
        })
        self.assertEqual(created.status_code, 200)
        pid = created.json()["id"]
        self.assertEqual(created.json()["status"], "proposed")

        self.assertEqual(self.client.post(f"/api/stock/proposals/{pid}/accept", json={}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/stock/proposals/{pid}/execute", json={}).status_code, 400)

        executed = self.client.post(f"/api/stock/proposals/{pid}/execute",
                                    json={"confirm": True, "note": "确认执行"})
        self.assertEqual(executed.status_code, 200)
        self.assertEqual(executed.json()["status"], "executed")

    def test_classify_rejects_unknown_pool(self):
        response = self.client.post("/api/stock/allocation/classify",
                                    json={"secID": "600741.SH", "pool": "not_a_pool"})
        self.assertEqual(response.status_code, 400)

    def test_allocation_endpoint(self):
        response = self.client.get("/api/stock/allocation")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("ok", body)
        if body.get("ok"):
            self.assertEqual(len(body["pools"]), 4)
            self.assertIn("rebalance", body)


if __name__ == "__main__":
    unittest.main()
