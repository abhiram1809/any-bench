import json
import copy
import contextlib
import io
import os
import subprocess
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.response import addinfourl

from anybench.dataset import analyze_commit, build_dataset, validate_dataset
from anybench.cli import main
from anybench.evaluate import evaluate, judge, validate_test_commands
from anybench.harness import execute_harness, parse_events
from anybench.llm import ChatClient, Reply, parse_json_object
from anybench.model import (Case, ModelConfig, RunRecord, append_jsonl,
                            append_case, read_cases, read_jsonl, write_cases, write_jsonl)
from anybench.report import report
from anybench.runner import agent_loop, preflight_run, run_one, run_sweep
from anybench.sandbox import Sandbox, ToolError, _extract_checkout, _limited_run, prepare_snapshot


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
            self.assertIn("value = 1\n", client.calls[1][0][-1]["content"])

    def test_builder_forces_decision_after_bounded_inspection(self):
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
            request = {"tool_calls": [{"id": "1", "function": {"name": "RecentCommits",
                        "arguments": "{}"}}]}
            client = StubClient(*(request for _ in range(4)),
                                {"content": '{"eligible":false}'})
            self.assertFalse(analyze_commit(client, repo, base, target, "Analyze")["eligible"])
            self.assertTrue(all(tools for _, tools in client.calls[:4]))
            self.assertIsNone(client.calls[4][1])

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
            cache = root / ".pytest_cache" / "v"
            cache.mkdir(parents=True)
            (cache / "nodeids").write_text("generated\n")
            diff = sandbox.diff()
            self.assertIn("+value = 2", diff)
            self.assertIn("new.log", diff)
            self.assertNotIn(".pytest_cache", diff)


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
        opener = Mock()
        opener.open.side_effect = fake_open
        with patch.dict(os.environ, {"TEST_API_KEY": "test-secret"}), \
             patch("anybench.llm.urllib.request.build_opener", return_value=opener):
            reply = ChatClient(config).complete([{"role": "user", "content": "hello"}])
        self.assertEqual(seen["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(seen["authorization"], "Bearer test-secret")
        self.assertEqual(seen["body"]["model"], "model-x")
        self.assertEqual((reply.prompt_tokens, reply.completion_tokens), (3, 2))

    def test_openrouter_uses_stable_session_for_cache_routing(self):
        config = ModelConfig("provider", "https://openrouter.ai/api/v1", "model-x", "KEY")
        client = ChatClient(config)
        bodies = []
        def fake_open(request, timeout):
            bodies.append(json.loads(request.data))
            return io.BytesIO(b'{"choices":[{"message":{"content":"done"}}]}')
        opener = Mock()
        opener.open.side_effect = fake_open
        with patch.dict(os.environ, {"KEY": "test-secret"}), \
             patch("anybench.llm.urllib.request.build_opener", return_value=opener):
            client.complete([{"role": "user", "content": "hello"}])
            client.complete([{"role": "user", "content": "hello again"}])
        self.assertEqual(bodies[0]["session_id"], bodies[1]["session_id"])
        self.assertTrue(bodies[0]["session_id"])
        self.assertEqual(client.session_id, ChatClient(config).session_id)

    def test_chat_reasoning_effort_is_opt_in(self):
        config = ModelConfig("provider", "https://example.test/v1", "model", "KEY",
                             reasoning_effort="low")
        self.assertEqual(ChatClient(config)._payload([{"role": "user", "content": "hi"}], None)
                         ["reasoning_effort"], "low")
        with self.assertRaisesRegex(ValueError, "reasoning_effort"):
            ModelConfig("provider", "https://example.test/v1", "model", "KEY",
                        reasoning_effort="extreme")
        with self.assertRaisesRegex(ValueError, "reasoning_effort"):
            ModelConfig("provider", "https://example.test/v1", "model", "KEY",
                        api="responses", reasoning_effort="low")

    def test_retries_throttled_request(self):
        config = ModelConfig("provider", "https://example.test/v1", "model-x",
                             "TEST_API_KEY", max_retries=1)
        response = io.BytesIO(b'{"choices":[{"message":{"content":"done"}}]}')
        throttle = urllib.error.HTTPError("https://example.test", 429, "throttled",
                                          {"Retry-After": "0"}, io.BytesIO(b"busy"))
        opener = Mock()
        opener.open.side_effect = [throttle, response]
        with patch.dict(os.environ, {"TEST_API_KEY": "test-secret"}), \
             patch("anybench.llm.urllib.request.build_opener", return_value=opener), \
             patch("anybench.llm.time.sleep") as sleep:
            reply = ChatClient(config).complete([{"role": "user", "content": "hello"}])
        self.assertEqual(reply.message["content"], "done")
        self.assertEqual(opener.open.call_count, 2)
        sleep.assert_called_once_with(0)

    def test_timeout_with_unknown_billing_is_not_retried(self):
        config = ModelConfig("provider", "https://example.test/v1", "model-x",
                             "TEST_API_KEY", max_retries=2)
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(TimeoutError("timed out"))
        with patch.dict(os.environ, {"TEST_API_KEY": "test-secret"}), \
             patch("anybench.llm.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "billing state unknown"):
                ChatClient(config).complete([{"role": "user", "content": "hello"}])
        self.assertEqual(opener.open.call_count, 1)

    def test_remote_http_is_rejected_but_loopback_http_is_allowed(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            ModelConfig("provider", "http://example.test/v1", "model", "KEY")
        self.assertEqual(ModelConfig("local", "http://127.0.0.1:8000/v1", "model", "KEY")
                         .base_url, "http://127.0.0.1:8000/v1")

    def test_redirect_does_not_forward_api_key(self):
        requested = []
        class RedirectingTransport(urllib.request.BaseHandler):
            handler_order = 100
            def http_open(self, request):
                requested.append(request.full_url)
                headers = Message()
                headers["Location"] = "http://127.0.0.1:2/collect"
                response = addinfourl(io.BytesIO(), headers, request.full_url, code=302)
                response.msg = "Found"
                return response
        build_opener = urllib.request.build_opener
        config = ModelConfig("local", "http://127.0.0.1:1/v1", "model", "TEST_API_KEY")
        with patch.dict(os.environ, {"TEST_API_KEY": "dummy-key"}), \
             patch("anybench.llm.urllib.request.build_opener",
                   side_effect=lambda handler: build_opener(RedirectingTransport(), handler)):
            with self.assertRaisesRegex(RuntimeError, "redirect blocked"):
                ChatClient(config).complete([{"role": "user", "content": "hello"}])
        self.assertEqual(requested, ["http://127.0.0.1:1/v1/chat/completions"])

    def test_responses_tool_round_trip_and_cache_usage(self):
        config = ModelConfig("r", "https://example.test/v1", "model", "KEY", api="responses")
        calls = []
        responses = [
            {"output": [{"type": "reasoning", "id": "rs_1", "summary": []},
                        {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                         "name": "Read", "arguments": '{"file_path":"a"}'}],
             "usage": {"input_tokens": 20, "output_tokens": 4,
                       "input_tokens_details": {"cached_tokens": 12}}},
            {"output": [{"type": "message", "content": [{"type": "output_text",
                                                    "text": "done"}]}],
             "usage": {"input_tokens": 30, "output_tokens": 2}}]
        def fake_open(request, timeout):
            calls.append((request.full_url, json.loads(request.data)))
            return io.BytesIO(json.dumps(responses.pop(0)).encode())
        opener = Mock()
        opener.open.side_effect = fake_open
        with patch.dict(os.environ, {"KEY": "secret"}), \
             patch("anybench.llm.urllib.request.build_opener", return_value=opener):
            client = ChatClient(config)
            first = client.complete([{"role": "user", "content": "fix"}],
                                    [{"type": "function", "function": {"name": "Read",
                                      "parameters": {"type": "object"}}}])
            second = client.complete([{"role": "user", "content": "fix"}, first.message,
                                      {"role": "tool", "tool_call_id": "call_1", "content": "text"}])
        self.assertEqual(calls[0][0], "https://example.test/v1/responses")
        self.assertEqual(calls[1][1]["input"][-1]["type"], "function_call_output")
        self.assertEqual(calls[1][1]["input"][1]["type"], "reasoning")
        self.assertEqual(first.message["tool_calls"][0]["id"], "call_1")
        self.assertEqual(second.message["content"], "done")
        self.assertEqual(client.cached_prompt_tokens, 12)

    def test_anthropic_tool_round_trip_and_total_input_usage(self):
        config = ModelConfig("a", "https://example.test/v1", "model", "KEY", api="anthropic")
        seen = []
        responses = [{"content": [{"type": "tool_use", "id": "tool_1", "name": "Read",
                                   "input": {"file_path": "a"}}],
                      "usage": {"input_tokens": 5, "output_tokens": 2,
                                "cache_read_input_tokens": 10,
                                "cache_creation_input_tokens": 3}},
                     {"content": [{"type": "text", "text": "done"}], "usage": {}}]
        def fake_open(request, timeout):
            seen.append((request.full_url, request.get_header("X-api-key"), json.loads(request.data)))
            return io.BytesIO(json.dumps(responses.pop(0)).encode())
        opener = Mock()
        opener.open.side_effect = fake_open
        with patch.dict(os.environ, {"KEY": "secret"}), \
             patch("anybench.llm.urllib.request.build_opener", return_value=opener):
            client = ChatClient(config)
            first = client.complete([{"role": "system", "content": "stable"},
                                     {"role": "user", "content": "fix"}])
            client.complete([{"role": "system", "content": "stable"},
                             {"role": "user", "content": "fix"}, first.message,
                             {"role": "tool", "tool_call_id": "tool_1", "content": "text"}])
        self.assertEqual(seen[0][0], "https://example.test/v1/messages")
        self.assertEqual(seen[0][1], "secret")
        self.assertEqual(first.prompt_tokens, 18)
        self.assertEqual(seen[1][2]["messages"][-1]["content"][0]["tool_use_id"], "tool_1")
        self.assertEqual(client.cached_prompt_tokens, 10)

    def test_external_config_validation(self):
        with self.assertRaisesRegex(ValueError, "image and allowed_hosts"):
            ModelConfig("x", model="model", harness="custom", command=["tool"],
                        api_key_env="KEY")
        with self.assertRaisesRegex(ValueError, "proxy settings"):
            ModelConfig("x", model="model", harness="custom", command=["tool"],
                        image="image", allowed_hosts=["api.example.com"],
                        env={"HTTPS_PROXY": "KEY"})

    def test_headless_event_usage(self):
        codex = parse_events("codex", '\n'.join([
            json.dumps({"type": "item.completed", "item": {"type": "command_execution"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 30,
                        "cached_input_tokens": 10, "output_tokens": 5}})]))
        self.assertEqual((codex.prompt_tokens, codex.cached_prompt_tokens,
                          codex.tool_calls), (30, 10, 1))
        claude = parse_events("claude", json.dumps({"type": "result", "usage": {
            "input_tokens": 3, "output_tokens": 2, "cache_read_input_tokens": 7}}))
        self.assertEqual(claude.cached_prompt_tokens, 7)
        opencode = parse_events("opencode", json.dumps({"type": "step_finish", "part": {
            "type": "step-finish", "tokens": {"input": 3, "output": 2,
                                               "cache": {"read": 7, "write": 1}}}}))
        self.assertEqual((opencode.prompt_tokens, opencode.cached_prompt_tokens,
                          opencode.cache_creation_tokens), (11, 7, 1))

    def test_headless_adapters_use_structured_mode(self):
        case = Case("c", "/tmp/repo", "a", "b", "Fix", "", "")
        for harness, flag in (("codex", "--json"), ("claude", "stream-json"),
                              ("opencode", "json")):
            with self.subTest(harness=harness):
                config = ModelConfig(harness, model="model", api_key_env="KEY",
                                     harness=harness, image="image",
                                     allowed_hosts=["api.example.com"])
                sandbox = Mock()
                sandbox.container = "container"
                sandbox.command.return_value = subprocess.CompletedProcess([], 0, "", "")
                with patch.dict(os.environ, {"KEY": "private-test-value"}), \
                     patch("anybench.harness.subprocess.run", return_value=
                           subprocess.CompletedProcess([], 0, "", "")) as run:
                    execute_harness(sandbox, case, config)
                self.assertIn(flag, sandbox.command.call_args_list[0].args[0])
                self.assertNotIn("private-test-value", str(sandbox.command.call_args_list))
                self.assertIn("private-test-value", run.call_args_list[1].kwargs["input"])
                if harness == "codex":
                    self.assertIn("--dangerously-bypass-approvals-and-sandbox",
                                  sandbox.command.call_args_list[0].args[0])


class ToolTests(unittest.TestCase):
    def test_harness_archive_rejects_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            for name, link in (("../outside", None), ("link", "../outside")):
                with self.subTest(name=name):
                    content = io.BytesIO()
                    with tarfile.open(fileobj=content, mode="w") as archive:
                        entry = tarfile.TarInfo(name)
                        if link:
                            entry.type = tarfile.SYMTYPE
                            entry.linkname = link
                        else:
                            entry.size = 1
                        archive.addfile(entry, io.BytesIO(b"x") if not link else None)
                    content.seek(0)
                    with tarfile.open(fileobj=content, mode="r|") as archive:
                        with self.assertRaisesRegex(RuntimeError, "unsafe|escapes"):
                            _extract_checkout(archive, Path(temp), 1024)
            self.assertFalse((Path(temp).parent / "outside").exists())

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
        self.assertIn("type=bind,src=", run[run.index("--mount") + 1])
        self.assertIn("dst=/seed,readonly", run[run.index("--mount") + 1])
        self.assertIn("/repo:rw,exec,nosuid,nodev,size=512m,mode=1777", run)
        self.assertEqual(commands[1][:4], ["docker", "exec", "container-id", "cp"])
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
    def test_custom_harness_isolated_checkout_and_egress(self):
        ready = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                               capture_output=True)
        image = subprocess.run(["docker", "image", "inspect", "python:3.11-slim"],
                               capture_output=True) if ready.returncode == 0 else ready
        if ready.returncode or image.returncode:
            if os.environ.get("ANYBENCH_REQUIRE_DOCKER") == "1":
                self.fail("Docker daemon or python:3.11-slim unavailable")
            self.skipTest("Docker daemon or python:3.11-slim unavailable")
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "file.txt").write_text("before\n")
            commit(repo, "Initial")
            base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                           text=True).strip()
            script = ("import json, pathlib, socket, urllib.request, urllib.error; "
                      "task=json.loads(pathlib.Path('/tmp/task.json').read_text()); "
                      "assert task['problem_statement']=='Fix'; "
                      "blocked=False; "
                      "\ntry: urllib.request.urlopen('https://blocked.example.invalid', timeout=5)"
                      "\nexcept urllib.error.URLError as error: blocked='403' in str(error)"
                      "\nassert blocked; direct_blocked=False"
                      "\ntry: socket.create_connection(('1.1.1.1', 443), timeout=2)"
                      "\nexcept OSError: direct_blocked=True"
                      "\nassert direct_blocked; "
                      "pathlib.Path('/repo/file.txt').write_text('after\\n'); "
                      "pathlib.Path('/tmp/usage.json').write_text(json.dumps({'prompt_tokens': 5, "
                      "'completion_tokens': 2, 'tool_calls': 1}))")
            case = Case("custom", str(repo), base, base, "Fix", "", "",
                        test_command="python -c 'assert open(\"file.txt\").read() == \"after\\n\"'")
            config = ModelConfig("custom", model="test-model", api_key_env="TEST_KEY",
                                 harness="custom", command=["python", "-c", script],
                                 image="python:3.11-slim", allowed_hosts=["api.example.com"])
            with patch.dict(os.environ, {"TEST_KEY": "dummy"}):
                record = run_one(case, config)
            self.assertEqual(record.status, "completed", record.error)
            self.assertTrue(record.test_passed)
            self.assertIn("+after", record.diff)
            self.assertEqual(record.prompt_tokens, 5)
            self.assertEqual((repo / "file.txt").read_text(), "before\n")

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
            gold_diff = subprocess.check_output(
                ["git", "-C", str(repo), "diff", "--binary", base, target], text=True)
            case = Case("c", str(repo), base, target, "Fix", "", gold_diff,
                        test_command="grep -q TWO file.txt")
            with Sandbox(case) as sandbox:
                self.assertEqual(sandbox.command(["git", "status", "--porcelain"]).returncode, 0)
                self.assertEqual(sandbox.read("file.txt", [[1, 1]]), "one\n")
                sandbox.edit("file.txt", "TWO\n", [[2, 2]])
                sandbox.write("new.json", '{"a":1}\n')
                self.assertIn("TWO", sandbox.bash("cat file.txt"))
                self.assertIn("TWO", sandbox.bash("grep TWO file.txt"))
                self.assertIn("2", sandbox.bash("wc -l file.txt"))
                self.assertEqual(sandbox.bash("jq .a new.json").strip(), "1")
                self.assertIn("new.json", sandbox.bash("glob *.json"))
                sandbox.write("name$(touch /repo/escaped).txt", "literal\n")
                self.assertEqual(sandbox.bash("cat 'name$(touch /repo/escaped).txt'").strip(),
                                 "literal")
                self.assertFalse(sandbox.test("test -e escaped")[0])
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
            with Sandbox(case, workspace_size="8m") as limited:
                passed, output = limited.test("dd if=/dev/zero of=large.bin bs=1M count=16 status=none")
                self.assertFalse(passed)
                self.assertIn("No space left on device", output)
                self.assertFalse((limited.root / "large.bin").exists())


class RunnerTests(unittest.TestCase):
    def test_external_preflight_checks_harness_binary(self):
        case = Case("c", "/tmp/repo", "a", "b", "Fix", "", "")
        config = ModelConfig("custom", model="model", api_key_env="KEY", harness="custom",
                             image="runner-image", allowed_hosts=["api.example.com"],
                             command=["missing-cli"])
        def fake_run(argv, **kwargs):
            code = 1 if argv[:3] == ["docker", "run", "--rm"] else 0
            return subprocess.CompletedProcess(argv, code, "ok", "")
        with patch.dict(os.environ, {"KEY": "dummy"}), \
             patch("anybench.runner.subprocess.run", side_effect=fake_run) as run:
            with self.assertRaisesRegex(RuntimeError, "lacks missing-cli"):
                preflight_run([case], [config], "anybench-sandbox:latest")
        self.assertTrue(any(call.args[0][:3] == ["docker", "run", "--rm"]
                            for call in run.call_args_list))

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
        limits = []
        completed = []
        def fake_run(c, m, attempt, image, steps, workspace_size, memory):
            images.append(image)
            limits.append((workspace_size, memory))
            return RunRecord(c.case_id, m.name, attempt, "completed", 1)
        with patch("anybench.runner.run_one", side_effect=fake_run):
            results = run_sweep([case], [config], [1, 2], attempts=3,
                                image_map={case.repository: "repo-image:latest"},
                                on_record=completed.append, workspace_size="128m", memory="2g")
        self.assertEqual([r.concurrency for r in results], [1, 1, 1, 2, 2, 2])
        self.assertEqual([r.attempt for r in results], [1, 2, 3, 1, 2, 3])
        self.assertEqual(images, ["repo-image:latest"] * 6)
        self.assertEqual(limits, [("128m", "2g")] * 6)
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
    def test_report_distinguishes_harness_and_unknown_usage(self):
        records = [RunRecord("a", "same", 1, "completed", 2, harness="anybench",
                             model_id="model", prompt_tokens=12),
                   RunRecord("a", "same", 1, "completed", 2, harness="custom",
                             model_id="model", usage_available=False, tool_calls=None)]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.html"
            report(records, path)
            output = path.read_text()
        self.assertIn("same (anybench)", output)
        self.assertIn("same (custom)", output)
        self.assertIn("N/A", output)

    def test_rejects_non_discriminating_test_command(self):
        case = Case("c", "/tmp/repo", "base", "gold", "Fix", "", "",
                    test_command="check")
        class FakeSandbox:
            def __init__(self, case, image, workspace_size, memory, **kwargs):
                self.case = case
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def command(self, argv, timeout=120):
                return subprocess.CompletedProcess(argv, 0, "Ran 1 test\nOK", "")
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
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.chmod(0o644)
            report([record], path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

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
            self.assertIn("Each point shows local test success", output)
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
                if self.evaluation_patch is not None:
                    self.prepare_evaluation()
                return self

            def command(self, argv, timeout=120):
                return subprocess.run(argv, cwd=self.root, capture_output=True, text=True, timeout=timeout)

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
            config = ModelConfig("candidate", "https://example.test/v1", "model", "KEY",
                                 context_profile="legacy")
            with patch("anybench.runner.Sandbox", LocalSandbox), \
                 patch("anybench.evaluate.Sandbox", LocalSandbox), \
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


class ContextOrchestrationTests(unittest.TestCase):
    def setUp(self):
        from anybench.workspace_tool import execute
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.artifacts = Path(self.temp.name) / 'artifacts'
        self.config = ModelConfig('test', 'https://example.test/v1', 'model', 'KEY')
        self.sandbox = Mock(root=self.root)
        self.sandbox.enhanced_tool.side_effect = lambda name, args: execute(self.root, name, args)

    @staticmethod
    def call(name, arguments, ident='one'):
        return {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': ident, 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(arguments)}}]}

    def run_loop(self, client, steps=30, config=None):
        from anybench.enhanced import enhanced_loop
        return enhanced_loop(client, self.sandbox, 'Fix public behavior', config or self.config,
                             max_steps=steps, artifact_base=self.artifacts)

    def test_default_context_and_old_records(self):
        self.assertEqual(self.config.context_window_tokens, 200_000)
        self.assertEqual(self.config.context_profile, 'enhanced')
        old = RunRecord('c', 'm', 1, 'completed', 1)
        self.assertEqual(old.context_profile, 'legacy')
        self.assertIsNone(old.compactions)
        with self.assertRaises(ValueError):
            ModelConfig('x', 'https://example.test', 'm', 'KEY', context_window_tokens=100)

    def test_full_read_and_explicit_range(self):
        text = 'a long line of source code\n' * 2000
        (self.root / 'source.py').write_text(text)
        client = StubClient(self.call('Read', {'file_path': 'source.py'}),
                            self.call('Read', {'file_path': 'source.py', 'lines_range': [[1999, 2000]]}, 'two'),
                            {'content': 'Done'})
        result = self.run_loop(client)
        self.assertEqual(result.stop_reason, 'completed', result.error)
        full = client.calls[1][0][-1]['content']
        self.assertGreater(len(full), 20000)
        self.assertIn('2000: a long line of source code', full)
        self.assertNotIn('preview', full)
        selected = client.calls[2][0][-1]['content']
        self.assertIn('1999: ', selected)
        self.assertNotIn('1998: ', selected)
        self.assertEqual(client.calls[0][0], client.calls[1][0][:len(client.calls[0][0])])
        self.assertEqual(client.calls[0][1], client.calls[1][1])

    def test_large_file_can_be_read_by_range(self):
        (self.root / 'large').write_text('line\n' * 410000)
        from anybench.workspace_tool import execute
        with self.assertRaisesRegex(ValueError, 'request a line range'):
            execute(self.root, 'Read', {'file_path': 'large'})
        result = execute(self.root, 'Read', {'file_path': 'large', 'lines_range': [[409999, 410000]]})
        self.assertIn('410000: line', result['output'])
        self.assertNotIn('409998: line', result['output'])

    def test_single_flat_line_range_is_accepted(self):
        from anybench.workspace_tool import execute
        (self.root / 'source.py').write_text('one\ntwo\nthree\n')
        result = execute(self.root, 'Read', {'file_path': 'source.py', 'lines_range': [2, 3]})
        self.assertIn('2: two', result['output'])
        self.assertNotIn('1: one', result['output'])
        execute(self.root, 'Edit', {'file_path': 'source.py', 'line_range': [2, 2],
                                    'content': 'changed'})
        self.assertEqual((self.root / 'source.py').read_text(), 'one\nchanged\nthree\n')

    def test_search_accepts_file_path(self):
        from anybench.workspace_tool import execute
        (self.root / 'source.py').write_text('one\nneedle\nthree\n')
        result = execute(self.root, 'Search', {'path': 'source.py', 'query': 'needle'})
        self.assertEqual(result['output'], 'source.py:2: needle\n')

    def test_oversized_read_is_an_explicit_error(self):
        (self.root / 'large').write_text('large content\n' * 5000)
        self.config.context_window_tokens = 12000
        self.config.max_output_tokens = 1000
        client = StubClient(self.call('Read', {'file_path': 'large'}), {'content': 'Use ranges'})
        result = self.run_loop(client)
        self.assertEqual(result.stop_reason, 'completed', result.error)
        self.assertIn('Specify explicit line ranges', client.calls[1][0][-1]['content'])
        self.assertNotIn('1: large content', client.calls[1][0][-1]['content'])

    def test_guidance_is_scoped_and_edits_wait_for_review(self):
        (self.root / 'AGENTS.md').write_text('Root convention')
        (self.root / 'CLAUDE.md').write_text('Fallback must not load')
        (self.root / 'pkg').mkdir()
        (self.root / 'pkg' / 'CLAUDE.md').write_text('Nested convention')
        client = StubClient(self.call('Write', {'file_path': 'pkg/new.py', 'content': 'ok'}),
                            self.call('Write', {'file_path': 'pkg/new.py', 'content': 'ok'}, 'two'),
                            {'content': 'Done'})
        result = self.run_loop(client)
        self.assertEqual(result.stop_reason, 'completed', result.error)
        first = json.dumps(client.calls[0][0])
        second = json.dumps(client.calls[1][0])
        self.assertIn('Root convention', first)
        self.assertNotIn('Nested convention', first)
        self.assertNotIn('Fallback must not load', second)
        self.assertIn('Nested convention', second)
        self.assertIn('retry the edit', second)
        self.assertEqual((self.root / 'pkg/new.py').read_text(), 'ok')

    def test_instruction_symlinks_cannot_escape(self):
        from anybench.context import Instructions
        (Path(self.temp.name) / 'AGENTS.md').write_text('HOST SECRET')
        (self.root / 'AGENTS.md').symlink_to(Path(self.temp.name) / 'AGENTS.md')
        instructions = Instructions(self.root)
        self.assertEqual(instructions.load('.', directory=True), [])
        with self.assertRaises(ToolError):
            instructions.load('../AGENTS.md')

    def test_artifact_isolation_and_retrieval(self):
        from anybench.context import Artifacts, ContextExhausted
        first = Artifacts(self.artifacts)
        second = Artifacts(self.artifacts)
        handle = first.put('one\ntwo\nthree\n')
        self.assertIn('2: two', first.get(handle, 2, 2))
        self.assertNotIn('one', first.get(handle, query='three'))
        with self.assertRaises(ToolError):
            second.get(handle)
        with self.assertRaises(ToolError):
            first.get('../../etc/passwd')
        self.assertEqual(first.handles[handle].stat().st_mode & 0o777, 0o600)
        first.limit = first.size
        with self.assertRaises(ContextExhausted):
            first.put('more')

    def test_search_outputs_remain_recoverable(self):
        (self.root / 'many.py').write_text('match\n' * 4000)
        client = StubClient(self.call('Search', {'query': 'match'}), {'content': 'Done'})
        result = self.run_loop(client)
        self.assertEqual(result.stop_reason, 'completed', result.error)
        self.assertIn('Full collected output: artifact', client.calls[1][0][-1]['content'])
        event = next(event for event in result.trace if event.get('tool') == 'Search')
        full = (Path(result.artifact_directory) / (event['output_artifact'] + '.txt')).read_text()
        self.assertIn('many.py:2000: match', full)
        self.assertIn('many.py:4000: match', full)

    def test_readonly_subagent_and_shared_budget(self):
        client = StubClient(self.call('Agent', {'prompt': 'Review files'}),
                            self.call('Write', {'file_path': 'bad', 'content': 'bad'}, 'child'),
                            {'content': 'Cannot edit; reviewed source'}, {'content': 'Done'})
        result = self.run_loop(client, steps=4)
        self.assertEqual(result.stop_reason, 'completed', result.error)
        self.assertEqual(result.model_calls_by_purpose, {'main': 2, 'subagent': 2, 'compaction': 0})
        self.assertFalse((self.root / 'bad').exists())
        child_tools = {t['function']['name'] for t in client.calls[1][1]}
        self.assertFalse(child_tools & {'Run', 'Write', 'Edit', 'Agent'})
        self.assertNotIn('Unknown or unavailable tool: Write', json.dumps(client.calls[-1][0]))
        self.assertIn('Subagent transcript artifact', json.dumps(client.calls[-1][0]))

    def test_exhaustion_keeps_edits_and_trace(self):
        client = StubClient(self.call('Write', {'file_path': 'kept', 'content': 'partial'}))
        result = self.run_loop(client, steps=1)
        self.assertEqual(result.stop_reason, 'step_limit')
        self.assertEqual((self.root / 'kept').read_text(), 'partial')
        self.assertEqual(result.model_calls_by_purpose['main'], 1)
        self.assertTrue(result.trace)

    def make_context(self, api='chat_completions'):
        from anybench.context import Artifacts, Context, Instructions
        config = ModelConfig('test', 'https://example.test', 'model', 'KEY', api=api,
                             context_window_tokens=12000, max_output_tokens=1000)
        (self.root / 'AGENTS.md').write_text('Exact durable rules')
        instructions = Instructions(self.root)
        instructions.load('.', directory=True)
        ctx = Context(config, 'stable system', 'Exact original task', [],
                      Artifacts(self.artifacts), instructions)
        for i in range(4):
            message = self.call('Read', {'file_path': 'source.py'}, f'tool{i}')
            if api == 'responses':
                message['_raw_output'] = [
                    {'type': 'reasoning', 'id': f'r{i}', 'encrypted_content': 'opaque'},
                    {'type': 'function_call', 'id': f'f{i}', 'call_id': f'tool{i}',
                     'name': 'Read', 'arguments': '{"file_path":"source.py"}'}]
            if api == 'anthropic':
                message['_raw_content'] = [
                    {'type': 'thinking', 'thinking': 'provider data', 'signature': 'opaque'},
                    {'type': 'tool_use', 'id': f'tool{i}', 'name': 'Read', 'input': {'file_path': 'source.py'}}]
            ctx.groups.append([message, {'role': 'tool', 'tool_call_id': f'tool{i}', 'content': 'fact ' * 1500}])
        return ctx

    def test_compaction_preserves_task_rules_and_native_exchanges(self):
        for api in ('chat_completions', 'responses', 'anthropic'):
            with self.subTest(api=api):
                ctx = self.make_context(api)
                latest = copy.deepcopy(ctx.groups[-1])
                def summarize(messages, tools, purpose):
                    self.assertEqual(purpose, 'compaction')
                    return Reply({'content': 'Goal: fix. Done: inspected. Next: edit source.py.'})
                self.assertTrue(ctx.maintain(summarize))
                self.assertEqual(ctx.compactions, 1)
                self.assertEqual(ctx.groups[-1], latest)
                messages = ctx.messages()
                self.assertIn('Exact original task', json.dumps(messages))
                self.assertIn('Exact durable rules', json.dumps(messages))
                payload = ChatClient(ctx.config)._payload(messages, [])
                if api == 'responses':
                    self.assertIn(latest[0]['_raw_output'][0], payload['input'])
                    self.assertEqual(payload['input'][-1]['call_id'], 'tool3')
                elif api == 'anthropic':
                    self.assertEqual(payload['messages'][-2]['content'], latest[0]['_raw_content'])
                    self.assertEqual(payload['messages'][-1]['content'][0]['tool_use_id'], 'tool3')

    def test_failed_summary_does_not_replace_history(self):
        ctx = self.make_context()
        previous = copy.deepcopy(ctx.messages())
        self.assertFalse(ctx.maintain(lambda *args: Reply({'content': ''}), force=True))
        self.assertEqual(ctx.messages(), previous)
        self.assertEqual(ctx.compactions, 0)

    def test_repeated_compaction_preserves_rules_and_previous_summary(self):
        ctx = self.make_context()
        self.assertTrue(ctx.maintain(lambda *args: Reply({'content': 'First summary'})))
        for i in range(4):
            ctx.groups.append([{'role': 'assistant', 'content': 'more facts ' * 1000}])
        observed = []
        def summarize(messages, *_):
            observed.append(messages)
            return Reply({'content': 'Second summary'})
        self.assertTrue(ctx.maintain(summarize))
        self.assertIn('First summary', json.dumps(observed))
        self.assertIn('Exact durable rules', json.dumps(ctx.messages()))
        self.assertEqual(ctx.compactions, 2)

    def test_compaction_charges_shared_usage(self):
        from anybench.enhanced import Attempt
        client = Mock()
        client.complete.return_value = Reply({'content': 'Summary'}, 50, 12, 0.2)
        attempt = Attempt(client, self.sandbox, self.config, 1, self.artifacts)
        ctx = self.make_context()
        self.assertTrue(ctx.maintain(attempt.complete))
        self.assertEqual(attempt.remaining, 0)
        self.assertEqual(attempt.result.prompt_tokens, 50)
        self.assertEqual(attempt.result.completion_tokens, 12)
        self.assertEqual(attempt.result.model_calls_by_purpose['compaction'], 1)

    def test_context_overflow_has_one_bounded_recovery(self):
        from anybench.llm import ContextLimitError
        client = Mock()
        client.complete.side_effect = ContextLimitError('too long')
        result = self.run_loop(client)
        self.assertEqual(result.stop_reason, 'context_limit')
        self.assertEqual(client.complete.call_count, 1)

    def test_context_report_distinguishes_profiles(self):
        records = [RunRecord('c', 'same', 1, 'completed', 1),
                   RunRecord('c', 'same', 1, 'exhausted', 2, context_profile='enhanced',
                             context_window_tokens=200000, compactions=1,
                             model_calls_by_purpose={'main': 2, 'compaction': 1},
                             stop_reason='step_limit', verification_runs=[])]
        output = Path(self.temp.name) / 'report.html'
        report(records, output)
        document = output.read_text()
        self.assertIn('[legacy]', document)
        self.assertIn('[enhanced]', document)
        self.assertIn('200,000 tokens', document)
        self.assertIn('Compaction calls', document)
        self.assertIn('step_limit', document)
        self.assertIn('N/A', document)


    def test_pruning_retains_recent_full_outputs_and_recovery_handles(self):
        ctx = self.make_context()
        recent = copy.deepcopy(ctx.groups[-2:])
        for group in ctx.groups:
            message = group[-1]
            ctx.outputs[message['tool_call_id']] = ctx.artifacts.put(message['content'])
        self.assertTrue(ctx.maintain(lambda *args: self.fail('Pruning should suffice')))
        self.assertEqual(ctx.groups[-2:], recent)
        handle = ctx.outputs['tool0']
        self.assertIn(handle, ctx.groups[0][-1]['content'])
        self.assertIn('fact', ctx.artifacts.get(handle))
        self.assertEqual(ctx.compactions, 0)

    def test_native_output_usage_counts_towards_context_budget(self):
        ctx = self.make_context('responses')
        message = ctx.groups[-1][0]
        before = ctx.tokens()
        ctx.observe(ctx.messages(), Reply(message, 0, 12000))
        self.assertGreater(ctx.tokens(), before + 10000)

    def test_internal_symlink_uses_target_directory_instructions(self):
        (self.root / 'pkg').mkdir()
        (self.root / 'pkg' / 'AGENTS.md').write_text('Target directory convention')
        (self.root / 'pkg' / 'source').write_text('old')
        (self.root / 'alias').symlink_to('pkg/source')
        client = StubClient(self.call('Write', {'file_path': 'alias', 'content': 'new'}),
                            {'content': 'Review instructions'})
        result = self.run_loop(client)
        self.assertEqual(result.stop_reason, 'completed', result.error)
        self.assertIn('Target directory convention', json.dumps(client.calls[-1][0]))
        self.assertEqual((self.root / 'pkg' / 'source').read_text(), 'old')


class DockerEnhancedIntegrationTests(unittest.TestCase):
    def setUp(self):
        ready = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'], capture_output=True)
        image = subprocess.run(['docker', 'image', 'inspect', 'anybench-sandbox:latest'],
                               capture_output=True) if ready.returncode == 0 else ready
        if ready.returncode or image.returncode:
            if os.environ.get('ANYBENCH_REQUIRE_DOCKER') == '1':
                self.fail('Docker daemon or sandbox image unavailable')
            self.skipTest('Docker daemon or sandbox image unavailable')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        repo = Path(self.temp.name) / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        (repo / 'app.py').write_text('value = 1\n')
        (repo / 'AGENTS.md').write_text('Keep the API stable')
        commit(repo, 'Base')
        base = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
        self.case = Case('enhanced', str(repo), base, 'HIDDEN_TARGET', 'Set value to 2',
                         'HIDDEN_HINT', 'HIDDEN_GOLD',
                         "python -c 'import app; assert app.value == 2' # HIDDEN_EVALUATOR")
        self.config = ModelConfig('candidate', 'https://example.test/v1', 'model', 'KEY')

    def test_container_is_authoritative_and_final_tests_are_private(self):
        call = ContextOrchestrationTests.call
        client = StubClient(
            call('Run', {'command': "printf 'value = 2\\n' > app.py; printf 'created\\n' > generated.txt"}),
            call('Read', {'file_path': 'app.py'}, 'read'),
            call('Edit', {'file_path': 'generated.txt', 'line_range': [[1, 1]], 'content': 'edited\n'}, 'edit'),
            call('Run', {'command': "python -c 'import app; assert app.value == 2'"}, 'verify'),
            {'content': 'Implemented and verified'})
        with patch('anybench.runner.ChatClient', return_value=client):
            record = run_one(self.case, self.config)
        self.assertEqual(record.status, 'completed', record.error)
        self.assertTrue(record.test_passed)
        self.assertIn('+value = 2', record.diff)
        self.assertIn('+edited', record.diff)
        self.assertIn('1: value = 2', json.dumps(client.calls[2][0]))
        self.assertEqual(len(record.verification_runs), 2)
        self.assertEqual(record.model_calls_by_purpose['main'], 5)
        sent = json.dumps(client.calls)
        for hidden in ('HIDDEN_TARGET', 'HIDDEN_HINT', 'HIDDEN_GOLD', 'HIDDEN_EVALUATOR'):
            self.assertNotIn(hidden, sent)
        self.assertEqual((Path(self.case.repository) / 'app.py').read_text(), 'value = 1\n')
        self.assertNotIn('artifact', record.diff)

    def test_timeout_descendants_output_limits_and_isolation(self):
        with Sandbox(self.case) as sandbox:
            command = ("python -c \"import os,subprocess,time; "
                       "subprocess.Popen(['python','-c',"
                       "'import pathlib,time; time.sleep(2); pathlib.Path(\\\"escaped\\\").write_text(\\\"bad\\\")'],"
                       "start_new_session=True); time.sleep(20)\"")
            result = sandbox.enhanced_tool('Run', {'command': command, 'timeout_seconds': 1})
            self.assertTrue(result['timed_out'])
            self.assertEqual(result['exit_code'], 124)
            check = sandbox.enhanced_tool('Run', {'command': 'sleep 2; test ! -e escaped'})
            self.assertEqual(check['exit_code'], 0)
            limited = sandbox.enhanced_tool('Run', {'command': "python -c \"print('x'*2100000)\""})
            self.assertTrue(limited['truncated'])
            self.assertIn('output collection stopped', limited['output'])
            network = sandbox.enhanced_tool('Run', {'command':
                "python -c \"import socket; socket.create_connection(('1.1.1.1',443),timeout=1)\""})
            self.assertNotEqual(network['exit_code'], 0)
            sandbox.enhanced_tool('Run', {'command': 'ln -s /etc/passwd outside'})
            with self.assertRaises(ToolError):
                sandbox.enhanced_tool('Read', {'file_path': 'outside'})
            # Candidate Git operations cannot replace the trusted scoring baseline.
            sandbox.enhanced_tool('Run', {'command':
                "printf 'value = 2\\n' > app.py; git add .; git -c user.name=Agent -c user.email=a@b commit -qm altered"})
            # Remove the intentionally escaping symlink before archive collection.
            sandbox.enhanced_tool('Run', {'command': 'rm outside'})
            self.assertIn('+value = 2', sandbox.collect_container_diff(trusted_baseline=True))

    def test_exhausted_attempt_preserves_partial_diff(self):
        client = StubClient(ContextOrchestrationTests.call('Write', {'file_path': 'app.py', 'content': 'value = 2\n'}))
        with patch('anybench.runner.ChatClient', return_value=client):
            record = run_one(self.case, self.config, max_steps=1)
        self.assertEqual(record.status, 'exhausted', record.error)
        self.assertEqual(record.stop_reason, 'step_limit')
        self.assertIn('+value = 2', record.diff)
        self.assertTrue(record.test_passed)


if __name__ == '__main__':
    unittest.main()
