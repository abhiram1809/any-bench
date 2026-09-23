"""Isolated headless coding-harness execution and usage normalization."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .model import Case, ModelConfig
from .sandbox import Sandbox


PROXY_IMAGE = "python:3.11-slim"


def _docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Docker {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


@contextmanager
def external_sandbox(case: Case, config: ModelConfig, workspace_size: str, memory: str):
    network = proxy = None
    try:
        network = "anybench-egress-" + os.urandom(6).hex()
        _docker("network", "create", "--internal", "--driver", "bridge",
                "--label", "anybench=egress", network)
        script = Path(__file__).with_name("egress_proxy.py").resolve()
        proxy = _docker("run", "-d", "--rm", "--network", "bridge", "--read-only",
                        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                        "--pids-limit", "64", "--memory", "128m", "--cpus", "0.5",
                        "--mount", f"type=bind,src={script},dst=/proxy.py,readonly",
                        PROXY_IMAGE, "python", "/proxy.py", *config.allowed_hosts)
        _docker("network", "connect", network, proxy)
        details = json.loads(_docker("inspect", proxy))
        address = details[0]["NetworkSettings"]["Networks"][network]["IPAddress"]
        endpoint = f"http://{address}:8888"
        environment = {"HTTPS_PROXY": endpoint, "HTTP_PROXY": endpoint,
                       "https_proxy": endpoint, "http_proxy": endpoint,
                       "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}
        with Sandbox(case, image=config.image, workspace_size=workspace_size, memory=memory,
                     network=network, environment=environment) as sandbox:
            yield sandbox
    finally:
        if proxy:
            subprocess.run(["docker", "rm", "-f", proxy], capture_output=True)
        if network:
            subprocess.run(["docker", "network", "rm", network], capture_output=True)


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
            result.prompt_tokens = usage.get("input_tokens", result.prompt_tokens)
            result.completion_tokens = usage.get("output_tokens", result.completion_tokens)
            result.cached_prompt_tokens = usage.get("cached_input_tokens", result.cached_prompt_tokens)
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


def execute_harness(sandbox: Sandbox, case: Case, config: ModelConfig) -> HarnessResult:
    task = {"case_id": case.case_id, "base_commit": case.base_commit,
            "problem_statement": case.problem_statement, "repository": "/repo"}
    written = subprocess.run(["docker", "exec", "-i", sandbox.container, "sh", "-c",
                              "cat > /tmp/task.json"], input=json.dumps(task),
                             capture_output=True, text=True)
    if written.returncode:
        raise RuntimeError(f"Could not send task to harness: {written.stderr.strip()}")
    credentials = {}
    if config.api_key_env:
        target = {"codex": "CODEX_API_KEY", "claude": "ANTHROPIC_API_KEY"}.get(
            config.harness, config.api_key_env)
        credentials[target] = os.environ[config.api_key_env]
    for target, source in config.env.items():
        credentials[target] = os.environ[source]
    exports = "".join(f"export {name}={shlex.quote(value)}\n"
                      for name, value in credentials.items())
    sent = subprocess.run(["docker", "exec", "-i", sandbox.container, "sh", "-c",
                           "umask 077; cat > /tmp/anybench-env.sh"],
                          input=exports, capture_output=True, text=True)
    if sent.returncode:
        raise RuntimeError(f"Could not send credentials to harness: {sent.stderr.strip()}")
    prompt = (f"Base commit: {case.base_commit}\n\n{case.problem_statement}\n\n"
              "Work only in /repo. Finish with a short explanation.")
    if config.harness == "codex":
        command = ["codex", "exec", "--json", "--ephemeral",
                   "--dangerously-bypass-approvals-and-sandbox", "--model", config.model, prompt]
    elif config.harness == "claude":
        command = ["claude", "-p", "--output-format", "stream-json", "--verbose",
                   "--permission-mode", "bypassPermissions", "--model", config.model, prompt]
    elif config.harness == "opencode":
        command = ["opencode", "run", "--standalone", "--format", "json",
                   "--model", config.model, prompt]
    else:
        command = config.command
    process = sandbox.command(["sh", "-c",
                               '. /tmp/anybench-env.sh; '
                               'export ANYBENCH_TASK_FILE=/tmp/task.json '
                               'ANYBENCH_USAGE_FILE=/tmp/usage.json; exec "$@"',
                               "sh", *command],
                              timeout=config.harness_timeout, limit=20_000_000)
    if process.returncode:
        detail = process.stdout[-1000:]
        for source in ([config.api_key_env] if config.api_key_env else []) + list(config.env.values()):
            secret = os.environ.get(source)
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        raise RuntimeError(f"Harness exited {process.returncode}: {detail}")
    result = parse_events(config.harness, process.stdout)
    if config.harness == "custom":
        usage = sandbox.command(["cat", "/tmp/usage.json"])
        if usage.returncode == 0:
            data = json.loads(usage.stdout)
            if not isinstance(data, dict):
                raise ValueError("Custom harness usage must be a JSON object")
            for name in ("prompt_tokens", "completion_tokens", "cached_prompt_tokens",
                         "cache_creation_tokens", "tool_calls"):
                if name in data:
                    if not isinstance(data[name], int) or data[name] < 0:
                        raise ValueError(f"Invalid custom harness usage: {name}")
                    setattr(result, name, data[name])
            if "version" in data:
                if not isinstance(data["version"], str):
                    raise ValueError("Invalid custom harness usage: version")
                result.version = data["version"][:200]
    else:
        version = sandbox.command([command[0], "--version"], timeout=10)
        if version.returncode == 0:
            result.version = version.stdout.strip()[:200]
    return result
