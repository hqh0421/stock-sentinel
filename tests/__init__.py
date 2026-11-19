"""测试隔离底座：任何测试都不允许碰到生产数据库或生产配置。

历史事故（提交 6be3afe）是「记账测试写穿生产库」。根因是测试隔离靠人肉约定，
没有框架保证。这里在包导入阶段就强制把 DB / 模型配置重定向到临时目录，
并提供 `assert_isolated()` 供每个测试类自检。

用标准库 unittest 编写（pytest 也能直接发现并运行），因为项目 venv 刻意保持零
测试依赖；系统 Python 与 venv 版本不同，混用 site-packages 会加载错 C 扩展。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="monitor_stock_tests_"))

os.environ["APP_DB_PATH"] = str(_TEST_ROOT / "app.db")
os.environ["APP_SECRET"] = "test-only-secret"
os.environ["APP_PASSWORD"] = "test-only-password"
os.environ["MONITOR_STOCK_MODELS_FILE"] = str(_TEST_ROOT / "models.json")
# 决策规则用项目里的真实文件（不含凭证），保证测的就是线上口径
os.environ.setdefault("MONITOR_STOCK_RULES_FILE", str(Path(__file__).resolve().parent.parent / "config" / "decision_rules.json"))

TEST_ROOT = _TEST_ROOT


def assert_isolated() -> None:
    """断言当前进程确实跑在临时库上；任何测试类 setUpClass 都该调一次。"""
    from app.db import database

    default = database.DEFAULT_DB_PATH.resolve()
    actual = database.db_path().resolve()
    if actual == default:
        raise RuntimeError("测试隔离失效：APP_DB_PATH 未生效，会写穿生产库！")
    if "monitor_stock_tests_" not in str(actual):
        raise RuntimeError(f"测试隔离失效：数据库路径异常 {actual}")
