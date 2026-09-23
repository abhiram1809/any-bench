from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .model import ModelConfig


@dataclass
class Reply:
    message: dict
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0


def parse_json_object(content: str) -> dict:
    """Accept a JSON object with optional surrounding model prose or fences."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", content):
        try:
            value, _ = decoder.raw_decode(content[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("Model did not return a JSON object")


class ChatClient:
    """Minimal OpenAI compatible chat completions client."""

    def __init__(self, config: ModelConfig, timeout: int = 120):
        self.config = config
        self.timeout = timeout

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> Reply:
        started = time.monotonic()
        key = os.environ.get(self.config.api_key_env)
        if not key:
            raise ValueError(f"Missing API key environment variable: {self.config.api_key_env}")
        endpoint = self.config.base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        payload = {"model": self.config.model, "messages": messages,
                   "temperature": self.config.temperature}
        if tools:
            payload["tools"] = tools
        request = urllib.request.Request(
            endpoint, data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        for attempt in range(self.config.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == self.config.max_retries:
                    detail = exc.read(2048).decode(errors="replace")
                    exc.close()
                    raise RuntimeError(f"LLM API returned HTTP {exc.code}: {detail}") from exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                try:
                    delay = float(retry_after) if retry_after is not None else 2 ** attempt
                except ValueError:
                    delay = 2 ** attempt
                time.sleep(max(0, min(delay, 30)))
            except urllib.error.URLError as exc:
                if attempt == self.config.max_retries:
                    raise RuntimeError(f"LLM API connection failed: {exc.reason}") from exc
                time.sleep(2 ** attempt)
        usage = data.get("usage") or {}
        return Reply(data["choices"][0]["message"],
                     usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                     time.monotonic() - started)
