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
run.set_meta("budget_usd", 1.0)
run.set_meta("pricing", {"local-candidate": {"input": 1.0, "output": 2.0}})
for stage, kind, payload in (
    ("builder", "commit.completed", {"accepted": True}),
    ("validation", "validation.finished", {"status": "verified"}),
    ("candidate", "attempt.started", {"problem": "Handle trailing separators"}),
    ("candidate", "attempt.finished", {"status": "completed", "test_passed": True}),
    ("evaluation", "test.finished", {"status": "passed", "tests": 1}),
    ("judge", "judge.finished", {"score": .9}),
):
    run.emit(kind, stage, payload, case_id="browser-001")
request = run.artifact('{"messages":[{"role":"system","content":"Fix the parser"}]}')
response = run.artifact('{"role":"assistant","content":"Done"}')
run.emit("model.request", "candidate", {"model": "local/model", "api": "chat_completions",
         "prompt": request}, case_id="browser-001", model="local-candidate",
         operation_id="fixture-call")
run.emit("model.response", "candidate", {"prompt_tokens": 80, "completion_tokens": 30,
         "reasoning_tokens": 18, "content_tokens": 12, "seconds": .5,
         "usage_available": True, "response": response}, case_id="browser-001",
         model="local-candidate", operation_id="fixture-call")
run.emit("tool.completed", "candidate", {"tool": "Run", "status": "ok"},
         case_id="browser-001", model="local-candidate")
print(run.run_id)
