"""DeepSeek（Anthropic 兼容接口）调用封装与 JSON 解析。"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Protocol

from .config import LLMConfig, get_llm_config


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str: ...

    def complete_json(
        self, system: str, user: str, *, max_tokens: int | None = None
    ) -> dict | list: ...


class DeepSeekClient:
    """通过 Anthropic 兼容 HTTP 接口访问 DeepSeek。

    刻意不依赖 anthropic SDK：本机 `import anthropic` 需要 8 秒以上，
    而这里只需要一次 JSON POST，标准库 urllib 导入几乎无成本。
    """

    def __init__(self, config: LLMConfig | None = None, *, timeout: float | None = None):
        self.config = config or get_llm_config()
        self.timeout = timeout if timeout is not None else self.config.timeout

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        body = {
            "model": self.config.model,
            "max_tokens": max_tokens or self.config.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        request = urllib.request.Request(
            self.config.base_url.rstrip("/") + "/v1/messages",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.config.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise LLMError(f"云端 LLM 返回 HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LLMError(f"调用云端 LLM 失败: {exc}") from exc
        except ValueError as exc:
            raise LLMError(f"云端 LLM 返回内容不是 JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise LLMError("云端 LLM 返回格式异常")
        parts = [
            block.get("text", "")
            for block in data.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "".join(parts).strip()
        if not text:
            raise LLMError("云端 LLM 返回了空内容")
        return text

    def complete_json(
        self, system: str, user: str, *, max_tokens: int | None = None
    ) -> dict | list:
        text = self.complete(system, user, max_tokens=max_tokens)
        try:
            return parse_json(text)
        except ValueError:
            repair = (
                f"{user}\n\n"
                "上一次输出不是合法 JSON。请只输出 JSON，不要解释，不要 Markdown 代码块。"
            )
            text = self.complete(system, repair, max_tokens=max_tokens)
            return parse_json(text)


def parse_json(text: str) -> dict | list:
    """从模型输出中取出 JSON，容忍 ```json 代码块和前后多余文字。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[A-Za-z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    snippet = _extract_balanced(cleaned)
    if snippet is None:
        raise ValueError(f"无法从 LLM 输出中解析 JSON: {text[:200]}")
    return json.loads(snippet)


def _extract_balanced(text: str) -> str | None:
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        return None
    start = min(starts)
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False

    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
