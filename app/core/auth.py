"""多用户会话鉴权（SQLite 用户表 + 会话表）。

设计要点
--------
- 口令：PBKDF2-HMAC-SHA256 20 万轮，每用户独立盐；数据库只存盐与哈希。
- 会话：令牌 ``<sid>.<expiry>.<uid>.<sig>``，HMAC-SHA256 签名；同时会话落库，
  因此**可以主动吊销**（登出、改密、停用账号），不再是「签出去就收不回」。
- 签名密钥：环境变量 ``APP_SECRET``，否则持久化在 ``config/auth.json``（随卷保留）。
- 首个用户：从历史单用户配置引导。优先级 APP_PASSWORD > auth.json 里的口令哈希；
  两者都没有就随机生成一次并打印到日志。引导时把 favorites / settings / trade_log /
  strategies / alert_rules 一并迁移到该用户（见 ``app/db/migrate.py``）。
- 防爆破：按用户名分别计数 + 全局兜底计数，指数退避（前 5 次不惩罚，5s→…→300s），
  退避期间登录接口直接 429，不做口令校验。
- 时序防护：用户名不存在时也跑一次 PBKDF2，避免用响应时间枚举账号。
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..db import store
from ..db.database import init_db
from ..db.migrate import migrate_legacy_json

logger = logging.getLogger("app.auth")

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
AUTH_FILE = CONFIG_DIR / "auth.json"

SESSION_COOKIE = "ms_session"
SESSION_TTL = 60 * 60 * 24 * 7  # 7 天
PBKDF2_ROUNDS = 200_000

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fa5]{2,32}$")
MIN_PASSWORD_LEN = 8

# ---- 登录失败退避 -----------------------------------------------------------
FAIL_FREE = 5
FAIL_BASE = 5.0
FAIL_MAX = 300.0
FAIL_RESET = 600.0
GLOBAL_KEY = "*"
GLOBAL_FREE = FAIL_FREE * 6      # 跨用户名喷射：30 次后才开始罚
MAX_FAIL_ENTRIES = 500

_fail_lock = threading.Lock()
_fail_states: Dict[str, Dict[str, float]] = {}

_secret_lock = threading.Lock()
_secret_cache: Optional[str] = None


def now_text() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 口令哈希

def _pbkdf2(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ROUNDS
    ).hex()


def hash_password(password: str) -> Tuple[str, str]:
    salt = secrets.token_hex(16)
    return salt, _pbkdf2(password, salt)


def validate_username(username: str) -> Optional[str]:
    name = str(username or "").strip()
    if not name:
        return "请输入用户名"
    if not USERNAME_RE.match(name):
        return "用户名需为 2-32 位字母、数字、下划线、连字符或中文"
    return None


def validate_password(password: str) -> Optional[str]:
    if not password:
        return "请输入口令"
    if len(password) < MIN_PASSWORD_LEN:
        return f"口令至少 {MIN_PASSWORD_LEN} 位"
    if len(password) > 128:
        return "口令过长"
    return None


def allow_register() -> bool:
    return os.environ.get("APP_ALLOW_REGISTER", "0").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- 签名密钥

def _load_secret() -> str:
    global _secret_cache
    with _secret_lock:
        if _secret_cache:
            return _secret_cache
        env_secret = os.environ.get("APP_SECRET", "").strip()
        if env_secret:
            _secret_cache = env_secret
            return env_secret

        data: Dict[str, Any] = {}
        if AUTH_FILE.exists():
            try:
                data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        secret = str(data.get("secret") or "").strip()
        if not secret:
            secret = secrets.token_hex(32)
            data["secret"] = secret
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                AUTH_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                try:
                    os.chmod(AUTH_FILE, 0o600)
                except Exception:
                    pass
            except Exception as exc:  # pragma: no cover
                logger.error("写入 auth.json 失败: %s", exc)
        _secret_cache = secret
        return secret


# ---------------------------------------------------------------- 初始化 / 引导

def ensure_initialized() -> Dict[str, Any]:
    """建库、加载密钥、必要时创建首个用户并迁移历史配置。幂等。"""
    init_db()
    _load_secret()
    created = False
    if store.count_users() == 0:
        created = _bootstrap_first_user()
    try:
        store.purge_expired_sessions(int(time.time()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理过期会话失败: %r", exc)
    return {"bootstrapped": created, "users": store.count_users()}


def _bootstrap_first_user() -> bool:
    legacy: Dict[str, Any] = {}
    if AUTH_FILE.exists():
        try:
            legacy = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        except Exception:
            legacy = {}

    username = (os.environ.get("APP_USERNAME") or "admin").strip() or "admin"
    env_pw = os.environ.get("APP_PASSWORD", "")

    if env_pw:
        salt = str(legacy.get("salt") or "") or secrets.token_hex(16)
        if not legacy.get("salt") or not legacy.get("password_hash"):
            salt = secrets.token_hex(16)
        pwd_hash = _pbkdf2(env_pw, salt)
        source = "APP_PASSWORD"
    elif legacy.get("salt") and legacy.get("password_hash"):
        salt = str(legacy["salt"])
        pwd_hash = str(legacy["password_hash"])
        source = "config/auth.json"
    else:
        generated = secrets.token_urlsafe(12)
        salt, pwd_hash = hash_password(generated)
        source = "随机生成"
        logger.warning(
            "未设置 APP_PASSWORD，已为初始用户 %s 随机生成口令（请妥善保存）：%s", username, generated
        )

    try:
        user_id = store.create_user(username, salt, pwd_hash, created_at=now_text())
    except Exception as exc:  # noqa: BLE001  并发启动时可能已被别的进程创建
        existing = store.get_user_by_username(username)
        if not existing:
            logger.error("创建首个用户失败: %r", exc)
            return False
        user_id = int(existing["id"])

    try:
        stats = migrate_legacy_json(user_id, CONFIG_DIR, now_text())
    except Exception as exc:  # noqa: BLE001
        stats = {}
        logger.error("历史配置迁移失败（不影响启动）: %r", exc)

    logger.info(
        "首个用户已创建 username=%s source=%s migrated=%s", username, source, stats or "无"
    )
    return True


# ---------------------------------------------------------------- 用户

def get_user_public(user: Dict[str, Any]) -> Dict[str, Any]:
    uid = int(user.get("id") or 0)
    return {
        "id": uid,
        "username": str(user.get("username") or ""),
        "display_name": str(user.get("display_name") or ""),
        "created_at": str(user.get("created_at") or ""),
        # 前端据此决定是否显示「备份/建号/数据源」等管理员入口
        "is_admin": is_admin(uid),
    }


def is_admin(user_id: Optional[int]) -> bool:
    """首个用户（引导创建的那个）视为管理员。

    只用于「实例级共享配置」的写权限（模型 Key、雪球 Cookie、建号），
    不参与用户数据的读写隔离——那部分由 user_id 分桶保证。
    """
    if not user_id:
        return False
    return int(user_id) == store.first_user_id()


# ---------------------------------------------------------------- 分层底座（M1）
# 设计红线见 docs/product/positioning.md「商业模式的硬约束」与 roadmap.md M1：
# - tier 只标记「运营方服务资格」，不是功能开关；本地纪律能力对所有人开放。
# - 这里的 capability 只描述「服务」，绝不描述「功能」。禁止出现
#   "免费版隐藏回测/限制天数"这类判断——本地可改，且违背免费版承诺。
# - 判定只允许有这一处；路由/服务若需判服务资格，调用 entitlement()，不各写一份。
TIER_FREE = "free"

# 服务能力清单（只列名字，不代表已上线）。当前 M2 三个前提（计费主体/价格/
# 是否托管 Key）尚未拍板，运营方服务均未上线，故任何 tier 都不具备服务资格。
# 将来托管/算力/数据落地后，只需在这里给对应 tier 增加条目，调用点永不改。
_SERVICE_ENTITLEMENTS: Dict[str, frozenset] = {
    TIER_FREE: frozenset(),
}


def user_tier(user_id: Optional[int]) -> str:
    """读用户层级；无用户 / 异常一律按 free 处理（fail closed）。"""
    if not user_id:
        return TIER_FREE
    try:
        user = store.get_user_by_id(int(user_id))
    except Exception:  # noqa: BLE001
        return TIER_FREE
    if not user:
        return TIER_FREE
    return str(user.get("tier") or TIER_FREE).strip().lower() or TIER_FREE


def entitlement(user_id: Optional[int], capability: str) -> bool:
    """单点服务资格判定：该用户能否使用某项「运营方服务」。

    只判服务资格，不判功能可用性——四池/闸门/回测/说人话等本地能力永不查这里。
    未知 tier / 未知 capability 一律 False（fail closed），避免手滑开放未上线服务。
    """
    if not capability:
        return False
    allowed = _SERVICE_ENTITLEMENTS.get(user_tier(user_id), frozenset())
    return str(capability) in allowed


def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    """校验用户名+口令。用户名不存在也跑一次 PBKDF2，避免时序枚举。"""
    user = store.get_user_by_username(username)
    if not user:
        try:
            _pbkdf2(password or "", "00" * 16)
        except Exception:
            pass
        return None
    computed = _pbkdf2(password or "", str(user["salt"]))
    if not hmac.compare_digest(computed, str(user["password_hash"])):
        return None
    if not int(user.get("is_active") or 0):
        return None
    return user


def create_user(username: str, password: str, display_name: str = "") -> Dict[str, Any]:
    """创建新用户（含默认设置）。用户名重复抛 ValueError。"""
    err = validate_username(username)
    if err:
        raise ValueError(err)
    err = validate_password(password)
    if err:
        raise ValueError(err)
    if store.get_user_by_username(username):
        raise ValueError("该用户名已被占用")
    salt, pwd_hash = hash_password(password)
    user_id = store.create_user(username.strip(), salt, pwd_hash, display_name=display_name, created_at=now_text())
    store.save_settings(user_id, {"total_capital": 0.0}, now_text())
    logger.info("新用户已创建 username=%s id=%s", username, user_id)
    return store.get_user_by_id(user_id) or {"id": user_id, "username": username}


def change_password(user_id: int, old_password: str, new_password: str,
                    keep_session_id: Optional[str] = None) -> None:
    user = store.get_user_by_id(int(user_id))
    if not user:
        raise ValueError("用户不存在")
    if not hmac.compare_digest(_pbkdf2(old_password or "", str(user["salt"])), str(user["password_hash"])):
        raise ValueError("原口令不正确")
    err = validate_password(new_password)
    if err:
        raise ValueError(err)
    salt, pwd_hash = hash_password(new_password)
    store.update_user_password(int(user_id), salt, pwd_hash)
    # 改密后其它设备全部下线；当前这次会话保留，避免「改完密码把自己也踢了」
    store.delete_user_sessions(int(user_id), keep_session_id=keep_session_id)
    logger.info("用户口令已修改 user_id=%s", user_id)


# ---------------------------------------------------------------- 会话令牌

def create_session_token(user_id: Optional[int] = None, user_agent: str = "") -> str:
    if user_id is None:
        user_id = store.first_user_id()
    if user_id is None:
        raise RuntimeError("数据库中还没有任何用户")
    secret = _load_secret().encode("utf-8")
    sid = secrets.token_hex(16)
    expiry = int(time.time()) + SESSION_TTL
    store.create_session(sid, int(user_id), expiry, user_agent, now_text())
    payload = f"{sid}.{expiry}.{int(user_id)}"
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: Optional[str]) -> Optional[int]:
    """校验令牌，返回 user_id；无效返回 None。"""
    if not token:
        return None
    try:
        sid, expiry_s, user_s, sig = token.rsplit(".", 3)
        expiry = int(expiry_s)
        user_id = int(user_s)
    except Exception:
        return None
    try:
        secret = _load_secret().encode("utf-8")
        payload = f"{sid}.{expiry}.{user_id}"
        expected = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
    except Exception:
        return None
    if expiry <= int(time.time()):
        return None
    session = store.get_session(sid)
    if not session:
        return None
    if int(session.get("user_id") or 0) != user_id or int(session.get("expiry") or 0) != expiry:
        return None
    user = store.get_user_by_id(user_id)
    if not user or not int(user.get("is_active") or 0):
        return None
    return user_id


def revoke_session_token(token: Optional[str]) -> None:
    if not token:
        return
    try:
        sid = token.split(".")[0]
    except Exception:
        return
    if sid:
        store.delete_session(sid)


def revoke_user_sessions(user_id: int, keep_session_id: Optional[str] = None) -> None:
    store.delete_user_sessions(int(user_id), keep_session_id=keep_session_id)


# ---------------------------------------------------------------- 失败退避

def _get_state(key: str) -> Dict[str, float]:
    state = _fail_states.get(key)
    if state is None:
        state = {"count": 0.0, "until": 0.0, "last": 0.0}
        _fail_states[key] = state
        if len(_fail_states) > MAX_FAIL_ENTRIES:
            # 丢掉最久没更新的条目，防止恶意用户名把内存撑大
            oldest = min(
                (k for k in _fail_states if k != key),
                key=lambda k: _fail_states[k].get("last", 0.0),
                default=None,
            )
            if oldest:
                _fail_states.pop(oldest, None)
    return state


def _record(key: str, free: int) -> float:
    now = time.time()
    state = _get_state(key)
    if state["last"] and now - state["last"] > FAIL_RESET:
        state["count"] = 0.0
        state["until"] = 0.0
    state["count"] += 1
    state["last"] = now
    n = int(state["count"])
    if n <= free:
        return 0.0
    delay = min(FAIL_BASE * (2 ** (n - free - 1)), FAIL_MAX)
    state["until"] = now + delay
    return delay


def login_block_remaining(username: str = "") -> float:
    """当前是否处于惩罚窗口，返回剩余秒数（0 = 可尝试）。"""
    key = str(username or "").strip().lower()
    now = time.time()
    with _fail_lock:
        waits = [max(0.0, _get_state(key)["until"] - now)]
        if key != GLOBAL_KEY:
            waits.append(max(0.0, _get_state(GLOBAL_KEY)["until"] - now))
        return max(waits) if waits else 0.0


def register_login_failure(username: str = "") -> float:
    key = str(username or "").strip().lower()
    with _fail_lock:
        delay_key = _record(key, FAIL_FREE)
        delay_global = _record(GLOBAL_KEY, GLOBAL_FREE)
        delay = max(delay_key, delay_global)
        if delay > 0:
            logger.warning("登录失败 key=%s，退避 %ss", key or "-", int(delay))
        return delay


def reset_login_failures(username: str = "") -> None:
    key = str(username or "").strip().lower()
    with _fail_lock:
        _fail_states.pop(key, None)
        _fail_states.pop(GLOBAL_KEY, None)


def login_fail_count(username: str = "") -> int:
    key = str(username or "").strip().lower()
    with _fail_lock:
        return int(_get_state(key)["count"])
