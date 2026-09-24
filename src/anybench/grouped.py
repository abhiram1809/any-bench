"""Opt-in reconstruction of focused tasks from a frozen historical window."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import tempfile

from .dataset import _repository
from .metadata import case_metadata
from .model import Case
from .repository import OutputLimitError, git, revision
from .sandbox import prepare_snapshot
from .workflow import fingerprint


def discover(repositories: list[str], limit: int = 50, revision_name: str = "HEAD",
             since: str | None = None, until: str | None = None,
             paths: list[str] | None = None, max_patch_bytes: int = 500_000,
             seed: int = 0) -> list[dict]:
    if limit < 1 or max_patch_bytes < 1:
        raise ValueError("History and patch limits must be positive")
    contexts = []
    for spec in repositories:
        with _repository(spec) as repo:
            source = str(repo) if Path(spec).expanduser().exists() else spec
            head = revision(repo, revision_name)
            args = ["log", "--first-parent", f"-{limit}", "--format=%H"]
            if since:
                args.append(f"--since={since}")
            if until:
                args.append(f"--until={until}")
            commits = git(repo, *args, head, "--", *(paths or [])).splitlines()
            for commit in reversed(commits):
                parents = git(repo, "rev-list", "--parents", "-n", "1", commit).split()[1:]
                entry = {"repository": source, "frozen_head": head, "commit": commit,
                         "parents": parents, "selection_seed": seed}
                if not parents:
                    contexts.append({**entry, "eligible": False, "reason": "root commit"})
                    continue
                try:
                    patch = git(repo, "diff", "--binary", "--no-ext-diff", "--no-textconv",
                                parents[0], commit, "--", max_output_bytes=max_patch_bytes + 1)
                except OutputLimitError:
                    contexts.append({**entry, "base_commit": parents[0], "eligible": False,
                                     "reason": "patch too large"})
                    continue
                message = git(repo, "show", "-s", "--format=%B", commit)
                changed = git(repo, "diff", "--name-only", parents[0], commit, "--").splitlines()
                entry.update(base_commit=parents[0], message=message[:8000], changed_files=changed,
                             issue_refs=re.findall(r"(?:#\d+|[A-Z][A-Z0-9]+-\d+)", message),
                             symbols=re.findall(r"^@@.*?@@\s*(.*)$", patch, re.MULTILINE),
                             patch_excerpt=patch[:16000], patch_bytes=len(patch.encode()),
                             patch_excerpt_truncated=len(patch) > 16000,
                             eligible=bool(patch.strip()) and len(patch.encode()) <= max_patch_bytes,
                             reason="patch too large" if len(patch.encode()) > max_patch_bytes else "",
                             merge_constituents=(git(repo, "rev-list", f"{parents[0]}..{parents[1]}").splitlines()
                                                 if len(parents) > 1 else []))
                contexts.append(entry)
    return contexts


def reconstruct(repo: Path, commits: list[str]) -> tuple[str, str, str]:
    if not commits or len(set(commits)) != len(commits):
        raise ValueError("Select a nonempty, unique ordered commit sequence")
    commits = [revision(repo, value) for value in commits]
    parent = git(repo, "rev-list", "--parents", "-n", "1", commits[0]).split()[1:]
    if not parent:
        raise ValueError("A root commit cannot start a task")
    base = parent[0]
    for before, after in zip(commits, commits[1:]):
        try:
            git(repo, "merge-base", "--is-ancestor", before, after)
        except subprocess.CalledProcessError as exc:
            raise ValueError("Selected commits must follow ancestry order") from exc
    with tempfile.TemporaryDirectory(prefix="anybench-reconstruct-") as temp:
        checkout = Path(temp) / "repo"
        prepare_snapshot(str(repo), base, checkout)
        git(checkout, "update-index", "--refresh")
        for commit in commits:
            parents = git(repo, "rev-list", "--parents", "-n", "1", commit).split()[1:]
            patch = git(repo, "diff", "--binary", "--no-ext-diff", "--no-textconv",
                        parents[0], commit, "--")
            try:
                git(checkout, "apply", "--index", "--binary", "--whitespace=nowarn", "-", input=patch)
            except subprocess.CalledProcessError as exc:
                raise ValueError(f"Focused reconstruction conflicts at {commit}: {exc.stderr[-1000:]}") from exc
        patch = git(checkout, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--")
        tree = git(checkout, "write-tree").strip()
    if not patch.strip():
        raise ValueError("Selected fixes have no net change (possibly reverted)")
    return base, patch, tree


def import_groups(contexts: list[dict], proposals: list[dict]) -> tuple[list[Case], list[dict]]:
    known = {(entry["repository"], entry["commit"]): entry for entry in contexts}
    cases, decisions = [], []
    used: set[tuple[str, str]] = set()
    patches: set[str] = set()
    for proposal in proposals:
        source = proposal.get("repository", "")
        selected = proposal.get("selected_commits", [])
        decision = {"repository": source, "selected_commits": selected, "accepted": False}
        try:
            if not isinstance(selected, list) or not selected or any(not isinstance(c, str) for c in selected):
                raise ValueError("selected_commits must be a nonempty commit array")
            entries = [known[(source, commit)] for commit in selected]
            if any(not entry.get("eligible") for entry in entries):
                raise ValueError("Group includes a filtered or ineligible commit")
            if any((source, commit) in used for commit in selected):
                raise ValueError("Group overlaps an already accepted task")
            constituents = {commit for entry in entries for commit in entry.get("merge_constituents", [])}
            if constituents.intersection(selected) or any((source, commit) in used for commit in constituents):
                raise ValueError("Merge and constituent changes would be counted twice")
            evidence = proposal.get("evidence", [])
            if {item.get("commit") for item in evidence} != set(selected):
                raise ValueError("Every selected commit requires grouping evidence")
            for item in evidence:
                entry = known[(source, item["commit"])]
                if not isinstance(item.get("reason"), str) or not item["reason"].strip():
                    raise ValueError("Grouping evidence requires a causal explanation")
                if not set(item.get("paths", [])).intersection(entry["changed_files"]):
                    raise ValueError("Grouping evidence must identify actual changed paths")
            problem = proposal.get("problem_statement", "")
            if not isinstance(problem, str) or not problem.strip():
                raise ValueError("A behavioral problem statement is required")
            files = proposal.get("evaluation_files", {})
            command = proposal.get("evaluation_command", proposal.get("test_command", ""))
            if not isinstance(files, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                   for k, v in files.items()):
                raise ValueError("evaluation_files must map relative paths to text")
            for name in files:
                if not name or Path(name).is_absolute() or set(Path(name).parts) & {"..", ".git"}:
                    raise ValueError("Unsafe evaluation file path")
            if not command or not isinstance(command, str):
                raise ValueError("An independent evaluation command is required")
            if not proposal.get("regression_command"):
                raise ValueError("An unchanged regression command is required")
            with _repository(source) as repo:
                base, patch, tree = reconstruct(repo, selected)
            patch_hash = fingerprint(re.sub(r"^index .*$", "", patch, flags=re.MULTILINE))
            if patch_hash in patches:
                raise ValueError("Equivalent reference patch already accepted")
            identifier = fingerprint([source, selected])[:16]
            case = Case(f"{Path(source).stem}-group-{identifier}", source, base, selected[-1],
                        problem, str(proposal.get("hint", "")), patch, command)
            case._metadata = {"kind": "group", "selected_commits": selected, "evidence": evidence,
                              "reconstruction_tree": tree, "reference_sha256": fingerprint(patch),
                              "frozen_head": entries[0]["frozen_head"], "evaluation_files": files,
                              "evaluation_command": command,
                              "regression_command": proposal["regression_command"],
                              "test_provenance": proposal.get("test_provenance", "host-authored"),
                              "task_type": proposal.get("task_type", "bugfix"),
                              "languages": sorted({Path(p).suffix for e in entries for p in e["changed_files"]}),
                              "selection_seed": entries[0].get("selection_seed", 0),
                              "validation": {"status": "pending"}}
            cases.append(case)
            patches.add(patch_hash)
            used.update((source, commit) for commit in [*selected, *constituents])
            decision.update(accepted=True, case_id=case.case_id, reason="reconstructed; verification pending")
        except (ValueError, KeyError, TypeError, subprocess.SubprocessError, OSError) as exc:
            decision["reason"] = str(exc)
        decisions.append(decision)
    for key, entry in known.items():
        if key not in used:
            decisions.append({"repository": key[0], "selected_commits": [key[1]], "accepted": False,
                              "reason": entry.get("reason") or "not selected in an accepted group"})
    return cases, decisions


def validate_group(case: Case, repo: Path) -> None:
    metadata = case_metadata(case)
    selected = metadata.get("selected_commits", [])
    base, patch, tree = reconstruct(repo, selected)
    if (case.base_commit != base or case.target_commit != selected[-1] or case.gold_diff != patch
            or metadata.get("reconstruction_tree") != tree):
        raise ValueError("Grouped case does not match reproducible reconstruction")
