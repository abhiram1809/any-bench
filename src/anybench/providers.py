"""Pure provider payload/reply normalization, independent of HTTP and retries."""
from __future__ import annotations

from dataclasses import dataclass
import json

from .model import ModelConfig


@dataclass
class Reply:
    message: dict
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    cached_prompt_tokens: int | None = None
    cache_creation_tokens: int | None = None



class ProtocolAdapter:
    def __init__(self, config: ModelConfig):
        self.config = config

    def payload(self, messages: list[dict], tools: list[dict] | None) -> dict:
        api = self.config.api
        if api == "chat_completions":
            payload = {"model": self.config.model, "messages": messages,
                       "temperature": self.config.temperature}
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
            if self.config.temperature != 0:
                payload["temperature"] = self.config.temperature
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
                   "messages": converted, "temperature": self.config.temperature}
        if self.config.prompt_cache:
            payload["cache_control"] = {"type": "ephemeral"}
        if system:
            payload["system"] = "\n\n".join(system)
        if tools:
            payload["tools"] = [{"name": item["function"]["name"],
                                 "description": item["function"].get("description", ""),
                                 "input_schema": item["function"]["parameters"]} for item in tools]
        return payload

    def reply(self, data: dict, seconds: float) -> Reply:
        usage = data.get("usage") or {}
        if self.config.api == "chat_completions":
            return Reply(data["choices"][0]["message"], usage.get("prompt_tokens", 0),
                         usage.get("completion_tokens", 0), seconds,
                         (usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
        if self.config.api == "responses":
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
