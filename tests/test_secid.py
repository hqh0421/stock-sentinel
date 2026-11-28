"""代码归一化的单元测试（app/core/secid.py）。

重点锁死一条历史 bug：不能把 9 开头一律当沪市。
全市场代码表里 9 开头是北交所 920xxx（2024 年起启用），
误存成 920000.SH 会永远查不到行情。
"""
import unittest

from app.core.secid import normalize_secid, split_secid


class TestSplitSecid(unittest.TestCase):
    def test_plain_digits_infers_market(self):
        cases = {
            "600000": "sh",   # 沪主板
            "601036": "sh",   # 沪主板
            "688111": "sh",   # 科创板
            "000001": "sz",   # 深主板
            "002230": "sz",   # 深中小
            "300750": "sz",   # 创业板
            "301001": "sz",
            "430047": "bj",   # 北交所旧段
            "830799": "bj",
            "870508": "bj",
            "920001": "bj",   # 北交所新段，历史 bug 点
        }
        for code, market in cases.items():
            with self.subTest(code=code):
                parsed = split_secid(code)
                self.assertEqual(parsed["digits"], code)
                self.assertEqual(parsed["market"], market)

    def test_nine_prefix_is_not_always_shanghai(self):
        """920xxx 必须归北交所；900xxx 才是沪 B。"""
        self.assertEqual(split_secid("920001")["market"], "bj")
        self.assertEqual(split_secid("920999")["market"], "bj")
        self.assertEqual(split_secid("900901")["market"], "sh")

    def test_suffix_and_prefix_forms(self):
        cases = {
            "600000.SH": ("600000", "sh"),
            "000001.SZ": ("000001", "sz"),
            "601036.SH": ("601036", "sh"),
            "sz.300750": ("300750", "sz"),
            "sh600000": ("600000", "sh"),
            "bj430047": ("430047", "bj"),
            "sse600000": ("600000", "sh"),
            "szse000001": ("000001", "sz"),
            "  600000  ": ("600000", "sh"),
        }
        for code, (digits, market) in cases.items():
            with self.subTest(code=code):
                parsed = split_secid(code)
                self.assertEqual(parsed["digits"], digits)
                self.assertEqual(parsed["market"], market)

    def test_invalid_raises(self):
        for bad in ("", "  ", "12345", "abcdef", "6000000", None):
            with self.subTest(code=bad):
                with self.assertRaises(ValueError):
                    split_secid(bad)


class TestNormalizeSecid(unittest.TestCase):
    def test_returns_digits_upper_market(self):
        self.assertEqual(normalize_secid("000001"), "000001.SZ")
        self.assertEqual(normalize_secid("600000.SH"), "600000.SH")
        self.assertEqual(normalize_secid("920001"), "920001.BJ")

    def test_invalid_returns_none(self):
        self.assertIsNone(normalize_secid("abc"))
        self.assertIsNone(normalize_secid(""))


class TestStockServiceDelegates(unittest.TestCase):
    """stock_service 的私有方法必须与单一真源保持一致。"""

    def test_delegates_to_secid_module(self):
        from app.services.stock_service import stock_service

        for code in ("600000", "000001", "920001", "sz.300750"):
            with self.subTest(code=code):
                self.assertEqual(
                    stock_service._normalize_stock_code(code),
                    split_secid(code),
                )


if __name__ == "__main__":
    unittest.main()
