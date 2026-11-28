"""行业估值中枢服务（Industry Valuation Center）。

为什么需要这一层
----------------
用户的原话：**「你算 pe、pb 的时候得看行业均位；不能不同行业进行比较，
每个行业商业模式都不一样。」**

这句话戳的是本项目过去一个真实的缺陷：

- 银行 PB 0.8 是"便宜"，半导体 PB 0.8 是"快要 bankrupt"；
- 白酒 PE 15 是"便宜"，软件 PE 15 是"高估"；
- 用一把尺子量所有行业，结论必然对一半错一半。

所以本服务的职责很简单：**把估值放回它自己的行业坐标系里再比较。**

产出三层口径（从粗到细）
------------------------
1. **行业中枢（median）**：该行业全部成分股 PE/PB 的**中位数**。
   用中位数而不是均值，因为均值会被少数极值股带偏
   （一个 PE 800 的微利股就能把整个行业的 PE 均值抬高 3 倍）。
2. **行业内分位（percentile）**：目标个股在**同行业成分股**里的估值分位。
   这是"这只票在本行业里贵不贵"的直接答案，比任何跨行业阈值都准。
3. **行业整体口径（index_pe/index_pb）**：申万官方发布的行业指数 PE/PB
   （市值加权口径），作为交叉校验与兜底——官方口径覆盖全部行业，
   而成分股中枢需要全市场快照，可能缺。

三者的分工：成分股中位数用于**判断相对贵贱**，官方行业 PE 用于
**交叉校验**，个股自身历史分位用于**判断相对自己的位置**。
三者都不替代彼此。

数据来源与降级链
----------------
- 全市场快照 `stock_zh_a_spot_em` → 一次性拿到所有 A 股的 PE/PB，
  在本地按申万二级行业成分股聚合出中位数。**这是主口径。**
- 申万 `index_analysis_daily_sw` / `sw_index_second_info` → 行业指数 PE/PB。
  **降级兜底。**（这两者走 swsresearch.com，不受东财限流影响）
- 两者都拿不到 → 返回空并明说"行业中枢不可用"，绝不假装有。

扫盘策略：全市场一次扫描 → 所有行业同时算出 → 长周期落盘
---------------------------------------------------------
东财 push2 接口有严格限流（短时间内请求过多会被临时封 IP），
所以**不能每个行业各抓一次**。本服务的做法是：

1. 一次全市场快照（约 5000 只 A 股的 PE/PB）+ 已缓存的行业成分股名单
   → **一次性算出所有 131 个申万二级行业的中枢**；
2. 结果按行业落盘，TTL 5 天（PE/PB 由财报驱动，日频足够，5 天更稳）。

这样限流恢复后只需成功一次，之后所有行业查询都是本地读缓存。
成分股名单缺失的行业走 `index_component_sw` 懒加载并同样缓存。

刻意不做的事
------------
- 不做行业内的"质量筛选"（不剔除亏损股再算中枢）：亏损股 PE 无意义
  但**它们的 PB 有效**，剔除会让金融/周期行业的中枢失真。PE 中位数
  单独剔除负值即可。
- 不提供跨行业的绝对估值排名：不同商业模式不可比，这是本服务存在的理由。
"""

from __future__ import annotations

import json
import logging
import math
import os
import statistics
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# 行业中枢缓存有效期：5 天。PE/PB 由财报驱动，日频足够；
# 但全市场快照要打东财接口（有严格限流），缓存长一点能大幅减少请求量。
CENTER_TTL_SEC = 5 * 86400
# 全市场快照缓存有效期：6 小时
SPOT_TTL_SEC = 6 * 3600
# 扫盘失败后的退避时间：东财限流首次约 5 分钟，反复触发可达 40 分钟以上。
# 失败后退避 10 分钟再试，避免越打越封。
SWEEP_RETRY_BACKOFF_SEC = 10 * 60

# 个股 PE/PB 的合理上限：超过说明盈利接近 0 或数据异常，纳入中位数会污染口径
_MAX_PE = 400.0
_MAX_PB = 60.0


def _f(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _median(values: Sequence[float]) -> Optional[float]:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    return float(statistics.median(clean))


def _trimmed_mean(values: Sequence[float], trim: float = 0.05) -> Optional[float]:
    """截尾均值：去掉两端各 trim 比例的极值后再平均。

    比中位数更平滑、比均值更抗污染，两者一起用可以互相印证。
    """
    clean = sorted(float(v) for v in values if v is not None)
    if not clean:
        return None
    if len(clean) < 10:
        return sum(clean) / len(clean)
    cut = int(len(clean) * trim)
    core = clean[cut:len(clean) - cut] if len(clean) - 2 * cut >= 3 else clean
    return sum(core) / len(core)


def _percentile_of(value: Optional[float], population: Sequence[float]) -> Optional[float]:
    """value 在 population 里的分位（0~1）。小于等于它的占比。"""
    if value is None or not population:
        return None
    clean = sorted(float(v) for v in population if v is not None)
    if not clean:
        return None
    below = sum(1 for v in clean if v <= value)
    return below / len(clean)


class IndustryValuationCenter:
    """行业估值中枢：算得出就用，算不出就明说。"""

    def __init__(
        self,
        config_dir: Optional[Path] = None,
        cache_file: Optional[Path] = None,
        spot_provider=None,
        sw_provider=None,
        components_provider=None,
        single_components_provider=None,
    ) -> None:
        self._config_dir = Path(config_dir) if config_dir else Path("config")
        self._cache_file = Path(cache_file) if cache_file else self._config_dir / "industry_valuation_cache.json"
        # 注入式依赖（测试友好，也避免本模块直接依赖网络）：
        #   spot_provider()             -> List[Dict] 全市场 {code, pe, pb}
        #   sw_provider()               -> Dict[code, {pe, pb, ...}] 申万行业整体口径
        #   components_provider()       -> Dict[code, List[str]] 行业成分股名单（全量）
        #   single_components_provider(code) -> List[str] 单个行业的成分股（按需补齐用）
        self._spot_provider = spot_provider
        self._sw_provider = sw_provider
        self._components_provider = components_provider
        self._single_components_provider = single_components_provider
        self._memory: Dict[str, Any] = {}
        self._sweep_lock = Lock()

    # ------------------------------------------------------------------
    # 缓存读写
    # ------------------------------------------------------------------
    def _load_cache(self) -> Dict[str, Any]:
        if self._memory:
            return self._memory
        try:
            if self._cache_file.exists():
                data = json.loads(self._cache_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._memory = data
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("industry_valuation_cache_read_failed error=%r", exc)
        self._memory = {}
        return self._memory

    def _save_cache(self, payload: Dict[str, Any]) -> None:
        try:
            self._config_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._cache_file)
            self._memory = payload
        except Exception as exc:  # noqa: BLE001
            logger.warning("industry_valuation_cache_write_failed error=%r", exc)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def get_industry_stats(
        self,
        industry_code: str,
        force: bool = False,
    ) -> Dict[str, Any]:
        """返回某个申万二级行业的估值中枢。

        返回字段：
          - pe_median / pb_median:  成分股中位数（主口径）
          - pe_trimmed / pb_trimmed: 成分股截尾均值（辅助）
          - pe_count / pb_count:   有效样本数
          - peer_pe_list / peer_pb_list: 成分股估值样本（用于算个股行业内分位）
          - index_pe / index_pb:   申万官方行业指数 PE/PB（交叉校验）
          - index_dividend_yield:  行业股息率（金融/公用事业的关键锚）
          - source:                数据来源标记
        """
        code = str(industry_code or "").strip()
        if not code:
            return {"ok": False, "error": "缺少行业代码"}

        cache = self._load_cache()
        stats = (cache.get("industries") or {}).get(code)
        if stats and not force:
            age = time.time() - float(cache.get("updated_at") or 0)
            if age < CENTER_TTL_SEC:
                return stats

        # 该行业没有新鲜缓存 → 触发一次全市场扫盘（一次算出所有行业）
        self._ensure_swept(force=force)

        stats = (self._load_cache().get("industries") or {}).get(code)
        if stats:
            if force:
                stats = dict(stats)
                stats.pop("stale", None)
            return stats

        # 扫盘结果里没有这个行业（行业较新 / 名单刚补齐）→ 单独算一次
        single = self._compute_single(code)
        if single.get("ok"):
            industries = dict(self._load_cache().get("industries") or {})
            industries[code] = single
            new_cache = dict(self._load_cache())
            new_cache["industries"] = industries
            self._save_cache(new_cache)
            return single

        # 扫盘失败 → 降级到官方行业口径
        fallback = self._fallback_index_stats(code)
        if fallback.get("ok"):
            return fallback
        return {
            "ok": False,
            "error": "行业估值中枢不可用（成分股中枢与官方行业口径均取不到）",
            "industry_code": code,
        }

    def _compute_single(self, industry_code: str) -> Dict[str, Any]:
        """只算一个行业的中枢（用于扫盘结果里缺该行业时的按需补算）。"""
        code = str(industry_code or "").strip()
        if not code:
            return {"ok": False, "error": "缺少行业代码"}

        components: List[str] = []
        if self._single_components_provider is not None:
            try:
                components = list(self._single_components_provider(code) or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning("single_components_failed code=%s error=%r", code, exc)
                components = []
        if not components:
            return {"ok": False, "error": "行业成分股不可用"}

        try:
            spot = self._spot_map()
        except Exception as exc:  # noqa: BLE001
            logger.warning("single_spot_failed code=%s error=%r", code, exc)
            return {"ok": False}
        if not spot:
            return {"ok": False}

        pe_list: List[float] = []
        pb_list: List[float] = []
        for member in components:
            row = spot.get(member)
            if not row:
                continue
            pe = row.get("pe")
            pb = row.get("pb")
            if pe is not None and 0 < pe <= _MAX_PE:
                pe_list.append(pe)
            if pb is not None and 0 < pb <= _MAX_PB:
                pb_list.append(pb)
        if not pe_list and not pb_list:
            return {"ok": False, "error": "行业成分股估值样本为空"}

        idx = self._index_rows().get(code) or {}
        return {
            "ok": True,
            "source": "sw_component_median",
            "industry_code": code,
            "industry_name": idx.get("industry_name") or "",
            "component_count": len(components),
            "pe_median": _round(_median(pe_list), 4),
            "pe_trimmed": _round(_trimmed_mean(pe_list), 4),
            "pe_count": len(pe_list),
            "pb_median": _round(_median(pb_list), 4),
            "pb_trimmed": _round(_trimmed_mean(pb_list), 4),
            "pb_count": len(pb_list),
            "index_pe": idx.get("index_pe"),
            "index_pb": idx.get("index_pb"),
            "index_dividend_yield": idx.get("index_dividend_yield"),
            "index_date": idx.get("index_date"),
            "peer_pe_list": sorted(pe_list),
            "peer_pb_list": sorted(pb_list),
            "updated_at": time.time(),
        }

    # ------------------------------------------------------------------
    # 全市场扫盘：一次成功算出所有行业
    # ------------------------------------------------------------------
    def _ensure_swept(self, force: bool = False) -> bool:
        """确保缓存里有新鲜的全行业中枢。返回是否扫成功。"""
        cache = self._load_cache()
        if not force:
            age = time.time() - float(cache.get("updated_at") or 0)
            if age < CENTER_TTL_SEC and cache.get("industries"):
                return True
        return bool(self._sweep_all())

    def _sweep_all(self) -> Dict[str, Any]:
        """一次全市场快照 + 成分股名单 → 所有行业中枢。"""
        if not self._sweep_lock.acquire(blocking=False):
            # 已有别的线程在扫盘，直接用缓存
            return self._load_cache().get("industries") or {}
        try:
            return self._do_sweep()
        finally:
            self._sweep_lock.release()

    def _do_sweep(self) -> Dict[str, Any]:
        cache = self._load_cache()
        # 已有足够新的结果就不用再扫
        age = time.time() - float(cache.get("updated_at") or 0)
        if age < CENTER_TTL_SEC and cache.get("industries"):
            return cache.get("industries") or {}

        # 上次扫盘失败过 → 退避，避免在限流期间反复打接口把封禁时间越拖越长
        failed_at = float(cache.get("sweep_failed_at") or 0)
        if failed_at and (time.time() - failed_at) < SWEEP_RETRY_BACKOFF_SEC:
            logger.info("industry_sweep_backoff remain=%.0fs",
                        SWEEP_RETRY_BACKOFF_SEC - (time.time() - failed_at))
            return {}

        components = self._components_map()
        if not components:
            logger.warning("industry_sweep_skipped reason=no_components")
            self._mark_sweep_failed()
            return {}

        try:
            spot = self._spot_map()
        except Exception as exc:  # noqa: BLE001
            logger.warning("industry_sweep_spot_failed error=%r", exc)
            self._mark_sweep_failed()
            return {}
        if not spot:
            logger.warning("industry_sweep_skipped reason=no_spot")
            self._mark_sweep_failed()
            return {}

        index_rows = self._index_rows()
        industries: Dict[str, Any] = {}
        for code, codes in components.items():
            if not codes:
                continue
            pe_list: List[float] = []
            pb_list: List[float] = []
            for member in codes:
                row = spot.get(member)
                if not row:
                    continue
                pe = row.get("pe")
                pb = row.get("pb")
                if pe is not None and 0 < pe <= _MAX_PE:
                    pe_list.append(pe)
                if pb is not None and 0 < pb <= _MAX_PB:
                    pb_list.append(pb)
            if not pe_list and not pb_list:
                continue
            idx = index_rows.get(code) or {}
            industries[code] = {
                "ok": True,
                "source": "sw_component_median",
                "industry_code": code,
                "industry_name": idx.get("industry_name") or "",
                "component_count": len(codes),
                "pe_median": _round(_median(pe_list), 4),
                "pe_trimmed": _round(_trimmed_mean(pe_list), 4),
                "pe_count": len(pe_list),
                "pb_median": _round(_median(pb_list), 4),
                "pb_trimmed": _round(_trimmed_mean(pb_list), 4),
                "pb_count": len(pb_list),
                "index_pe": idx.get("index_pe"),
                "index_pb": idx.get("index_pb"),
                "index_dividend_yield": idx.get("index_dividend_yield"),
                "index_date": idx.get("index_date"),
                "peer_pe_list": sorted(pe_list),
                "peer_pb_list": sorted(pb_list),
                "updated_at": time.time(),
            }

        if not industries:
            logger.warning("industry_sweep_empty")
            self._mark_sweep_failed()
            return {}

        new_cache = dict(cache)
        new_cache["industries"] = industries
        new_cache["updated_at"] = time.time()
        new_cache["sweep_at"] = time.time()
        new_cache["sweep_industry_count"] = len(industries)
        new_cache.pop("sweep_failed_at", None)
        self._save_cache(new_cache)
        logger.info("industry_sweep_done industries=%s", len(industries))
        return industries

    def _mark_sweep_failed(self) -> None:
        """记录扫盘失败时间，进入退避窗口。"""
        cache = dict(self._load_cache())
        cache["sweep_failed_at"] = time.time()
        self._save_cache(cache)

    # ------------------------------------------------------------------
    # 数据注入点
    # ------------------------------------------------------------------
    def _components_map(self) -> Dict[str, List[str]]:
        """全部申万二级行业的成分股名单。优先用已有缓存，缺失的懒加载。"""
        if self._components_provider is None:
            return {}
        try:
            data = self._components_provider() or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("industry_components_map_failed error=%r", exc)
            return {}
        out: Dict[str, List[str]] = {}
        for code, codes in (data or {}).items():
            if isinstance(codes, list) and codes:
                out[str(code)] = [str(c) for c in codes if str(c).strip()]
        return out

    def _spot_map(self) -> Dict[str, Dict[str, float]]:
        """全市场 PE/PB 快照（按 6 位代码索引），按日缓存。"""
        cache = self._load_cache()
        age = time.time() - float(cache.get("spot_updated_at") or 0)
        if age < SPOT_TTL_SEC and cache.get("spot"):
            return cache["spot"]

        if self._spot_provider is None:
            return cache.get("spot") or {}

        rows = self._spot_provider() or []
        spot: Dict[str, Dict[str, float]] = {}
        for row in rows:
            code = str(row.get("code") or "").strip()
            if not code:
                continue
            pe = _f(row.get("pe"))
            pb = _f(row.get("pb"))
            if pe is None and pb is None:
                continue
            spot[code] = {"pe": pe, "pb": pb}

        if spot:
            new_cache = dict(cache)
            new_cache["spot"] = spot
            new_cache["spot_updated_at"] = time.time()
            self._save_cache(new_cache)
            return spot
        return cache.get("spot") or {}

    def _index_rows(self) -> Dict[str, Dict[str, Any]]:
        """申万官方行业整体口径（交叉校验 + 兜底），统一成 index_* 字段名。"""
        if self._sw_provider is None:
            return {}
        try:
            raw = self._sw_provider() or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("sw_provider_failed error=%r", exc)
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for code, row in (raw or {}).items():
            if not isinstance(row, dict):
                continue
            out[str(code)] = {
                "industry_name": row.get("industry_name"),
                "index_pe": _f(row.get("index_pe")) if row.get("index_pe") is not None else _f(row.get("pe")),
                "index_pb": _f(row.get("index_pb")) if row.get("index_pb") is not None else _f(row.get("pb")),
                "index_dividend_yield": (
                    row.get("index_dividend_yield")
                    if row.get("index_dividend_yield") is not None
                    else row.get("dividend_yield")
                ),
                "index_date": row.get("index_date") or row.get("date"),
            }
        return out

    def _fallback_index_stats(self, industry_code: str) -> Dict[str, Any]:
        """降级到申万官方行业整体口径（成分股中枢不可用时）。

        注意字段口径差异：官方行业 PE 是**市值加权**（大市值股主导），
        成分股中枢是**中位数**（抗极值）。两者不一致时以中位为准，
        但官方口径仍然满足"按行业比、不跨行业比"的核心要求。
        """
        rows = self._index_rows()
        row = rows.get(str(industry_code))
        if not row:
            return {"ok": False}
        index_pe = _f(row.get("index_pe"))
        index_pb = _f(row.get("index_pb"))
        if index_pe is None and index_pb is None:
            return {"ok": False}
        return {
            "ok": True,
            "source": "sw_index_only",
            "industry_code": industry_code,
            "industry_name": row.get("industry_name"),
            "component_count": None,
            "pe_median": None,
            "pb_median": None,
            "pe_count": None,
            "pb_count": None,
            "peer_pe_list": [],
            "peer_pb_list": [],
            "index_pe": index_pe,
            "index_pb": index_pb,
            "index_dividend_yield": row.get("index_dividend_yield"),
            "index_date": row.get("index_date"),
            "note": "行业成分股中枢不可用，已降级为申万官方行业口径（市值加权，非中位）",
            "updated_at": time.time(),
        }


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


# ---------------------------------------------------------------------------
# 相对估值：把个股放进行业坐标系
# ---------------------------------------------------------------------------

def assess_industry_relative(
    pe: Optional[float],
    pb: Optional[float],
    industry_stats: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """判断个股相对**本行业**是贵还是便宜。

    这是"不能跨行业比较"的落地：所有判断都基于行业中枢，
    不再使用任何跨行业的绝对阈值。

    返回：
      - pe_vs_median_pct / pb_vs_median_pct: 相对行业中位的溢价/折价（%）
      - pe_percentile / pb_percentile:        在行业成分股里的分位（0=最低）
      - valuation_zone: cheap / fair / expensive（以行业中位为基准）
      - notes:  给 agent 读的解释
    """
    stats = industry_stats or {}
    notes: List[str] = []

    pe_med = _f(stats.get("pe_median"))
    pb_med = _f(stats.get("pb_median"))
    peer_pe = [float(v) for v in (stats.get("peer_pe_list") or []) if v is not None]
    peer_pb = [float(v) for v in (stats.get("peer_pb_list") or []) if v is not None]
    industry_name = str(stats.get("industry_name") or "") or "所属行业"

    if not pe_med and not pb_med:
        return {
            "ok": False,
            "error": "行业估值中枢不可用",
            "notes": ["行业中枢取不到，本次不给出行业内相对估值结论"],
        }

    pe_val = _f(pe)
    pb_val = _f(pb)

    pe_rel: Optional[float] = None
    pb_rel: Optional[float] = None
    if pe_val is not None and pe_med:
        pe_rel = round((pe_val / pe_med - 1.0) * 100.0, 2)
    if pb_val is not None and pb_med:
        pb_rel = round((pb_val / pb_med - 1.0) * 100.0, 2)

    pe_pct = _percentile_of(pe_val, peer_pe) if pe_val is not None and peer_pe else None
    pb_pct = _percentile_of(pb_val, peer_pb) if pb_val is not None and peer_pb else None

    # 以行业中位为基准划档：折价 15% 以上算便宜，溢价 15% 以上算贵
    rels = [r for r in (pe_rel, pb_rel) if r is not None]
    if rels:
        avg_rel = sum(rels) / len(rels)
        if avg_rel <= -15.0:
            zone = "cheap"
        elif avg_rel >= 15.0:
            zone = "expensive"
        else:
            zone = "fair"
    else:
        avg_rel = None
        zone = "unknown"

    if pe_rel is not None:
        direction = "折价" if pe_rel < 0 else "溢价"
        notes.append(
            f"PE {pe_val:.1f} 相对{industry_name}中位 {pe_med:.1f} {direction} {abs(pe_rel):.0f}%"
        )
    if pb_rel is not None:
        direction = "折价" if pb_rel < 0 else "溢价"
        notes.append(
            f"PB {pb_val:.2f} 相对{industry_name}中位 {pb_med:.2f} {direction} {abs(pb_rel):.0f}%"
        )
    if pe_pct is not None:
        notes.append(f"PE 在{industry_name} {len(peer_pe)} 只成分股中处 {pe_pct:.0%} 分位")
    if pb_pct is not None:
        notes.append(f"PB 在{industry_name} {len(peer_pb)} 只成分股中处 {pb_pct:.0%} 分位")

    index_pe = _f(stats.get("index_pe"))
    if index_pe and pe_med:
        # 官方行业口径（加权）与成分股中位数的偏离度：偏离大说明行业内分化严重
        gap = abs(index_pe / pe_med - 1.0) * 100.0
        if gap >= 60.0:
            notes.append(
                f"申万官方{industry_name}PE {index_pe:.1f} 与成分股中位 {pe_med:.1f} 偏离 {gap:.0f}%，"
                "行业内头部与中位分化明显，以中位口径为准更稳"
            )

    return {
        "ok": True,
        "industry_name": industry_name,
        "industry_code": stats.get("industry_code"),
        "pe_median": pe_med,
        "pb_median": pb_med,
        "pe_vs_median_pct": pe_rel,
        "pb_vs_median_pct": pb_rel,
        "pe_percentile": round(pe_pct, 4) if pe_pct is not None else None,
        "pb_percentile": round(pb_pct, 4) if pb_pct is not None else None,
        "avg_rel_pct": round(avg_rel, 2) if avg_rel is not None else None,
        "valuation_zone": zone,
        "source": stats.get("source"),
        "notes": notes,
    }
