"""因子有效性纪律：IC / IR、分层回测、样本外 walk-forward。

对应 `docs/量化方法论与监控系统设计原则.md` §4 第 5 项与
`docs/投资哲学与低位价值分析设计.md` §8.1「未做样本外检验」的欠账。

为什么必须有这一层
------------------
因子库能写表达式、能跑回测，但「回测赚钱」很可能只是把参数拟合到了这段历史。
本模块不预测收益，只回答一个可证伪的问题：**这个信号在今天之后的数据上还成立吗**。
做法是把「信号触发 / 未触发」当成分层，比较未来 N 日收益，再把样本按时间切成
样本内 / 样本外两段——样本外明显走弱就判疑似过拟合，不允许载入监控。

纯计算，不联网；表达式求值复用 `app.core.expr`（与监控、回测同一份口径）。
"""

from __future__ import annotations

import datetime
import logging
import math
from typing import Any, Dict, List, Optional

from ..core.expr import eval_signal_expr

logger = logging.getLogger(__name__)

MIN_SAMPLES = 30
DEFAULT_HORIZON_DAYS = 20
IC_CHUNK = 20


def _f(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 1e-12 or vy <= 1e-12:
        return None  # 常量序列（例如一段里全是未触发）无法算相关
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / math.sqrt(vx * vy)


def _mean(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 4) if values else None


def validate_factor(series: List[Dict[str, Any]], entry_expr: str,
                    horizon_days: int = DEFAULT_HORIZON_DAYS,
                    split_ratio: float = 0.7) -> Dict[str, Any]:
    """对一段日线做因子有效性检验（纯计算）。"""
    horizon_days = max(1, min(120, int(horizon_days or DEFAULT_HORIZON_DAYS)))
    split_ratio = max(0.5, min(0.9, float(split_ratio or 0.7)))
    bars = [b for b in (series or []) if _f(b.get("close")) is not None]
    warnings: List[str] = []
    if len(bars) < horizon_days + MIN_SAMPLES:
        return {"ok": False, "error": "insufficient_data",
                "message": f"有效日线 {len(bars)} 根，至少需要 {horizon_days + MIN_SAMPLES} 根"}
    if not (entry_expr or "").strip():
        return {"ok": False, "error": "empty_entry_expr", "message": "因子缺少入场表达式"}

    pairs: List[Dict[str, Any]] = []
    for i in range(len(bars) - horizon_days):
        c0 = _f(bars[i].get("close"))
        c1 = _f(bars[i + horizon_days].get("close"))
        if not c0 or not c1 or c0 <= 0:
            continue
        pairs.append({
            "date": str(bars[i].get("date") or ""),
            "signal": 1.0 if eval_signal_expr(entry_expr, bars, i) else 0.0,
            "fwd_pct": (c1 / c0 - 1.0) * 100.0,
        })
    if len(pairs) < MIN_SAMPLES:
        return {"ok": False, "error": "insufficient_samples",
                "message": f"可用配对样本 {len(pairs)} 个，至少需要 {MIN_SAMPLES} 个"}

    # ---- IC 序列（按时间分块，避免用全样本算一个虚高的相关） ----
    ic_values: List[float] = []
    for start in range(0, len(pairs), IC_CHUNK):
        chunk = pairs[start:start + IC_CHUNK]
        ic = _pearson([p["signal"] for p in chunk], [p["fwd_pct"] for p in chunk])
        if ic is not None:
            ic_values.append(round(ic, 6))
    ic_mean = _mean(ic_values)
    if ic_values and len(ic_values) > 1:
        m = sum(ic_values) / len(ic_values)
        std = math.sqrt(sum((v - m) ** 2 for v in ic_values) / len(ic_values))
        icir = round(m / std, 4) if std > 1e-9 else None
    else:
        icir = None

    # ---- 分层回测：触发组 vs 未触发组 ----
    hit = [p["fwd_pct"] for p in pairs if p["signal"] > 0]
    miss = [p["fwd_pct"] for p in pairs if p["signal"] == 0]
    hit_mean, miss_mean = _mean(hit), _mean(miss)
    layer_monotonic = bool(hit_mean is not None and miss_mean is not None and hit_mean > miss_mean)
    layer_edge = round(hit_mean - miss_mean, 4) if layer_monotonic else None
    # 单边上涨行情里「触发」和「未触发」都赚钱，差 0.1pct 也单调，但那不是信号有效，
    # 是 beta。要求有可辨认的超额区分度，宁可判证据不足也不误放行。
    MIN_LAYER_EDGE_PCT = 0.5
    if not hit:
        warnings.append("样本期内该因子从未触发，无法检验有效性")

    # ---- walk-forward：按时间切分，样本外才是真考场 ----
    split = int(len(pairs) * split_ratio)
    ins, outs = pairs[:split], pairs[split:]
    ins_hit = _mean([p["fwd_pct"] for p in ins if p["signal"] > 0])
    out_hit = _mean([p["fwd_pct"] for p in outs if p["signal"] > 0])
    out_ic = _pearson([p["signal"] for p in outs], [p["fwd_pct"] for p in outs])

    if ic_mean is None or out_hit is None:
        verdict = "证据不足"
        note = "样本不足或样本外无触发记录，不建议载入监控。"
    elif (ins_hit or 0) > 0 and out_hit <= 0:
        verdict = "疑似过拟合"
        note = "样本内赚钱、样本外转负，典型的参数拟合；不要载入监控。"
    elif not layer_monotonic:
        verdict = "无效（分层不单调）"
        note = "触发组未来收益并不高于未触发组，信号没有区分度。"
    elif (layer_edge or 0) < MIN_LAYER_EDGE_PCT:
        verdict = "证据不足（区分度不足）"
        note = (f"触发组只比未触发组高 {layer_edge} 个百分点，很可能是行情 beta 而非因子；"
                f"要求至少 {MIN_LAYER_EDGE_PCT} 个百分点。")
    elif ic_mean > 0.02 and out_hit > 0:
        verdict = "有效（样本外同向）"
        note = "IC 为正、分层有区分度且样本外收益同向；仍建议定期复检，因子会失效。"
    else:
        verdict = "证据不足"
        note = "IC 偏弱或样本外收益不足以支持有效性，建议扩大样本区间再验。"

    return {
        "ok": True,
        "samples": len(pairs),
        "horizon_days": horizon_days,
        "metrics": {
            "samples": len(pairs),
            "ic_mean": ic_mean,
            "icir": icir,
            "ic_chunks": len(ic_values),
            "layer_monotonic": layer_monotonic,
            "layer_edge_pct": layer_edge,
            "hit_mean_return_pct": hit_mean,
            "nonhit_mean_return_pct": miss_mean,
            "hit_samples": len(hit),
            "in_sample_return_pct": ins_hit,
        },
        "walk_forward": {
            "split_ratio": split_ratio,
            "split_date": outs[0]["date"] if outs else "",
            "in_sample_count": len(ins),
            "out_sample_count": len(outs),
            "in_sample_return_pct": ins_hit,
            "out_sample_return_pct": out_hit,
            "out_sample_ic": round(out_ic, 6) if out_ic is not None else None,
        },
        "verdict": verdict,
        "note": note,
        "warnings": warnings,
    }


def validate_saved_factor(code: str, factor_name: str,
                          horizon_days: int = DEFAULT_HORIZON_DAYS) -> Dict[str, Any]:
    """取已保存因子 + 日线，跑一次检验（供 API 调用）。"""
    from .stock_service import stock_service

    factor = stock_service._find_strategy_by_name(str(factor_name or "").strip())  # noqa: SLF001
    if not factor:
        return {"ok": False, "error": "factor_not_found", "message": f"找不到因子「{factor_name}」"}
    today = datetime.datetime.now()
    start = (today - datetime.timedelta(days=900)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    try:
        series = stock_service.get_k_line_data(code, start, end, "d", caller="factor_validation")
    except Exception as exc:  # noqa: BLE001
        logger.warning("factor_validation_kline_failed code=%s error=%r", code, exc)
        return {"ok": False, "error": "kline_failed", "message": f"获取日线失败：{exc}"}
    result = validate_factor(series, str(factor.get("entry_expr") or ""), horizon_days)
    if result.get("ok"):
        result.update({"code": code, "factor_name": factor_name,
                       "entry_expr": factor.get("entry_expr") or ""})
    return result
