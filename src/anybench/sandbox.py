from __future__ import annotations

import glob as globlib
import json
import os
import re
import selectors
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import threading
from pathlib import Path

from .model import Case
from .repository import git, git_args, git_environment
from .metadata import case_metadata
from .execution import CANCELLED


class ToolError(ValueError):
    pass


def validate_size(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*[kmg]", value):
        raise ValueError("size must be a positive value such as 512m or 2g")
    return value


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("timeout", 120)
    if argv[0] == "git":
        kwargs.setdefault("env", git_environment())
        argv = git_args(None, *argv[1:])
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def _limited_run(argv: list[str], timeout: int, limit: int = 2_000_000,
                 input_text: str | None = None) -> subprocess.CompletedProcess:
    stdin = tempfile.TemporaryFile() if input_text is not None else None
    if stdin is not None:
        stdin.write(input_text.encode())
        stdin.seek(0)
    try:
        process = subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    finally:
        if stdin is not None:
            stdin.close()
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    exceeded = False
    try:
        while selector.get_map():
            if CANCELLED.is_set():
                raise TimeoutError("Run cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            ready = selector.select(min(remaining, .5))
            if not ready:
                continue
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
            output += f"\n[tool output exceeded {limit} byte limit]"
        return subprocess.CompletedProcess(argv, 124 if exceeded else process.returncode,
                                           output, "")
    except (subprocess.TimeoutExpired, TimeoutError):
        process.kill()
        process.wait(timeout=1)
        error = ToolError("Run cancelled" if CANCELLED.is_set() else f"Command timed out after {timeout}s")
        error.output = b"".join(chunks).decode(errors="replace")
        raise error
    finally:
        selector.close()
        process.stdout.close()


def prepare_snapshot(repository: str, base_commit: str, root: Path) -> None:
    """Cache only full commit IDs, and copy into a private per-attempt checkout."""
    from .workflow import fingerprint, output_lock, private_json
    if not re.fullmatch(r"[a-fA-F0-9]{40,64}", base_commit) or os.environ.get("ANYBENCH_NO_CACHE") == "1":
        _prepare_snapshot(repository, base_commit, root)
        return
    cache = Path(os.environ.get("ANYBENCH_CACHE_DIR", ".anybench/cache/snapshots"))
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = fingerprint([repository, base_commit])
    stored = cache / key
    with output_lock(stored, wait_seconds=180):
        if not (stored / "complete.json").is_file():
            with tempfile.TemporaryDirectory(dir=cache, prefix="snapshot-") as directory:
                staged = Path(directory)
                _prepare_snapshot(repository, base_commit, staged / "repo")
                private_json(staged / "complete.json", {"repository": repository, "commit": base_commit})
                if stored.exists():
                    shutil.rmtree(stored)
                staged.rename(stored)
        shutil.copytree(stored / "repo", root, symlinks=True)


def _prepare_snapshot(repository: str, base_commit: str, root: Path) -> None:
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


def _extract_checkout(archive: tarfile.TarFile, target: Path, size_limit: int) -> None:
    """Extract a container checkout without allowing archive paths outside target."""
    root = target.resolve()
    directories: list[tuple[Path, int]] = []
    size = 0
    for index, member in enumerate(archive):
        if index > 100_000:
            raise RuntimeError("Harness archive contains too many entries")
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError("Harness archive contains an unsafe path")
        # Never materialize agent-owned Git configuration, hooks, or object databases.
        if ".git" in path.parts:
            continue
        destination = (target / path).resolve()
        if not destination.is_relative_to(root):
            raise RuntimeError("Harness archive path escapes checkout")
        size += member.size
        if size > size_limit:
            raise RuntimeError("Harness archive exceeds checkout size limit")
        if member.isdir():
            destination.mkdir(parents=True, exist_ok=True)
            directories.append((destination, member.mode))
        elif member.isfile():
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("Could not read harness archive file")
            with source, destination.open("wb") as stream:
                shutil.copyfileobj(source, stream)
            destination.chmod(member.mode & 0o777)
        elif member.issym() or member.islnk():
            link = Path(member.linkname)
            origin = (destination.parent / link if member.issym() else target / link).resolve()
            if (link.is_absolute() or not origin.is_relative_to(root)
                    or ".git" in origin.relative_to(root).parts):
                raise RuntimeError("Harness archive link escapes checkout")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if member.issym():
                destination.symlink_to(member.linkname)
            else:
                os.link(origin, destination)
        else:
            raise RuntimeError("Harness archive contains an unsupported file type")
    for directory, mode in reversed(directories):
        directory.chmod(mode & 0o777)


class Sandbox:
    """An isolated checkout with Docker for all repository command execution."""

    def __init__(self, case: Case, image: str = "anybench-sandbox:latest", memory: str = "1g",
                 workspace_size: str = "512m", network: str = "none",
                 environment: dict[str, str] | None = None,
                 evaluation_files: dict[str, str] | None = None,
                 evaluation_patch: str | None = None,
                 protected_paths: list[str] | None = None):
        validate_size(memory)
        validate_size(workspace_size)
        self.case = case
        self.image = image
        self.memory = memory
        self.workspace_size = workspace_size
        self.network = network
        self.environment = environment or {}
        self.evaluation_files = evaluation_files or {}
        self.evaluation_patch = evaluation_patch
        self.protected_paths = protected_paths or []
        self._tmp: tempfile.TemporaryDirectory | None = None
        self.root: Path | None = None
        self.container: str | None = None
        self.baseline: Path | None = None

    def __enter__(self) -> "Sandbox":
        self._tmp = tempfile.TemporaryDirectory(prefix="anybench-")
        self.root = Path(self._tmp.name) / "repo"
        try:
            prepare_snapshot(case_metadata(self.case).get("repository_alias", self.case.repository),
                             self.case.base_commit, self.root)
            self.baseline = self.root
            if self.evaluation_patch is not None:
                self.prepare_evaluation()
        except Exception:
            self.__exit__(None, None, None)
            raise
        # Keep repository writes on a size-limited tmpfs, never a writable host mount.
        command = ["docker", "run", "-d", "--rm", "--network", self.network,
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--pids-limit", "128", "--memory", self.memory, "--cpus", "1",
                   "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
                   "--tmpfs", f"/repo:rw,exec,nosuid,nodev,size={self.workspace_size},mode=1777",
                   "--tmpfs", "/work:rw,exec,nosuid,nodev,size=256m",
                   "--env", "HOME=/tmp",
                   "--env", "TMPDIR=/work", "--env", "GOTMPDIR=/work",
                   "--user", f"{os.getuid()}:{os.getgid()}", "--workdir", "/repo",
                   "--mount", f"type=bind,src={self.root},dst=/seed,readonly"]
        if self.evaluation_patch is not None:
            index = command.index(f"/repo:rw,exec,nosuid,nodev,size={self.workspace_size},mode=1777")
            del command[index - 1:index + 1]
            command.extend(["--mount", f"type=bind,src={self.root},dst=/repo,readonly",
                            "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPATH=/repo"])
        if self.evaluation_files:
            assets = Path(self._tmp.name) / "evaluation"
            assets.mkdir(mode=0o700)
            for name, content in self.evaluation_files.items():
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
                    raise ValueError("Unsafe evaluation asset path")
                destination = assets / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            command.extend(["--mount", f"type=bind,src={assets},dst=/evaluation,readonly"])
        for name, value in self.environment.items():
            command.extend(["--env", f"{name}={value}"])
        command.extend([self.image, "sleep", "infinity"])
        started = _run(command)
        if started.returncode:
            self.__exit__(None, None, None)
            raise RuntimeError(f"Docker failed: {started.stderr.strip()}")
        self.container = started.stdout.strip()
        if self.evaluation_patch is not None:
            return self
        copied = _run(["docker", "exec", self.container, "cp", "-R", "/seed/.", "/repo/"])
        if copied.returncode:
            self.__exit__(None, None, None)
            raise RuntimeError(f"Could not copy snapshot into bounded workspace: {copied.stderr.strip()}")
        return self

    def prepare_evaluation(self) -> None:
        """Apply a patch using trusted metadata and restore authoritative tests."""
        assert self.root is not None and self.evaluation_patch is not None
        def test_asset(path: Path) -> bool:
            return ("tests" in path.parts or "test" in path.parts or
                    path.name.startswith("test_") or path.name in
                    {"conftest.py", "pytest.ini"} or
                    ".test." in path.name or ".spec." in path.name)

        baseline_files = {str(path.relative_to(self.root)) for path in self.root.rglob("*")
                          if (path.is_file() or path.is_symlink()) and ".git" not in path.parts}
        protected = list(self.protected_paths)
        if any(Path(path).is_absolute() or not path or
               set(Path(path).parts) & {"..", ".git"} for path in protected):
            raise ValueError("Unsafe protected test path")
        protected.extend(path for path in baseline_files if test_asset(Path(path)))
        if self.evaluation_patch.strip():
            applied = _run(["git", "-C", str(self.root), "apply", "--binary",
                            "--whitespace=nowarn", "-"], input=self.evaluation_patch)
            if applied.returncode:
                raise ToolError(f"Patch cannot be applied: {applied.stderr[-2000:]}")
        # New candidate tests and collection hooks are not authoritative either.
        for path in self.root.rglob("*"):
            relative = str(path.relative_to(self.root))
            if (relative not in baseline_files and test_asset(Path(relative)) and
                    (path.is_file() or path.is_symlink())):
                path.unlink()
        for path in sorted(set(protected)):
            if path not in baseline_files:
                candidate = self.root / path
                if candidate.is_file() or candidate.is_symlink():
                    candidate.unlink()
                continue
            restored = _run(["git", "-C", str(self.root), "checkout", "HEAD", "--", path])
            if restored.returncode:
                raise ToolError(f"Cannot restore authoritative test: {path}")

    def __exit__(self, *_exc) -> None:
        try:
            if self.container:
                _run(["docker", "rm", "-f", self.container], timeout=30)
        finally:
            self.container = None
            if self._tmp:
                self._tmp.cleanup()
                self._tmp = None

    def quiesce(self) -> None:
        """Stop leftover candidate processes before collecting their filesystem."""
        if self.container:
            _run(["docker", "exec", self.container, "sh", "-c",
                  'for p in /proc/[0-9]*; do p=${p##*/}; '
                  '[ "$p" = 1 ] || [ "$p" = "$$" ] || kill -KILL "$p" 2>/dev/null || :; done'],
                 timeout=10)

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

    def command(self, argv: list[str], timeout: int = 60,
                limit: int = 2_000_000) -> subprocess.CompletedProcess:
        if not self.container:
            raise RuntimeError("Sandbox is not running")
        return _limited_run(["docker", "exec", self.container, *argv], timeout, limit)

    def enhanced_tool(self, operation: str, arguments: dict) -> dict:
        """All enhanced tools operate on the live container checkout."""
        if not self.container:
            raise RuntimeError("Sandbox is not running")
        worker = Path(__file__).with_name("workspace_tool.py").read_text()
        timeout = arguments.get("timeout_seconds", 120) if operation == "Run" else 60
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ToolError("timeout_seconds must be between 1 and 300")
        result = _limited_run(["docker", "exec", "-i", self.container, "python", "-c", worker],
                              timeout + 10, limit=16_000_000,
                              input_text=json.dumps({"operation": operation, "arguments": arguments}))
        if result.returncode:
            raise ToolError(f"Workspace worker failed: {result.stdout}")
        response = json.loads(result.stdout)
        if "error" in response:
            raise ToolError(response["error"])
        return response

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

    def apply_patch(self, patch: str) -> None:
        if not patch.strip():
            return
        if not self.container:
            raise RuntimeError("Sandbox is not running")
        result = _limited_run(["docker", "exec", "-i", self.container, "git", "apply",
                               "--binary", "--whitespace=nowarn", "-"], 60,
                              input_text=patch)
        if result.returncode:
            raise ToolError(f"Patch cannot be applied: {result.stdout[-2000:]}")

    def diff(self) -> str:
        if not self.root:
            raise RuntimeError("Sandbox is not running")
        git(self.root, "add", "-N", "--force", ".")
        return git(self.root, "diff", "--binary", "--no-textconv", "--no-ext-diff",
                   "HEAD", "--")

    def collect_container_diff(self, trusted_baseline: bool = True) -> str:
        if not self.container or not self.root:
            raise RuntimeError("Sandbox is not running")
        target = self.root.parent / "collected"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(mode=0o700)
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(["docker", "exec", self.container, "tar", "-C",
                                        "/repo", "-cf", "-", "."],
                                       stdout=subprocess.PIPE, stderr=errors)
            assert process.stdout is not None
            timer = threading.Timer(120, process.kill)
            timer.start()
            try:
                with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                    unit = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
                    size_limit = int(self.workspace_size[:-1]) * unit[self.workspace_size[-1]]
                    _extract_checkout(archive, target, size_limit)
            except Exception:
                process.kill()
                process.wait()
                raise
            finally:
                timer.cancel()
                process.stdout.close()
            if process.wait() != 0:
                errors.seek(0)
                error = errors.read().decode(errors="replace")
                raise RuntimeError(f"Could not collect harness checkout: {error.strip()}")
        # The compatibility argument cannot opt out of this trust boundary.
        shutil.copytree((self.baseline or self.root) / ".git", target / ".git")
        if not (target / ".git").is_dir():
            raise RuntimeError(f"Harness checkout lacks Git baseline: {[p.name for p in target.iterdir()]}")
        self.root = target
        return self.diff()
