"""模型配置读写（密钥不落版本库）。

约定：
- 真实配置文件默认 `config/models.json`，已被 .gitignore 忽略；可用
  `MONITOR_STOCK_MODELS_FILE` 指向仓库外的路径（推荐部署方式）。
- 仓库里只保留 `config/models.example.json` 模板，值一律为空。
- 环境变量 `MONITOR_STOCK_LLM_API_KEY` 优先级最高，且 **save_config 不会把它写回磁盘**，
  避免出现「环境变量被固化进本地文件」这种二次泄露。
"""

from pathlib import Path
import json
import logging
import os

from .core.secrets import env_value, overlay_model_keys
from .schemas import ModelConfig

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
EXAMPLE_PATH = CONFIG_DIR / "models.example.json"


def ensure_config_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def config_path() -> Path:
    override = os.environ.get("MONITOR_STOCK_MODELS_FILE", "").strip()
    return Path(override) if override else CONFIG_DIR / "models.json"


def _default_cfg() -> ModelConfig:
    return ModelConfig(
        provider="openai",
        api_key="",
        model="gpt-4o-mini",
        temperature=0.2,
        base_url=None,
        searx_base_url=None,
        system_prompt="你是一个有帮助的AI助手，擅长简洁准确地回答问题。",
        custom_models=[],
    )


def _read_disk_payload() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型配置解析失败，使用默认值 path=%s error=%r", path, exc)
        return {}


def load_config() -> ModelConfig:
    ensure_config_dirs()
    path = config_path()
    if not path.exists():
        # 首次运行：从模板复制一份空配置，绝不凭空写入任何密钥
        payload = {}
        if EXAMPLE_PATH.exists():
            try:
                payload = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001
                payload = {}
        path.write_text(json.dumps(payload or _default_cfg().model_dump(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        _chmod_private(path)
    payload = _read_disk_payload()
    payload = overlay_model_keys(payload)
    try:
        return ModelConfig(**payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型配置字段不兼容，回退默认值 error=%r", exc)
        cfg = _default_cfg()
        cfg.api_key = env_value("llm_api_key") or ""
        return cfg


def save_config(cfg: ModelConfig) -> None:
    """保存非密钥字段；环境变量提供的密钥不落盘，磁盘上原有的值保持不变。"""
    ensure_config_dirs()
    path = config_path()
    payload = cfg.model_dump()
    disk = _read_disk_payload()
    if env_value("llm_api_key"):
        payload["api_key"] = disk.get("api_key", "")
    if env_value("llm_vision_api_key"):
        payload["vision_api_key"] = disk.get("vision_api_key", "")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _chmod_private(path)


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows / 某些文件系统不支持 POSIX 权限，忽略


def config_source() -> dict:
    """运维自检：只回报来源，不回报任何密钥值。"""
    path = config_path()
    return {
        "path": str(path),
        "from_env": bool(os.environ.get("MONITOR_STOCK_MODELS_FILE", "").strip()),
        "exists": path.exists(),
        "api_key_from_env": bool(env_value("llm_api_key")),
        "vision_api_key_from_env": bool(env_value("llm_vision_api_key")),
    }
