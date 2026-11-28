"""SQLite 连接与建表。

为什么用标准库 sqlite3 而不是 SQLAlchemy
----------------------------------------
- 部署方式是「本地构建镜像 → docker commit → 服务器只部署不构建」，服务器上
  没有 pip/编译环境。新增第三方依赖要重做镜像，而 sqlite3 是标准库，零依赖。
- 单文件数据库随 ``config`` 卷持久化，备份就是拷一个文件，运维成本最低。

并发策略
--------
- WAL 模式 + ``busy_timeout``，读写不互相阻塞；
- 每次操作独立连接（短事务），不跨线程共享连接对象；
- 写操作显式 ``BEGIN IMMEDIATE``，避免两个写事务同时升级锁导致 SQLITE_BUSY。

数据库文件默认 ``config/app.db``，可用环境变量 ``APP_DB_PATH`` 覆盖（测试用）。
"""
from __future__ import annotations

import os
import sqlite3
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = BASE_DIR / "config" / "app.db"

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT    NOT NULL DEFAULT '',
    salt          TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1,
    -- 套餐层级：默认 free。只标记服务资格，不参与任何业务判断（见 core/auth.entitlement）
    tier          TEXT    NOT NULL DEFAULT 'free'
);

CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expiry     INTEGER NOT NULL,
    created_at TEXT    NOT NULL,
    user_agent TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- 设置存 JSON：字段随业务演进，不值得为每个开关改表结构
CREATE TABLE IF NOT EXISTS user_settings (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data       TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

-- 自选 + 持仓（同一张表：自选股 shares=0 即为只观察）
CREATE TABLE IF NOT EXISTS favorites (
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    secID    TEXT    NOT NULL,
    shares   INTEGER NOT NULL DEFAULT 0,
    cost     REAL    NOT NULL DEFAULT 0,
    horizon  TEXT    NOT NULL DEFAULT 'long',
    ordinal  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, secID)
);

-- 交易流水：常用筛选列单独建列，完整记录仍存 JSON（含费用明细、前后持仓）
CREATE TABLE IF NOT EXISTS trades (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    id         TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    trade_date TEXT    NOT NULL DEFAULT '',
    secID      TEXT    NOT NULL DEFAULT '',
    side       TEXT    NOT NULL DEFAULT '',
    payload    TEXT    NOT NULL,
    PRIMARY KEY (user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_trades_user_date ON trades(user_id, trade_date);

CREATE TABLE IF NOT EXISTS strategies (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    entry_expr TEXT    NOT NULL DEFAULT '',
    exit_expr  TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT '',
    updated_at TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, name)
);

CREATE TABLE IF NOT EXISTS alert_rules (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data       TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

-- 告警推送历史：推了什么、推给谁、成没成，出问题能回查（v2）
CREATE TABLE IF NOT EXISTS alert_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL,
    channel    TEXT    NOT NULL DEFAULT '',
    title      TEXT    NOT NULL DEFAULT '',
    content    TEXT    NOT NULL DEFAULT '',
    item_count INTEGER NOT NULL DEFAULT 0,
    ok         INTEGER NOT NULL DEFAULT 0,
    error      TEXT    NOT NULL DEFAULT '',
    source     TEXT    NOT NULL DEFAULT 'auto'
);
CREATE INDEX IF NOT EXISTS idx_alert_log_user_time ON alert_log(user_id, created_at DESC);

-- Agent 对话历史：刷新页面/换设备后还能接着聊（v2）
CREATE TABLE IF NOT EXISTS agent_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id TEXT    NOT NULL,
    role            TEXT    NOT NULL,
    content         TEXT    NOT NULL DEFAULT '',
    meta            TEXT    NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_msg_user_conv ON agent_messages(user_id, conversation_id, id);

-- 写操作审计：谁在什么时候改了仓/改了配置（v2）
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    created_at TEXT    NOT NULL,
    method     TEXT    NOT NULL DEFAULT '',
    path       TEXT    NOT NULL DEFAULT '',
    status     INTEGER NOT NULL DEFAULT 0,
    ip         TEXT    NOT NULL DEFAULT '',
    detail     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_user_time ON audit_log(user_id, created_at DESC);

-- 建议单：Agent 建议 → 用户采纳（第一道）→ 二次确认执行（第二道）
-- 不自动下单：只有 execute 且 confirm=true 才允许写台账
CREATE TABLE IF NOT EXISTS agent_proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL DEFAULT 'rebalance',
    status      TEXT    NOT NULL DEFAULT 'proposed',
    title       TEXT    NOT NULL DEFAULT '',
    payload     TEXT    NOT NULL DEFAULT '{}',
    analysis    TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    accepted_at TEXT    NOT NULL DEFAULT '',
    executed_at TEXT    NOT NULL DEFAULT '',
    ignored_at  TEXT    NOT NULL DEFAULT '',
    note        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_proposals_user_time ON agent_proposals(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 市场情绪日度快照（v5）。全局数据，与用户无关：同一市场同一交易日只有一条。
-- 为什么进表而不是 JSON：它是**追加型时序**（每交易日一条、只留最近 90 条），
-- 文件存法每次都要把整份读出来再整份重写，且没有索引可用；
-- 表结构下一次写入 + 一次裁剪就够，查历史分位也只扫 90 行。
CREATE TABLE IF NOT EXISTS sentiment_snapshots (
    trade_date  TEXT PRIMARY KEY,
    zt_count    INTEGER,
    dt_count    INTEGER,
    max_board   INTEGER,
    seal_rate   REAL,
    temperature REAL,
    phase       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sentiment_date ON sentiment_snapshots(trade_date DESC);

-- 时间止损豁免（v6）。属于「用户决策记录」，不是缓存：要能事后复盘
-- 「当时为什么决定不砍」。每轮监控每只持仓都会查一次，放文件的话
-- 挂载盘上每次读要 1.9 毫秒，六只票就是十几毫秒，纯浪费。
-- 注：当前是全局表（项目实际单用户）。若要多用户，需加 user_id 并处理
-- 调度线程的 contextvars（后台线程不继承用户上下文）。
CREATE TABLE IF NOT EXISTS time_stop_overrides (
    sec_id     TEXT PRIMARY KEY,
    reason     TEXT NOT NULL DEFAULT '',
    basis      TEXT NOT NULL DEFAULT '',
    created_on TEXT NOT NULL DEFAULT '',
    expires_on TEXT NOT NULL DEFAULT '',
    days       INTEGER NOT NULL DEFAULT 0,
    meta       TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""

# 老库补列：全部用 ADD COLUMN，SQLite 不支持 DROP COLUMN 也无所谓（加列很便宜）
_COLUMN_MIGRATIONS = {
    "users": [
        # M1 分层底座：只标记服务资格，默认 free，不参与任何业务判断
        ("tier", "TEXT NOT NULL DEFAULT 'free'"),
    ],
    "favorites": [
        ("group_name", "TEXT NOT NULL DEFAULT ''"),
        ("note", "TEXT NOT NULL DEFAULT ''"),
        # 四池分类：dividend / catalyst / long_track / value；空串=未分类
        ("pool", "TEXT NOT NULL DEFAULT ''"),
    ],
    # 表达式因子（回测策略）扩展字段：描述、回测参数、最近一次回测指标快照
    "strategies": [
        ("description", "TEXT NOT NULL DEFAULT ''"),
        ("benchmark", "TEXT NOT NULL DEFAULT 'hs300'"),
        ("capital", "REAL NOT NULL DEFAULT 100000"),
        ("fee_rate", "REAL NOT NULL DEFAULT 0.00075"),
        ("stamp_tax_rate", "REAL NOT NULL DEFAULT 0.0005"),
        ("slippage_pct", "REAL NOT NULL DEFAULT 0.001"),
        ("position_pct", "REAL NOT NULL DEFAULT 1.0"),
        ("metrics_json", "TEXT NOT NULL DEFAULT ''"),
    ],
}

_init_lock = threading.Lock()
_initialized = False


def db_path() -> Path:
    override = os.environ.get("APP_DB_PATH", "").strip()
    return Path(override) if override else DEFAULT_DB_PATH


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # busy_timeout 必须**先设**：切 WAL 需要短暂的独占锁，没设等待时间时
    # 一遇到别的连接持有锁就直接抛 "unable to open database file"，
    # 而这个错误看起来像路径问题，极难排查（实测在挂载盘上偶发）。
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        # 切不过去就退回默认日志模式：功能受影响（并发读），但不能因此连不上库
        logger.warning("db_wal_enable_failed error=%r", exc)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    """给老库补新增列。幂等：列已存在就跳过。"""
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not existing:
            continue
        for name, ddl in columns:
            if name in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            except sqlite3.OperationalError:
                # 并发启动时另一个进程刚好加过，忽略
                pass


def init_db(force: bool = False) -> None:
    """幂等建表。首次调用或 force=True 时执行。"""
    global _initialized
    with _init_lock:
        if _initialized and not force:
            return
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            _apply_column_migrations(conn)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
        finally:
            conn.close()
        _initialized = True


_local = threading.local()


def _thread_conn() -> sqlite3.Connection:
    """复用当前线程的连接。

    ⚠️ 别改成「每次调用新建连接」：config/ 目录在容器里是 Windows 挂载盘，
    实测新建连接 + 一次查询要 **22 毫秒**（本地盘只要 0.01 毫秒），
    而一次页面加载会打十几次 store，光连接开销就能吃掉两三百毫秒。
    复用后同一次查询降到 **0.5 毫秒**。
    线程本地是因为 sqlite3.Connection 默认不能跨线程共享；线程数由
    FastAPI 线程池与调度器决定，数量有限，不会无限增长。
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    init_db()
    # 不 close：连接随线程存活，由 WAL + autocommit 保证一致性
    yield _thread_conn()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """写事务。BEGIN IMMEDIATE 先拿写锁，避免读事务升级锁时互相踩。"""
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def reset_for_tests() -> None:
    """仅测试用：删除数据库文件并重置初始化标记。"""
    global _initialized
    with _init_lock:
        # 先关掉线程缓存连接，否则 Windows 上文件被占用删不掉，
        # 且后续操作会继续用已删除的旧句柄
        conn = getattr(_local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            _local.conn = None
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(db_path()) + suffix)
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        _initialized = False


def get_meta(key: str, default: Optional[str] = None) -> Optional[str]:
    with connection() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with transaction() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
