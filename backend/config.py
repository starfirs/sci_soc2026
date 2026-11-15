"""运行时配置：加载 .env，定位数据库与 LLM 参数。"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # 未安装时仍可用环境变量运行

    def load_dotenv(*args, **kwargs):
        return False


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_MODEL = "deepseek-flash"


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    max_tokens: int = 4096
    timeout: float = 30.0


def get_llm_config() -> LLMConfig:
    api_key = os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未找到 API key。请在仓库根目录的 .env 中设置 "
            "ANTHROPIC_AUTH_TOKEN，或设置同名环境变量。"
        )
    return LLMConfig(
        api_key=api_key,
        base_url=os.getenv("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL),
        model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
        max_tokens=int(os.getenv("DEEPSEEK_MAX_TOKENS", "4096")),
        timeout=float(os.getenv("DEEPSEEK_TIMEOUT", "30")),
    )


def get_db_path() -> Path:
    raw = os.getenv("MEMORY_DB")
    if raw:
        return Path(raw).expanduser().resolve()
    return ROOT_DIR / "data" / "memory.db"


def configure_stdio() -> None:
    """Windows 控制台默认可能是 GBK，统一按 UTF-8 输出。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, OSError):
                pass
