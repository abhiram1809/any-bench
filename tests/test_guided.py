from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from anybench.evaluate import validation_results
from anybench.guided import _build_image, _dockerfile, _prepare_image, _role_files, configure, start
from anybench.model import Case, read_cases, write_cases
from anybench.sandbox import Sandbox
from anybench.workflow import private_json


@contextmanager
def temporary_cwd():
    original = Path.cwd()
    with tempfile.TemporaryDirectory() as temporary:
        os.chdir(temporary)
        try:
            yield Path(temporary)
        finally:
            os.chdir(original)


def model(name: str, role: str, **credential) -> dict:
    return {"name": name, "role": role, "model": "test-model",
            "base_url": "https://example.test/v1", **credential}


class GuidedTests(unittest.TestCase):
    def test_wizard_saves_private_config_and_hidden_literal_key(self):
        with temporary_cwd():
            subprocess.run(["git", "init", "-q"], check=True)
            answers = iter(["", "", "", "builder-id", "save",
                            "", "", "", "candidate-id", "env", "CANDIDATE_KEY", "n", "n"])
            with patch("builtins.input", side_effect=lambda _: next(answers)), \
                 patch("anybench.guided.getpass", return_value="private-builder-key"):
                saved = configure(repositories=["/repo"])
            path = Path(".anybench/config.json")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual((path.parent / ".gitignore").read_text(), "*\n!.gitignore\n")
            self.assertEqual(subprocess.run(["git", "check-ignore", "-q", str(path)]).returncode, 0)
            self.assertEqual(saved["models"][0]["api_key"], "private-builder-key")
            self.assertEqual(saved["models"][1]["api_key_env"], "CANDIDATE_KEY")

    def test_role_files_never_contain_literal_keys(self):
        with temporary_cwd() as root:
            config = {"models": [model("builder", "builder", api_key="literal-secret"),
                                 model("candidate", "candidate", api_key_env="CANDIDATE_KEY")]}
            with patch.dict(os.environ, {"CANDIDATE_KEY": "candidate-secret"}):
                paths = _role_files(config, root / "session")
                for path in paths.values():
                    content = path.read_text()
                    self.assertNotIn("literal-secret", content)
                    self.assertNotIn("candidate-secret", content)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(os.environ["ANYBENCH_SESSION_KEY_0"], "literal-secret")

    def test_start_uses_all_verified_cases_across_repositories_and_optional_judge(self):
        with temporary_cwd():
            config = {"repositories": ["repo-a", "repo-b"],
                      "models": [model("builder", "builder", api_key_env="BUILDER_KEY"),
                                 model("candidate", "candidate", api_key_env="CANDIDATE_KEY"),
                                 model("judge", "judge", api_key_env="JUDGE_KEY")]}
            private_json(Path(".anybench/config.json"), config)
            cases = [Case(f"case-{index}", source, "a" * 40, "b" * 40,
                          "Fix behavior", "", "diff", "true")
                     for index, source in enumerate(("repo-a", "repo-a", "repo-b"))]
            calls = []

            def fake_main(argv):
                calls.append(argv)
                output = Path(argv[argv.index("--output") + 1])
                if argv[0] == "build":
                    write_cases(output, cases)
                elif argv[0] == "report":
                    output.write_text("<html>report</html>")
                else:
                    output.write_text("")

            def fake_validation(items, image, image_map):
                return [{"case_id": item.case_id, "status": "verified", "reason": ""}
                        for item in items]

            with patch.dict(os.environ, {"BUILDER_KEY": "b", "CANDIDATE_KEY": "c", "JUDGE_KEY": "j"}), \
                 patch("builtins.input", return_value="y"), \
                 patch("anybench.guided._prerequisites"), \
                 patch("anybench.guided._prepare_image", return_value=("test-image", "")), \
                 patch("anybench.guided.validation_results", side_effect=fake_validation), \
                 patch("anybench.cli.main", side_effect=fake_main):
                path = start([], max_problems=None)
                capped = start([], max_problems=2)
                calls.clear()
                resumed = start([], resume=path.parent)
            self.assertTrue(path.exists())
            self.assertEqual(resumed.resolve(), path.resolve())
            self.assertEqual(len(read_cases(path.parent / "verified.csv")), 3)
            self.assertEqual(len(read_cases(capped.parent / "verified.csv")), 2)
            self.assertEqual({item["repository"] for item in
                              json.loads((path.parent / "validation.json").read_text())},
                             {"repo-a", "repo-b"})
            self.assertEqual([call[0] for call in calls], ["run", "evaluate", "report"])
            self.assertIn("--resume", calls[0])
            self.assertIn("--resume", calls[1])
            self.assertIn("--image-map", calls[0])

    def test_start_reports_zero_verified_without_running_candidates(self):
        with temporary_cwd():
            config = {"repositories": ["repo-a"],
                      "models": [model("builder", "builder", api_key="literal-builder-key"),
                                 model("candidate", "candidate", api_key_env="CANDIDATE_KEY")]}
            private_json(Path(".anybench/config.json"), config)
            calls = []

            def fake_main(argv):
                calls.append(argv[0])
                write_cases(Path(argv[argv.index("--output") + 1]), [])

            with patch.dict(os.environ, {"CANDIDATE_KEY": "c"}), \
                 patch("builtins.input", return_value="y"), \
                 patch("anybench.guided._prerequisites"), \
                 patch("anybench.cli.main", side_effect=fake_main):
                report = start([])
            self.assertEqual(calls, ["build"])
            self.assertIn("No verified problems", report.read_text())
            for file in report.parent.iterdir():
                if file.is_file():
                    self.assertNotIn("literal-builder-key", file.read_text())

    def test_image_build_has_one_paid_repair_and_reuses_saved_recipe(self):
        with temporary_cwd() as root:
            session = root / "session"
            session.mkdir()
            case = Case("case", "repo", "a" * 40, "b" * 40, "Fix", "", "diff")
            builder = model("builder", "builder", api_key_env="BUILDER_KEY")
            from anybench.model import ModelConfig
            with patch.dict(os.environ, {"BUILDER_KEY": "secret"}), \
                 patch("anybench.guided._case_context", return_value="manifest"), \
                 patch("anybench.guided._recipe", side_effect=[["true"], ["true"]]) as recipe, \
                 patch("anybench.guided.ChatClient"), \
                 patch("anybench.guided._build_image", side_effect=[(False, "missing package"),
                                                                    (True, "built")]) as build, \
                 patch("anybench.guided._image_id", return_value="sha256:abc"):
                image, error = _prepare_image("repo", [case], ModelConfig(**builder), session)
            self.assertTrue(image.startswith("anybench-guided:"))
            self.assertEqual(error, "")
            self.assertEqual(recipe.call_count, 2)
            self.assertEqual(build.call_count, 2)
            state = next(session.glob("image-*.json"))
            self.assertTrue(json.loads(state.read_text())["repaired"])
            self.assertNotIn("secret", next(session.glob("*.Dockerfile")).read_text())

    def test_pending_environment_call_is_not_repaid_on_resume(self):
        with temporary_cwd() as root:
            session = root / "session"
            session.mkdir()
            case = Case("case", "repo", "a" * 40, "b" * 40, "Fix", "", "diff")
            from anybench.model import ModelConfig
            builder = ModelConfig(**model("builder", "builder", api_key_env="BUILDER_KEY"))
            from anybench.workflow import fingerprint
            private_json(session / f"image-{fingerprint('repo')[:12]}.json",
                         {"phase": "planning_pending"})
            with patch("anybench.guided.ChatClient") as client:
                with self.assertRaisesRegex(ValueError, "unknown billing state"):
                    _prepare_image("repo", [case], builder, session)
            client.assert_not_called()

    @unittest.skipUnless(os.environ.get("ANYBENCH_REQUIRE_DOCKER") == "1",
                         "Set ANYBENCH_REQUIRE_DOCKER=1 for real language integration")
    def test_real_generated_image_verifies_python_node_and_go(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recipe = root / "Dockerfile"
            recipe.write_text(_dockerfile([
                "apt-get update && apt-get install -y --no-install-recommends nodejs golang-go "
                "&& rm -rf /var/lib/apt/lists/*"
            ]))
            image = "anybench-guided-integration:local"
            ok, output = _build_image(image, recipe)
            self.assertTrue(ok, output[-2000:])
            cases = []
            examples = [
                ("python", "app.py", "value = 1\n", "value = 2\n",
                 "python -c 'import app; assert app.value == 2'"),
                ("node", "app.js", "module.exports = 1;\n", "module.exports = 2;\n",
                 "node -e 'const assert=require(\"node:assert\"); "
                 "assert.equal(require(\"./app.js\"),2)'"),
                ("go", "main.go", 'package main\nimport "fmt"\nfunc main() {fmt.Println("before")}\n',
                 'package main\nimport "fmt"\nfunc main() {fmt.Println("after")}\n',
                 'test "$(go run main.go)" = after'),
            ]
            for language, filename, before, after, command in examples:
                repo = root / language
                repo.mkdir()
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
                (repo / filename).write_text(before)
                subprocess.run(["git", "-C", str(repo), "add", filename], check=True)
                subprocess.run(["git", "-C", str(repo), "-c", "user.name=AnyBench", "-c",
                                "user.email=test@example.com", "commit", "-qm", "base"], check=True)
                base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
                (repo / filename).write_text(after)
                subprocess.run(["git", "-C", str(repo), "add", filename], check=True)
                subprocess.run(["git", "-C", str(repo), "-c", "user.name=AnyBench", "-c",
                                "user.email=test@example.com", "commit", "-qm", "fix"], check=True)
                target = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
                diff = subprocess.check_output(["git", "-C", str(repo), "diff", base, target], text=True)
                cases.append(Case(language, str(repo), base, target, "Fix behavior", "", diff, command))
            outcomes = validation_results(cases, image)
            self.assertEqual([item["status"] for item in outcomes], ["verified"] * 3, outcomes)
            with Sandbox(cases[-1], image=image) as sandbox:
                result = sandbox.command(["sh", "-lc", "go run main.go"], timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stdout.strip(), "before")

    @unittest.skipUnless(os.environ.get("ANYBENCH_REQUIRE_DOCKER") == "1",
                         "Set ANYBENCH_REQUIRE_DOCKER=1 for real guided integration")
    def test_real_start_to_report_with_local_model_endpoint(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(size))
                prompt = json.dumps(request.get("messages", []))
                if "Create a coding benchmark task" in prompt:
                    content = json.dumps({"eligible": True, "problem_statement": "Set app.value to 2",
                                          "hint": "", "test_command":
                                          "python -c 'import app; assert app.value == 2'",
                                          "external_validation": False,
                                          "external_validation_reason": ""})
                elif "Prepare an offline-capable Docker test environment" in prompt:
                    content = '{"commands": []}'
                elif "Evaluate whether the candidate patch solves the task" in prompt:
                    content = '{"score": 0, "reason": "No patch"}'
                else:
                    content = "No patch produced"
                response = json.dumps({"choices": [{"message": {"role": "assistant",
                                                               "content": content},
                                                     "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with temporary_cwd() as root:
                repo = root / "sample"
                repo.mkdir()
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
                (repo / "app.py").write_text("value = 1\n")
                subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
                subprocess.run(["git", "-C", str(repo), "-c", "user.name=AnyBench", "-c",
                                "user.email=test@example.com", "commit", "-qm", "base"], check=True)
                (repo / "app.py").write_text("value = 2\n")
                subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
                subprocess.run(["git", "-C", str(repo), "-c", "user.name=AnyBench", "-c",
                                "user.email=test@example.com", "commit", "-qm", "fix value"], check=True)
                endpoint = f"http://127.0.0.1:{server.server_port}/v1"
                config = {"repositories": [str(repo)], "models": [
                    {**model("builder", "builder", api_key_env="GUIDED_TEST_KEY"),
                     "base_url": endpoint},
                    {**model("candidate", "candidate", api_key_env="GUIDED_TEST_KEY"),
                     "base_url": endpoint},
                    {**model("judge", "judge", api_key_env="GUIDED_TEST_KEY"),
                     "base_url": endpoint}]}
                private_json(Path(".anybench/config.json"), config)
                with patch.dict(os.environ, {"GUIDED_TEST_KEY": "test-only-key"}), \
                     patch("builtins.input", return_value="y"):
                    report = start([], commits=2)
                self.assertTrue(report.exists())
                self.assertEqual(len(read_cases(report.parent / "verified.csv")), 1)
                self.assertTrue((report.parent / "scored.jsonl").exists())
                self.assertIn("AnyBench", report.read_text())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
