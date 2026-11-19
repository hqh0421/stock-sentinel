"""市场情绪历史曲线与周期位置（Sentiment History Service）。

为什么需要这一层
----------------
用户理念：**「市场情绪低落的时候我们得留有现金开始建仓；市场情绪疯狂的时候要卖出。」**

这条纪律要落地，光有"今天情绪多少"是不够的——还得知道：

- 当前温度在**历史上处于什么位置**？（今天 49% 是"从冰点刚起来"还是"从亢奋掉下来"）
- 这一轮情绪**走了多久**？（持续 3 天的亢奋 vs 持续 3 周的退潮，含义完全不同）
- 情绪的**变化方向**？（退潮期最怕的是"退潮中的一次反弹"被误认成反转）
- 涨停家数是不是**腰斩**了？（这是退潮最硬的证据，比温度更能说明问题）

所以本服务把 `sentiment_history.json` 里的日度快照加工成：

1. **时间序列**：日期、温度、阶段、涨停/跌停家数、封板率、最高连板；
2. **周期位置**：当前温度在过去 N 日的分位、当前阶段已持续多少个交易日；
3. **转折识别**：阶段切换点（什么时候从亢奋切到退潮）、腰斩信号（涨停家数较峰值腰斩）；
4. **给 agent 的解读**：一句话说清"现在处在情绪周期的哪一段"。

数据来源：`sentiment_service` 已经把每日快照落盘到 `config/sentiment_history.json`
（最多 90 个交易日）。本服务只做读取与加工，不自己抓数据。

刻意不做的事
------------
- 不做情绪的"预测"：历史序列能告诉你位置，不能告诉你明天会怎样。
- 不自己算温度：温度口径由 `sentiment_service` 统一定义，避免两处算法漂移。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 情绪阶段的顺序（用于判断"阶段切换"与"退潮中的反弹"）
_PHASE_ORDER = ["freezing", "repair", "advance", "euphoria", "ebb", "collapse"]

_PHASE_LABEL = {
    "freezing": "冰点",
    "repair": "修复",
    "advance": "主升",
    "euphoria": "亢奋",
    "ebb": "退潮",
    "collapse": "崩塌",
    "unknown": "未知",
}


def _f(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num:  # NaN
        return None
    return num


class SentimentHistoryService:
    """情绪历史曲线与周期位置。"""

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        self._config_dir = Path(config_dir) if config_dir else Path("config")
        self._history_file = self._config_dir / "sentiment_history.json"

    # ------------------------------------------------------------------
    # 原始历史
    # ------------------------------------------------------------------
    def load_history(self) -> List[Dict[str, Any]]:
        try:
            if not self._history_file.exists():
                return []
            data = json.loads(self._history_file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as exc:  # noqa: BLE001
            logger.warning("sentiment_history_read_failed error=%r", exc)
            return []

    # ------------------------------------------------------------------
    # 主入口：曲线 + 周期位置
    # ------------------------------------------------------------------
    def get_history_view(self, current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """返回情绪历史曲线与周期位置。

        参数 current：当前情绪快照（get_market_sentiment 的返回）。
                      不传则用历史里最后一条。
        """
        history = self.load_history()
        notes: List[str] = []

        if not history:
            return {
                "ok": False,
                "error": "情绪历史为空（需要每日快照累积，通常 3~5 个交易日后才有曲线）",
                "points": [],
                "notes": notes,
            }

        points = self._build_points(history)
        if current and current.get("ok"):
            # 用实时快照覆盖最后一条，保证曲线末端与页面顶部的情绪档位一致
            live = {
                "date": current.get("as_of"),
                "temperature": current.get("temperature"),
                "phase": current.get("phase"),
                "zt_count": (current.get("metrics") or {}).get("zt_count"),
                "dt_count": (current.get("metrics") or {}).get("dt_count"),
                "seal_rate": (current.get("metrics") or {}).get("seal_rate"),
                "max_board": (current.get("metrics") or {}).get("max_board"),
            }
            if live.get("date"):
                points = [p for p in points if p.get("date") != live["date"]]
                points.append(live)
                points.sort(key=lambda x: str(x.get("date") or ""))

        cycle = self._analyze_cycle(points)

        # 阶段统计：各阶段出现过多少天、当前处于哪个阶段
        phase_days: Dict[str, int] = {}
        for point in points:
            phase = str(point.get("phase") or "unknown")
            phase_days[phase] = phase_days.get(phase, 0) + 1

        return {
            "ok": True,
            "points": points,
            "count": len(points),
            "cycle": cycle,
            "phase_days": phase_days,
            "phase_labels": _PHASE_LABEL,
            "notes": notes,
        }

    # ------------------------------------------------------------------
    # 曲线构造
    # ------------------------------------------------------------------
    def _build_points(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        points: List[Dict[str, Any]] = []
        for row in history:
            if not isinstance(row, dict):
                continue
            temperature = _f(row.get("temperature"))
            phase = str(row.get("phase") or "unknown")
            points.append({
                "date": str(row.get("date") or ""),
                "temperature": temperature,
                "phase": phase,
                "phase_label": _PHASE_LABEL.get(phase, phase),
                "zt_count": row.get("zt_count"),
                "dt_count": row.get("dt_count"),
                "seal_rate": row.get("seal_rate"),
                "max_board": row.get("max_board"),
            })
        points.sort(key=lambda x: str(x.get("date") or ""))
        return points

    # ------------------------------------------------------------------
    # 周期位置分析
    # ------------------------------------------------------------------
    def _analyze_cycle(self, points: List[Dict[str, Any]]) -> Dict[str, Any]:
        """当前情绪在历史中的位置 + 阶段持续时长 + 转折信号。"""
        notes: List[str] = []
        temps = [p["temperature"] for p in points if p.get("temperature") is not None]
        if not temps:
            return {"ok": False, "error": "历史温度序列为空"}

        current = points[-1]
        cur_temp = _f(current.get("temperature"))
        cur_phase = str(current.get("phase") or "unknown")

        # --- 温度分位：当前温度在历史样本中的位置 ---
        temp_pctile: Optional[float] = None
        if cur_temp is not None:
            temp_pctile = sum(1 for t in temps if t <= cur_temp) / len(temps)

        # --- 阶段持续天数（从最后一次阶段切换算起）---
        phase_run = 1
        for i in range(len(points) - 2, -1, -1):
            if str(points[i].get("phase") or "unknown") == cur_phase:
                phase_run += 1
            else:
                break
        prev_phase = str(points[-2].get("phase") or "unknown") if len(points) >= 2 else None

        # --- 温度峰值/谷值与回撤 ---
        peak_temp = max(temps)
        trough_temp = min(temps)
        peak_idx = max(range(len(temps)), key=lambda i: temps[i])
        temp_from_peak = (cur_temp / peak_temp - 1.0) * 100.0 if cur_temp is not None and peak_temp > 0 else None

        # --- 涨停家数：峰值与腰斩信号 ---
        zt_series = [p.get("zt_count") for p in points if isinstance(p.get("zt_count"), (int, float))]
        zt_peak = max(zt_series) if zt_series else None
        zt_current = current.get("zt_count")
        zt_halved = bool(
            zt_peak and isinstance(zt_current, (int, float)) and zt_peak > 0
            and float(zt_current) <= float(zt_peak) * 0.5
        )
        zt_drop_pct = (
            (float(zt_current) / float(zt_peak) - 1.0) * 100.0
            if zt_peak and isinstance(zt_current, (int, float)) and zt_peak > 0 else None
        )

        # --- 阶段转折点 ---
        transitions: List[Dict[str, Any]] = []
        for i in range(1, len(points)):
            before = str(points[i - 1].get("phase") or "unknown")
            after = str(points[i].get("phase") or "unknown")
            if before != after:
                transitions.append({
                    "date": points[i].get("date"),
                    "from": before,
                    "from_label": _PHASE_LABEL.get(before, before),
                    "to": after,
                    "to_label": _PHASE_LABEL.get(after, after),
                    "temperature": points[i].get("temperature"),
                })

        # --- 周期阶段判定：把"位置"翻译成可执行的话 ---
        cycle_stage, cycle_text = self._judge_cycle_stage(
            phase=cur_phase,
            temp_pctile=temp_pctile,
            phase_run=phase_run,
            zt_halved=zt_halved,
            temp_from_peak=temp_from_peak,
        )

        if temp_pctile is not None:
            notes.append(
                f"当前情绪温度 {cur_temp:.0%}，在近 {len(temps)} 个交易日样本中处 {temp_pctile:.0%} 分位"
            )
        notes.append(
            f"当前阶段「{_PHASE_LABEL.get(cur_phase, cur_phase)}」已持续 {phase_run} 个交易日"
        )
        if prev_phase and prev_phase != cur_phase:
            notes.append(
                f"上一阶段是「{_PHASE_LABEL.get(prev_phase, prev_phase)}」，"
                f"于 {current.get('date')} 切换为「{_PHASE_LABEL.get(cur_phase, cur_phase)}」"
            )
        if zt_halved:
            notes.append(
                f"涨停家数已从峰值 {int(zt_peak)} 家降到 {int(zt_current)} 家（腰斩），"
                "这是退潮最硬的证据——赚钱效应在收缩"
            )
        elif zt_drop_pct is not None and zt_drop_pct <= -30:
            notes.append(f"涨停家数较峰值下降 {abs(zt_drop_pct):.0f}%，接力意愿在转弱")

        return {
            "ok": True,
            "current_phase": cur_phase,
            "current_phase_label": _PHASE_LABEL.get(cur_phase, cur_phase),
            "current_temperature": cur_temp,
            "temperature_percentile": round(temp_pctile, 4) if temp_pctile is not None else None,
            "temperature_peak": round(peak_temp, 4),
            "temperature_trough": round(trough_temp, 4),
            "temperature_from_peak_pct": round(temp_from_peak, 2) if temp_from_peak is not None else None,
            "peak_date": points[peak_idx].get("date") if peak_idx < len(points) else None,
            "phase_run_days": phase_run,
            "previous_phase": prev_phase,
            "previous_phase_label": _PHASE_LABEL.get(prev_phase, prev_phase) if prev_phase else None,
            "zt_peak": zt_peak,
            "zt_current": zt_current,
            "zt_drop_pct": round(zt_drop_pct, 2) if zt_drop_pct is not None else None,
            "zt_halved": zt_halved,
            "transitions": transitions[-8:],
            "cycle_stage": cycle_stage,
            "cycle_text": cycle_text,
            "notes": notes,
        }

    def _judge_cycle_stage(
        self,
        phase: str,
        temp_pctile: Optional[float],
        phase_run: int,
        zt_halved: bool,
        temp_from_peak: Optional[float],
    ) -> tuple:
        """把周期位置翻译成一句话判断（给 agent 和前端都用）。"""
        if phase in ("collapse", "ebb"):
            if zt_halved:
                return "退潮确认", (
                    "退潮阶段且涨停家数已较峰值腰斩：赚钱效应实质收缩，"
                    "此时任何反弹都先当减仓机会看，不接最后一棒"
                )
            return "退潮进行中", (
                "退潮阶段：接力资金在撤，此时的反弹多数是给套牢盘减仓的机会，"
                "不要把反弹当成反转"
            )
        if phase == "freezing":
            return "冰点筑底", (
                "情绪冰点：赚钱效应消失、没人愿意接力——"
                "这是留现金、为下一波建仓备弹的窗口，只买跌出来的价值"
            )
        if phase == "repair":
            return "修复启动", (
                "情绪修复：赚钱效应初现，可以分批建仓，"
                "但只买位置低、有基本面支撑的，不追已经冲起来的"
            )
        if phase == "advance":
            if temp_from_peak is not None and temp_from_peak <= -20:
                return "主升后段", (
                    "主升阶段但温度已从峰值回落 20% 以上：接力在减弱，"
                    "持有可以，但不宜加仓，更不宜在此位置追高"
                )
            return "主升接力", "主升阶段：接力正在进行，持有让利润奔跑，不主动加仓"
        if phase == "euphoria":
            return "亢奋兑现", (
                "亢奋阶段：涨是别人接力给的，不是自己赚的——"
                "兑现浮盈、不加仓、不追高"
            )
        return "位置不明", "情绪数据不足，按原有纪律常规管理"

    # ------------------------------------------------------------------
    # 给 agent 读的文本
    # ------------------------------------------------------------------
    def format_history_view(self, view: Dict[str, Any], name: str = "市场情绪") -> str:
        if not view or not view.get("ok"):
            return f"{name}历史不可用：{(view or {}).get('error') or '数据不足'}"

        cycle = view.get("cycle") or {}
        lines = [
            f"{name}周期位置：{cycle.get('cycle_stage')}——{cycle.get('cycle_text')}",
        ]
        if cycle.get("temperature_percentile") is not None:
            lines.append(
                f"当前温度 {float(cycle.get('current_temperature') or 0):.0%}，"
                f"历史 {float(cycle['temperature_percentile']):.0%} 分位"
                f"（峰值 {float(cycle.get('temperature_peak') or 0):.0%}，"
                f"谷值 {float(cycle.get('temperature_trough') or 0):.0%}）"
            )
        lines.append(
            f"当前阶段已持续 {cycle.get('phase_run_days')} 个交易日"
            + (f"，上一阶段是{cycle.get('previous_phase_label')}" if cycle.get("previous_phase_label") else "")
        )
        if cycle.get("zt_peak") and cycle.get("zt_current") is not None:
            lines.append(
                f"涨停家数：峰值 {int(cycle['zt_peak'])} 家 → 当前 {int(cycle['zt_current'])} 家"
                + ("（已腰斩，退潮硬证据）" if cycle.get("zt_halved") else "")
            )
        notes = view.get("notes") or []
        if notes:
            lines.append("说明：" + "；".join(notes))
        return "\n".join(lines)


# 默认实例（stock_service 会用自己的 config 目录另建一个）
sentiment_history_service = SentimentHistoryService()
