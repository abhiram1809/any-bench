from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .model import ModelConfig


class ContextLimitError(RuntimeError):
    """The endpoint rejected a request because its context window was exceeded."""


@dataclass
class Reply:
    message: dict
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    cached_prompt_tokens: int | None = None
    cache_creation_tokens: int | None = None


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

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> Reply:
        started = time.monotonic()
        key = os.environ.get(self.config.api_key_env)
        if not key:
            raise ValueError(f"Missing API key environment variable: {self.config.api_key_env}")
        api = self.config.api
        suffix = {"chat_completions": "/chat/completions", "responses": "/responses",
                  "anthropic": "/messages"}[api]
        endpoint = self.config.base_url.rstrip("/")
        if not endpoint.endswith(suffix):
            endpoint += suffix
        payload = self._payload(messages, tools)
        headers = {"Content-Type": "application/json"}
        if api == "anthropic":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        else:
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(
            endpoint, data=json.dumps(payload).encode(),
            headers=headers,
        )
        opener = urllib.request.build_opener(_RejectRedirect())
        for attempt in range(self.config.max_retries + 1):
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    data = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == self.config.max_retries:
                    detail = exc.read(2048).decode(errors="replace")
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
            except urllib.error.URLError as exc:
                if attempt == self.config.max_retries:
                    raise RuntimeError(f"LLM API connection failed: {exc.reason}") from exc
                time.sleep(2 ** attempt)
        reply = self._reply(data, time.monotonic() - started)
        if reply.cached_prompt_tokens is not None:
            self.cached_prompt_tokens = (self.cached_prompt_tokens or 0) + reply.cached_prompt_tokens
        if reply.cache_creation_tokens is not None:
            self.cache_creation_tokens = (self.cache_creation_tokens or 0) + reply.cache_creation_tokens
        return reply

    def _payload(self, messages: list[dict], tools: list[dict] | None) -> dict:
        api = self.config.api
        if api == "chat_completions":
            payload = {"model": self.config.model, "messages": messages,
                       "temperature": self.config.temperature}
            if self.config.context_profile == "enhanced":
                payload["max_tokens"] = self.config.max_output_tokens
            if tools:
                payload["tools"] = tools
            return payload
        if api == "responses":
            inputs = []
            instructions = []
            for message in messages:
                role = message["role"]
                if role == "system":
                    instructions.append(message["content"])
                elif role == "assistant" and "_raw_output" in message:
                    inputs.extend(message["_raw_output"])
                elif role == "tool":
                    inputs.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                                   "output": message["content"]})
                else:
                    inputs.append({"role": role, "content": message.get("content") or ""})
            payload = {"model": self.config.model, "input": inputs, "store": False,
                       "max_output_tokens": self.config.max_output_tokens}
            if instructions:
                payload["instructions"] = "\n\n".join(instructions)
            if tools:
                payload["tools"] = [{"type": "function", **item["function"]} for item in tools]
            return payload
        system = [message["content"] for message in messages if message["role"] == "system"]
        converted = []
        for message in messages:
            role = message["role"]
            if role == "system":
                continue
            if role == "tool":
                item = {"type": "tool_result", "tool_use_id": message["tool_call_id"],
                        "content": message["content"]}
                role = "user"
                content = [item]
            elif role == "assistant" and "_raw_content" in message:
                content = message["_raw_content"]
            else:
                content = message.get("content") or ""
            if converted and converted[-1]["role"] == role:
                previous = converted[-1]["content"]
                if not isinstance(previous, list):
                    previous = [{"type": "text", "text": previous}]
                converted[-1]["content"] = previous + (content if isinstance(content, list)
                                                        else [{"type": "text", "text": content}])
            else:
                converted.append({"role": role, "content": content})
        payload = {"model": self.config.model, "max_tokens": self.config.max_output_tokens,
                   "messages": converted}
        if self.config.prompt_cache:
            payload["cache_control"] = {"type": "ephemeral"}
        if system:
            payload["system"] = "\n\n".join(system)
        if tools:
            payload["tools"] = [{"name": item["function"]["name"],
                                 "description": item["function"].get("description", ""),
                                 "input_schema": item["function"]["parameters"]} for item in tools]
        return payload

    def _reply(self, data: dict, seconds: float) -> Reply:
        usage = data.get("usage") or {}
        if self.config.api == "chat_completions":
            if data["choices"][0].get("finish_reason") == "length":
                raise RuntimeError("LLM response exceeded output token limit")
            return Reply(data["choices"][0]["message"], usage.get("prompt_tokens", 0),
                         usage.get("completion_tokens", 0), seconds,
                         (usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
        if self.config.api == "responses":
            if data.get("status") == "incomplete":
                raise RuntimeError(f"LLM response incomplete: {data.get('incomplete_details')}")
            output = data.get("output", [])
            calls = [{"id": item["call_id"], "function": {"name": item["name"],
                     "arguments": item["arguments"]}} for item in output
                     if item.get("type") == "function_call"]
            content = "\n".join(part.get("text", "") for item in output
                                if item.get("type") == "message"
                                for part in item.get("content", []) if part.get("type") == "output_text")
            return Reply({"role": "assistant", "content": content, "tool_calls": calls,
                          "_raw_output": output},
                         usage.get("input_tokens", 0), usage.get("output_tokens", 0), seconds,
                         (usage.get("input_tokens_details") or {}).get("cached_tokens"))
        if data.get("stop_reason") == "max_tokens":
            raise RuntimeError("LLM response exceeded output token limit")
        blocks = data.get("content", [])
        calls = [{"id": item["id"], "function": {"name": item["name"],
                  "arguments": json.dumps(item["input"])}} for item in blocks
                 if item.get("type") == "tool_use"]
        content = "\n".join(item.get("text", "") for item in blocks
                            if item.get("type") == "text")
        total_input = (usage.get("input_tokens", 0) +
                       usage.get("cache_read_input_tokens", 0) +
                       usage.get("cache_creation_input_tokens", 0))
        return Reply({"role": "assistant", "content": content, "tool_calls": calls,
                      "_raw_content": blocks},
                     total_input, usage.get("output_tokens", 0), seconds,
                     usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens"))
