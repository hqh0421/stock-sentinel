"""服务端监控调度器。

解决的根因
----------
改造前监控是「前端轮询驱动」的：没人开着页面，就没人调用 /api/stock/monitor，
也就不会推送钉钉。对监控类产品来说这是致命的——最需要报警的时刻（人在开会、
在通勤）恰恰是页面关着的时候。

设计取舍
--------
- **为什么用线程而不是 asyncio 任务**：整条业务链（行情抓取、策略计算、
  钉钉推送）全是同步阻塞 IO。放进事件循环会把整个 API 卡住；放线程池里
  才能与请求天然隔离。
- **tick 30 秒，每用户按自己的 monitor_interval_sec 节流**：tick 要快才能
  及时响应"刚打开开关"，但真正抓行情由用户自己的间隔控制，不会因为 tick
  密就频繁打行情源。
- **只在交易时段跑**：非交易时段行情不变，跑了也没意义，还白耗内存。
- **串行处理用户**：行情源对并发敏感（东财会封禁），宁可一个个来。
"""
from __future__ import annotations

import contextvars
import datetime
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from ..core import decision_rules
from ..core.context import reset_current_user_id, set_current_user_id
from ..core.trading_time import is_trading_now, trading_status
from ..db import database, store

logger = logging.getLogger(__name__)

# 调度口径统一来自 config/decision_rules.json（原为四散的模块级常量）
_SCHED_RULES = decision_rules.scheduler_rules()
TICK_SECONDS = int(_SCHED_RULES.get("tick_seconds", 30))
DEFAULT_INTERVAL_SEC = int(_SCHED_RULES.get("default_interval_sec", 300))
MIN_INTERVAL_SEC = int(_SCHED_RULES.get("min_interval_sec", 60))
MAX_INTERVAL_SEC = int(_SCHED_RULES.get("max_interval_sec", 3600))


class MonitorScheduler:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._started_at: Optional[float] = None
        self._last_tick_at: Optional[float] = None
        self._last_error: str = ""
        self._last_backup_check = 0.0
        # Agent 定时任务/事件任务最近执行记录（供运维面板查看）
        self._last_agent_runs: List[Dict[str, Any]] = []
        # uid -> {"at": ts, "ok": bool, "rows": int, "notify": str, "elapsed": float}
        self._last_runs: Dict[int, Dict[str, Any]] = {}

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="monitor-scheduler", daemon=True)
            self._thread.start()
            self._started_at = time.time()
        logger.info("监控调度器已启动 tick=%ss", TICK_SECONDS)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        logger.info("监控调度器已停止")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---------------------------------------------------------------- 主循环

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("调度器 tick 异常")
            # 分段等待，stop() 能及时生效
            self._stop.wait(TICK_SECONDS)

    def _tick(self) -> None:
        self._last_tick_at = time.time()
        self._maybe_daily_backup()
        # Agent 定时任务必须在交易时段判断之前：盘后复盘 15:08、开盘前巡检 09:05
        # 都不在 is_trading_now() 的 9:30-11:30 / 13:00-15:00 区间里。
        trading_now = is_trading_now()
        self._maybe_agent_tasks(trading_now)
        if not trading_now:
            return
        try:
            user_ids = store.list_active_user_ids()
        except Exception as exc:  # noqa: BLE001
            logger.warning("调度器读取用户失败 error=%r", exc)
            return
        for uid in user_ids:
            if self._stop.is_set():
                return
            if not self._due(uid):
                continue
            self._run_user(uid, trigger="auto")

    def _maybe_agent_tasks(self, trading_now: bool = True) -> None:
        """消费 Agent 事件队列 + 定时任务 + 数据源持续失败升级；失败不影响监控主链路。"""
        from .agent_schedule_service import agent_schedule_service

        try:
            records = agent_schedule_service.run_events()
            uids = store.list_active_user_ids()
            records += agent_schedule_service.run_scheduled(uids)
            records += agent_schedule_service.check_source_health(uids, trading_now=trading_now)
            if records:
                self._last_agent_runs = (records + self._last_agent_runs)[:20]
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent_scheduled_tick_failed error=%r", exc)

    def _due(self, uid: int) -> bool:
        settings = store.get_settings(uid)
        if not bool(settings.get("monitor_enabled", True)):
            return False
        try:
            interval = int(settings.get("monitor_interval_sec") or DEFAULT_INTERVAL_SEC)
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL_SEC
        interval = max(MIN_INTERVAL_SEC, min(MAX_INTERVAL_SEC, interval))
        last = self._last_runs.get(uid, {}).get("at") or 0
        return (time.time() - float(last)) >= interval

    def _run_user(self, uid: int, trigger: str = "auto") -> Dict[str, Any]:
        from .stock_service import stock_service

        started = time.time()
        review: Dict[str, Any] = {"decision": "pass", "reason": "not_checked"}
        token = set_current_user_id(int(uid))
        try:
            data = stock_service.monitor_favorites(force_refresh=False, respect_trading_hours=True)
            busy = any(item.get("monitor_busy") for item in data)
            if busy:
                result: Dict[str, Any] = {"ok": False, "sent": False, "reason": "monitor_busy"}
            elif not data:
                result = {"ok": True, "sent": False, "reason": "empty_watchlist"}
            else:
                # 监控命中后、推送前先做一次真假信号复核（review 场景 trigger 落地）。
                # AI 不可用/被冷却时一律放行，绝不让模型故障吞掉真实告警。
                triggered = [item for item in data
                             if item.get("strategy_triggered") or item.get("alert_triggered")]
                if triggered:
                    from .agent_schedule_service import agent_schedule_service

                    review = agent_schedule_service.review_monitor(uid, triggered)
                if review.get("decision") == "block":
                    result = {"ok": True, "sent": False, "reason": "agent_review_blocked"}
                else:
                    result = stock_service.notify_monitor_results(data, mode="auto")
            record = {
                "at": time.time(),
                "ok": bool(result.get("ok", True)) and not busy,
                "rows": len(data),
                "notify": str(result.get("reason") or ("sent" if result.get("sent") else "-")),
                "elapsed": round(time.time() - started, 2),
                "trigger": trigger,
                "agent_review": review.get("decision"),
                "review_headline": str(review.get("headline") or ""),
            }
            self._last_runs[int(uid)] = record
            logger.info(
                "调度器完成 user=%s trigger=%s rows=%s notify=%s elapsed=%.2fs",
                uid, trigger, len(data), record["notify"], record["elapsed"],
            )
            return record
        except Exception as exc:  # noqa: BLE001
            self._last_runs[int(uid)] = {
                "at": time.time(),
                "ok": False,
                "rows": 0,
                "notify": f"error: {type(exc).__name__}",
                "elapsed": round(time.time() - started, 2),
                "trigger": trigger,
            }
            logger.exception("调度器执行失败 user=%s", uid)
            return self._last_runs[int(uid)]
        finally:
            reset_current_user_id(token)

    # ---------------------------------------------------------------- 每日备份

    def _maybe_daily_backup(self) -> None:
        # 每小时最多检查一次，避免每 30 秒都去读一次 meta
        if time.time() - self._last_backup_check < 3600:
            return
        self._last_backup_check = time.time()
        try:
            from .backup_service import ensure_daily_backup

            info = ensure_daily_backup()
            if info:
                logger.info("每日备份已生成 file=%s", info.get("name"))
            store.purge_audit_logs(keep_days=int(decision_rules.get("scheduler.audit_keep_days", 90)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("每日备份检查失败 error=%r", exc)

    # ---------------------------------------------------------------- 状态

    def status(self) -> Dict[str, Any]:
        now = time.time()
        users: List[Dict[str, Any]] = []
        try:
            for uid in store.list_active_user_ids():
                settings = store.get_settings(uid)
                try:
                    interval = int(settings.get("monitor_interval_sec") or DEFAULT_INTERVAL_SEC)
                except (TypeError, ValueError):
                    interval = DEFAULT_INTERVAL_SEC
                interval = max(MIN_INTERVAL_SEC, min(MAX_INTERVAL_SEC, interval))
                last = self._last_runs.get(uid, {})
                user = store.get_user_by_id(uid) or {}
                users.append(
                    {
                        "user_id": uid,
                        "username": user.get("username", ""),
                        "enabled": bool(settings.get("monitor_enabled", True)),
                        "interval_sec": interval,
                        "last_run_at": (
                            datetime.datetime.fromtimestamp(last["at"]).strftime("%Y-%m-%d %H:%M:%S")
                            if last.get("at") else ""
                        ),
                        "last_run_ok": last.get("ok"),
                        "last_rows": last.get("rows"),
                        "last_notify": last.get("notify", ""),
                        "last_elapsed": last.get("elapsed"),
                        "last_agent_review": last.get("agent_review", ""),
                        "next_run_in": max(0, int(interval - (now - float(last.get("at") or 0)))) if last.get("at") else 0,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("调度器状态读取失败 error=%r", exc)

        return {
            "running": self.running,
            "tick_seconds": TICK_SECONDS,
            "trading_now": is_trading_now(),
            "trading_status": trading_status(),
            "started_at": (
                datetime.datetime.fromtimestamp(self._started_at).strftime("%Y-%m-%d %H:%M:%S")
                if self._started_at else ""
            ),
            "last_tick_at": (
                datetime.datetime.fromtimestamp(self._last_tick_at).strftime("%Y-%m-%d %H:%M:%S")
                if self._last_tick_at else ""
            ),
            "last_error": self._last_error,
            "users": users,
            "last_backup_date": database.get_meta("last_backup_date") or "",
            "agent": self._agent_status(),
            "agent_recent": self._last_agent_runs[:10],
        }

    @staticmethod
    def _agent_status() -> Dict[str, Any]:
        from .agent_schedule_service import agent_schedule_service

        try:
            return agent_schedule_service.status()
        except Exception as exc:  # noqa: BLE001
            return {"enabled": False, "error": f"{type(exc).__name__}: {exc}"}

    def run_now(self, user_id: Optional[int] = None) -> Dict[str, Any]:
        """手动触发一次（忽略交易时段，用于验证配置是否通）。"""
        from .stock_service import stock_service

        uid = int(user_id) if user_id else None
        if uid is None:
            uid = (store.list_active_user_ids() or [None])[0]
        if not uid:
            return {"ok": False, "reason": "no_user"}
        token = set_current_user_id(uid)
        started = time.time()
        try:
            data = stock_service.monitor_favorites(force_refresh=True, respect_trading_hours=False)
            busy = any(item.get("monitor_busy") for item in data)
            notify = (
                {"ok": False, "sent": False, "reason": "monitor_busy"}
                if busy
                else stock_service.notify_monitor_results(data, mode="auto")
            )
            record = {
                "at": time.time(),
                "ok": not busy,
                "rows": len(data),
                "notify": str(notify.get("reason") or ("sent" if notify.get("sent") else "-")),
                "elapsed": round(time.time() - started, 2),
                "trigger": "manual",
            }
            self._last_runs[uid] = record
            return {
                "ok": not busy,
                "user_id": uid,
                "rows": len(data),
                "elapsed": record["elapsed"],
                "notify": notify,
                "triggered": [
                    {
                        "secID": item.get("secID"),
                        "name": item.get("name"),
                        "signal": item.get("strategy_signal"),
                        "price": item.get("price"),
                    }
                    for item in data
                    if item.get("strategy_triggered")
                ],
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("手动触发调度失败 user=%s", uid)
            return {"ok": False, "user_id": uid, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            reset_current_user_id(token)


scheduler = MonitorScheduler()
