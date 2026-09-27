"""Create an offline run for the browser smoke test; no model or Docker calls."""
from pathlib import Path
import time

from anybench.live import RunObserver
from anybench.model import Case, RunRecord, write_cases, write_jsonl

session = Path(".anybench/browser-fixture")
session.mkdir(parents=True, exist_ok=True)
write_cases(session / "verified.csv", [Case("browser-001", "fixture/repo", "a" * 40,
    "b" * 40, "Handle trailing separators in the parser.", "", "",
    "python -m unittest test_parser")])
now = time.time()
write_jsonl(session / "attempts.jsonl", [RunRecord("browser-001", "local-candidate", 1,
    "completed", 3, started_at=now - 5, finished_at=now - 2, test_passed=True,
    judge_score=.9, harness="anybench", model_id="local/model")])
run = RunObserver()
run.set_meta("command", "start")
run.set_meta("session_path", str(session.resolve()))
run.set_meta("started_at", now - 10)
run.set_meta("state", "finished")
for stage, kind, payload in (
    ("builder", "commit.completed", {"accepted": True}),
    ("validation", "validation.finished", {"status": "verified"}),
    ("candidate", "attempt.started", {"problem": "Handle trailing separators"}),
    ("candidate", "attempt.finished", {"status": "completed", "test_passed": True}),
    ("evaluation", "test.finished", {"status": "passed", "tests": 1}),
    ("judge", "judge.finished", {"score": .9}),
):
    run.emit(kind, stage, payload, case_id="browser-001")
print(run.run_id)
