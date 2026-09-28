"""Experimental Studio's durable controls and compatibility boundaries."""
from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from anybench.live import RunObserver, activate, emit, snapshot, stage_scope, submit_control
from anybench.model import Case, ModelConfig, RunRecord, write_cases, write_jsonl
from anybench.providers import ProtocolAdapter
from anybench.report import report
from anybench.runner import run_cases


class LiveStoreTests(unittest.TestCase):
    def test_provider_reasoning_usage_is_separate_from_output(self):
        chat = ProtocolAdapter(ModelConfig("chat", model="example", base_url="http://localhost:1"))
        reply = chat.reply({"choices": [{"message": {"role": "assistant", "content": "Done"}}],
                            "usage": {"prompt_tokens": 80, "completion_tokens": 30,
                                      "completion_tokens_details": {"reasoning_tokens": 18}}}, .1)
        self.assertEqual((reply.prompt_tokens, reply.completion_tokens, reply.reasoning_tokens),
                         (80, 30, 18))
        responses = ProtocolAdapter(ModelConfig("responses", model="example", api="responses",
                                                base_url="http://localhost:1"))
        reply = responses.reply({"output": [], "usage": {"input_tokens": 20,
                                "output_tokens": 12,
                                "output_tokens_details": {"reasoning_tokens": 7}}}, .1)
        self.assertEqual(reply.reasoning_tokens, 7)

    def test_event_order_and_idempotent_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            run = RunObserver(root=Path(directory))
            run.emit("first", "builder", {"value": 1})
            run.emit("second", "candidate", {"value": 2})
            initial = snapshot(run.path, limit=1)
            self.assertEqual((initial["cursor"], initial["latest"], initial["has_more"]),
                             (1, 2, True))
            self.assertEqual(snapshot(run.path, after=initial["cursor"])["events"][0]["kind"],
                             "second")
            first = submit_control(run.path, "concurrency", 1, "once")
            same = submit_control(run.path, "concurrency", 1, "once")
            self.assertEqual(first["id"], same["id"])
            self.assertEqual(first["status"], "pending")
            run.controller._poll()
            self.assertEqual(snapshot(run.path)["controls"][0]["status"], "applied")
            self.assertEqual(run.controller.target, 1)

    def test_nested_spans_have_stable_parent_and_measured_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            run = RunObserver(root=Path(directory))
            with activate(run), stage_scope("builder"):
                with stage_scope("tool"):
                    emit("tool.completed", "tool", {"name": "ReadPatch"})
            events = snapshot(run.path)["events"]
            parent = next(item["data"]["span_id"] for item in events
                          if item["kind"] == "span.started" and item["stage"] == "builder")
            child = next(item for item in events if item["kind"] == "tool.completed")
            self.assertEqual(child["data"]["parent_span_id"], parent)
            self.assertTrue(any(item["kind"] == "span.finished" and
                                item["data"]["payload"]["seconds"] >= 0 for item in events))

    def test_stream_redacts_secret_across_chunks_and_artifacts_are_private(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"STUDIO_KEY": "very-secret"}):
            run = RunObserver(root=Path(directory))
            run.set_configs([ModelConfig("model", model="example", base_url="http://localhost:1",
                                         api_key_env="STUDIO_KEY")])
            output = run.output_stream("subprocess")
            output.write("before very-")
            output.write("secret after")
            output.close()
            combined = "".join(item["data"]["payload"]["text"] for item in snapshot(run.path)["events"])
            self.assertNotIn("very-secret", combined)
            self.assertIn("[REDACTED]", combined)
            artifact = run.artifact({"key": "very-secret"})
            path = run.path.parent / "artifacts" / (artifact["id"] + ".txt")
            self.assertNotIn("very-secret", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            run.set_meta("diagnostic", "very-secret")
            self.assertNotIn("very-secret", str(snapshot(run.path)["meta"]))

    def test_dynamic_scheduler_drains_before_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            run = RunObserver(root=Path(directory))
            config = ModelConfig("candidate", model="example", base_url="http://localhost:1")
            cases = [Case(str(number), "/tmp/repo", "a", "b", "Fix", "", "")
                     for number in range(4)]
            started: list[str] = []
            gate = threading.Event()
            lock = threading.Lock()
            result: list[RunRecord] = []

            def fake_run(case, config, attempt, *args):
                with lock:
                    started.append(case.case_id)
                gate.wait(3)
                return RunRecord(case.case_id, config.name, attempt, "completed", .1)

            def worker():
                with activate(run):
                    result.extend(run_cases(cases, [config], concurrency=2))

            with patch("anybench.runner.run_one", side_effect=fake_run):
                thread = threading.Thread(target=worker)
                thread.start()
                deadline = time.time() + 3
                while len(started) < 2 and time.time() < deadline:
                    time.sleep(.01)
                self.assertEqual(len(started), 2)
                submit_control(run.path, "concurrency", 1, "lower")
                run.controller._poll()
                self.assertEqual(run.controller.target, 1)
                gate.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(result), 4)
                self.assertEqual(len(started), 4)
                self.assertEqual(snapshot(run.path)["meta"]["variable_groups"], [["candidate", 2]])
                run.controller.set_level(4, provider_cap=2, model="candidate")
                self.assertEqual(run.controller.target, 4)
                self.assertEqual(snapshot(run.path)["meta"]["concurrency"]["effective"], 2)

    def test_request_gate_waits_for_lowered_target_and_respects_provider_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            run = RunObserver(root=Path(directory))
            run.controller.set_level(3, provider_cap=2, model="candidate")
            entered = []
            release = threading.Event()
            lock = threading.Lock()
            def call(number):
                with run.controller.request("http://localhost/model"):
                    with lock:
                        entered.append(number)
                    release.wait(3)
            first = threading.Thread(target=call, args=(1,))
            second = threading.Thread(target=call, args=(2,))
            first.start(); second.start()
            deadline = time.time() + 3
            while len(entered) < 2 and time.time() < deadline:
                time.sleep(.01)
            self.assertEqual(len(entered), 2)
            submit_control(run.path, "concurrency", 1, "lower")
            run.controller._poll()
            third = threading.Thread(target=call, args=(3,))
            third.start()
            time.sleep(.2)
            self.assertEqual(len(entered), 2)
            release.set()
            for thread in (first, second, third):
                thread.join(4)
                self.assertFalse(thread.is_alive())
            self.assertEqual(len(entered), 3)
            self.assertTrue(any(event["kind"] == "request.waiting" for event in snapshot(run.path)["events"]))

    def test_pause_resume_and_stop_at_serial_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            run = RunObserver(root=Path(directory))
            submit_control(run.path, "pause", None, "pause")
            run.controller._poll()
            outcome = []
            thread = threading.Thread(target=lambda: outcome.append(run.controller.checkpoint()))
            thread.start()
            time.sleep(.2)
            self.assertEqual(outcome, [])
            submit_control(run.path, "resume", None, "resume")
            thread.join(3)
            self.assertEqual(outcome, [True])
            submit_control(run.path, "pause", None, "pause-again")
            run.controller._poll()
            submit_control(run.path, "stop", None, "stop")
            self.assertFalse(run.controller.checkpoint())
            self.assertEqual(snapshot(run.path)["controls"][0]["status"], "applied")

    def test_artifact_budget_and_output_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            run = RunObserver(root=Path(directory))
            first = run.artifact("abcd", limit=2)
            self.assertTrue(first["truncated"])
            run._artifact_bytes = 128_000_000
            self.assertEqual(run.artifact("more")["unavailable"], "artifact budget reached")
            output = run.output_stream("candidate")
            output.emitted = 8_000_000
            output.write("extra")
            output.close()
            self.assertEqual(sum(event["kind"] == "process.output.truncated" for event in snapshot(run.path)["events"]), 1)

    def test_variable_concurrency_report_omits_speedup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            records = [RunRecord("case", "candidate", 1, "completed", 1,
                                 started_at=1, finished_at=2, concurrency=2, test_passed=True)]
            metrics = report(records, path, variable_groups={("candidate", 2)})
            self.assertTrue(metrics["groups"][0]["variable_concurrency"])
            self.assertIn("2 (variable)", path.read_text())


class StudioApiTests(unittest.TestCase):
    def test_imported_dataset_is_validated_for_guided_launch(self):
        try:
            from fastapi.testclient import TestClient
        except (ImportError, RuntimeError):
            self.skipTest("Studio API test client unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_cwd = Path.cwd()
            os.chdir(root)
            try:
                config = {"repositories": [], "models": [
                    {"name": "builder", "role": "builder", "model": "local",
                     "base_url": "http://localhost:1", "api_key_env": "BUILDER_KEY"},
                    {"name": "candidate", "role": "candidate", "model": "local",
                     "base_url": "http://localhost:1", "api_key_env": "CANDIDATE_KEY"}]}
                Path(".anybench").mkdir()
                Path(".anybench/config.json").write_text(json.dumps(config))
                csv_path = Path("source.csv")
                write_cases(csv_path, [Case("own-1", "my-repo", "a" * 40, "b" * 40,
                                            "Fix", "", "diff", "true")])
                with patch("anybench.live.ROOT", root / "live"), \
                     patch("anybench.studio_server.ROOT", root / "live"):
                    from anybench.studio_server import create_app, token
                    with TestClient(create_app(), base_url="http://localhost:8765") as client:
                        client.get("/auth", params={"credential": token()})
                        imported = client.post("/api/experimental/v1/datasets/import",
                                               json={"text": csv_path.read_text()})
                        self.assertEqual(imported.status_code, 200)
                        body = {"mode": "guided", "repositories": [],
                                "dataset_path": imported.json()["path"]}
                        plan = client.post("/api/experimental/v1/runs/validate", json=body)
                        self.assertEqual(plan.status_code, 200)
                        self.assertEqual(plan.json()["workload"]["custom_dataset_cases"], 1)
                        self.assertEqual(plan.json()["workload"]["maximum_commits"], 0)
                        self.assertIn("--dataset", plan.json()["command"])
                        self.assertEqual(client.post("/api/experimental/v1/datasets/import",
                                                     json={"text": "invalid"}).status_code, 400)
            finally:
                os.chdir(old_cwd)
    def test_browser_launch_does_not_persist_raw_worker_output(self):
        try:
            from fastapi.testclient import TestClient
        except (ImportError, RuntimeError):
            self.skipTest("Studio API test client unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("anybench.live.ROOT", root), patch("anybench.studio_server.ROOT", root), \
                    patch("anybench.studio_server.subprocess.Popen",
                          return_value=SimpleNamespace(pid=123)) as spawn:
                from anybench.studio_server import create_app, token
                with TestClient(create_app(), base_url="http://localhost:8765") as client:
                    client.get("/auth", params={"credential": token()})
                    result = client.post("/api/experimental/v1/runs", json={"mode": "advanced",
                        "argv": ["validate", "cases.csv"], "confirmed": True})
                    self.assertEqual(result.status_code, 200)
                    self.assertFalse((root / result.json()["id"] / "worker.log").exists())
                    self.assertEqual(spawn.call_args.kwargs["stdout"], subprocess.DEVNULL)
                    self.assertEqual(spawn.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_problem_filters_and_case_record_lookup(self):
        try:
            from fastapi.testclient import TestClient
        except (ImportError, RuntimeError):
            self.skipTest("Studio API test client unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session"
            session.mkdir()
            write_cases(session / "cases.csv", [
                Case("a", "repo-one", "base", "target", "Fix one", "", ""),
                Case("b", "repo-two", "base", "target", "Fix two", "", "")])
            write_jsonl(session / "attempts.jsonl", [
                RunRecord("a", "alpha", 1, "completed", 1, harness="anybench",
                          test_passed=True, judge_score=.9),
                RunRecord("b", "beta", 1, "completed", 1, harness="custom",
                          test_passed=False, judge_score=.2)])
            with patch("anybench.live.ROOT", root), patch("anybench.studio_server.ROOT", root):
                from anybench.studio_server import create_app, token
                run = RunObserver(root=root)
                run.set_meta("session_path", str(session))
                run.set_meta("state", "finished")
                with TestClient(create_app(), base_url="http://localhost:8765") as client:
                    client.get("/auth", params={"credential": token()})
                    url = f"/api/experimental/v1/runs/{run.run_id}"
                    page = client.get(url + "/problems?model=alpha&harness=anybench&test_result=passed&judge_min=0.8").json()
                    self.assertEqual(page["total"], 1)
                    self.assertEqual(page["problems"][0]["attempts"][0]["model"], "alpha")
                    self.assertEqual(client.get(url + "/problems?model=beta&test_result=passed").json()["total"], 0)
                    self.assertEqual(client.get(url + "/records?case_id=b").json()["total"], 1)

    def test_judging_keeps_unscored_candidate_attempts_visible(self):
        try:
            from fastapi.testclient import TestClient
        except (ImportError, RuntimeError):
            self.skipTest("Studio API test client unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session"
            session.mkdir()
            write_cases(session / "cases.csv", [
                Case("a", "repo", "base", "target", "Fix one", "", ""),
                Case("b", "repo", "base", "target", "Fix two", "", "")])
            write_jsonl(session / "attempts.jsonl", [
                RunRecord("a", "alpha", 1, "completed", 1, test_passed=True),
                RunRecord("b", "alpha", 1, "completed", 1, test_passed=False)])
            write_jsonl(session / "scored.jsonl", [
                RunRecord("a", "alpha", 1, "completed", 1, test_passed=True, judge_score=.8)])
            with patch("anybench.live.ROOT", root), patch("anybench.studio_server.ROOT", root):
                from anybench.studio_server import create_app, token
                run = RunObserver(root=root)
                run.set_meta("session_path", str(session))
                run.set_meta("state", "finished")
                with TestClient(create_app(), base_url="http://localhost:8765") as client:
                    client.get("/auth", params={"credential": token()})
                    url = f"/api/experimental/v1/runs/{run.run_id}"
                    records = client.get(url + "/records").json()["records"]
                    self.assertEqual(len(records), 2)
                    self.assertEqual({item["case_id"]: item["judge_score"] for item in records},
                                     {"a": .8, "b": None})
                    self.assertEqual(client.get(url + "/problems").json()["total"], 2)
                    self.assertEqual(client.get(url + "/summary").json()["groups"][0]["attempts"], 2)

    def test_auth_snapshot_controls_and_artifact_boundaries(self):
        try:
            from fastapi.testclient import TestClient
        except (ImportError, RuntimeError):
            self.skipTest("Studio API test client unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("anybench.live.ROOT", root), patch("anybench.studio_server.ROOT", root):
                from anybench.studio_server import create_app, token
                run = RunObserver(root=root)
                run.set_meta("state", "running")
                run.set_meta("worker_pid", os.getpid())
                run.emit("commit.started", "builder", {"repository": "repo"})
                run.emit("process.output", "candidate", {"text": "first"}, case_id="case-1")
                run.emit("process.output", "candidate", {"text": "second"}, case_id="case-2")
                with TestClient(create_app(), base_url="http://localhost:8765") as client:
                    endpoint = f"/api/experimental/v1/runs/{run.run_id}"
                    self.assertEqual(client.get(endpoint + "/snapshot").status_code, 401)
                    self.assertEqual(client.get("/auth", params={"credential": token()}).status_code, 200)
                    state = client.get(endpoint + "/snapshot").json()
                    self.assertEqual(state["events"][0]["kind"], "commit.started")
                    self.assertEqual(client.get(endpoint + "/snapshot?after=3").json()["events"], [])
                    logs = client.get(endpoint + "/logs?case_id=case-1&limit=1").json()
                    self.assertEqual([item["data"]["payload"]["text"] for item in logs["logs"]], ["first"])
                    good = client.post(endpoint + "/controls", json={"action": "pause", "request_id": "id-1"})
                    self.assertEqual(good.status_code, 200)
                    duplicate = client.post(endpoint + "/controls", json={"action": "pause", "request_id": "id-1"})
                    self.assertEqual(duplicate.json()["id"], good.json()["id"])
                    self.assertEqual(client.post(endpoint + "/controls", json={"action": "stop",
                        "request_id": "id-1"}).status_code, 409)
                    self.assertEqual(client.post(endpoint + "/controls", json={"action": "stop"},
                        headers={"Origin": "https://evil.example"}).status_code, 403)
                    self.assertEqual(client.get(endpoint + "/artifact/" + "a" * 32).status_code, 404)
                    registered = run.artifact("visible artifact")
                    self.assertEqual(client.get(endpoint + "/artifact/" + registered["id"]).status_code, 200)
                    forged = "b" * 32
                    (run.path.parent / "artifacts" / (forged + ".txt")).write_text("forged")
                    self.assertEqual(client.get(endpoint + "/artifact/" + forged).status_code, 404)
                    target = run.path.parent / "artifacts" / (registered["id"] + ".txt")
                    target.unlink()
                    target.symlink_to(run.path)
                    self.assertEqual(client.get(endpoint + "/artifact/" + registered["id"]).status_code, 404)
                    preview = client.post("/api/experimental/v1/runs/validate", json={"mode": "advanced",
                        "argv": ["validate", "cases.csv"]})
                    self.assertEqual(preview.status_code, 200)
                    self.assertEqual(preview.json()["workload"]["command"], "validate")
                    self.assertEqual(client.post("/api/experimental/v1/runs", json={"mode": "advanced",
                        "argv": ["validate", "cases.csv"]}).status_code, 400)
                    imported = client.post("/api/experimental/v1/config/import", json={"format": "json",
                        "text": '[{"name":"candidate","model":"local","base_url":"http://localhost:1"}]'}).json()
                    self.assertTrue(Path(imported["model_path"]).is_file())
                    run.set_meta("worker_pid", 99999999)
                    self.assertEqual(client.get(endpoint + "/snapshot").json()["meta"]["state"], "interrupted")
                    self.assertEqual(client.post(endpoint + "/controls", json={"action": "pause"}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
