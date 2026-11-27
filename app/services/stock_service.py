import datetime
import copy
import contextvars
import json
import logging
import os
import random
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

import pandas as pd
import akshare as ak
from ..tools.monitor.notify_tool import NotifyTool
from ..tools.sources.company_info_tool import CompanyInfoTool
from ..tools.sources.http_json_tool import HttpJsonTool
from ..tools.sources.industry_valuation_tool import IndustryValuationTool
from ..tools.sources.stock_dupont_tool import StockDupontTool
from ..tools.sources.stock_financial_analysis_tool import StockFinancialAnalysisTool, StockFinancialIndicatorTool
from ..tools.sources.sw_industry_valuation_tool import SwIndustryValuationTool
from ..tools.sources.xueqiu_cookie_refresher import XueqiuCookieRefresher
from ..tools.sources.xueqiu_market import XueqiuMarketDataTool
from ..tools.sources.market_data_tool import MarketDataTool
from ..tools.sources.tencent_quote_tool import TencentQuoteTool
from .sources.amazingdata_source import AmazingDataTool  # 星耀数智官方 SDK 适配器
from .sources.amazingdata_source import pool_ready as _ad_pool_ready
from .sources.amazingdata_source import warmup_async as _ad_warmup_async
from .alert_service import AlertService
from .allocation_service import normalize_pool, horizon_for_pool
from .monitor_service import MonitorService
from .sentiment_service import SentimentService
from .strategy_service import StrategyService
from .industry_valuation_service import IndustryValuationCenter, assess_industry_relative
from ..core import context as user_context
from ..core.secid import split_secid
from ..core.trading_time import is_trading_now
from ..db import store as db_store

logger = logging.getLogger("app.market_data")


def _friendly_error(exc: Exception) -> str:
    """把第三方库的原始异常压成一句人话。

    这些字符串会直接出现在前端卡片和 Agent 的观察结果里，不能把
    `(<class 'urllib3.exceptions.ProxyError'>, ...)` 这种栈信息甩给用户。
    """
    name = type(exc).__name__
    text = str(exc)
    low = text.lower()
    # 项目内自定义异常常常已经写了很好的中文说明，取首句直接用，别再压成类名
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        head = text.split("。")[0].strip()
        if not head:
            return name
        return head + "。" if len(head) <= 100 else head[:100] + "…"
    if "proxy" in name.lower() or "proxy" in low:
        return "网络代理不可达"
    if "timeout" in name.lower() or "timed out" in low or "time out" in low:
        return "请求超时"
    if "connection" in name.lower() or "remotedisconnected" in low or "connection" in low:
        return "连接被断开或已被限流"
    if "httperror" in name.lower() or "429" in text:
        return "接口限流或返回异常"
    if "json" in name.lower():
        return "返回内容无法解析"
    return name


class StockService:
    # JSON 读取缓存：TTL 秒数 / 允许进缓存的最大文件字节数
    _JSON_CACHE_TTL = 3.0
    _JSON_CACHE_MAX_BYTES = 256 * 1024

    def __init__(self) -> None:
        self._config_dir = Path(__file__).resolve().parent.parent.parent / "config"
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._json_cache: Dict[str, Any] = {}
        self._favorites_file = self._config_dir / "favorites.json"
        self._settings_file = self._config_dir / "user_settings.json"
        self._xueqiu_auth_file = self._config_dir / "xueqiu_auth.json"
        self._alert_rules_file = self._config_dir / "alert_rules.json"
        self._profiles_file = self._config_dir / "company_profiles.json"
        self._sw_industry_cache_file = self._config_dir / "sw_industry_cache.json"
        self._valuation_rules_file = self._config_dir / "valuation_rules.json"
        self._fundamentals_cache_file = self._config_dir / "fundamentals_cache.json"
        self._industry_cycle_cache_file = self._config_dir / "industry_cycle_cache.json"
        self._market_regime_cache_file = self._config_dir / "market_regime_cache.json"
        self._strategies_file = self._config_dir / "strategies.json"
        self._trade_strategy_files = {
            "short": self._config_dir / "trade_strategy_short.json",
            "mid_long": self._config_dir / "trade_strategy_mid_long.json",
            "long": self._config_dir / "trade_strategy_long.json",
        }
        # 用户态缓存按 user_id 分桶：单例是全进程共享的，不能再用一个全局列表，
        # 否则 A 的自选会出现在 B 的页面上。
        self._fav_caches: Dict[int, List[Dict[str, Any]]] = {}
        self._account_snap_caches: Dict[int, Dict[str, Any]] = {}
        self._stock_list_cache: List[Dict[str, Any]] = []
        self._stock_list_last_update = 0.0
        self._stock_list_last_fail = 0.0
        self._stock_list_lock = Lock()
        self._stock_list_file = self._config_dir / "stock_list_cache.json"
        self._load_stock_list_disk()
        self._spot_data_cache: Optional[pd.DataFrame] = None
        self._spot_data_last_update = 0.0
        self._spot_refresh_lock = Lock()
        self._spot_refreshing = False
        self._http_timeout = 8.0
        self._monitor_lock = Lock()
        self._spot_source_timeout = 2.0
        self._xueqiu_tool = XueqiuMarketDataTool(timeout_sec=self._spot_source_timeout)
        # 腾讯行情兜底：无鉴权、批量、几 KB，云服务器上比雪球/东财稳得多
        self._tencent_tool = TencentQuoteTool(timeout_sec=3.0)
        # 行情源健康登记表：谁连续失败就冷却谁，冷却期内兜底链直接跳过它。
        # 没有「坏源拖慢好源」的问题——坏源被跳过的代价是 0 秒，不跳过是每票 2 秒。
        self._source_health: Dict[str, Dict[str, Any]] = {}
        self._xueqiu_cookie_refresher = XueqiuCookieRefresher()
        self._market_tool = MarketDataTool(timeout_sec=self._http_timeout)
        self._company_info_tool = CompanyInfoTool(self._profiles_file, market_tool=self._market_tool)
        self._http_json_tool = HttpJsonTool(timeout_sec=self._http_timeout)
        self._stock_dupont_tool = StockDupontTool()
        self._stock_financial_tool = StockFinancialAnalysisTool()
        self._stock_financial_indicator_tool = StockFinancialIndicatorTool()
        self._industry_valuation_tool = IndustryValuationTool(min_samples=3, max_peers=12)
        self._sw_industry_valuation_tool = SwIndustryValuationTool(self._sw_industry_cache_file, market_tool=self._market_tool)
        # 行业估值中枢：算"这只票在自己行业里贵不贵"（用户要求：不能跨行业比 PE/PB）
        self._industry_valuation_center = IndustryValuationCenter(
            config_dir=self._config_dir,
            spot_provider=self._spot_valuation_rows,
            sw_provider=self._sw_industry_valuation_rows,
            components_provider=self._sw_industry_components_map,
            single_components_provider=self._ensure_industry_components,
        )
        self._notify_tool = NotifyTool(timeout_sec=self._http_timeout)
        self._tencent_quote_tool = TencentQuoteTool(timeout_sec=self._http_timeout)
        self._sentiment_service = SentimentService(self._config_dir)
        self._alert_service = AlertService(self)
        self._monitor_service = MonitorService(self)
        self._strategy_service = StrategyService(self)
        self._alert_sent_by_user: Dict[int, Dict[str, float]] = {}
        self._kline_cache: Dict[str, Dict[str, Any]] = {}
        self._kline_cache_lock = Lock()
        self._source_cache: Dict[str, Dict[str, Any]] = {}
        self._source_cache_lock = Lock()
        self._source_inflight_locks: Dict[str, Lock] = {}
        self._monitor_caches: Dict[int, List[Dict[str, Any]]] = {}
        self._monitor_cache_updates: Dict[int, float] = {}
        self._prefer_source = str((self.get_user_settings().get('prefer_source') or 'xueqiu')).strip().lower()
        self._amazingdata_tool = None
        self._amazingdata_timeout = 30.0  # 星耀数智走独立子进程+多日回退，需比其它源更长的超时
        self._amazingdata_credentials = self._load_amazingdata_credentials()
        # 仅当明确禁用代理(USE_PROXY!=1)时清除代理环境变量；
        # 部署环境设 USE_PROXY=1 + HTTPS_PROXY 以复用宿主出网代理访问国内行情源(雪球/新浪/AkShare等)
        if os.environ.get('USE_PROXY', '0') != '1':
            self._clear_proxy_env_for_process()

    def _clear_proxy_env_for_process(self) -> None:
        proxy_keys = ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']
        removed = [key for key in proxy_keys if os.environ.pop(key, None)]
        if removed:
            logger.info("disabled_proxy_env keys=%s", removed)

    # ------------------------------------------------------------------
    # 当前用户 / 用户态缓存
    #
    # 单例对象全进程共享，凡是「跟着人走」的数据都必须按 user_id 分桶。
    # 这里用 property 把老的 `self._favorite_cache` 形态接住，业务代码
    # （monitor_service / trade_ledger / backtest_service）不用改一行。
    # ------------------------------------------------------------------
    @staticmethod
    def _uid_safe() -> Optional[int]:
        try:
            return user_context.resolve_user_id_optional()
        except Exception:
            return None

    def _uid(self) -> int:
        return user_context.resolve_user_id()

    @property
    def _favorite_cache(self) -> List[Dict[str, Any]]:
        uid = self._uid()
        if uid not in self._fav_caches:
            self._fav_caches[uid] = self._sanitize_favorites(db_store.list_favorites(uid))
        return self._fav_caches[uid]

    @_favorite_cache.setter
    def _favorite_cache(self, value: List[Dict[str, Any]]) -> None:
        self._fav_caches[self._uid()] = value

    @property
    def _alert_last_sent(self) -> Dict[str, float]:
        return self._alert_sent_by_user.setdefault(self._uid(), {})

    def _snap_cache_get(self, uid: int) -> Optional[Dict[str, Any]]:
        return self._account_snap_caches.get(uid)

    def _snap_cache_set(self, uid: int, snap: Dict[str, Any]) -> None:
        self._account_snap_caches[uid] = {'ts': time.time(), 'snap': snap}

    def _snap_cache_clear(self, uid: Optional[int] = None) -> None:
        if uid is None:
            self._account_snap_caches.clear()
        else:
            self._account_snap_caches.pop(uid, None)

    @property
    def _monitor_cache(self) -> List[Dict[str, Any]]:
        return self._monitor_caches.setdefault(self._uid(), [])

    @_monitor_cache.setter
    def _monitor_cache(self, value: List[Dict[str, Any]]) -> None:
        self._monitor_caches[self._uid()] = value

    @property
    def _monitor_cache_last_update(self) -> float:
        return self._monitor_cache_updates.get(self._uid(), 0.0)

    @_monitor_cache_last_update.setter
    def _monitor_cache_last_update(self, value: float) -> None:
        self._monitor_cache_updates[self._uid()] = value


    def _find_strategy_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        target = (name or "").strip()
        if not target:
            return None
        for item in self.list_strategies():
            if str(item.get("name") or "").strip() == target:
                return item
        return None

    def _eval_strategy_signal(self, code: str, entry_expr: str, exit_expr: str, kline_data: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        return self._strategy_service.eval_signal(code, entry_expr, exit_expr, kline_data=kline_data)

    def _send_dingtalk_markdown(self, webhook_url: str, title: str, text: str, secret: str = "") -> Dict[str, Any]:
        return self._notify_tool.send_dingtalk_markdown(webhook_url, title, text, secret=secret)

    def notify_monitor_results(self, monitor_data: List[Dict[str, Any]], force_send: bool = False) -> Dict[str, Any]:
        settings = self.get_user_settings()
        webhook_url = str(settings.get('dingtalk_webhook_url') or '').strip()
        dingtalk_secret = str(settings.get('dingtalk_secret') or '').strip()
        notify_enabled = bool(settings.get('dingtalk_enabled', False))
        monitor_enabled = bool(settings.get('monitor_enabled', True))
        strategy_name = str(settings.get('monitor_strategy') or '').strip()
        if not webhook_url:
            return {'ok': False, 'sent': False, 'reason': 'missing_webhook'}
        if not notify_enabled and not force_send:
            return {'ok': False, 'sent': False, 'reason': 'dingtalk_disabled'}
        if not monitor_enabled and not force_send:
            return {'ok': False, 'sent': False, 'reason': 'monitor_disabled'}
        triggered = [item for item in monitor_data if item.get('strategy_triggered')]
        if not triggered and not force_send:
            return {'ok': True, 'sent': False, 'reason': 'no_strategy_trigger'}
        now_text = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if force_send:
            lines = [
                f"### 股票监控测试提醒",
                f"- 时间: {now_text}",
                f"- 监控策略: {strategy_name or '未设置'}",
                f"- 说明: 这是测试消息，用于验证钉钉 webhook 配置",
            ]
            if triggered:
                lines.append(f"- 当前触发数量: {len(triggered)}")
        else:
            lines = [
                f"### 股票监控策略提醒",
                f"- 时间: {now_text}",
                f"- 触发策略: {strategy_name or '未设置'}",
                f"- 触发数量: {len(triggered)}",
            ]
            for item in triggered[:20]:
                lines.append(
                    f"- {item.get('name') or item.get('secID')}: 动作 {item.get('action')}, "
                    f"策略信号 {item.get('strategy_signal') or '-'}, "
                    f"现价 {item.get('price') if item.get('price') is not None else '-'}"
                )
        body = '\n'.join(lines)
        sent = self._send_dingtalk_markdown(webhook_url, '股票监控提醒', body, dingtalk_secret)
        return {'ok': bool(sent.get('ok')), 'sent': bool(sent.get('ok')), 'detail': sent, 'triggered_count': len(triggered)}

    def _monitor_pct_change(self, item: Dict[str, Any]) -> Optional[float]:
        spot = item.get('spot') if isinstance(item.get('spot'), dict) else {}
        return self._to_float(item.get('pctChg') or item.get('pct_chg') or spot.get('pct_chg') or spot.get('pctChg'))

    def get_alert_rules(self) -> Dict[str, Any]:
        return self._alert_service.get_alert_rules()

    def save_alert_rules(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._alert_service.save_alert_rules(payload)

    def load_alert_rules_raw(self, defaults: Dict[str, Any]) -> Dict[str, Any]:
        """告警规则原始数据（供 AlertService 读写，多用户按 current_user 隔离）。"""
        uid = self._uid_safe()
        if uid is None:
            data = self._load_json(self._alert_rules_file, defaults)
            return data if isinstance(data, dict) else defaults
        data = db_store.get_alert_rules(uid)
        return data if isinstance(data, dict) else defaults

    def save_alert_rules_raw(self, payload: Dict[str, Any]) -> None:
        uid = self._uid()
        db_store.save_alert_rules(uid, payload, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    def notify_monitor_results(self, monitor_data: List[Dict[str, Any]], force_send: bool = False, mode: str = 'auto') -> Dict[str, Any]:
        return self._alert_service.notify_monitor_results(monitor_data, force_send=force_send, mode=mode)

    def _load_json(self, path: Path, default: Any) -> Any:
        """读 JSON，带 mtime + TTL 缓存。

        为什么要缓存：`config/` 在容器里是 Windows 挂载盘，实测读一个 1.9KB 的
        JSON 要 **1.9 毫秒**（本地盘 22 微秒，差 88 倍）。而 `_load_xueqiu_auth`
        这类调用在每次 `get_user_settings()` 里都会发生，页面一加载就打十几次。
        缓存后命中只要几十微秒；规则/凭证改动最多延迟 3 秒生效。

        命中时返回深拷贝：调用方会就地改返回值（例如把环境变量覆盖进雪球凭证），
        直接返回缓存对象会把缓存污染成「环境变量优先」的副本。
        超过 256KB 的大缓存文件不进内存缓存，避免多用户下内存翻倍。
        """
        key = str(path)
        now = time.time()
        hit = self._json_cache.get(key)
        # TTL 内直接返回，**连 stat 都不做**：挂载盘上一次 stat 要 0.59 毫秒，
        # 和读文件本身一样贵，缓存命中还去 stat 等于白缓存。
        if hit and (now - hit[0]) < self._JSON_CACHE_TTL:
            return copy.deepcopy(hit[2])
        try:
            stat = path.stat()
        except OSError:
            return default
        try:
            with open(path, 'r', encoding='utf-8') as f:
                obj = json.load(f)
        except Exception:
            return default
        if stat.st_size <= self._JSON_CACHE_MAX_BYTES:
            self._json_cache[key] = (now, stat.st_mtime, obj)
        return copy.deepcopy(obj) if key in self._json_cache else obj

    def _save_json(self, path: Path, payload: Any) -> None:
        self._json_cache.pop(str(path), None)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _normalize_horizon(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        aliases = {
            "short": "short",
            # 四池口径下「催化」映射到 swing，而监控参数只有 short/mid_long/long 三档；
            # 系统禁短线，short 档本身就是波段参数，所以 swing 统一归到 short。
            "swing": "short",
            "band": "short",
            "medium": "mid_long",
            "mid": "mid_long",
            "mid_long": "mid_long",
            "middle": "mid_long",
            "long": "long",
        }
        return aliases.get(text, "long")

    def _default_trade_strategy_config(self, horizon: str) -> Dict[str, Any]:
        defaults: Dict[str, Dict[str, Any]] = {
            "short": {
                "id": "short",
                "name": "短线策略",
                "kline_frequency": "d",
                "required_kline_count": 30,
                "entry": {
                    "allowed_stages": ["uptrend", "washout", "accumulation"],
                    "require_buy_signal": True,
                    "min_price_above_ma20_pct": -2.0
                },
                "take_profit": {"pct": 6.0, "action": "reduce"},
                "stop_loss": {"pct": -3.0, "action": "stop_loss"},
                "position": {"initial_pct": 20, "max_pct": 35, "add_pct": 10, "add_signal_preference": "right"},
                "confirmations": {"require_volume": False, "require_money_flow": False}
            },
            "mid_long": {
                "id": "mid_long",
                "name": "中长线策略",
                "kline_frequency": "d",
                "required_kline_count": 60,
                "entry": {
                    "allowed_stages": ["uptrend", "accumulation", "washout"],
                    "require_buy_signal": False,
                    "min_price_above_ma20_pct": -5.0
                },
                "take_profit": {"pct": 15.0, "action": "reduce"},
                "stop_loss": {"pct": -8.0, "action": "stop_loss"},
                "position": {"initial_pct": 30, "max_pct": 55, "add_pct": 15, "add_signal_preference": "right"},
                "confirmations": {"require_volume": False, "require_money_flow": False}
            },
            "long": {
                "id": "long",
                "name": "长线策略",
                "kline_frequency": "w",
                "required_kline_count": 80,
                "entry": {
                    "allowed_stages": ["uptrend", "accumulation"],
                    "require_buy_signal": False,
                    "min_price_above_ma20_pct": -10.0
                },
                "take_profit": {"pct": 30.0, "action": "hold_watch"},
                "stop_loss": {"pct": -15.0, "action": "reduce"},
                "position": {"initial_pct": 35, "max_pct": 70, "add_pct": 15, "add_signal_preference": "right"},
                "confirmations": {"require_volume": False, "require_money_flow": False}
            },
        }
        return copy.deepcopy(defaults.get(self._normalize_horizon(horizon), defaults["long"]))

    def _trade_strategy_store(self) -> Dict[str, Any]:
        """趋势策略模板按用户存：写进 user_settings.trade_strategies。

        没有数据库记录时回退读旧的 trade_strategy_*.json —— 这份只当「初始默认值」，
        任何一个用户保存后都只写自己的记录，不会影响别人。
        """
        uid = self._uid_safe()
        if uid is not None:
            settings = db_store.get_settings(uid)
            data = settings.get('trade_strategies') if isinstance(settings, dict) else None
            if isinstance(data, dict):
                return data
        out: Dict[str, Any] = {}
        for horizon, path in self._trade_strategy_files.items():
            if path.exists():
                file_data = self._load_json(path, {})
                if isinstance(file_data, dict):
                    out[horizon] = file_data
        return out

    def _save_trade_strategy_store(self, data: Dict[str, Any]) -> None:
        self.save_user_settings({'trade_strategies': data})

    def get_trade_strategy_config(self, horizon: Any) -> Dict[str, Any]:
        normalized = self._normalize_horizon(horizon)
        config = self._default_trade_strategy_config(normalized)
        loaded = self._trade_strategy_store().get(normalized)
        if isinstance(loaded, dict):
            config.update(loaded)
        config["id"] = normalized
        return config

    def list_trade_strategy_configs(self) -> Dict[str, Dict[str, Any]]:
        return {
            horizon: self.get_trade_strategy_config(horizon)
            for horizon in ["short", "mid_long", "long"]
        }

    def save_trade_strategy_config(self, horizon: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_horizon(horizon)
        current = self.get_trade_strategy_config(normalized)
        if not isinstance(payload, dict):
            raise ValueError("strategy payload must be an object")

        allowed_frequency = {"d", "w", "m", "1", "5", "15", "30", "60"}
        current["name"] = str(payload.get("name") or current.get("name") or "").strip()
        current["description"] = str(payload.get("description") or current.get("description") or "").strip()
        frequency = str(payload.get("kline_frequency") or current.get("kline_frequency") or "d").strip().lower()
        current["kline_frequency"] = frequency if frequency in allowed_frequency else "d"
        current["required_kline_count"] = max(2, int(payload.get("required_kline_count") or current.get("required_kline_count") or 30))

        def update_nested(name: str, numeric_fields: List[str], bool_fields: List[str] = None, list_fields: List[str] = None) -> None:
            incoming = payload.get(name)
            if not isinstance(incoming, dict):
                return
            target = current.get(name) if isinstance(current.get(name), dict) else {}
            for field in numeric_fields:
                if field in incoming:
                    value = self._to_float(incoming.get(field))
                    if value is not None:
                        target[field] = value
            for field in bool_fields or []:
                if field in incoming:
                    target[field] = bool(incoming.get(field))
            for field in list_fields or []:
                if field in incoming:
                    raw = incoming.get(field)
                    if isinstance(raw, list):
                        target[field] = [str(item).strip() for item in raw if str(item).strip()]
                    elif isinstance(raw, str):
                        target[field] = [item.strip() for item in raw.split(",") if item.strip()]
            if "action" in incoming:
                target["action"] = str(incoming.get("action") or target.get("action") or "").strip()
            current[name] = target

        update_nested("entry", ["min_price_above_ma20_pct"], ["require_buy_signal"], ["allowed_stages"])
        update_nested("take_profit", ["pct"])
        update_nested("stop_loss", ["pct"])
        update_nested("position", ["initial_pct", "max_pct", "add_pct"])
        if isinstance(current.get("position"), dict):
            position_target = current["position"]
            if isinstance(payload.get("position"), dict) and "add_signal_preference" in payload["position"]:
                pref = str(payload["position"].get("add_signal_preference") or position_target.get("add_signal_preference") or "right").strip().lower()
                position_target["add_signal_preference"] = "left" if pref == "left" else "right"
        update_nested("confirmations", [], ["require_volume", "require_money_flow"])

        current["id"] = normalized
        store = dict(self._trade_strategy_store())
        store[normalized] = current
        self._save_trade_strategy_store(store)
        return current

    def _sanitize_favorites(self, payload: Any) -> List[Dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        sanitized: List[Dict[str, Any]] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            sec_id = str(raw.get('secID') or '').strip()
            if not sec_id:
                continue
            try:
                normalized = self._normalize_stock_code(sec_id)
                sec_id = f"{normalized['digits']}.{normalized['market'].upper()}"
            except Exception:
                continue
            try:
                shares = int(raw.get('shares') or 0)
            except Exception:
                shares = 0
            try:
                cost = float(raw.get('cost') or 0.0)
            except Exception:
                cost = 0.0
            sanitized.append({
                'secID': sec_id,
                'shares': max(0, shares),
                'cost': max(0.0, cost),
                'horizon': self._normalize_horizon(raw.get('horizon') or 'long'),
                # 四池分类（dividend/catalyst/long_track/value），空串=未分类
                'pool': normalize_pool(raw.get('pool')),
                # 分组/备注是纯展示字段，清洗只做长度裁剪，不参与任何计算
                'group': str(raw.get('group') or raw.get('group_name') or '').strip()[:40],
                'note': str(raw.get('note') or '').strip()[:200],
            })
        return sanitized

    def _normalize_stock_code(self, code: str) -> Dict[str, str]:
        """归一成 ``{'digits': '000001', 'market': 'sz'}``，无法识别抛 ValueError。

        实现统一收敛到 ``app/core/secid.py``（单一真源），这里只保留薄委托：
        因子/自选/回测等多处都要"是不是同一只票"的判断，各写一份必然走样
        （历史 bug：9 开头一律当沪市，北交所 920xxx 会被存成 920000.SH）。
        """
        return split_secid(code)

    def _to_float(self, value: Any) -> Optional[float]:
        try:
            if value in [None, '']:
                return None
            return float(value)
        except Exception:
            return None

    def _load_stock_list_disk(self) -> None:
        """从磁盘恢复代码->名称表，避免每次重启都付一次全市场分页拉取的代价。"""
        try:
            payload = self._load_json(self._stock_list_file, {})
            records = (payload or {}).get("records") or []
            updated = float((payload or {}).get("updated") or 0)
            if records and time.time() - updated < 86400:
                self._stock_list_cache = records
                self._stock_list_last_update = updated
                logger.info("stock_list_loaded_from_disk count=%s", len(records))
        except Exception as exc:  # noqa: BLE001
            logger.warning("stock_list_disk_load_failed error=%r", exc)

    def _save_stock_list_disk(self) -> None:
        try:
            self._stock_list_file.write_text(
                json.dumps({"updated": self._stock_list_last_update,
                            "records": self._stock_list_cache},
                           ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("stock_list_disk_save_failed error=%r", exc)

    def _refresh_stock_list_cache(self) -> None:
        """拉取全市场代码->名称表。

        并发保护是必须的：组合类接口会在线程池里逐只股票查名称，缓存为空时
        若不加锁，N 个线程会同时拉 N 次全市场列表，并并发首次 import openpyxl，
        触发 "partially initialized module 'openpyxl'" 的循环导入报错，
        导致名称表永远拉不到、股票名退化成 secID。
        失败后 10 分钟内不再重试，避免每次请求都阻塞在网络上。
        """
        with self._stock_list_lock:
            if self._stock_list_cache and time.time() - self._stock_list_last_update < 86400:
                return
            if self._stock_list_last_fail and time.time() - self._stock_list_last_fail < 600:
                return
            try:
                import openpyxl  # noqa: F401  预热：akshare 读 Excel 依赖，避免线程内并发首导入
            except Exception:  # noqa: BLE001
                pass
            try:
                df = self._market_tool.get_stock_list()
                if df is not None and not df.empty:
                    self._stock_list_cache = df[['code', 'name']].to_dict('records')
                    self._stock_list_last_update = time.time()
                    self._stock_list_last_fail = 0.0
                    self._save_stock_list_disk()
                else:
                    self._stock_list_last_fail = time.time()
                    logger.warning("stock_list_refresh_empty")
            except Exception as exc:
                self._stock_list_last_fail = time.time()
                logger.warning("stock_list_refresh_failed error=%r", exc)

    def _get_cached_stock_name(self, digits: str) -> Optional[str]:
        if not self._stock_list_cache or time.time() - self._stock_list_last_update > 86400:
            self._refresh_stock_list_cache()
        for item in self._stock_list_cache:
            if str(item.get('code')) == digits:
                return str(item.get('name') or '').strip() or None
        # 场内基金不在全市场股票表里（那张表只有 A 股），名称只能问行情接口。
        # 不补这一步，ETF 在持仓表/卖出计划里会显示成裸代码「159934.SZ」。
        return self._get_fund_name(digits)

    def _get_fund_name(self, digits: str) -> Optional[str]:
        """基金名称，缓存 12 小时。基金改名极少，但也不能每次调用都打一次网络。"""
        cache = getattr(self, '_fund_name_cache', None)
        if cache is None:
            cache = {}
            self._fund_name_cache = cache
        now = time.time()
        hit = cache.get(digits)
        if hit and now - hit[1] < 43200:
            return hit[0]
        if digits[:2] not in self._FUND_PREFIXES:
            return None
        try:
            # 必须用 sh510300 / sz159934 这种腾讯原生格式；
            # 传 510300.SH 时沪市那只取不到（深市碰巧能过，别因此以为格式没问题）。
            symbol = self._fund_symbol(digits)
            result = self._tencent_tool.fetch_quotes([symbol])
            quote = result.get(digits) or {}
            name = str(quote.get('name') or '').strip() or None
        except Exception as exc:  # noqa: BLE001
            logger.debug("fund_name_fetch_failed digits=%s error=%r", digits, exc)
            name = None
        # 只在拿到名字时缓存：否则一次网络抖动会把 None 锁死 12 小时，
        # 之后这只 ETF 在整个进程生命周期里都显示成裸代码。
        if name:
            cache[digits] = (name, now)
        return name

    def _get_cached_spot_data(self, force_refresh: bool = False) -> Optional[pd.DataFrame]:
        if not force_refresh and self._spot_data_cache is not None and time.time() - self._spot_data_last_update < 300:
            return self._spot_data_cache
        # 单飞：59 页全市场爬取要数秒，30 秒轮询若并发触发会堆出多个爬取线程，
        # 抢锁、抢带宽、还加速触发东财限流。同一时刻只允许一个在爬，其余直接用旧缓存。
        with self._spot_refresh_lock:
            if not force_refresh and self._spot_data_cache is not None and time.time() - self._spot_data_last_update < 300:
                return self._spot_data_cache
            if self._spot_refreshing:
                return self._spot_data_cache
            self._spot_refreshing = True
            try:
                df = self._market_tool.get_spot_df()
                if df is not None and not df.empty:
                    self._spot_data_cache = df
                    self._spot_data_last_update = time.time()
                    return df
            except Exception as exc:
                print(f'Error refreshing spot cache: {exc}')
            finally:
                self._spot_refreshing = False
        return self._spot_data_cache

    def _first_existing_column(self, columns: List[str], candidates: List[str], fallback_index: Optional[int] = None) -> Optional[str]:
        for candidate in candidates:
            if candidate in columns:
                return candidate
        if fallback_index is not None and 0 <= fallback_index < len(columns):
            return columns[fallback_index]
        return None

    def _get_row_value(self, row: Dict[str, Any], columns: List[str], candidates: List[str], fallback_index: Optional[int] = None) -> Any:
        column = self._first_existing_column(columns, candidates, fallback_index)
        return row.get(column) if column else None

    @contextmanager
    def _without_proxy_env(self):
        proxy_keys = ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']
        backup = {key: os.environ.get(key) for key in proxy_keys}
        try:
            for key in proxy_keys:
                os.environ.pop(key, None)
            yield
        finally:
            for key, value in backup.items():
                if value is not None:
                    os.environ[key] = value

    def _kline_cache_key(self, code: str, start_date: str, end_date: str, frequency: str) -> str:
        normalized = self._normalize_stock_code(code)
        return f"{normalized['market']}.{normalized['digits']}|{frequency}|{start_date}|{end_date}"

    def _kline_cache_ttl(self, frequency: str) -> int:
        if frequency in ['1', '5', '15', '1d', '5d', 'minute']:
            return 60
        if frequency in ['30', '60']:
            return 180
        return 900

    def _get_cached_kline(self, key: str, frequency: str) -> Optional[List[Dict[str, Any]]]:
        now = time.time()
        ttl = self._kline_cache_ttl(frequency)
        with self._kline_cache_lock:
            cached = self._kline_cache.get(key)
            if not cached:
                return None
            if now - float(cached.get('updated_at') or 0.0) > ttl:
                self._kline_cache.pop(key, None)
                return None
            data = cached.get('data') or []
            return [dict(item) for item in data]

    def _set_cached_kline(self, key: str, data: List[Dict[str, Any]]) -> None:
        with self._kline_cache_lock:
            self._kline_cache[key] = {
                'updated_at': time.time(),
                'data': [dict(item) for item in data],
            }

    def _cache_empty_kline_if_minute(self, cache_key: str, frequency: str) -> None:
        """分钟级"取不到数据"也缓存 60 秒。

        首页每只自选都会拉一次分时，若空结果不缓存，就会每 60 秒把所有
        失败源（星耀 → 雪球 → 东财 → akshare）重新走一遍，每票烧掉 4–7 秒。
        日线不这么做：偶发失败应该立刻能重试，否则绩效/回测会长时间拿不到数据。
        """
        if frequency in ('1', '5', '15', '30', '60', '1d', '5d', 'minute'):
            self._set_cached_kline(cache_key, [])

    _MINUTE_FREQS = ('1', 'minute', '1d', '5d')

    @staticmethod
    def _daily_window_sufficient(records: List[Dict[str, Any]], start_date: str, end_date: str) -> bool:
        """显式日期窗口的日线数量下限。

        星耀对长窗口日线也偶发只回 1 根（与周线同源的残缺问题），1 根日线
        画出来的净值/绩效曲线是假的。A 股约 5/7 的日历日是交易日，这里取
        0.2 的宽松系数；短窗口（<30 天）不校验，避免误伤合法的小样本请求。
        """
        try:
            span = (datetime.datetime.strptime(str(end_date)[:10], '%Y-%m-%d')
                    - datetime.datetime.strptime(str(start_date)[:10], '%Y-%m-%d')).days
        except Exception:
            return True
        if span < 30:
            return True
        return len(records) >= max(5, int(span * 0.2))

    def _minute_records_fresh(self, records: List[Dict[str, Any]]) -> bool:
        """分钟级数据是否"新"到可以当当日分时用。

        今天是工作日且已过 09:15 → 最新一根必须是今天的；
        非工作日或早盘集合竞价前，最后一个交易日的数据视为可接受。
        法定节假日本地无法判断，统一按"不新鲜"处理走当日分时兜底链，
        兜底链也拿不到今天的，get_k_line_data 末尾会回退用旧数据。
        """
        if not records:
            return False
        last = str(records[-1].get('date') or '')
        m = re.search(r'(\d{4})-(\d{2})-(\d{2})', last)
        if not m:
            return False
        last_day = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        now = datetime.datetime.now()
        if last_day == now.date():
            return True
        return now.weekday() >= 5 or now.time() < datetime.time(9, 15)

    def _get_source_cache(self, key: str, ttl_sec: float) -> Any:
        now = time.time()
        with self._source_cache_lock:
            cached = self._source_cache.get(key)
            if not cached:
                return None
            if now - float(cached.get('updated_at') or 0.0) > ttl_sec:
                self._source_cache.pop(key, None)
                return None
            logger.info("source_cache_hit key=%s", key)
            return copy.deepcopy(cached.get('data'))

    def _set_source_cache(self, key: str, data: Any) -> None:
        with self._source_cache_lock:
            self._source_cache[key] = {
                'updated_at': time.time(),
                'data': copy.deepcopy(data),
            }

    def _source_cache_key(self, source: str, *parts: Any) -> str:
        normalized_parts = [str(part).strip() for part in parts]
        return f"{source}|" + "|".join(normalized_parts)

    def _get_source_inflight_lock(self, key: str) -> Lock:
        with self._source_cache_lock:
            lock = self._source_inflight_locks.get(key)
            if lock is None:
                lock = Lock()
                self._source_inflight_locks[key] = lock
            return lock

    def _build_secid(self, code: str) -> str:
        normalized = self._normalize_stock_code(code)
        market = normalized['market']
        market_id = '1' if market == 'sh' else '0'
        return f"{market_id}.{normalized['digits']}"

    def _http_get_json(self, url: str, params: Dict[str, Any], ttl_sec: Optional[float] = None, cache_key: Optional[str] = None) -> Dict[str, Any]:
        key = cache_key or self._source_cache_key('http_json', url, json.dumps(params, sort_keys=True, ensure_ascii=False))
        if ttl_sec and ttl_sec > 0:
            cached = self._get_source_cache(key, ttl_sec)
            if isinstance(cached, dict):
                return cached
        inflight_lock = self._get_source_inflight_lock(key) if ttl_sec and ttl_sec > 0 else None
        if inflight_lock:
            inflight_lock.acquire()
            cached = self._get_source_cache(key, ttl_sec or 0)
            if isinstance(cached, dict):
                inflight_lock.release()
                return cached
        try:
            data = self._http_json_tool.get_json(
                url=url,
                params=params,
                headers={
                    'User-Agent': 'Mozilla/5.0',
                    'Referer': 'https://quote.eastmoney.com/',
                },
                retries=2,
                retry_sleep_sec=0.4,
            )
            if ttl_sec and ttl_sec > 0:
                self._set_source_cache(key, data)
            return data
        finally:
            if inflight_lock:
                inflight_lock.release()

    def _xueqiu_auth(self) -> Dict[str, Optional[str]]:
        auth = self._load_xueqiu_auth()
        return {
            'cookie': str(auth.get('xueqiu_cookie') or '').strip() or None,
            'user_agent': str(auth.get('xueqiu_user_agent') or '').strip() or None,
        }

    def _refresh_xueqiu_guest_cookie(self) -> bool:
        auth_cfg = self._load_xueqiu_auth()
        ua = str(auth_cfg.get('xueqiu_user_agent') or '').strip() or None
        refresh_url = str(auth_cfg.get('xueqiu_refresh_url') or '').strip() or None
        headless = bool(auth_cfg.get('xueqiu_refresh_headless', True))
        executable_path = str(auth_cfg.get('xueqiu_browser_executable_path') or '').strip() or None
        refreshed = self._xueqiu_cookie_refresher.refresh_guest_cookie(
            user_agent=ua,
            refresh_url=refresh_url,
            headless=headless,
            executable_path=executable_path,
        )
        cookie = str(refreshed.get("cookie") or "").strip()
        if not cookie:
            logger.warning(
                "xueqiu_cookie_refresh_failed source=%s refresh_url=%s reason=no_cookie",
                refreshed.get("source"),
                refreshed.get("refresh_url") or refresh_url,
            )
            return False
        validated = self._validate_xueqiu_cookie(cookie, str(refreshed.get("user_agent") or ua or ""))
        if not validated:
            logger.warning(
                "xueqiu_cookie_refresh_failed source=%s refresh_url=%s reason=kline_validation_failed",
                refreshed.get("source"),
                refreshed.get("refresh_url") or refresh_url,
            )
            return False
        self._save_xueqiu_auth({
            "xueqiu_cookie": cookie,
            "xueqiu_user_agent": str(refreshed.get("user_agent") or ua or ""),
            "xueqiu_refresh_url": str(refreshed.get("refresh_url") or refresh_url or ""),
            "xueqiu_refresh_headless": headless,
            "xueqiu_browser_executable_path": executable_path or "",
        })
        logger.info(
            "xueqiu_cookie_refresh_ok source=%s refresh_url=%s",
            refreshed.get("source"),
            refreshed.get("refresh_url") or refresh_url,
        )
        return True

    def _validate_xueqiu_cookie(self, cookie: str, user_agent: str) -> bool:
        try:
            records = self._xueqiu_tool.fetch_kline_by_code(
                "002230.SZ",
                frequency="w",
                count=-284,
                cookie=cookie,
                user_agent=user_agent or None,
            )
            return bool(records)
        except Exception as exc:
            logger.warning("xueqiu_cookie_validate_failed error=%r", exc)
            return False

    def _fetch_eastmoney_quote(self, code: str, timeout_sec: Optional[float] = None) -> Dict[str, Any]:
        secid = self._build_secid(code)
        try:
            payload = self._market_tool.fetch_eastmoney_quote(secid)
        except Exception as exc:
            # 云 IP 被 push2 限流时典型表现就是断连；立刻进冷却，避免重试把封禁延长
            self._note_eastmoney_failure(exc)
            raise
        data = (payload or {}).get('data') or {}
        if not data:
            return {}

        def scaled_price(field: str) -> Optional[float]:
            return self._to_float(data.get(field))

        def scaled_pct(field: str) -> Optional[float]:
            return self._to_float(data.get(field))

        digits = str(data.get('f57') or self._normalize_stock_code(code)['digits'])
        self._note_source_success('eastmoney')
        return {
            'code': digits,
            'name': str(data.get('f58') or self._get_cached_stock_name(digits) or digits),
            'price': scaled_price('f43'),
            'high': scaled_price('f44'),
            'low': scaled_price('f45'),
            'open': scaled_price('f46'),
            'volume': self._to_float(data.get('f47')),
            'amount': self._to_float(data.get('f48')),
            'prev_close': scaled_price('f60'),
            'change': scaled_price('f169'),
            'pct_chg': scaled_pct('f170'),
            'source': 'eastmoney',
        }

    # ---------- 行情源通用熔断 ----------
    # 每个源记连续失败次数：达到 threshold 触发 cooldown_sec 冷却，
    # 冷却期内兜底链跳过该源；任何一次成功即复位。

    def _source_in_cooldown(self, name: str) -> bool:
        state = self._source_health.get(name)
        return bool(state and time.time() < float(state.get('cooldown_until') or 0.0))

    def _note_source_failure(self, name: str, cooldown_sec: float = 600.0,
                             threshold: int = 1, reason: Any = None) -> None:
        state = self._source_health.setdefault(name, {'fails': 0, 'cooldown_until': 0.0})
        state['fails'] = int(state.get('fails') or 0) + 1
        if state['fails'] >= max(1, threshold):
            state['cooldown_until'] = time.time() + cooldown_sec
            state['fails'] = 0
            logger.warning("source_cooldown source=%s cooldown=%ss reason=%s",
                           name, cooldown_sec, str(reason)[:140])

    def _note_source_success(self, name: str) -> None:
        self._source_health.pop(name, None)

    def source_health_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """给前端/诊断用的只读快照：每个源是否在冷却、还剩多久。"""
        now = time.time()
        out: Dict[str, Dict[str, Any]] = {}
        for name, state in self._source_health.items():
            remain = float(state.get('cooldown_until') or 0.0) - now
            if remain > 0:
                out[name] = {'cooldown': True, 'remaining_sec': round(remain)}
        return out

    def _eastmoney_in_cooldown(self) -> bool:
        return self._source_in_cooldown('eastmoney')

    def _amazingdata_in_cooldown(self) -> bool:
        return self._source_in_cooldown('amazingdata')

    def _amazingdata_ready(self) -> bool:
        """星耀能否作为兜底立刻取数。

        三个条件缺一不可：配了凭证、不在熔断冷却期、常驻池已暖机。
        最后一条最关键——池未就绪时 `fetch_spot_quotes` 会立刻返回空，
        而首次调用只能触发后台暖机（实测 9~17s），当场拿不到数。
        所以「能不能兜底」严格等于这里是否 True，绝不拿请求时间去等暖机。
        """
        if not self._amazingdata_credentials:
            return False
        if self._amazingdata_in_cooldown():
            return False
        try:
            return bool(_ad_pool_ready())
        except Exception:
            return False

    def warmup_amazingdata(self) -> bool:
        """后台发起星耀暖机，让兜底能力在真需要之前就绪。

        只在 `AD_WARMUP_ON_START=1` 时由应用启动流程调用。默认不开：
        登录会把同一账号在别处（MCP/本机）的会话踢下线，各机器只能选一个守着。
        """
        if not self._amazingdata_credentials or self._amazingdata_in_cooldown():
            return False
        creds = self._amazingdata_credentials
        try:
            logger.info("amazingdata_warmup_start")
            started = _ad_warmup_async(
                creds.get('username', ''), creds.get('password', ''),
                creds.get('host', ''), int(creds.get('port') or 8600),
            )
            logger.info("amazingdata_warmup_started ok=%s", started)
            return bool(started)
        except Exception as exc:
            logger.warning("amazingdata_warmup_failed error=%r", exc)
            return False

    def _note_amazingdata_failure(self, exc: Any) -> None:
        """星耀不可用时熔断，避免每个请求都先等它失败再降级。

        两类要分开处理：
        - **权限类**（403 / 无访问权限 / 登录失败）：账号就不具备该权限，
          重试一万次也一样，熔断 30 分钟，等用户自己换账号或改配置；
        - **瞬时故障**（超时 / 连接断开）：可能马上恢复，只熔断 2 分钟。
        """
        text = f"{type(exc).__name__}: {exc}"
        lowered = text.lower()
        permission_markers = ('403', '无访问权限', '权限', 'permission', 'forbidden', 'denied', '401', 'unauthorized')
        transient_markers = ('timeout', 'timed out', 'connect', 'disconnected', 'connection')
        if any(marker in lowered for marker in permission_markers):
            self._note_source_failure('amazingdata', cooldown_sec=1800, threshold=1, reason=text)
        elif any(marker in lowered for marker in transient_markers):
            self._note_source_failure('amazingdata', cooldown_sec=120, threshold=1, reason=text)
        else:
            self._note_source_failure('amazingdata', cooldown_sec=120, threshold=2, reason=text)

    def _note_eastmoney_failure(self, exc: Any) -> None:
        """东财 push2 被限流后熔断 10 分钟。

        限流是 IP 级的，越重试恢复越慢（首次约 5 分钟，反复触发可能 40 分钟以上）。
        熔断期间现价改由腾讯/雪球承担，代价只是少一个数据源。
        """
        text = f"{type(exc).__name__}: {exc}"
        if 'RateLimit' in text or 'disconnected' in text or 'ConnectError' in text:
            self._note_source_failure('eastmoney', cooldown_sec=600, threshold=1, reason=text)

    def _fetch_xueqiu_quote(self, code: str) -> Dict[str, Any]:
        auth = self._xueqiu_auth()
        cookie = auth.get('cookie')
        ua = auth.get('user_agent')
        symbol = self._xueqiu_tool.to_symbol(code)
        cache_key = self._source_cache_key('xueqiu_quote', symbol, bool(cookie), ua or '')
        cached = self._get_source_cache(cache_key, 20)
        if isinstance(cached, dict):
            return cached
        inflight_lock = self._get_source_inflight_lock(cache_key)
        with inflight_lock:
            cached = self._get_source_cache(cache_key, 20)
            if isinstance(cached, dict):
                return cached
            # 雪球 cookie 过期的典型表现是连续 302/HTML：连续失败 2 次就冷却 30 分钟，
            # 期间兜底链直接跳过它，别让每只票都先白等 2 秒超时。
            try:
                quote = self._xueqiu_tool.fetch_quote_by_code(code, cookie=cookie, user_agent=ua)
            except Exception as exc:
                self._note_source_failure('xueqiu', cooldown_sec=1800, threshold=2, reason=exc)
                raise
            if quote and quote.get('price') is not None:
                self._note_source_success('xueqiu')
                self._set_source_cache(cache_key, quote)
            return quote

    def _fetch_tencent_quote(self, code: str) -> Dict[str, Any]:
        """单只票的腾讯报价，失败返回 {}（不抛异常，供兜底链顺序尝试）。"""
        try:
            digits = self._normalize_stock_code(code)['digits']
            cache_key = self._source_cache_key('tencent_quote', digits)
            cached = self._get_source_cache(cache_key, 20)
            if isinstance(cached, dict):
                return cached
            result = self._tencent_tool.fetch_quotes([code])
            quote = result.get(digits) or {}
            if isinstance(quote, dict) and quote.get('price') is not None:
                self._set_source_cache(cache_key, quote)
                return quote
        except Exception as exc:
            logger.warning("tencent_quote_failed code=%s error=%r", code, exc)
        return {}

    def _fetch_tencent_quotes(self, codes: List[str]) -> List[Dict[str, Any]]:
        """批量取报价（自选/持仓列表用），返回与入参同序的列表，取不到的 price=None。"""
        clean = [c for c in codes if c]
        if not clean:
            return []
        try:
            result = self._tencent_tool.fetch_quotes(clean)
        except Exception as exc:
            logger.warning("tencent_quotes_failed error=%r", exc)
            result = {}
        out: List[Dict[str, Any]] = []
        for code in clean:
            try:
                digits = self._normalize_stock_code(code)['digits']
            except Exception:
                continue
            quote = result.get(digits)
            if isinstance(quote, dict) and quote.get('price') is not None:
                if not quote.get('name'):
                    quote['name'] = self._get_cached_stock_name(digits) or digits
                out.append(self._normalize_spot_quote(quote))
        return out

    # ---------- 星耀数智 AmazingData SDK（可选实时主源） ----------
    def _load_amazingdata_credentials(self) -> Dict[str, Any]:
        """优先读取环境变量 AD_USERNAME/AD_PASSWORD/AD_HOST/AD_PORT，回退到 config/amazingdata_auth.json。"""
        username = os.environ.get('AD_USERNAME') or ''
        password = os.environ.get('AD_PASSWORD') or ''
        host = os.environ.get('AD_HOST') or ''
        port = os.environ.get('AD_PORT') or ''
        if username and password and host and port:
            return {'username': username, 'password': password, 'host': host, 'port': int(port)}
        auth_file = self._config_dir / 'amazingdata_auth.json'
        data = self._load_json(auth_file, {})
        if isinstance(data, dict) and data.get('username') and data.get('password') and data.get('host') and data.get('port'):
            return {
                'username': str(data['username']),
                'password': str(data['password']),
                'host': str(data['host']),
                'port': int(data['port']),
            }
        return {}

    def _get_amazingdata_tool(self) -> Optional[Any]:
        if self._amazingdata_tool is None:
            creds = self._amazingdata_credentials
            if not creds:
                return None
            self._amazingdata_tool = AmazingDataTool(
                username=creds['username'],
                password=creds['password'],
                host=creds['host'],
                port=creds['port'],
                timeout_sec=10.0,
            )
        return self._amazingdata_tool

    def _fetch_amazingdata_quote(self, code: str) -> Dict[str, Any]:
        if self._amazingdata_in_cooldown():
            return {}
        try:
            tool = self._get_amazingdata_tool()
            if tool is None:
                return {}
            normalized = self._normalize_stock_code(code)
            digits = normalized['digits']
            result = tool.fetch_spot_quotes([code], timeout_sec=8.0)
            quote = result.get(digits)
            # price 必须为正数：开盘前/数据异常时快照会给 0.0，
            # 「is not None」会把 0 当有效价采纳，页面直接显示 0 元、涨跌幅 -100%
            price = self._to_float((quote or {}).get('price'))
            if not isinstance(quote, dict) or not price or price <= 0:
                # 池未暖机时返回空属正常降级；池就绪还拿不到，记一次失败
                if getattr(tool, 'is_ready', lambda: False)():
                    self._note_source_failure('amazingdata', cooldown_sec=300, threshold=2,
                                              reason=f'pool ready but no valid quote for {digits}')
                return {}
            if not quote.get('name'):
                quote['name'] = self._get_cached_stock_name(digits) or digits
            self._note_source_success('amazingdata')
            return self._normalize_spot_quote(quote)
        except Exception as exc:
            logger.warning("amazingdata_spot_fetch_failed code=%s error=%r", code, exc)
            self._note_amazingdata_failure(exc)
            return {}

    def _fetch_amazingdata_favorites(self, codes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """星耀批量报价。**只返回真的拿到价的票**，拿不到的不占位。

        之前的实现会把取不到的票补成 price=None 占位返回，调用方看到"非空列表"
        就整体采用了，结果是这些票永远没有现价、也不走后面的兜底源。

        `codes` 为空时取全部自选；显式传入则只查这些票（批量兜底补缺时用，
        避免为了 3 只缺价的票去请求全部 30 只）。
        """
        if codes is None:
            codes = [item.get('secID') for item in self._favorite_cache if item.get('secID')]
        codes = [c for c in codes if c]
        if not codes or self._amazingdata_in_cooldown():
            return []
        try:
            tool = self._get_amazingdata_tool()
            if tool is None:
                return []
            result = tool.fetch_spot_quotes(codes, timeout_sec=8.0)
        except Exception as exc:
            logger.warning("amazingdata_favorites_fetch_failed error=%r", exc)
            self._note_amazingdata_failure(exc)
            return []
        if not result:
            # AD 无数据时交由上层继续兜底；池已就绪还拿不到 → 记一次失败（如被互踢/服务端异常）
            try:
                if getattr(tool, 'is_ready', lambda: False)():
                    self._note_source_failure('amazingdata', cooldown_sec=300, threshold=2,
                                              reason='pool ready but batch empty')
            except Exception:
                pass
            return []
        out: List[Dict[str, Any]] = []
        valid = 0
        for code in codes:
            try:
                digits = self._normalize_stock_code(code)['digits']
            except Exception:
                continue
            quote = result.get(digits)
            # 开盘前快照可能给 price=0.0，必须按「无有效价」处理，否则页面显示 0 元
            price = self._to_float((quote or {}).get('price'))
            if not isinstance(quote, dict) or not price or price <= 0:
                continue
            valid += 1
            if not quote.get('name'):
                quote['name'] = self._get_cached_stock_name(digits) or digits
            out.append(self._normalize_spot_quote(quote))
        if valid > 0:
            self._note_source_success('amazingdata')
        elif getattr(tool, 'is_ready', lambda: False)():
            # 池就绪但整批都没有有效价（如开盘前快照为空）→ 记失败，让链路走腾讯等兜底
            self._note_source_failure('amazingdata', cooldown_sec=300, threshold=2,
                                      reason='pool ready but no valid price in batch')
            return []
        return out

    def _fetch_amazingdata_kline(self, code: str, start_date: str, end_date: str, frequency: str) -> List[Dict[str, Any]]:
        """星耀数智 K线/分时（主源原则：星耀有的数据优先走星耀）。

        支持 d/w/m 与分钟级(1/5/15/30/60...)；失败 / 无权限 / 无数据返回空列表，
        调用方据此回退到雪球等降级数据源。
        """
        if self._amazingdata_in_cooldown():
            return []
        try:
            tool = self._get_amazingdata_tool()
            if tool is None:
                return []
            freq = str(frequency or 'd').strip().lower()
            result = tool.fetch_kline([code], start_date, end_date, freq)
            if not result:
                return []
            digits = self._normalize_stock_code(code)['digits']
            rows = list(result.get(digits) or [])
            if not rows and len(result) == 1:
                # 兜底：只查了一只股票时，取唯一键的数据
                rows = list(next(iter(result.values())) or [])
            if not rows:
                return []
            # 分钟级窗口裁剪，与雪球分时行为对齐：1d 只留最近交易日，5d 留最近 5 个交易日
            if freq in ('1', 'minute', '1d'):
                rows = self._clip_kline_rows_to_days(rows, 1)
            elif freq == '5d':
                rows = self._clip_kline_rows_to_days(rows, 5)
            logger.info(
                "amazingdata_kline_fetch_ok code=%s freq=%s rows=%s", code, freq, len(rows)
            )
            return rows
        except Exception as exc:
            logger.warning("amazingdata_kline_fetch_failed code=%s freq=%s error=%r", code, frequency, exc)
            self._note_amazingdata_failure(exc)
            return []

    @staticmethod
    def _clip_kline_rows_to_days(rows: List[Dict[str, Any]], max_days: int) -> List[Dict[str, Any]]:
        days = sorted({str(r.get('date') or '')[:10] for r in rows}, reverse=True)[:max(1, max_days)]
        keep = set(days)
        return [r for r in rows if str(r.get('date') or '')[:10] in keep]

    def _fetch_xueqiu_kline(self, code: str, start_date: str = None, end_date: str = None, frequency: str = 'd') -> List[Dict[str, Any]]:
        period = self._xueqiu_tool.period_from_frequency(frequency)
        if not period:
            return []
        auth = self._xueqiu_auth()
        symbol = self._xueqiu_tool.to_symbol(code)
        begin_ms = self._xueqiu_tool.begin_ms_from_end_date(end_date or datetime.datetime.now().strftime('%Y-%m-%d'))
        count = self._xueqiu_tool.count_from_range(start_date or '2020-01-01', end_date or datetime.datetime.now().strftime('%Y-%m-%d'), frequency)
        cache_key = self._source_cache_key('xueqiu_kline', symbol, period, begin_ms, count)
        cached = self._get_source_cache(cache_key, self._kline_cache_ttl(frequency))
        if isinstance(cached, list):
            return cached
        inflight_lock = self._get_source_inflight_lock(cache_key)
        with inflight_lock:
            cached = self._get_source_cache(cache_key, self._kline_cache_ttl(frequency))
            if isinstance(cached, list):
                return cached
            try:
                records = self._xueqiu_tool.fetch_kline_by_code(
                    code,
                    start_date=start_date,
                    end_date=end_date,
                    frequency=frequency,
                    count=-284,
                    cookie=auth.get('cookie'),
                    user_agent=auth.get('user_agent'),
                )
            except Exception as exc:
                msg = str(exc)
                if "400016" in msg or "重新登录帐号" in msg or "Client error '400" in msg:
                    if self._refresh_xueqiu_guest_cookie():
                        auth_retry = self._xueqiu_auth()
                        records = self._xueqiu_tool.fetch_kline_by_code(
                            code,
                            start_date=start_date,
                            end_date=end_date,
                            frequency=frequency,
                            count=-284,
                            cookie=auth_retry.get('cookie'),
                            user_agent=auth_retry.get('user_agent'),
                        )
                    else:
                        raise
                else:
                    raise
            if records:
                self._set_source_cache(cache_key, records)
            return records

    def _fetch_tencent_minute(self, code: str, days: int = 1) -> List[Dict[str, Any]]:
        """腾讯分时，转成 K 线记录形状（只有收盘价，OHLC 同价）。

        服务器实测：雪球 cookie 过期返回登录页(1.4s 失败)、东财 push2 被限流断连
        (1.8s 失败)，整条降级链走完要 4–7 秒还拿不到数据；腾讯 0.15 秒就返回
        240+ 个点。所以分时以腾讯为首选，days=5 走它的多日接口。
        """
        symbol = self._tencent_tool.to_symbol(code)
        cache_key = self._source_cache_key('tencent_minute', symbol, str(days))
        cached = self._get_source_cache(cache_key, 30)
        if isinstance(cached, list):
            return cached

        lock = self._get_source_inflight_lock(cache_key)
        with lock:
            cached = self._get_source_cache(cache_key, 30)
            if isinstance(cached, list):
                return cached
            try:
                points = self._tencent_tool.fetch_minute(code, days=days) or []
            except Exception as exc:  # noqa: BLE001
                logger.warning("tencent_minute_failed code=%s days=%s error=%r", code, days, exc)
                return []
            records = [
                {
                    'date': point.get('time'),
                    'open': point.get('price'),
                    'close': point.get('price'),
                    'high': point.get('price'),
                    'low': point.get('price'),
                    'volume': point.get('volume'),
                    'amount': point.get('amount'),
                }
                for point in points
                if point.get('time') and point.get('price') is not None
            ]
            if records:
                self._set_source_cache(cache_key, records)
                logger.info("tencent_minute_ok code=%s days=%s rows=%s", code, days, len(records))
            return records

    def _fetch_xueqiu_minute(self, code: str, period: str = '1d') -> List[Dict[str, Any]]:
        auth = self._xueqiu_auth()
        symbol = self._xueqiu_tool.to_symbol(code)
        cache_key = self._source_cache_key('xueqiu_minute', symbol, period)
        cached = self._get_source_cache(cache_key, 30)
        if isinstance(cached, list):
            return cached
        inflight_lock = self._get_source_inflight_lock(cache_key)
        with inflight_lock:
            cached = self._get_source_cache(cache_key, 30)
            if isinstance(cached, list):
                return cached
            records = self._xueqiu_tool.fetch_minute_by_code(
                code,
                period=period,
                cookie=auth.get('cookie'),
                user_agent=auth.get('user_agent'),
            )
            if records:
                self._set_source_cache(cache_key, records)
            return records

    def get_trade_history(self, code: str, count: int = 10) -> List[Dict[str, Any]]:
        auth = self._xueqiu_auth()
        symbol = self._xueqiu_tool.to_symbol(code)
        safe_count = max(1, min(int(count or 10), 100))
        cache_key = self._source_cache_key('xueqiu_trade_history', symbol, safe_count)
        cached = self._get_source_cache(cache_key, 60)
        if isinstance(cached, list):
            return cached
        inflight_lock = self._get_source_inflight_lock(cache_key)
        with inflight_lock:
            cached = self._get_source_cache(cache_key, 60)
            if isinstance(cached, list):
                return cached
            records = self._xueqiu_tool.fetch_trade_history_by_code(
                code,
                count=safe_count,
                cookie=auth.get('cookie'),
                user_agent=auth.get('user_agent'),
            )
            if records:
                self._set_source_cache(cache_key, records)
            return records

    def _fetch_spot_from_cache_fast(self, code: str) -> Dict[str, Any]:
        try:
            digits = self._normalize_stock_code(code)['digits']
        except Exception:
            return {}
        df = self._spot_data_cache
        if df is None or df.empty:
            return {}
        columns = [str(col) for col in df.columns]
        code_col = self._first_existing_column(columns, ['code', 'symbol', 'ticker', 'secid'], 1)
        if not code_col:
            return {}
        row = df[df[code_col].astype(str) == digits]
        if row.empty:
            return {}
        data = row.iloc[0].to_dict()
        cached_name = self._get_cached_stock_name(digits)
        name = self._get_row_value(data, columns, ['name', 'sec_name', 'display_name'], 2) or cached_name or digits
        return {
            'code': digits,
            'name': str(name),
            'price': self._to_float(self._get_row_value(data, columns, ['price', 'last_price', 'close'], 3)),
            'pct_chg': self._to_float(self._get_row_value(data, columns, ['pct_chg', 'change_percent', 'pct'], 4)),
            'change': self._to_float(self._get_row_value(data, columns, ['change', 'change_amount'], 5)),
            'volume': self._to_float(self._get_row_value(data, columns, ['volume', 'deal_volume'], 6)),
            'amount': self._to_float(self._get_row_value(data, columns, ['amount', 'turnover', 'deal_amount'], 7)),
            'high': self._to_float(self._get_row_value(data, columns, ['high'], 9)),
            'low': self._to_float(self._get_row_value(data, columns, ['low'], 10)),
            'open': self._to_float(self._get_row_value(data, columns, ['open'], 11)),
            'prev_close': self._to_float(self._get_row_value(data, columns, ['prev_close', 'pre_close'], 12)),
            'source': 'akshare_cache',
        }

    def _run_with_timeout(self, fn, timeout_sec: float) -> Dict[str, Any]:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(fn)
            try:
                return future.result(timeout=timeout_sec)
            except Exception:
                return {}

    def _normalize_spot_quote(self, quote: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(quote, dict):
            return {}
        normalized = dict(quote)
        price = self._to_float(normalized.get('price'))
        prev_close = self._to_float(normalized.get('prev_close'))
        change = self._to_float(normalized.get('change'))
        pct_chg = self._to_float(normalized.get('pct_chg') or normalized.get('pctChg'))
        if change is None and price is not None and prev_close not in (None, 0):
            change = price - prev_close
        if pct_chg is None and change is not None and prev_close not in (None, 0):
            pct_chg = change / prev_close * 100
        normalized['price'] = price
        normalized['prev_close'] = prev_close
        normalized['change'] = change
        normalized['pct_chg'] = pct_chg
        return normalized

    def _extract_roe_from_dupont(self, code: str) -> Optional[float]:
        try:
            normalized = self._normalize_stock_code(code)
            symbol = f"{normalized['market'].upper()}{normalized['digits']}"
            payload = self._stock_dupont_tool.fetch(symbol)
            rows = payload.get("rows") if isinstance(payload, dict) else []
            if not isinstance(rows, list) or not rows:
                return None
            digits = normalized["digits"]
            target = None
            for row in rows:
                row_code = str(row.get("代码") or row.get("code") or "").strip()
                if row_code == digits:
                    target = row
                    break
            if target is None:
                target = rows[0]
            for key in ("ROE-3年平均", "ROE-24A", "ROE-23A", "ROE-22A", "ROE"):
                value = self._to_float((target or {}).get(key))
                if value is not None:
                    return value
        except Exception:
            pass
        try:
            normalized = self._normalize_stock_code(code)
            payload = self._stock_financial_tool.fetch(normalized["digits"], indicator="按报告期")
            rows = payload.get("rows") if isinstance(payload, dict) else []
            if not isinstance(rows, list) or not rows:
                return None
            latest = rows[0]
            for key in ("净资产收益率(%)", "ROE", "roe", "ROEJQ"):
                value = self._to_float((latest or {}).get(key))
                if value is not None:
                    return value
        except Exception:
            return None
        return None

    def _latest_xueqiu_valuation_from_kline(self, code: str) -> Dict[str, Optional[float]]:
        try:
            records = self._fetch_xueqiu_kline(code, frequency='d')
        except Exception as exc:
            logger.warning("valuation_kline_fetch_failed code=%s error=%r", code, exc)
            return {"pe": None, "pb": None}
        for row in reversed(records or []):
            pe = self._to_float((row or {}).get("pe"))
            pb = self._to_float((row or {}).get("pb"))
            if pe is not None or pb is not None:
                return {"pe": pe, "pb": pb}
        return {"pe": None, "pb": None}

    def _valuation_metrics_for_code(self, code: str, spot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        normalized = self._normalize_stock_code(code)
        digits = normalized["digits"]
        quote = spot if isinstance(spot, dict) else None
        pe = self._to_float((quote or {}).get("pe"))
        pb = self._to_float((quote or {}).get("pb"))
        if pe is None or pb is None:
            kline_metrics = self._latest_xueqiu_valuation_from_kline(digits)
            pe = pe if pe is not None else kline_metrics.get("pe")
            pb = pb if pb is not None else kline_metrics.get("pb")
        return {
            "code": digits,
            "pe": pe,
            "pb": pb,
            "roe": self._extract_roe_from_dupont(digits),
        }

    def _evaluate_absolute_valuation(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        pe = self._to_float((metrics or {}).get("pe"))
        pb = self._to_float((metrics or {}).get("pb"))
        roe = self._to_float((metrics or {}).get("roe"))
        zone = "unknown"
        reason = "insufficient_metrics"
        score = 0
        if pe is not None and pe > 0:
            if pe <= 20:
                score += 1
            elif pe >= 45:
                score -= 1
        if pb is not None and pb > 0:
            if pb <= 2.0:
                score += 1
            elif pb >= 6.0:
                score -= 1
        if roe is not None:
            if roe >= 12.0:
                score += 1
            elif 0 <= roe < 6.0:
                score -= 1
        if score >= 2:
            zone = "undervalued"
            reason = "low_pe_pb_and_healthy_roe"
        elif score <= -1:
            zone = "overvalued"
            reason = "valuation_is_high_or_profitability_weak"
        elif pe is not None or pb is not None or roe is not None:
            zone = "fair"
            reason = "valuation_near_fair_range"
        return {
            "zone": zone,
            "reason": reason,
            "score": score,
            "pe": pe,
            "pb": pb,
            "roe": roe,
            "method": "single_stock_absolute",
        }

    def _evaluate_valuation(self, code: str, spot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cache_key = self._source_cache_key("valuation", str(code or "").upper())
        cached = self._get_source_cache(cache_key, 600)
        if isinstance(cached, dict):
            return cached
        quote = spot if isinstance(spot, dict) else self.get_stock_spot_info(code)
        metrics = self._valuation_metrics_for_code(code, spot=quote)
        fallback = self._evaluate_absolute_valuation(metrics)
        profiles = self._load_json(self._profiles_file, {})
        profiles = profiles if isinstance(profiles, dict) else {}
        digits = self._normalize_stock_code(code)["digits"]
        profile = profiles.get(digits) if isinstance(profiles.get(digits), dict) else {}
        has_industry = any(str((profile or {}).get(key) or "").strip() for key in ("industry", "Industry", "hangye", "行业", "琛屼笟"))
        if not has_industry:
            company_info = self.ensure_company_info_cache(digits)
            if isinstance(company_info, dict) and company_info:
                profiles[digits] = company_info
        result = self._industry_valuation_tool.evaluate(
            code=code,
            profiles=profiles,
            metric_provider=lambda peer_code: self._valuation_metrics_for_code(peer_code),
            fallback=fallback,
        )
        result = self._sw_industry_valuation_tool.evaluate(
            code=code,
            metrics=metrics,
            fallback=result,
            profile=profiles.get(digits) if isinstance(profiles.get(digits), dict) else {},
            rules=self.get_valuation_rules(),
        )
        self._set_source_cache(cache_key, result)
        return result

    # ------------------------------------------------------------------
    # 多因子卖出决策上下文：财报 / 行业周期 / 市场周期
    #
    # 卖出目标价不能只看 K 线与 ATR。同一个技术形态，PE 90 分位和 PE 20 分位
    # 该给的目标价完全不同。这里把估值、财报、行业与市场周期统一取好，
    # 交给纯计算层（sell_factors / sell_target_service）做合成。
    # 全部带磁盘缓存：财报 3 天、行业周期 1 天、市场周期 6 小时，
    # 失败一律降级为 {} 而不是抛错（卖出计划不能因为一个数据源挂了就整体失败）。
    # ------------------------------------------------------------------

    _FUNDAMENTALS_TTL = 3 * 86400
    _INDUSTRY_CYCLE_TTL = 86400
    _MARKET_REGIME_TTL = 6 * 3600

    def get_fundamental_snapshot(self, code: str, force_refresh: bool = False) -> Dict[str, Any]:
        """最近两个报告期的关键财务指标（新浪财务分析指标）。"""
        digits = self._normalize_stock_code(code)["digits"]
        cache = self._load_json(self._fundamentals_cache_file, {})
        cache = cache if isinstance(cache, dict) else {}
        cached = cache.get(digits)
        if (isinstance(cached, dict) and not force_refresh
                and time.time() - float(cached.get("updated_at") or 0) < self._FUNDAMENTALS_TTL):
            return {k: v for k, v in cached.items() if k != "updated_at"}

        start_year = str(datetime.datetime.now().year - 2)
        payload = self._run_with_timeout(
            lambda: self._stock_financial_indicator_tool.fetch(digits, start_year=start_year),
            timeout_sec=12.0,
        )
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            return {}

        def pick(row: Dict[str, Any], *keys: str) -> Optional[float]:
            for key in keys:
                if key in row:
                    value = self._to_float(row.get(key))
                    if value is not None:
                        return value
            return None

        parsed: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            date_value = row.get("日期") or row.get("报告期")
            parsed.append({
                "report_date": str(date_value or "")[:10],
                "roe": pick(row, "净资产收益率(%)", "加权净资产收益率(%)"),
                "roe_weighted": pick(row, "加权净资产收益率(%)"),
                "rev_growth": pick(row, "主营业务收入增长率(%)"),
                "profit_growth": pick(row, "净利润增长率(%)"),
                "gross_margin": pick(row, "销售毛利率(%)", "主营业务利润率(%)"),
                "net_margin": pick(row, "销售净利率(%)"),
                "debt_ratio": pick(row, "资产负债率(%)"),
                "ocf_to_profit": pick(row, "经营现金净流量与净利润的比率(%)"),
                "bps": pick(row, "每股净资产_调整前(元)", "调整后的每股净资产(元)"),
                "eps": pick(row, "摊薄每股收益(元)", "加权每股收益(元)"),
                "current_ratio": pick(row, "流动比率"),
            })
        parsed = [p for p in parsed if p["report_date"]]
        if not parsed:
            return {}
        parsed.sort(key=lambda p: p["report_date"])
        latest = dict(parsed[-1])
        prev = dict(parsed[-2]) if len(parsed) >= 2 else {}

        result = {
            "ok": True,
            **latest,
            "prev_report_date": prev.get("report_date"),
            "profit_growth_prev": prev.get("profit_growth"),
            "rev_growth_prev": prev.get("rev_growth"),
            "gross_margin_prev": prev.get("gross_margin"),
            "roe_prev": prev.get("roe"),
            "history": parsed[-6:],
            "source": "akshare.stock_financial_analysis_indicator",
            "updated_at": time.time(),
        }
        cache[digits] = result
        self._save_json(self._fundamentals_cache_file, cache)
        return {k: v for k, v in result.items() if k != "updated_at"}

    # ------------------------------------------------------------------
    # 行业估值中枢（用户要求：PE/PB 必须按行业比，不能跨行业）
    # ------------------------------------------------------------------

    def _spot_valuation_rows(self) -> List[Dict[str, Any]]:
        """全市场 PE/PB 快照（只取代码/PE/PB 三列，供行业中枢聚合）。

        两个来源，自动降级：
          ① 东财 stock_zh_a_spot_em —— 一次请求拿全市场，最快；
          ② 腾讯 qt.gtimg.cn —— 60 只一批批量拉，不受东财限流影响，较慢但稳。
        东财被限流时（服务器上很常见）自动走腾讯，保证行业中枢能算出来。
        """
        df = self._get_cached_spot_data()
        if df is not None and not getattr(df, "empty", True):
            columns = list(df.columns)
            code_col = self._first_existing_column(columns, ["代码", "code", "证券代码"])
            pe_col = self._first_existing_column(columns, ["市盈率-动态", "市盈率", "pe"])
            pb_col = self._first_existing_column(columns, ["市净率", "pb"])
            if code_col and (pe_col or pb_col):
                rows: List[Dict[str, Any]] = []
                for record in df.to_dict("records"):
                    code = str(record.get(code_col) or "").strip()
                    if not code:
                        continue
                    rows.append({
                        "code": code,
                        "pe": self._to_float(record.get(pe_col)) if pe_col else None,
                        "pb": self._to_float(record.get(pb_col)) if pb_col else None,
                    })
                if rows:
                    return rows

        return self._spot_valuation_rows_tencent()

    def _spot_valuation_rows_tencent(self) -> List[Dict[str, Any]]:
        """腾讯行情批量取全市场 PE/PB（东财限流时的兜底）。"""
        components = self._sw_industry_components_map()
        codes: List[str] = []
        seen = set()
        for members in components.values():
            for member in members:
                if member not in seen:
                    seen.add(member)
                    codes.append(member)
        if not codes:
            return []

        rows: List[Dict[str, Any]] = []
        batch = 300
        for i in range(0, len(codes), batch):
            chunk = codes[i:i + batch]
            try:
                quotes = self._tencent_quote_tool.fetch_quotes(chunk, batch_size=300)
            except Exception as exc:  # noqa: BLE001
                logger.warning("tencent_valuation_batch_failed error=%r", exc)
                continue
            for code, quote in (quotes or {}).items():
                pe = self._to_float(quote.get("pe"))
                pb = self._to_float(quote.get("pb"))
                if pe is None and pb is None:
                    continue
                rows.append({"code": str(code), "pe": pe, "pb": pb})
        return rows

    def _sw_industry_valuation_rows(self) -> Dict[str, Dict[str, Any]]:
        """申万二级行业官方 PE/PB（行业指数口径），用作交叉校验与兜底。

        两个来源，都不走东财 push2（那套接口有严格限流）：
          ① index_analysis_daily_sw —— 带发布日期，权威但需逐日爬取，较慢；
          ② sw_index_second_info      —— 一次请求拿全部 131 个行业，最快，作为备份。
        """
        cache = self._load_json(self._sw_industry_cache_file, {})
        cached = cache.get("sw_second_valuation")
        if isinstance(cached, dict) and cached:
            rows: Dict[str, Dict[str, Any]] = {}
            for code, row in cached.items():
                if not isinstance(row, dict):
                    continue
                rows[str(code)] = {
                    "industry_name": row.get("industry_name"),
                    "pe": self._to_float(row.get("pe")),
                    "pb": self._to_float(row.get("pb")),
                    "dividend_yield": self._to_float(row.get("dividend_yield")),
                    "date": row.get("date"),
                }
            if rows:
                return rows
        try:
            df = self._run_with_timeout(
                lambda: self._market_tool.get_sw_index_analysis_daily(
                    "二级行业",
                    (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y%m%d"),
                    datetime.date.today().strftime("%Y%m%d"),
                ),
                timeout_sec=30.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sw_industry_valuation_rows_failed error=%r", exc)
            df = None
        if df is not None and not getattr(df, "empty", True):
            rows = {}
            for record in df.to_dict("records"):
                code = "".join(re.findall(r"\d", str(record.get("指数代码") or "")))[:6]
                if not code:
                    continue
                rows[code] = {
                    "industry_name": str(record.get("指数名称") or "").strip(),
                    "pe": self._to_float(record.get("市盈率")),
                    "pb": self._to_float(record.get("市净率")),
                    "dividend_yield": self._to_float(record.get("股息率")),
                    "date": str(record.get("发布日期") or ""),
                }
            if rows:
                cache = dict(cache)
                cache["sw_second_valuation"] = {
                    code: {**row, "updated_at": time.time()} for code, row in rows.items()
                }
                self._save_json(self._sw_industry_cache_file, cache)
                return rows

        # 备份来源：一次拿全部二级行业的 TTM PE / PB / 股息率
        try:
            df2 = self._run_with_timeout(
                lambda: ak.sw_index_second_info(), timeout_sec=30.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sw_index_second_info_failed error=%r", exc)
            return {}
        if df2 is None or getattr(df2, "empty", True):
            return {}
        rows = {}
        for record in df2.to_dict("records"):
            code = "".join(re.findall(r"\d", str(record.get("行业代码") or "")))[:6]
            if not code:
                continue
            rows[code] = {
                "industry_name": str(record.get("行业名称") or "").strip(),
                "pe": self._to_float(record.get("TTM(滚动)市盈率")),
                "pb": self._to_float(record.get("市净率")),
                "dividend_yield": self._to_float(record.get("静态股息率")),
                "date": "",
            }
        if rows:
            cache = dict(cache)
            cache["sw_second_valuation"] = {
                code: {**row, "updated_at": time.time()} for code, row in rows.items()
            }
            self._save_json(self._sw_industry_cache_file, cache)
        return rows

    def _sw_industry_components_map(self) -> Dict[str, List[str]]:
        """已缓存的申万二级行业成分股代码名单（只读缓存，不做网络请求）。

        刻意不在这里补齐：补齐要逐个行业调申万接口，28 个缺失行业
        实测要 4 分钟，绝不能出现在用户请求路径上。
        缺失行业由 `_ensure_industry_components(code)` 按需单补。
        """
        cache = self._load_json(self._sw_industry_cache_file, {})
        components = cache.get("sw_second_components")
        components = components if isinstance(components, dict) else {}

        out: Dict[str, List[str]] = {}
        for code, entry in components.items():
            rows = entry.get("rows") if isinstance(entry, dict) else None
            if isinstance(rows, list) and rows:
                codes = [
                    str(row.get("code") or "").strip()
                    for row in rows
                    if isinstance(row, dict) and str(row.get("code") or "").strip()
                ]
                if codes:
                    out[str(code)] = codes
        return out

    def _ensure_industry_components(self, industry_code: str) -> List[str]:
        """按需补齐**单个**行业的成分股名单（1 次请求），并回写缓存。"""
        code = str(industry_code or "").strip()
        if not code:
            return []
        cached = self._sw_industry_components_map().get(code)
        if cached:
            return cached

        try:
            comp_df = self._run_with_timeout(
                lambda: self._market_tool.get_sw_index_component(code),
                timeout_sec=20.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("sw_component_fetch_failed code=%s error=%r", code, exc)
            return []
        if comp_df is None or getattr(comp_df, "empty", True):
            return []

        rows = []
        for record in comp_df.to_dict("records"):
            member = str(record.get("证券代码") or "").strip()
            if not member:
                continue
            rows.append({
                "code": member,
                "name": str(record.get("证券名称") or "").strip(),
                "weight": self._to_float(record.get("最新权重")),
                "date": str(record.get("计入日期") or "").strip(),
            })
        if not rows:
            return []

        cache = self._load_json(self._sw_industry_cache_file, {})
        components = cache.get("sw_second_components")
        components = components if isinstance(components, dict) else {}
        new_components = dict(components)
        new_components[code] = {"updated_at": time.time(), "rows": rows}
        cache = dict(cache)
        cache["sw_second_components"] = new_components
        self._save_json(self._sw_industry_cache_file, cache)
        return [r["code"] for r in rows]

    def get_industry_valuation_stats(self, industry_code: str, force: bool = False) -> Dict[str, Any]:
        """行业估值中枢（成分股 PE/PB 中位数 + 官方行业口径交叉校验）。"""
        if not industry_code:
            return {"ok": False, "error": "缺少行业代码"}
        code = str(industry_code).strip()
        # 该行业成分股还没缓存 → 按需补齐（1 次请求，避免整轮补齐的 4 分钟耗时）
        if code not in self._sw_industry_components_map():
            self._ensure_industry_components(code)
        try:
            return self._industry_valuation_center.get_industry_stats(code, force=force)
        except Exception as exc:  # noqa: BLE001
            logger.warning("industry_valuation_stats_failed code=%s error=%r", industry_code, exc)
            return {"ok": False, "error": f"行业估值中枢不可用：{exc}"}

    def get_industry_cycle(self, industry_code: str, industry_name: str = "") -> Dict[str, Any]:
        """申万行业指数周期位置：250 日分位、均线排列、近 20/60/120 日动量。"""
        code = "".join(re.findall(r"\d", str(industry_code or "")))
        if not code:
            return {}
        cache = self._load_json(self._industry_cycle_cache_file, {})
        cache = cache if isinstance(cache, dict) else {}
        cached = cache.get(code)
        if (isinstance(cached, dict)
                and time.time() - float(cached.get("updated_at") or 0) < self._INDUSTRY_CYCLE_TTL):
            return {k: v for k, v in cached.items() if k != "updated_at"}

        df = self._run_with_timeout(lambda: self._market_tool.get_sw_index_hist(code), timeout_sec=15.0)
        if df is None or getattr(df, "empty", True):
            return {}
        try:
            closes = [self._to_float(v) for v in df["收盘"].tolist()]
        except Exception:  # noqa: BLE001
            return {}
        closes = [c for c in closes if c and c > 0]
        if len(closes) < 60:
            return {}
        window = closes[-250:] if len(closes) >= 250 else closes
        last = closes[-1]
        high, low = max(window), min(window)
        span = high - low
        result = {
            "ok": True,
            "industry_code": code,
            "industry_name": industry_name,
            "close": round(last, 2),
            "pos250": round((last - low) / span, 4) if span > 0 else 0.5,
            "high250": round(high, 2),
            "low250": round(low, 2),
            "ma60": round(sum(closes[-60:]) / 60, 2),
            "ma120": round(sum(closes[-120:]) / min(120, len(closes)), 2) if len(closes) >= 60 else None,
            "ma250": round(sum(closes[-250:]) / min(250, len(closes)), 2),
            "ret20": round((last / closes[-21] - 1) * 100, 2) if len(closes) > 21 else None,
            "ret60": round((last / closes[-61] - 1) * 100, 2) if len(closes) > 61 else None,
            "ret120": round((last / closes[-121] - 1) * 100, 2) if len(closes) > 121 else None,
            "bars": len(closes),
            "source": "akshare.index_hist_sw",
            "updated_at": time.time(),
        }
        cache[code] = result
        self._save_json(self._industry_cycle_cache_file, cache)
        return {k: v for k, v in result.items() if k != "updated_at"}

    def get_market_regime(self) -> Dict[str, Any]:
        """大盘周期位置（上证指数）：判断"现在是不是适合给高目标价"的环境。"""
        cache = self._load_json(self._market_regime_cache_file, {})
        cache = cache if isinstance(cache, dict) else {}
        cached = cache.get("sh000001")
        if (isinstance(cached, dict)
                and time.time() - float(cached.get("updated_at") or 0) < self._MARKET_REGIME_TTL):
            return {k: v for k, v in cached.items() if k != "updated_at"}

        df = self._run_with_timeout(lambda: self._market_tool.get_index_daily("sh000001"), timeout_sec=12.0)
        if df is None or getattr(df, "empty", True):
            return {}
        try:
            closes = [self._to_float(v) for v in df["close"].tolist()]
        except Exception:  # noqa: BLE001
            return {}
        closes = [c for c in closes if c and c > 0]
        if len(closes) < 60:
            return {}
        window = closes[-250:] if len(closes) >= 250 else closes
        last = closes[-1]
        high, low = max(window), min(window)
        span = high - low
        result = {
            "ok": True,
            "index": "sh000001",
            "close": round(last, 2),
            "pos250": round((last - low) / span, 4) if span > 0 else 0.5,
            "ma60": round(sum(closes[-60:]) / 60, 2),
            "ma250": round(sum(closes[-250:]) / min(250, len(closes)), 2),
            "ret20": round((last / closes[-21] - 1) * 100, 2) if len(closes) > 21 else None,
            "ret60": round((last / closes[-61] - 1) * 100, 2) if len(closes) > 61 else None,
            "source": "akshare.stock_zh_index_daily",
            "updated_at": time.time(),
        }
        cache["sh000001"] = result
        self._save_json(self._market_regime_cache_file, cache)
        return {k: v for k, v in result.items() if k != "updated_at"}

    def get_market_sentiment(self, date: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        """全市场情绪快照（涨停/炸板/跌停/连板高度/封板率）。按交易日缓存，失败返回 unknown。"""
        try:
            return self._sentiment_service.get_market_sentiment(date=date, force=force)
        except Exception as exc:  # noqa: BLE001
            logger.warning("market_sentiment_failed error=%r", exc)
            return {"ok": False, "phase": "unknown", "phase_label": "未知",
                    "temperature": 0.5, "detail": f"情绪数据不可用：{_friendly_error(exc)}"}

    def get_valuation_context(self, code: str) -> Dict[str, Any]:
        """卖出决策所需的外部上下文一次性取齐，供 sell_factors 计算。"""
        valuation: Dict[str, Any] = {}
        try:
            valuation = self._evaluate_valuation(code) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("valuation_context_failed code=%s error=%r", code, exc)
        industry_stats = valuation.get("industry_stats") if isinstance(valuation.get("industry_stats"), dict) else {}
        sw_industry = valuation.get("sw_industry") if isinstance(valuation.get("sw_industry"), dict) else {}
        industry_code = str(industry_stats.get("industry_code") or sw_industry.get("industry_code") or "").strip()
        industry_name = str(valuation.get("industry") or industry_stats.get("industry_name") or "").strip()
        industry_cycle = self.get_industry_cycle(industry_code, industry_name=industry_name) if industry_code else {}
        # 行业估值中枢：PE/PB 必须放回本行业坐标系比较（不能跨行业比）
        industry_valuation = self.get_industry_valuation_stats(industry_code) if industry_code else {}
        return {
            "valuation": valuation,
            "industry_stats": industry_stats,
            "industry_code": industry_code,
            "industry_name": industry_name,
            "industry_valuation": industry_valuation,
            "leader_weight": self._to_float(sw_industry.get("component_weight")),
            "fundamentals": self.get_fundamental_snapshot(code),
            "industry_cycle": industry_cycle,
            "market": self.get_market_regime(),
            "sentiment": self.get_market_sentiment(),
        }

    def get_valuation_rules(self) -> Dict[str, Any]:
        defaults = {
            "sw_second_industry": {
                "weights": {"pe": 0.45, "pb": 0.45, "dividend_yield": 0.1},
                "thresholds": {
                    "undervalued_score": 0.35,
                    "overvalued_score": -0.35,
                    "pe_discount_pct": -20.0,
                    "pe_premium_pct": 20.0,
                    "pb_discount_pct": -20.0,
                    "pb_premium_pct": 20.0,
                    "dividend_yield_premium_pct": 20.0,
                    "dividend_yield_discount_pct": -20.0,
                },
            }
        }
        data = self._load_json(self._valuation_rules_file, defaults)
        if not isinstance(data, dict):
            return defaults
        sw_rules = data.get("sw_second_industry") if isinstance(data.get("sw_second_industry"), dict) else {}
        weights = sw_rules.get("weights") if isinstance(sw_rules.get("weights"), dict) else {}
        thresholds = sw_rules.get("thresholds") if isinstance(sw_rules.get("thresholds"), dict) else {}
        merged = {
            "sw_second_industry": {
                "weights": {**defaults["sw_second_industry"]["weights"], **weights},
                "thresholds": {**defaults["sw_second_industry"]["thresholds"], **thresholds},
            }
        }
        return merged

    def _fetch_eastmoney_kline(self, code: str, start_date: str = None, end_date: str = None, frequency: str = 'd') -> List[Dict[str, Any]]:
        secid = self._build_secid(code)
        return self._market_tool.fetch_eastmoney_kline(secid, frequency, start_date or "0", end_date or "20500101")

    # ---------------------------------------------------------------- ETF / LOF
    # 场内基金为什么要单开一条源
    # --------------------------
    # 实测四个既有源对基金全部无效（2026-11-26）：
    #   星耀 AmazingData  → 对基金代码返回 {}
    #   雪球              → 整体 SSL 故障（股票同样失败，与基金无关）
    #   baostock          → 只覆盖 A 股股票，不含 ETF/LOF
    #   东财 push2his     → 对 0.159934 / 1.510300 直接 RemoteProtocolError 断连，
    #                       而同样接口取 1.600000 正常（6436 根）——说明是基金被拒，
    #                       不是网络问题（同时刻东财 push2 报价对基金是通的）
    # 所以基金如果混在股票降级链里，要白等四个源超时后才必然失败。
    # 腾讯 ifzq 的 fqkline 接口对场内有完整日/周/月线且含当日，新浪作为日线兜底。
    _FUND_PREFIXES = ("15", "16", "18", "50", "51", "52", "56", "58")
    _FUND_FREQ = {'d': 'day', 'w': 'week', 'm': 'month'}

    def _is_fund_code(self, code: str) -> bool:
        """场内基金 / ETF / LOF。可转债（11/12 开头）不算，它们不走这条源。

        能力判断的唯一真源在 ``market_gateway.detect_asset``，这里只做转接——
        两处各写一份前缀表，迟早会有一处忘记更新。
        """
        try:
            from .market_gateway import ASSET_FUND, detect_asset

            return detect_asset(code) == ASSET_FUND
        except Exception:  # noqa: BLE001
            return False

    def _fund_symbol(self, code: str) -> str:
        normalized = self._normalize_stock_code(code)
        market = 'sh' if normalized['market'].lower() == 'sh' else 'sz'
        return f"{market}{normalized['digits']}"

    def _fetch_tencent_kline(self, code: str, start_date: str = None,
                             end_date: str = None, frequency: str = 'd') -> List[Dict[str, Any]]:
        """腾讯 ifzq K 线：日/周/月，基金与股票通吃。含当日，通常是最后一道能出数的源。

        ⚠️ 两个必须记住的坑，都是实测出来的：

        1. **数组顺序是 [日期, 开, 收, 高, 低, 量]**——收在高前面。按常规 OHLC
           顺序硬套会把 K 线画反、把涨跌算反。
        2. **volume 单位是「手」，要 ×100 才是「股」**。同一天 159934：
           腾讯 1,178,501 手 vs 新浪 117,850,112 股，正好差 100 倍。
           不换算的话，「放量/缩量」「量比」这类绝对阈值的判断会差两个数量级。

        背景：2026-11-26 实测星耀对基金返回空、雪球整体 SSL 故障、东财 K 线域名
        对基金直接断连且对股票限流、akshare 连接中断——只剩这条链还能出数。
        """
        import httpx

        freq_key = self._FUND_FREQ.get(str(frequency or 'd').lower())
        if not freq_key:
            return []
        symbol = self._fund_symbol(code)
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}

        # 够历史的根数：MA200 需要 200 根日线，周线策略也要 MA60
        want = 1200 if freq_key == 'day' else 640
        try:
            with httpx.Client(trust_env=False, timeout=12, headers=headers) as client:
                resp = client.get(
                    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                    params={"param": f"{symbol},{freq_key},,,{want},qfq"},
                )
                payload = resp.json() if resp.status_code == 200 else {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("tencent_kline_failed symbol=%s error=%r", symbol, exc)
            payload = {}

        node = ((payload or {}).get("data") or {}).get(symbol) or {}
        rows = node.get(f"qfq{freq_key}") or node.get(freq_key) or []
        records: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            day = str(row[0])[:10]
            if start_date and day < str(start_date)[:10]:
                continue
            if end_date and day > str(end_date)[:10]:
                continue
            volume = self._to_float_safe(row[5])
            records.append({
                "date": day,
                "open": self._to_float_safe(row[1]),
                "close": self._to_float_safe(row[2]),
                "high": self._to_float_safe(row[3]),
                "low": self._to_float_safe(row[4]),
                # 手 → 股
                "volume": round(volume * 100, 2) if volume is not None else None,
                "amount": None,
            })

        if records:
            logger.info("tencent_kline_ok symbol=%s freq=%s rows=%s",
                        symbol, freq_key, len(records))
            return records

        # 新浪兜底：只有日线（scale=240），且不含当日
        if freq_key != 'day':
            return []
        try:
            import json as _json
            with httpx.Client(trust_env=False, timeout=12,
                              headers={**headers, "Referer": "https://finance.sina.com.cn/"}) as client:
                resp = client.get(
                    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
                    params={"symbol": symbol, "scale": "240", "ma": "no", "datalen": "1023"},
                )
                data = _json.loads(resp.text or "[]")
        except Exception as exc:  # noqa: BLE001
            logger.warning("sina_kline_failed symbol=%s error=%r", symbol, exc)
            return []
        for item in (data or []):
            if not isinstance(item, dict):
                continue
            day = str(item.get("day") or "")[:10]
            if not day:
                continue
            if start_date and day < str(start_date)[:10]:
                continue
            if end_date and day > str(end_date)[:10]:
                continue
            records.append({
                "date": day,
                "open": self._to_float_safe(item.get("open")),
                "close": self._to_float_safe(item.get("close")),
                "high": self._to_float_safe(item.get("high")),
                "low": self._to_float_safe(item.get("low")),
                # 新浪本来就是「股」，不需要换算
                "volume": self._to_float_safe(item.get("volume")),
                "amount": None,
            })
        if records:
            logger.info("sina_kline_ok symbol=%s rows=%s", symbol, len(records))
        return records

    @staticmethod
    def _to_float_safe(value: Any) -> Optional[float]:
        try:
            if value in (None, "", "-"):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _fetch_baostock_kline(self, code: str, start_date: str = None, end_date: str = None, frequency: str = 'd') -> List[Dict[str, Any]]:
        normalized = self._normalize_stock_code(code)
        bs_code = f"{normalized['market']}.{normalized['digits']}"
        return self._market_tool.fetch_baostock_kline(
            bs_code=bs_code,
            frequency=frequency,
            start_date=(start_date or '2020-01-01'),
            end_date=(end_date or datetime.datetime.now().strftime('%Y-%m-%d')),
        )

    def _finalize_kline_records(self, records: List[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
        if not records:
            return []

        df = pd.DataFrame(records)
        for col in ['open', 'close', 'high', 'low', 'volume', 'amount', 'pctChg', 'turnover', 'change', 'amplitude']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], errors='coerce')
            df = df.dropna(subset=['date']).sort_values('date')
            df['date'] = df['date'].dt.strftime('%Y-%m-%d %H:%M:%S').str.replace(' 00:00:00', '', regex=False)

        if 'close' not in df.columns or df.empty:
            return []

        df['MA5'] = df['close'].rolling(5).mean()
        df['MA10'] = df['close'].rolling(10).mean()
        df['MA20'] = df['close'].rolling(20).mean()
        df['MA30'] = df['close'].rolling(30).mean()
        df['MA60'] = df['close'].rolling(60).mean()

        ema12 = df['close'].ewm(span=12, adjust=False).mean()
        ema26 = df['close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = ema12 - ema26
        df['DEA'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['MACD'] = 2 * (df['DIF'] - df['DEA'])

        delta = df['close'].diff()
        up = delta.clip(lower=0)
        down = -1 * delta.clip(upper=0)
        ema_up = up.ewm(com=5, adjust=False).mean()
        ema_down = down.ewm(com=5, adjust=False).mean()
        rs = ema_up / ema_down
        df['RSI6'] = (100 - (100 / (1 + rs))).fillna(50)

        df = df.replace([float('inf'), float('-inf')], None)
        df = df.astype(object).where(pd.notnull(df), None)
        result = df.to_dict('records')
        for item in result:
            item['source'] = source
        return result

    def get_user_settings(self) -> Dict[str, Any]:
        uid = self._uid_safe()
        if uid is not None:
            data = db_store.get_settings(uid)
        else:
            # 数据库/用户尚未初始化（模块导入阶段）：退回旧 JSON，保证构造期不炸。
            data = self._load_json(self._settings_file, {})
        base = dict(data) if isinstance(data, dict) else {}
        base.setdefault('total_capital', 0.0)
        auth = self._load_xueqiu_auth()
        base['xueqiu_cookie'] = str(auth.get('xueqiu_cookie') or '').strip()
        base['xueqiu_user_agent'] = str(auth.get('xueqiu_user_agent') or '').strip()
        from ..core.secrets import overlay_secret_settings
        return overlay_secret_settings(base)

    def set_prefer_source(self, source: str) -> str:
        """切换实时行情主源：写配置 + 立即更新内存开关（否则要重启才生效）。"""
        src = str(source or "").strip().lower()
        if src not in ("amazingdata", "xueqiu"):
            raise ValueError("prefer_source must be 'amazingdata' or 'xueqiu'")
        self._prefer_source = src
        self.save_user_settings({"prefer_source": src})
        logger.info("prefer_source_changed source=%s", src)
        return src

    def save_user_settings(self, settings: Dict[str, Any]) -> None:
        uid = self._uid()
        base = self.get_user_settings()
        payload = dict(settings or {})
        # 只要写了可用资金就必须留时间戳：否则「0 = 满仓」和「没填」在下游分不开。
        if 'available_cash' in payload and not str(payload.get('cash_updated_at') or '').strip():
            payload['cash_updated_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        auth_update: Dict[str, Any] = {}
        if 'xueqiu_cookie' in payload:
            auth_update['xueqiu_cookie'] = str(payload.pop('xueqiu_cookie') or '').strip()
        if 'xueqiu_user_agent' in payload:
            auth_update['xueqiu_user_agent'] = str(payload.pop('xueqiu_user_agent') or '').strip()
        base.update(payload)
        base.pop('xueqiu_cookie', None)
        base.pop('xueqiu_user_agent', None)
        db_store.save_settings(uid, base, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        # 资金/本金变了，账户快照里的现金与仓位立刻失效
        self._snap_cache_clear(uid)
        if auth_update:
            current_auth = self._load_xueqiu_auth()
            current_auth.update(auth_update)
            self._save_xueqiu_auth(current_auth)

    def _load_xueqiu_auth(self) -> Dict[str, Any]:
        defaults = {
            'xueqiu_cookie': "",
            'xueqiu_user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
            'xueqiu_refresh_url': 'https://xueqiu.com/S/SZ002230',
            'xueqiu_refresh_headless': True,
            'xueqiu_browser_executable_path': '',
        }
        data = self._load_json(self._xueqiu_auth_file, defaults)
        if isinstance(data, dict):
            from ..core.secrets import env_value
            return {
                'xueqiu_cookie': (env_value('xueqiu_cookie') or str(data.get('xueqiu_cookie') or '').strip()),
                'xueqiu_user_agent': str(data.get('xueqiu_user_agent') or '').strip(),
                'xueqiu_refresh_url': str(data.get('xueqiu_refresh_url') or defaults['xueqiu_refresh_url']).strip(),
                'xueqiu_refresh_headless': bool(data.get('xueqiu_refresh_headless', defaults['xueqiu_refresh_headless'])),
                'xueqiu_browser_executable_path': str(data.get('xueqiu_browser_executable_path') or '').strip(),
            }
        return defaults

    def _save_xueqiu_auth(self, payload: Dict[str, Any]) -> None:
        data = {
            'xueqiu_cookie': str((payload or {}).get('xueqiu_cookie') or '').strip(),
            'xueqiu_user_agent': str((payload or {}).get('xueqiu_user_agent') or '').strip(),
            'xueqiu_refresh_url': str((payload or {}).get('xueqiu_refresh_url') or 'https://xueqiu.com/S/SZ002230').strip(),
            'xueqiu_refresh_headless': bool((payload or {}).get('xueqiu_refresh_headless', True)),
            'xueqiu_browser_executable_path': str((payload or {}).get('xueqiu_browser_executable_path') or '').strip(),
        }
        from ..core.secrets import env_value
        if env_value('xueqiu_cookie'):
            disk = self._load_json(self._xueqiu_auth_file, {})
            data['xueqiu_cookie'] = str((disk or {}).get('xueqiu_cookie') or '').strip()
        self._save_json(self._xueqiu_auth_file, data)

    def get_monitor_settings(self) -> Dict[str, Any]:
        settings = self.get_user_settings()
        monitor_kline_frequency = str(settings.get('monitor_kline_frequency') or 'd').strip().lower()
        if monitor_kline_frequency not in {'d', 'w', 'm', '1', '5', '15', '30', '60'}:
            monitor_kline_frequency = 'd'
        return {
            'enabled': bool(settings.get('monitor_enabled', True)),
            'interval_sec': int(settings.get('monitor_interval_sec', 120) or 120),
            'monitor_kline_frequency': monitor_kline_frequency,
            'dingtalk_enabled': bool(settings.get('dingtalk_enabled', False)),
            'dingtalk_webhook_url': str(settings.get('dingtalk_webhook_url') or '').strip(),
            'dingtalk_secret': str(settings.get('dingtalk_secret') or '').strip(),
            'xueqiu_cookie': str(settings.get('xueqiu_cookie') or '').strip(),
            'xueqiu_user_agent': str(settings.get('xueqiu_user_agent') or '').strip(),
        }

    def search_stocks(self, query: str) -> List[Dict[str, Any]]:
        if not query:
            return []
        if not self._stock_list_cache or time.time() - self._stock_list_last_update > 86400:
            self._refresh_stock_list_cache()
        results = []
        for item in self._stock_list_cache:
            code = str(item.get('code') or '')
            name = str(item.get('name') or '')
            if query in code or query in name:
                exchange = 'XSHG' if code.startswith(('6', '9')) else ('XSHE' if code.startswith(('0', '3')) else 'XBEI')
                suffix = '.SH' if exchange == 'XSHG' else ('.SZ' if exchange == 'XSHE' else '.BJ')
                results.append({'secID': f'{code}{suffix}', 'ticker': code, 'secShortName': name, 'exchangeCD': exchange, 'listStatusCD': 'L', 'equTypeCD': 'A', 'listDate': '-'})
                if len(results) >= 20:
                    break
        return results
    def get_stock_spot_info(self, code: str, allow_full_market_fallback: bool = True, force_refresh: bool = False) -> Dict[str, Any]:
        try:
            normalized = self._normalize_stock_code(code)
            digits = normalized['digits']
        except Exception:
            return {}
        spot_cache_key = self._source_cache_key('spot_quote', normalized['market'], digits)
        if not force_refresh:
            cached_spot = self._get_source_cache(spot_cache_key, 20)
            if isinstance(cached_spot, dict):
                return cached_spot
        source_timeout = self._spot_source_timeout
        inflight_lock = self._get_source_inflight_lock(spot_cache_key)
        with inflight_lock:
            if not force_refresh:
                cached_spot = self._get_source_cache(spot_cache_key, 20)
                if isinstance(cached_spot, dict):
                    return cached_spot

            # Source 0 (可选主源): 星耀数智 AmazingData SDK（配置 prefer_source=amazingdata 时启用）
            # 连续失败进入冷却后跳过：不让每个请求都先白等一次超时
            if self._prefer_source == 'amazingdata' and not self._source_in_cooldown('amazingdata'):
                ad_quote = self._run_with_timeout(lambda: self._fetch_amazingdata_quote(code), self._amazingdata_timeout)
                if ad_quote and self._to_float(ad_quote.get('price')):
                    ad_quote = self._normalize_spot_quote(ad_quote)
                    self._set_source_cache(spot_cache_key, ad_quote)
                    return ad_quote

            # Source 1: Tencent（无鉴权、几 KB、云上最稳，作为主力兜底）
            tencent_quote = self._run_with_timeout(lambda: self._fetch_tencent_quote(code), source_timeout + 1.0)
            if tencent_quote and tencent_quote.get('price') is not None:
                tencent_quote = self._normalize_spot_quote(tencent_quote)
                self._set_source_cache(spot_cache_key, tencent_quote)
                return tencent_quote

            # Source 2: Xueqiu API（cookie 过期连续失败后冷却 30 分钟，期间跳过）
            if not self._source_in_cooldown('xueqiu'):
                xueqiu_quote = self._run_with_timeout(lambda: self._fetch_xueqiu_quote(code), source_timeout)
                if xueqiu_quote and xueqiu_quote.get('price') is not None:
                    xueqiu_quote = self._normalize_spot_quote(xueqiu_quote)
                    self._set_source_cache(spot_cache_key, xueqiu_quote)
                    return xueqiu_quote

            # Source 3: EastMoney single-stock quote（被限流期间直接跳过，别把 5 分钟封禁拖成 40 分钟）
            if not self._eastmoney_in_cooldown():
                eastmoney_quote = self._run_with_timeout(lambda: self._fetch_eastmoney_quote(code, timeout_sec=source_timeout), source_timeout)
                if eastmoney_quote and eastmoney_quote.get('price') is not None:
                    eastmoney_quote = self._normalize_spot_quote(eastmoney_quote)
                    self._set_source_cache(spot_cache_key, eastmoney_quote)
                    return eastmoney_quote

            # Source 4: 星耀数智——稳定性兜底（腾讯/雪球/东财全失守时才轮到它）
            # 它数据最全（完整 OHLC+量额），但暖机 9~17s、取数每次数秒，且同一账号
            # 多地登录会互踢，所以不做主源，只当最后一道能出数的防线。
            # 条件严格：池已暖机 + 未熔断；否则连碰都不碰，一次多余 RPC 都不发。
            if self._prefer_source != 'amazingdata' and self._amazingdata_ready():
                ad_quote = self._run_with_timeout(lambda: self._fetch_amazingdata_quote(code), source_timeout + 4.0)
                if ad_quote and ad_quote.get('price') is not None:
                    ad_quote = self._normalize_spot_quote(ad_quote)
                    self._set_source_cache(spot_cache_key, ad_quote)
                    return ad_quote

            # Source 5: cached full-market snapshot (non-blocking)
            cached_quote = self._fetch_spot_from_cache_fast(code)
            if cached_quote and cached_quote.get('price') is not None:
                cached_quote = self._normalize_spot_quote(cached_quote)
                self._set_source_cache(spot_cache_key, cached_quote)
                return cached_quote

            # Source 5: optional snapshot refresh guarded by timeout.
            if allow_full_market_fallback and force_refresh:
                _ = self._run_with_timeout(lambda: self._get_cached_spot_data(force_refresh=True), source_timeout)
                cached_quote = self._fetch_spot_from_cache_fast(code)
                if cached_quote:
                    cached_quote = self._normalize_spot_quote(cached_quote)
                    self._set_source_cache(spot_cache_key, cached_quote)
                    return cached_quote
            empty_quote = {
                'code': digits,
                'name': self._get_cached_stock_name(digits) or digits,
                'price': None,
                'pct_chg': None,
                'change': None,
                'amount': None,
                'volume': None,
                'high': None,
                'low': None,
                'open': None,
                'prev_close': None,
                'source': 'none',
            }
            self._set_source_cache(spot_cache_key, empty_quote)
            return empty_quote

    def get_company_info(self, code: str) -> Dict[str, Any]:
        symbol = self._normalize_stock_code(code)['digits']
        return self._company_info_tool.get(symbol)

    def ensure_company_info_cache(self, code: str) -> Dict[str, Any]:
        symbol = self._normalize_stock_code(code)['digits']
        return self._company_info_tool.ensure_cached(symbol)

    def get_k_line_data(self, code: str, start_date: str = None, end_date: str = None, frequency: str = 'd', caller: str = 'unknown') -> List[Dict[str, Any]]:
        symbol = self._normalize_stock_code(code)['digits']
        explicit_start = bool(start_date)
        if not end_date:
            end_date = datetime.datetime.now().strftime('%Y-%m-%d')
        if not start_date:
            if frequency == 'd':
                start_date = '2020-01-01'
            elif frequency in ('w', 'week'):
                # 周线要算 MA20/信号回看：5 天窗口只有 1 根（本周还没走完）。
                # 雪球周线固定拉 284 根不受窗口影响，星耀按日期窗口查询，
                # 曾因此只返回 1 根周线 → 策略信号「K线数据不足 1/20」。
                start_date = (datetime.datetime.now() - datetime.timedelta(days=500)).strftime('%Y-%m-%d')
            elif frequency in ('m', 'month'):
                start_date = (datetime.datetime.now() - datetime.timedelta(days=2500)).strftime('%Y-%m-%d')
            else:
                start_date = (datetime.datetime.now() - datetime.timedelta(days=5)).strftime('%Y-%m-%d')
        context = {
            'caller': caller,
            'code': code,
            'symbol': symbol,
            'frequency': frequency,
            'start_date': start_date,
            'end_date': end_date,
        }
        cache_key = self._kline_cache_key(code, start_date, end_date, frequency)
        cached = self._get_cached_kline(cache_key, frequency)
        if cached is not None and not caller.endswith('_refresh'):
            logger.info("kline_fetch_ok source=cache rows=%s context=%s", len(cached), context)
            return cached

        # ETF / LOF 走专用源，直接返回，不再落到下面的股票降级链。
        # 理由见 _fetch_fund_kline 上方注释：四个股票源对基金全部无效，
        # 混进链路只会白等超时；而基金在股票链末尾也会被 akshare 覆掉。
        if self._is_fund_code(code):
            try:
                records = self._fetch_tencent_kline(code, start_date, end_date, frequency)
                finalized = self._finalize_kline_records(records, 'fund')
                if finalized:
                    self._set_cached_kline(cache_key, finalized)
                    logger.info("kline_fetch_ok source=fund rows=%s context=%s",
                                len(finalized), context)
                    return finalized
            except Exception as exc:  # noqa: BLE001
                logger.warning("kline_fetch_failed source=fund error=%r context=%s", exc, context)
            logger.warning("kline_fetch_empty source=fund context=%s", context)
            self._cache_empty_kline_if_minute(cache_key, frequency)
            return []

        # Source -1 (主源原则): 星耀数智 K线/分时——星耀支持全周期(日/周/月/分钟级)，
        # prefer_source=amazingdata 时所有频率优先走星耀，失败自动落入下方降级链。
        # 分钟频率有额外的新鲜度校验：星耀 1 分钟线返回的是区间内最后一个有数据的
        # 交易日（早盘刚开/假期后它会端出上周四的全天 240 根），旧数据冒充当日分时
        # 会让前端把上周四的走势画在今天的价格旁边——不新鲜就不采用，落入腾讯/雪球链。
        ad_records = None
        if self._prefer_source == 'amazingdata' and self._amazingdata_credentials:
            ad_records = self._fetch_amazingdata_kline(code, start_date, end_date, frequency)
            if ad_records:
                finalized = self._finalize_kline_records(ad_records, 'amazingdata_kline')
                if finalized:
                    # 周/月线数量下限：调用方没显式限定窗口时，少于 20 根算不上有效
                    # K 线序列（MA20/信号都算不出来），不采用、落入雪球/东财兜底链
                    if (frequency in ('w', 'm') and not explicit_start
                            and len(finalized) < 20):
                        logger.warning(
                            "kline_insufficient_bars source=amazingdata freq=%s rows=%s context=%s",
                            frequency, len(finalized), context,
                        )
                    elif (frequency == 'd' and explicit_start
                          and not self._daily_window_sufficient(finalized, start_date, end_date)):
                        # 显式长窗口的日线同样会残缺（星耀曾对 90 天窗口只回 1 根，
                        # 绩效复盘的净值曲线因此只剩 1 个交易日），不采用、落兜底
                        logger.warning(
                            "kline_insufficient_bars source=amazingdata freq=d rows=%s context=%s",
                            len(finalized), context,
                        )
                    elif frequency in self._MINUTE_FREQS and not self._minute_records_fresh(finalized):
                        logger.warning(
                            "kline_stale_minute source=amazingdata last=%s context=%s",
                            finalized[-1].get('date'), context,
                        )
                    else:
                        self._set_cached_kline(cache_key, finalized)
                        logger.info(
                            "kline_fetch_ok source=amazingdata rows=%s context=%s", len(finalized), context
                        )
                        return finalized

        try:
            if frequency in ['1', 'minute', '1d']:
                # 当日分时：腾讯优先（云上唯一稳定源），雪球兜底
                finalized = self._finalize_kline_records(
                    self._fetch_tencent_minute(code), 'tencent_minute'
                )
                if not finalized:
                    finalized = self._finalize_kline_records(
                        self._fetch_xueqiu_minute(code, period='1d'), 'xueqiu_minute'
                    )
            elif frequency == '5d':
                # 5 日分时：腾讯多日接口优先，雪球兜底
                finalized = self._finalize_kline_records(
                    self._fetch_tencent_minute(code, days=5), 'tencent_minute'
                )
                if not finalized:
                    finalized = self._finalize_kline_records(
                        self._fetch_xueqiu_minute(code, period='5d'), 'xueqiu_minute'
                    )
            else:
                records = self._fetch_xueqiu_kline(code, start_date, end_date, frequency)
                finalized = self._finalize_kline_records(records, 'xueqiu')
            if finalized:
                self._set_cached_kline(cache_key, finalized)
                logger.info("kline_fetch_ok source=%s rows=%s context=%s", finalized[0].get('source') or 'xueqiu', len(finalized), context)
                return finalized
        except Exception as exc:
            logger.warning("kline_fetch_failed source=xueqiu error=%r context=%s", exc, context)

        # 腾讯 / 新浪（股票与基金通用）——放在 baostock 与东财**之前**。
        # 顺序是拿时间换来的：2026-11-26 实测雪球 SSL 故障、baostock 登录失败会
        # 各自阻塞数十秒，东财限流再加一次，akshare 也断——一轮下来单只票 4 分钟
        # 还出不来一根 K 线，监控实质瘫痪。腾讯这个源 0.2~0.3 秒就返回完整日/周/月线
        # 且含当日，所以雪球失败后立刻用它，不再陪 baostock/东财一起等超时。
        # 复权口径：腾讯 qfq = 前复权，与雪球/baostock 一致（成交量已由手换算成股）。
        if frequency in ['d', 'w', 'm']:
            try:
                records = self._fetch_tencent_kline(code, start_date, end_date, frequency)
                # 过统一闸门再采用：残缺返回（只有几根）比没有更危险——
                # 下游会算出 MA20=None、信号凭空消失，而且不会报错。
                from .market_gateway import validate

                usable, reason = validate(records, frequency, explicit_start)
                if not usable:
                    logger.warning("kline_source_rejected source=tencent reason=%s context=%s",
                                   reason, context)
                finalized = self._finalize_kline_records(records, 'tencent') if usable else []
                if finalized:
                    self._set_cached_kline(cache_key, finalized)
                    logger.info("kline_fetch_ok source=tencent rows=%s context=%s",
                                len(finalized), context)
                    return finalized
            except Exception as exc:  # noqa: BLE001
                logger.warning("kline_fetch_failed source=tencent error=%r context=%s", exc, context)

        if frequency in ['d', 'w', 'm']:
            try:
                records = self._fetch_baostock_kline(code, start_date, end_date, frequency)
                finalized = self._finalize_kline_records(records, 'baostock')
                if finalized:
                    self._set_cached_kline(cache_key, finalized)
                    logger.info("kline_fetch_ok source=baostock rows=%s context=%s", len(finalized), context)
                    return finalized
            except Exception as exc:
                logger.warning("kline_fetch_failed source=baostock error=%r context=%s", exc, context)

        try:
            records = self._fetch_eastmoney_kline(code, start_date, end_date, frequency)
            finalized = self._finalize_kline_records(records, 'eastmoney')
            if finalized:
                self._set_cached_kline(cache_key, finalized)
                logger.info("kline_fetch_ok source=eastmoney rows=%s context=%s", len(finalized), context)
                return finalized
        except Exception as exc:
            logger.warning("kline_fetch_failed source=eastmoney error=%r context=%s", exc, context)

        # 分钟频率走到这里，说明所有当日源（星耀未过新鲜度校验/腾讯/雪球/东财）都没拿到
        # 今天的数据——可能是节假日或各源还没出今天的分钟线。回退采用星耀旧数据：
        # 有总比没有强，前端会通过数据截止时间提示这不是今天的走势。
        if ad_records and frequency in self._MINUTE_FREQS:
            finalized = self._finalize_kline_records(ad_records, 'amazingdata_kline')
            if finalized:
                self._set_cached_kline(cache_key, finalized)
                logger.info(
                    "kline_fetch_ok source=amazingdata_stale_fallback rows=%s context=%s",
                    len(finalized), context,
                )
                return finalized

        try:
            df = self._market_tool.get_akshare_kline(symbol, frequency, start_date, end_date)
            if df is None or df.empty:
                logger.warning("kline_fetch_empty source=akshare context=%s", context)
                self._cache_empty_kline_if_minute(cache_key, frequency)
                return []
        except Exception as exc:
            logger.warning("kline_fetch_failed source=akshare error=%r context=%s", exc, context)
            self._cache_empty_kline_if_minute(cache_key, frequency)
            return []

        columns = [str(col) for col in df.columns]
        rename_targets = {
            'date': self._first_existing_column(columns, ['date', 'time', 'datetime'], 0),
            'open': self._first_existing_column(columns, ['open'], 1),
            'close': self._first_existing_column(columns, ['close', 'price'], 2),
            'high': self._first_existing_column(columns, ['high'], 3),
            'low': self._first_existing_column(columns, ['low'], 4),
            'volume': self._first_existing_column(columns, ['volume'], 5),
            'amount': self._first_existing_column(columns, ['amount', 'turnover'], 6),
            'pctChg': self._first_existing_column(columns, ['pctChg', 'pct_chg', 'change_percent'], 7),
            'turnover': self._first_existing_column(columns, ['turnover', 'turnover_rate'], 8),
        }
        df = df.rename(columns={source: target for target, source in rename_targets.items() if source})

        for col in ['open', 'close', 'high', 'low', 'volume', 'amount', 'pctChg', 'turnover']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date')
            df['date'] = df['date'].dt.strftime('%Y-%m-%d %H:%M:%S').str.replace(' 00:00:00', '')

        if 'close' not in df.columns:
            self._cache_empty_kline_if_minute(cache_key, frequency)
            return []

        finalized = self._finalize_kline_records(df.to_dict('records'), 'akshare')
        if finalized:
            self._set_cached_kline(cache_key, finalized)
            logger.info("kline_fetch_ok source=akshare rows=%s context=%s", len(finalized), context)
        else:
            logger.warning("kline_fetch_empty source=akshare_normalized context=%s", context)
            self._cache_empty_kline_if_minute(cache_key, frequency)
        return finalized

    def get_realtime_minute_data(self, code: str, period: str = '1d') -> List[Dict[str, Any]]:
        frequency = '5d' if str(period or '').lower() == '5d' else '1'
        data = self.get_k_line_data(code, frequency=frequency, caller='api_realtime_minute')
        # 分时只用于画走势线（前端取 close，Agent 的 tool_minute 也只取 close）。
        # get_k_line_data 会顺带给每个点算 MA5/MA10/MA20/MA30/MA60/DIF/DEA/MACD/RSI6，
        # 267 个点整包 95KB；裁成 3 个字段后约 5KB，首屏 4 只票省下 360KB 流量。
        return [
            {
                'date': item.get('date'),
                'close': item.get('close'),
                'volume': item.get('volume'),
            }
            for item in (data or [])
            if isinstance(item, dict)
        ]

    def get_k_line_payload(self, code: str, start_date: str = None, end_date: str = None, frequency: str = 'd') -> Dict[str, Any]:
        data = self.get_k_line_data(code, start_date, end_date, frequency, caller='api_kline')
        return {
            'code': code,
            'frequency': frequency,
            'start_date': start_date,
            'end_date': end_date,
            'count': len(data),
            'has_data': bool(data),
            'source': (data[0].get('source') if data else ''),
            'data': data,
        }

    def _save_favorites(self) -> None:
        uid = self._uid()
        items = self._sanitize_favorites(self._favorite_cache)
        self._fav_caches[uid] = items
        db_store.replace_favorites(uid, items)
        # 持仓变了，账户市值与现金占比都要重算
        self._snap_cache_clear(uid)

    def prewarm_quote_sources(self) -> None:
        """容器启动时后台预热行情源。

        星耀慢的只是第一次登录（实测 9.2s），热状态批量报价只要 0.1s。
        现在进程池是首次有人请求行情才建，早高峰第一下永远最慢——
        改成启动即后台建池登录，用户打开页面时星耀已经是热的。
        非阻塞、失败只记日志，绝不影响服务启动。
        """
        try:
            if self._prefer_source != 'amazingdata' or not self._amazingdata_credentials:
                return
            tool = self._get_amazingdata_tool()
            if tool is not None:
                tool.prewarm()
        except Exception as exc:
            logger.warning("quote_prewarm_failed error=%r", exc)

    def get_favorites_spot(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """自选股批量现价：星耀 → 腾讯（批量）→ 逐只兜底链。

        关键点是**按票补齐**而不是按列表取舍：任何一个源只拿到部分票，
        剩下的都要继续走下一个源，不能让几只票拖成永久没有现价。
        """
        codes = [item.get('secID') for item in self._favorite_cache if item.get('secID')]
        if not codes:
            return []

        resolved: Dict[str, Dict[str, Any]] = {}

        def _digits_of(code: str) -> Optional[str]:
            try:
                return self._normalize_stock_code(code)['digits']
            except Exception:
                return None

        if self._prefer_source == 'amazingdata' and self._amazingdata_credentials \
                and not self._source_in_cooldown('amazingdata'):
            for quote in self._fetch_amazingdata_favorites() or []:
                digits = quote.get('code')
                if digits:
                    resolved[digits] = quote

        missing = [c for c in codes if _digits_of(c) not in resolved]
        if missing:
            for quote in self._fetch_tencent_quotes(missing):
                digits = quote.get('code')
                if digits:
                    resolved[digits] = quote

        # 星耀兜底补缺：腾讯批量没拿到的票，若常驻池已暖机则补一次。
        # 池未就绪直接跳过——它一次都没暖过就调用，只会返回空还多花一次 RPC。
        missing = [c for c in codes if _digits_of(c) not in resolved]
        if missing and self._prefer_source != 'amazingdata' and self._amazingdata_ready():
            for quote in self._fetch_amazingdata_favorites(missing) or []:
                digits = quote.get('code')
                if digits and digits not in resolved:
                    resolved[digits] = quote
            logger.info(
                "amazingdata_fallback_batch requested=%s resolved=%s",
                len(missing), sum(1 for c in missing if _digits_of(c) in resolved),
            )

        # 全市场快照（东财 push2）只在批量源确实没覆盖到某些票时才拉：
        # 它要遍历几十页、耗时数秒，而且是 IP 限流的主要触发点。
        if any(_digits_of(c) not in resolved for c in codes):
            self._get_cached_spot_data(force_refresh=force_refresh)

        results: List[Dict[str, Any]] = []
        for code in codes:
            digits = _digits_of(code)
            quote = resolved.get(digits) if digits else None
            if not quote:
                # 批量源都没有 → 走单只兜底链（内部还有雪球/东财/全市场快照）
                quote = self.get_stock_spot_info(code)
                if quote and quote.get('code'):
                    quote = self._normalize_spot_quote(quote)
            if quote:
                results.append(quote)
        return results

    def get_favorites_list(self, force_refresh: bool = False, include_quotes: bool = True) -> List[Dict[str, Any]]:
        # 不再预拉全市场快照：下游 get_favorites_spot 在批量源取不到票时才会去拉，
        # 避免「首页刷新就扫一遍东财 59 页」这种既慢又容易被限流的开销。

        def is_code_like(value: Any) -> bool:
            text = str(value or "").strip().upper()
            if not text:
                return True
            if re.match(r"^(SH|SZ|BJ)\d{6}$", text):
                return True
            if re.match(r"^\d{6}\.(SH|SZ|BJ)$", text):
                return True
            if re.match(r"^\d{6}$", text):
                return True
            return False

        def build_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            sec_id = item.get('secID')
            if not sec_id:
                return None
            digits = self._normalize_stock_code(sec_id)['digits']
            spot = self.get_stock_spot_info(sec_id) if include_quotes else {}
            price = self._to_float((spot or {}).get('price')) or 0.0
            prev_close = self._to_float((spot or {}).get('prev_close'))
            change_value = self._to_float((spot or {}).get('change'))
            pct_value = self._to_float((spot or {}).get('pct_chg') or (spot or {}).get('pctChg'))
            if pct_value is None and change_value is not None and prev_close not in (None, 0):
                pct_value = change_value / prev_close * 100
            if pct_value is None and price and prev_close not in (None, 0):
                pct_value = (price - prev_close) / prev_close * 100
            shares = int(item.get('shares') or 0)
            cost = float(item.get('cost') or 0.0)
            profit = shares * (price - cost) if shares and price else 0.0
            profit_pct = ((price - cost) / cost * 100) if shares and price and cost else 0.0
            cached_name = self._get_cached_stock_name(digits)
            raw_name = (spot or {}).get('name') or item.get('name') or cached_name or sec_id
            display_name = cached_name if is_code_like(raw_name) and cached_name else raw_name
            merged = dict(item)
            merged.update({
                'secID': sec_id,
                'name': display_name,
                'secShortName': display_name,
                'ticker': (spot or {}).get('code') or digits,
                'price': price or None,
                'pctChg': pct_value,
                'change': change_value,
                'shares': shares,
                'cost': cost,
                'horizon': self._normalize_horizon(item.get('horizon') or 'long'),
                'pool': normalize_pool(item.get('pool')),
                'buy_date': item.get('buy_date'),
                'profit': profit,
                'profit_pct': profit_pct,
                'no_data': not bool(spot),
                'spot': spot or {},
            })
            return merged

        items = list(self._favorite_cache)
        if not items:
            return []
        if len(items) == 1:
            result = build_item(items[0])
            return [result] if result else []

        results: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(8, len(items))) as executor:
            futures = [executor.submit(contextvars.copy_context().run, build_item, item) for item in items]
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:
                    logger.warning("favorites_build_item_failed error=%r", exc)
                    continue
                if result:
                    results.append(result)
        return sorted(results, key=lambda item: item.get('secID', ''))

    def add_favorite(self, secID: str, shares: int = 0, cost: float = 0.0, horizon: str = None, buy_date: str = None, name: str = None,
                     group: str = '', note: str = '', pool: str = '') -> None:
        normalized = self._normalize_stock_code(secID)
        normalized_secid = f"{normalized['digits']}.{normalized['market'].upper()}"
        if any(str(item.get('secID') or '').strip().upper() == normalized_secid for item in self._favorite_cache):
            self.ensure_company_info_cache(normalized_secid)
            return
        normalized_pool = normalize_pool(pool)
        self._favorite_cache.append({
            'secID': normalized_secid,
            'shares': int(shares or 0),
            'cost': float(cost or 0.0),
            # 池是唯一用户口径；horizon 只为底层卖出/监控参数服务，按池自动推导
            'horizon': self._normalize_horizon(horizon_for_pool(normalized_pool, horizon or 'long')),
            'pool': normalized_pool,
            'group': str(group or '').strip()[:40],
            'note': str(note or '').strip()[:200],
        })
        self._save_favorites()
        self.ensure_company_info_cache(normalized_secid)

    def effective_horizon(self, item: Dict[str, Any]) -> str:
        """取一只持仓实际该用的卖出/监控风格：有池按池映射，未分类才看旧 horizon。"""
        if not isinstance(item, dict):
            return 'long'
        return self._normalize_horizon(horizon_for_pool(item.get('pool'), item.get('horizon') or 'long'))

    def update_favorite_position(self, secID: str, shares: Optional[int] = None, cost: Optional[float] = None, horizon: str = None, buy_date: str = None,
                                group: Optional[str] = None, note: Optional[str] = None,
                                pool: Optional[str] = None) -> None:
        for item in self._favorite_cache:
            if item.get('secID') == secID:
                if shares is not None:
                    item['shares'] = shares
                if cost is not None:
                    item['cost'] = cost
                if pool is not None:
                    item['pool'] = normalize_pool(pool)
                # 池优先：池变化后 horizon 自动跟随；未分类时才允许显式 horizon
                if item.get('pool'):
                    item['horizon'] = self._normalize_horizon(
                        horizon_for_pool(item.get('pool'), horizon or item.get('horizon') or 'long')
                    )
                elif horizon is not None:
                    item['horizon'] = self._normalize_horizon(horizon)
                else:
                    item['horizon'] = self._normalize_horizon(item.get('horizon') or 'long')
                if group is not None:
                    item['group'] = str(group or '').strip()[:40]
                if note is not None:
                    item['note'] = str(note or '').strip()[:200]
                self._save_favorites()
                return

    def remove_favorite(self, secID: str) -> None:
        self._favorite_cache = [item for item in self._favorite_cache if item.get('secID') != secID]
        self._save_favorites()

    def is_favorite(self, secID: str) -> bool:
        return any(item.get('secID') == secID for item in self._favorite_cache)


    def _normalize_expr(self, expr: str) -> str:
        return self._strategy_service.normalize_expr(expr)

    def run_backtest(self, code: str, start_date: str = None, end_date: str = None, capital: float = 100000.0,
                     entry_expr: str = '', exit_expr: str = '', **kwargs: Any) -> Dict[str, Any]:
        """委托给 BacktestService（含费率/滑点/完整指标/基准对比）。"""
        from .backtest_service import backtest_service
        return backtest_service.run(
            code=code,
            start_date=start_date,
            end_date=end_date,
            capital=capital,
            entry_expr=entry_expr,
            exit_expr=exit_expr,
            **kwargs,
        )

    def list_strategies(self) -> List[Dict[str, Any]]:
        uid = self._uid_safe()
        if uid is None:
            data = self._load_json(self._strategies_file, [])
            return data if isinstance(data, list) else []
        return db_store.list_strategies(uid)

    def get_strategy(self, name: str) -> Optional[Dict[str, Any]]:
        uid = self._uid_safe()
        if uid is None:
            data = self._load_json(self._strategies_file, [])
            for it in (data or []):
                if isinstance(it, dict) and str(it.get("name") or "") == str(name):
                    return it
            return None
        return db_store.get_strategy(uid, name)

    def save_factor(self, factor: Dict[str, Any]) -> None:
        """保存/更新一个表达式因子（回测策略），支持描述、回测参数与最近回测指标快照。"""
        uid = self._uid()
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        db_store.save_factor(uid, factor, now)

    def save_strategy(self, name: str, entry_expr: str, exit_expr: str) -> None:
        self.save_factor({"name": name, "entry_expr": entry_expr, "exit_expr": exit_expr})

    def delete_strategy(self, name: str) -> None:
        uid = self._uid()
        db_store.delete_strategy(uid, name)

    def _cash_detail(self, items: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """可用现金及其来源，供账户快照与弹药门控共用。

        两种来源，可信度差别很大：
        - explicit：用户在设置页/对话里填的「账户可用资金」，就是券商里的可用余额，可信。
        - estimated：本金 − 持仓成本。这个公式只在「从未卖出过」时等于现金。
          一旦卖出（尤其割肉），成本全额释放、实际只回收市值，算出来的现金会系统性偏大，
          让人误以为还有弹药。所以它只是兜底，必须带上 estimated 标记让上层提示校准。
        - unknown：没设本金也没设现金，无法判断，不做弹药门控。
        """
        settings = self.get_user_settings() or {}
        raw = self._to_float(settings.get('available_cash'))
        total_capital = self._to_float(settings.get('total_capital')) or 0.0
        # 「0 元现金」和「没填」是两件事：满仓时现金就是 0，不能退化成估算。
        # 判据有两条，任一成立就算用户显式告知过：
        #   ① 有 cash_updated_at（新版写入路径一定带）
        #   ② 配置文件里存在 available_cash 这个键（老版本写路径只存值不存时间戳，
        #      用户明明在设置页填了 0 表示满仓，却被当成"没填"退回估算，白填一遍）
        cash_declared = bool(str(settings.get('cash_updated_at') or '').strip()) or ('available_cash' in settings)
        if raw is not None and (raw > 0 or cash_declared):
            return {'value': max(0.0, float(raw)), 'source': 'explicit',
                    'total_capital': total_capital,
                    'updated_at': str(settings.get('cash_updated_at') or '')}

        if total_capital <= 0:
            return {'value': None, 'source': 'unknown', 'total_capital': total_capital}

        held_cost = 0.0
        try:
            source_items = items if items is not None else self.get_favorites_list(include_quotes=False)
            for it in source_items or []:
                shares = int(self._to_float(it.get('shares')) or 0)
                cost = self._to_float(it.get('cost')) or 0.0
                if shares > 0 and cost > 0:
                    held_cost += shares * cost
        except Exception:
            return {'value': None, 'source': 'unknown', 'total_capital': total_capital}
        return {
            'value': max(0.0, total_capital - held_cost),
            'source': 'estimated',
            'total_capital': total_capital,
            'held_cost': held_cost,
        }

    def _available_cash(self) -> Optional[float]:
        """可用弹药（剩余可投入资金）。无法判断时返回 None = 不做弹药门控。"""
        return self._cash_detail().get('value')

    def get_account_snapshot(self, items: Optional[List[Dict[str, Any]]] = None,
                             use_cache: bool = True) -> Dict[str, Any]:
        """账户级真实口径：持仓市值、可用现金、总资产、仓位、弹药状态。

        为什么不再用「市值 / 本金」当仓位：
        本金是历史投入额，不等于当前总资产（有已实现盈亏、出入金），
        用它当分母算出的「仓位」在亏钱时会虚高、在出金后会失真。
        真实口径只有一个：仓位 = 持仓市值 / (持仓市值 + 可用现金)。

        弹药状态三档，判据是「还能不能真的买进去」，不是固定百分比：
        - full  现金 < 一手门槛（默认 5000 元）或 < 总资产 3%：满仓，禁止一切买入提醒
        - tight 现金 < 总资产 10%：弹药紧，只允许减仓换仓，不建议新开仓
        - ample 其余
        - unknown 无法判断，不做门控
        """
        # 一轮监控里每只票都会问一次，30 秒内复用同一份快照，避免重复拉行情
        uid = self._uid()
        if items is None and use_cache:
            cached = self._snap_cache_get(uid)
            if isinstance(cached, dict) and (time.time() - float(cached.get('ts') or 0)) < 30:
                return cached.get('snap') or {}

        settings = self.get_user_settings() or {}
        min_ticket = self._to_float(settings.get('min_trade_amount')) or 5000.0

        if items is None:
            try:
                items = self.get_favorites_list(include_quotes=True)
            except Exception:
                items = []

        market_value = 0.0
        held_cost = 0.0
        held_count = 0
        for it in items or []:
            shares = int(self._to_float(it.get('shares')) or 0)
            if shares <= 0:
                continue
            cost = self._to_float(it.get('cost')) or 0.0
            price = self._to_float(it.get('price')) or cost
            market_value += (price or 0.0) * shares
            held_cost += cost * shares
            held_count += 1

        cash = self._cash_detail(items if items is not None else None)
        cash_value = cash.get('value')
        cash_source = str(cash.get('source') or 'unknown')

        snap: Dict[str, Any] = {
            'ok': True,
            'market_value': round(market_value, 2),
            'held_cost': round(held_cost, 2),
            'position_pnl': round(market_value - held_cost, 2),
            'position_pnl_pct': round((market_value - held_cost) / held_cost * 100, 2) if held_cost > 0 else None,
            'held_count': held_count,
            'total_capital': round(cash.get('total_capital') or 0.0, 2),
            'available_cash': round(cash_value, 2) if cash_value is not None else None,
            'cash_source': cash_source,
            'min_trade_amount': round(min_ticket, 2),
        }

        if cash_value is None:
            snap.update({
                'total_assets': None,
                'cash_ratio': None,
                'position_ratio': None,
                'ammo_state': 'unknown',
                'ammo_label': '未知',
                'buy_allowed': True,
                'reason': '未设置本金与可用资金，无法判断弹药；请到设置页填写「账户可用资金」',
            })
            self._attach_sentiment_policy(snap)
            self._attach_trade_stats(snap)
            if items is None and use_cache:
                self._snap_cache_set(uid, snap)
            return snap

        total_assets = market_value + cash_value
        cash_ratio = cash_value / total_assets if total_assets > 0 else 1.0
        # 满仓：一手都买不起，或者现金占比已经低到没有调仓空间
        if cash_value < min_ticket or cash_ratio < 0.03:
            ammo_state, ammo_label, buy_allowed = 'full', '满仓', False
            reason = (f'可用现金 {cash_value:,.0f} 元，不足一手门槛 {min_ticket:,.0f} 元'
                      if cash_value < min_ticket else f'现金占比仅 {cash_ratio:.1%}')
            reason += '；满仓状态下只提醒卖出与风险，不提醒买入'
        elif cash_ratio < 0.10:
            ammo_state, ammo_label, buy_allowed = 'tight', '弹药偏紧', False
            reason = f'现金占比 {cash_ratio:.1%}，只够零星补仓；建议优先减仓换仓，不建议新开仓'
        else:
            ammo_state, ammo_label, buy_allowed = 'ample', '弹药充足', True
            reason = f'可用现金 {cash_value:,.0f} 元（占总资产 {cash_ratio:.1%}）'

        snap.update({
            'total_assets': round(total_assets, 2),
            'cash_ratio': round(cash_ratio, 4),
            'position_ratio': round(1.0 - cash_ratio, 4),
            'ammo_state': ammo_state,
            'ammo_label': ammo_label,
            'buy_allowed': buy_allowed,
            'reason': reason,
        })
        if cash_source == 'estimated':
            snap['reason'] += '（现金为「本金−持仓成本」估算值，卖出过就会偏大，建议校准）'
        self._attach_sentiment_policy(snap)
        self._attach_trade_stats(snap)
        if items is None and use_cache:
            self._snap_cache_set(uid, snap)
        return snap

    def _attach_sentiment_policy(self, snap: Dict[str, Any]) -> None:
        """按当前市场情绪给出目标仓位区间，并对"能否买入"做情绪否决。

        用户理念：市场情绪低落时要留有现金、开始建仓；情绪疯狂时要卖出。
        账户弹药原本只看现金比例（能不能买得起），这里叠加市场情绪
        （该不该在这个时点买）——两个维度缺一不可。
        """
        try:
            from .sentiment_position_service import build_sentiment_position_policy
            sentiment = self.get_market_sentiment() or {}
            market = self.get_market_regime() or {}
            policy = build_sentiment_position_policy(
                sentiment=sentiment,
                market=market,
                account={'position_ratio': snap.get('position_ratio')},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug('sentiment_policy_unavailable error=%r', exc)
            return
        if not policy.get('ok'):
            return

        snap['sentiment_policy'] = policy
        snap['sentiment_regime'] = policy.get('regime')
        snap['sentiment_regime_label'] = policy.get('regime_label')
        snap['target_position_min'] = policy.get('target_position_min')
        snap['target_position_max'] = policy.get('target_position_max')
        snap['target_cash_min'] = policy.get('target_cash_min')
        snap['target_cash_max'] = policy.get('target_cash_max')
        snap['sentiment_action'] = policy.get('action_text')
        snap['sentiment_gap_action'] = policy.get('gap_action')
        snap['sentiment_allow_new_buy'] = policy.get('allow_new_buy')

        # 情绪否决层：亢奋 / 退潮 / 崩塌期一律不新增买入（不博傻、活着优先）
        if not policy.get('allow_new_buy', True) and snap.get('buy_allowed'):
            snap['buy_allowed'] = False
            snap['reason'] += (
                f"；且当前市场处于「{policy.get('regime_label')}」，"
                "情绪纪律禁止新增买入（低情绪留现金建仓、高情绪兑现，不追高）"
            )

    def _attach_trade_stats(self, snap: Dict[str, Any]) -> None:
        """把交易流水里的已实现盈亏挂到账户快照上。

        有记账的情况下，这个数比「持仓成本+现金−本金」反推出来的估算值可信：
        它按每笔卖出的真实成交价减成本算，不受入金/出金影响。
        """
        try:
            from .trade_ledger import trade_ledger
            summary = trade_ledger.summary()
        except Exception as exc:  # noqa: BLE001
            logger.debug("trade_stats_unavailable error=%r", exc)
            return
        snap['realized_pnl'] = summary.get('realized_pnl')
        snap['realized_pnl_source'] = 'ledger' if summary.get('realized_pnl') is not None else None
        snap['trade_count'] = summary.get('total_records') or 0
        snap['trade_fee_total'] = summary.get('total_fee') or 0.0

    def _build_position_plan(
        self,
        shares: int,
        cost: float,
        price: Optional[float],
        position_cfg: Dict[str, Any],
        market_stage: str,
        entry_stage_ok: bool,
        entry_ma_ok: bool,
        entry_hit: bool,
        has_buy: bool,
        has_sell: bool,
    ) -> Dict[str, Any]:
        total_capital = self._to_float(self.get_user_settings().get('total_capital')) or 0.0
        shares = int(shares or 0)
        cost = float(cost or 0.0)
        current_price = self._to_float(price) or cost
        cost_amount = shares * cost if shares > 0 and cost > 0 else 0.0
        current_pct = (cost_amount / total_capital * 100.0) if total_capital > 0 else 0.0
        initial_pct = self._to_float(position_cfg.get('initial_pct')) or 0.0
        max_pct = self._to_float(position_cfg.get('max_pct')) or 0.0
        add_pct = self._to_float(position_cfg.get('add_pct')) or 0.0
        add_signal_preference = str(position_cfg.get('add_signal_preference') or 'right').strip().lower()
        add_signal_preference = 'left' if add_signal_preference == 'left' else 'right'
        plan = {
            'total_capital': round(total_capital, 2),
            'cost_amount': round(cost_amount, 2),
            'current_pct': round(current_pct, 2),
            'initial_pct': round(initial_pct, 2),
            'max_pct': round(max_pct, 2),
            'add_pct': round(add_pct, 2),
            'add_signal_preference': add_signal_preference,
            'suggestion': 'hold',
            'suggestion_text': '当前仓位在配置范围内',
            'suggest_price': round(current_price, 3) if current_price else None,
            'suggest_shares': 0,
            'suggest_amount': 0.0,
            'target_pct': round(current_pct, 2),
        }
        if total_capital <= 0 or current_price <= 0:
            plan['suggestion'] = 'set_capital'
            plan['suggestion_text'] = '请先设置本金，再计算个股仓位'
            return plan

        def round_lot(value: float) -> int:
            return max(0, int(value // 100) * 100)

        if shares > 0 and max_pct > 0 and current_pct > max_pct:
            reduce_amount = max(0.0, cost_amount - total_capital * max_pct / 100.0)
            reduce_shares = min(shares, round_lot(reduce_amount / current_price))
            plan.update({
                'suggestion': 'reduce',
                'suggestion_text': '当前个股仓位超过上限，建议减仓',
                'suggestion_reason': 'over_max_position',
                'suggest_shares': reduce_shares,
                'suggest_amount': round(reduce_shares * current_price, 2),
                'target_pct': round(max_pct, 2),
            })
            return plan

        if shares > 0 and has_sell:
            reduce_amount = min(cost_amount, total_capital * max(add_pct, 5.0) / 100.0)
            reduce_shares = min(shares, round_lot(reduce_amount / current_price))
            plan.update({
                'suggestion': 'reduce',
                'suggestion_text': '持仓中出现卖出信号，建议减仓',
                'suggestion_reason': 'sell_signal',
                'suggest_shares': reduce_shares,
                'suggest_amount': round(reduce_shares * current_price, 2),
                'target_pct': round(max(0.0, current_pct - (reduce_shares * current_price / total_capital * 100.0)), 2),
            })
            return plan

        if add_signal_preference == 'left':
            add_allowed = entry_stage_ok and entry_ma_ok and market_stage != 'downtrend'
            target_pct = initial_pct if shares <= 0 else min(max_pct or current_pct, current_pct + (add_pct or 0.0))
        else:
            add_allowed = entry_hit or has_buy
            target_pct = initial_pct if shares <= 0 else min(max_pct or current_pct, current_pct + (add_pct or 0.0))

        if add_allowed:
            if target_pct > current_pct:
                # 账户级弹药门控：满仓时不管个股信号多好都不推买入。
                # 和下面的个股级门控（按一手算）是两道关：
                # 个股级管"这只票买不买得起"，账户级管"整个账户还有没有钱"。
                account = self.get_account_snapshot()
                if account.get('buy_allowed') is False:
                    state = str(account.get('ammo_state') or '')
                    plan.update({
                        'suggestion': 'hold',
                        'suggestion_reason': 'account_full' if state == 'full' else 'account_tight',
                        'suggestion_text': (
                            f"出现买入信号，但账户{account.get('ammo_label') or '弹药不足'}"
                            f"（{account.get('reason') or ''}），本次不建议买入"
                        ),
                        'suppressed_suggestion': 'add' if shares > 0 else 'buy',
                        'available_cash': account.get('available_cash'),
                        'ammo_state': state,
                        'position_ratio': account.get('position_ratio'),
                        'suggest_shares': 0,
                        'suggest_amount': 0.0,
                        'target_pct': round(current_pct, 2),
                    })
                    return plan
                add_amount = total_capital * (target_pct - current_pct) / 100.0
                add_shares = round_lot(add_amount / current_price)
                if add_shares > 0:
                    # 弹药门控：可用资金不足时不建议买入（部分不足则缩减到可买股数）
                    ammo = self._available_cash()
                    ammo_note = ''
                    if ammo is not None and ammo + 1e-6 < add_shares * current_price:
                        shrunk_shares = round_lot(ammo / current_price)
                        if shrunk_shares > 0:
                            add_shares = shrunk_shares
                            ammo_note = f'（受可用弹药 {ammo:,.0f} 元限制，已缩减股数）'
                        else:
                            plan.update({
                                'suggestion': 'hold',
                                'suggestion_reason': 'no_ammo',
                                'suggestion_text': f'出现买入信号，但可用弹药不足（约 {ammo:,.0f} 元，不够买入一手），不建议买入；如已补充资金请到系统设置更新可用弹药',
                                'suppressed_suggestion': 'add' if shares > 0 else 'buy',
                                'available_cash': round(ammo, 2),
                                'suggest_shares': 0,
                                'suggest_amount': 0.0,
                                'target_pct': round(current_pct, 2),
                            })
                            return plan
                    plan.update({
                        'suggestion': 'add' if shares > 0 else 'buy',
                        'suggestion_text': ('左侧信号偏好，满足早期建仓条件，建议先手加仓' if add_signal_preference == 'left' else '买入条件满足且当前仓位低于目标，建议加仓') + ammo_note,
                        'suggest_shares': add_shares,
                        'suggest_amount': round(add_shares * current_price, 2),
                        'target_pct': round((current_pct + add_shares * current_price / total_capital * 100.0) if ammo_note else target_pct, 2),
                    })
                    return plan

        if shares > 0 and initial_pct > 0 and current_pct < initial_pct:
            plan['suggestion_text'] = '当前仓位低于初始仓位，但还没有出现明确买入信号'
        return plan

    def _adjust_position_plan_by_valuation(self, position_plan: Dict[str, Any], valuation: Dict[str, Any]) -> Dict[str, Any]:
        plan = dict(position_plan or {})
        if (
            plan.get('suggestion') == 'reduce'
            and plan.get('suggestion_reason') == 'over_max_position'
            and str((valuation or {}).get('zone') or '').lower() == 'undervalued'
        ):
            plan.update({
                'suggestion': 'hold',
                'suggestion_reason': 'undervalued_over_max_wait',
                'suggestion_text': '当前仍处于低估区，虽然仓位超过上限，先持有等待价格回归或估值转高后再考虑减仓',
                'suppressed_suggestion': 'reduce',
                'suggest_shares': 0,
                'suggest_amount': 0.0,
                'target_pct': plan.get('current_pct'),
            })
        return plan

    def _build_monitor_explanation(
        self,
        price: Any,
        spot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        market_stage: str,
        stage_reason: str,
        valuation: Dict[str, Any],
        value_advice: str,
        position_plan: Dict[str, Any],
        action: str,
        action_desc: str,
        strategy_eval: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        pct_chg = self._to_float((spot or {}).get('pct_chg') or (spot or {}).get('pctChg'))
        pnl_pct = self._to_float((strategy_eval or {}).get('pnl_pct')) if isinstance(strategy_eval, dict) else None
        buy_count = len([s for s in signals or [] if s.get('type') == 'buy'])
        sell_count = len([s for s in signals or [] if s.get('type') == 'sell'])
        valuation_zone = str((valuation or {}).get('zone') or 'unknown')
        return {
            'price_signal': {
                'status': 'ok' if price is not None else 'missing',
                'title': '价格信号',
                'detail': f"现价 {price if price is not None else '-'}，涨跌幅 {pct_chg if pct_chg is not None else '-'}%，当前盈亏 {pnl_pct if pnl_pct is not None else '-'}%。",
                'price': price,
                'pct_chg': pct_chg,
                'pnl_pct': pnl_pct,
            },
            'trend_signal': {
                'status': market_stage or 'unknown',
                'title': '趋势信号',
                'detail': stage_reason or '暂无趋势说明',
                'stage': market_stage,
                'buy_signal_count': buy_count,
                'sell_signal_count': sell_count,
            },
            'valuation_signal': {
                'status': valuation_zone,
                'title': '估值信号',
                'detail': (valuation or {}).get('reason') or (valuation or {}).get('industry_fallback_reason') or '暂无估值说明',
                'zone': valuation_zone,
                'industry': (valuation or {}).get('industry'),
                'score': (valuation or {}).get('score'),
                'value_advice': value_advice,
            },
            'position_signal': {
                'status': (position_plan or {}).get('suggestion') or 'hold',
                'title': '仓位信号',
                'detail': (position_plan or {}).get('suggestion_text') or '暂无仓位建议',
                'current_pct': (position_plan or {}).get('current_pct'),
                'max_pct': (position_plan or {}).get('max_pct'),
                'target_pct': (position_plan or {}).get('target_pct'),
            },
            'final_advice': {
                'status': action,
                'title': '最终建议',
                'detail': action_desc or '暂无最终建议',
                'action': action,
            },
        }

    def check_strategy(self, code: str, shares: int = 0, cost: float = 0.0, horizon: str = 'long', buy_date: str = None, kline_data: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        horizon = self._normalize_horizon(horizon)
        strategy_config = self.get_trade_strategy_config(horizon)
        data = kline_data if kline_data is not None else self.get_k_line_data(code, frequency='d', caller='monitor_check')
        kline_count = len(data)
        # 基金/ETF 只给数据，不进策略体系。放在最前面是为了**根本不算**：
        # 阶段判定、信号表达式、估值、仓位建议全部跳过——算完再丢弃既浪费又容易漏一处。
        from .market_gateway import is_data_only

        if is_data_only(code):
            last_bar = data[-1] if data else {}
            price = self._to_float(last_bar.get('close'))
            return {
                'code': code,
                'status': 'data_only',
                'signals': [],
                'action': 'data_only',
                'action_desc': '仅提供行情数据，不参与策略判断',
                'market_stage': 'unknown',
                'stage_reason': 'ETF/场内基金不进策略体系（无 PE/PB、无财报，个股框架不适用）',
                'price': price,
                'kline_count': kline_count,
                'required_kline_count': 0,
                'kline_error': '',
                'spot': {},
                'source': (last_bar.get('source') or ''),
                'horizon': horizon,
                'buy_date': buy_date,
                'trend_strategy': 'data_only',
                'position_advice': {},
                'valuation': {'available': False, 'reason': 'fund_data_only'},
                'value_advice': 'none',
                'position_plan': {},
                'explanation': ('ETF/场内基金只提供行情数据（K线、报价、名称），'
                                '不做时间止损、不出买卖信号、不做估值判断。'
                                '需要判断买卖时，请按指数/标的所处位置自行决定。'),
                'strategy_triggered': False,
                'strategy_signal': '',
                'strategy_eval': {},
                'data_only': True,
            }
        required_kline_count = int(strategy_config.get('required_kline_count') or 30)
        if len(data) < required_kline_count:
            spot = self.get_stock_spot_info(code)
            valuation = self._evaluate_valuation(code, spot=spot)
            value_advice = "none"
            if horizon == "long" and valuation.get("zone") == "undervalued":
                value_advice = "add_on_dips"
            position_cfg = strategy_config.get('position') if isinstance(strategy_config.get('position'), dict) else {}
            position_plan = self._adjust_position_plan_by_valuation(
                self._build_position_plan(
                    shares,
                    cost,
                    (spot or {}).get('price'),
                    position_cfg,
                    'unknown',
                    False,
                    False,
                    False,
                    False,
                    False,
                ),
                valuation,
            )
            if position_plan.get('suggestion_reason') == 'undervalued_over_max_wait':
                value_advice = "hold_wait_revaluation"
            explanation = self._build_monitor_explanation(
                (spot or {}).get('price'),
                spot or {},
                [],
                'unknown',
                'Not enough data.',
                valuation,
                value_advice,
                position_plan,
                'watch',
                f'Insufficient kline data: {kline_count}/{required_kline_count}.',
                {'pnl_pct': 0.0},
            )
            return {
                'code': code,
                'status': 'insufficient_kline',
                'signals': [],
                'action': 'watch',
                'action_desc': f'Insufficient kline data: {kline_count}/{required_kline_count}.',
                'market_stage': 'unknown',
                'stage_reason': 'Not enough data.',
                'kline_count': kline_count,
                'required_kline_count': required_kline_count,
                'kline_error': 'insufficient_kline_data',
                'price': (spot or {}).get('price'),
                'spot': spot or {},
                'source': (spot or {}).get('source') or '',
                'horizon': horizon,
                'buy_date': buy_date,
                'trend_strategy': strategy_config.get('name') or horizon,
                'position_advice': position_cfg,
                'valuation': valuation,
                'value_advice': value_advice,
                'position_plan': position_plan,
                'explanation': explanation,
                'strategy_triggered': False,
                'strategy_signal': '',
            }
        last, prev = data[-1], data[-2]
        signals = []
        price, ma20 = last.get('close'), last.get('MA20')
        if price and ma20 and prev.get('close') and prev.get('MA20'):
            if prev['close'] <= prev['MA20'] and price > ma20:
                signals.append({'name': 'ma_break_up', 'type': 'buy', 'desc': 'Price crossed above MA20.'})
            elif prev['close'] >= prev['MA20'] and price < ma20:
                signals.append({'name': 'ma_break_down', 'type': 'sell', 'desc': 'Price crossed below MA20.'})
        rsi = last.get('RSI6')
        if rsi is not None:
            if rsi < 20:
                signals.append({'name': 'rsi_oversold', 'type': 'buy', 'desc': f'RSI6 is low at {rsi:.1f}.'})
            elif rsi > 80:
                signals.append({'name': 'rsi_overbought', 'type': 'sell', 'desc': f'RSI6 is high at {rsi:.1f}.'})
        dif, dea = last.get('DIF'), last.get('DEA')
        if None not in [dif, dea, prev.get('DIF'), prev.get('DEA')]:
            if prev['DIF'] <= prev['DEA'] and dif > dea:
                signals.append({'name': 'macd_golden_cross', 'type': 'buy', 'desc': 'MACD golden cross.'})
            elif prev['DIF'] >= prev['DEA'] and dif < dea:
                signals.append({'name': 'macd_dead_cross', 'type': 'sell', 'desc': 'MACD dead cross.'})
        market_stage, stage_reason = 'sideways', 'No clear market structure detected.'
        ma5, ma10, ma60 = last.get('MA5'), last.get('MA10'), last.get('MA60')
        if None not in [price, ma5, ma10, ma20, ma60]:
            if price > ma5 > ma10 > ma20 > ma60:
                market_stage, stage_reason = 'uptrend', 'Price and moving averages are aligned in a bullish structure.'
            elif price < ma20 < ma60:
                market_stage, stage_reason = 'downtrend', 'Price is below medium and long-term averages.'
            elif abs(price - ma60) / ma60 < 0.05 and abs(ma20 - ma60) / ma60 < 0.05:
                market_stage, stage_reason = 'accumulation', 'Price is close to long-term averages and volatility is compressed.'
            elif price < ma10 and price > ma20:
                market_stage, stage_reason = 'washout', 'Short pullback inside a stronger structure.'
        has_buy = any(s['type'] == 'buy' for s in signals)
        has_sell = any(s['type'] == 'sell' for s in signals)
        entry_cfg = strategy_config.get('entry') if isinstance(strategy_config.get('entry'), dict) else {}
        take_profit_cfg = strategy_config.get('take_profit') if isinstance(strategy_config.get('take_profit'), dict) else {}
        stop_loss_cfg = strategy_config.get('stop_loss') if isinstance(strategy_config.get('stop_loss'), dict) else {}
        position_cfg = strategy_config.get('position') if isinstance(strategy_config.get('position'), dict) else {}
        allowed_stages = set(entry_cfg.get('allowed_stages') or [])
        require_buy_signal = bool(entry_cfg.get('require_buy_signal', False))
        min_price_above_ma20_pct = self._to_float(entry_cfg.get('min_price_above_ma20_pct'))
        price_ma20_pct = ((price - ma20) / ma20 * 100) if price and ma20 else None
        entry_stage_ok = not allowed_stages or market_stage in allowed_stages
        entry_signal_ok = has_buy or not require_buy_signal
        entry_ma_ok = price_ma20_pct is None or min_price_above_ma20_pct is None or price_ma20_pct >= min_price_above_ma20_pct
        entry_hit = bool(entry_stage_ok and entry_signal_ok and entry_ma_ok and market_stage != 'downtrend')
        pnl = ((price - cost) / cost * 100) if price and cost else 0.0
        stop_loss_pct = self._to_float(stop_loss_cfg.get('pct'))
        take_profit_pct = self._to_float(take_profit_cfg.get('pct'))
        stop_hit = shares > 0 and stop_loss_pct is not None and pnl <= stop_loss_pct
        take_profit_hit = shares > 0 and take_profit_pct is not None and pnl >= take_profit_pct
        action, action_desc = 'watch', 'No strong signal yet. Keep monitoring.'
        strategy_triggered = False
        strategy_signal = ''
        if shares > 0:
            if stop_hit:
                action = str(stop_loss_cfg.get('action') or 'stop_loss')
                action_desc = f"Stop loss triggered by {strategy_config.get('name')}: current pnl is {pnl:.2f}%."
                strategy_triggered = True
                strategy_signal = 'stop_loss'
            elif take_profit_hit:
                action = str(take_profit_cfg.get('action') or 'reduce')
                action_desc = f"Take profit triggered by {strategy_config.get('name')}: current pnl is {pnl:.2f}%."
                strategy_triggered = True
                strategy_signal = 'take_profit'
            elif has_sell:
                action, action_desc = 'reduce', 'Sell signals are present while holding a position.'
                strategy_triggered = True
                strategy_signal = 'sell'
            elif has_buy:
                action, action_desc = 'hold', 'Buy-side conditions still support holding.'
            else:
                action, action_desc = 'hold', f'No stop rule triggered. Current pnl is {pnl:.2f}%.'
        else:
            if entry_hit and market_stage == 'uptrend':
                action, action_desc = 'trend_follow', f"{strategy_config.get('name')} entry conditions matched: confirmed uptrend."
                strategy_triggered = True
                strategy_signal = 'buy'
            elif entry_hit and market_stage == 'accumulation':
                action, action_desc = 'accumulate', f"{strategy_config.get('name')} entry conditions matched: accumulation zone."
                strategy_triggered = True
                strategy_signal = 'buy'
            elif entry_hit and market_stage == 'washout':
                action, action_desc = 'buy_dip', f"{strategy_config.get('name')} entry conditions matched: constructive pullback."
                strategy_triggered = True
                strategy_signal = 'buy'
            elif has_buy:
                action, action_desc = 'probe_buy', 'Buy signal detected. Consider a small starter position.'
        position_plan = self._build_position_plan(
            shares,
            cost,
            price,
            position_cfg,
            market_stage,
            entry_stage_ok,
            entry_ma_ok,
            entry_hit,
            has_buy,
            has_sell,
        )
        spot = self.get_stock_spot_info(code)
        valuation = self._evaluate_valuation(code, spot=spot)
        value_advice = "none"
        if horizon == "long" and valuation.get("zone") == "undervalued":
            value_advice = "add_on_dips"
        position_plan = self._adjust_position_plan_by_valuation(position_plan, valuation)
        if position_plan.get('suggestion_reason') == 'undervalued_over_max_wait':
            value_advice = "hold_wait_revaluation"
        if position_plan.get('suggestion') == 'reduce' and action in {'watch', 'hold'}:
            action = 'reduce'
            action_desc = 'Position is above configured single-stock max. Consider reducing to target position.'
            strategy_triggered = True
            strategy_signal = strategy_signal or 'position_reduce'
        elif position_plan.get('suggestion') in {'buy', 'add'} and action in {'watch', 'hold'}:
            action = 'hold_add' if shares > 0 else 'buy'
            action_desc = 'Buy conditions matched and current position is below target. Consider adding near current price.'
            strategy_triggered = True
            strategy_signal = strategy_signal or 'position_add'
        if horizon == "long" and valuation.get("zone") == "undervalued":
            if value_advice == "add_on_dips" and action in {'watch', 'hold'} and position_plan.get('suggestion') in {'hold', 'buy', 'add'}:
                action = 'hold_add'
                action_desc = 'Long-horizon valuation is in an undervalued zone. Consider adding on dips.'
                strategy_triggered = True
                strategy_signal = strategy_signal or 'value_add'
        strategy_eval = {
            'entry_hit': entry_hit,
            'stop_loss_hit': stop_hit,
            'take_profit_hit': take_profit_hit,
            'pnl_pct': pnl,
            'price_ma20_pct': price_ma20_pct,
        }
        explanation = self._build_monitor_explanation(
            price,
            spot or {},
            signals,
            market_stage,
            stage_reason,
            valuation,
            value_advice,
            position_plan,
            action,
            action_desc,
            strategy_eval,
        )
        return {
            'code': code,
            'status': 'ok',
            'signals': signals,
            'action': action,
            'action_desc': action_desc,
            'market_stage': market_stage,
            'stage_reason': stage_reason,
            'price': price,
            'kline_count': kline_count,
            'required_kline_count': required_kline_count,
            'kline_error': '',
            'spot': spot or {},
            'source': (data[-1].get('source') if data else '') or (spot or {}).get('source') or '',
            'horizon': horizon,
            'buy_date': buy_date,
            'trend_strategy': strategy_config.get('name') or horizon,
            'position_advice': position_cfg,
            'valuation': valuation,
            'value_advice': value_advice,
            'position_plan': position_plan,
            'explanation': explanation,
            'strategy_triggered': strategy_triggered,
            'strategy_signal': strategy_signal,
            'strategy_eval': strategy_eval,
        }

    def monitor_favorites(self, force_refresh: bool = False, respect_trading_hours: bool = True) -> List[Dict[str, Any]]:
        return self._monitor_service.monitor_favorites(force_refresh=force_refresh, respect_trading_hours=respect_trading_hours)
        if not self._monitor_lock.acquire(blocking=False):
            cached = [dict(item) for item in self._monitor_cache]
            for item in cached:
                item['monitor_busy'] = True
            logger.info("monitor_skip_busy cached_rows=%s", len(cached))
            return cached
        try:
            return self._monitor_favorites_locked(force_refresh=force_refresh)
        finally:
            self._monitor_lock.release()

    def _monitor_favorites_locked(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        return self._monitor_service.monitor_favorites_locked(force_refresh=force_refresh)
        settings = self.get_user_settings()
        monitor_strategy_name = str(settings.get('monitor_strategy') or '').strip()
        monitor_strategy = self._find_strategy_by_name(monitor_strategy_name)

        def build_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            code = item.get('secID')
            if not code:
                return None
            shares = int(item.get('shares') or 0)
            caller = 'monitor_check_refresh' if force_refresh else 'monitor_check'
            kline_data = self.get_k_line_data(code, frequency='d', caller=caller)
            strategy = self.check_strategy(
                code,
                shares=shares,
                cost=float(item.get('cost') or 0.0),
                horizon=item.get('horizon', 'short'),
                buy_date=item.get('buy_date'),
                kline_data=kline_data,
            )
            strategy['secID'] = code
            strategy['name'] = item.get('name') or self._get_cached_stock_name(self._normalize_stock_code(code)['digits']) or code
            strategy['monitor_strategy'] = monitor_strategy_name
            strategy['strategy_triggered'] = False
            strategy['strategy_signal'] = ''
            if monitor_strategy:
                eval_result = self._eval_strategy_signal(
                    code,
                    str(monitor_strategy.get('entry_expr') or ''),
                    str(monitor_strategy.get('exit_expr') or ''),
                    kline_data=kline_data,
                )
                entry_hit = bool(eval_result.get('entry_hit'))
                exit_hit = bool(eval_result.get('exit_hit'))
                # 有持仓优先关注卖出条件；无持仓优先关注买入条件
                if shares > 0:
                    strategy['strategy_triggered'] = exit_hit or entry_hit
                    strategy['strategy_signal'] = 'sell' if exit_hit else ('buy' if entry_hit else '')
                else:
                    strategy['strategy_triggered'] = entry_hit or exit_hit
                    strategy['strategy_signal'] = 'buy' if entry_hit else ('sell' if exit_hit else '')
                strategy['strategy_eval'] = eval_result
            return strategy

        items = list(self._favorite_cache)
        if not items:
            self._monitor_cache = []
            self._monitor_cache_last_update = time.time()
            return []
        if len(items) == 1:
            result = build_item(items[0])
            results = [result] if result else []
            self._monitor_cache = [dict(item) for item in results]
            self._monitor_cache_last_update = time.time()
            return results

        results: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(6, len(items))) as executor:
            futures = [executor.submit(contextvars.copy_context().run, build_item, item) for item in items]
            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)
        results = sorted(results, key=lambda item: item.get('secID', ''))
        self._monitor_cache = [dict(item) for item in results]
        self._monitor_cache_last_update = time.time()
        return results

    # 指数代码带市场前缀：sh000001 / sz399001 / sz399006
    _INDEX_TARGETS = [('sh000001', '上证指数'), ('sz399001', '深证成指'), ('sz399006', '创业板指')]

    def get_market_indices(self) -> Dict[str, Any]:
        """首屏顶栏的三个大盘指数。

        这是页面打开后的**第一个**请求，所以对延迟最敏感。原实现走
        `ak.stock_zh_index_spot_sina()`，它内部要**串行翻 8 页**（实测 9–11 秒）
        才凑齐全量 562 行，而我们只需要 3 行。现改为腾讯批量接口（实测 ~0.3 秒），
        新浪只作为回退，并加 10 秒缓存（非交易时段 60 秒，收盘价不会变）。
        """
        targets = self._INDEX_TARGETS
        ttl = 10.0 if is_trading_now() else 60.0
        cache_key = self._source_cache_key('market_indices')

        cached = self._get_source_cache(cache_key, ttl)
        if isinstance(cached, list) and cached:
            return {'data': cached}

        # 首屏可能同时有多个请求（自动刷新 + 手动刷新），只让第一个真正取数
        lock = self._get_source_inflight_lock(cache_key)
        lock.acquire()
        try:
            cached = self._get_source_cache(cache_key, ttl)
            if isinstance(cached, list) and cached:
                return {'data': cached}
            result = self._fetch_indices_tencent(targets) or self._fetch_indices_sina(targets)
            if result:
                self._set_source_cache(cache_key, result)
            return {'data': result}
        finally:
            lock.release()

    def _fetch_indices_tencent(self, targets) -> List[Dict[str, Any]]:
        """腾讯批量快照取指数，失败返回空列表（由调用方回退新浪）。"""
        try:
            quotes = self._tencent_tool.fetch_quotes([symbol for symbol, _ in targets]) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning('market_indices_tencent_failed error=%r', exc)
            return []

        result: List[Dict[str, Any]] = []
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        for symbol, name in targets:
            # fetch_quotes 以 6 位代码为键。sh000001 与 sz000001（平安银行）同码，
            # 这里只请求指数本身，不会与个股撞键。
            quote = quotes.get(symbol[-6:]) or {}
            price = self._to_float(quote.get('price'))
            if price is None:
                continue
            result.append({
                'name': name,
                'code': symbol,
                'price': price,
                'change': self._to_float(quote.get('pct_chg')),
                'amount': self._to_float(quote.get('amount')),
                'date': today,
            })
        if result:
            logger.info('market_indices_source=tencent rows=%s', len(result))
        return result

    def _fetch_indices_sina(self, targets) -> List[Dict[str, Any]]:
        """回退链路：akshare 新浪全量快照（慢，仅当腾讯不可用时才走）。"""
        result: List[Dict[str, Any]] = []
        try:
            df = self._market_tool.get_market_indices_df()
            columns = [str(col) for col in df.columns]
            code_col = self._first_existing_column(columns, ['代码', 'code', 'symbol', 'ticker'], 0)
            for digits, name in targets:
                row = df[df[code_col].astype(str) == digits]
                if not row.empty:
                    item = row.iloc[0].to_dict()
                    result.append({
                        'name': name,
                        'code': digits,
                        'price': self._to_float(self._get_row_value(item, columns, ['最新价', 'price', 'last_price', 'close'], 2)),
                        'change': self._to_float(self._get_row_value(item, columns, ['涨跌幅', 'pct_chg', 'change_percent'], 4)),
                        'amount': self._to_float(self._get_row_value(item, columns, ['成交额', 'amount', 'turnover'], 6)),
                        'date': datetime.datetime.now().strftime('%Y-%m-%d'),
                    })
        except Exception as exc:  # noqa: BLE001
            logger.warning('market_indices_sina_failed error=%r', exc)
        if result:
            logger.info('market_indices_source=sina rows=%s', len(result))
        return result

    def get_chip_distribution(self, code: str, kline_data: List[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = kline_data or self.get_k_line_data(code, start_date=(datetime.datetime.now() - datetime.timedelta(days=300)).strftime('%Y-%m-%d'), caller='chip_distribution')
        if not data:
            return {'error': 'No data available'}
        closes = [float(item['close']) for item in data if item.get('close') is not None]
        if not closes:
            return {'error': 'No data available'}
        current_price = closes[-1]
        bins = {}
        for close in closes:
            bucket = round(close, 1)
            bins[bucket] = bins.get(bucket, 0) + 1
        distribution = [{'price': price, 'volume': volume, 'type': 'profit' if price < current_price else 'trapped'} for price, volume in sorted(bins.items())]
        total = sum(bins.values())
        profit_ratio = sum(v for p, v in bins.items() if p < current_price) / total * 100
        trapped_ratio = 100 - profit_ratio
        avg_cost = sum(p * v for p, v in bins.items()) / total
        return {'current_price': current_price, 'distribution': distribution, 'profit_ratio': round(profit_ratio, 2), 'trapped_ratio': round(trapped_ratio, 2), 'average_cost': round(avg_cost, 2), 'concentration': 0, 'cost_range_90': '-'}

    def get_money_flow(self, code: str) -> Dict[str, Any]:
        try:
            normalized = self._normalize_stock_code(code)
            df = self._market_tool.get_money_flow_df(normalized['digits'], normalized['market'])
            if df is None or df.empty:
                return {'data': []}
            columns = [str(col) for col in df.columns]
            result = []
            for _, row in df.tail(60).iloc[::-1].iterrows():
                result.append({
                    'date': str(self._get_row_value(row, columns, ['date'], 0) or ''),
                    'close': self._get_row_value(row, columns, ['close', 'price'], 1),
                    'pct_chg': self._get_row_value(row, columns, ['pct_chg', 'change_percent'], 2),
                    'main_net_in': self._get_row_value(row, columns, ['main_net_in', 'main_net_amount'], 3),
                    'main_net_pct': self._get_row_value(row, columns, ['main_net_pct', 'main_net_ratio'], 4),
                })
            return {'data': result}
        except Exception as exc:
            logger.warning("money_flow_failed code=%s error=%r", code, exc)
            return {'data': [], 'error': _friendly_error(exc)}

    def diagnose_stock(self, secID: str, llm) -> str:
        strategy = self.check_strategy(secID)
        company = self.get_company_info(secID)
        parts = [f"Code: {secID}", f"Stage: {strategy.get('market_stage')}", f"Action: {strategy.get('action')}", f"Reason: {strategy.get('action_desc')}", f"Signals: {', '.join(signal['name'] for signal in strategy.get('signals', [])) or 'none'}"]
        if company.get('industry'):
            parts.append(f"Industry: {company.get('industry')}")
        return '\n'.join(parts)

    def benchmark_spot_sources(self, sample_size: int = 80, timeout_sec: float = 2.0) -> Dict[str, Any]:
        sample_size = max(1, int(sample_size))
        timeout_sec = max(0.5, float(timeout_sec))
        benchmark: Dict[str, Any] = {
            'full_snapshot': {},
            'single_quote': {},
        }

        full_t0 = time.perf_counter()
        df = self._get_cached_spot_data(force_refresh=True)
        full_dt = time.perf_counter() - full_t0
        benchmark['full_snapshot'] = {
            'rows': 0 if df is None else int(len(df)),
            'seconds': round(full_dt, 3),
        }

        codes: List[str] = []
        code_df = self._market_tool.get_stock_list()
        if code_df is not None and not code_df.empty:
            codes = [str(v) for v in code_df['code'].tolist()]
        if not codes:
            benchmark['single_quote'] = {
                'sample_size': sample_size,
                'error': 'failed_to_get_code_list',
            }
            return benchmark

        random.shuffle(codes)
        picked = codes[:sample_size]
        src_counter: Counter = Counter()
        latencies: List[float] = []
        ok = 0
        fail = 0

        old_timeout = self._spot_source_timeout
        self._spot_source_timeout = timeout_sec
        try:
            for digits in picked:
                secid = f"sh{digits}" if digits.startswith(("6", "9")) else f"sz{digits}"
                t0 = time.perf_counter()
                quote = self.get_stock_spot_info(secid, allow_full_market_fallback=True, force_refresh=False)
                dt = time.perf_counter() - t0
                latencies.append(dt)
                if quote and quote.get('price') is not None:
                    ok += 1
                    src_counter[str(quote.get('source') or 'unknown')] += 1
                else:
                    fail += 1
        finally:
            self._spot_source_timeout = old_timeout

        slow_threshold = timeout_sec * 2
        slow = sum(1 for x in latencies if x > slow_threshold)
        benchmark['single_quote'] = {
            'sample_size': sample_size,
            'ok': ok,
            'fail': fail,
            'avg_seconds': round(sum(latencies) / max(1, len(latencies)), 3),
            'min_seconds': round(min(latencies), 3) if latencies else None,
            'max_seconds': round(max(latencies), 3) if latencies else None,
            'slow_gt_seconds': round(slow_threshold, 3),
            'slow_count': slow,
            'source_distribution': dict(src_counter),
        }
        return benchmark


stock_service = StockService()

