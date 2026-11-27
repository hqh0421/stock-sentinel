"""决策规则单一事实源。

改造前阈值散落在 10+ 个 service 的模块级常量里，最要命的是**费率两套口径**：
回测用 `DEFAULT_FEE_RATE=0.00075` 一刀切，实盘台账用「佣金万 2.5（最低 5 元）+
印花税 + 过户费」。同一策略回测赚钱、实盘亏钱，回测结论因此不可信。

本模块把可编辑的规则收敛到 `config/decision_rules.json`，按 mtime 热更新，
并暴露**唯一一份**费用计算 `calc_trade_fee()`，供回测与台账共同调用。
模块内保留的 `DEFAULT_RULES` 只是文件缺失/字段缺失时的兜底，不是第二份口径。

用法：
    from ..core.decision_rules import get, fee_rules, calc_trade_fee
    get("risk.max_single_weight_pct", 35.0)
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_RULES_PATH = BASE_DIR / "config" / "decision_rules.json"

DEFAULT_RULES: Dict[str, Any] = {
    "version": 1,
    "fees": {
        "commission_rate": 0.00025,
        "commission_min": 5.0,
        "stamp_rate": 0.0005,
        "transfer_rate": 0.00001,
        "slippage_pct": 0.001,
        "lot_size": 100,
        "trading_days_per_year": 252,
    },
    "risk": {
        "vol_target_annual": 0.20,
        "max_portfolio_drawdown_pct": 15.0,
        "max_single_weight_pct": 35.0,
        "max_industry_weight_pct": 50.0,
        "stop_loss_distance_pct": 3.0,
        "relative_position_window": 250,
        "ma_long": 200,
        "vol_lookback": 20,
        "min_bars_for_guard": 20,
    },
    "scheduler": {
        "tick_seconds": 30,
        "default_interval_sec": 300,
        "min_interval_sec": 60,
        "max_interval_sec": 3600,
        "daily_review_at": "15:08",
        "risk_check_at": "09:05",
        "agent_enabled": True,
        "review_before_notify": True,
        "review_cooldown_sec": 1800,
        "push_scheduled": True,
    },
    "monitor": {
        "relative_position_window": 60,
        "vol_target_enabled": False,
        "portfolio_drawdown_guard_enabled": False,
    },
    # 四池 + 现金 + 全局风控：产品口径的唯一事实源，详见 config/decision_rules.json
    "pools": {},
    "cash": {"target_pct": 25.0, "min_pct": 10.0, "max_pct": 30.0},
    "global_risk": {
        "dd_stop_new_pct": -15.0,
        "dd_halve_growth_value_pct": -20.0,
        "dd_defensive_pct": -25.0,
        "max_industry_pct": 30.0,
        "gap_stop_at_open": True,
        "pool_dd_review_pct": -15.0,
        "rebalance_band_pp": 3.0,
        "rebalance_cadence": "monthly",
    },
    # 分类建议的数据抓取预算。估值/财报要打外部接口，雪球 Cookie 失效时会反复重试；
    # 必须设硬截止，否则「Agent 初判」会把请求挂成分钟级（2026-11-15 实测 306 秒）。
    "pool_classify": {
        "deadline_sec": 12.0,
        "max_workers": 8,
    },
}

_CACHE_LOCK = threading.Lock()
_WRITE_LOCK = threading.Lock()
_cached_rules: Optional[Dict[str, Any]] = None
_cached_mtime: float = -1.0
_cached_path: Optional[Path] = None
_cached_checked_at: float = 0.0

# mtime 检查的冷却时间（秒）。
# 为什么要冷却：config/ 在容器里是 Windows 挂载盘，实测 ``stat`` 一次要 **588 微秒**
# （本地盘只要 2 微秒）。规则读取是全系统热路径——monitor 一轮、agent 一次工具调用
# 会打几十上百次 ``get()``，每次都 stat 就是几百毫秒的纯开销。
# 冷却后 ``get()`` 从 1.6 毫秒降到几微秒；规则改动最多延迟这么久生效。
# 通过 API 改规则走 ``update()``，它会直接刷新缓存，不受冷却影响。
_MTIME_CHECK_TTL = 3.0


def _check_ttl() -> float:
    raw = os.environ.get("MONITOR_STOCK_RULES_TTL", "").strip()
    if not raw:
        return _MTIME_CHECK_TTL
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _MTIME_CHECK_TTL


def rules_path() -> Path:
    override = os.environ.get("MONITOR_STOCK_RULES_FILE", "").strip()
    return Path(override) if override else DEFAULT_RULES_PATH


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def get_rules() -> Dict[str, Any]:
    """读取规则并按文件 mtime 热更新；文件损坏时回退到内置兜底并告警。

    mtime 检查有冷却（见 ``_MTIME_CHECK_TTL``）：挂载盘上一次 stat 要 0.6 毫秒，
    热路径上每调用一次都查会白白吃掉大量时间。冷却期内直接返回缓存。
    """
    global _cached_rules, _cached_mtime, _cached_path, _cached_checked_at
    path = rules_path()
    now = time.monotonic()
    with _CACHE_LOCK:
        fresh_enough = (
            _cached_rules is not None
            and _cached_path == path
            and (now - _cached_checked_at) < _check_ttl()
        )
        if fresh_enough:
            return _cached_rules
        try:
            mtime = path.stat().st_mtime if path.exists() else -1.0
        except OSError:
            mtime = -1.0
        _cached_checked_at = now
        if _cached_rules is not None and _cached_path == path and mtime == _cached_mtime:
            return _cached_rules
        data: Dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
                else:
                    logger.warning("决策规则文件不是对象，已忽略 path=%s", path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("决策规则解析失败，使用内置兜底 path=%s error=%r", path, exc)
        _cached_rules = _deep_merge(DEFAULT_RULES, data)
        _cached_mtime = mtime
        _cached_path = path
        return _cached_rules


def get(path: str, default: Any = None) -> Any:
    """点路径读取，如 `get("fees.stamp_rate", 0.0005)`。"""
    node: Any = get_rules()
    for part in str(path).split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def section(name: str) -> Dict[str, Any]:
    value = get_rules().get(name)
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def update(patch: Dict[str, Any], backup: bool = True) -> Dict[str, Any]:
    """把 patch 深合并进规则文件并原子落盘，返回合并后的完整规则。

    为什么基于原始文件而不是 ``get_rules()``：后者已经混入了 ``DEFAULT_RULES``
    兜底，直接回写会把兜底值固化进文件，以后改兜底就不生效了。

    写前把旧文件另存一份 ``.bak``（只留一份，覆盖式），误改可以一键回滚。
    落盘后 mtime 变化，``get_rules()`` 下次调用会自动重载，无需重启服务。
    """
    path = rules_path()
    with _WRITE_LOCK:
        raw: Dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"现有规则文件无法解析，已中止写入：{exc}") from exc
            if not isinstance(loaded, dict):
                raise ValueError("现有规则文件顶层不是对象，已中止写入")
            raw = loaded
        merged = _deep_merge(raw, patch)
        if backup and path.exists():
            try:
                path.with_suffix(path.suffix + ".bak").write_text(
                    path.read_text(encoding="utf-8"), encoding="utf-8"
                )
            except OSError as exc:  # noqa: BLE001
                logger.warning("规则备份失败，继续写入 error=%r", exc)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

        global _cached_rules, _cached_mtime, _cached_path
        with _CACHE_LOCK:
            _cached_rules = _deep_merge(DEFAULT_RULES, merged)
            try:
                _cached_mtime = path.stat().st_mtime
            except OSError:
                _cached_mtime = -1.0
            _cached_path = path
        logger.info("决策规则已更新 keys=%s", ",".join(sorted(patch.keys())))
        return copy.deepcopy(_cached_rules)


def fee_rules() -> Dict[str, Any]:
    return section("fees")


def risk_rules() -> Dict[str, Any]:
    return section("risk")


def scheduler_rules() -> Dict[str, Any]:
    return section("scheduler")


def monitor_rules() -> Dict[str, Any]:
    return section("monitor")


def expr_rules() -> Dict[str, Any]:
    return section("expr")


def sentiment_rules() -> Dict[str, Any]:
    return section("sentiment")


def regime_table() -> Dict[str, Dict[str, Any]]:
    return section("sentiment_position").get("regime_table", {})


def style_tilt() -> Dict[str, Dict[str, Any]]:
    return section("sentiment_position").get("style_tilt", {})


def phase_labels() -> Dict[str, str]:
    return section("sentiment_position").get("phase_label", {})


def sell_style_params() -> Dict[str, Dict[str, Any]]:
    """返回三档卖出参数（atr_targets 转 tuple，与原调用方保持兼容）。"""
    raw = section("sell_style")
    out: Dict[str, Dict[str, Any]] = {}
    for style, params in raw.items():
        if not isinstance(params, dict):
            continue
        item = copy.deepcopy(params)
        if isinstance(item.get("atr_targets"), list):
            item["atr_targets"] = tuple(float(v) for v in item["atr_targets"])
        out[style] = item
    return out


def pools() -> Dict[str, Dict[str, Any]]:
    """四池定义：dividend / catalyst / long_track / value。"""
    return section("pools")


def pool(name: str) -> Dict[str, Any]:
    """按池名取单池定义，未知池返回空 dict（调用方需自行判断）。"""
    return copy.deepcopy(pools().get(str(name or "").strip().lower(), {}))


def pool_order() -> list:
    return list(pools().keys())


def pool_labels() -> Dict[str, str]:
    return {key: str(val.get("label") or key) for key, val in pools().items() if isinstance(val, dict)}


def cash_rules() -> Dict[str, Any]:
    return section("cash")


def global_risk() -> Dict[str, Any]:
    return section("global_risk")


def pool_classify_rules() -> Dict[str, Any]:
    """分类建议的抓数预算：deadline_sec 硬截止、max_workers 并发上限。"""
    raw = section("pool_classify")
    return {
        "deadline_sec": float(raw.get("deadline_sec") or 12.0),
        "max_workers": int(raw.get("max_workers") or 8),
    }


def pool_sell_params() -> Dict[str, Dict[str, Any]]:
    """把每池的 sell_params 转成卖出引擎可直接吃的参数（atr_targets 转 tuple）。"""
    out: Dict[str, Dict[str, Any]] = {}
    for name, item in pools().items():
        params = item.get("sell_params") if isinstance(item, dict) else None
        if not isinstance(params, dict):
            continue
        value = copy.deepcopy(params)
        if isinstance(value.get("atr_targets"), list):
            value["atr_targets"] = tuple(float(v) for v in value["atr_targets"])
        out[name] = value
    return out


def recover_principal_sell_pct(gain_pct: float) -> float:
    """涨到 gain_pct% 时，卖出多少比例能收回全部本金：f = 1/(1+r)。

    历史规则里「+30% 卖 67% 收回本金」是错的（0.67×1.30=87%），
    这里给出唯一口径：+30% 需卖 76.9%，+50% 才是 66.7%。
    """
    rate = float(gain_pct or 0.0) / 100.0
    if rate <= 0:
        return 100.0
    return round(min(100.0, 100.0 / (1.0 + rate)), 2)


def calc_trade_fee(side: str, amount: float, fee: Optional[float] = None) -> Dict[str, float]:
    """唯一一份费用口径：佣金（含最低）+ 印花税（仅卖出）+ 过户费。

    `fee` 由用户口述的实际手续费覆盖，给定时不再叠加估算。
    回测撮合与交易台账都调这里，保证「回测结论与实盘可比」。
    """
    rules = fee_rules()
    amount = max(0.0, float(amount or 0.0))
    if fee is not None:
        total = max(0.0, float(fee))
        return {"total": round(total, 2), "commission": round(total, 2),
                "stamp": 0.0, "transfer": 0.0, "estimated": False}
    commission_rate = float(rules.get("commission_rate", 0.00025))
    commission_min = float(rules.get("commission_min", 5.0))
    stamp_rate = float(rules.get("stamp_rate", 0.0005))
    transfer_rate = float(rules.get("transfer_rate", 0.00001))
    commission = max(commission_min, amount * commission_rate) if amount > 0 else 0.0
    stamp = amount * stamp_rate if str(side).lower() == "sell" else 0.0
    transfer = amount * transfer_rate
    return {
        "total": round(commission + stamp + transfer, 2),
        "commission": round(commission, 2),
        "stamp": round(stamp, 2),
        "transfer": round(transfer, 2),
        "estimated": True,
    }


def effective_fee_rate(amount: float = 10000.0) -> float:
    """给回测展示用的等效单边费率（含最低佣金影响），不再是拍脑袋的 0.00075。"""
    if amount <= 0:
        amount = 10000.0
    return round(calc_trade_fee("buy", amount)["total"] / amount, 8)


def rules_summary() -> Dict[str, Any]:
    """给运维/前端看的规则摘要（规则文件里不含凭证，可安全返回）。"""
    return {
        "path": str(rules_path()),
        "version": get("version", 1),
        "fee_model": "ledger_aligned",
        "effective_buy_fee_rate": effective_fee_rate(),
        "sections": sorted(k for k in get_rules() if not k.startswith("_")),
    }
