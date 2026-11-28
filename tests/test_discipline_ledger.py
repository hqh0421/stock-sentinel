"""纪律执行台账：违规结构化 → 越权落盘 → 可复核的复盘聚合。

对标结论见 docs/product/competitive-analysis/discipline-ledger-2026-11-15.md。
本文件锁死三件事：
  1. 违规 code 与文案同源，旧 ``issues`` 调用方不受影响；
  2. ``force=true`` 越过后「违反了什么」必须落到台账，重启/刷新后仍可查；
  3. 遵守率的分母口径可复核，不编造数字。

复用 tests.test_pool_gate.FakeSvc，不复制第二份 fake。
"""
from __future__ import annotations

import datetime
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import allocation_service as alloc
from app.services.trade_ledger import TradeLedger
from app.routers import stock_router
from tests.test_pool_gate import FakeSvc


class TestViolationCodes(unittest.TestCase):
    def test_codes_issues_violations_are_one_to_one(self):
        v = alloc.validate_pool_trade("dividend", post_weight_pct=30,
                                      post_positions=1, is_add=True, cash_after_pct=5)
        self.assertFalse(v["ok"])
        self.assertEqual(v["codes"], [x["code"] for x in v["violations"]])
        self.assertEqual(v["issues"], [x["message"] for x in v["violations"]])
        self.assertEqual(len(v["codes"]), len(v["issues"]))
        self.assertIn("max_single", v["codes"])
        self.assertIn("cash_floor", v["codes"])

    def test_legacy_cash_floor_issue_still_returns_string(self):
        item = alloc.cash_floor_violation(5)
        self.assertEqual(item["code"], "cash_floor")
        self.assertEqual(alloc.cash_floor_issue(5), item["message"])
        self.assertIsInstance(alloc.cash_floor_issue(5), str)
        self.assertIsNone(alloc.cash_floor_issue(20))
        self.assertIsNone(alloc.cash_floor_issue(None))

    def test_unknown_pool_returns_same_shape(self):
        v = alloc.validate_pool_trade("不存在的池", 1, 1)
        self.assertEqual(v["codes"], ["unknown_pool"])
        self.assertEqual(len(v["issues"]), 1)
        self.assertEqual(v["violations"][0]["label"], "未知池")

    def test_label_comes_from_single_source(self):
        item = alloc.make_violation("max_single", "超了")
        self.assertEqual(item["label"], alloc.violation_label("max_single"))
        self.assertEqual(alloc.violation_label("no_such_code"), "no_such_code")
        self.assertEqual(alloc.violation_label(""), "未知规则")

    def test_clean_trade_has_no_codes(self):
        v = alloc.validate_pool_trade("value", post_weight_pct=5,
                                      post_positions=1, is_add=False, cash_after_pct=30)
        self.assertTrue(v["ok"])
        self.assertEqual(v["codes"], [])
        self.assertEqual(v["violations"], [])


class _LedgerBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="discipline_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def make(self, favorites=None, **kwargs):
        svc = FakeSvc(favorites or [], **kwargs)
        return TradeLedger(config_dir=self._tmpdir, svc=svc)

    def write_ledger(self, rows):
        Path(self._tmpdir, "trade_log.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8")


class TestOverridePersistence(_LedgerBase):
    def _over_cap(self):
        return self.make([{"secID": "600000.SH", "shares": 2000, "cost": 10,
                           "pool": "dividend"}])

    def test_force_override_is_persisted_and_survives_reload(self):
        ledger = self._over_cap()
        result = ledger.apply_trade("600000", "buy", 900, 10, force=True)
        self.assertTrue(result["ok"], result)
        override = result["rule_override"]
        self.assertTrue(override["codes"])
        self.assertTrue(override["violations"])
        self.assertEqual(override["pool"], "dividend")
        self.assertIn("rule_override", result["record"])

        # 换一个实例重新读文件 = 模拟刷新页面 / 重启服务
        reloaded = self.make()._load()
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0]["rule_override"]["codes"], override["codes"])
        self.assertIn("单票", " ".join(reloaded[0]["rule_override"]["issues"]))

    def test_warning_alone_is_not_a_trace(self):
        """旧实现只在响应 warnings 里留痕；本用例锁死现在必须同时落盘。"""
        ledger = self._over_cap()
        result = ledger.apply_trade("600000", "buy", 900, 10, force=True)
        self.assertTrue(any("越过四池规则" in w for w in result["warnings"]))
        raw = Path(self._tmpdir, "trade_log.json").read_text(encoding="utf-8")
        self.assertIn("rule_override", raw)
        self.assertIn("max_single", raw)

    def test_blocked_buy_exposes_codes_and_writes_nothing(self):
        ledger = self._over_cap()
        result = ledger.apply_trade("600000", "buy", 900, 10)
        self.assertFalse(result["ok"])
        self.assertEqual(result["gate"], "pool_rules")
        self.assertIn("max_single", result["codes"])
        self.assertEqual(ledger._load(), [])

    def test_plain_buy_has_no_override(self):
        ledger = self.make()
        result = ledger.apply_trade("000001", "buy", 500, 10)
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["rule_override"])
        self.assertNotIn("rule_override", result["record"])

    def test_dry_run_reports_but_never_persists(self):
        ledger = self._over_cap()
        result = ledger.apply_trade("600000", "buy", 700, 10, force=True, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["rule_override"])
        self.assertEqual(ledger._load(), [])

    def test_sell_never_overrides_even_with_force(self):
        ledger = self._over_cap()
        result = ledger.apply_trade("600000", "sell", 100, 10, force=True)
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["rule_override"])


class TestDisciplineSummary(_LedgerBase):
    def _rows(self):
        today = datetime.date.today()
        old = today - datetime.timedelta(days=40)
        hit = {
            "pool": "dividend", "codes": ["max_single", "cash_floor"],
            "violations": [
                {"code": "max_single", "label": "单票上限", "message": "超单票"},
                {"code": "cash_floor", "label": "现金下限", "message": "破现金"},
            ],
            "issues": ["超单票", "破现金"],
        }
        return [
            {"id": "T1", "created_at": f"{today} 10:00:00", "trade_date": str(today),
             "secID": "600000.SH", "name": "测试A", "side": "buy", "rule_override": hit},
            {"id": "T2", "created_at": f"{today} 11:00:00", "trade_date": str(today),
             "secID": "000001.SZ", "name": "测试B", "side": "buy"},
            {"id": "T3", "created_at": f"{today} 12:00:00", "trade_date": str(today),
             "secID": "600000.SH", "name": "测试A", "side": "sell"},
            {"id": "T4", "created_at": f"{old} 10:00:00", "trade_date": str(old),
             "secID": "600519.SH", "name": "测试C", "side": "buy", "rule_override": hit},
        ]

    def test_rate_and_scope_are_explicit(self):
        self.write_ledger(self._rows())
        d = self.make().discipline_summary(days=30)
        self.assertTrue(d["ok"])
        self.assertEqual(d["buy_count"], 2)      # 卖出 + 40 天前的都不计入
        self.assertEqual(d["override_count"], 1)
        self.assertEqual(d["compliance_pct"], 50.0)
        self.assertIn("分母", d["scope"])
        self.assertEqual(d["since"], str(datetime.date.today() - datetime.timedelta(days=29)))

    def test_by_code_and_by_pool_use_backend_labels(self):
        self.write_ledger(self._rows())
        d = self.make().discipline_summary(days=30)
        codes = {x["code"]: x for x in d["by_code"]}
        self.assertEqual(set(codes), {"max_single", "cash_floor"})
        self.assertEqual(codes["max_single"]["label"], "单票上限")
        self.assertEqual(codes["max_single"]["count"], 1)
        self.assertEqual(codes["cash_floor"]["last_message"], "破现金")
        self.assertEqual(d["by_pool"][0]["pool"], alloc.pool_label("dividend"))

    def test_no_buy_does_not_invent_100_percent(self):
        self.write_ledger([{"id": "S1", "trade_date": str(datetime.date.today()),
                            "created_at": f"{datetime.date.today()} 10:00:00", "side": "sell"}])
        d = self.make().discipline_summary()
        self.assertEqual(d["buy_count"], 0)
        self.assertIsNone(d["compliance_pct"])
        self.assertEqual(d["override_count"], 0)

    def test_recent_sorted_desc_and_limited(self):
        self.write_ledger(self._rows())
        d = self.make().discipline_summary(days=400, recent=1)
        self.assertEqual(len(d["recent"]), 1)
        self.assertEqual(d["recent"][0]["id"], "T1")
        self.assertEqual(d["recent"][0]["labels"], ["单票上限", "现金下限"])

    def test_days_are_clamped(self):
        self.write_ledger([])
        self.assertEqual(self.make().discipline_summary(days=0)["days"], 1)
        self.assertEqual(self.make().discipline_summary(days=9999)["days"], 365)

    def test_legacy_override_without_codes_is_not_guessed(self):
        today = str(datetime.date.today())
        self.write_ledger([{"id": "OLD", "trade_date": today, "created_at": f"{today} 09:00:00",
                            "side": "buy", "secID": "600000.SH",
                            "rule_override": {"issues": ["旧文案"]}}])
        d = self.make().discipline_summary()
        self.assertEqual(d["override_count"], 1)
        self.assertEqual(d["by_code"][0]["code"], "unclassified")
        self.assertEqual(d["by_pool"][0]["pool"], "未分类")


class TestAdherencePnl(_LedgerBase):
    """守规 vs 越权的落袋盈亏对比：分摊之和恒等于台账 realized_pnl，可复核。"""

    today = str(datetime.date.today())

    def _hit(self):
        return {"pool": "dividend", "codes": ["max_single"],
                "violations": [{"code": "max_single", "label": "单票上限", "message": "超了"}],
                "issues": ["超了"]}

    def _row(self, rid, sec, side, shares, price, amount, fee=0.0, override=None,
             cost_before=None, cost_after=None, day=None):
        row = {"id": rid, "created_at": f"{day or self.today} 10:0{len(rid) % 10}:00",
               "trade_date": day or self.today, "secID": sec, "name": sec,
               "side": side, "shares": shares, "price": price, "amount": amount,
               "fee": fee, "realized_pnl": None}
        if side == "adjust":
            row["shares_after"] = shares
        if override is not None:
            row["rule_override"] = override
        if cost_before is not None:
            row["cost_before"] = cost_before
        if cost_after is not None:
            row["cost_after"] = cost_after
        return row

    def test_slices_sum_equals_ledger_realized(self):
        """台账口径：摊薄成本 15 元，卖出 600 股 realized=0；
        分解后 越权 +1500 / 守规 −1500，两组之和必须等于 0。"""
        hit = self._hit()
        self.write_ledger([
            self._row("B1", "600000.SH", "buy", 500, 10, 5000, override=hit),
            self._row("B2", "600000.SH", "buy", 500, 20, 10000),
            self._row("S1", "600000.SH", "sell", 600, 15, 9000),
        ])
        pnl = self.make().discipline_summary(days=30)["pnl_by_adherence"]
        self.assertTrue(pnl["sufficient"])
        self.assertEqual(pnl["violated"]["realized_pnl"], 1500.0)
        self.assertEqual(pnl["compliant"]["realized_pnl"], -1500.0)
        self.assertAlmostEqual(pnl["violated"]["realized_pnl"] + pnl["compliant"]["realized_pnl"], 0.0)
        self.assertEqual(pnl["violated"]["win_rate_pct"], 100.0)
        self.assertEqual(pnl["compliant"]["win_rate_pct"], 0.0)
        self.assertEqual(pnl["violated"]["closed_shares"], 300)
        self.assertEqual(pnl["compliant"]["closed_shares"], 300)
        self.assertIsNone(pnl["unattributed"])

    def test_fee_is_split_proportionally(self):
        """卖出费用按两组股数占比分摊：980 + (−1530) = −550 = 台账 realized。"""
        hit = self._hit()
        self.write_ledger([
            self._row("B1", "600000.SH", "buy", 400, 10, 4000, override=hit),
            self._row("B2", "600000.SH", "buy", 600, 20, 12000),
            self._row("S1", "600000.SH", "sell", 500, 15, 7500, fee=50),
        ])
        pnl = self.make().discipline_summary(days=30)["pnl_by_adherence"]
        self.assertEqual(pnl["violated"]["realized_pnl"], 980.0)
        self.assertEqual(pnl["compliant"]["realized_pnl"], -1530.0)
        self.assertAlmostEqual(pnl["violated"]["realized_pnl"] + pnl["compliant"]["realized_pnl"], -550.0)
        self.assertTrue(pnl["sufficient"])

    def test_sell_outside_window_yields_no_numbers(self):
        """口径：按卖出落袋日期计入窗口；窗口外的不出数，不编造。"""
        hit = self._hit()
        old = str(datetime.date.today() - datetime.timedelta(days=40))
        self.write_ledger([
            self._row("B1", "600000.SH", "buy", 400, 10, 4000, override=hit, day=old),
            self._row("B2", "600000.SH", "buy", 600, 20, 12000, day=old),
            self._row("S1", "600000.SH", "sell", 500, 15, 7500, day=old),
        ])
        pnl = self.make().discipline_summary(days=30)["pnl_by_adherence"]
        self.assertFalse(pnl["sufficient"])
        self.assertIsNone(pnl["violated"])
        self.assertIsNone(pnl["compliant"])

    def test_adjust_resets_attribution_to_unattributed(self):
        """持仓纠正后成本无法归因：剩余持仓单列，不混进守规/越权。"""
        hit = self._hit()
        self.write_ledger([
            self._row("B1", "600000.SH", "buy", 500, 10, 5000, override=hit),
            self._row("ADJ", "600000.SH", "adjust", 200, None, None,
                      cost_before=10.0, cost_after=10.0),
            self._row("S1", "600000.SH", "sell", 200, 12, 2400),
        ])
        pnl = self.make().discipline_summary(days=30)["pnl_by_adherence"]
        self.assertFalse(pnl["sufficient"])
        self.assertIsNone(pnl["violated"])
        self.assertIsNone(pnl["compliant"])
        self.assertEqual(pnl["unattributed"]["realized_pnl"], 400.0)  # (12−10)×200

    def test_legacy_minimal_rows_do_not_crash(self):
        """历史行缺 price/shares 字段时不出数但不报错（兼容旧台账）。"""
        self.write_ledger(self._minimal_rows())
        d = self.make().discipline_summary(days=30)
        pnl = d["pnl_by_adherence"]
        self.assertFalse(pnl["sufficient"])
        self.assertIn("scope", pnl)

    def _minimal_rows(self):
        # 与 TestDisciplineSummary._rows 相同的极简结构（无 price/amount/shares 语义）
        hit = self._hit()
        return [
            {"id": "T1", "created_at": f"{self.today} 10:00:00", "trade_date": self.today,
             "secID": "600000.SH", "name": "测试A", "side": "buy", "rule_override": hit},
            {"id": "T2", "created_at": f"{self.today} 11:00:00", "trade_date": self.today,
             "secID": "000001.SZ", "name": "测试B", "side": "buy"},
            {"id": "T3", "created_at": f"{self.today} 12:00:00", "trade_date": self.today,
             "secID": "600000.SH", "name": "测试A", "side": "sell"},
        ]


class TestDisciplineRoute(unittest.TestCase):
    """路由接线：接口存在、days 透传、越权写审计明细、直接调用兼容。"""

    def _client(self):
        captured = []
        app = FastAPI()
        app.include_router(stock_router.router)

        @app.middleware("http")
        async def capture(request, call_next):
            response = await call_next(request)
            captured.append(getattr(request.state, "audit_detail", ""))
            return response

        return TestClient(app), captured

    def test_get_discipline_passes_days(self):
        client, _ = self._client()
        with mock.patch.object(stock_router.trade_ledger, "discipline_summary",
                               return_value={"ok": True, "days": 7}) as summary:
            res = client.get("/api/stock/discipline?days=7")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["days"], 7)
        summary.assert_called_once_with(days=7)

    def test_force_trade_writes_audit_detail(self):
        client, captured = self._client()
        override = {
            "pool": "dividend", "codes": ["max_single"],
            "violations": [{"code": "max_single", "label": "单票上限", "message": "超了"}],
            "issues": ["超了"],
        }
        fake_result = {"ok": True, "record": {"secID": "600000.SH"},
                       "rule_override": override, "warnings": []}
        with mock.patch.object(stock_router.trade_ledger, "apply_trade", return_value=fake_result), \
             mock.patch.object(stock_router.stock_service, "get_account_snapshot", return_value={}):
            res = client.post("/api/stock/trade", json={"code": "600000", "side": "buy",
                                                        "shares": 900, "price": 10, "force": True})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(captured[0])
        self.assertIn("单票上限", captured[0])
        self.assertIn("600000.SH", captured[0])
        # 复用统一审计结构，ops.js 能直接渲染
        self.assertIn("changes", json.loads(captured[0]))
        self.assertIsNotNone(res.json()["rule_override"])

    def test_clean_trade_has_no_audit_detail(self):
        client, captured = self._client()
        fake_result = {"ok": True, "record": {"secID": "000001.SZ"},
                       "rule_override": None, "warnings": []}
        with mock.patch.object(stock_router.trade_ledger, "apply_trade", return_value=fake_result), \
             mock.patch.object(stock_router.stock_service, "get_account_snapshot", return_value={}):
            client.post("/api/stock/trade", json={"code": "000001", "side": "buy",
                                                  "shares": 100, "price": 10})
        self.assertEqual(captured[0], "")

    def test_direct_call_without_request_still_works(self):
        """服务内部/旧脚本可能直接调路由函数，不能因为加了 Request 就炸。"""
        fake_result = {"ok": True, "record": {"secID": "000001.SZ"},
                       "rule_override": None, "warnings": []}
        with mock.patch.object(stock_router.trade_ledger, "apply_trade", return_value=fake_result), \
             mock.patch.object(stock_router.stock_service, "get_account_snapshot", return_value={}):
            out = stock_router.record_trade({"code": "000001", "side": "buy"})
        self.assertTrue(out["ok"])


if __name__ == "__main__":
    unittest.main()
