import statistics
from typing import Any, Callable, Dict, List, Optional, Tuple


MetricProvider = Callable[[str], Dict[str, Any]]


class IndustryValuationTool:
    """Compare one stock valuation with peers in the same local industry group."""

    def __init__(self, min_samples: int = 3, max_peers: int = 12) -> None:
        self._min_samples = max(1, int(min_samples or 3))
        self._max_peers = max(self._min_samples, int(max_peers or 12))

    def evaluate(
        self,
        code: str,
        profiles: Dict[str, Any],
        metric_provider: MetricProvider,
        fallback: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        digits = self._digits(code)
        target_profile = self._profile_for(profiles, digits)
        industry = self._industry_of(target_profile)
        target_metrics = self._safe_metrics(metric_provider, digits)
        fallback_result = dict(fallback or {})
        fallback_result.update({k: v for k, v in target_metrics.items() if v is not None})

        if not industry:
            return self._fallback(fallback_result, "missing_industry", target_metrics)

        peer_codes = self._peer_codes(profiles, industry, digits)
        if not peer_codes:
            return self._fallback(fallback_result, "missing_industry_peers", target_metrics, industry)

        rows = []
        for peer_code in peer_codes[: self._max_peers]:
            metrics = target_metrics if peer_code == digits else self._safe_metrics(metric_provider, peer_code)
            if any(metrics.get(key) is not None for key in ("pe", "pb", "roe")):
                rows.append(metrics)

        stats = self._industry_stats(rows)
        sample_count = int(stats.get("sample_count") or 0)
        if sample_count < self._min_samples:
            return self._fallback(fallback_result, "insufficient_industry_sample", target_metrics, industry, stats)

        relative = self._relative_metrics(target_metrics, stats)
        score, reason = self._score(target_metrics, stats)
        zone = "fair"
        if score >= 2:
            zone = "undervalued"
            reason = "relative_to_industry_undervalued"
        elif score <= -1:
            zone = "overvalued"
            reason = "relative_to_industry_overvalued"
        elif any(target_metrics.get(key) is not None for key in ("pe", "pb", "roe")):
            reason = "relative_to_industry_fair"
        else:
            zone = "unknown"
            reason = "insufficient_metrics"

        return {
            "zone": zone,
            "reason": reason,
            "score": score,
            "pe": target_metrics.get("pe"),
            "pb": target_metrics.get("pb"),
            "roe": target_metrics.get("roe"),
            "industry": industry,
            "industry_sample_count": sample_count,
            "industry_stats": stats,
            "relative": relative,
            "method": "industry_relative",
        }

    def _fallback(
        self,
        fallback: Dict[str, Any],
        reason: str,
        target_metrics: Dict[str, Any],
        industry: Optional[str] = None,
        stats: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        result = dict(fallback or {})
        result.setdefault("zone", "unknown")
        result.setdefault("score", 0)
        result.setdefault("reason", reason)
        result.setdefault("method", "single_stock_absolute")
        result.setdefault("pe", target_metrics.get("pe"))
        result.setdefault("pb", target_metrics.get("pb"))
        result.setdefault("roe", target_metrics.get("roe"))
        if industry:
            result["industry"] = industry
        if stats:
            result["industry_stats"] = stats
            result["industry_sample_count"] = int(stats.get("sample_count") or 0)
        result["industry_fallback_reason"] = reason
        return result

    def _score(self, metrics: Dict[str, Any], stats: Dict[str, Any]) -> Tuple[int, str]:
        score = 0
        pe = self._to_float(metrics.get("pe"))
        pb = self._to_float(metrics.get("pb"))
        roe = self._to_float(metrics.get("roe"))
        pe_avg = self._to_float(stats.get("pe_avg"))
        pb_median = self._to_float(stats.get("pb_median"))
        roe_avg = self._to_float(stats.get("roe_avg"))

        if pe is not None and pe > 0 and pe_avg is not None and pe_avg > 0:
            if pe <= pe_avg * 0.8:
                score += 1
            elif pe >= pe_avg * 1.2:
                score -= 1
        if pb is not None and pb > 0 and pb_median is not None and pb_median > 0:
            if pb <= pb_median:
                score += 1
            elif pb >= pb_median * 1.2:
                score -= 1
        if roe is not None and roe_avg is not None:
            if roe >= roe_avg:
                score += 1
            elif roe_avg > 0 and roe < roe_avg * 0.8:
                score -= 1
        return score, "relative_to_industry_fair"

    def _industry_stats(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        pe_values = self._valid_values(rows, "pe", positive=True)
        pb_values = self._valid_values(rows, "pb", positive=True)
        roe_values = self._valid_values(rows, "roe", positive=False)
        sample_count = max(len(pe_values), len(pb_values), len(roe_values))
        return {
            "sample_count": sample_count,
            "pe_avg": self._avg(pe_values),
            "pe_median": self._median(pe_values),
            "pb_avg": self._avg(pb_values),
            "pb_median": self._median(pb_values),
            "roe_avg": self._avg(roe_values),
            "roe_median": self._median(roe_values),
        }

    def _relative_metrics(self, metrics: Dict[str, Any], stats: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "pe_vs_avg_pct": self._delta_pct(metrics.get("pe"), stats.get("pe_avg")),
            "pb_vs_median_pct": self._delta_pct(metrics.get("pb"), stats.get("pb_median")),
            "roe_vs_avg_pct": self._delta_pct(metrics.get("roe"), stats.get("roe_avg")),
        }

    def _peer_codes(self, profiles: Dict[str, Any], industry: str, target_digits: str) -> List[str]:
        peers = [target_digits]
        for raw_code, profile in (profiles or {}).items():
            digits = self._digits(raw_code)
            if digits == target_digits:
                continue
            if self._industry_of(profile) == industry:
                peers.append(digits)
        return peers

    def _profile_for(self, profiles: Dict[str, Any], digits: str) -> Dict[str, Any]:
        profile = (profiles or {}).get(digits) or {}
        return profile if isinstance(profile, dict) else {}

    def _industry_of(self, profile: Any) -> str:
        if not isinstance(profile, dict):
            return ""
        for key in ("industry", "Industry", "hangye", "行业", "琛屼笟"):
            value = str(profile.get(key) or "").strip()
            if value:
                return value
        return ""

    def _safe_metrics(self, provider: MetricProvider, code: str) -> Dict[str, Any]:
        try:
            raw = provider(code) or {}
        except Exception:
            raw = {}
        return {
            "code": self._digits(raw.get("code") or code),
            "pe": self._to_float(raw.get("pe")),
            "pb": self._to_float(raw.get("pb")),
            "roe": self._to_float(raw.get("roe")),
        }

    def _valid_values(self, rows: List[Dict[str, Any]], key: str, positive: bool) -> List[float]:
        values = []
        for row in rows:
            value = self._to_float(row.get(key))
            if value is None:
                continue
            if positive and value <= 0:
                continue
            values.append(value)
        return self._trim_outliers(values)

    def _trim_outliers(self, values: List[float]) -> List[float]:
        if len(values) < 6:
            return values
        ordered = sorted(values)
        drop = max(1, int(len(ordered) * 0.1))
        return ordered[drop:-drop] or ordered

    def _avg(self, values: List[float]) -> Optional[float]:
        return round(sum(values) / len(values), 4) if values else None

    def _median(self, values: List[float]) -> Optional[float]:
        return round(float(statistics.median(values)), 4) if values else None

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

    def _digits(self, code: Any) -> str:
        text = str(code or "").strip()
        digits = "".join(ch for ch in text if ch.isdigit())
        return digits[-6:] if len(digits) >= 6 else digits
