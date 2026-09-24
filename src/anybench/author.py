"""Host-agent case authoring without a separate builder endpoint."""
from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

from .dataset import _repository, case_from_annotation, git
from .model import Case


def commit_contexts(repositories: list[str], limit: int = 50) -> list[dict]:
    if limit < 1:
        raise ValueError("commits must be positive")
    contexts = []
    with ExitStack() as stack:
        for spec in repositories:
            repo = stack.enter_context(_repository(spec))
            source = str(repo.resolve()) if Path(spec).expanduser().exists() else spec
            identifier = hashlib.sha256(source.encode()).hexdigest()[:8]
            for commit in git(repo, "log", f"-{limit}", "--first-parent", "--format=%H").splitlines():
                parents = git(repo, "rev-list", "--parents", "-n", "1", commit).split()
                if len(parents) != 2:
                    continue
                parent = parents[1]
                diff = git(repo, "diff", "--no-ext-diff", "--find-renames", parent, commit, "--")
                if not diff.strip():
                    continue
                contexts.append({"case_id": f"{repo.name}-{identifier}-{commit[:12]}",
                                 "repository": source, "base_commit": parent,
                                 "target_commit": commit,
                                 "message": git(repo, "show", "-s", "--format=%B", commit)[:8000],
                                 "changed_files": git(repo, "diff", "--name-only", parent, commit, "--").splitlines(),
                                 "patch_excerpt": diff[:60000],
                                 "patch_excerpt_truncated": len(diff) > 60000})
    return contexts


def import_annotations(contexts: list[dict], annotations: list[dict]) -> tuple[list[Case], list[dict]]:
    known = {item["case_id"]: item for item in contexts}
    if len(known) != len(contexts):
        raise ValueError("Duplicate context case ID")
    cases: list[Case] = []
    decisions: list[dict] = []
    seen = set()
    with ExitStack() as stack:
        repos = {}
        for data in annotations:
            if not isinstance(data, dict) or "case_id" not in data:
                raise ValueError("Each annotation requires case_id")
            case_id = data["case_id"]
            if case_id not in known or case_id in seen:
                raise ValueError(f"Unknown or duplicate annotation: {case_id}")
            seen.add(case_id)
            forbidden = {"gold_diff", "base_commit", "target_commit", "repository"} & data.keys()
            if forbidden:
                raise ValueError(f"Annotation may not replace Git fields: {sorted(forbidden)}")
            if type(data.get("eligible")) is not bool:
                raise ValueError(f"Annotation eligible must be boolean: {case_id}")
            context = known[case_id]
            source = context["repository"]
            if source not in repos:
                repos[source] = stack.enter_context(_repository(source))
            repo = repos[source]
            identifier = hashlib.sha256(source.encode()).hexdigest()[:8]
            case = case_from_annotation(repo, source, identifier, context["target_commit"], data)
            if case is not None:
                if case.case_id != case_id or case.base_commit != context["base_commit"]:
                    raise ValueError(f"Git history changed for {case_id}")
                cases.append(case)
            decisions.append({"case_id": case_id, "accepted": case is not None,
                              "reason": data.get("reason", "")})
    return cases, decisions
