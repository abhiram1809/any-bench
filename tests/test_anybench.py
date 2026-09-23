import json
import copy
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from anybench.dataset import analyze_commit, build_dataset, validate_dataset
from anybench.cli import main
from anybench.evaluate import evaluate, judge, validate_test_commands
from anybench.llm import ChatClient, Reply, parse_json_object
from anybench.model import (Case, ModelConfig, RunRecord, append_jsonl,
                            append_case, read_cases, read_jsonl, write_cases, write_jsonl)
from anybench.report import report
from anybench.runner import agent_loop, preflight_run, run_cases, run_one, run_sweep
from anybench.sandbox import Sandbox, ToolError, _limited_run, prepare_snapshot


class StubClient:
    def __init__(self, *messages):
        self.messages = list(messages)
        self.calls = []

    def complete(self, messages, tools=None):
        self.calls.append((copy.deepcopy(messages), tools))
        return Reply(self.messages.pop(0))


def commit(repo, message):
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.com", "commit", "-qm", message], check=True)


class DatasetTests(unittest.TestCase):
    def test_csv_accepts_large_gold_patch(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cases.csv"
            case = Case("large", "/tmp/repo", "a", "b", "Fix", "", "+" + "x" * 200_000)
            write_cases(path, [case])
            self.assertEqual(read_cases(path)[0].gold_diff, case.gold_diff)

    def test_builder_skips_unsuitable_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "README.md").write_text("before\n")
            commit(repo, "Initial")
            (repo / "README.md").write_text("after\n")
            commit(repo, "Fix typo")
            client = StubClient({"content": '{"eligible":false}'})
            self.assertEqual(build_dataset([repo], client, commits=2), [])

    def test_builder_agent_can_inspect_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "app.py").write_text("value = 1\n")
            commit(repo, "Initial")
            base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                           text=True).strip()
            (repo / "app.py").write_text("value = 2\n")
            commit(repo, "Update")
            target = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                             text=True).strip()
            client = StubClient(
                {"tool_calls": [{"id": "1", "function": {"name": "ReadRevision",
                 "arguments": '{"revision":"parent","file_path":"app.py"}'}}]},
                {"content": '{"problem_statement":"Fix","hint":"","test_command":"",'
                            '"external_validation":false,"external_validation_reason":""}'})
            result = analyze_commit(client, repo, base, target, "Analyze")
            self.assertEqual(result["problem_statement"], "Fix")
            self.assertEqual(client.calls[1][0][-1]["content"], "value = 1\n")

    def test_build_and_csv_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "app.py").write_text("value = 1\n")
            commit(repo, "Initial")
            (repo / "app.py").write_text("value = 2\n")
            commit(repo, "Correct value")
            client = StubClient({"content": json.dumps({"problem_statement": "Correct the value",
                "hint": "See app.py", "test_command": "python -c 'import app; assert app.value == 2'",
                "external_validation": False, "external_validation_reason": ""})})
            cases = build_dataset([repo], client, commits=2)
            self.assertEqual(len(cases), 1)
            self.assertIn("+value = 2", cases[0].gold_diff)
            self.assertNotEqual(cases[0].base_commit, cases[0].target_commit)
            path = Path(temp) / "cases.csv"
            write_cases(path, cases)
            self.assertEqual(read_cases(path), cases)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(validate_dataset(cases), [])
            cases[0].gold_diff += "bad"
            self.assertIn("gold diff does not match", validate_dataset(cases)[0])

    def test_builder_streams_and_resumes_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "app.py").write_text("value = 1\n")
            commit(repo, "Initial")
            (repo / "app.py").write_text("value = 2\n")
            commit(repo, "Change value")
            path = Path(temp) / "cases.csv"
            write_cases(path, [])
            client = StubClient({"content": json.dumps({
                "problem_statement": "Change value", "hint": "", "test_command": "",
                "external_validation": False, "external_validation_reason": ""})})
            cases = build_dataset([repo], client, commits=2,
                                  on_case=lambda case: append_case(path, case))
            self.assertEqual(read_cases(path), cases)
            resume_client = StubClient()
            resumed = build_dataset([repo], resume_client, commits=2, existing_cases=cases,
                                    on_case=lambda case: append_case(path, case))
            self.assertEqual(resumed, cases)
            self.assertEqual(resume_client.calls, [])

    def test_case_cap_spreads_across_repositories(self):
        with tempfile.TemporaryDirectory() as temp:
            repos = []
            for name in ("one", "two"):
                repo = Path(temp) / name
                repo.mkdir()
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
                (repo / "app.py").write_text("value = 1\n")
                commit(repo, "Initial")
                (repo / "app.py").write_text("value = 2\n")
                commit(repo, "Change value")
                repos.append(repo)
            response = {"content": json.dumps({
                "problem_statement": "Change value", "hint": "", "test_command": "",
                "external_validation": False, "external_validation_reason": ""})}
            cases = build_dataset(repos, StubClient(response, response), commits=2, max_cases=2)
            self.assertEqual({case.repository for case in cases}, {str(repo) for repo in repos})

    def test_sandbox_snapshot_excludes_target_history(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "source-repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "file.txt").write_text("before\n")
            commit(repo, "Before")
            base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                           text=True).strip()
            (repo / "file.txt").write_text("after\n")
            commit(repo, "After")
            target = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                             text=True).strip()
            root = Path(temp) / "sandbox" / "repo"
            root.parent.mkdir()
            prepare_snapshot(str(repo), base, root)
            self.assertEqual((root / "file.txt").read_text(), "before\n")
            self.assertNotEqual(0, subprocess.run(["git", "-C", str(root), "cat-file", "-e", target],
                                                capture_output=True).returncode)

    def test_snapshot_preserves_tracked_ignored_files_and_diff(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "source-repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "generated.py").write_text("value = 1\n")
            commit(repo, "Track generated file")
            (repo / ".gitignore").write_text("generated.py\n*.log\n")
            commit(repo, "Ignore generated files")
            base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                           text=True).strip()
            root = Path(temp) / "sandbox" / "repo"
            root.parent.mkdir()
            prepare_snapshot(str(repo), base, root)
            tracked = subprocess.run(["git", "-C", str(root), "ls-files", "--error-unmatch",
                                      "generated.py"], capture_output=True)
            self.assertEqual(tracked.returncode, 0)
            sandbox = Sandbox(Case("c", str(repo), base, base, "Fix", "", ""))
            sandbox.root = root
            sandbox.write("generated.py", "value = 2\n")
            sandbox.write("new.log", "a new file\n")
            diff = sandbox.diff()
            self.assertIn("+value = 2", diff)
            self.assertIn("new.log", diff)


class ClientTests(unittest.TestCase):
    def test_parses_fenced_json_with_trailing_text(self):
        parsed = parse_json_object('Result:\n```json\n{"score": 1}\n```\nNote {other}')
        self.assertEqual(parsed, {"score": 1})

    def test_configured_endpoint_and_key_env(self):
        config = ModelConfig("provider", "https://example.test/v1", "model-x", "TEST_API_KEY")
        seen = {}
        def fake_open(request, timeout):
            seen["url"] = request.full_url
            seen["authorization"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data)
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": "done"}}],
                                          "usage": {"prompt_tokens": 3,
                                                    "completion_tokens": 2}}).encode())
        with patch.dict(os.environ, {"TEST_API_KEY": "test-secret"}), \
             patch("anybench.llm.urllib.request.urlopen", side_effect=fake_open):
            reply = ChatClient(config).complete([{"role": "user", "content": "hello"}])
        self.assertEqual(seen["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(seen["authorization"], "Bearer test-secret")
        self.assertEqual(seen["body"]["model"], "model-x")
        self.assertEqual((reply.prompt_tokens, reply.completion_tokens), (3, 2))

    def test_retries_throttled_request(self):
        config = ModelConfig("provider", "https://example.test/v1", "model-x",
                             "TEST_API_KEY", max_retries=1)
        response = io.BytesIO(b'{"choices":[{"message":{"content":"done"}}]}')
        throttle = urllib.error.HTTPError("https://example.test", 429, "throttled",
                                          {"Retry-After": "0"}, io.BytesIO(b"busy"))
        with patch.dict(os.environ, {"TEST_API_KEY": "test-secret"}), \
             patch("anybench.llm.urllib.request.urlopen", side_effect=[throttle, response]) as open_url, \
             patch("anybench.llm.time.sleep") as sleep:
            reply = ChatClient(config).complete([{"role": "user", "content": "hello"}])
        self.assertEqual(reply.message["content"], "done")
        self.assertEqual(open_url.call_count, 2)
        sleep.assert_called_once_with(0)


class ToolTests(unittest.TestCase):
    def test_container_starts_with_isolation_flags(self):
        case = Case("c", "/tmp/repo", "base", "target", "Fix", "", "")
        commands = []
        def fake_run(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0,
                                               "container-id\n" if argv[:2] == ["docker", "run"] else "", "")
        with patch("anybench.sandbox.prepare_snapshot",
                   side_effect=lambda repository, base_commit, root: root.mkdir()), \
             patch("anybench.sandbox._run", side_effect=fake_run):
            with Sandbox(case) as sandbox:
                self.assertEqual(sandbox.container, "container-id")
                self.assertEqual(Path(sandbox._tmp.name).stat().st_mode & 0o777, 0o700)
        run = commands[0]
        self.assertIn("--network", run)
        self.assertEqual(run[run.index("--network") + 1], "none")
        self.assertIn("--read-only", run)
        self.assertIn("--cap-drop", run)
        self.assertEqual(commands[-1][:3], ["docker", "rm", "-f"])

    def test_command_output_is_bounded(self):
        result = _limited_run(["python3", "-c", "print('x' * 2000)"], timeout=5,
                              limit=100)
        self.assertEqual(result.returncode, 124)
        self.assertIn("output exceeded", result.stdout)
        self.assertLess(len(result.stdout), 200)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.case = Case("c", str(self.root), "a", "b", "problem", "", "")
        self.sandbox = Sandbox(self.case)
        self.sandbox.root = self.root
        (self.root / "file.txt").write_text("one\ntwo\nthree\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_read_write_edit_and_glob(self):
        self.assertEqual(self.sandbox.read("file.txt", [[2, 3]]), "two\nthree\n")
        self.sandbox.edit("file.txt", "TWO\n", [[2, 2]])
        self.assertEqual(self.sandbox.read("file.txt"), "one\nTWO\nthree\n")
        self.sandbox.write("new.txt", "created\n")
        self.assertIn("new.txt", self.sandbox.bash("glob *.txt"))
        self.assertEqual(self.sandbox.read("new.txt"), "created\n")

    def test_rejects_escape_and_invalid_commands(self):
        with self.assertRaises(ToolError):
            self.sandbox.write("../escape", "bad")
        with self.assertRaises(ToolError):
            self.sandbox.edit("file.txt", "bad", [[0, 2]])
        with self.assertRaises(ToolError):
            self.sandbox.bash("rm file.txt")
        (self.root / "outside").symlink_to("/etc/passwd")
        with self.assertRaises(ToolError):
            self.sandbox.read("outside")
        (self.root / ".git").mkdir()
        (self.root / "git-link").symlink_to(".git")
        with self.assertRaises(ToolError):
            self.sandbox.write("git-link/config", "bad")

    def test_bash_read_commands_through_container_interface(self):
        self.sandbox.container = "fake"
        seen = []
        def command(argv, timeout=60):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "ok\n", "")
        with patch.object(self.sandbox, "command", side_effect=command):
            for name in ("cat", "grep", "wc", "jq"):
                self.assertEqual(self.sandbox.bash(f"{name} file.txt"), "ok\n")
        self.assertEqual([s[0] for s in seen], ["cat", "grep", "wc", "jq"])


class DockerToolIntegrationTests(unittest.TestCase):
    def test_tools_in_real_container(self):
        ready = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                               capture_output=True)
        image = subprocess.run(["docker", "image", "inspect", "anybench-sandbox:latest"],
                               capture_output=True) if ready.returncode == 0 else ready
        if ready.returncode or image.returncode:
            if os.environ.get("ANYBENCH_REQUIRE_DOCKER") == "1":
                self.fail("Docker daemon or anybench-sandbox:latest unavailable")
            self.skipTest("Docker daemon or anybench-sandbox:latest unavailable")
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "file.txt").write_text("one\ntwo\n")
            commit(repo, "Initial")
            base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                           text=True).strip()
            (repo / "file.txt").write_text("one\nTWO\n")
            commit(repo, "Uppercase second line")
            target = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                             text=True).strip()
            case = Case("c", str(repo), base, target, "Fix", "", "",
                        test_command="grep -q TWO file.txt")
            with Sandbox(case) as sandbox:
                self.assertEqual(sandbox.read("file.txt", [[1, 1]]), "one\n")
                sandbox.edit("file.txt", "TWO\n", [[2, 2]])
                sandbox.write("new.json", '{"a":1}\n')
                self.assertIn("TWO", sandbox.bash("cat file.txt"))
                self.assertIn("TWO", sandbox.bash("grep TWO file.txt"))
                self.assertIn("2", sandbox.bash("wc -l file.txt"))
                self.assertEqual(sandbox.bash("jq .a new.json").strip(), "1")
                self.assertIn("new.json", sandbox.bash("glob *.json"))
                self.assertTrue(sandbox.test("test -f new.json")[0])
                self.assertIn("+TWO", sandbox.diff())
                client = StubClient(
                    {"content": None, "tool_calls": [{"id": "delegate", "function": {
                        "name": "Agent", "arguments": json.dumps({"prompt": "Create delegated.txt"})}}]},
                    {"content": None, "tool_calls": [{"id": "write", "function": {
                        "name": "Write", "arguments": json.dumps({
                            "file_path": "delegated.txt", "content": "from subagent\n"})}}]},
                    {"content": "Subagent done"}, {"content": "Main done"})
                result, _, _, _, _, _ = agent_loop(client, sandbox, "Delegate")
                self.assertEqual(result, "Main done")
                self.assertIn("from subagent", sandbox.bash("cat delegated.txt"))
            self.assertEqual(validate_test_commands([case]), [])
            candidate = StubClient(
                {"content": None, "tool_calls": [{"id": "fix", "function": {
                    "name": "Edit", "arguments": json.dumps({
                        "file_path": "file.txt", "content": "TWO\n",
                        "line_range": [[2, 2]]})}}]},
                {"content": "Done"})
            config = ModelConfig("candidate", "https://example.test/v1", "model", "KEY")
            with patch("anybench.runner.ChatClient", return_value=candidate):
                record = run_one(case, config)
            self.assertEqual(record.status, "completed", record.error)
            self.assertTrue(record.test_passed)
            self.assertIn("+TWO", record.diff)


class RunnerTests(unittest.TestCase):
    def test_preflight_rejects_missing_key_and_docker(self):
        case = Case("c", "/tmp/repo", "a", "b", "Fix", "", "")
        config = ModelConfig("m", "https://example.com/v1", "m", "MISSING_KEY")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "MISSING_KEY"):
                preflight_run([case], [config], "image")
        with patch.dict(os.environ, {"MISSING_KEY": "secret"}), \
             patch("anybench.runner.subprocess.run", return_value=
                   subprocess.CompletedProcess([], 1, "", "socket denied")):
            with self.assertRaisesRegex(RuntimeError, "Docker daemon unavailable"):
                preflight_run([case], [config], "image")

    def test_cli_preflight_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            dataset = path / "cases.csv"
            models = path / "models.json"
            output = path / "results.jsonl"
            write_cases(dataset, [Case("c", "/tmp/repo", "a", "b", "Fix", "", "")])
            models.write_text(json.dumps([{"name": "m", "base_url": "https://example.com/v1",
                                           "model": "m", "api_key_env": "KEY"}]))
            output.write_text("previous results\n")
            with patch("anybench.cli.preflight_run", side_effect=RuntimeError("Docker denied")):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        main(["run", str(dataset), "--models", str(models), "--output", str(output)])
            self.assertEqual(output.read_text(), "previous results\n")

    def test_tool_file_error_returns_to_agent(self):
        case = Case("c", "/tmp/repo", "a", "b", "Fix", "", "")
        with tempfile.TemporaryDirectory() as temp:
            sandbox = Sandbox(case)
            sandbox.root = Path(temp)
            (sandbox.root / "directory").mkdir()
            client = StubClient({"tool_calls": [{"id": "1", "function": {
                "name": "Write", "arguments": json.dumps({"file_path": "directory",
                                                          "content": "text"})}}]},
                {"content": "Recovered"})
            result, trace, _, _, _, _ = agent_loop(client, sandbox, "Fix")
            self.assertEqual(result, "Recovered")
            self.assertIn("Tool error", trace[0]["result"])

    def test_agent_tools_and_subagent_limit(self):
        case = Case("c", "/tmp/repo", "a", "b", "Fix", "", "")
        sandbox = Sandbox(case)
        def call(name, arguments, ident):
            return {"id": ident, "type": "function", "function": {"name": name,
                    "arguments": json.dumps(arguments)}}
        client = StubClient(
            {"content": None, "tool_calls": [call("Agent", {"prompt": "inspect"}, "1")]},
            {"content": None, "tool_calls": [call("Write", {"file_path": "a", "content": "x"}, "2")]},
            {"content": "done"}, {"content": "finished"})
        with patch.object(sandbox, "write", return_value="Wrote a") as write:
            final, trace, _, _, count, _ = agent_loop(client, sandbox, "Fix")
        self.assertEqual(final, "finished")
        self.assertEqual(count, 2)
        write.assert_called_once()
        self.assertTrue(all(t["function"]["name"] != "Agent" for t in client.calls[1][1]))

    def test_concurrency_setting_schedules_all_attempts(self):
        case = Case("c", "/tmp/repo", "a", "b", "Fix", "", "")
        config = ModelConfig("m", "https://example.com/v1", "m", "KEY")
        images = []
        completed = []
        def fake_run(c, m, attempt, image, steps):
            images.append(image)
            return RunRecord(c.case_id, m.name, attempt, "completed", 1)
        with patch("anybench.runner.run_one", side_effect=fake_run):
            results = run_sweep([case], [config], [1, 2], attempts=3,
                                image_map={case.repository: "repo-image:latest"},
                                on_record=completed.append)
        self.assertEqual([r.concurrency for r in results], [1, 1, 1, 2, 2, 2])
        self.assertEqual([r.attempt for r in results], [1, 2, 3, 1, 2, 3])
        self.assertEqual(images, ["repo-image:latest"] * 6)
        self.assertEqual(len(completed), 6)

    def test_results_append_during_run(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "results.jsonl"
            write_jsonl(path, [])
            append_jsonl(path, RunRecord("a", "m", 1, "completed", 1))
            append_jsonl(path, RunRecord("b", "m", 1, "completed", 1))
            self.assertEqual([r.case_id for r in read_jsonl(path)], ["a", "b"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class EvaluationReportTests(unittest.TestCase):
    def test_rejects_non_discriminating_test_command(self):
        case = Case("c", "/tmp/repo", "base", "gold", "Fix", "", "",
                    test_command="check")
        class FakeSandbox:
            def __init__(self, case, image):
                self.case = case
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def test(self, command):
                return True, "passed"
        with patch("anybench.evaluate.Sandbox", FakeSandbox):
            errors = validate_test_commands([case])
        self.assertIn("test also passes before", errors[0])

    def test_judge_and_html_report(self):
        case = Case("c", "/tmp/repo", "a", "b", "Fix", "", "+fix")
        record = RunRecord("c", "model", 1, "completed", 2, diff="+fix")
        class UsageClient:
            def complete(self, messages):
                return Reply({"content": '{"score":0.9,"reason":"Works"}'}, 3, 2, 1.5)
        judge(record, case, UsageClient())
        self.assertEqual(record.judge_score, 0.9)
        self.assertEqual(record.completion_tokens, 0)
        self.assertEqual(record.judge_completion_tokens, 2)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.html"
            report([record], path)
            self.assertIn("Accuracy vs speed", path.read_text())
            self.assertIn("90.0%", path.read_text())

    def test_report_separates_concurrency_levels(self):
        records = [RunRecord("a", "m", 1, "completed", 2, started_at=100,
                             finished_at=104, concurrency=1, judge_score=0.5,
                             model_seconds=2, completion_tokens=20),
                   RunRecord("b", "m", 1, "completed", 2, started_at=101,
                             finished_at=104, concurrency=1, judge_score=0.5),
                   RunRecord("a", "m", 1, "completed", 1, started_at=200,
                             finished_at=202, concurrency=2, judge_score=0.5),
                   RunRecord("b", "m", 1, "completed", 1, started_at=200,
                             finished_at=202, concurrency=2, judge_score=0.5)]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.html"
            report(records, path)
            output = path.read_text()
            self.assertIn("1800.0", output)
            self.assertIn("3600.0", output)
            self.assertIn("Quadrants split", output)
            self.assertIn("Output tokens/s", output)
            self.assertIn("2.00×", output)
            self.assertIn("Efficiency", output)

    def test_report_counts_failures_and_test_disagreement(self):
        records = [RunRecord("a", "m", 1, "completed", 1, judge_score=1,
                             test_passed=False),
                   RunRecord("b", "m", 1, "error", 1),
                   RunRecord("c", "other", 1, "completed", 1)]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.html"
            report(records, path)
            output = path.read_text()
            self.assertIn("N/A", output)
            self.assertIn("Coverage", output)
            self.assertIn("0.0%", output)


class PipelineTests(unittest.TestCase):
    def test_synthetic_repository_pipeline(self):
        class LocalSandbox(Sandbox):
            # Tests orchestration with a controlled local checkout; Docker is tested separately.
            def __enter__(self):
                self._tmp = tempfile.TemporaryDirectory()
                self.root = Path(self._tmp.name) / "repo"
                prepare_snapshot(self.case.repository, self.case.base_commit, self.root)
                return self

            def test(self, command, timeout=120):
                result = subprocess.run(["sh", "-lc", command], cwd=self.root,
                                        capture_output=True, text=True, timeout=timeout)
                return result.returncode == 0, result.stdout + result.stderr

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "project"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "app.py").write_text("value = 1\n")
            commit(repo, "Initial")
            (repo / "app.py").write_text("value = 2\n")
            commit(repo, "Correct value")
            builder = StubClient({"content": json.dumps({
                "problem_statement": "Make app.value equal 2", "hint": "See app.py",
                "test_command": "python -c 'import app; assert app.value == 2'",
                "external_validation": False, "external_validation_reason": ""})})
            dataset = Path(temp) / "cases.csv"
            write_cases(dataset, build_dataset([repo], builder, commits=2))
            case = read_cases(dataset)[0]
            tool_call = {"id": "write-1", "type": "function", "function": {
                "name": "Write", "arguments": json.dumps({"file_path": "app.py",
                                                          "content": "value = 2\n"})}}
            candidate = StubClient({"content": None, "tool_calls": [tool_call]},
                                   {"content": "Done"})
            config = ModelConfig("candidate", "https://example.test/v1", "model", "KEY")
            with patch("anybench.runner.Sandbox", LocalSandbox), \
                 patch("anybench.runner.ChatClient", return_value=candidate):
                record = run_one(case, config)
            self.assertEqual(record.status, "completed", record.error)
            self.assertTrue(record.test_passed)
            self.assertIn("+value = 2", record.diff)
            self.assertIn(case.base_commit, candidate.calls[0][0][1]["content"])
            judge_client = StubClient({"content": '{"score":1,"reason":"Correct"}'})
            evaluate([record], [case], judge_client)
            self.assertEqual(record.judge_score, 1)
            output = Path(temp) / "report.html"
            report([record], output)
            self.assertIn("100.0%", output.read_text())


if __name__ == "__main__":
    unittest.main()
