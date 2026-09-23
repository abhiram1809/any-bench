from __future__ import annotations

import glob as globlib
import os
import re
import selectors
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .model import Case


class ToolError(ValueError):
    pass


def validate_size(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*[kmg]", value):
        raise ValueError("size must be a positive value such as 512m or 2g")
    return value


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def _limited_run(argv: list[str], timeout: int, limit: int = 2_000_000) -> subprocess.CompletedProcess:
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    exceeded = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            ready = selector.select(remaining)
            if not ready:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in ready:
                data = os.read(key.fd, min(65536, limit - size + 1))
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                if size + len(data) > limit:
                    chunks.append(data[:limit - size])
                    exceeded = True
                    process.kill()
                    break
                chunks.append(data)
                size += len(data)
            if exceeded:
                break
        process.wait(timeout=1 if exceeded else max(0, deadline - time.monotonic()))
        output = b"".join(chunks).decode(errors="replace")
        if exceeded:
            output += "\n[tool output exceeded 2 MB limit]"
        return subprocess.CompletedProcess(argv, 124 if exceeded else process.returncode,
                                           output, "")
    except (subprocess.TimeoutExpired, TimeoutError):
        process.kill()
        process.wait(timeout=1)
        raise ToolError(f"Command timed out after {timeout}s")
    finally:
        selector.close()
        process.stdout.close()


def prepare_snapshot(repository: str, base_commit: str, root: Path) -> None:
    """Create a fresh Git baseline containing only the pre-change files."""
    source = root.parent / "source"
    clone = _run(["git", "clone", "--quiet", "--no-hardlinks", "--", repository, str(source)])
    if clone.returncode:
        raise RuntimeError(f"Clone failed: {clone.stderr}")
    checkout = _run(["git", "-C", str(source), "checkout", "--quiet", base_commit])
    if checkout.returncode:
        raise RuntimeError(f"Checkout failed: {checkout.stderr}")
    shutil.copytree(source, root, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    shutil.rmtree(source)
    for args in (["init", "-q"], ["add", "--force", "."], ["-c", "user.name=AnyBench",
                 "-c", "user.email=anybench@localhost", "commit", "-qm", "base snapshot"]):
        result = _run(["git", "-C", str(root), *args])
        if result.returncode:
            raise RuntimeError(f"Could not create clean base snapshot: {result.stderr}")


class Sandbox:
    """An isolated checkout with Docker for all repository command execution."""

    def __init__(self, case: Case, image: str = "anybench-sandbox:latest", memory: str = "1g",
                 workspace_size: str = "512m"):
        validate_size(memory)
        validate_size(workspace_size)
        self.case = case
        self.image = image
        self.memory = memory
        self.workspace_size = workspace_size
        self._tmp: tempfile.TemporaryDirectory | None = None
        self.root: Path | None = None
        self.container: str | None = None

    def __enter__(self) -> "Sandbox":
        self._tmp = tempfile.TemporaryDirectory(prefix="anybench-")
        self.root = Path(self._tmp.name) / "repo"
        try:
            prepare_snapshot(self.case.repository, self.case.base_commit, self.root)
        except Exception:
            self.__exit__(None, None, None)
            raise
        # Keep repository writes on a size-limited tmpfs, never a writable host mount.
        command = ["docker", "run", "-d", "--rm", "--network", "none",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--pids-limit", "128", "--memory", self.memory, "--cpus", "1",
                   "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
                   "--tmpfs", f"/repo:rw,exec,nosuid,nodev,size={self.workspace_size},mode=1777",
                   "--env", "HOME=/tmp",
                   "--user", f"{os.getuid()}:{os.getgid()}", "--workdir", "/repo",
                   "--mount", f"type=bind,src={self.root},dst=/seed,readonly",
                   self.image, "sleep", "infinity"]
        started = _run(command)
        if started.returncode:
            self.__exit__(None, None, None)
            raise RuntimeError(f"Docker failed: {started.stderr.strip()}")
        self.container = started.stdout.strip()
        copied = _run(["docker", "exec", self.container, "cp", "-a", "/seed/.", "/repo/"])
        if copied.returncode:
            self.__exit__(None, None, None)
            raise RuntimeError(f"Could not copy snapshot into bounded workspace: {copied.stderr.strip()}")
        return self

    def __exit__(self, *_exc) -> None:
        if self.container:
            _run(["docker", "rm", "-f", self.container])
            self.container = None
        if self._tmp:
            self._tmp.cleanup()
            self._tmp = None

    def _path(self, file_path: str) -> Path:
        if not self.root or not file_path or Path(file_path).is_absolute():
            raise ToolError("Expected a repository-relative path")
        path = (self.root / file_path).resolve()
        if not path.is_relative_to(self.root.resolve()) or path == self.root:
            raise ToolError("Path escapes repository")
        if ".git" in path.relative_to(self.root.resolve()).parts:
            raise ToolError("Git internals are not editable through tools")
        return path

    @staticmethod
    def _ranges(lines: list[str], ranges: list[list[int]] | None) -> str:
        if ranges is None:
            return "".join(lines)
        selected = []
        for pair in ranges:
            if len(pair) != 2 or pair[0] < 1 or pair[1] < pair[0]:
                raise ToolError("Ranges use [start, end], 1-based and inclusive")
            selected.extend(lines[pair[0] - 1:pair[1]])
        return "".join(selected)

    def read(self, file_path: str, lines_range: list[list[int]] | None = None) -> str:
        path = self._path(file_path)
        if not path.is_file():
            raise ToolError(f"File not found: {file_path}")
        if path.stat().st_size > 2_000_000:
            raise ToolError("File exceeds 2 MB tool limit")
        return self._ranges(path.read_text(encoding="utf-8").splitlines(keepends=True), lines_range)

    def write(self, file_path: str, content: str,
              line_range: list[list[int]] | None = None) -> str:
        path = self._path(file_path)
        if len(content.encode()) > 2_000_000:
            raise ToolError("Content exceeds 2 MB tool limit")
        if line_range is not None:
            if len(line_range) != 1 or not path.exists():
                raise ToolError("A ranged write needs one range in an existing file")
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            start, end = line_range[0]
            if start < 1 or end < start or end > len(lines):
                raise ToolError("Invalid write range")
            content = "".join(lines[:start - 1]) + content + "".join(lines[end:])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if self.container:
            target = Path("/repo") / path.relative_to(self.root.resolve())
            parent = _run(["docker", "exec", self.container, "mkdir", "-p", "--",
                           str(target.parent)])
            if parent.returncode:
                raise RuntimeError(f"Could not prepare workspace path: {parent.stderr.strip()}")
            written = _run(["docker", "exec", "-i", self.container, "sh", "-c",
                            f"cat > {shlex.quote(str(target))}"], input=content)
            if written.returncode:
                raise RuntimeError(f"Could not write edited file into bounded workspace: {written.stderr.strip()}")
        return f"Wrote {file_path}"

    def edit(self, file_path: str, content: str, line_range: list[list[int]]) -> str:
        return self.write(file_path, content, line_range)

    def command(self, argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
        if not self.container:
            raise RuntimeError("Sandbox is not running")
        return _limited_run(["docker", "exec", self.container, *argv], timeout)

    def bash(self, command: str) -> str:
        try:
            words = shlex.split(command)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        if not words or words[0] not in {"cat", "grep", "wc", "jq", "glob"}:
            raise ToolError("Allowed commands: cat, grep, glob, wc, jq")
        if any(word in {";", "|", "&&", "||", ">", "<"} for word in words):
            raise ToolError("Shell operators are not allowed")
        if words[0] == "glob":
            if len(words) != 2:
                raise ToolError("Usage: glob PATTERN")
            pattern = words[1]
            if pattern.startswith("/") or ".." in Path(pattern).parts:
                raise ToolError("Glob pattern escapes repository")
            return "\n".join(str(p.relative_to(self.root)) for p in sorted(
                Path(x) for x in globlib.glob(str(self.root / pattern), recursive=True)
                if Path(x).resolve().is_relative_to(self.root.resolve())))[:12000]
        result = self.command(words)
        return (result.stdout + result.stderr)[:12000]

    def test(self, command: str, timeout: int = 120) -> tuple[bool, str]:
        # Test commands are dataset metadata; execute only in the isolated container.
        result = self.command(["sh", "-lc", command], timeout=timeout)
        return result.returncode == 0, (result.stdout + result.stderr)[-12000:]

    def diff(self) -> str:
        if not self.root:
            raise RuntimeError("Sandbox is not running")
        added = _run(["git", "-C", str(self.root), "add", "-N", "--force", "."])
        if added.returncode:
            raise RuntimeError(f"Could not collect candidate files: {added.stderr}")
        result = _run(["git", "-C", str(self.root), "diff", "--no-ext-diff", "--"])
        if result.returncode:
            raise RuntimeError(f"Could not collect candidate diff: {result.stderr}")
        return result.stdout
