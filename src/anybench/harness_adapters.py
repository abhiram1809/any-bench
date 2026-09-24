"""Pure structured-event normalization for headless coding harnesses."""
from __future__ import annotations

from dataclasses import dataclass
import json


@dataclass
class HarnessResult:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    cache_creation_tokens: int | None = None
    tool_calls: int | None = None
    version: str = ""


def parse_events(harness: str, output: str) -> HarnessResult:
    result = HarnessResult()
    seen_tools: set[str] = set()
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if harness == "codex" and event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
            for attr, key in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"),
                              ("cached_prompt_tokens", "cached_input_tokens")):
                if type(usage.get(key)) is int:
                    setattr(result, attr, (getattr(result, attr) or 0) + usage[key])
        elif harness == "codex" and event.get("type") == "item.completed":
            if (event.get("item") or {}).get("type") in {"command_execution", "file_change"}:
                result.tool_calls = (result.tool_calls or 0) + 1
        elif harness == "claude":
            if event.get("type") == "result":
                usage = event.get("usage") or {}
                result.prompt_tokens = usage.get("input_tokens", result.prompt_tokens)
                result.completion_tokens = usage.get("output_tokens", result.completion_tokens)
                result.cached_prompt_tokens = usage.get("cache_read_input_tokens", result.cached_prompt_tokens)
                result.cache_creation_tokens = usage.get("cache_creation_input_tokens", result.cache_creation_tokens)
                if result.prompt_tokens is not None:
                    result.prompt_tokens += (result.cached_prompt_tokens or 0) + (result.cache_creation_tokens or 0)
            elif event.get("type") == "assistant":
                blocks = (event.get("message") or {}).get("content") or []
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        ident = str(block.get("id", ""))
                        if ident not in seen_tools:
                            seen_tools.add(ident)
                            result.tool_calls = (result.tool_calls or 0) + 1
        elif harness == "opencode":
            part = event.get("part") or {}
            if part.get("type") == "tool":
                ident = str(part.get("id", ""))
                if ident not in seen_tools:
                    seen_tools.add(ident)
                    result.tool_calls = (result.tool_calls or 0) + 1
            if part.get("type") == "step-finish":
                tokens = part.get("tokens") or {}
                for attr, key in (("prompt_tokens", "input"), ("completion_tokens", "output")):
                    value = tokens.get(key)
                    if isinstance(value, int):
                        setattr(result, attr, (getattr(result, attr) or 0) + value)
                cache = tokens.get("cache") or {}
                if isinstance(cache, dict):
                    for attr, key in (("cached_prompt_tokens", "read"),
                                      ("cache_creation_tokens", "write")):
                        if isinstance(cache.get(key), int):
                            setattr(result, attr, (getattr(result, attr) or 0) + cache[key])
                            result.prompt_tokens = (result.prompt_tokens or 0) + cache[key]
    return result
