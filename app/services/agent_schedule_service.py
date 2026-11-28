"""Agent 定时任务与事件任务（把场景 trigger 从文案变成真实调度）。

改造前 `scenarios.py` 里写着 `daily: 交易日 15:08`、`risk: 每日一次`、
`review: 监控命中后`、`doctor: 回测完成后`，但 `scheduler.py` 只跑监控和通知，
从不调用 `agent_runtime.run`——所有场景全靠用户手动点按钮。这等于「纪律写了，
系统不执行」，是本项目最实质的一处断链。

本服务提供四类任务的统一执行与推送：

    定时：daily（收盘后复盘日报）、risk（开盘前持仓风险巡检）
    事件：review（监控命中后先复核再推送）、doctor（回测不理想时诊断）

设计取舍
--------
- **同一天同一用户同一任务只跑一次**：用 `meta` 表落库标记，重启也不会重复推。
- **推送复用 `NotifyTool`**：不新写一套钉钉签名逻辑（复用铁律）。
- **失败不阻断主流程**：LLM 超时/未配置时，告警照原逻辑推送，复盘任务记录失败原因。
- **开关与时刻都在 `config/decision_rules.json`**：不把口径散进代码。

"""

from __future__ import annotations

import datetime
import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from ..core import decision_rules
from ..core.context import current_user_id, reset_current_user_id, set_current_user_id
from ..core.trading_time import is_trading_day
from ..db import database
from ..tools.monitor.notify_tool import NotifyTool
from .alert_service import in_quiet_hours

logger = logging.getLogger(__name__)

SCHEDULED_TASKS: Dict[str, Dict[str, str]] = {
    "daily": {
        "rule_key": "daily_review_at",
        "default_at": "15:08",
        "scenario": "daily",
        "title": "盘后复盘日报",
        "question": "请为我的持仓做今日盘后复盘：今日概览、异动标的、盈亏归因、明日预案（含具体止损止盈位）。",
    },
    "risk": {
        "rule_key": "risk_check_at",
        "default_at": "09:05",
        "scenario": "risk",
        "title": "持仓风险巡检",
        "question": "请做一次组合风险巡检：集中度、行业暴露、止损距离、盈亏结构、以及情绪目标仓位与实际仓位是否匹配，按紧急程度排序给动作建议。",
    },
}

EVENT_TASKS: Dict[str, Dict[str, str]] = {
    "review": {"scenario": "review", "title": "监控信号复核"},
    "doctor": {"scenario": "doctor", "title": "回测诊断"},
}


class AgentScheduleService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: Deque[Dict[str, Any]] = deque(maxlen=200)
        self._review_cooldown: Dict[int, float] = {}
        self._source_streak: Dict[str, int] = {}
        self._last_results: List[Dict[str, Any]] = []

    # ---------------------------------------------------------------- 配置
    @staticmethod
    def _rules() -> Dict[str, Any]:
        return decision_rules.scheduler_rules()

    @staticmethod
    def _hhmm(key: str, default: str) -> Tuple[int, int]:
        text = str(decision_rules.get(f"scheduler.{key}", default) or default)
        try:
            hour, minute = text.strip().split(":")[:2]
            return max(0, min(23, int(hour))), max(0, min(59, int(minute)))
        except Exception:
            return 15, 8

    # ---------------------------------------------------------------- 队列
    def enqueue(self, user_id: Optional[int], scenario: str, prompt: str,
                payload: Optional[Dict[str, Any]] = None) -> bool:
        """由 API 层投递事件（如回测完成、监控命中）。"""
        if scenario not in EVENT_TASKS:
            return False
        uid = int(user_id or 0)
        with self._lock:
            self._events.append({
                "user_id": uid,
                "scenario": scenario,
                "prompt": prompt,
                "payload": payload or {},
                "at": time.time(),
            })
        return True

    def _drain(self) -> List[Dict[str, Any]]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    # ---------------------------------------------------------------- 去重标记
    @staticmethod
    def _task_key(uid: int, day: str, task: str) -> str:
        return f"agent_task:{uid}:{day}:{task}"

    def _is_done(self, uid: int, day: str, task: str) -> bool:
        try:
            return database.get_meta(self._task_key(uid, day, task)) == "done"
        except Exception:
            return False

    def _mark_done(self, uid: int, day: str, task: str) -> None:
        try:
            database.set_meta(self._task_key(uid, day, task), "done")
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent_task_mark_failed uid=%s task=%s error=%r", uid, task, exc)

    # ---------------------------------------------------------------- 执行
    def due_scheduled(self, now: Optional[datetime.datetime] = None) -> List[Tuple[str, Dict[str, str]]]:
        """返回此刻该跑的定时任务（只按时刻和交易日判断，不查用户）。"""
        now = now or datetime.datetime.now()
        if not is_trading_day(now):
            return []
        due: List[Tuple[str, Dict[str, str]]] = []
        for task, spec in SCHEDULED_TASKS.items():
            hour, minute = self._hhmm(spec["rule_key"], spec["default_at"])
            start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # 窗口 = 触发时刻起 30 分钟（tick 30 秒，不会漏；重启也能补跑）
            if start <= now <= start + datetime.timedelta(minutes=30):
                due.append((task, spec))
        return due

    def run_scenario(self, uid: int, scenario: str, question: str,
                     prompt: str = "") -> Dict[str, Any]:
        """跑一次场景，收集 answer 事件，返回结构化结果。"""
        from ..agent.runtime import agent_runtime

        token = set_current_user_id(int(uid))
        started = time.time()
        try:
            answer: Optional[Dict[str, Any]] = None
            error = ""
            events = 0
            for event in agent_runtime.run(question, scenario=scenario,
                                           context={"prompt": prompt} if prompt else None):
                events += 1
                if event.get("type") == "answer":
                    answer = event.get("answer")
                elif event.get("type") == "error":
                    error = str(event.get("message") or "")
            text = self.format_markdown(answer) if answer else ""
            return {
                "ok": bool(answer), "scenario": scenario, "user_id": uid,
                "answer": answer, "text": text, "error": error,
                "events": events, "elapsed": round(time.time() - started, 1),
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent_scheduled_run_failed uid=%s scenario=%s", uid, scenario)
            return {"ok": False, "scenario": scenario, "user_id": uid,
                    "answer": None, "text": "", "error": f"{type(exc).__name__}: {exc}",
                    "events": 0, "elapsed": round(time.time() - started, 1)}
        finally:
            reset_current_user_id(token)

    @staticmethod
    def format_markdown(answer: Optional[Dict[str, Any]]) -> str:
        """把 answer 结构转成钉钉 markdown（唯一一份格式化逻辑）。"""
        if not isinstance(answer, dict):
            return ""
        lines = [f"### {str(answer.get('headline') or '分析结果')}", ""]
        for item in (answer.get("items") or [])[:8]:
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag") or "").strip()
            title = str(item.get("title") or "").strip()
            body = str(item.get("body") or "").strip()
            action = str(item.get("action") or "").strip()
            head = f"**[{tag}] {title}**" if tag and title else (f"**{title}**" if title else "")
            if head:
                lines.append(head)
            if body:
                lines.append(body)
            if action:
                lines.append(f"> 动作：{action}")
            lines.append("")
        lines.append("---")
        lines.append(f"生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · 由系统自动调度")
        return "\n".join(lines)

    def push(self, uid: int, title: str, text: str) -> Dict[str, Any]:
        """复用 NotifyTool 推送；未开启钉钉时不算失败。"""
        if not text.strip():
            return {"ok": False, "reason": "empty"}
        try:
            from .stock_service import stock_service

            token = set_current_user_id(int(uid))
            try:
                settings = stock_service.get_user_settings()
            finally:
                reset_current_user_id(token)
            # 与监控告警共用同一处免打扰判定；定时报告属自动推送，同样不打扰。
            if in_quiet_hours(settings):
                return {"ok": False, "reason": "quiet_hours"}
            if not bool(settings.get("dingtalk_enabled", False)):
                return {"ok": False, "reason": "dingtalk_disabled"}
            webhook = str(settings.get("dingtalk_webhook_url") or "").strip()
            secret = str(settings.get("dingtalk_secret") or "").strip()
            return NotifyTool().send_dingtalk_markdown(webhook, title, text, secret)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

    # ---------------------------------------------------------------- 调度入口
    def run_scheduled(self, user_ids: List[int], now: Optional[datetime.datetime] = None) -> List[Dict[str, Any]]:
        """定时任务主入口，由 MonitorScheduler 每个 tick 调用。"""
        if not bool(self._rules().get("agent_enabled", True)):
            return []
        now = now or datetime.datetime.now()
        tasks = self.due_scheduled(now)
        if not tasks:
            return []
        day = now.strftime("%Y-%m-%d")
        records: List[Dict[str, Any]] = []
        for uid in user_ids:
            for task, spec in tasks:
                if self._is_done(uid, day, task):
                    continue
                result = self.run_scenario(uid, spec["scenario"], spec["question"])
                pushed = {"ok": False, "reason": "not_attempted"}
                if result.get("ok") and bool(self._rules().get("push_scheduled", True)):
                    pushed = self.push(uid, spec["title"], result["text"])
                if result.get("ok"):
                    self._mark_done(uid, day, task)
                record = {
                    "at": time.time(), "user_id": uid, "task": task,
                    "scenario": spec["scenario"], "ok": bool(result.get("ok")),
                    "error": result.get("error", ""), "elapsed": result.get("elapsed"),
                    "pushed": bool(pushed.get("ok")), "push_reason": pushed.get("reason", ""),
                    "headline": str((result.get("answer") or {}).get("headline") or ""),
                }
                records.append(record)
                self._record(record)
                logger.info("agent_task_done uid=%s task=%s ok=%s pushed=%s", uid, task,
                            record["ok"], record["pushed"])
        return records

    def run_events(self) -> List[Dict[str, Any]]:
        """消费事件队列（回测诊断等）。"""
        if not bool(self._rules().get("agent_enabled", True)):
            return []
        records: List[Dict[str, Any]] = []
        for event in self._drain():
            spec = EVENT_TASKS.get(event["scenario"])
            if not spec:
                continue
            result = self.run_scenario(event["user_id"], spec["scenario"],
                                       spec["title"], str(event.get("prompt") or ""))
            pushed = {"ok": False, "reason": "not_attempted"}
            if result.get("ok") and bool(self._rules().get("push_scheduled", True)):
                pushed = self.push(event["user_id"], spec["title"], result["text"])
            record = {
                "at": time.time(), "user_id": event["user_id"], "task": event["scenario"],
                "scenario": spec["scenario"], "ok": bool(result.get("ok")),
                "error": result.get("error", ""), "elapsed": result.get("elapsed"),
                "pushed": bool(pushed.get("ok")), "push_reason": pushed.get("reason", ""),
                "headline": str((result.get("answer") or {}).get("headline") or ""),
            }
            records.append(record)
            self._record(record)
        return records

    # ---------------------------------------------------------------- 数据源升级告警
    def check_source_health(self, user_ids: List[int], trading_now: bool = True) -> List[Dict[str, Any]]:
        """某个行情源连续多个 tick 处于冷却时，升级推一条钉钉告警（每天最多一次）。

        静默降级是这套系统最危险的行为：报价源挂了却只返回 source='none'，
        用户会以为行情没变。这里把「持续失败」变成一个可见事件。
        """
        rules = self._rules()
        if not trading_now or not bool(rules.get("source_alert_enabled", True)) or not user_ids:
            return []
        try:
            from .stock_service import stock_service

            snapshot = stock_service.source_health_snapshot() or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("source_health_read_failed error=%r", exc)
            return []
        cooling = {name for name, state in snapshot.items()
                   if isinstance(state, dict) and state.get("cooldown")}
        threshold = max(2, int(rules.get("source_failure_streak", 3) or 3))
        with self._lock:
            for name in cooling:
                self._source_streak[name] = self._source_streak.get(name, 0) + 1
            for name in list(self._source_streak):
                if name not in cooling:
                    self._source_streak[name] = 0
            breached = sorted(name for name, count in self._source_streak.items()
                              if count >= threshold)
        if not breached:
            return []
        day = datetime.datetime.now().strftime("%Y-%m-%d")
        if self._is_done(0, day, "source_alert"):
            return []
        text = self.format_markdown({
            "headline": f"行情数据源持续异常：{', '.join(breached)}",
            "items": [{
                "tag": "超阈值", "title": "连续失败已升级",
                "body": "以下数据源连续多个调度周期处于熔断冷却，报价可能退化为 source='none' 或旧快照："
                        + "、".join(breached),
                "action": "检查网络 / 账号登录状态 / 数据源限流，必要时切换主源；期间不要依据旧报价下单",
            }],
        })
        records: List[Dict[str, Any]] = []
        for uid in user_ids:
            pushed = self.push(uid, "行情数据源持续异常", text)
            record = {
                "at": time.time(), "user_id": uid, "task": "source_alert",
                "scenario": "system", "ok": True, "error": "",
                "elapsed": 0, "pushed": bool(pushed.get("ok")),
                "push_reason": pushed.get("reason", ""),
                "headline": "、".join(breached),
            }
            records.append(record)
            self._record(record)
        self._mark_done(0, day, "source_alert")
        return records

    # ---------------------------------------------------------------- 信号复核
    def review_monitor(self, uid: int, triggered: List[Dict[str, Any]]) -> Dict[str, Any]:
        """监控命中后、推送前做一次真假信号复核。

        返回 decision = pass / downgrade / block。LLM 不可用一律 pass（放行），
        绝不让 AI 故障吞掉真实告警。
        """
        if not bool(self._rules().get("review_before_notify", True)):
            return {"decision": "pass", "reason": "disabled"}
        if not triggered:
            return {"decision": "pass", "reason": "no_trigger"}
        last = self._review_cooldown.get(int(uid), 0)
        cooldown = int(decision_rules.get("scheduler.review_cooldown_sec", 1800) or 1800)
        if time.time() - last < cooldown:
            return {"decision": "pass", "reason": "cooldown"}
        self._review_cooldown[int(uid)] = time.time()

        lines = ["以下是本次监控命中的信号，请按复核标准判断真信号还是噪音："]
        for item in triggered[:6]:
            lines.append(
                f"- {item.get('name') or item.get('secID') or '未知'}："
                f"现价 {item.get('price', '-')}，涨跌 {item.get('pct_change', item.get('change_pct', '-'))}，"
                f"信号 {str(item.get('strategy_signal') or item.get('signal') or '规则命中')}"
            )
        result = self.run_scenario(uid, "review", "请复核这批监控信号，给出通过/降级/拦截判定。",
                                   "\n".join(lines))
        if not result.get("ok"):
            return {"decision": "pass", "reason": f"agent_unavailable:{result.get('error', '')[:80]}"}
        items = (result.get("answer") or {}).get("items") or []
        first_tag = str((items[0] if items and isinstance(items[0], dict) else {}).get("tag") or "").strip()
        if "拦截" in first_tag:
            decision = "block"
        elif "降级" in first_tag:
            decision = "downgrade"
        else:
            decision = "pass"
        return {"decision": decision, "headline": str((result.get("answer") or {}).get("headline") or ""),
                "text": result.get("text", ""), "elapsed": result.get("elapsed")}

    # ---------------------------------------------------------------- 状态
    def _record(self, record: Dict[str, Any]) -> None:
        with self._lock:
            self._last_results = ([record] + self._last_results)[:20]

    def status(self) -> Dict[str, Any]:
        with self._lock:
            results = list(self._last_results)
            queued = len(self._events)
            streaks = dict(self._source_streak)
        return {
            "enabled": bool(self._rules().get("agent_enabled", True)),
            "review_before_notify": bool(self._rules().get("review_before_notify", True)),
            "tasks": {k: {"at": str(decision_rules.get(f"scheduler.{v['rule_key']}", v["default_at"])),
                          "scenario": v["scenario"]} for k, v in SCHEDULED_TASKS.items()},
            "queued_events": queued,
            "source_failure_streak": streaks,
            "recent": results,
        }


agent_schedule_service = AgentScheduleService()


def enqueue_backtest_review(result: Dict[str, Any]) -> None:
    """回测完成后由路由层调用：结果不理想才入队诊断，避免每次回测都烧 LLM。"""
    if not isinstance(result, dict) or not result.get("ok"):
        return
    metrics = result.get("metrics") or {}
    ret = metrics.get("total_return_pct")
    mdd = metrics.get("max_drawdown_pct")
    try:
        bad = (ret is not None and float(ret) < 0) or (mdd is not None and float(mdd) > 15.0)
    except (TypeError, ValueError):
        bad = False
    if not bad:
        return
    prompt = (
        f"刚跑完一次回测：标的 {result.get('name') or result.get('code')}，"
        f"策略 {result.get('strategy_name') or '未命名'}，"
        f"总收益 {ret}%，最大回撤 {mdd}%，"
        f"交易次数 {metrics.get('trade_count')}，胜率 {metrics.get('win_rate_pct')}%。\n"
        f"入场表达式：{result.get('entry_expr') or '无'}；出场表达式：{result.get('exit_expr') or '无'}。"
    )
    agent_schedule_service.enqueue(current_user_id(), "doctor", prompt, result)
