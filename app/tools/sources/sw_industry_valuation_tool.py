import datetime
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .market_data_tool import MarketDataTool

logger = logging.getLogger("app.sw_industry_valuation")


class SwIndustryValuationTool:
    """SW second-level industry valuation and stock-industry mapping."""

    def __init__(self, cache_file: Path, market_tool: Optional[MarketDataTool] = None, ttl_sec: int = 86400) -> None:
        self._cache_file = Path(cache_file)
        self._market_tool = market_tool or MarketDataTool()
        self._ttl_sec = ttl_sec

    def evaluate(self, code: str, metrics: Dict[str, Any], fallback: Dict[str, Any], profile: Optional[Dict[str, Any]] = None, rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        digits = self._digits(code)
        if not digits:
            return dict(fallback or {})
        industry = self.get_stock_sw_second_industry(digits, profile=profile)
        if not industry:
            result = dict(fallback or {})
            result["sw_fallback_reason"] = "missing_sw_second_industry"
            return result
        valuation = self.get_sw_second_industry_valuation(str(industry.get("industry_code") or ""))
        if not valuation:
            result = dict(fallback or {})
            result.update({
                "industry": industry.get("industry_name"),
                "sw_industry": industry,
                "sw_fallback_reason": "missing_sw_industry_valuation",
            })
            return result

        stats = {
            "source": valuation.get("source"),
            "industry_system": "申万二级行业",
            "industry_code": valuation.get("industry_code"),
            "industry_name": valuation.get("industry_name"),
            "date": valuation.get("date"),
            "sample_count": valuation.get("component_count"),
            "pe_avg": valuation.get("pe"),
            "pe_median": None,
            "pb_avg": valuation.get("pb"),
            "pb_median": valuation.get("pb"),
            "dividend_yield": valuation.get("dividend_yield"),
            "turnover_rate": valuation.get("turnover_rate"),
            "market_value": valuation.get("market_value"),
        }
        relative = {
            "pe_vs_avg_pct": self._delta_pct(metrics.get("pe"), stats.get("pe_avg")),
            "pb_vs_median_pct": self._delta_pct(metrics.get("pb"), stats.get("pb_median")),
            "dividend_yield_vs_industry_pct": self._delta_pct(metrics.get("dividend_yield"), stats.get("dividend_yield")),
        }
        score_detail = self._weighted_score(relative, rules or {})
        score = score_detail.get("score", 0.0)
        zone = "fair"
        reason = "relative_to_sw_industry_fair"
        if score >= score_detail.get("undervalued_score", 0.35):
            zone = "undervalued"
            reason = "relative_to_sw_industry_undervalued"
        elif score <= score_detail.get("overvalued_score", -0.35):
            zone = "overvalued"
            reason = "relative_to_sw_industry_overvalued"
        result = dict(fallback or {})
        result.update({
            "zone": zone,
            "reason": reason,
            "score": score,
            "pe": self._to_float(metrics.get("pe")),
            "pb": self._to_float(metrics.get("pb")),
            "roe": self._to_float(metrics.get("roe")),
            "industry": valuation.get("industry_name") or industry.get("industry_name"),
            "industry_sample_count": valuation.get("component_count"),
            "industry_stats": stats,
            "relative": relative,
            "factor_score": score_detail,
            "method": "sw_second_industry_relative",
            "sw_industry": industry,
        })
        return result

    def get_stock_sw_second_industry(self, code: str, profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        digits = self._digits(code)
        cache = self._load_cache()
        mapping = cache.setdefault("stock_second_industry", {})
        cached = mapping.get(digits)
        if isinstance(cached, dict) and time.time() - float(cached.get("updated_at") or 0) < self._ttl_sec * 30:
            return cached

        industries = self._rank_candidate_industries(self._get_second_industries(cache), profile or {})
        for industry in industries:
            industry_code = str(industry.get("industry_code") or "").strip()
            if not industry_code:
                continue
            components = self._get_components(cache, industry_code)
            for row in components:
                if self._digits(row.get("code")) == digits:
                    payload = {
                        "code": digits,
                        "industry_code": industry_code,
                        "industry_name": industry.get("industry_name"),
                        "component_name": row.get("name"),
                        "component_weight": row.get("weight"),
                        "updated_at": time.time(),
                    }
                    mapping[digits] = payload
                    self._save_cache(cache)
                    return payload
        self._save_cache(cache)
        return {}

    def _rank_candidate_industries(self, industries: List[Dict[str, Any]], profile: Dict[str, Any]) -> List[Dict[str, Any]]:
        text = " ".join(str((profile or {}).get(key) or "") for key in ("industry", "main_business", "business_scope", "profile"))
        keyword_map = {
            "汽车": ["汽车零部件", "乘用车", "商用车", "摩托车及其他", "汽车服务"],
            "软件": ["软件开发", "IT服务Ⅱ", "计算机设备"],
            "信息技术": ["软件开发", "IT服务Ⅱ", "计算机设备"],
            "保险": ["保险Ⅱ"],
            "橡胶": ["橡胶"],
            "塑料": ["塑料"],
            "电气": ["电网设备", "电机Ⅱ", "电池", "光伏设备"],
            "电子": ["消费电子", "半导体", "元件", "光学光电子"],
            "银行": ["银行"],
        }
        preferred_names: List[str] = []
        for key, names in keyword_map.items():
            if key in text:
                preferred_names.extend(names)
        if not preferred_names:
            return industries
        preferred = [row for row in industries if row.get("industry_name") in preferred_names]
        rest = [row for row in industries if row.get("industry_name") not in preferred_names]
        return preferred + rest

    def get_sw_second_industry_valuation(self, industry_code: str) -> Dict[str, Any]:
        normalized_code = self._industry_code(industry_code)
        if not normalized_code:
            return {}
        cache = self._load_cache()
        valuations = cache.setdefault("sw_second_valuation", {})
        cached = valuations.get(normalized_code)
        if isinstance(cached, dict) and time.time() - float(cached.get("updated_at") or 0) < self._ttl_sec:
            return cached

        rows = self._fetch_sw_second_valuation_rows()
        for row in rows:
            if self._industry_code(row.get("industry_code")) == normalized_code:
                payload = dict(row)
                payload["updated_at"] = time.time()
                valuations[normalized_code] = payload
                self._save_cache(cache)
                return payload
        self._save_cache(cache)
        return {}

    def _fetch_sw_second_valuation_rows(self) -> List[Dict[str, Any]]:
        today = datetime.datetime.now().date()
        for offset in range(0, 16):
            date_text = (today - datetime.timedelta(days=offset)).strftime("%Y%m%d")
            try:
                df = self._market_tool.get_sw_index_analysis_daily("二级行业", date_text, date_text)
                if df is None or getattr(df, "empty", True):
                    continue
                rows = []
                for row in df.to_dict("records"):
                    rows.append({
                        "source": "akshare.index_analysis_daily_sw",
                        "industry_code": self._industry_code(row.get("指数代码")),
                        "industry_name": str(row.get("指数名称") or "").strip(),
                        "date": str(row.get("发布日期") or date_text),
                        "pe": self._to_float(row.get("市盈率")),
                        "pb": self._to_float(row.get("市净率")),
                        "dividend_yield": self._to_float(row.get("股息率")),
                        "turnover_rate": self._to_float(row.get("换手率")),
                        "market_value": self._to_float(row.get("流通市值")),
                        "component_count": None,
                    })
                return rows
            except Exception as exc:
                logger.info("sw_second_valuation_fetch_failed date=%s error=%r", date_text, exc)
        return []

    def _get_second_industries(self, cache: Dict[str, Any]) -> List[Dict[str, Any]]:
        cached = cache.get("sw_second_industries")
        if isinstance(cached, dict) and time.time() - float(cached.get("updated_at") or 0) < self._ttl_sec * 7:
            rows = cached.get("rows")
            if isinstance(rows, list) and rows:
                return rows
        rows = []
        try:
            df = self._market_tool.get_sw_index_realtime("二级行业")
            for row in df.to_dict("records"):
                rows.append({
                    "industry_code": self._industry_code(row.get("指数代码")),
                    "industry_name": str(row.get("指数名称") or "").strip(),
                })
        except Exception as exc:
            logger.warning("sw_second_industry_list_failed error=%r", exc)
        cache["sw_second_industries"] = {"updated_at": time.time(), "rows": rows}
        self._save_cache(cache)
        return rows

    def _get_components(self, cache: Dict[str, Any], industry_code: str) -> List[Dict[str, Any]]:
        normalized_code = self._industry_code(industry_code)
        components_cache = cache.setdefault("sw_second_components", {})
        cached = components_cache.get(normalized_code)
        if isinstance(cached, dict) and time.time() - float(cached.get("updated_at") or 0) < self._ttl_sec * 30:
            rows = cached.get("rows")
            if isinstance(rows, list):
                return rows
        rows = []
        try:
            df = self._market_tool.get_sw_index_component(normalized_code)
            for row in df.to_dict("records"):
                rows.append({
                    "code": self._digits(row.get("证券代码")),
                    "name": str(row.get("证券名称") or "").strip(),
                    "weight": self._to_float(row.get("最新权重")),
                    "date": str(row.get("计入日期") or "").strip(),
                })
        except Exception as exc:
            logger.info("sw_second_component_fetch_failed industry=%s error=%r", normalized_code, exc)
        components_cache[normalized_code] = {"updated_at": time.time(), "rows": rows}
        self._save_cache(cache)
        return rows

    def _weighted_score(self, relative: Dict[str, Any], rules: Dict[str, Any]) -> Dict[str, Any]:
        sw_rules = rules.get("sw_second_industry") if isinstance(rules.get("sw_second_industry"), dict) else {}
        weights = sw_rules.get("weights") if isinstance(sw_rules.get("weights"), dict) else {}
        thresholds = sw_rules.get("thresholds") if isinstance(sw_rules.get("thresholds"), dict) else {}
        normalized_weights = self._normalize_weights({
            "pe": self._to_float(weights.get("pe")) if weights else 0.45,
            "pb": self._to_float(weights.get("pb")) if weights else 0.45,
            "dividend_yield": self._to_float(weights.get("dividend_yield")) if weights else 0.1,
        })
        pe_score = self._factor_score(
            relative.get("pe_vs_avg_pct"),
            self._to_float(thresholds.get("pe_discount_pct")) or -20.0,
            self._to_float(thresholds.get("pe_premium_pct")) or 20.0,
            lower_is_better=True,
        )
        pb_score = self._factor_score(
            relative.get("pb_vs_median_pct"),
            self._to_float(thresholds.get("pb_discount_pct")) or -20.0,
            self._to_float(thresholds.get("pb_premium_pct")) or 20.0,
            lower_is_better=True,
        )
        dividend_score = self._factor_score(
            relative.get("dividend_yield_vs_industry_pct"),
            self._to_float(thresholds.get("dividend_yield_discount_pct")) or -20.0,
            self._to_float(thresholds.get("dividend_yield_premium_pct")) or 20.0,
            lower_is_better=False,
        )
        factor_scores = {"pe": pe_score, "pb": pb_score, "dividend_yield": dividend_score}
        total_weight = sum(weight for key, weight in normalized_weights.items() if factor_scores.get(key) is not None)
        if total_weight <= 0:
            score = 0.0
        else:
            score = sum((factor_scores[key] or 0.0) * weight for key, weight in normalized_weights.items()) / total_weight
        return {
            "score": round(score, 4),
            "weights": normalized_weights,
            "factors": factor_scores,
            "undervalued_score": self._to_float(thresholds.get("undervalued_score")) or 0.35,
            "overvalued_score": self._to_float(thresholds.get("overvalued_score")) or -0.35,
        }

    def _factor_score(self, value: Any, cheap_threshold: float, expensive_threshold: float, lower_is_better: bool) -> Optional[float]:
        pct = self._to_float(value)
        if pct is None:
            return None
        if lower_is_better:
            if pct <= cheap_threshold:
                return 1.0
            if pct >= expensive_threshold:
                return -1.0
        else:
            if pct >= expensive_threshold:
                return 1.0
            if pct <= cheap_threshold:
                return -1.0
        return 0.0

    def _normalize_weights(self, weights: Dict[str, Optional[float]]) -> Dict[str, float]:
        cleaned = {key: max(0.0, float(value or 0.0)) for key, value in weights.items()}
        total = sum(cleaned.values())
        if total <= 0:
            return {"pe": 0.45, "pb": 0.45, "dividend_yield": 0.1}
        return {key: round(value / total, 4) for key, value in cleaned.items()}

    def _load_cache(self) -> Dict[str, Any]:
        if not self._cache_file.exists():
            return {}
        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_cache(self, payload: Dict[str, Any]) -> None:
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._cache_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _delta_pct(self, value: Any, baseline: Any) -> Optional[float]:
        current = self._to_float(value)
        base = self._to_float(baseline)
        if current is None or base is None or base == 0:
            return None
        return round((current - base) / base * 100.0, 2)

    def _to_float(self, value: Any) -> Optional[float]:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except Exception:
            return None

    def _digits(self, value: Any) -> str:
        digits = "".join(ch for ch in str(value or "") if ch.isdigit())
        return digits[-6:] if len(digits) >= 6 else digits

    def _industry_code(self, value: Any) -> str:
        digits = "".join(ch for ch in str(value or "") if ch.isdigit())
        return digits[:6] if len(digits) >= 6 else digits
