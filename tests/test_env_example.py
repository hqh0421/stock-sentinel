"""验证环境变量模板覆盖应用实际读取的配置项。"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = ROOT / "app"
ENV_EXAMPLE = ROOT / ".env.example"
ENV_NAME_RE = re.compile(r"\bMONITOR_STOCK_[A-Z0-9_]+\b")


class EnvExampleCoverageTest(unittest.TestCase):
    def test_covers_all_monitor_stock_environment_variables(self):
        source_names = set()
        for path in APP_ROOT.rglob("*.py"):
            source_names.update(ENV_NAME_RE.findall(path.read_text(encoding="utf-8")))

        template_names = set(ENV_NAME_RE.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))

        self.assertTrue(source_names, "app/ 中应至少读取一个 MONITOR_STOCK_* 环境变量")
        self.assertEqual(
            template_names,
            source_names,
            "环境变量模板必须覆盖 app/ 中实际读取的全部 MONITOR_STOCK_* 变量",
        )


if __name__ == "__main__":
    unittest.main()
