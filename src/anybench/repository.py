"""Bounded Git operations with host configuration and executable hooks disabled."""
from __future__ import annotations

import os
import selectors
import subprocess
import tempfile
import time
from pathlib import Path

from .execution import CANCELLED


class OutputLimitError(ValueError):
    """A repository command produced more data than its caller permits."""


def git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0", GIT_ATTR_NOSYSTEM="1")
    return environment


def git_args(repo: Path | None, *args: str) -> list[str]:
    return ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "core.attributesFile=/dev/null", "-c", "init.templateDir=",
            *(["-C", str(repo)] if repo is not None else []), *args]


def git(repo: Path, *args: str, input: str | None = None,
        max_output_bytes: int = 16_000_000) -> str:
    command = git_args(repo, *args)
    stdin = tempfile.TemporaryFile() if input is not None else None
    if stdin is not None:
        assert input is not None
        stdin.write(input.encode())
        stdin.seek(0)
    try:
        process = subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=git_environment())
    finally:
        if stdin is not None:
            stdin.close()
    assert process.stdout is not None and process.stderr is not None
    output = {process.stdout: bytearray(), process.stderr: bytearray()}
    selector = selectors.DefaultSelector()
    for stream in output:
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + 120
    try:
        while selector.get_map():
            if CANCELLED.is_set():
                raise RuntimeError("Git command cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, 120)
            for key, _ in selector.select(min(remaining, .5)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                stream = process.stdout if key.fileobj == process.stdout else process.stderr
                output[stream].extend(chunk)
                if sum(map(len, output.values())) > max_output_bytes:
                    raise OutputLimitError(f"Git output exceeds {max_output_bytes} bytes")
        code = process.wait(timeout=max(0, deadline - time.monotonic()))
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    stdout, stderr = (bytes(output[stream]).decode(errors="replace") for stream in output)
    if code:
        raise subprocess.CalledProcessError(code, command, stdout, stderr)
    return stdout


def revision(repo: Path, value: str) -> str:
    return git(repo, "rev-parse", "--verify", "--end-of-options", f"{value}^{{commit}}").strip()
