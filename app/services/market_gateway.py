"""行情源统一网关：能力矩阵 + 数据质量闸门 + 口径交叉校验。

为什么必须有这一层
------------------
「再加一个兜底源」听起来是加法，实际是乘法风险。每加一个源都会引入三类事故：

1. **用错源**：让只覆盖 A 股股票的源去取 ETF，白等一次超时还必然失败。
   2026-11-26 实测星耀对基金返回空、baostock 根本不含基金。
2. **口径混**：不同源的成交量单位（手/股）、复权方式（前复权/不复权）不一致。
   混进同一条指标序列后，「放量」「量比」「偏离度」算出来全是错的，
   而且**不会报错**——只会得到看起来合理的错误结论。
   实测：同一天 159934，腾讯 1,178,501（手）vs 新浪 117,850,112（股），差 100 倍；
   东财 f56 同样是手，项目原先直接当股用了。
3. **静默降级**：某个源返回残缺数据（只有 3 根），下游算出 MA20=None，
   信号凭空消失，没有任何告警。周线策略要 MA60，3 根等于没有。

所以这里把三件事变成**数据**而不是靠记性：
- ``SOURCE_SPECS``：每个源支持哪些资产、哪些频率、什么单位、是否含当日；
- ``plan_for()``：给定资产与频率，给出「该试哪些源、按什么顺序」；
- ``validate()``：拿到结果后判它够不够用，不够就换下一个源；
- ``cross_check()``：两个源都取到时比对收盘价，偏差过大就把冲突暴露出来。

⚠️ 单位口径的唯一约定：**项目内部一律「股」+「前复权」**。
任何源返回「手」的，必须在各自的 fetch 里乘 100（腾讯、东财已处理）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

ASSET_STOCK = "stock"
ASSET_FUND = "fund"
ASSET_INDEX = "index"

# 场内基金 / ETF / LOF 的代码前缀（深市 15/16/18，沪市 50/51/52/56/58）
FUND_PREFIXES = ("15", "16", "18", "50", "51", "52", "56", "58")

# 分钟级频率集合，用于区分「日/周/月」和「分钟」两套约束
DAILY_FREQS = ("d", "w", "m")


@dataclass(frozen=True)
class SourceSpec:
    """一个行情源的静态能力声明。字段都是实测出来的，不是照文档抄的。"""

    key: str
    label: str
    assets: FrozenSet[str]
    freqs: FrozenSet[str]
    volume_unit: str          # "share"（股，可直接用）/ "lot"（手，需 ×100）
    adjust: str               # "qfq" 前复权 / "none" 不复权
    has_today: bool           # 是否包含当日未收盘的 bar
    priority: int             # 越小越先试
    note: str = ""

    def supports(self, asset: str, freq: str) -> bool:
        return asset in self.assets and freq in self.freqs


SOURCE_SPECS: Tuple[SourceSpec, ...] = (
    SourceSpec("amazingdata", "星耀数智", frozenset({ASSET_STOCK}),
               frozenset({"d", "w", "m", "1", "5", "15", "30", "60"}),
               "share", "qfq", True, 10,
               "付费主源，股票覆盖全周期；对场内基金返回空"),
    SourceSpec("xueqiu", "雪球", frozenset({ASSET_STOCK}),
               frozenset({"d", "w", "m", "1", "5d"}),
               "share", "qfq", False, 20,
               "与 baostock 前复权口径一致（差异<0.04%）；不含当日；仅股票"),
    SourceSpec("tencent", "腾讯", frozenset({ASSET_STOCK, ASSET_FUND}),
               frozenset({"d", "w", "m"}),
               "lot", "qfq", True, 30,
               "股票与基金通吃、含当日、速度最快；⚠️ volume 单位是手"),
    SourceSpec("sina", "新浪", frozenset({ASSET_STOCK, ASSET_FUND}),
               frozenset({"d"}),
               "share", "none", False, 40,
               "仅日线、不复权、不含当日；作为腾讯失败后的最后一道"),
    SourceSpec("baostock", "baostock", frozenset({ASSET_STOCK}),
               frozenset({"d", "w", "m"}),
               "share", "qfq", False, 50,
               "只覆盖 A 股股票，不含 ETF/LOF；登录不稳定"),
    SourceSpec("eastmoney", "东财", frozenset({ASSET_STOCK, ASSET_INDEX}),
               frozenset({"d", "w", "m", "1", "5", "15", "30", "60"}),
               "lot", "qfq", True, 60,
               "⚠️ push2his 对基金 secid 直接断连；限流频繁；f56 单位是手"),
    SourceSpec("akshare", "akshare", frozenset({ASSET_STOCK}),
               frozenset({"d", "w", "m"}),
               "share", "qfq", False, 70,
               "后端也是东财，东财挂了它必挂；仅作最后兜底"),
)

_SPEC_BY_KEY: Dict[str, SourceSpec] = {spec.key: spec for spec in SOURCE_SPECS}


def spec_of(key: str) -> Optional[SourceSpec]:
    return _SPEC_BY_KEY.get(key)


def detect_asset(code: str) -> str:
    """从代码推断资产类型。基金前缀 → fund，其余按股票处理。"""
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) >= 6 and digits[:2] in FUND_PREFIXES:
        return ASSET_FUND
    return ASSET_STOCK


def is_data_only(code: str) -> bool:
    """该标的是否只提供行情数据、不进策略/价值/风控体系。

    判定门槛很低但很关键：基金没有自己的 PE/PB、没有财报、没有「公司质量」，
    个股那套框架对它是错的尺子。硬套的后果不是报错，而是**输出一堆看着专业的假结论**
    （实测曾对黄金ETF 输出「戴维斯双击条件初步具备」「安全边际充足」）。

    开关在 ``decision_rules.funds.data_only``，默认 True。
    关掉它可以让基金重新走个股框架——但那是明知故犯，别在出问题时图省事这么干。
    """
    if detect_asset(code) != ASSET_FUND:
        return False
    try:
        from ..core import decision_rules

        cfg = decision_rules.section("funds") or {}
        return bool(cfg.get("data_only", True))
    except Exception:  # noqa: BLE001
        # 配置读不到时选择「只给数据」——宁可少给结论，不给错结论
        return True


def plan_for(asset: str, freq: str) -> List[SourceSpec]:
    """给定资产与频率，返回按优先级排好的候选源（已过滤掉不支持的源）。

    这一步的价值是**不问不该问的源**：取 ETF 日线时，星耀/雪球/baostock/akshare
    全都不在列表里，不会为了它们各等一次超时。
    """
    freq = str(freq or "d").strip().lower()
    if freq in ("week",):
        freq = "w"
    elif freq in ("month",):
        freq = "m"
    elif freq in ("day",):
        freq = "d"
    candidates = [s for s in SOURCE_SPECS if s.supports(asset, freq)]
    return sorted(candidates, key=lambda s: s.priority)


# ---------------- 质量闸门 ----------------

_MIN_BARS = {"w": 20, "m": 12}     # 周线要 MA20、月线要 MA12 才算有意义的序列
_DEFAULT_MIN_BARS = 5


def min_bars_for(freq: str, explicit_start: bool = False) -> int:
    """该频率下「多少根才算能用」。

    周/月线的下限不是随便定的：长线策略在周线上跑 MA60，
    少于 60 根就什么都算不出来；这里取 20/12 作为「至少能出信号」的门槛，
    真正要 MA60 的地方由调用方自己再判。
    """
    freq = str(freq or "d").strip().lower()
    if freq in _MIN_BARS:
        return _MIN_BARS[freq]
    return _DEFAULT_MIN_BARS


def validate(records: Sequence[Dict[str, Any]], freq: str,
             explicit_start: bool = False) -> Tuple[bool, str]:
    """拿到一个源的原始结果后，判断它够不够用。返回 (是否可用, 原因)。

    拦的是「看起来成功、其实没用」的返回：条数太少、收盘价全空、
    日期全都一样（有些源会重复吐同一根）。
    """
    if not records:
        return False, "空结果"

    closes = []
    dates = set()
    for row in records:
        if not isinstance(row, dict):
            continue
        close = row.get("close")
        try:
            if close is not None:
                closes.append(float(close))
        except (TypeError, ValueError):
            pass
        day = str(row.get("date") or "")[:10]
        if day:
            dates.add(day)

    if not closes:
        return False, "收盘价全部缺失"
    if len(dates) < 2 and len(records) > 1:
        return False, "日期重复，疑似只返回了一根"

    need = min_bars_for(freq, explicit_start)
    if len(records) < need:
        return False, f"仅 {len(records)} 根，少于 {freq} 频率的下限 {need} 根"
    if any(c <= 0 for c in closes[-5:]):
        return False, "最近几根收盘价出现 0 或负值"
    return True, "ok"


# ---------------- 口径交叉校验 ----------------

def cross_check(left: Sequence[Dict[str, Any]], right: Sequence[Dict[str, Any]],
                left_name: str = "A", right_name: str = "B",
                tolerance_pct: float = 1.0) -> Dict[str, Any]:
    """比对两个源在**同一天**的收盘价与成交量，把口径冲突显式暴露出来。

    这是给自检/运维用的，不参与日常取数（它需要多打一次请求）。
    正常情况下两个源前复权口径一致，收盘价偏差应远小于 1%；
    如果偏差很大，通常是复权方式不同（前复权 vs 不复权）或交易日错位。
    成交量的比值则直接反映单位问题：约 100 倍 = 一个按手一个按股。
    """
    def index(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        out = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            day = str(row.get("date") or "")[:10]
            if day:
                out[day] = row
        return out

    li, ri = index(left), index(right)
    common = sorted(set(li) & set(ri))
    if not common:
        return {"ok": False, "error": "两个源没有共同交易日，无法比对",
                "left_days": len(li), "right_days": len(ri)}

    price_diffs: List[Dict[str, Any]] = []
    vol_ratios: List[float] = []
    for day in common[-20:]:
        try:
            lp = float(li[day].get("close"))
            rp = float(ri[day].get("close"))
            if rp:
                diff = (lp - rp) / rp * 100.0
                if abs(diff) > tolerance_pct:
                    price_diffs.append({"date": day, "left": lp, "right": rp,
                                        "diff_pct": round(diff, 3)})
        except (TypeError, ValueError):
            pass
        try:
            lv = float(li[day].get("volume"))
            rv = float(ri[day].get("volume"))
            if lv and rv:
                vol_ratios.append(lv / rv)
        except (TypeError, ValueError):
            pass

    median_ratio = None
    if vol_ratios:
        ordered = sorted(vol_ratios)
        median_ratio = round(ordered[len(ordered) // 2], 3)

    verdict = "ok"
    hint = ""
    if price_diffs:
        verdict = "price_mismatch"
        hint = (f"有 {len(price_diffs)} 天收盘价偏差超过 {tolerance_pct}%，"
                f"可能是复权方式不同（前复权 vs 不复权）或交易日错位")
    elif median_ratio is not None and (median_ratio > 50 or median_ratio < 0.02):
        verdict = "volume_unit_mismatch"
        hint = (f"成交量中位比值 {median_ratio}，接近 100 或 0.01 说明一方按「手」一方按「股」，"
                f"混用会让放量/量比判断差两个数量级")

    return {
        "ok": verdict == "ok",
        "verdict": verdict,
        "hint": hint,
        "left": left_name,
        "right": right_name,
        "common_days": len(common),
        "price_outliers": price_diffs[:5],
        "volume_median_ratio": median_ratio,
    }


def describe_sources() -> List[Dict[str, Any]]:
    """能力矩阵的可读版，供 /api/stock/data_sources 与自检使用。"""
    return [
        {
            "key": s.key,
            "label": s.label,
            "assets": sorted(s.assets),
            "freqs": sorted(s.freqs),
            "volume_unit": s.volume_unit,
            "adjust": s.adjust,
            "has_today": s.has_today,
            "priority": s.priority,
            "note": s.note,
        }
        for s in sorted(SOURCE_SPECS, key=lambda x: x.priority)
    ]
