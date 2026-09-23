from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .llm import ChatClient
from .model import Case, ModelConfig, RunRecord
from .sandbox import Sandbox, ToolError


TOOL_SPECS = [
    {"name": "Read", "description": "Read a repository file. Ranges are 1-based inclusive.",
     "parameters": {"type": "object", "properties": {"file_path": {"type": "string"},
                   "lines_range": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}},
                    "required": ["file_path"]}},
    {"name": "Write", "description": "Create or replace a file; optionally replace one inclusive line range.",
     "parameters": {"type": "object", "properties": {"file_path": {"type": "string"},
                   "content": {"type": "string"}, "line_range": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}},
                    "required": ["file_path", "content"]}},
    {"name": "Edit", "description": "Replace one inclusive line range in an existing file.",
     "parameters": {"type": "object", "properties": {"file_path": {"type": "string"},
                   "content": {"type": "string"}, "line_range": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}},
                    "required": ["file_path", "content", "line_range"]}},
    {"name": "Bash", "description": "Run one read-only command: cat, grep, glob, wc or jq.",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "Agent", "description": "Delegate a bounded repository task to a subagent; no further delegation.",
     "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}},
]


def _api_tools(allow_agent: bool) -> list[dict]:
    return [{"type": "function", "function": spec} for spec in TOOL_SPECS
            if allow_agent or spec["name"] != "Agent"]


def agent_loop(client: ChatClient, sandbox: Sandbox, problem: str, max_steps: int = 30,
               allow_agent: bool = True) -> tuple[str, list[dict], int, int, int, float]:
    messages = [
        {"role": "system", "content": "You are fixing a coding task in /repo. Use tools to inspect and edit. "
         "The repository is checked out before the target change. Never assume the historical patch is visible. "
         "Finish with a short explanation. Ranges are 1-based and inclusive."},
        {"role": "user", "content": problem},
    ]
    trace: list[dict] = []
    prompt_tokens = completion_tokens = calls = 0
    model_seconds = 0.0
    for _ in range(max_steps):
        reply = client.complete(messages, _api_tools(allow_agent))
        model_seconds += reply.seconds
        prompt_tokens += reply.prompt_tokens
        completion_tokens += reply.completion_tokens
        message = reply.message
        messages.append(message)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return (str(message.get("content") or ""), trace, prompt_tokens,
                    completion_tokens, calls, model_seconds)
        for call in tool_calls:
            name = call["function"]["name"]
            calls += 1
            arguments = {}
            try:
                arguments = json.loads(call["function"]["arguments"])
                if name == "Read":
                    output = sandbox.read(**arguments)
                elif name == "Write":
                    output = sandbox.write(**arguments)
                elif name == "Edit":
                    output = sandbox.edit(**arguments)
                elif name == "Bash":
                    output = sandbox.bash(**arguments)
                elif name == "Agent" and allow_agent:
                    output, subtrace, pt, ct, subcalls, subtime = agent_loop(
                        client, sandbox, arguments["prompt"], max_steps=10, allow_agent=False)
                    trace.extend(subtrace)
                    prompt_tokens += pt
                    completion_tokens += ct
                    calls += subcalls
                    model_seconds += subtime
                else:
                    raise ToolError(f"Unknown or unavailable tool: {name}")
                output = str(output)[:20000]
            except (ValueError, TypeError, KeyError, OSError, ToolError) as exc:
                output = f"Tool error: {exc}"
            trace.append({"tool": name, "arguments": arguments, "result": output[:2000]})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
    return "Step limit reached", trace, prompt_tokens, completion_tokens, calls, model_seconds


def run_one(case: Case, config: ModelConfig, attempt: int = 1,
            image: str = "anybench-sandbox:latest", max_steps: int = 30,
            workspace_size: str = "512m", memory: str = "1g") -> RunRecord:
    start = time.monotonic()
    record = RunRecord(case.case_id, config.name, attempt, "error", 0,
                       started_at=time.time())
    try:
        with Sandbox(case, image=image, workspace_size=workspace_size, memory=memory) as sandbox:
            record.setup_seconds = time.monotonic() - start
            client = ChatClient(config)
            task = f"Base commit: {case.base_commit}\n\n{case.problem_statement}"
            _, trace, pt, ct, calls, model_seconds = agent_loop(
                client, sandbox, task, max_steps=max_steps)
            record.trace = trace
            record.prompt_tokens, record.completion_tokens, record.tool_calls = pt, ct, calls
            record.model_seconds = model_seconds
            record.diff = sandbox.diff()
            record.status = "completed"
            if case.test_command and not case.external_validation:
                test_start = time.monotonic()
                record.test_passed, test_output = sandbox.test(case.test_command)
                record.test_seconds = time.monotonic() - test_start
                record.trace.append({"test_command": case.test_command, "result": test_output})
    except Exception as exc:
        record.error = f"{type(exc).__name__}: {exc}"
    record.seconds = time.monotonic() - start
    record.finished_at = time.time()
    return record


def run_cases(cases: list[Case], configs: list[ModelConfig], concurrency: int = 2,
             attempts: int = 1, image: str = "anybench-sandbox:latest",
             max_steps: int = 30, image_map: dict[str, str] | None = None,
             on_record: Callable[[RunRecord], None] | None = None,
             workspace_size: str = "512m", memory: str = "1g") -> list[RunRecord]:
    if concurrency < 1 or attempts < 1:
        raise ValueError("concurrency and attempts must be positive")
    results: list[RunRecord] = []
    for config in configs:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(run_one, case, config, attempt,
                                   (image_map or {}).get(case.repository, image), max_steps,
                                   workspace_size, memory)
                       for case in cases for attempt in range(1, attempts + 1)]
            for future in as_completed(futures):
                record = future.result()
                record.concurrency = concurrency
                results.append(record)
                if on_record:
                    on_record(record)
    return sorted(results, key=lambda r: (r.model, r.case_id, r.attempt))


def run_sweep(cases: list[Case], configs: list[ModelConfig],
             concurrencies: list[int], attempts: int = 1,
             image: str = "anybench-sandbox:latest", max_steps: int = 30,
             image_map: dict[str, str] | None = None,
             on_record: Callable[[RunRecord], None] | None = None,
             workspace_size: str = "512m", memory: str = "1g") -> list[RunRecord]:
    if not concurrencies or any(level < 1 for level in concurrencies):
        raise ValueError("concurrencies must contain positive integers")
    results = []
    for level in concurrencies:
        results.extend(run_cases(cases, configs, level, attempts, image, max_steps,
                                 image_map, on_record, workspace_size, memory))
    return results


def preflight_run(cases: list[Case], configs: list[ModelConfig],
                  image: str, image_map: dict[str, str] | None = None) -> None:
    missing = sorted({config.api_key_env for config in configs if not os.environ.get(config.api_key_env)})
    if missing:
        raise ValueError(f"Missing API key environment variables: {', '.join(missing)}")
    daemon = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                            capture_output=True, text=True)
    if daemon.returncode:
        raise RuntimeError(f"Docker daemon unavailable: {daemon.stderr.strip()}")
    images = {(image_map or {}).get(case.repository, image) for case in cases}
    for selected in sorted(images):
        result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", selected],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"Sandbox image unavailable: {selected}. Build or pull it first")
