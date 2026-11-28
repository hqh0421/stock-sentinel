"""实例级密钥覆盖（唯一入口）。

为什么要这一层
--------------
改造前 `config/models.json`（模型 API Key）与 `config/user_settings.json`
（钉钉 webhook / 加签密钥）被 git 跟踪且含明文；`stock_service.py` 里甚至把一整串
雪球 Cookie 硬编码成默认值。这些文件一旦进过版本库，历史里就永远留着。

现在的约定：
- 仓库里只放 `*.example.json` 模板（值为空），真实值一律来自环境变量或本地未跟踪文件；
- 环境变量优先级最高，且**不会被写回磁盘**（见 config_loader.save_config）；
- 所有需要密钥的服务都经本模块做覆盖，不各自读 os.environ。

支持的环境变量：
    MONITOR_STOCK_LLM_API_KEY / MONITOR_STOCK_LLM_VISION_API_KEY
    MONITOR_STOCK_DINGTALK_WEBHOOK_URL / MONITOR_STOCK_DINGTALK_SECRET
    MONITOR_STOCK_XUEQIU_COOKIE / MONITOR_STOCK_XUEQIU_USER_AGENT
    MONITOR_STOCK_MODELS_FILE （模型配置文件路径，可指向仓库外）
    MONITOR_STOCK_RULES_FILE  （决策规则文件路径）
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

ENV_KEYS: Dict[str, str] = {
    "llm_api_key": "MONITOR_STOCK_LLM_API_KEY",
    "llm_vision_api_key": "MONITOR_STOCK_LLM_VISION_API_KEY",
    "dingtalk_webhook_url": "MONITOR_STOCK_DINGTALK_WEBHOOK_URL",
    "dingtalk_secret": "MONITOR_STOCK_DINGTALK_SECRET",
    "xueqiu_cookie": "MONITOR_STOCK_XUEQIU_COOKIE",
    "xueqiu_user_agent": "MONITOR_STOCK_XUEQIU_USER_AGENT",
}

SETTING_FIELDS = ("dingtalk_webhook_url", "dingtalk_secret", "xueqiu_cookie", "xueqiu_user_agent")


def env_value(field: str) -> Optional[str]:
    name = ENV_KEYS.get(field)
    if not name:
        return None
    value = os.environ.get(name, "").strip()
    return value or None


def overlay_secret_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """把环境变量覆盖到用户设置副本上（不改原 dict，不落盘）。"""
    out = dict(settings or {})
    for field in SETTING_FIELDS:
        value = env_value(field)
        if value:
            out[field] = value
    return out


def overlay_model_keys(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把环境变量覆盖到模型配置字典上。"""
    out = dict(payload or {})
    api_key = env_value("llm_api_key")
    vision_key = env_value("llm_vision_api_key")
    if api_key:
        out["api_key"] = api_key
    if vision_key:
        out["vision_api_key"] = vision_key
    return out


def secret_sources(settings: Dict[str, Any]) -> Dict[str, str]:
    """只回报来源（env / local / none），绝不回报值，供运维自检。"""
    result: Dict[str, str] = {}
    for field in SETTING_FIELDS + ("llm_api_key", "llm_vision_api_key"):
        if env_value(field):
            result[field] = "env"
        elif field in SETTING_FIELDS and str((settings or {}).get(field) or "").strip():
            result[field] = "local"
        else:
            result[field] = "local_or_none"
    return result
