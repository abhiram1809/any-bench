from __future__ import annotations

import csv
import ipaddress
import json
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
    base_url: str
    model: str
    api_key_env: str
    temperature: float = 0.0
    max_retries: int = 2

    def __post_init__(self) -> None:
        if not self.name or not self.model:
            raise ValueError("name and model are required")
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
        if not self.api_key_env:
            raise ValueError("api_key_env is required")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")


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
    tool_calls: int = 0
    test_passed: bool | None = None
    judge_score: float | None = None
    judge_reason: str = ""
    judge_prompt_tokens: int = 0
    judge_completion_tokens: int = 0
    judge_seconds: float = 0.0
    diff: str = ""
    error: str = ""
    trace: list[dict] = field(default_factory=list)


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
