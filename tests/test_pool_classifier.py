"""四池分类器与「池 → 内部卖出风格」映射的回归测试。

覆盖三件事：
1. 池到 horizon 的单向映射（删除前端周期选择后，监控与卖出目标价仍要有确定口径）
2. 分类器在典型样本上的判断与降级行为（数据不足必须说不足，不硬猜）
3. 分类建议接口是只读的：不落库，采纳才写
"""

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import assert_isolated


def _ensure_user() -> int:
    from app.core.context import set_current_user_id
    from app.db import database, store

    database.init_db(force=True)
    if store.count_users() == 0:
        store.create_user("pool_cls_test", "salt", "hash", "cls", "2026-01-01")
    uid = int(store.first_user_id())
    set_current_user_id(uid)
    return uid


def _client() -> TestClient:
    from app.routers.stock_router import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestPoolHorizonMapping(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_isolated()

    def test_pool_maps_to_sell_style(self):
        from app.services.allocation_service import horizon_for_pool

        self.assertEqual(horizon_for_pool("dividend"), "long")
        self.assertEqual(horizon_for_pool("value"), "long")
        self.assertEqual(horizon_for_pool("long_track"), "mid_long")
        self.assertEqual(horizon_for_pool("catalyst"), "swing")

    def test_unclassified_falls_back(self):
        from app.services.allocation_service import horizon_for_pool

        self.assertEqual(horizon_for_pool("", "mid_long"), "mid_long")
        self.assertEqual(horizon_for_pool("", ""), "long")
        self.assertEqual(horizon_for_pool(None, "garbage"), "long")

    def test_chinese_alias_works(self):
        from app.services.allocation_service import horizon_for_pool

        self.assertEqual(horizon_for_pool("股息"), "long")
        self.assertEqual(horizon_for_pool("催化波段成长"), "swing")

    def test_effective_horizon_prefers_pool(self):
        from app.services.stock_service import stock_service

        # 池存在时忽略旧 horizon，避免「删了周期下拉却仍按旧值算卖出」
        # 催化池映射到 swing，而监控参数只有 short/mid_long/long，swing 归到 short（波段档）
        self.assertEqual(
            stock_service.effective_horizon({"pool": "catalyst", "horizon": "long"}), "short"
        )
        self.assertEqual(
            stock_service.effective_horizon({"pool": "long_track", "horizon": "long"}), "mid_long"
        )
        self.assertEqual(
            stock_service.effective_horizon({"pool": "dividend", "horizon": "short"}), "long"
        )
        # 未分类才回退到旧 horizon
        self.assertEqual(stock_service.effective_horizon({"pool": "", "horizon": "swing"}), "short")
        self.assertEqual(stock_service.effective_horizon({}), "long")


class TestPoolClassifier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_isolated()

    def test_high_dividend_bank(self):
        from app.services.pool_classifier import classify_one

        r = classify_one(
            code="601398.SH", name="工商银行", industry="银行",
            valuation={"zone": "undervalued", "pe": 5.2, "pb": 0.6, "roe": 11},
            fundamentals={"roe": 11.5, "rev_growth": 3.0, "profit_growth": 2.0, "debt_ratio": 91},
        )
        self.assertEqual(r["suggested_pool"], "dividend")
        self.assertEqual(r["confidence"], "high")
        self.assertFalse(r["needs_manual"])

    def test_growth_stock_goes_long_track(self):
        from app.services.pool_classifier import classify_one

        r = classify_one(
            code="002230.SZ", name="科大讯飞", industry="计算机",
            valuation={"zone": "overvalued", "pe": 80, "pb": 6.0, "roe": 6},
            fundamentals={"roe": 6.0, "rev_growth": 28.0, "profit_growth": 35.0, "gross_margin": 55},
        )
        self.assertEqual(r["suggested_pool"], "long_track")
        # 成长倾向必须提示「可能是催化」，把意图判断交回用户
        self.assertIn("催化波段成长", r["note"])

    def test_cheap_non_dividend_goes_value(self):
        from app.services.pool_classifier import classify_one

        r = classify_one(
            code="600741.SH", name="华域汽车", industry="汽车零部件",
            valuation={"zone": "undervalued", "pe": 8, "pb": 0.8, "roe": 7},
            fundamentals={"roe": 7.0, "rev_growth": -5.0, "profit_growth": -8.0},
        )
        self.assertEqual(r["suggested_pool"], "value")

    def test_no_data_must_not_guess(self):
        from app.services.pool_classifier import classify_one

        r = classify_one(code="000001.SZ")
        self.assertEqual(r["suggested_pool"], "")
        self.assertTrue(r["needs_manual"])
        self.assertIn("手动", r["note"])

    def test_weak_signal_marks_needs_manual(self):
        from app.services.pool_classifier import classify_one

        # 只有一个弱信号（增速低），不足以定池
        r = classify_one(code="600000.SH", fundamentals={"rev_growth": 3.0, "profit_growth": 2.0})
        self.assertTrue(r["needs_manual"])
        self.assertEqual(r["confidence"], "low")

    def test_override_marks_manual(self):
        from app.services.pool_classifier import apply_overrides

        items = [{"code": "600000.SH", "suggested_pool": "", "confidence": "low", "needs_manual": True}]
        out = apply_overrides(items, {"600000.SH": "股息"})
        self.assertEqual(out[0]["suggested_pool"], "dividend")
        self.assertEqual(out[0]["confidence"], "manual")
        self.assertFalse(out[0]["needs_manual"])


class TestClassifySuggestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_isolated()
        _ensure_user()

    def test_suggest_is_read_only(self):
        """建议接口不能改库：调用前后 pool 必须一致。"""
        from app.services.stock_service import stock_service

        created = False
        if not stock_service.is_favorite("600741.SH"):
            stock_service.add_favorite("600741.SH", 100, 15.0, name="华域汽车")
            created = True
        try:
            before = {it["secID"]: it.get("pool") for it in stock_service.get_favorites_list(include_quotes=False)}
            client = _client()
            with patch.object(stock_service, "_evaluate_valuation", return_value={}), \
                 patch.object(stock_service, "get_company_info", return_value={"industry": "汽车零部件"}), \
                 patch.object(stock_service, "get_fundamental_snapshot", return_value={}):
                resp = client.post("/api/stock/allocation/classify/suggest", json={})
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertTrue(body["ok"])
            self.assertIsInstance(body["items"], list)
            after = {it["secID"]: it.get("pool") for it in stock_service.get_favorites_list(include_quotes=False)}
            self.assertEqual(before, after, "建议接口不应写库")
        finally:
            if created:
                stock_service.remove_favorite("600741.SH")

    def test_suggest_respects_deadline_when_data_source_hangs(self):
        """外部估值源卡死时，接口必须按 deadline 降级返回，而不是挂成分钟级。

        2026-11-15 线上实测：串行拉估值 + 雪球 Cookie 失效重试，单次请求耗时 306 秒。
        """
        import time as _time

        from app.services.stock_service import stock_service

        created = False
        if not stock_service.is_favorite("002558.SZ"):
            stock_service.add_favorite("002558.SZ", 100, 19.0, name="巨人网络")
            created = True

        def _slow(_code):
            _time.sleep(3.0)
            return {}

        try:
            client = _client()
            with patch("app.core.decision_rules.pool_classify_rules",
                       return_value={"deadline_sec": 0.4, "max_workers": 4}), \
                 patch.object(stock_service, "_evaluate_valuation", side_effect=_slow), \
                 patch.object(stock_service, "get_fundamental_snapshot", side_effect=_slow), \
                 patch.object(stock_service, "get_company_info", side_effect=_slow):
                started = _time.time()
                resp = client.post("/api/stock/allocation/classify/suggest", json={})
                elapsed = _time.time() - started
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertLess(elapsed, 2.0, "接口必须按 deadline 返回，不能被慢数据源拖住")
            self.assertEqual(body["deadline_sec"], 0.4)
            self.assertGreater(body["partial_count"], 0, "超时的票必须标成 partial")
            item = next(it for it in body["items"] if it["code"] == "002558.SZ")
            self.assertTrue(item["partial"])
            self.assertIn("valuation", item["missing_data"])
            # 数据缺失时不能硬猜：要么给不出建议，要么置信度是低的
            if item["suggested_pool"]:
                self.assertEqual(item["confidence"], "low")
        finally:
            if created:
                stock_service.remove_favorite("002558.SZ")

    def test_suggest_skips_classified_by_default(self):
        from app.services.stock_service import stock_service

        created = False
        if not stock_service.is_favorite("601318.SH"):
            stock_service.add_favorite("601318.SH", 100, 50.0, name="中国平安", pool="dividend")
            created = True
        try:
            client = _client()
            with patch.object(stock_service, "_evaluate_valuation", return_value={}), \
                 patch.object(stock_service, "get_company_info", return_value={}), \
                 patch.object(stock_service, "get_fundamental_snapshot", return_value={}):
                resp = client.post("/api/stock/allocation/classify/suggest", json={"only_unclassified": True})
            codes = {it["code"] for it in resp.json()["items"]}
            self.assertNotIn("601318.SH", codes, "已分类持仓默认不应出现在建议里")
        finally:
            if created:
                stock_service.remove_favorite("601318.SH")


if __name__ == "__main__":
    unittest.main()
