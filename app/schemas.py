from typing import Literal, Optional, List, Union
from pydantic import BaseModel


class ModelConfig(BaseModel):
    provider: Literal["openai", "openai_compat", "anthropic", "gemini", "deepseek", "modelscope"]
    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    # 可选：SearXNG 基址，如 http://localhost:8080
    searx_base_url: Optional[str] = None
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    use_proxy: bool = False
    proxy_url: Optional[str] = None
    system_prompt: Optional[str] = None
    custom_models: List[str] = []
    
    # 视觉模型配置 (独立于主模型)
    vision_provider: Optional[Literal["openai", "openai_compat", "anthropic", "gemini", "deepseek", "modelscope"]] = "openai_compat"
    vision_model: Optional[str] = "qwen3-vl-flash"
    vision_api_key: Optional[str] = None  # 如果为空，默认使用 api_key
    vision_base_url: Optional[str] = None # 如果为空，默认使用 base_url


# 早期 MCP / A2A 原型的 19 个请求响应 schema 已于 2026-11-15 清理：
# 全库静态扫描（app / tests / scripts）零引用，属死代码。
# 原文件备份在 .workbuddy/backups/dead-code-20261115/schemas.py，需要时按类挑回。


class EquGetRequest(BaseModel):
    secID: Optional[Union[str, List[str]]] = ""
    ticker: Optional[Union[str, List[str]]] = ""
    equTypeCD: Optional[Union[str, List[str]]] = "A"
    listStatusCD: Optional[Union[str, List[str]]] = ""
    exchangeCD: Optional[str] = ""
    ListSectorCD: Optional[Union[int, List[int], str]] = ""
    field: Optional[Union[str, List[str]]] = ""
    pandas: Optional[str] = "1"


class FavoriteRequest(BaseModel):
    secID: str
    shares: Optional[int] = None
    cost: Optional[float] = None
    horizon: Optional[str] = "long"
    buy_date: Optional[str] = None
    name: Optional[str] = None
    # 自选分组与备注：纯展示字段，便于把「持仓/观察/试仓」分开
    group: Optional[str] = None
    note: Optional[str] = None
    # 四池分类：dividend / catalyst / long_track / value；空串=未分类
    pool: Optional[str] = None


class SettingsRequest(BaseModel):
    total_capital: float = 0.0
    # None = 本次不修改（保持原值）；>=0 = 用户显式告知的账户可用资金（0 就是满仓）
    available_cash: Optional[float] = None
    investment_style: Optional[str] = None  # long | mid_long | swing
    monitor_enabled: Optional[bool] = None
    monitor_interval_sec: Optional[int] = None
