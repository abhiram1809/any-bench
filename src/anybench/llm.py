from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from uuid import uuid4

from .model import ModelConfig
from .providers import ProtocolAdapter, Reply
from .privacy import redact, redact_value
from .execution import CANCELLED


_ROUTER_RUN_ID = uuid4().hex


class ContextLimitError(RuntimeError):
    """The endpoint rejected a request because its context window was exceeded."""


class LimitExceeded(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason




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


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise RuntimeError("LLM API redirect blocked; configure the final endpoint URL")


class ChatClient:
    """Provider-independent tool-calling client with a chat-shaped internal reply."""

    def __init__(self, config: ModelConfig, timeout: int = 120):
        self.config = config
        self.timeout = timeout
        self.cached_prompt_tokens: int | None = None
        self.cache_creation_tokens: int | None = None
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.events: list[dict] = []
        self.usage_available = True
        self.started = time.monotonic()
        model_tag = hashlib.sha256(config.model.encode()).hexdigest()[:12]
        self.session_id = (f"anybench-{_ROUTER_RUN_ID}-{model_tag}"
                           if config.api == "chat_completions" and
                           urlsplit(config.base_url).hostname == "openrouter.ai" else None)

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> Reply:
        if CANCELLED.is_set():
            raise LimitExceeded("cancelled")
        if self.config.max_total_tokens is not None and self.prompt_tokens + self.completion_tokens >= self.config.max_total_tokens:
            raise LimitExceeded("token_limit")
        if self.config.attempt_timeout is not None and time.monotonic() - self.started >= self.config.attempt_timeout:
            raise LimitExceeded("time_limit")
        started = time.monotonic()
        key = os.environ.get(self.config.api_key_env)
        if not key and self.config.api_key_env:
            raise ValueError(f"Missing API key environment variable: {self.config.api_key_env}")
        api = self.config.api
        suffix = {"chat_completions": "/chat/completions", "responses": "/responses",
                  "anthropic": "/messages"}[api]
        endpoint = self.config.base_url.rstrip("/")
        if not endpoint.endswith(suffix):
            endpoint += suffix
        payload = self._payload(messages, tools)
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        headers = {"Content-Type": "application/json"}
        if api == "anthropic":
            headers.update({"anthropic-version": "2023-06-01"})
            if key:
                headers["x-api-key"] = key
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(
            endpoint, data=json.dumps(payload).encode(),
            headers=headers,
        )
        opener = urllib.request.build_opener(_RejectRedirect())
        for attempt in range(self.config.max_retries + 1):
            try:
                timeout = min(self.timeout, max(1, self.config.attempt_timeout - (time.monotonic() - self.started))) if self.config.attempt_timeout else self.timeout
                deadline = time.monotonic() + timeout
                with opener.open(request, timeout=timeout) as response:
                    parts = []
                    total = 0
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("LLM API response timed out")
                        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                        if sock is not None:
                            sock.settimeout(remaining)
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > 16_000_000:
                            raise ValueError("LLM API response exceeds 16 MB")
                        parts.append(chunk)
                    data = json.loads(b"".join(parts))
                break
            except (TimeoutError, socket.timeout) as exc:
                raise RuntimeError("LLM API response timed out; billing state unknown") from exc
            except urllib.error.HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == self.config.max_retries:
                    detail = redact(exc.read(2048).decode(errors="replace"), self.config)
                    exc.close()
                    if exc.code in {400, 413} and any(marker in detail.lower() for marker in
                            ("context_length_exceeded", "maximum context", "prompt is too long",
                             "too many tokens", "context window", "input is too long")):
                        raise ContextLimitError(detail) from exc
                    raise RuntimeError(f"LLM API returned HTTP {exc.code}: {detail}") from exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                try:
                    delay = float(retry_after) if retry_after is not None else 2 ** attempt
                except ValueError:
                    delay = 2 ** attempt
                time.sleep(max(0, min(delay, 30)))
                self.events.append({"retry": attempt + 1, "http_status": exc.code, "delay": max(0, min(delay, 30))})
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, TimeoutError) or "timed out" in str(exc.reason).lower():
                    raise RuntimeError("LLM API response timed out; billing state unknown") from exc
                if attempt == self.config.max_retries:
                    raise RuntimeError(f"LLM API connection failed: {exc.reason}") from exc
                time.sleep(2 ** attempt)
        reply = self._reply(data, time.monotonic() - started)
        self.usage_available = self.usage_available and bool(data.get("usage"))
        self.prompt_tokens += reply.prompt_tokens
        self.completion_tokens += reply.completion_tokens
        if reply.cached_prompt_tokens is not None:
            self.cached_prompt_tokens = (self.cached_prompt_tokens or 0) + reply.cached_prompt_tokens
        if reply.cache_creation_tokens is not None:
            self.cache_creation_tokens = (self.cache_creation_tokens or 0) + reply.cache_creation_tokens
        if (data.get("status") == "incomplete" or data.get("stop_reason") == "max_tokens" or
                any(choice.get("finish_reason") == "length" for choice in data.get("choices", []))):
            raise RuntimeError("LLM response exceeded output token limit; usage preserved")
        reply.message = redact_value(reply.message, self.config)
        return reply

    def _payload(self, messages: list[dict], tools: list[dict] | None) -> dict:
        return ProtocolAdapter(self.config).payload(messages, tools)

    def _reply(self, data: dict, seconds: float) -> Reply:
        return ProtocolAdapter(self.config).reply(data, seconds)
