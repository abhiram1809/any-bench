from __future__ import annotations

from dataclasses import replace

from .llm import ChatClient, parse_json_object
from .model import Case, RunRecord
from .sandbox import Sandbox


def judge(record: RunRecord, case: Case, client: ChatClient) -> RunRecord:
    if record.status != "completed":
        return record
    prompt = (
        "Evaluate whether the candidate patch solves the task. The reference patch is one "
        "valid implementation, not the only one. Consider behavior, regressions, and missing work. "
        "Return JSON only: {\"score\": number between 0 and 1, \"reason\": string}.\n\n"
        f"Task:\n{case.problem_statement}\n\nReference patch:\n{case.gold_diff[:60000]}"
        f"\n\nCandidate patch:\n{record.diff[:60000]}"
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
            try:
                judge(record, case, judge_client)
            except Exception as exc:
                record.judge_reason = f"Judge error: {exc}"
    return records


def validate_test_commands(cases: list[Case], image: str = "anybench-sandbox:latest",
                           image_map: dict[str, str] | None = None) -> list[str]:
    """Require local checks to fail on the base and pass on the gold revision."""
    errors = []
    for case in cases:
        if not case.test_command or case.external_validation:
            continue
        selected_image = (image_map or {}).get(case.repository, image)
        try:
            with Sandbox(case, image=selected_image) as base:
                base_passed, _ = base.test(case.test_command)
            gold_case = replace(case, base_commit=case.target_commit)
            with Sandbox(gold_case, image=selected_image) as gold:
                gold_passed, gold_output = gold.test(case.test_command)
        except Exception as exc:
            errors.append(f"{case.case_id}: test validation error: {exc}")
            continue
        if base_passed:
            errors.append(f"{case.case_id}: test also passes before the change")
        if not gold_passed:
            errors.append(f"{case.case_id}: test fails on gold commit: {gold_output[-500:]}")
    return errors
