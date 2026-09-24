"""Private, resumable workflow files and deterministic input checks."""
from __future__ import annotations

import hashlib
import json
import os
import fcntl
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def fingerprint(value: Any) -> str:
    def convert(item: Any) -> Any:
        if is_dataclass(item):
            return convert(asdict(item))
        if isinstance(item, Path):
            return str(item.resolve())
        if isinstance(item, dict):
            return {str(key): convert(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [convert(val) for val in item]
        return item
    data = json.dumps(convert(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def manifest_path(output: Path) -> Path:
    return output.with_name(output.name + ".manifest.json")


def ensure_private_parent(path: Path) -> None:
    missing = []
    directory = path.parent
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700)


def private_json(path: Path, value: Any) -> None:
    ensure_private_parent(path)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_output(output: Path, inputs: Any, *, resume: bool = False,
                   overwrite: bool = False) -> bool:
    """Validate a result destination before writing; return whether resuming."""
    if resume and overwrite:
        raise ValueError("--resume and --overwrite are mutually exclusive")
    manifest = manifest_path(output)
    expected = fingerprint(inputs)
    present = output.exists() or manifest.exists()
    if resume:
        if not output.exists() or not manifest.exists():
            raise ValueError("Resume requires both output and its manifest")
        old = json.loads(manifest.read_text(encoding="utf-8"))
        if old.get("fingerprint") != expected:
            raise ValueError("Resume inputs differ from the frozen session manifest")
        return True
    if present and not overwrite:
        raise ValueError(f"Output already exists: {output}; use --resume or --overwrite")
    if present and overwrite:
        output.unlink(missing_ok=True)
    private_json(manifest, {"schema": 1, "fingerprint": expected, "inputs": inputs})
    return False


def record_key(record: Any) -> tuple[str, str, int, int]:
    return (record.case_id, record.model, record.concurrency, record.attempt)


def read_complete_jsonl(path: Path, make: Any, *, repair: bool = False) -> list[Any]:
    """Read complete records; repair a torn final line only with explicit permission."""
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    values = []
    for index, line in enumerate(lines):
        if not line.endswith(b"\n"):
            if index != len(lines) - 1:
                raise ValueError(f"Corrupt JSONL in {path}")
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                if not repair:
                    raise ValueError(f"Torn final JSONL record in {path}; resume to repair")
                with path.open("r+b") as stream:
                    stream.truncate(len(raw) - len(line))
            else:
                values.append(make(parsed))
                if repair:
                    with path.open("ab") as stream:
                        stream.write(b"\n")
            break
        values.append(make(json.loads(line)))
    return values


def append_dict_jsonl(path: Path, value: dict) -> None:
    ensure_private_parent(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def output_lock(output: Path, wait_seconds: float = 0):
    """A stable lock inode prevents concurrent invocations from interleaving writes."""
    ensure_private_parent(output)
    path = output.with_name(output.name + ".lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ValueError(f"Output is locked by another AnyBench process: {output}") from exc
                time.sleep(.05)
        yield
    finally:
        os.close(descriptor)


def reserved_paths(output: Path) -> set[Path]:
    return {output.resolve(), *(output.with_name(output.name + suffix).resolve() for suffix in
                               (".manifest.json", ".metadata.json", ".events.jsonl", ".journal.jsonl",
                                ".decisions.json", ".proposals.json", ".contexts.json", ".validation.json", ".lock"))}
