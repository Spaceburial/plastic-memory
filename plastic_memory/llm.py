"""llm.py — 一个最小的 OpenAI 兼容客户端，给"梦"做蒸馏用。

不绑任何云厂商：DeepSeek / OpenAI / Ollama / vLLM / LM Studio 都是同一套
`/chat/completions`，配好 base_url + api_key + model 就能用。

    from plastic_memory.llm import from_env
    distill.set_llm(from_env())

环境变量：
    PLASTIC_MEMORY_BASE_URL   默认 https://api.deepseek.com/v1
    PLASTIC_MEMORY_API_KEY    也接受 DEEPSEEK_API_KEY / OPENAI_API_KEY
    PLASTIC_MEMORY_MODEL      默认 deepseek-chat
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass


@dataclass
class _Result:
    text: str


class OpenAICompat:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(self, messages, *, temperature: float = 0.2,
                 max_tokens: int = 800, **_ignored) -> _Result:
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="ignore"))
        return _Result(text=(data["choices"][0]["message"]["content"] or "").strip())


def from_env() -> OpenAICompat | None:
    key = (os.environ.get("PLASTIC_MEMORY_API_KEY")
           or os.environ.get("DEEPSEEK_API_KEY")
           or os.environ.get("OPENAI_API_KEY"))
    if not key:
        return None
    return OpenAICompat(
        base_url=os.environ.get("PLASTIC_MEMORY_BASE_URL", "https://api.deepseek.com/v1"),
        api_key=key,
        model=os.environ.get("PLASTIC_MEMORY_MODEL", "deepseek-chat"),
    )
