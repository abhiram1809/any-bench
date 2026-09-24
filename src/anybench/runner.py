from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import asdict

from .evaluate import evaluate_patch
from .privacy import redact, redact_value
from .execution import CANCELLED
from pathlib import Path
from typing import Callable

from .llm import ChatClient
from .enhanced import enhanced_loop
from .harness import PROXY_IMAGE, execute_harness, external_sandbox
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
            workspace_size: str = "512m", memory: str = "1g",
            artifact_base: Path | None = None) -> RunRecord:
    start = time.monotonic()
    record = RunRecord(case.case_id, config.name, attempt, "error", 0,
                       started_at=time.time(), harness=config.harness, model_id=config.model)
    record.context_profile = config.context_profile if config.harness == "anybench" else "external"
    client = None
    try:
        context = (Sandbox(case, image=image, workspace_size=workspace_size, memory=memory)
                   if config.harness == "anybench" else
                   external_sandbox(case, config, workspace_size, memory))
        with context as sandbox:
            record.setup_seconds = time.monotonic() - start
            harness_start = time.monotonic()
            try:
                if config.harness == "anybench":
                    client = ChatClient(config)
                    task = f"Base commit: {case.base_commit}\n\n{case.problem_statement}"
                    if config.context_profile == "enhanced":
                        result = enhanced_loop(client, sandbox, task, config, max_steps,
                                               artifact_base or Path('.anybench/artifacts'))
                        record.context_window_tokens = config.context_window_tokens
                        record.harness_version = "anybench-context-v2"
                        for name in ("trace", "prompt_tokens", "completion_tokens", "tool_calls",
                                     "model_seconds", "model_calls_by_purpose", "compactions",
                                     "peak_context_tokens", "artifact_directory", "verification_runs",
                                     "stop_reason", "error"):
                            setattr(record, name, getattr(result, name))
                    else:
                        final, trace, pt, ct, calls, seconds = agent_loop(
                            client, sandbox, task, max_steps=max_steps)
                        record.trace = trace
                        record.prompt_tokens, record.completion_tokens, record.tool_calls = pt, ct, calls
                        record.model_seconds = seconds
                        record.harness_version = "anybench-legacy-v2"
                        record.stop_reason = "step_limit" if final == "Step limit reached" else "completed"
                else:
                    record.usage_available = False
                    record.tool_calls = None
                    usage = execute_harness(sandbox, case, config)
                    record.prompt_tokens = usage.prompt_tokens or 0
                    record.completion_tokens = usage.completion_tokens or 0
                    record.tool_calls = usage.tool_calls
                    record.cached_prompt_tokens = usage.cached_prompt_tokens
                    record.cache_creation_tokens = usage.cache_creation_tokens
                    record.usage_available = usage.prompt_tokens is not None or usage.completion_tokens is not None
                    record.harness_version = usage.version
            except Exception as exc:
                record.stop_reason = getattr(exc, "reason", "timeout" if "timed out" in str(exc).lower() else "error")
                record.error = f"{type(exc).__name__}: {exc}"
                usage = getattr(exc, "usage", None)
                if usage:
                    for name in ("prompt_tokens", "completion_tokens", "cached_prompt_tokens",
                                 "cache_creation_tokens", "tool_calls"):
                        value = getattr(usage, name, None)
                        if value is not None:
                            setattr(record, name, value)
                    record.usage_available = usage.prompt_tokens is not None or usage.completion_tokens is not None
            finally:
                record.harness_seconds = time.monotonic() - harness_start
                if client:
                    record.usage_available = getattr(client, "usage_available", True)
                    for name in ("prompt_tokens", "completion_tokens", "cached_prompt_tokens", "cache_creation_tokens"):
                        value = getattr(client, name, None)
                        if isinstance(value, int):
                            setattr(record, name, value)
                    record.trace.extend(getattr(client, "events", []))
                try:
                    sandbox.quiesce()
                    record.diff = (sandbox.diff() if config.harness == "anybench" and
                                   config.context_profile == "legacy" else sandbox.collect_container_diff())
                except Exception as exc:
                    record.stop_reason = "error"
                    record.error += f"; patch collection failed: {exc}"
        record.status = ("completed" if record.stop_reason in {"", "completed"} else
                         "exhausted" if record.stop_reason in {"step_limit", "context_limit", "token_limit", "time_limit"}
                         else "error")
        if case.test_command and not case.external_validation:
            test_start = time.monotonic()
            outcome = evaluate_patch(case, record.diff,
                                     config.image if config.harness != "anybench" else image,
                                     workspace_size, memory)
            record.test_passed = outcome.passed
            record.test_seconds = time.monotonic() - test_start
            record.trace.append({"test_command": case.test_command, "result": outcome.output,
                                 "evaluation": asdict(outcome)})
    except Exception as exc:
        record.status = "error"
        if record.stop_reason in {"", "completed"}:
            record.stop_reason = "error"
        record.error = (record.error + "; " if record.error else "") + f"{type(exc).__name__}: {exc}"
    record.error = redact(record.error, config)
    record.diff = redact(record.diff, config)
    record.trace = redact_value(record.trace, config)
    record.seconds = time.monotonic() - start
    record.finished_at = time.time()
    return record


def run_cases(cases: list[Case], configs: list[ModelConfig], concurrency: int = 2,
             attempts: int = 1, image: str = "anybench-sandbox:latest",
             max_steps: int = 30, image_map: dict[str, str] | None = None,
             on_record: Callable[[RunRecord], None] | None = None,
             workspace_size: str = "512m", memory: str = "1g",
             completed_keys: set[tuple[str, str, int, int]] | None = None,
             artifact_base: Path | None = None) -> list[RunRecord]:
    if concurrency < 1 or attempts < 1:
        raise ValueError("concurrency and attempts must be positive")
    results: list[RunRecord] = []
    CANCELLED.clear()
    for config in configs:
        workers = min(concurrency, config.provider_concurrency or concurrency)
        jobs = iter((case, attempt) for case in cases for attempt in range(1, attempts + 1)
                    if (case.case_id, config.name, concurrency, attempt) not in (completed_keys or set()))
        pool = ThreadPoolExecutor(max_workers=workers)
        pending = set()
        def submit_next():
            job = next(jobs, None)
            if job is None:
                return
            case, attempt = job
            pending.add(pool.submit(run_one, case, config, attempt,
                                    (image_map or {}).get(case.repository, image), max_steps,
                                    workspace_size, memory,
                                    *([artifact_base] if artifact_base is not None else [])))
        try:
            for _ in range(workers):
                submit_next()
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    pending.remove(future)
                    record = future.result()
                    record.concurrency = concurrency
                    results.append(record)
                    if on_record:
                        on_record(record)
                    submit_next()
        except KeyboardInterrupt:
            CANCELLED.set()
            raise
        finally:
            for future in pending:
                future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
    return sorted(results, key=lambda r: (r.model, r.case_id, r.attempt))


def run_sweep(cases: list[Case], configs: list[ModelConfig],
             concurrencies: list[int], attempts: int = 1,
             image: str = "anybench-sandbox:latest", max_steps: int = 30,
             image_map: dict[str, str] | None = None,
             on_record: Callable[[RunRecord], None] | None = None,
             workspace_size: str = "512m", memory: str = "1g",
             completed_keys: set[tuple[str, str, int, int]] | None = None,
             artifact_base: Path | None = None) -> list[RunRecord]:
    if not concurrencies or any(level < 1 for level in concurrencies):
        raise ValueError("concurrencies must contain positive integers")
    results = []
    for level in concurrencies:
        results.extend(run_cases(cases, configs, level, attempts, image, max_steps,
                                 image_map, on_record, workspace_size, memory,
                                 completed_keys, artifact_base))
    return results


def preflight_run(cases: list[Case], configs: list[ModelConfig],
                  image: str, image_map: dict[str, str] | None = None) -> None:
    required = {name for config in configs for name in
                ([config.api_key_env] if config.api_key_env else []) + list(config.env.values())}
    missing = sorted(name for name in required if not os.environ.get(name))
    if missing:
        raise ValueError(f"Missing API key environment variables: {', '.join(missing)}")
    daemon = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                            capture_output=True, text=True, timeout=30)
    if daemon.returncode:
        raise RuntimeError(f"Docker daemon unavailable: {daemon.stderr.strip()}")
    images = {(image_map or {}).get(case.repository, image) for case in cases
              if any(config.harness == "anybench" for config in configs)}
    images.update(config.image for config in configs if config.harness != "anybench")
    if any(config.harness != "anybench" for config in configs):
        images.add(PROXY_IMAGE)
    for selected in sorted(images):
        result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", selected],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(f"Sandbox image unavailable: {selected}. Build or pull it first")
    if any(config.harness == "anybench" and config.context_profile == "enhanced" for config in configs):
        builtin_images = {(image_map or {}).get(case.repository, image) for case in cases}
        for selected in sorted(builtin_images):
            checked = subprocess.run(["docker", "run", "--rm", "--network", "none",
                                      "--entrypoint", "python", selected, "-c",
                                      "import sys; assert sys.version_info >= (3, 11)"],
                                     capture_output=True, text=True, timeout=30)
            if checked.returncode:
                raise RuntimeError(f"Enhanced harness image {selected} requires Python 3.11+")
    commands = {"codex": "codex", "claude": "claude", "opencode": "opencode"}
    for config in configs:
        if config.harness == "anybench":
            continue
        executable = (commands[config.harness] if config.harness in commands
                      else config.command[0])
        result = subprocess.run(["docker", "run", "--rm", "--network", "none",
                                 "--entrypoint", "sh", config.image, "-c",
                                 'command -v "$1" >/dev/null && command -v tar >/dev/null',
                                "sh", executable], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(f"Harness image {config.image} lacks {executable} or tar")
