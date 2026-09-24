"""One calculation layer for summaries, offline reports, and paired comparisons."""
from __future__ import annotations

from collections import defaultdict
import random
from statistics import mean

from .model import RunRecord
from .metadata import case_metadata


def active_seconds(records: list[RunRecord]) -> float:
    if not all(r.started_at and r.finished_at >= r.started_at for r in records):
        return sum(r.seconds for r in records)
    total, end = 0.0, 0.0
    for start, finish in sorted((r.started_at, r.finished_at) for r in records):
        total += max(0, finish - max(start, end))
        end = max(end, finish)
    return total


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    index = int(position)
    return ordered[index] + (ordered[min(index + 1, len(ordered) - 1)] - ordered[index]) * (position - index)


def solved(record: RunRecord) -> bool:
    return record.status == "completed" and record.test_passed is True


def confidence(values: list[float]) -> list[float] | None:
    if len(values) < 2:
        return None
    rng = random.Random(0)
    samples = [mean(rng.choices(values, k=len(values))) for _ in range(1000)]
    lower, upper = percentile(samples, .025), percentile(samples, .975)
    assert lower is not None and upper is not None
    return [lower, upper]


def summary(records: list[RunRecord], cases: list | None = None,
            manifest: dict | None = None, rates: dict | None = None) -> dict:
    if cases is not None:
        ids = {case.case_id for case in cases}
        if len(ids) != len(cases) or any(record.case_id not in ids for record in records):
            raise ValueError("Results do not match a unique dataset")
    inputs = manifest.get("inputs", {}) if manifest else {}
    if inputs.get("kind") == "run" and inputs.get("configs"):
        expected_keys = {(case["case_id"], config["name"], level, attempt)
                    for case in inputs["cases"] for config in inputs["configs"]
                    for level in inputs["levels"] for attempt in range(1, inputs["attempts"] + 1)}
        keys = [(r.case_id, r.model, r.concurrency, r.attempt) for r in records]
        if len(set(keys)) != len(keys) or not set(keys) <= expected_keys:
            raise ValueError("Results contain duplicate or unexpected manifest identities")
    groups = defaultdict(list)
    for record in records:
        groups[(record.model, record.harness, record.model_id, record.context_profile,
                record.context_window_tokens, record.harness_version, record.concurrency)].append(record)
    case_map = {case.case_id: case for case in cases or []}
    output = []
    for key, items in sorted(groups.items(), key=lambda item: str(item[0])):
        model, harness, model_id, profile, window, version, concurrency = key
        if len({(r.case_id, r.attempt) for r in items}) != len(items):
            raise ValueError("Duplicate attempt identities in a metric group")
        local = [r for r in items if (bool(case_map[r.case_id].test_command) and
                  not case_map[r.case_id].external_validation if r.case_id in case_map
                  else r.test_passed is not None)]
        passed = sum(solved(r) for r in local)
        verified = [r for r in local if r.case_id in case_map and
                    case_metadata(case_map[r.case_id]).get("validation", {}).get("status") == "verified"]
        scores = [r.judge_score for r in items if r.judge_score is not None]
        legacy = []
        for record in items:
            available = [value for value in (record.judge_score,
                         float(record.test_passed) if record.test_passed is not None else None)
                         if value is not None]
            if record.status != "completed":
                legacy.append(0.0)
            elif available:
                legacy.append(min(available))
        by_case = defaultdict(list)
        for record in local:
            by_case[record.case_id].append(record)
        case_rates = [mean(solved(record) for record in subset) for subset in by_case.values()]
        solve_k = {}
        for k in (1, 2, 3, 5, 10):
            eligible = [[r for r in subset if r.attempt <= k] for subset in by_case.values()
                        if set(range(1, k + 1)).issubset({r.attempt for r in subset})]
            if eligible:
                solve_k[str(k)] = {"rate": mean(any(solved(r) for r in subset) for subset in eligible),
                                   "cases": len(eligible)}
        wall = active_seconds(items)
        completed = sum(r.status == "completed" for r in items)
        calls_missing = sum(r.model_calls_by_purpose is None for r in items)
        expected = None
        if manifest and manifest.get("inputs", {}).get("kind") == "run":
            inputs = manifest["inputs"]
            expected = len(inputs.get("cases", [])) * inputs.get("attempts", 1)
        cost = None
        rate = (rates or {}).get(model_id or model)
        if rate and all(r.usage_available and r.cached_prompt_tokens is not None for r in items):
            cost = sum((max(0, r.prompt_tokens - (r.cached_prompt_tokens or 0) -
                              (r.cache_creation_tokens or 0)) * rate["input"] +
                        r.completion_tokens * rate["output"] +
                        (r.cached_prompt_tokens or 0) * rate.get("cache_read", rate["input"]) +
                        (r.cache_creation_tokens or 0) * rate.get("cache_write", rate["input"])) / 1_000_000
                       for r in items)
        errors: dict[str, int] = defaultdict(int)
        for record in items:
            if record.status != "completed":
                errors[record.stop_reason or record.status] += 1
            for event in record.trace:
                if "evaluation" in event and event["evaluation"]["status"] != "passed":
                    errors[event["evaluation"]["status"]] += 1
        group = {"model": model, "harness": harness, "model_id": model_id, "profile": profile,
                 "context_window_tokens": window, "harness_version": version,
                 "concurrency": concurrency, "attempts": len(items), "expected_attempts": expected,
                 "complete": len(items) == expected if expected is not None else None,
                 "completed": completed, "execution_success": completed / len(items),
                 "errors": sum(r.status == "error" for r in items),
                 "exhausted": sum(r.status == "exhausted" for r in items),
                 "test_passed": passed, "test_failed": len(local) - passed,
                 "test_accuracy": passed / len(local) if local else None,
                 "verified_test_accuracy": mean(solved(r) for r in verified) if verified else None,
                 "verified_test_coverage": len(verified) / len(items),
                 "test_coverage": len(local) / len(items),
                 "unscored": sum(r.test_passed is None and r.judge_score is None for r in items),
                 "judge_mean": mean(scores) if scores else None,
                 "judge_coverage": len(scores) / len(items),
                 "judge_errors": sum(r.judge_reason.startswith("Judge error:") for r in items),
                 "legacy_accuracy": mean(legacy) if legacy else None,
                 "coverage": len(legacy) / len(items), "active_seconds": wall,
                 "throughput": completed / wall * 3600 if wall else 0,
                 "solved_per_hour": passed / wall * 3600 if wall else 0,
                 "seconds": sum(r.seconds + r.judge_seconds for r in items),
                 "setup_seconds": sum(r.setup_seconds for r in items),
                 "model_seconds": sum(r.model_seconds for r in items),
                 "test_seconds": sum(r.test_seconds for r in items),
                 "latency_p50": percentile([r.seconds for r in items], .5),
                 "latency_p95": percentile([r.seconds for r in items], .95),
                 "prompt_tokens": sum(r.prompt_tokens + r.judge_prompt_tokens for r in items),
                 "completion_tokens": sum(r.completion_tokens + r.judge_completion_tokens for r in items),
                 "cached_prompt_tokens": sum(r.cached_prompt_tokens or 0 for r in items),
                 "cache_missing_attempts": sum(r.cached_prompt_tokens is None for r in items),
                 "usage_missing_attempts": sum(not r.usage_available for r in items),
                 "tool_calls": sum(r.tool_calls or 0 for r in items),
                 "model_calls": None if calls_missing else sum(sum((r.model_calls_by_purpose or {}).values()) for r in items),
                 "model_calls_missing_attempts": calls_missing,
                 "retry_count": sum("retry" in event for r in items for event in r.trace),
                 "test_confidence_95": confidence(case_rates), "case_count": len(by_case),
                 "solve_within_k": solve_k,
                 "case_consistency": {name: mean(solved(r) for r in subset) for name, subset in by_case.items()},
                 "failure_categories": dict(errors), "estimated_cost": cost}
        output.append(group)
    return {"schema": 1, "cases": len(cases) if cases is not None else None, "groups": output}


def compare(baseline: list[RunRecord], candidate: list[RunRecord]) -> dict:
    def indexed(records):
        keys = {(r.model, r.harness, r.concurrency) for r in records}
        if len(keys) != 1:
            raise ValueError("Comparison requires one candidate and worker count per input")
        mapping = {(r.case_id, r.attempt): r for r in records}
        if len(mapping) != len(records):
            raise ValueError("Duplicate comparison attempts")
        return mapping
    left, right = indexed(baseline), indexed(candidate)
    if left.keys() != right.keys():
        raise ValueError("Comparison inputs must have matching cases and attempt numbers")
    if any(r.test_passed is None for r in [*left.values(), *right.values()]):
        raise ValueError("Comparison requires local test outcomes for every attempt")
    changes = [{"case_id": key[0], "attempt": key[1], "baseline": solved(left[key]),
                "candidate": solved(right[key])} for key in sorted(left)]
    wins = [item for item in changes if item["candidate"] and not item["baseline"]]
    losses = [item for item in changes if item["baseline"] and not item["candidate"]]
    by_case = defaultdict(list)
    for item in changes:
        by_case[item["case_id"]].append(int(item["candidate"]) - int(item["baseline"]))
    delta = mean(int(item["candidate"]) - int(item["baseline"]) for item in changes)
    return {"paired_attempts": len(changes), "wins": wins, "losses": losses,
            "ties": len(changes) - len(wins) - len(losses), "accuracy_delta": delta,
            "delta_confidence_95": confidence([mean(values) for values in by_case.values()]),
            "newly_failing_cases": sorted({item["case_id"] for item in losses})}
