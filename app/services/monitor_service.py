import contextvars
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from ..core import decision_rules
from ..core.trading_time import is_trading_now, trading_status

logger = logging.getLogger(__name__)


class MonitorService:
    def __init__(self, host: Any) -> None:
        self.host = host

    def monitor_favorites(self, force_refresh: bool = False, respect_trading_hours: bool = True) -> List[Dict[str, Any]]:
        if respect_trading_hours and not is_trading_now():
            logger.info("非交易时段，跳过监控（%s）", trading_status())
            return []
        if not self.host._monitor_lock.acquire(blocking=False):
            cached = [dict(item) for item in self.host._monitor_cache]
            for item in cached:
                item["monitor_busy"] = True
            return cached
        try:
            return self.monitor_favorites_locked(force_refresh=force_refresh)
        finally:
            self.host._monitor_lock.release()

    def monitor_favorites_locked(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        settings = self.host.get_user_settings()
        monitor_strategy_name = str(settings.get("monitor_strategy") or "").strip()
        monitor_kline_frequency = str(settings.get("monitor_kline_frequency") or "d").strip().lower()
        if monitor_kline_frequency not in {"d", "w", "m", "1", "5", "15", "30", "60"}:
            monitor_kline_frequency = "d"
        monitor_strategy = self.host._find_strategy_by_name(monitor_strategy_name)

        def build_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            code = item.get("secID")
            if not code:
                return None
            shares = int(item.get("shares") or 0)
            horizon = self.host.effective_horizon(item)
            trend_strategy_config = self.host.get_trade_strategy_config(horizon)
            item_kline_frequency = str(trend_strategy_config.get("kline_frequency") or monitor_kline_frequency).strip().lower()
            if item_kline_frequency not in {"d", "w", "m", "1", "5", "15", "30", "60"}:
                item_kline_frequency = monitor_kline_frequency
            caller = "monitor_check_refresh" if force_refresh else "monitor_check"
            kline_data = self.host.get_k_line_data(code, frequency=item_kline_frequency, caller=caller)
            strategy = self.host.check_strategy(
                code,
                shares=shares,
                cost=float(item.get("cost") or 0.0),
                horizon=horizon,
                buy_date=item.get("buy_date"),
                kline_data=kline_data,
            )
            strategy["secID"] = code
            strategy["name"] = item.get("name") or self.host._get_cached_stock_name(self.host._normalize_stock_code(code)["digits"]) or code
            strategy["monitor_strategy"] = monitor_strategy_name
            strategy["monitor_kline_frequency"] = item_kline_frequency
            strategy["trend_strategy_config"] = trend_strategy_config
            # 相对位置指标：让「低位/高位」有可核对的数字，而不只是一句定性判断。
            # 口径统一由 risk_guard_service 提供，阈值来自 decision_rules。
            try:
                from .risk_guard_service import relative_position

                strategy["relative_position"] = relative_position(
                    kline_data,
                    window=int(decision_rules.get("monitor.relative_position_window", 60)),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("relative_position_failed code=%s error=%r", code, exc)
                strategy["relative_position"] = {"available": False, "reason": "calc_failed"}
            # 两个风控开关都在 config/decision_rules.json 的 monitor 段：
            # 打开后才在监控信号里附带建议仓位与回撤护栏，默认关闭以免改变既有告警行为。
            monitor_cfg = decision_rules.monitor_rules()
            try:
                from . import risk_guard_service as rg

                if bool(monitor_cfg.get("vol_target_enabled", False)):
                    strategy["vol_target"] = rg.vol_target_position(rg.realized_volatility(kline_data))
                if bool(monitor_cfg.get("portfolio_drawdown_guard_enabled", False)):
                    strategy["drawdown_guard"] = rg.price_drawdown_guard(kline_data)
            except Exception as exc:  # noqa: BLE001
                logger.debug("risk_guard_failed code=%s error=%r", code, exc)
            strategy["strategy_triggered"] = bool(strategy.get("strategy_triggered"))
            strategy["strategy_signal"] = str(strategy.get("strategy_signal") or "")
            if monitor_strategy:
                eval_result = self.host._eval_strategy_signal(
                    code,
                    str(monitor_strategy.get("entry_expr") or ""),
                    str(monitor_strategy.get("exit_expr") or ""),
                    kline_data=kline_data,
                )
                entry_hit = bool(eval_result.get("entry_hit"))
                exit_hit = bool(eval_result.get("exit_hit"))
                if shares > 0:
                    custom_triggered = exit_hit or entry_hit
                    custom_signal = "sell" if exit_hit else ("buy" if entry_hit else "")
                else:
                    custom_triggered = entry_hit or exit_hit
                    custom_signal = "buy" if entry_hit else ("sell" if exit_hit else "")
                strategy["custom_strategy_triggered"] = custom_triggered
                strategy["custom_strategy_signal"] = custom_signal
                strategy["custom_strategy_eval"] = eval_result
                if custom_triggered:
                    strategy["strategy_triggered"] = True
                    strategy["strategy_signal"] = custom_signal
            return strategy

        items = list(self.host._favorite_cache)
        if not items:
            self.host._monitor_cache = []
            self.host._monitor_cache_last_update = time.time()
            return []
        if len(items) == 1:
            result = build_item(items[0])
            results = [result] if result else []
            self.host._monitor_cache = [dict(item) for item in results]
            self.host._monitor_cache_last_update = time.time()
            return results

        results: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(6, len(items))) as executor:
            # 线程池不会自动继承 contextvars，必须显式复制，
            # 否则 worker 里读不到 current_user，会回退到「首个用户」的数据。
            futures = [executor.submit(contextvars.copy_context().run, build_item, item) for item in items]
            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)
        self.host._monitor_cache = [dict(item) for item in results]
        self.host._monitor_cache_last_update = time.time()
        return results
