import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

from .market_data_tool import MarketDataTool

logger = logging.getLogger("app.company_info")


class CompanyInfoTool:
    """Fetch and cache company profile info used by industry-aware tools."""

    def __init__(self, cache_file: Path, market_tool: Optional[MarketDataTool] = None) -> None:
        self._cache_file = Path(cache_file)
        self._market_tool = market_tool or MarketDataTool()

    def get(self, code: str, refresh: bool = False) -> Dict[str, Any]:
        digits = self._digits(code)
        if not digits:
            return {}
        cache = self._load_cache()
        cached = cache.get(digits)
        if isinstance(cached, dict) and not refresh and self.has_valid_industry(cached):
            return cached

        info = self.fetch(digits)
        if info:
            cache[digits] = info
            self._save_cache(cache)
            return info
        return cached if isinstance(cached, dict) else {}

    def ensure_cached(self, code: str) -> Dict[str, Any]:
        return self.get(code, refresh=False)

    def fetch(self, code: str) -> Dict[str, Any]:
        digits = self._digits(code)
        if not digits:
            return {}
        try:
            df = self._market_tool.get_company_profile(digits)
            if df is None or getattr(df, "empty", True):
                return {}
            row = df.iloc[0].to_dict()
            columns = [str(col) for col in df.columns]
            return self._normalize_row(row, columns)
        except Exception as exc:
            logger.warning("company_info_fetch_failed code=%s error=%r", digits, exc)
            return {}

    def has_valid_industry(self, profile: Dict[str, Any]) -> bool:
        industry = str((profile or {}).get("industry") or (profile or {}).get("行业") or "").strip()
        if not industry:
            return False
        lowered = industry.lower()
        if "co., ltd" in lowered or "company" in lowered:
            return False
        if "股份有限公司" in industry or "有限责任公司" in industry:
            return False
        return True

    def _normalize_row(self, row: Dict[str, Any], columns: list[str]) -> Dict[str, Any]:
        def pick(candidates: list[str], fallback_index: Optional[int] = None) -> Any:
            for key in candidates:
                if key in row:
                    value = row.get(key)
                    if value not in (None, ""):
                        return value
            if fallback_index is not None and 0 <= fallback_index < len(columns):
                return row.get(columns[fallback_index])
            return None

        return {
            "org_name": pick(["org_name", "company_name", "full_name", "公司名称"], 0),
            "short_name": pick(["short_name", "股票简称", "A股简称"]),
            "code": str(pick(["code", "股票代码", "A股代码"]) or "").strip(),
            "industry": pick(["industry", "所属行业", "行业"], 11),
            "market": pick(["market", "所属市场"]),
            "legal_rep": pick(["legal_rep", "legal_person", "法人代表"], 12),
            "found_date": pick(["found_date", "established_date", "成立日期"], 14),
            "list_date": pick(["list_date", "上市日期"], 15),
            "reg_capital": pick(["reg_capital", "registered_capital", "注册资金"], 13),
            "website": pick(["website", "company_website", "官方网站"], 16),
            "email": pick(["email", "电子邮箱"], 17),
            "phone": pick(["phone", "联系电话"], 18),
            "address": pick(["address", "office_address", "registered_address", "办公地址", "注册地址"], 21),
            "main_business": pick(["main_business", "主营业务"], 23),
            "business_scope": pick(["business_scope", "经营范围"], 24),
            "profile": pick(["profile", "机构简介"], 25),
            "shareholders": [],
        }

    def _load_cache(self) -> Dict[str, Any]:
        if not self._cache_file.exists():
            return {}
        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("company_info_cache_load_failed path=%s error=%r", self._cache_file, exc)
            return {}

    def _save_cache(self, payload: Dict[str, Any]) -> None:
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._cache_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _digits(self, code: Any) -> str:
        digits = "".join(re.findall(r"\d", str(code or "")))
        return digits[-6:] if len(digits) >= 6 else ""
