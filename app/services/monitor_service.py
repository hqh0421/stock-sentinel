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

        # 时间止损：流水与环境都是只读、与个股无关，循环外取一次。
        # 若放进 build_item，6 只持仓就会读 6 遍流水、拉 6 次外盘。
        ts_enabled = False
        ts_records: List[Dict[str, Any]] = []
        ts_env: Dict[str, Any] = {}
        try:
            from . import market_env_service, time_stop_service  # noqa: F401
            from .trade_ledger import trade_ledger as _ledger

            ts_enabled = bool(decision_rules.get("time_stop.enabled", True))
            if ts_enabled:
                ts_env = market_env_service.build_env()
                ts_records = _ledger.list_trades(limit=20000)
        except Exception as exc:  # noqa: BLE001
            logger.warning("time_stop_init_failed error=%r", exc)
            ts_enabled = False

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
            # 基金/ETF 只给数据：连自定义表达式信号也不跑。
            # 表达式里常写 MA/MACD/量价背离，套在 ETF 上照样会“命中”，
            # 但那只是价格形态，不是这只基金该不该买卖的依据。
            data_only = bool(strategy.get("data_only"))
            if monitor_strategy and not data_only:
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

            # 单票止损降级：这只票在时间止损豁免期内时，「止损」信号仍记录但不单独推送。
            # 理由——两条信号指向同一笔持仓，一条豁免、另一条天天推，等于没豁免。
            # 降级而非屏蔽：每天汇总一条，事实不消失，豁免期一过就能查回这段亏损轨迹。
            if str(strategy.get("strategy_signal") or "").lower() == "stop_loss":
                try:
                    from . import time_stop_service as _tss

                    _ov = _tss.active_override(code)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("stop_loss_downgrade_check_failed code=%s error=%r", code, exc)
                    _ov = None
                if _ov:
                    strategy["stop_loss_downgraded"] = True
                    strategy["stop_loss_downgrade_detail"] = {
                        "signal": "stop_loss",
                        "expires_on": _ov.get("expires_on"),
                        "note": "处于时间止损豁免期，止损信号降级为仅记录（每日汇总一条）",
                    }
                    # 界面上还显示「止损」，但不推送，容易让人以为漏发了——补一句说明
                    strategy["action_desc"] = (
                        f"{strategy.get('action_desc') or ''}"
                        f"（豁免至 {_ov.get('expires_on')}：止损信号仅记录，不单独推送）"
                    ).strip()
                    strategy["strategy_triggered"] = False

            # 时间止损放在最后算：它是风控信号，必须独立成字段。
            # ⚠️ 不能把结论写进 strategy_signal —— 上面因子库自定义信号会覆盖它
            # （「金叉入量价背离出」曾把 stop_loss 盖成 buy），风控信息会被吞。
            if ts_enabled and shares > 0 and not data_only:
                try:
                    from . import time_stop_service

                    # 直接用当前持仓周期的 K 线（长线是周线），不再额外取日线：
                    # 行情源限流时多一次全链降级会让单票拖到分钟级，监控等于瘫痪。
                    # 「最近 N 根未创新高」已按频率换算（日线 10 根≈周线 4 根）。
                    ts_result = time_stop_service.evaluate(
                        code,
                        strategy.get("name") or code,
                        shares,
                        float(item.get("cost") or 0.0),
                        kline_data,
                        records=ts_records,
                        env=ts_env,
                        kline_frequency=item_kline_frequency,
                    )
                    # 非日线持仓：触发后再补一次日线算「炒作度」重算阈值。
                    # 只对已触发的票补取——为「可能不触发」的判定给全部持仓加请求，
                    # 在行情源限流时会把监控拖死。
                    # 依据：沉寂的票（近一年没炒过）值得多等，刚炒过的票退潮易阴跌要快走。
                    if ts_result.get("triggered") and item_kline_frequency != "d":
                        try:
                            from .value_position_service import analyze_hype_and_position

                            daily = self.host.get_k_line_data(code, frequency="d",
                                                              caller="time_stop_hype")
                            hype = analyze_hype_and_position(daily, window=250) if daily else {}
                            if hype.get("ok"):
                                ts_result = time_stop_service.evaluate(
                                    code,
                                    strategy.get("name") or code,
                                    shares,
                                    float(item.get("cost") or 0.0),
                                    kline_data,
                                    records=ts_records,
                                    env=ts_env,
                                    kline_frequency=item_kline_frequency,
                                    hype=hype,
                                )
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("time_stop_hype_failed code=%s error=%r", code, exc)
                    strategy["time_stop"] = ts_result
                    strategy["time_stop_signal"] = str(ts_result.get("signal") or "")
                    # 触发时要能推出去（告警只看 strategy_triggered），但不改 signal。
                    # 已豁免的不推——豁免有期限（最长 30 天），到期自动恢复提醒。
                    if ts_result.get("triggered") and not ts_result.get("overridden"):
                        strategy["strategy_triggered"] = True
                except Exception as exc:  # noqa: BLE001
                    logger.debug("time_stop_failed code=%s error=%r", code, exc)
                    strategy["time_stop"] = {"available": False, "reason": "calc_failed"}
                    strategy["time_stop_signal"] = ""
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
