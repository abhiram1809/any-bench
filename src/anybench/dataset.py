from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .llm import ChatClient, parse_json_object
from .model import Case
from .repository import OutputLimitError, git, git_args, git_environment
from .metadata import case_metadata


BUILDER_TOOLS = [
    {"type": "function", "function": {"name": "ReadRevision",
     "description": "Read a file at the parent or target commit for task context.",
     "parameters": {"type": "object", "properties": {
         "revision": {"type": "string", "enum": ["parent", "target"]},
         "file_path": {"type": "string"}, "start": {"type": "integer"},
         "length": {"type": "integer"}}, "required": ["revision", "file_path"]}}},
    {"type": "function", "function": {"name": "ReadPatch",
     "description": "Read a bounded character range of the complete patch.",
     "parameters": {"type": "object", "properties": {"start": {"type": "integer"},
                     "length": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {"name": "RecentCommits",
     "description": "Read nearby historical commit messages.",
     "parameters": {"type": "object", "properties": {}, "required": []}}},
]


def analyze_commit(client: ChatClient, repo: Path, parent: str, commit: str,
                   prompt: str, use_tools: bool = True) -> dict:
    messages = [{"role": "user", "content": prompt}]
    for turn in range(8):
        # The final call must ask for a decision, even when inspection keeps
        # producing tool requests. Small complete patches need no tools at all.
        tools = BUILDER_TOOLS if use_tools and turn < 4 else None
        reply = client.complete(messages, tools)
        message = reply.message
        messages.append(message)
        tool_calls = message.get("tool_calls") or []
        if tool_calls and tools is None:
            raise ValueError(f"Dataset agent requested unavailable tools for {commit}")
        if not tool_calls:
            raw = message.get("content") or ""
            return parse_json_object(raw)
        for call in tool_calls:
            try:
                arguments = json.loads(call["function"]["arguments"])
                name = call["function"]["name"]
                if name == "ReadRevision":
                    revision = {"parent": parent, "target": commit}[arguments["revision"]]
                    path = arguments["file_path"]
                    if path.startswith("/") or ".." in Path(path).parts:
                        raise ValueError("Invalid repository path")
                    full = git(repo, "show", f"{revision}:{path}")
                    start, length = arguments.get("start", 0), arguments.get("length", 20000)
                    if type(start) is not int or type(length) is not int or start < 0 or not 1 <= length <= 20000:
                        raise ValueError("Invalid character range")
                    output = f"Characters {start}:{start + length} of {len(full)}\n" + full[start:start + length]
                elif name == "ReadPatch":
                    full = git(repo, "diff", "--no-ext-diff", "--no-textconv", parent, commit, "--")
                    start, length = arguments.get("start", 0), arguments.get("length", 20000)
                    if type(start) is not int or type(length) is not int or start < 0 or not 1 <= length <= 20000:
                        raise ValueError("Invalid character range")
                    output = f"Characters {start}:{start + length} of {len(full)}\n" + full[start:start + length]
                elif name == "RecentCommits":
                    output = git(repo, "log", "-10", "--format=%h %s", commit)[:5000]
                else:
                    raise ValueError(f"Unknown builder tool: {name}")
            except (ValueError, KeyError, subprocess.CalledProcessError) as exc:
                output = f"Tool error: {exc}"
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
    raise ValueError(f"Dataset agent exceeded step limit for {commit}")


@contextmanager
def _repository(spec: str):
    local = Path(spec).expanduser()
    if local.exists():
        yield local.resolve(strict=True)
    elif spec.startswith(("https://", "ssh://", "git@")):
        with tempfile.TemporaryDirectory(prefix="anybench-build-") as temp:
            checkout = Path(temp) / "repo"
            subprocess.run(git_args(None, "clone", "--quiet", "--", spec, str(checkout)),
                           check=True, capture_output=True, text=True, timeout=120,
                           env=git_environment())
            yield checkout
    else:
        raise ValueError(f"Repository is not a local path or supported Git URL: {spec}")


def build_dataset(repositories: list[str | Path], client: ChatClient, commits: int = 50,
                  max_cases: int | None = None, existing_cases: list[Case] | None = None,
                  on_case: Callable[[Case], None] | None = None,
                  on_decision: Callable[[str, str, bool], None] | None = None,
                  completed: set[tuple[str, str]] | None = None,
                  frozen_revisions: dict[str, list[str]] | None = None,
                  max_patch_bytes: int = 500_000,
                  on_event: Callable[[dict], None] | None = None) -> list[Case]:
    if commits < 1 or max_cases is not None and max_cases < 1:
        raise ValueError("commits and max_cases must be positive")
    cases: list[Case] = list(existing_cases or [])
    seen = {(case.repository, case.target_commit) for case in cases}
    if max_cases is not None and len(cases) >= max_cases:
        return cases
    with ExitStack() as stack:
        sources: list[tuple[Path, str, str, list[str]]] = []
        for spec in repositories:
            repo = stack.enter_context(_repository(str(spec)))
            source = str(repo.resolve()) if Path(str(spec)).expanduser().exists() else str(spec)
            identifier = hashlib.sha256(source.encode()).hexdigest()[:8]
            git(repo, "rev-parse", "--is-inside-work-tree")
            revisions = (frozen_revisions[source] if frozen_revisions is not None else
                         git(repo, "log", f"-{commits}", "--first-parent", "--format=%H").splitlines())
            sources.append((repo, source, identifier, revisions))
        for index in range(max((len(item[3]) for item in sources), default=0)):
            for repo, source, identifier, revisions in sources:
                if (index >= len(revisions) or (source, revisions[index]) in seen or
                        (source, revisions[index]) in (completed or set())):
                    continue
                before = (getattr(client, "prompt_tokens", 0), getattr(client, "completion_tokens", 0))
                if on_event:
                    on_event({"event": "commit_started", "repository": source, "commit": revisions[index]})
                try:
                    case = _case_from_commit(client, repo, source, identifier, revisions[index], max_patch_bytes)
                except Exception as exc:
                    if on_event:
                        on_event({"event": "commit_error", "repository": source, "commit": revisions[index],
                                  "error": str(exc), "prompt_tokens": getattr(client, "prompt_tokens", 0) - before[0],
                                  "completion_tokens": getattr(client, "completion_tokens", 0) - before[1]})
                    raise
                if on_event:
                    on_event({"event": "commit_completed", "repository": source, "commit": revisions[index],
                              "accepted": case is not None,
                              "case": asdict(case) if case is not None else None,
                              "prompt_tokens": getattr(client, "prompt_tokens", 0) - before[0],
                              "completion_tokens": getattr(client, "completion_tokens", 0) - before[1]})
                if case is None:
                    if on_decision:
                        on_decision(source, revisions[index], False)
                    continue
                cases.append(case)
                seen.add((source, case.target_commit))
                if on_case:
                    on_case(case)
                if on_decision:
                    on_decision(source, revisions[index], True)
                if max_cases is not None and len(cases) >= max_cases:
                    return cases
    return cases


def _case_from_commit(client: ChatClient, repo: Path, source: str,
                      identifier: str, commit: str, max_patch_bytes: int = 500_000) -> Case | None:
    parents = git(repo, "rev-list", "--parents", "-n", "1", commit).split()
    if len(parents) != 2:  # merge commits and root commits are ambiguous cases
        return None
    parent = parents[1]
    try:
        diff = git(repo, "diff", "--no-ext-diff", "--find-renames", parent, commit, "--",
                   max_output_bytes=max_patch_bytes + 1)
    except OutputLimitError:
        return None
    if not diff.strip():
        return None
    if len(diff.encode()) > max_patch_bytes:
        return None
    # The original diff remains exact in the CSV. The model gets a bounded excerpt.
    context = git(repo, "show", "-s", "--format=%B", commit)
    changed = git(repo, "diff", "--name-only", parent, commit, "--")
    prompt = (
        "Create a coding benchmark task from this historical commit. Infer only facts "
        "supported by the message and patch. Return one JSON object with keys "
        "eligible (boolean), problem_statement, hint, test_command, "
        "external_validation (boolean), external_validation_reason. Set eligible=false "
        "for documentation-only, formatting-only, or otherwise unsuitable commits. "
        "For eligible commits, the problem statement must describe the requested "
        "behavior without revealing the solution. Use an empty test_command when no "
        "safe, repository-local check can be inferred. A test_command must run on both "
        "the parent and target snapshots without depending on files introduced only "
        "by the target. Never select tests or test names added by the target commit: "
        "the evaluator restores the parent's tests. Prefer a short self-contained "
        "assertion using existing code and only tools present in the sandbox. The "
        "command must be valid shell and Python syntax, fail on the parent, and pass "
        "on the target. If that cannot be established, leave test_command empty. "
        "Mark external_validation true "
        "for cases needing services or conditions unavailable in a local sandbox.\n\n"
        f"Commit message:\n{context[:8000]}\nChanged files:\n{changed[:8000]}\n"
        f"Patch ({len(diff)} characters; excerpt truncated={len(diff) > 60000}; use ReadPatch):\n{diff[:60000]}"
    )
    data = analyze_commit(client, repo, parent, commit, prompt,
                          use_tools=len(diff) > 60000)
    return case_from_annotation(repo, source, identifier, commit, data,
                                parent=parent, diff=diff)


def case_from_annotation(repo: Path, source: str, identifier: str, commit: str,
                         data: dict, *, parent: str | None = None,
                         diff: str | None = None) -> Case | None:
    """Build a case from host or API annotations while Git supplies its immutable patch."""
    if parent is None:
        parents = git(repo, "rev-list", "--parents", "-n", "1", commit).split()
        if len(parents) != 2:
            return None
        parent = parents[1]
    if diff is None:
        diff = git(repo, "diff", "--no-ext-diff", "--find-renames", parent, commit, "--")
    if not diff.strip():
        return None
    if data.get("eligible") is False:
        return None
    if "eligible" in data and data["eligible"] is not True:
        raise ValueError(f"Model returned a non-boolean eligible for {commit}")
    for key in ("problem_statement", "hint", "test_command",
                "external_validation", "external_validation_reason"):
        if key not in data:
            raise ValueError(f"Model omitted {key} for {commit}")
    if not isinstance(data["external_validation"], bool):
        raise ValueError(f"Model returned a non-boolean external_validation for {commit}")
    if not isinstance(data["problem_statement"], str) or not data["problem_statement"].strip():
        raise ValueError(f"Model returned an empty problem statement for {commit}")
    return Case(
        case_id=f"{repo.name}-{identifier}-{commit[:12]}", repository=source,
        base_commit=parent, target_commit=commit,
        problem_statement=str(data["problem_statement"]), hint=str(data["hint"]),
        gold_diff=diff, test_command=str(data["test_command"]),
        external_validation=data["external_validation"],
        external_validation_reason=str(data["external_validation_reason"]),
    )


def validate_dataset(cases: list[Case]) -> list[str]:
    """Check that every reference patch and base commit match its repository."""
    errors: list[str] = []
    seen = set()
    repositories: dict[str, list[Case]] = {}
    for case in cases:
        if case.case_id in seen:
            errors.append(f"{case.case_id}: duplicate case ID")
        seen.add(case.case_id)
        if not case.problem_statement.strip():
            errors.append(f"{case.case_id}: empty problem statement")
        repositories.setdefault(case.repository, []).append(case)
    for spec, subset in repositories.items():
        try:
            with _repository(case_metadata(subset[0]).get("repository_alias", spec)) as repo:
                for case in subset:
                    try:
                        if case_metadata(case).get("kind") == "group":
                            from .grouped import validate_group
                            validate_group(case, repo)
                            continue
                        parents = git(repo, "rev-list", "--parents", "-n", "1",
                                      case.target_commit).split()
                        if len(parents) != 2 or parents[1] != case.base_commit:
                            errors.append(f"{case.case_id}: base is not target's sole parent")
                            continue
                        diff = git(repo, "diff", "--no-ext-diff", "--find-renames",
                                   case.base_commit, case.target_commit, "--")
                        if diff != case.gold_diff:
                            errors.append(f"{case.case_id}: gold diff does not match commits")
                    except (ValueError, subprocess.SubprocessError) as exc:
                        errors.append(f"{case.case_id}: Git error: {exc}")
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            errors.append(f"{spec}: repository unavailable: {exc}")
    return errors
