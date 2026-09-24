"""Isolated headless coding-harness execution and usage normalization."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from contextlib import contextmanager
from pathlib import Path

from .model import Case, ModelConfig
from .harness_adapters import HarnessResult, parse_events
from .sandbox import Sandbox


PROXY_IMAGE = "python:3.11-slim"


def _docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
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
                        getattr(config, "_proxy_image", PROXY_IMAGE), "python", "/proxy.py", *config.allowed_hosts)
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
        try:
            if proxy:
                subprocess.run(["docker", "rm", "-f", proxy], capture_output=True, timeout=30)
        finally:
            if network:
                subprocess.run(["docker", "network", "rm", network], capture_output=True, timeout=30)



def execute_harness(sandbox: Sandbox, case: Case, config: ModelConfig) -> HarnessResult:
    task = {"case_id": case.case_id, "base_commit": case.base_commit,
            "problem_statement": case.problem_statement, "repository": "/repo"}
    written = subprocess.run(["docker", "exec", "-i", sandbox.container, "sh", "-c",
                              "cat > /tmp/task.json"], input=json.dumps(task),
                              capture_output=True, text=True, timeout=30)
    if written.returncode:
        raise RuntimeError(f"Could not send task to harness: {written.stderr.strip()}")
    credentials = {}
    if config.api_key_env:
        target = {"codex": "CODEX_API_KEY", "claude": "ANTHROPIC_API_KEY"}.get(
            config.harness, config.api_key_env)
        credentials[target] = os.environ[config.api_key_env]
    for target, source in config.env.items():
        credentials[target] = os.environ[source]
    if config.base_url:
        if config.harness == "claude":
            credentials["ANTHROPIC_BASE_URL"] = config.base_url
        elif config.harness == "codex":
            credentials["OPENAI_BASE_URL"] = config.base_url
        elif config.harness == "custom":
            credentials["ANYBENCH_BASE_URL"] = config.base_url
    exports = "".join(f"export {name}={shlex.quote(value)}\n"
                      for name, value in credentials.items())
    sent = subprocess.run(["docker", "exec", "-i", sandbox.container, "sh", "-c",
                           "umask 077; cat > /tmp/anybench-env.sh"],
                          input=exports, capture_output=True, text=True, timeout=30)
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
    try:
        process = sandbox.command(["sh", "-c",
                               '. /tmp/anybench-env.sh; '
                               'export ANYBENCH_TASK_FILE=/tmp/task.json '
                               'ANYBENCH_USAGE_FILE=/tmp/usage.json; exec "$@"',
                               "sh", *command],
                              timeout=min(config.harness_timeout, config.attempt_timeout or config.harness_timeout),
                              limit=20_000_000)
    except ValueError as exc:
        exc.usage = parse_events(config.harness, getattr(exc, "output", ""))
        raise
    if process.returncode:
        detail = process.stdout[-1000:]
        for source in ([config.api_key_env] if config.api_key_env else []) + list(config.env.values()):
            secret = os.environ.get(source)
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        error = RuntimeError(f"Harness exited {process.returncode}: {detail}")
        error.usage = parse_events(config.harness, process.stdout)
        error.reason = "output_limit" if process.returncode == 124 else "error"
        raise error
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
