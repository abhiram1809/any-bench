"""Controlled local fixtures; production repository commands always use Docker."""
import os
from pathlib import Path
import subprocess
import tempfile

from anybench.repository import git
from anybench.sandbox import Sandbox, prepare_snapshot


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "--force", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD").strip()


class LocalSandbox(Sandbox):
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "repo"
        prepare_snapshot(self.case.repository, self.case.base_commit, self.root)
        self.baseline = self.root
        if self.evaluation_patch is not None:
            self.prepare_evaluation()
        self.assets = Path(self._tmp.name) / "evaluation"
        self.assets.mkdir()
        for name, text in self.evaluation_files.items():
            path = self.assets / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return self

    def command(self, argv, timeout=120, limit=2000000):
        argv = [item.replace('/evaluation', str(self.assets)).replace('/repo', str(self.root)) for item in argv]
        environment = dict(os.environ, PYTHONPATH=str(self.root), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run(argv, cwd=self.root, capture_output=True, text=True, timeout=timeout, env=environment)
