"""市场情绪 → 仓位与现金纪律服务（Sentiment Position Service）。

用户的经验写在这里：

- **「市场情绪低落的时候我们得留有现金开始建仓」**——低迷不是看空的理由，
  而是"攒子弹"的理由。留现金、敢在别人恐慌时分批建仓，才有资格吃均值回归。
- **「市场情绪疯狂的时候要卖出」**——亢奋期的上涨是别人接力给的，不是自己赚的。
  兑现浮盈、不追高、不加仓，把浮盈变成实实在在的现金。
- **「绝对不参与博傻游戏」**——不接最后一棒、不买没有基本面支撑的纯题材、
  不为"感觉"下单、不在退潮期逆势抄底投机股。

这套服务把上述经验变成**可计算、可复现**的仓位政策：

    情绪阶段 + 情绪温度 + 大盘位置 + 当前实际仓位
        → 目标仓位区间 / 目标现金比例 / 具体动作 / 建仓节奏
        → 是否允许开仓、是否允许追高、不博傻检查清单

设计原则（与项目理念一致）：
- 目标仓位是**区间**不是点位，风格档位只做微调——不追求精确，追求纪律。
- 「崩塌」与「低迷」必须分开：崩塌期任何反弹都是减仓机会，只有冰点/修复
  才是建仓窗口。把两者混为一谈，会在最不该抄底的时候抄底。
- 大盘位置做**系统性叠加**：系统性低估期放宽仓位上限，高估期收紧。
- 情绪数据缺失时给中性政策，绝不因为"没数据"就默认满仓。
"""

from __future__ import annotations

import math

from ..core import decision_rules
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 阶段 → 仓位政策基准表
# ---------------------------------------------------------------------------
# pos_min/pos_max：目标仓位区间（占总资产）
# cash_min/cash_max：目标现金比例区间（占总资产）
# 两者互补但保留缓冲，避免把仓位卡死在单一数字上。
_REGIME_TABLE: Dict[str, Dict[str, Any]] = decision_rules.regime_table()
_STYLE_TILT: Dict[str, Dict[str, Any]] = decision_rules.style_tilt()
_PHASE_LABEL: Dict[str, str] = decision_rules.phase_labels()


def _f(value: Any) -> Optional[float]:
    """宽松地转 float，失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _regime_for(phase: str, temperature: Optional[float]) -> str:
    """把情绪阶段映射到仓位政策档位。

    温度只在阶段判定缺失时兜底（阶段本身就是温度分档的结果，
    但极端温度需要区分「冰点」和「退潮」）。
    """
    key = str(phase or "unknown").strip().lower()
    if key in _REGIME_TABLE:
        return key
    if temperature is None:
        return "unknown"
    temp = float(temperature)
    if temp < 0.22:
        return "freezing"
    if temp < 0.42:
        return "repair"
    if temp < 0.68:
        return "advance"
    return "euphoria"


def build_sentiment_position_policy(
    sentiment: Optional[Dict[str, Any]] = None,
    market: Optional[Dict[str, Any]] = None,
    account: Optional[Dict[str, Any]] = None,
    style: Optional[str] = None,
) -> Dict[str, Any]:
    """把市场情绪翻译成仓位与现金纪律。

    参数：
      sentiment: get_market_sentiment 的返回（含 phase / temperature / metrics）
      market:    get_market_regime 的返回（含 pos250 大盘 250 日分位）
      account:   账户快照（含 position_ratio 实际仓位 / cash_ratio）
      style:     投资风格 long / mid_long / swing，只做小幅微调

    返回：
      regime / regime_label / target_position_min / target_position_max /
      target_cash_min / target_cash_max / action / action_text / pace /
      allow_new_buy / allow_chase / gamble_guards / gap_action / reason / notes
    """
    sentiment = sentiment or {}
    market = market or {}
    account = account or {}
    notes: List[str] = []

    phase = str(sentiment.get("phase") or "unknown").strip().lower()
    temperature = _f(sentiment.get("temperature"))
    phase_label = str(sentiment.get("phase_label") or _PHASE_LABEL.get(phase, "未知"))

    key = _regime_for(phase, temperature)
    base = dict(_REGIME_TABLE[key])

    # ---------------- 风格微调（只在基准区间内小幅挪动）----------------
    style_key = str(style or "").strip().lower()
    tilt = _STYLE_TILT.get(style_key)
    pos_min = float(base["pos_min"])
    pos_max = float(base["pos_max"])
    if tilt:
        delta = float(tilt["pos_delta"])
        pos_min = _clip(pos_min + delta)
        pos_max = _clip(pos_max + delta)
        notes.append(f"按「{tilt['label']}」风格微调目标仓位 {delta:+.0%}")

    # ---------------- 大盘位置做系统性叠加 ----------------
    mpos = _f(market.get("pos250"))
    market_overlay = "中性"
    if mpos is not None:
        if mpos <= 0.25:
            pos_min = _clip(pos_min + 0.10)
            pos_max = _clip(pos_max + 0.10)
            market_overlay = "系统性低估（放宽仓位上限）"
            notes.append(
                f"大盘处 250 日区间 {mpos:.0%} 位，属系统性低估——"
                "别人恐慌时该有子弹，目标仓位上限放宽 10 个百分点"
            )
        elif mpos >= 0.85:
            pos_min = _clip(pos_min - 0.10)
            pos_max = _clip(pos_max - 0.10)
            market_overlay = "系统性高估（收紧仓位上限）"
            notes.append(
                f"大盘处 250 日区间 {mpos:.0%} 位，指数本身已不便宜——"
                "缺乏系统性支撑，目标仓位上限收紧 10 个百分点"
            )
        else:
            notes.append(f"大盘处 250 日区间 {mpos:.0%} 位，位置中性，不做系统性加减")
    else:
        notes.append("大盘位置不可用，未做系统性叠加")

    # 现金目标 = 仓位目标的互补
    cash_min = _clip(1.0 - pos_max)
    cash_max = _clip(1.0 - pos_min)

    # ---------------- 不博傻检查清单 ----------------
    metrics = sentiment.get("metrics") or {}
    gamble_guards: List[str] = [
        "不接最后一棒：高位连板股不参与，等它炸板就晚了",
        "不买没有基本面支撑的纯题材：故事不能当安全边际",
        "不追高：不给已经冲起来的票加仓",
        "不因感觉下单：每一次买卖都要有可核实的数字支撑",
    ]
    max_board = metrics.get("max_board")
    zt_count = metrics.get("zt_count")
    zb_rate = _f(metrics.get("zb_rate"))
    if max_board is not None and int(max_board) >= 5:
        gamble_guards.append(
            f"当前最高连板 {int(max_board)} 板，情绪高位——接力风险高，"
            "此时买入等于给别人接盘"
        )
    if zt_count is not None and int(zt_count) >= 80:
        gamble_guards.append(
            f"涨停 {int(zt_count)} 家属于普涨亢奋——普涨高潮通常是阶段顶部区域，"
            "此时最该做的是兑现而不是追击"
        )
    if zb_rate is not None and zb_rate >= 0.35:
        gamble_guards.append(
            f"炸板率 {zb_rate:.0%}，高位封不住——接盘的意愿在下降，"
            "说明接力已经不可持续"
        )

    # ---------------- 与实际仓位的差距 → 可执行动作 ----------------
    actual_pos = _f(account.get("position_ratio"))
    gap_action = "维持"
    if actual_pos is not None:
        if actual_pos < pos_min - 0.03:
            gap = pos_min - actual_pos
            gap_action = f"低于目标下限：可用现金分批补约 {gap:.0%} 的仓位"
            if base["action"] in ("accumulate_cash", "start_building", "hold"):
                base["action_text"] += f"；当前实际仓位 {actual_pos:.0%} 低于目标下限，优先用现金补足"
        elif actual_pos > pos_max + 0.03:
            gap = actual_pos - pos_max
            gap_action = f"高于目标上限：需要减约 {gap:.0%} 的仓位"
            if base["action"] in ("trim", "trim_speculative", "defend"):
                base["action_text"] += f"；当前实际仓位 {actual_pos:.0%} 高于目标上限，优先减仓落袋"
            else:
                base["action_text"] += (
                    f"；当前实际仓位 {actual_pos:.0%} 偏高，建议先减到目标上限内再谈加仓"
                )
        else:
            gap_action = f"在目标区间内（当前 {actual_pos:.0%}）：维持，不主动加减"

    reason = str(sentiment.get("phase_reason") or sentiment.get("detail") or "").strip()
    if not reason:
        reason = f"情绪阶段 {phase_label}"

    return {
        "ok": True,
        "phase": phase,
        "phase_label": phase_label,
        "temperature": temperature,
        "regime": base["regime"],
        "regime_label": base["regime_label"],
        "target_position_min": round(pos_min, 4),
        "target_position_max": round(pos_max, 4),
        "target_cash_min": round(cash_min, 4),
        "target_cash_max": round(cash_max, 4),
        "action": base["action"],
        "action_text": base["action_text"],
        "pace": base["pace"],
        "allow_new_buy": bool(base["allow_new_buy"]),
        "allow_chase": False,  # 任何阶段都不允许追高——这是"不博傻"的硬底线
        "gamble_guards": gamble_guards,
        "gamble_note": base["gamble_note"],
        "market_pos250": round(mpos, 4) if mpos is not None else None,
        "market_overlay": market_overlay,
        "actual_position": round(actual_pos, 4) if actual_pos is not None else None,
        "gap_action": gap_action,
        "reason": reason,
        "notes": notes,
    }


def format_sentiment_policy(policy: Dict[str, Any]) -> str:
    """把仓位政策渲染成给 agent 读的文字。"""
    if not policy or not policy.get("ok"):
        return f"市场情绪策略不可用：{(policy or {}).get('error') or '数据不足'}"

    temp = policy.get("temperature")
    temp_txt = f"{float(temp):.0%}" if temp is not None else "未知"
    lines = [
        f"市场情绪策略：{policy.get('regime_label')}（阶段 {policy.get('phase_label')}，温度 {temp_txt}）",
        f"目标仓位 {float(policy.get('target_position_min') or 0):.0%}~"
        f"{float(policy.get('target_position_max') or 0):.0%}"
        f"｜目标现金 {float(policy.get('target_cash_min') or 0):.0%}~"
        f"{float(policy.get('target_cash_max') or 0):.0%}",
        f"动作：{policy.get('action_text')}",
        f"节奏：{policy.get('pace')}",
        f"开仓：{'允许（只做低位、有价值的方向）' if policy.get('allow_new_buy') else '禁止——此阶段只做减仓/保命/持有'}",
        f"追高：{'允许' if policy.get('allow_chase') else '任何阶段都禁止（不博傻的硬底线）'}",
        f"仓位差距：{policy.get('gap_action')}",
    ]
    if policy.get("reason"):
        lines.append(f"依据：{policy.get('reason')}")
    guards = policy.get("gamble_guards") or []
    if guards:
        lines.append("不博傻清单：" + "；".join(guards))
    if policy.get("gamble_note"):
        lines.append(f"纪律提醒：{policy.get('gamble_note')}")
    notes = policy.get("notes") or []
    if notes:
        lines.append("说明：" + "；".join(notes))
    return "\n".join(lines)
