from __future__ import annotations

import csv
import ipaddress
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class Case:
    case_id: str
    repository: str
    base_commit: str
    target_commit: str
    problem_statement: str
    hint: str
    gold_diff: str
    test_command: str = ""
    external_validation: bool = False
    external_validation_reason: str = ""


FIELDS = list(Case.__dataclass_fields__)


def write_cases(path: Path, cases: list[Case]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8", opener=_private_opener) as out:
        writer = csv.DictWriter(out, FIELDS)
        writer.writeheader()
        for case in cases:
            writer.writerow(asdict(case))


def append_case(path: Path, case: Case) -> None:
    with open(path, "a", newline="", encoding="utf-8", opener=_private_opener) as out:
        csv.DictWriter(out, FIELDS).writerow(asdict(case))


def read_cases(path: Path) -> list[Case]:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            break
        except OverflowError:
            limit //= 10
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    cases = []
    for row in rows:
        if set(row) != set(FIELDS):
            raise ValueError(f"Invalid dataset columns in {path}")
        flag = row["external_validation"].lower()
        if flag not in {"true", "false"}:
            raise ValueError(f"Invalid external_validation value in {path}: {flag}")
        row["external_validation"] = flag == "true"
        cases.append(Case(**row))
    return cases


@dataclass
class ModelConfig:
    name: str
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    temperature: float = 0.0
    max_retries: int = 2
    api: str = "chat_completions"
    max_output_tokens: int = 4096
    harness: str = "anybench"
    command: list[str] = field(default_factory=list)
    image: str = ""
    allowed_hosts: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    harness_timeout: int = 1800
    prompt_cache: bool = True
    context_profile: str = "enhanced"
    context_window_tokens: int = 200_000

    def __post_init__(self) -> None:
        if not self.name or not self.model:
            raise ValueError("name and model are required")
        if self.api not in {"chat_completions", "responses", "anthropic"}:
            raise ValueError("Unknown model API")
        if self.harness not in {"anybench", "codex", "claude", "opencode", "custom"}:
            raise ValueError("Unknown harness")
        if self.harness == "custom" and not self.command:
            raise ValueError("Custom harness requires command")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if self.context_profile not in {"enhanced", "legacy"}:
            raise ValueError("context_profile must be enhanced or legacy")
        if (type(self.context_window_tokens) is not int or
                self.context_window_tokens * 0.9 <= self.max_output_tokens + 1024):
            raise ValueError("context_window_tokens must leave room for input and output")
        if self.max_retries < 0 or self.harness_timeout < 1:
            raise ValueError("Retry count and harness timeout must be valid")
        if self.api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",
                                                 self.api_key_env):
            raise ValueError("api_key_env must be an environment variable name")
        if self.harness != "anybench":
            if not self.image or not self.allowed_hosts:
                raise ValueError("External harness requires image and allowed_hosts")
            if not self.api_key_env and not self.env:
                raise ValueError("External harness requires credential environment variables")
            if not isinstance(self.allowed_hosts, list) or any(
                    not isinstance(host, str) or
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host)
                    for host in self.allowed_hosts):
                raise ValueError("allowed_hosts must contain DNS hostnames")
            if not isinstance(self.command, list) or any(
                    not isinstance(word, str) or not word for word in self.command):
                raise ValueError("command must be an argument array")
            if not isinstance(self.env, dict) or any(
                    not isinstance(target, str) or not isinstance(source, str) or
                    not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target) or
                    not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", source) or
                    target.lower() in
                    {"http_proxy", "https_proxy", "no_proxy"}
                    for target, source in self.env.items()):
                raise ValueError("env must map variable names without overriding proxy settings")
        if self.harness != "anybench" and not self.base_url:
            return
        try:
            endpoint = urlsplit(self.base_url)
            host = endpoint.hostname
            endpoint.port  # Reject malformed ports before sending a credential.
        except ValueError as exc:
            raise ValueError("base_url is not a valid URL") from exc
        if (endpoint.scheme not in {"http", "https"} or not host or
                endpoint.username is not None or endpoint.password is not None or
                endpoint.query or endpoint.fragment):
            raise ValueError("base_url must be an HTTP(S) URL without credentials, query, or fragment")
        if endpoint.scheme == "http":
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host == "localhost"
            if not local:
                raise ValueError("base_url must use HTTPS except for loopback endpoints")
        if not self.api_key_env and self.harness == "anybench":
            raise ValueError("api_key_env is required")


@dataclass
class RunRecord:
    case_id: str
    model: str
    attempt: int
    status: str
    seconds: float
    started_at: float = 0.0
    finished_at: float = 0.0
    concurrency: int = 2
    setup_seconds: float = 0.0
    model_seconds: float = 0.0
    test_seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int | None = 0
    test_passed: bool | None = None
    judge_score: float | None = None
    judge_reason: str = ""
    judge_prompt_tokens: int = 0
    judge_completion_tokens: int = 0
    judge_seconds: float = 0.0
    diff: str = ""
    error: str = ""
    trace: list[dict] = field(default_factory=list)
    harness: str = "anybench"
    model_id: str = ""
    cached_prompt_tokens: int | None = None
    cache_creation_tokens: int | None = None
    usage_available: bool = True
    harness_version: str = ""
    harness_seconds: float | None = None
    context_profile: str = "legacy"
    context_window_tokens: int | None = None
    model_calls_by_purpose: dict[str, int] | None = None
    compactions: int | None = None
    peak_context_tokens: int | None = None
    artifact_directory: str = ""
    verification_runs: list[dict] | None = None
    stop_reason: str = ""


def write_jsonl(path: Path, records: list[RunRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", opener=_private_opener) as out:
        for record in records:
            out.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def append_jsonl(path: Path, record: RunRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", opener=_private_opener) as out:
        out.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[RunRecord]:
    return [RunRecord(**json.loads(line)) for line in path.read_text().splitlines() if line]


def _private_opener(path: str, flags: int) -> int:
    import os
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return descriptor
