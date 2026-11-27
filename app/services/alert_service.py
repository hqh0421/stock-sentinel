import datetime
import logging
import time
from typing import Any, Dict, List, Optional

from ..core import decision_rules
from ..core.trading_time import is_trading_now, trading_status

logger = logging.getLogger(__name__)

# 买入方向的策略信号：账户满仓时这些整条不推
_BUY_SIGNALS = {"buy", "position_add", "value_add"}
# 卖出方向的策略信号：任何情况下都要推
_SELL_SIGNALS = {"sell", "take_profit", "stop_loss", "position_reduce"}


class AlertService:
    def __init__(self, host: Any) -> None:
        self.host = host

    def _stage_text(self, value: Any) -> str:
        mapping = {
            "unknown": "未知",
            "sideways": "震荡",
            "uptrend": "上升趋势",
            "downtrend": "下降趋势",
            "accumulation": "底部吸筹",
            "washout": "洗盘",
            "distribution": "顶部派发",
        }
        key = str(value or "").strip().lower()
        return mapping.get(key, key or "-")

    def _action_text(self, value: Any) -> str:
        mapping = {
            "watch": "观望",
            "hold": "持有",
            "hold_add": "持有/加仓",
            "hold_watch": "持有/观察",
            "buy": "买入",
            "buy_dip": "低吸",
            "probe_buy": "试探买入",
            "trend_follow": "顺势跟进",
            "accumulate": "逢低布局",
            "reduce": "减仓",
            "stop_loss": "止损",
            "exit": "清仓",
            "focus": "重点关注",
        }
        key = str(value or "").strip().lower()
        return mapping.get(key, key or "-")

    def _signal_text(self, value: Any) -> str:
        mapping = {
            "buy": "买入",
            "sell": "卖出",
            "stop_loss": "止损",
            "take_profit": "止盈",
            "position_add": "加仓",
            "position_reduce": "减仓",
            "value_add": "低吸加仓",
            "time_stop": "时间止损",
        }
        key = str(value or "").strip().lower()
        return mapping.get(key, key or "-")

    def _price_text(self, item: Dict[str, Any]) -> str:
        price = item.get("price")
        if price is None and isinstance(item.get("spot"), dict):
            price = item.get("spot", {}).get("price")
        if price is None:
            return "-"
        try:
            return f"{float(price):.2f}"
        except Exception:
            return str(price)

    def _pct_text(self, value: Any) -> str:
        try:
            if value is None:
                return "-"
            return f"{float(value):.2f}%"
        except Exception:
            return str(value or "-")

    def _position_plan_text(self, item: Dict[str, Any]) -> str:
        plan = item.get("position_plan") if isinstance(item.get("position_plan"), dict) else {}
        if not plan:
            return ""
        # 弹药门控：系统判定不建议买入时，推送里也不能出现买入建议
        _plan_reason = str(plan.get("suggestion_reason") or "").strip().lower()
        if _plan_reason in {"no_ammo", "account_full", "account_tight"}:
            ammo = plan.get("available_cash")
            try:
                ammo_text = f"{float(ammo):,.0f} 元"
            except Exception:
                ammo_text = "-"
            if _plan_reason == "account_full":
                return (f"账户已满仓（可用资金约 {ammo_text}），不推买入；"
                        f"如需买入请先减仓腾出资金，或到设置页更新「账户可用资金」")
            if _plan_reason == "account_tight":
                return (f"账户弹药偏紧（可用资金约 {ammo_text}），只够零星补仓；"
                        f"建议优先减仓换仓，不建议新开仓")
            return f"可用弹药不足（约 {ammo_text}），不建议买入；如已补充资金请到系统设置更新可用弹药。"
        suggestion = str(plan.get("suggestion") or "").strip().lower()
        current_pct = plan.get("current_pct")
        target_pct = plan.get("target_pct")
        max_pct = plan.get("max_pct")
        price = plan.get("suggest_price")
        shares = int(plan.get("suggest_shares") or 0)
        amount = plan.get("suggest_amount")
        try:
            current_pct_text = f"{float(current_pct):.2f}%"
        except Exception:
            current_pct_text = "-"
        try:
            target_pct_text = f"{float(target_pct):.2f}%"
        except Exception:
            target_pct_text = "-"
        try:
            max_pct_text = f"{float(max_pct):.2f}%"
        except Exception:
            max_pct_text = "-"
        try:
            price_text = f"{float(price):.2f}" if price is not None else "-"
        except Exception:
            price_text = str(price or "-")
        try:
            amount_text = f"{float(amount):.2f}" if amount is not None else "-"
        except Exception:
            amount_text = str(amount or "-")
        preference_text = "左侧信号偏好" if str(plan.get("add_signal_preference") or "").strip().lower() == "left" else "右侧信号偏好"
        suggestion_reason = str(plan.get("suggestion_reason") or "").strip().lower()

        if suggestion == "reduce":
            if suggestion_reason == "sell_signal":
                if shares > 0:
                    return f"{preference_text}，出现卖出信号，当前仓位 {current_pct_text}，策略上限 {max_pct_text}，建议在 {price_text} 附近减仓 {shares} 股，约 {amount_text} 元，目标仓位 {target_pct_text}。"
                return f"{preference_text}，出现卖出信号，当前仓位 {current_pct_text}，策略上限 {max_pct_text}，但按一手 100 股计算暂无可减仓手数。"
            if shares > 0:
                return f"{preference_text}，当前仓位 {current_pct_text}，高于上限 {max_pct_text}，建议在 {price_text} 附近减仓 {shares} 股，约 {amount_text} 元，目标仓位 {target_pct_text}。"
            return f"{preference_text}，当前仓位 {current_pct_text}，高于上限 {max_pct_text}，但按一手 100 股计算暂无可减仓手数。"
        if suggestion in {"buy", "add"}:
            if shares > 0:
                verb = "建仓" if suggestion == "buy" else "加仓"
                return f"{preference_text}，仓位 {current_pct_text}，低于目标 {target_pct_text}，建议在 {price_text} 附近{verb} {shares} 股，约 {amount_text} 元。"
            return f"{preference_text}，仓位 {current_pct_text}，低于目标 {target_pct_text}，但按一手 100 股计算暂无可买入手数。"
        if suggestion == "hold":
            return f"{preference_text}，仓位 {current_pct_text}，已在当前策略范围内。"
        if suggestion == "set_capital":
            return "请先设置本金，系统才能计算仓位建议。"
        return ""

    def _position_plan_text_for_signal(self, item: Dict[str, Any], signal: str) -> str:
        plan = item.get("position_plan") if isinstance(item.get("position_plan"), dict) else {}
        suggestion = str(plan.get("suggestion") or "").strip().lower()
        suggestion_reason = str(plan.get("suggestion_reason") or "").strip().lower()
        signal = str(signal or "").strip().lower()
        buy_signals = {"buy", "position_add", "value_add"}
        sell_signals = {"sell", "take_profit", "stop_loss", "position_reduce"}
        if signal in buy_signals and suggestion == "reduce":
            return ""
        if signal in sell_signals and (
            suggestion in {"buy", "add"}
            or suggestion_reason in {"no_ammo", "account_full", "account_tight"}
        ):
            return ""
        return self._position_plan_text(item)

    def _strategy_line(self, signal_text: str, signal: str, now_text: str):
        """生成策略信号推送行；弹药不足时不输出买入类动作建议。"""
        buy_signals = {"buy", "position_add", "value_add"}

        def _line(it: Dict[str, Any]) -> str:
            plan = it.get("position_plan") if isinstance(it.get("position_plan"), dict) else {}
            _reason = str(plan.get("suggestion_reason") or "").strip().lower()
            no_ammo = _reason in {"no_ammo", "account_full", "account_tight"}
            action_text = self._action_text(it.get("action"))
            if no_ammo and signal in buy_signals:
                action_text += "（账户满仓/弹药不足，不建议执行买入）"
            plan_text = self._position_plan_text_for_signal(it, signal)
            # 时间止损要带依据，否则推送只剩「减仓」两字，没人会执行
            ts = it.get("time_stop") if isinstance(it.get("time_stop"), dict) else {}
            ts_text = ""
            if ts.get("triggered") and ts.get("overridden"):
                # 已豁免的那一笔，事实还是「触发」（报告里保留，方便到期复盘），
                # 但不能在别人的告警行里写成「浮亏 X%、近期未创新高」——那会被读成
                # 时间止损也在报警。只留一句豁免到期日，提醒续期或重估。
                ov = ts.get("override") or {}
                ts_text = f"，时间止损已豁免至 {ov.get('expires_on') or '—'}"
            elif ts.get("triggered"):
                ts_text = f"，已持有 {ts.get('holding_days')} 天"
                pnl_pct = ts.get("pnl_pct")
                if pnl_pct is not None:
                    ts_text += f"、浮亏 {abs(float(pnl_pct)):.1f}%"
                if ts.get("drawdown_from_peak_pct") is not None:
                    ts_text += f"、距持有期高点 {float(ts['drawdown_from_peak_pct']):.1f}%"
                ts_text += "，近期未创新高"
            return (
                f"- {it.get('name') or it.get('secID') or it.get('code') or '-'}："
                f"策略信号 {signal_text}，阶段 {self._stage_text(it.get('market_stage'))}，动作 {action_text}，"
                f"现价 {self._price_text(it)}，涨幅 {self._pct_text(it.get('pct_chg'))}，时间 {now_text}"
                + ts_text
                + (f"，{plan_text}" if plan_text else "")
            )

        return _line

    def _monitor_pct_change(self, item: Dict[str, Any]) -> Optional[float]:
        spot = item.get("spot") if isinstance(item.get("spot"), dict) else {}
        return self.host._to_float(item.get("pctChg") or item.get("pct_chg") or spot.get("pct_chg") or spot.get("pctChg"))

    def _normalize_levels(self, value: Any, fallback: List[float]) -> List[float]:
        if isinstance(value, str):
            raw_items = [part.strip() for part in value.split(",")]
        elif isinstance(value, list):
            raw_items = value
        else:
            raw_items = fallback
        levels = []
        for item in raw_items:
            try:
                num = abs(float(item))
            except Exception:
                continue
            if num > 0:
                levels.append(round(num, 2))
        levels = sorted(set(levels))
        return levels or fallback

    def _matched_pct_level(self, pct_chg: Optional[float], levels: List[float], direction: str) -> Optional[float]:
        if pct_chg is None:
            return None
        if direction == "up":
            matched = [level for level in levels if pct_chg >= level]
        else:
            matched = [level for level in levels if pct_chg <= -level]
        return max(matched) if matched else None

    def _reset_pct_alert_state(self, code: str, direction: str) -> None:
        prefix = f"pct_change_{direction}:{code}:"
        for key in list(self.host._alert_last_sent.keys()):
            if str(key).startswith(prefix):
                self.host._alert_last_sent.pop(key, None)

    def _pct_alert_key(self, direction: str, item: Dict[str, Any], level: Any) -> str:
        code = item.get("secID") or item.get("code") or "-"
        return f"pct_change_{direction}:{code}:{level}"

    def get_alert_rules(self) -> Dict[str, Any]:
        defaults = {
            "dingtalk": {
                "enabled": True,
                "max_items": 20,
                "auto_dedupe_seconds": 1800,
                "rules": {
                    "pct_change_up": {"enabled": True, "threshold": 3.0, "levels": [3.0, 5.0, 8.0], "reset_below": 2.0, "operator": ">="},
                    "pct_change_down": {"enabled": True, "threshold": -3.0, "levels": [3.0, 5.0, 8.0], "reset_below": 2.0, "operator": "<="},
                    "strategy_signal": {"enabled": True},
                    "sentiment_position": {
                        "enabled": True,
                        "phases": ["euphoria", "ebb", "collapse"],
                        "cooldown_sec": 4 * 3600,
                        "gap_threshold": 0.05,
                    },
                },
            }
        }
        data = self.host.load_alert_rules_raw(defaults)
        if not isinstance(data, dict):
            return defaults
        merged = defaults.copy()
        merged["dingtalk"] = {**defaults["dingtalk"], **data.get("dingtalk", {})}
        merged["dingtalk"]["rules"] = {
            **defaults["dingtalk"]["rules"],
            **(data.get("dingtalk", {}).get("rules", {}) if isinstance(data.get("dingtalk", {}).get("rules", {}), dict) else {}),
        }
        return merged

    def save_alert_rules(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        defaults = self.get_alert_rules()
        if not isinstance(payload, dict):
            raise ValueError("alert rules payload must be an object")
        dingtalk = payload.get("dingtalk") if isinstance(payload.get("dingtalk"), dict) else {}
        merged = {
            "dingtalk": {
                "enabled": bool(dingtalk.get("enabled", defaults["dingtalk"].get("enabled", True))),
                "max_items": int(dingtalk.get("max_items") or defaults["dingtalk"].get("max_items", 20) or 20),
                "auto_dedupe_seconds": int(dingtalk.get("auto_dedupe_seconds") or defaults["dingtalk"].get("auto_dedupe_seconds", 1800) or 1800),
                "rules": {
                    "pct_change_up": {
                        "enabled": bool(((dingtalk.get("rules") or {}).get("pct_change_up") or {}).get("enabled", True)),
                        "threshold": float((((dingtalk.get("rules") or {}).get("pct_change_up") or {}).get("threshold", 3.0)) or 3.0),
                        "levels": self._normalize_levels(((dingtalk.get("rules") or {}).get("pct_change_up") or {}).get("levels"), [3.0, 5.0, 8.0]),
                        "reset_below": float((((dingtalk.get("rules") or {}).get("pct_change_up") or {}).get("reset_below", 2.0)) or 2.0),
                        "operator": str((((dingtalk.get("rules") or {}).get("pct_change_up") or {}).get("operator", ">=")) or ">=").strip() or ">=",
                    },
                    "pct_change_down": {
                        "enabled": bool(((dingtalk.get("rules") or {}).get("pct_change_down") or {}).get("enabled", True)),
                        "threshold": float((((dingtalk.get("rules") or {}).get("pct_change_down") or {}).get("threshold", -3.0)) or -3.0),
                        "levels": self._normalize_levels(((dingtalk.get("rules") or {}).get("pct_change_down") or {}).get("levels"), [3.0, 5.0, 8.0]),
                        "reset_below": float((((dingtalk.get("rules") or {}).get("pct_change_down") or {}).get("reset_below", 2.0)) or 2.0),
                        "operator": str((((dingtalk.get("rules") or {}).get("pct_change_down") or {}).get("operator", "<=")) or "<=").strip() or "<=",
                    },
                    "strategy_signal": {
                        "enabled": bool(((dingtalk.get("rules") or {}).get("strategy_signal") or {}).get("enabled", True)),
                    },
                    "sentiment_position": {
                        "enabled": bool(((dingtalk.get("rules") or {}).get("sentiment_position") or {}).get("enabled", True)),
                        # 只在这些阶段推仓位提醒（用户：情绪疯狂时要卖出）
                        "phases": list(
                            ((dingtalk.get("rules") or {}).get("sentiment_position") or {}).get("phases")
                            or ["euphoria", "ebb", "collapse"]
                        ),
                        # 同一阶段最短重复间隔（秒），避免每次扫描都推一遍
                        "cooldown_sec": int(
                            ((dingtalk.get("rules") or {}).get("sentiment_position") or {}).get("cooldown_sec")
                            or 4 * 3600
                        ),
                        # 仓位偏离目标超过多少才推（0.05 = 5 个百分点）
                        "gap_threshold": float(
                            ((dingtalk.get("rules") or {}).get("sentiment_position") or {}).get("gap_threshold")
                            or 0.05
                        ),
                    },
                },
            }
        }
        self.host.save_alert_rules_raw(merged)
        return merged

    def _log_alert(
        self,
        channel: str,
        title: str,
        content: str,
        item_count: int,
        result: Dict[str, Any],
        source: str,
    ) -> None:
        """把每次推送写进 alert_log：推过什么、成没成，事后能查。

        历史是排障的唯一依据——「昨晚为什么没收到提醒」这类问题，没有落库
        就只能靠猜。写库失败不能影响推送主流程，所以整个函数吞异常。
        """
        try:
            uid = self.host._uid_safe()
            if uid is None:
                return
            from ..db import store

            ok = bool((result or {}).get("ok"))
            error = ""
            if not ok:
                error = str(
                    (result or {}).get("error")
                    or (result or {}).get("message")
                    or (result or {}).get("errcode")
                    or ""
                )[:500]
            store.add_alert_log(
                user_id=int(uid),
                created_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                channel=channel,
                title=title,
                content=content,
                item_count=item_count,
                ok=ok,
                error=error,
                source=source,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入告警历史失败 error=%r", exc)

    # ------------------------------------------------------------------
    # 情绪仓位提醒
    # ------------------------------------------------------------------

    def _notify_sentiment_position(
        self,
        account: Dict[str, Any],
        webhook_url: str,
        dingtalk_secret: str,
        mode: str,
        force_send: bool,
        now_text: str,
    ) -> Dict[str, Any]:
        """情绪转关键阶段时，主动推一条仓位纪律提醒。

        用户理念：**「市场情绪疯狂的时候要卖出」**——但等用户想起来看的时候
        往往已经晚了一步。这条提醒的作用是把「情绪阶段 → 目标仓位 →
        实际仓位差距」直接推到眼前，让纪律变成可执行动作。

        触发条件（三者同时满足）：
          ① 情绪阶段在配置的名单里（默认亢奋/退潮/崩塌）；
          ② 实际仓位偏离目标仓位区间超过阈值；
          ③ 距离上次同类提醒超过冷却时间（避免每次扫描都推）。

        任何一步不满足都不发——**宁可不提醒，也不要刷屏**。
        """
        empty = {"ok": False, "sent": False, "reason": "no_rule"}
        if not webhook_url:
            return empty

        try:
            rules = self.get_alert_rules()
        except Exception as exc:  # noqa: BLE001
            logger.warning("sentiment_alert_rules_failed error=%r", exc)
            return empty

        dingtalk_rules = rules.get("dingtalk", {}) if isinstance(rules.get("dingtalk"), dict) else {}
        rule = (dingtalk_rules.get("rules") or {}).get("sentiment_position") or {}
        if not rule.get("enabled", True):
            return {"ok": False, "sent": False, "reason": "sentiment_position_disabled"}

        try:
            from .sentiment_position_service import build_sentiment_position_policy

            sentiment = self.host.get_market_sentiment() or {}
            market = self.host.get_market_regime() or {}
            policy = build_sentiment_position_policy(
                sentiment=sentiment,
                market=market,
                account={"position_ratio": account.get("position_ratio")},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sentiment_policy_failed error=%r", exc)
            return empty

        if not policy.get("ok"):
            return empty

        phase = str(policy.get("phase") or "unknown")
        phases = [str(p) for p in (rule.get("phases") or [])]
        if phase not in phases:
            return {"ok": False, "sent": False, "reason": "phase_not_in_list"}

        actual = policy.get("actual_position")
        pos_min = float(policy.get("target_position_min") or 0)
        pos_max = float(policy.get("target_position_max") or 0)
        gap = 0.0
        if actual is not None:
            actual_f = float(actual)
            if actual_f < pos_min:
                gap = pos_min - actual_f
            elif actual_f > pos_max:
                gap = actual_f - pos_max
        gap_threshold = float(rule.get("gap_threshold") or 0.05)
        if gap < gap_threshold:
            return {"ok": False, "sent": False, "reason": "position_in_range", "gap": round(gap, 4)}

        # 冷却：同一阶段 4 小时内不重复推
        cooldown = int(rule.get("cooldown_sec") or 4 * 3600)
        key = f"sentiment_position:{phase}"
        now_ts = time.time()
        last_sent = float(self.host._alert_last_sent.get(key, 0) or 0)
        if mode == "auto" and not force_send and (now_ts - last_sent) < cooldown:
            return {"ok": False, "sent": False, "reason": "cooldown", "remain_sec": int(cooldown - (now_ts - last_sent))}

        title = f"市场情绪·{policy.get('regime_label') or phase}｜目标仓位调整"
        lines = [
            f"### {title}",
            f"- 时间: {now_text}",
            f"- 情绪阶段: {policy.get('regime_label')}（温度 {float(policy.get('temperature') or 0):.0%}）",
            f"- 目标仓位: {pos_min:.0%} ~ {pos_max:.0%}"
            + (f"，当前实际仓位 {float(actual):.0%}" if actual is not None else ""),
            f"- 动作: {policy.get('gap_action')}",
            f"- 纪律: {policy.get('action_text')}",
        ]
        pace = str(policy.get("pace") or "").strip()
        if pace:
            lines.append(f"- 节奏: {pace}")
        if not policy.get("allow_new_buy", True):
            lines.append("- ⚠️ 当前阶段禁止新增买入（不博傻硬底线）")
        cycle_text = ""
        try:
            from .sentiment_history_service import sentiment_history_service

            history = sentiment_history_service.get_history_view(current=sentiment)
            cycle = history.get("cycle") or {}
            cycle_text = str(cycle.get("cycle_text") or "")
            if cycle_text:
                lines.append(f"- 周期位置: {cycle_text}")
        except Exception as exc:  # noqa: BLE001
            logger.debug("sentiment_cycle_failed error=%r", exc)

        body = "\n".join(lines)
        result = self.host._notify_tool.send_dingtalk_markdown(
            webhook_url, title, body, secret=dingtalk_secret,
        )
        self._log_alert("sentiment_position", title, body, 1, result, mode)
        if result.get("ok"):
            self.host._alert_last_sent[key] = now_ts
        return {
            "ok": bool(result.get("ok")),
            "sent": bool(result.get("ok")),
            "title": title,
            "result": result,
            "phase": phase,
            "gap": round(gap, 4),
        }

    # ------------------------------------------------------------------
    # 四池偏离提醒
    # ------------------------------------------------------------------

    def _notify_pool_drift(self, webhook_url: str, dingtalk_secret: str,
                           mode: str, force_send: bool) -> Dict[str, Any]:
        """四池实际占比偏离目标超过带宽时主动推一条。

        对标 Passiv 的 drift 通知，但**复用同一套 webhook / 冷却 / alert_log**，
        不新建推送通道。默认关闭：四池页与每日复盘已能看到偏离，是否额外推送
        由用户决定，避免和情绪仓位提醒叠在一起刷屏。
        """
        empty = {"ok": False, "sent": False}
        if not bool(decision_rules.get("monitor.pool_drift_alert_enabled", False)):
            return {**empty, "reason": "disabled"}
        if not webhook_url:
            return {**empty, "reason": "missing_webhook"}

        try:
            from . import allocation_service as alloc

            items = self.host.get_favorites_list(include_quotes=True) or []
            settings = self.host.get_user_settings() or {}
            holdings, total = alloc.holdings_from_items(items, settings)
            if total <= 0:
                return {**empty, "reason": "no_total_capital"}
            allocation = alloc.compute_allocation(holdings, total)
            if not allocation.get("ok"):
                return {**empty, "reason": "allocation_unavailable"}
            plan = alloc.rebalance_plan(allocation)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pool_drift_compute_failed error=%r", exc)
            return {**empty, "reason": "compute_failed"}

        if not plan:
            return {**empty, "reason": "in_band"}

        pools = sorted(str(p.get("pool") or "") for p in plan)
        key = "pool_drift:" + ",".join(pools)
        cooldown = int(decision_rules.get("monitor.pool_drift_cooldown_sec", 4 * 3600) or 4 * 3600)
        now_ts = time.time()
        last_sent = float(self.host._alert_last_sent.get(key, 0) or 0)
        if mode == "auto" and not force_send and (now_ts - last_sent) < cooldown:
            return {**empty, "reason": "cooldown",
                    "remain_sec": int(cooldown - (now_ts - last_sent))}

        now_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        title = "四池偏离提醒"
        cash = allocation.get("cash") or {}
        lines = [
            f"### {title}",
            f"- 时间: {now_text}",
            f"- 现金: 实际 {float(cash.get('actual_pct') or 0):.2f}%"
            f"（目标 {float(cash.get('target_pct') or 0):.0f}%，下限 {float(cash.get('min_pct') or 0):.0f}%）",
        ]
        for item in plan[:4]:
            action = "减持" if str(item.get("action")) == "trim" else "增配"
            lines.append(
                f"- {item.get('label')}：偏离 {float(item.get('diff_pp') or 0):+.2f}pp，"
                f"{action}约 {float(item.get('amount') or 0):,.0f} 元"
            )
        lines.append("- 约束: 资金只在本池内调拨，不跨池挪用；执行仍走两道确认")

        body = "\n".join(lines)
        result = self.host._notify_tool.send_dingtalk_markdown(
            webhook_url, title, body, secret=dingtalk_secret,
        )
        self._log_alert("pool_drift", title, body, len(plan), result, mode)
        if result.get("ok"):
            self.host._alert_last_sent[key] = now_ts
        return {
            "ok": bool(result.get("ok")),
            "sent": bool(result.get("ok")),
            "title": title,
            "count": len(plan),
            "pools": pools,
            "result": result,
        }

    def notify_monitor_results(self, monitor_data: List[Dict[str, Any]], force_send: bool = False, mode: str = "auto") -> Dict[str, Any]:
        settings = self.host.get_user_settings()
        alert_rules = self.get_alert_rules()
        dingtalk_rules = alert_rules.get("dingtalk", {}) if isinstance(alert_rules.get("dingtalk"), dict) else {}
        rules = dingtalk_rules.get("rules", {}) if isinstance(dingtalk_rules.get("rules"), dict) else {}
        pct_rule = rules.get("pct_change_up", {}) if isinstance(rules.get("pct_change_up"), dict) else {}
        pct_down_rule = rules.get("pct_change_down", {}) if isinstance(rules.get("pct_change_down"), dict) else {}
        strategy_rule = rules.get("strategy_signal", {}) if isinstance(rules.get("strategy_signal"), dict) else {}
        webhook_url = str(settings.get("dingtalk_webhook_url") or "").strip()
        dingtalk_secret = str(settings.get("dingtalk_secret") or "").strip()
        notify_enabled = bool(settings.get("dingtalk_enabled", False))
        monitor_enabled = bool(settings.get("monitor_enabled", True))
        pct_levels = self._normalize_levels(pct_rule.get("levels"), [abs(float(pct_rule.get("threshold", 3.0) or 3.0))])
        pct_down_levels = self._normalize_levels(pct_down_rule.get("levels"), [abs(float(pct_down_rule.get("threshold", -3.0) or -3.0))])
        pct_reset_below = abs(float(pct_rule.get("reset_below", 2.0) or 2.0))
        pct_down_reset_below = abs(float(pct_down_rule.get("reset_below", 2.0) or 2.0))
        pct_rule_enabled = bool(pct_rule.get("enabled", True))
        pct_down_rule_enabled = bool(pct_down_rule.get("enabled", True))
        strategy_rule_enabled = bool(strategy_rule.get("enabled", True))
        max_items = int(dingtalk_rules.get("max_items", 20) or 20)
        auto_dedupe_seconds = int(dingtalk_rules.get("auto_dedupe_seconds", 1800) or 1800)
        mode = "manual" if mode == "manual" else "auto"

        if not force_send and not is_trading_now():
            logger.info("非交易时段，跳过钉钉提醒（%s）", trading_status())
            return {"ok": False, "sent": False, "reason": "non_trading_hours", "mode": mode}

        if not webhook_url:
            return {"ok": False, "sent": False, "reason": "missing_webhook"}
        if (not notify_enabled or not bool(dingtalk_rules.get("enabled", True))) and not force_send:
            return {"ok": False, "sent": False, "reason": "dingtalk_disabled"}
        if not monitor_enabled and not force_send:
            return {"ok": False, "sent": False, "reason": "monitor_disabled"}

        # 四池偏离提醒：独立于个股涨跌触发，没有涨跌提醒时也要能推。
        # 默认关闭，开关在 monitor.pool_drift_alert_enabled。
        pool_drift = self._notify_pool_drift(
            webhook_url=webhook_url,
            dingtalk_secret=dingtalk_secret,
            mode=mode,
            force_send=force_send,
        )

        triggered: List[Dict[str, Any]] = []
        for item in monitor_data:
            pct_chg = self._monitor_pct_change(item)
            code = str(item.get("secID") or item.get("code") or "-")
            if pct_chg is not None and pct_chg < pct_reset_below:
                self._reset_pct_alert_state(code, "up")
            if pct_chg is not None and pct_chg > -pct_down_reset_below:
                self._reset_pct_alert_state(code, "down")
            pct_level = self._matched_pct_level(pct_chg, pct_levels, "up") if pct_rule_enabled else None
            pct_down_level = self._matched_pct_level(pct_chg, pct_down_levels, "down") if pct_down_rule_enabled else None
            pct_triggered = pct_level is not None
            pct_down_triggered = pct_down_level is not None
            strategy_triggered = strategy_rule_enabled and bool(item.get("strategy_triggered"))
            if pct_triggered or pct_down_triggered or strategy_triggered:
                copied = dict(item)
                copied["pct_chg"] = pct_chg
                copied["pct_triggered"] = pct_triggered
                copied["pct_down_triggered"] = pct_down_triggered
                copied["pct_alert_level"] = pct_level
                copied["pct_down_alert_level"] = pct_down_level
                copied["strategy_triggered"] = strategy_triggered
                triggered.append(copied)

        # 单票止损降级（豁免期内）：不单独推送，改每天汇总一条。
        # 为什么不是屏蔽——屏蔽了豁免一过就查不到「这期间其实一直在亏」；
        # 为什么不是照推——同一笔持仓一条豁免一条天天告警，用户很快就会无视全部告警。
        downgraded = [it for it in monitor_data if it.get("stop_loss_downgraded")]

        if not triggered and not force_send and not downgraded:
            if pool_drift.get("sent"):
                return {
                    "ok": True, "sent": True, "reason": "pool_drift_only", "mode": mode,
                    "channels": [{
                        "channel": "pool_drift",
                        "title": pool_drift.get("title"),
                        "count": pool_drift.get("count", 0),
                        "result": pool_drift.get("result"),
                    }],
                }
            return {"ok": True, "sent": False, "reason": "no_trigger", "mode": mode}

        if force_send and not triggered:
            # 手动测试：没有触发数据时直接发送一条测试消息，验证 webhook/验签配置
            now_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            strategy_name = str(settings.get("monitor_strategy") or "未设置")
            body = "\n".join(
                [
                    "### 钉钉测试提醒",
                    f"- 时间: {now_text}",
                    f"- 监控策略: {strategy_name}",
                    "- 说明: 这是测试消息，用于验证钉钉 webhook 配置",
                ]
            )
            result = self.host._notify_tool.send_dingtalk_markdown(
                webhook_url,
                "钉钉测试提醒",
                body,
                secret=dingtalk_secret,
            )
            self._log_alert("test", "钉钉测试提醒", body, 0, result, "test")
            return {
                "ok": bool(result.get("ok")),
                "sent": bool(result.get("ok")),
                "mode": mode,
                "reason": "test_message" if result.get("ok") else "test_send_failed",
                "detail": result,
            }

        now_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now_ts = time.time()
        channel_results: List[Dict[str, Any]] = []
        sent_any = False

        # 降级汇总：每天最多一条，跟其他告警并列发，不挤占它们的正文。
        if downgraded:
            digest_key = "stop_loss_digest"
            last_digest = float(self.host._alert_last_sent.get(digest_key, 0) or 0)
            if force_send or (now_ts - last_digest) >= 86400:
                digest_lines = [
                    "### 止损降级汇总（仅记录，不单独推送）",
                    "以下持仓处于时间止损豁免期，止损信号降级为记录，事实仍然有效：",
                ]
                for it in downgraded[:max_items]:
                    detail = it.get("stop_loss_downgrade_detail") or {}
                    ts = it.get("time_stop") if isinstance(it.get("time_stop"), dict) else {}
                    bits = [f"- {it.get('name') or it.get('secID') or '-'}：触发止损信号"]
                    if ts.get("pnl_pct") is not None:
                        bits.append(f"浮亏 {abs(float(ts['pnl_pct'])):.1f}%")
                    if ts.get("holding_days") is not None:
                        bits.append(f"持有 {ts.get('holding_days')} 天")
                    bits.append(f"豁免至 {detail.get('expires_on') or '—'}")
                    digest_lines.append("，".join(bits) + f"，现价 {self._price_text(it)}")
                digest_lines.append(f"- 时间: {now_text}")
                digest_body = "\n".join(digest_lines)
                digest_result = self.host._notify_tool.send_dingtalk_markdown(
                    webhook_url, "止损降级汇总", digest_body, secret=dingtalk_secret,
                )
                channel_results.append({
                    "channel": "stop_loss_digest",
                    "title": "止损降级汇总",
                    "count": len(downgraded),
                    "result": digest_result,
                })
                self._log_alert("stop_loss_digest", "止损降级汇总", digest_body,
                                len(downgraded), digest_result, mode)
                if digest_result.get("ok"):
                    sent_any = True
                    self.host._alert_last_sent[digest_key] = now_ts

        def _should_send(channel: str, item: Dict[str, Any]) -> bool:
            if mode != "auto" or force_send:
                return True
            if channel == "pct_change_up":
                key = self._pct_alert_key("up", item, item.get("pct_alert_level"))
            elif channel == "pct_change_down":
                key = self._pct_alert_key("down", item, item.get("pct_down_alert_level"))
            else:
                key = f"{channel}:{item.get('secID') or item.get('code')}"
            last_sent = self.host._alert_last_sent.get(key, 0)
            return now_ts - last_sent >= auto_dedupe_seconds

        def _mark_sent(channel: str, item: Dict[str, Any]) -> None:
            if channel == "pct_change_up":
                key = self._pct_alert_key("up", item, item.get("pct_alert_level"))
            elif channel == "pct_change_down":
                key = self._pct_alert_key("down", item, item.get("pct_down_alert_level"))
            else:
                key = f"{channel}:{item.get('secID') or item.get('code')}"
            self.host._alert_last_sent[key] = now_ts

        def _send_channel(channel: str, title: str, items: List[Dict[str, Any]], to_line) -> None:
            nonlocal sent_any
            picked = [it for it in items if _should_send(channel, it)][:max_items]
            if not picked:
                return
            lines = [f"### {title}"]
            for it in picked:
                lines.append(to_line(it))
            result = self.host._notify_tool.send_dingtalk_markdown(
                webhook_url,
                title,
                "\n".join(lines),
                secret=dingtalk_secret,
            )
            channel_results.append({"channel": channel, "title": title, "count": len(picked), "result": result})
            self._log_alert(channel, title, "\n".join(lines), len(picked), result, mode)
            if result.get("ok"):
                sent_any = True
                for it in picked:
                    _mark_sent(channel, it)

        pct_items = [item for item in triggered if item.get("pct_triggered")]
        _send_channel(
            "pct_change_up",
            "股票涨幅提醒",
            pct_items,
            lambda it: f"- {it.get('name') or it.get('secID') or it.get('code') or '-'}："
            f"触发 {self._pct_text(it.get('pct_alert_level'))} 档，涨幅 {self._pct_text(it.get('pct_chg'))}，现价 {self._price_text(it)}，时间 {now_text}",
        )

        pct_down_items = [item for item in triggered if item.get("pct_down_triggered")]
        _send_channel(
            "pct_change_down",
            "股票跌幅提醒",
            pct_down_items,
            lambda it: f"- {it.get('name') or it.get('secID') or it.get('code') or '-'}："
            f"触发 -{self._pct_text(it.get('pct_down_alert_level'))} 档，跌幅 {self._pct_text(it.get('pct_chg'))}，现价 {self._price_text(it)}，时间 {now_text}",
        )

        # 账户级弹药门控：满仓时整条买入类提醒都不发，不是加一句"不建议买入"
        account = {}
        try:
            account = self.host.get_account_snapshot(monitor_data) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("account_snapshot_failed error=%r", exc)
        buy_blocked = account.get("buy_allowed") is False
        ammo_label = str(account.get("ammo_label") or "")
        ammo_reason = str(account.get("reason") or "")

        # 情绪仓位提醒：情绪转亢奋/退潮/崩塌时主动推减仓提醒
        sentiment_result = self._notify_sentiment_position(
            account=account,
            webhook_url=webhook_url,
            dingtalk_secret=dingtalk_secret,
            mode=mode,
            force_send=force_send,
            now_text=now_text,
        )
        if sentiment_result.get("sent"):
            channel_results.append({
                "channel": "sentiment_position",
                "title": sentiment_result.get("title"),
                "count": 1,
                "result": sentiment_result.get("result"),
            })
            sent_any = True

        if pool_drift.get("sent"):
            channel_results.append({
                "channel": "pool_drift",
                "title": pool_drift.get("title"),
                "count": pool_drift.get("count", 0),
                "result": pool_drift.get("result"),
            })
            sent_any = True

        strategy_groups: Dict[str, List[Dict[str, Any]]] = {}
        for item in triggered:
            if item.get("strategy_triggered"):
                signal = str(item.get("strategy_signal") or "").lower()
                # 时间止损走独立字段。策略信号为空时才拿它分组——
                # 这样既不会被别的信号盖掉，也不会盖掉别人。
                if not signal and item.get("time_stop_signal"):
                    ts_slot = item.get("time_stop") if isinstance(item.get("time_stop"), dict) else {}
                    # 已豁免的不能拿它分组，否则会被塞进「时间止损」那一组去推送
                    if not ts_slot.get("overridden"):
                        signal = str(item.get("time_stop_signal")).lower()
                signal = signal or "unknown"
                strategy_groups.setdefault(signal, []).append(item)

        skipped_buy = 0
        if buy_blocked:
            for signal in list(strategy_groups.keys()):
                if signal in _BUY_SIGNALS:
                    skipped_buy += len(strategy_groups.pop(signal))

        # 满仓时给标题补一句，让人知道买入提醒不是漏了，而是被门控掉了
        title_suffix = f"｜{ammo_label}，不推买入" if buy_blocked and ammo_label else ""
        for signal, items in strategy_groups.items():
            signal_text = self._signal_text(signal)
            _send_channel(
                f"strategy_{signal}",
                f"策略提醒（{signal_text}）{title_suffix}",
                items,
                self._strategy_line(signal_text=signal_text, signal=signal, now_text=now_text),
            )

        if not channel_results:
            reason = "auto_deduped"
            if downgraded and not triggered:
                # 只有降级项，且今天的汇总已经发过 → 不是「没触发」，是「已汇总过」
                reason = "stop_loss_digest_throttled"
            elif buy_blocked and skipped_buy:
                reason = "account_full_skip_buy"
            elif buy_blocked and not skipped_buy:
                reason = "account_full_no_sell_signal"
            return {
                "ok": True, "sent": False, "reason": reason, "mode": mode,
                "auto_dedupe_seconds": auto_dedupe_seconds,
                "skipped_buy_count": skipped_buy,
                "ammo_state": account.get("ammo_state"),
                "ammo_reason": ammo_reason,
            }

        all_ok = all(bool(it.get("result", {}).get("ok")) for it in channel_results)
        return {
            "ok": all_ok, "sent": sent_any, "mode": mode,
            "triggered_count": len(triggered),
            "skipped_buy_count": skipped_buy,
            "ammo_state": account.get("ammo_state"),
            "ammo_reason": ammo_reason,
            "channels": channel_results,
        }
