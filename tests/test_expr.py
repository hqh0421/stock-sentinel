"""安全表达式引擎：白名单、归一化、与监控/回测口径一致。"""

import unittest

from app.core.expr import (
    check_expr, eval_signal_expr, evaluate, normalize_expr, parse_expr,
    build_kline_env, MAX_EXPR_LEN,
)
from tests import assert_isolated


def _series(n=40, cross=False):
    rows = []
    for i in range(n):
        close = 10 + i * 0.5
        rows.append({
            "date": f"d{i:03d}", "close": close, "open": close,
            "high": close + 0.4, "low": close - 0.4,
            "MA20": close - 1, "MA60": close - 2, "RSI6": 25,
            "DIF": (0.4 if cross and i == n - 1 else 0.2),
            "DEA": 0.3,
        })
    if cross:
        rows[-2]["DIF"] = 0.2
    return rows


class TestExprSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_isolated()

    def test_normal_expr_still_works(self):
        s = _series()
        self.assertTrue(eval_signal_expr("MA20 > MA60", s, 39))
        self.assertTrue(eval_signal_expr("close > LLV('close', 20)", s, 39))
        self.assertTrue(eval_signal_expr("ATR(14) > 0", s, 39))
        self.assertTrue(eval_signal_expr("MA20 > MA60 or RSI6 < 30", s, 39))

    def test_cross_up_detected(self):
        s = _series(cross=True)
        self.assertTrue(eval_signal_expr("CROSS(DIF, DEA)", s, 39))
        self.assertFalse(eval_signal_expr("CROSS(DIF, DEA)", _series(), 39))

    def test_normalize_is_idempotent(self):
        once = normalize_expr("MA(close, 5) > CROSS(DIF, DEA)")
        twice = normalize_expr(once)
        self.assertEqual(once, twice)
        self.assertIn("'close'", once)

    def test_reject_sandbox_escape(self):
        for bad in (
            "__import__('os').system('id')",
            "(1).__class__.__mro__",
            "[x for x in range(3)]",
            "lambda: 1",
            "10**10**10",
            "close.__class__",
            "'abc'[0]",
        ):
            ok, reason = check_expr(bad)
            self.assertFalse(ok, f"{bad!r} 不应通过（{reason}）")

    def test_reject_too_long_and_too_complex(self):
        ok, reason = check_expr("close > 0" + " and close > 0" * 50)
        self.assertFalse(ok)
        self.assertIn("过长", reason)
        self.assertLessEqual(MAX_EXPR_LEN, 500)

    def test_lookback_is_capped(self):
        s = _series()
        # 不因超大回看窗口卡死，且结果与限幅后一致
        self.assertEqual(evaluate("MA('close', 999999999)", build_kline_env(s, 39)),
                         evaluate("MA('close', 250)", build_kline_env(s, 39)))

    def test_invalid_syntax_reports_reason(self):
        ok, reason = check_expr("close >>>> 1")
        self.assertFalse(ok)
        self.assertTrue(reason)


if __name__ == "__main__":
    unittest.main()
