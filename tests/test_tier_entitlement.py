"""M1 分层底座：users.tier 迁移 + 单点 entitlement() 判定。

约束（roadmap.md M1 / positioning.md 硬约束）：
- 只加字段与判定，不改任何业务路由行为；
- 判定单点、fail closed；本地纪律能力永不查这里。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.core import auth
from app.db import database, store
from tests import assert_isolated


class TierMigrationTest(unittest.TestCase):
    """老库补列：tier 默认 free，且不触碰原有列。"""

    def test_old_db_gets_tier_column_default_free(self):
        assert_isolated()
        with tempfile.TemporaryDirectory(prefix="tier_migrate_") as d:
            path = Path(d) / "old.db"
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            # M1 之前的 users 表结构（无 tier）
            conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "username TEXT NOT NULL UNIQUE, salt TEXT NOT NULL, "
                "password_hash TEXT NOT NULL, created_at TEXT NOT NULL, "
                "is_active INTEGER NOT NULL DEFAULT 1)"
            )
            conn.execute(
                "INSERT INTO users(username, salt, password_hash, created_at, is_active) "
                "VALUES('old_user', 's', 'h', '2026-01-01', 1)"
            )
            conn.commit()
            database._apply_column_migrations(conn)
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            self.assertIn("tier", cols, "老库应补上 tier 列")
            row = conn.execute("SELECT tier, username, is_active FROM users").fetchone()
            self.assertEqual(row["tier"], "free")
            self.assertEqual(row["username"], "old_user")
            self.assertEqual(row["is_active"], 1)
            conn.close()

    def test_migration_is_idempotent(self):
        assert_isolated()
        with tempfile.TemporaryDirectory(prefix="tier_migrate_") as d:
            conn = sqlite3.connect(str(Path(d) / "x.db"))
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, "
                "salt TEXT, password_hash TEXT, created_at TEXT, is_active INTEGER)"
            )
            database._apply_column_migrations(conn)
            database._apply_column_migrations(conn)
            cols = [row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
            self.assertEqual(cols.count("tier"), 1, "重复迁移不应加出第二列")
            conn.close()


class EntitlementTest(unittest.TestCase):
    """单点判定：只判服务资格，未知一律 False，且本地功能不查这里。"""

    def test_new_user_defaults_to_free(self):
        assert_isolated()
        user = auth.create_user("tier_free_user", "StrongPass123")
        try:
            self.assertEqual(auth.user_tier(user["id"]), auth.TIER_FREE)
        finally:
            store.set_user_active(user["id"], False)

    def test_free_user_has_no_service_entitlement(self):
        assert_isolated()
        user = auth.create_user("tier_no_service", "StrongPass123")
        try:
            for cap in ("hosted_agent", "licensed_data", "managed_deploy", "anything"):
                self.assertFalse(auth.entitlement(user["id"], cap))
        finally:
            store.set_user_active(user["id"], False)

    def test_fail_closed_on_unknown_user_and_capability(self):
        assert_isolated()
        self.assertFalse(auth.entitlement(None, "hosted_agent"))
        self.assertFalse(auth.entitlement(999999, "hosted_agent"))
        self.assertFalse(auth.entitlement(1, ""))

    def test_unknown_tier_grants_nothing(self):
        """手滑把 tier 写成未登记值，不能意外获得任何服务。"""
        assert_isolated()
        self.assertEqual(auth.user_tier.__name__, "user_tier")
        # 直接改库模拟未知 tier，再验证判定 fail closed
        user = auth.create_user("tier_unknown", "StrongPass123")
        try:
            with store.transaction() as conn:
                conn.execute("UPDATE users SET tier='superadmin' WHERE id=?", (int(user["id"]),))
            self.assertEqual(auth.user_tier(user["id"]), "superadmin")
            self.assertFalse(auth.entitlement(user["id"], "hosted_agent"))
        finally:
            store.set_user_active(user["id"], False)

    def test_entitlement_is_single_source_of_truth(self):
        """判定表只有一份；所有 tier 当前都无服务资格（M2 未拍板）。"""
        self.assertIn(auth.TIER_FREE, auth._SERVICE_ENTITLEMENTS)
        self.assertEqual(auth._SERVICE_ENTITLEMENTS[auth.TIER_FREE], frozenset())


if __name__ == "__main__":
    unittest.main()
