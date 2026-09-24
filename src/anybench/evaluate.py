from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache
import subprocess
import time

from .llm import ChatClient, parse_json_object
from .model import Case, RunRecord
from .sandbox import Sandbox
from .metadata import case_metadata, verification_identity
from .outcomes import TestOutcome, check


@lru_cache(maxsize=32)
def _image_id(image: str) -> str | None:
    try:
        result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def judge(record: RunRecord, case: Case, client: ChatClient) -> RunRecord:
    if record.status != "completed":
        return record
    if len(case.gold_diff) > 200_000 or len(record.diff) > 200_000:
        raise ValueError("Patch exceeds judge input limit; no partial-patch score was produced")
    prompt = (
        "Evaluate whether the candidate patch solves the task. The reference patch is one "
        "valid implementation, not the only one. Consider behavior, regressions, and missing work. "
        "Return JSON only: {\"score\": number between 0 and 1, \"reason\": string}.\n\n"
        f"Task:\n{case.problem_statement}\n\nReference patch:\n{case.gold_diff}"
        f"\n\nCandidate patch:\n{record.diff}"
    )
    reply = client.complete([{"role": "user", "content": prompt}])
    raw = reply.message.get("content") or ""
    result = parse_json_object(raw)
    score = float(result["score"])
    if not 0 <= score <= 1:
        raise ValueError("Judge score is outside [0, 1]")
    record.judge_score = score
    record.judge_reason = str(result["reason"])
    record.judge_prompt_tokens = reply.prompt_tokens
    record.judge_completion_tokens = reply.completion_tokens
    record.judge_seconds = reply.seconds
    return record


def evaluate(records: list[RunRecord], cases: list[Case], judge_client: ChatClient | None) -> list[RunRecord]:
    by_id = {case.case_id: case for case in cases}
    for record in records:
        case = by_id[record.case_id]
        if judge_client:
            before = (getattr(judge_client, "prompt_tokens", 0), getattr(judge_client, "completion_tokens", 0))
            started = time.monotonic()
            try:
                judge(record, case, judge_client)
            except Exception as exc:
                record.judge_reason = f"Judge error: {exc}"
                record.judge_prompt_tokens = getattr(judge_client, "prompt_tokens", 0) - before[0]
                record.judge_completion_tokens = getattr(judge_client, "completion_tokens", 0) - before[1]
                record.judge_seconds = time.monotonic() - started
    return records


def validate_test_commands(cases: list[Case], image: str = "anybench-sandbox:latest",
                           image_map: dict[str, str] | None = None,
                           workspace_size: str = "512m", memory: str = "1g") -> list[str]:
    """Require local checks to fail on the base and pass on the gold revision."""
    results = validation_results(cases, image, image_map, workspace_size, memory)
    return [f"{item['case_id']}: {item['reason']}" for item in results
            if item['status'] == 'invalid']


def evaluate_patch(case: Case, patch: str, image: str = "anybench-sandbox:latest",
                   workspace_size: str = "512m", memory: str = "1g",
                   command: str | None = None, reference: bool = False) -> TestOutcome:
    metadata = case_metadata(case)
    profile = metadata.get("environment", {})
    with Sandbox(case, image=image, workspace_size=workspace_size, memory=memory,
                 evaluation_patch=patch,
                 evaluation_files=metadata.get("evaluation_files", {}),
                 protected_paths=metadata.get("protected_paths", [])) as sandbox:
        if profile.get("setup_check"):
            result = sandbox.command(["sh", "-lc", profile["setup_check"]], timeout=120)
            if result.returncode:
                return TestOutcome("setup_error", result.returncode, None, 0,
                                   (result.stdout + result.stderr)[-12000:])
        return check(sandbox, command or metadata.get("evaluation_command") or
                     profile.get("test_command") or case.test_command)


def validation_results(cases: list[Case], image: str = "anybench-sandbox:latest",
                       image_map: dict[str, str] | None = None,
                       workspace_size: str = "512m", memory: str = "1g") -> list[dict]:
    """Give every case a visible verification or skip reason."""
    results = []
    for case in cases:
        if case.external_validation:
            results.append({"case_id": case.case_id, "status": "skipped",
                            "reason": case.external_validation_reason or "external validation"})
            continue
        if not case.test_command:
            results.append({"case_id": case.case_id, "status": "skipped",
                            "reason": "no local test command"})
            continue
        metadata = case_metadata(case)
        metadata["validation"] = {"status": "pending"}
        case._metadata = metadata
        selected_image = (image_map or {}).get(case.repository,
                           metadata.get("environment", {}).get("image", image))
        selected_image_id = _image_id(selected_image)
        repeats = 3 if metadata.get("kind") == "group" else 1
        trials = []
        try:
            for _ in range(repeats):
                base = evaluate_patch(case, "", selected_image, workspace_size, memory)
                gold = evaluate_patch(case, case.gold_diff, selected_image, workspace_size, memory,
                                      reference=True)
                trial = {"base": asdict(base), "reference": asdict(gold)}
                regression = metadata.get("regression_command")
                if regression:
                    before = evaluate_patch(case, "", selected_image, workspace_size, memory,
                                            command=regression)
                    after = evaluate_patch(case, case.gold_diff, selected_image,
                                           workspace_size, memory, command=regression)
                    trial.update(regression_base=asdict(before), regression_reference=asdict(after))
                trials.append(trial)
        except Exception as exc:
            metadata["validation"] = {"status": "invalid", "reason": str(exc)}
            results.append({"case_id": case.case_id, "status": "invalid",
                            "reason": f"test validation error: {exc}"})
            continue
        reasons = []
        if any(t["base"]["status"] == "passed" for t in trials):
            reasons.append("test also passes before the change")
        if any(t["base"]["status"] != "assertion_failure" for t in trials):
            reasons.append("base must execute a genuine failing regression test")
        if any(t["reference"]["status"] != "passed" for t in trials):
            reasons.append("test fails on gold commit or did not execute tests")
        if any(t.get(key, {}).get("status", "passed") != "passed" for t in trials
               for key in ("regression_base", "regression_reference")):
            reasons.append("unchanged regression checks must pass on base and reference")
        if len({(t["base"]["status"], t["reference"]["status"]) for t in trials}) > 1:
            reasons.append("flaky verification outcomes")
        metadata["validation"] = {"status": "invalid" if reasons else "verified",
                                  "trials": trials, "image": selected_image,
                                  "image_id": selected_image_id,
                                  "inputs_sha256": verification_identity(case)}
        case._metadata = metadata
        results.append({"case_id": case.case_id,
                        "status": "invalid" if reasons else "verified",
                        "reason": "; ".join(reasons), "base_passed": base.passed,
                        "gold_passed": gold.passed, "image": selected_image,
                        "image_id": selected_image_id, "trials": trials})
    return results
