"""市场情绪与个股情绪度量。

为什么需要这一层
----------------
多因子回答的是「这家公司值多少钱」，但 A 股的目标价还有一半是「市场愿意为它疯到什么程度」。
2026 年这波半导体行情就是典型：业绩兑现远跟不上股价，靠的是散户情绪不断接力——
涨停家数维持高位、连板高度不断打开、封板资金充足、板块内每天有新的低位股补涨。
这种行情的特征是「涨的时候不讲估值，跌的时候也不讲估值」，用固定目标价或固定止损线
去交易，结果一定是过早卖出或者被闷杀。

所以这里度量两件事：
    市场温度（market）：全市场敢不敢做、赚钱效应强不强、分歧大不大
    个股情绪（stock） ：这只票是不是当下资金接力的核心标的（连板/炸板/换手/加速）

数据来源
--------
东方财富的四张当日快照（都是免费公开接口，单张 0.1~0.3 秒）：
    stock_zt_pool_em      涨停股池 —— 连板数、炸板次数、封板资金、所属行业
    stock_zt_pool_zbgc_em 炸板股池 —— 封板失败，判断分歧程度
    stock_zt_pool_dtgc_em 跌停股池 —— 恐慌程度、连续跌停
    stock_zt_pool_strong_em 强势股池 —— 多头氛围（含 60 日新高、量比）

刻意不做的事
------------
- 不抓舆情、不抓新闻情绪（没有稳定免费源，且容易被标题党带偏）
- 不猜"哪个板块是主线"，只用涨停股的行业分布做统计
- 不做指数级的情绪拟合，避免和已有的大盘因子重复

历史快照会落盘到 config/sentiment_history.json（最多 90 个交易日），
用于判断"涨停家数有没有腰斩""最高连板有没有断"，这是识别退潮的关键。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import akshare as ak
import pandas as pd

from ..core import decision_rules

logger = logging.getLogger("app.sentiment")

# 涨停家数达到这个量级视为情绪满分。A 股常态 30~120 家，极热时 150+。
_ZT_FULL = float(decision_rules.sentiment_rules().get('zt_full', 90.0))
# 跌停家数达到这个量级视为恐慌满分。
_DT_FULL = float(decision_rules.sentiment_rules().get('dt_full', 25.0))
# 连板高度达到 6 板视为高度满分（6 板以上属于妖股级别，边际意义下降）
_BOARD_FULL = float(decision_rules.sentiment_rules().get('board_full', 6.0))
# 二板及以上家数，衡量接力的广度
_LIANBAN_FULL = float(decision_rules.sentiment_rules().get('lianban_full', 22.0))

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
    if value is None or value == "":
        return None
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return None
    return fv if fv == fv else None


def _clip(value: Optional[float], low: float = 0.0, high: float = 1.0) -> float:
    if value is None:
        return 0.0
    return max(low, min(high, float(value)))


class SentimentService:
    """市场情绪与个股情绪。所有对外方法都不抛异常，取不到就返回 unknown。"""

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._config_dir = Path(config_dir) if config_dir else Path("config")
        self._history_file = self._config_dir / "sentiment_history.json"

    # ------------------------------------------------------------------
    # 市场情绪
    # ------------------------------------------------------------------
    def get_market_sentiment(self, date: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        """返回全市场情绪快照。date 形如 2026-11-02，默认取最新交易日。"""
        key = date or "latest"
        if not force and key in self._cache:
            return self._cache[key]

        trade_date = self._resolve_trade_date(date)
        zt_df = self._fetch_pool("zt", trade_date)
        zb_df = self._fetch_pool("zb", trade_date)
        dt_df = self._fetch_pool("dt", trade_date)
        strong_df = self._fetch_pool("strong", trade_date)

        if zt_df is None and dt_df is None and zb_df is None:
            result = self._unknown("涨停池/跌停池/炸板池均取不到，市场情绪不参与判断")
            self._cache[key] = result
            return result

        zt_count = 0 if zt_df is None else len(zt_df)
        zb_count = 0 if zb_df is None else len(zb_df)
        dt_count = 0 if dt_df is None else len(dt_df)
        strong_count = 0 if strong_df is None else len(strong_df)

        # 连板数据只有涨停池里有
        max_board = 0
        lianban_count = 0
        zt_amount = 0.0
        seal_fund = 0.0
        industry_counter: Dict[str, Dict[str, Any]] = {}
        if zt_df is not None and len(zt_df):
            boards: List[int] = []
            for _, row in zt_df.iterrows():
                board = _f(row.get("连板数"))
                board = int(board) if board else 1
                boards.append(board)
                amount = _f(row.get("成交额"))
                if amount:
                    zt_amount += amount
                fund = _f(row.get("封板资金"))
                if fund:
                    seal_fund += fund
                industry = str(row.get("所属行业") or "").strip()
                if industry:
                    slot = industry_counter.setdefault(industry, {"name": industry, "zt_count": 0, "max_board": 0})
                    slot["zt_count"] += 1
                    slot["max_board"] = max(slot["max_board"], board)
            max_board = max(boards) if boards else 0
            lianban_count = sum(1 for b in boards if b >= 2)

        # 炸板率：封板失败占比，是分歧最灵敏的指标
        attempts = zt_count + zb_count
        zb_rate = (zb_count / attempts) if attempts > 0 else 0.0
        seal_rate = (zt_count / attempts) if attempts > 0 else 0.0

        zt_score = _clip(zt_count / _ZT_FULL)
        dt_score = _clip(dt_count / _DT_FULL)
        board_score = _clip(max_board / _BOARD_FULL)
        lianban_score = _clip(lianban_count / _LIANBAN_FULL)
        strong_score = _clip(strong_count / 150.0)

        temperature = (
            0.26 * zt_score
            + 0.18 * seal_rate
            + 0.18 * board_score
            + 0.14 * lianban_score
            + 0.10 * strong_score
            + 0.14 * (1.0 - dt_score)
        )
        # 炸板率是减项：同样 80 家涨停，封住的 80 家和封了 40 家又炸 40 家不是一回事
        temperature -= 0.12 * _clip(zb_rate / 0.45)
        temperature = _clip(temperature)

        # 历史对比：涨停家数腰斩 / 最高连板下降 是退潮的硬证据
        history = self._load_history()
        prev = history[-1] if history else None
        trend: Dict[str, Any] = {}
        if prev:
            prev_zt = _f(prev.get("zt_count"))
            prev_board = _f(prev.get("max_board"))
            if prev_zt and prev_zt > 0:
                trend["zt_change_pct"] = round((zt_count / prev_zt - 1.0) * 100, 1)
            if prev_board is not None:
                trend["max_board_change"] = int(max_board - prev_board)

        phase, phase_reason = self._judge_phase(
            temperature=temperature, zt_count=zt_count, dt_count=dt_count,
            zb_rate=zb_rate, max_board=max_board, trend=trend,
        )

        hot_industries = sorted(
            industry_counter.values(), key=lambda x: (x["zt_count"], x["max_board"]), reverse=True
        )[:8]

        notes: List[str] = []
        notes.append(f"涨停 {zt_count} 家 / 炸板 {zb_count} 家 / 跌停 {dt_count} 家，封板率 {seal_rate:.0%}")
        notes.append(f"最高连板 {max_board} 板，二板及以上 {lianban_count} 家")
        if hot_industries:
            top = hot_industries[0]
            notes.append(f"涨停最集中：{top['name']}（{top['zt_count']} 家，最高 {top['max_board']} 板）")
        if zb_rate >= 0.3:
            notes.append(f"炸板率 {zb_rate:.0%}，分歧明显")

        result = {
            "ok": True,
            "as_of": trade_date,
            "temperature": round(temperature, 3),
            "phase": phase,
            "phase_label": _PHASE_LABEL.get(phase, "未知"),
            "phase_reason": phase_reason,
            "metrics": {
                "zt_count": zt_count,
                "zb_count": zb_count,
                "dt_count": dt_count,
                "strong_count": strong_count,
                "seal_rate": round(seal_rate, 3),
                "zb_rate": round(zb_rate, 3),
                "max_board": max_board,
                "lianban_count": lianban_count,
                "zt_amount_yi": round(zt_amount / 1e8, 1) if zt_amount else None,
                "seal_fund_yi": round(seal_fund / 1e8, 1) if seal_fund else None,
            },
            "trend": trend,
            "hot_industries": hot_industries,
            "notes": notes,
            "detail": f"{_PHASE_LABEL.get(phase, '未知')}（温度 {temperature:.0%}）：{phase_reason}",
        }
        self._cache[key] = result
        self._append_history(trade_date, result)
        return result

    def _judge_phase(
        self,
        temperature: float,
        zt_count: int,
        dt_count: int,
        zb_rate: float,
        max_board: int,
        trend: Dict[str, Any],
    ) -> Tuple[str, str]:
        """情绪阶段判定。先判极端情形（崩塌/退潮），再按温度分档。"""
        if dt_count >= 40:
            return "collapse", f"跌停 {dt_count} 家，属于系统性崩塌，任何反弹都是减仓机会"

        zt_drop = trend.get("zt_change_pct")
        board_drop = trend.get("max_board_change")
        ebb_signals: List[str] = []
        if zt_drop is not None and zt_drop <= -40:
            ebb_signals.append(f"涨停家数比上一交易日减少 {abs(zt_drop):.0f}%")
        if board_drop is not None and board_drop < 0 and max_board <= 3:
            ebb_signals.append(f"最高连板由 {max_board - board_drop} 板降到 {max_board} 板")
        if zb_rate >= 0.4 and temperature >= 0.45:
            ebb_signals.append(f"炸板率 {zb_rate:.0%}，高位封不住")
        if dt_count >= 12 and temperature < 0.5:
            ebb_signals.append(f"跌停 {dt_count} 家同时赚钱效应转弱")
        if ebb_signals:
            return "ebb", "；".join(ebb_signals)

        if temperature < 0.22:
            return "freezing", f"情绪温度仅 {temperature:.0%}，涨停 {zt_count} 家，资金不愿意接力"
        if temperature < 0.42:
            return "repair", f"情绪温度 {temperature:.0%}，从低位缓慢修复，赚钱效应初现"
        if temperature < 0.68:
            return "advance", f"情绪温度 {temperature:.0%}，涨停 {zt_count} 家、最高 {max_board} 板，接力正在进行"
        return "euphoria", (
            f"情绪温度 {temperature:.0%}，涨停 {zt_count} 家、最高 {max_board} 板，"
            f"属于亢奋区，风险在于随时可能一日退潮"
        )

    # ------------------------------------------------------------------
    # 个股情绪
    # ------------------------------------------------------------------
    def get_stock_sentiment(
        self,
        code: str,
        bars: List[Dict[str, Any]],
        industry_name: str = "",
        market: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """从日 K 判断个股是否处于资金接力的核心位置。

        只读 K 线，不额外请求接口——连板/炸板/换手都能从日线反推，
        这样即使涨停池接口挂了，个股情绪仍然可用。
        """
        if not bars or len(bars) < 10:
            return {
                "ok": False, "limit_up_streak": 0, "zb_today": False,
                "sector_zt": 0, "heat": 0.0, "relay": False,
                "detail": "K 线不足，个股情绪不可用", "notes": [],
            }

        limit_pct = 19.5 if (code[:2] in ("30", "68") or code.startswith(("SH688", "SZ30"))) else 9.7
        digits = "".join(ch for ch in code if ch.isdigit())
        if digits[:2] in ("30", "68"):
            limit_pct = 19.5

        pcts = [_f(b.get("pctChg")) for b in bars]
        # 连板：从最后一根往前数连续涨停
        streak = 0
        for pct in reversed(pcts):
            if pct is not None and pct >= limit_pct - 0.5:
                streak += 1
            else:
                break

        limit_ups_20 = sum(1 for p in pcts[-20:] if p is not None and p >= limit_pct - 0.5)
        limit_downs_20 = sum(1 for p in pcts[-20:] if p is not None and p <= -(limit_pct - 0.5))

        last = bars[-1]
        close = _f(last.get("close"))
        high = _f(last.get("high"))
        prev_close = _f(bars[-2].get("close")) if len(bars) >= 2 else None
        # 炸板：盘中摸到涨停价但收盘没封住（收盘涨幅明显小于涨停幅度）
        zb_today = False
        if close and high and prev_close and prev_close > 0:
            limit_price = prev_close * (1 + limit_pct / 100.0)
            today_pct = _f(last.get("pctChg"))
            if high >= limit_price * 0.998 and (today_pct is None or today_pct < limit_pct - 1.5):
                zb_today = True

        def avg_amount(subset: List[Dict[str, Any]]) -> Optional[float]:
            vals = [_f(b.get("amount")) for b in subset if _f(b.get("amount"))]
            return sum(vals) / len(vals) if vals else None

        a5 = avg_amount(bars[-5:])
        a60 = avg_amount(bars[-60:] if len(bars) >= 60 else bars)
        amount_ratio = (a5 / a60) if (a5 and a60 and a60 > 0) else None

        closes = [_f(b.get("close")) for b in bars if _f(b.get("close"))]
        ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
        ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        ret5 = (closes[-1] / closes[-6] - 1.0) * 100 if len(closes) >= 6 else None
        bias20 = ((closes[-1] / ma20 - 1.0) * 100) if (ma20 and closes) else None

        # 板块联动：该行业今天有多少只涨停
        sector_zt = 0
        sector_max_board = 0
        if market and industry_name:
            for item in market.get("hot_industries") or []:
                if item.get("name") and (item["name"] in industry_name or industry_name in item["name"]):
                    sector_zt = int(item.get("zt_count") or 0)
                    sector_max_board = int(item.get("max_board") or 0)
                    break

        # 是否处在"接力核心位"：
        #   连板≥2 是硬信号；
        #   首板 + 板块有 4 家以上涨停也算——这是"题材刚起来、板块在扩散"，正是最该吃的一段；
        #   板块有涨停且个股在加速，同样算接力。
        accelerating = bool(ret5 and ret5 >= 15 and bias20 and bias20 >= 8)
        relay = bool(
            streak >= 2
            or (streak >= 1 and sector_zt >= 4)
            or (sector_zt >= 3 and accelerating)
        )

        heat = 0.0
        heat += 0.34 * _clip(streak / 4.0)
        heat += 0.20 * _clip(((amount_ratio or 1.0) - 1.0) / 1.5)
        heat += 0.16 * _clip((ret5 or 0.0) / 25.0)
        heat += 0.14 * _clip((bias20 or 0.0) / 15.0)
        heat += 0.16 * _clip(limit_ups_20 / 3.0)
        # 炸板当天扣热度：摸板不封说明接力资金不够
        if zb_today:
            heat -= 0.18
        heat = _clip(heat)

        notes: List[str] = []
        if streak >= 2:
            notes.append(f"当前 {streak} 连板")
        elif streak == 1:
            notes.append("今日涨停")
        if zb_today:
            notes.append("今日炸板（盘中触板未封），接力资金不足")
        if limit_ups_20:
            notes.append(f"近 20 日涨停 {limit_ups_20} 次" + (f"、跌停 {limit_downs_20} 次" if limit_downs_20 else ""))
        if amount_ratio:
            notes.append(f"近 5 日成交额是 60 日均值 {amount_ratio:.2f} 倍")
        if sector_zt:
            notes.append(f"所属板块今日涨停 {sector_zt} 家（最高 {sector_max_board} 板）")
        if accelerating:
            notes.append(f"5 日涨 {ret5:.0f}%、乖离 {bias20:.0f}%，处于加速段")

        if relay and streak >= 2:
            verdict = "接力核心"
        elif relay:
            verdict = "板块共振"
        elif zb_today:
            verdict = "炸板转弱"
        elif streak == 1:
            verdict = "首板启动"
        elif heat >= 0.3:
            verdict = "有资金关注"
        else:
            verdict = "无情绪加持"

        return {
            "ok": True,
            "limit_up_streak": streak,
            "limit_ups_20": limit_ups_20,
            "limit_downs_20": limit_downs_20,
            "zb_today": zb_today,
            "amount_ratio": round(amount_ratio, 2) if amount_ratio else None,
            "ret5": round(ret5, 2) if ret5 is not None else None,
            "bias20": round(bias20, 2) if bias20 is not None else None,
            "ma5": round(ma5, 2) if ma5 else None,
            "ma10": round(ma10, 2) if ma10 else None,
            "ma20": round(ma20, 2) if ma20 else None,
            "sector_zt": sector_zt,
            "sector_max_board": sector_max_board,
            "accelerating": accelerating,
            "relay": relay,
            "heat": round(heat, 3),
            "verdict": verdict,
            "notes": notes,
            "detail": "；".join(notes) if notes else "无涨停、无放量，情绪面无加持",
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _resolve_trade_date(self, date: Optional[str]) -> str:
        """把任意日期落到最近一个可用的交易日（涨停池在非交易日是空的）。"""
        if date:
            try:
                probe = datetime.datetime.strptime(date, "%Y-%m-%d").date()
            except ValueError:
                probe = datetime.date.today()
        else:
            probe = datetime.date.today()
        # 最多回退 10 天，覆盖长假
        for _ in range(10):
            if probe.weekday() < 5:
                return probe.strftime("%Y-%m-%d")
            probe -= datetime.timedelta(days=1)
        return probe.strftime("%Y-%m-%d")

    def _fetch_pool(self, kind: str, trade_date: str) -> Optional[pd.DataFrame]:
        compact = trade_date.replace("-", "")
        try:
            if kind == "zt":
                df = ak.stock_zt_pool_em(date=compact)
            elif kind == "zb":
                df = ak.stock_zt_pool_zbgc_em(date=compact)
            elif kind == "dt":
                df = ak.stock_zt_pool_dtgc_em(date=compact)
            elif kind == "strong":
                df = ak.stock_zt_pool_strong_em(date=compact)
            else:
                return None
            if df is None or len(df) == 0:
                return None
            return df
        except Exception as exc:  # noqa: BLE001
            logger.warning("sentiment_pool_failed kind=%s date=%s error=%r", kind, trade_date, exc)
            return None

    def _unknown(self, reason: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "as_of": None,
            "temperature": 0.5,
            "phase": "unknown",
            "phase_label": "未知",
            "phase_reason": reason,
            "metrics": {},
            "trend": {},
            "hot_industries": [],
            "notes": [reason],
            "detail": f"未知：{reason}",
        }

    def _load_history(self) -> List[Dict[str, Any]]:
        try:
            if not self._history_file.exists():
                return []
            data = json.loads(self._history_file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    def _append_history(self, trade_date: str, result: Dict[str, Any]) -> None:
        """每个交易日只记一条，保留最近 90 条。写失败不影响主流程。"""
        try:
            history = self._load_history()
            if history and history[-1].get("date") == trade_date:
                return
            metrics = result.get("metrics") or {}
            history.append({
                "date": trade_date,
                "zt_count": metrics.get("zt_count"),
                "dt_count": metrics.get("dt_count"),
                "max_board": metrics.get("max_board"),
                "seal_rate": metrics.get("seal_rate"),
                "temperature": result.get("temperature"),
                "phase": result.get("phase"),
            })
            history = history[-90:]
            self._config_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._history_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._history_file)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sentiment_history_write_failed error=%r", exc)


# 默认实例：config 目录按进程 cwd 推导；stock_service 会用自己的 config 目录另建一个
sentiment_service = SentimentService()
