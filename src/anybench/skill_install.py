"""Install the bundled portable Agent Skill without clobbering local edits."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path

from .workflow import private_json
from . import __version__


def source_skill() -> Path:
    return Path(str(files("anybench").joinpath("skills", "anybench")))


def _digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name != ".anybench-install.json":
            digest.update(str(path.relative_to(directory)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def install_skill(agent: str, scope: str, project: Path | None = None,
                  home: Path | None = None, overwrite: bool = False) -> tuple[Path, str]:
    if agent not in {"claude", "portable"} or scope not in {"user", "project"}:
        raise ValueError("agent must be claude or portable; scope must be user or project")
    base = (home or Path.home()) if scope == "user" else (project or Path.cwd())
    directory = ".claude" if agent == "claude" else ".agents"
    destination = base / directory / "skills" / "anybench"
    source = source_skill()
    source_hash = _digest(source)
    if destination.exists():
        installed = destination / ".anybench-install.json"
        previous = json.loads(installed.read_text()) if installed.exists() else {}
        current_hash = _digest(destination)
        if current_hash == source_hash:
            return destination, "already current"
        if not overwrite and previous.get("digest") != current_hash:
            raise ValueError(f"Skill at {destination} has local changes; use --overwrite to replace")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="anybench-skill-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "anybench"
        shutil.copytree(source, staged)
        private_json(staged / ".anybench-install.json",
                     {"version": __version__, "digest": source_hash})
        backup = Path(temporary) / "previous"
        if destination.exists():
            destination.rename(backup)
        try:
            staged.rename(destination)
        except Exception:
            if backup.exists():
                backup.rename(destination)
            raise
    return destination, "installed"
