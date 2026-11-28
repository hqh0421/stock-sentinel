"""完整应用启动冒烟验证（生产库零接触）。

历史事故：手动 `import app.main` 做启动验证时，`ensure_initialized()` 会作用在默认库
`config/app.db` 上——业务数据不变，但会清理过期会话并改动文件时间。根因是「隔离靠人肉约定」。

本脚本把隔离变成框架保证：**直接复用 `tests/__init__.py` 的隔离底座**，不做第二份实现。
`import tests` 在包导入阶段就把 DB / 模型配置 / APP_SECRET 重定向到临时目录，
随后 `tests.assert_isolated()` 断言确实没落在默认库上，才允许导入 `app.main`。

用法（仓库根目录执行）：

    .venv/Scripts/python.exe scripts/verify_startup.py

说明：只做冒烟验证，行情源预热被替换为 no-op，避免真实外呼与多进程噪声；
调度器 tick 为 30s，冒烟窗口内不会触发真实 Agent / 钉钉任务。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 复用唯一隔离底座：导入即重定向，随后断言
import tests  # noqa: E402,F401  (副作用：设置隔离环境变量)

tests.assert_isolated()

from fastapi.testclient import TestClient  # noqa: E402

from app.db import database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.stock_service import stock_service  # noqa: E402

# 行情预热会启动多进程池并真实请求行情站点；冒烟验证不需要，替换为 no-op
stock_service.prewarm_quote_sources = lambda: None

REQUIRED_ROUTES = (
    "/api/stock/source_health",
    "/api/stock/decision_rules",
    "/api/stock/factor_validation",
    "/api/stock/risk_guard",
    "/api/stock/pools",
    "/api/stock/risk_switches",
    "/api/stock/discipline",
    "/api/stock/allocation",
    "/api/stock/allocation/classify/suggest",
    "/api/stock/proposals",
)


def main() -> int:
    print(f"实际使用数据库: {database.db_path()}")
    assert database.db_path().resolve() != database.DEFAULT_DB_PATH.resolve(), "生产库隔离失效"

    checks: list[tuple[str, bool, str]] = []
    with TestClient(app) as client:
        health = client.get("/api/health")
        checks.append(("health=200", health.status_code == 200, str(health.status_code)))

        anonymous = client.get("/api/stock/favorites")
        checks.append(("未登录受保护接口=401", anonymous.status_code == 401,
                       str(anonymous.status_code)))

        login = client.post("/api/auth/login", json={
            "username": "admin",
            "password": os.environ["APP_PASSWORD"],
        })
        checks.append(("login=200", login.status_code == 200, str(login.status_code)))

        authorized = client.get("/api/stock/favorites")
        checks.append(("登录后受保护接口=200", authorized.status_code == 200,
                       str(authorized.status_code)))

        paths = {r.path for r in app.routes if getattr(r, "path", "").startswith("/api/")}
        for route in REQUIRED_ROUTES:
            checks.append((f"路由 {route}", route in paths, "已注册" if route in paths else "缺失"))

    for label, ok, detail in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {label}  ({detail})")

    failed = [label for label, ok, _ in checks if not ok]
    print(f"启动验证：{len(checks) - len(failed)}/{len(checks)} 通过；生产库未被触碰。")
    if failed:
        print("失败项：" + "、".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
