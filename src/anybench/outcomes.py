"""Evaluation contracts kept outside the legacy result wire format."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import subprocess
import time


@dataclass(frozen=True)
class TestOutcome:
    status: str
    exit_code: int | None
    tests: int | None
    seconds: float
    output: str

    @property
    def passed(self) -> bool:
        return self.status == "passed"


def classify(command: str, code: int, output: str, seconds: float = 0) -> TestOutcome:
    """Be conservative: unknown failures are not evidence of a regression."""
    lower = output.lower()
    count = None
    for pattern in (r"ran (\d+) tests?", r"(?:#|ℹ)\s*tests (\d+)", r"(\d+) passing"):
        match = re.search(pattern, lower)
        if match:
            count = int(match[1])
            break
    pytest_counts = re.findall(r"(\d+) (?:passed|failed|skipped|xfailed|xpassed)", lower)
    if pytest_counts and count is None:
        count = sum(map(int, pytest_counts))
    explicit = re.search(r"^ANYBENCH_RESULT (\{.*\})$", output, re.MULTILINE)
    if explicit:
        try:
            item = json.loads(explicit[1])
            if type(item.get("tests")) is int and item["tests"] > 0:
                count = item["tests"]
        except (ValueError, TypeError):
            pass
    setup = ("can't open file", "cannot open", "no such file", "command not found",
             "modulenotfounderror", "importerror", "error collecting", "syntaxerror",
             "err_module_not_found", "cannot find module", "not found:")
    assertion_command = ("assert " in command or "node:assert" in command or
                         bool(re.match(r"\s*(?:grep\s+-q|test\s|\[\s)", command)))
    exercised = count is not None and count > 0 or assertion_command
    if "output exceeded" in lower or "no space left on device" in lower:
        status = "resource_exhaustion"
    elif code in {124, 137, -9}:
        status = "timeout" if code == 124 else "resource_exhaustion"
    elif count == 0 or "no tests ran" in lower or "no tests found" in lower:
        status = "no_tests"
    elif code in {126, 127} or any(marker in lower for marker in setup):
        status = "setup_error"
    elif code == 0:
        status = "passed" if exercised else "unverified"
    elif ("assertionerror" in lower or "assertion failed" in lower or
          (code == 1 and (exercised or " failed" in lower))):
        status = "assertion_failure"
    else:
        status = "setup_error"
    return TestOutcome(status, code, count, seconds, output[-12000:])


def check(sandbox, command: str, timeout: int = 120) -> TestOutcome:
    start = time.monotonic()
    try:
        result = sandbox.command(["sh", "-lc", command], timeout=timeout)
        return classify(command, result.returncode, result.stdout + result.stderr,
                        time.monotonic() - start)
    except (subprocess.TimeoutExpired, ValueError) as exc:
        if "timed out" not in str(exc).lower() and not isinstance(exc, subprocess.TimeoutExpired):
            raise
        return TestOutcome("timeout", 124, None, time.monotonic() - start, str(exc))
