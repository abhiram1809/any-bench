import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anybench.author import commit_contexts, import_annotations
from anybench.cli import _summary, main
from anybench.model import Case, RunRecord, read_cases, read_jsonl, write_cases, write_jsonl
from anybench.skill_install import install_skill
from anybench.workflow import manifest_path, read_complete_jsonl
from anybench.llm import Reply


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo, message):
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.com", "commit", "-qm", message], check=True)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "app.py").write_text("value = 1\n")
        commit(self.repo, "Initial")
        self.base = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "app.py").write_text("value = 2\n")
        commit(self.repo, "Set value to two")
        self.target = git(self.repo, "rev-parse", "HEAD")

    def _models(self):
        models = self.root / "models.json"
        models.write_text(json.dumps([{"name": "candidate", "base_url": "https://example.test/v1",
                                       "model": "mock", "api_key_env": "ANYBENCH_TEST_KEY"}]))
        return models

    def test_host_author_bridge_uses_git_and_refuses_patch_replacement(self):
        contexts = commit_contexts([str(self.repo)], 2)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]["base_commit"], self.base)
        annotation = {"case_id": contexts[0]["case_id"], "eligible": True,
                      "problem_statement": "Set value to two", "hint": "",
                      "test_command": "python -c 'import app; assert app.value == 2'",
                      "external_validation": False, "external_validation_reason": ""}
        cases, decisions = import_annotations(contexts, [annotation])
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].base_commit, self.base)
        self.assertEqual(cases[0].gold_diff,
                         subprocess.check_output(["git", "-C", str(self.repo), "diff",
                                                  "--no-ext-diff", "--find-renames",
                                                  self.base, self.target, "--"], text=True))
        self.assertTrue(decisions[0]["accepted"])
        with self.assertRaisesRegex(ValueError, "may not replace Git fields"):
            import_annotations(contexts, [{**annotation, "gold_diff": "fake"}])

    def test_skill_install_and_local_edit_protection(self):
        destination, status = install_skill("claude", "project", project=self.root)
        self.assertEqual(status, "installed")
        self.assertTrue((destination / "SKILL.md").exists())
        self.assertEqual(install_skill("claude", "project", project=self.root)[1],
                         "already current")
        with (destination / "SKILL.md").open("a") as stream:
            stream.write("\nlocal change\n")
        with self.assertRaisesRegex(ValueError, "local changes"):
            install_skill("claude", "project", project=self.root)
        install_skill("claude", "project", project=self.root, overwrite=True)
        self.assertNotIn("local change", (destination / "SKILL.md").read_text())
        portable, _ = install_skill("portable", "user", home=self.root)
        self.assertEqual(portable, self.root / ".agents/skills/anybench")

    def test_resume_run_skips_recorded_error_and_rejects_drift(self):
        cases = self.root / "cases.csv"
        write_cases(cases, [Case("c", str(self.repo), self.base, self.target, "Fix", "", "")])
        models = self._models()
        output = self.root / "attempts.jsonl"
        argv = ["run", str(cases), "--models", str(models), "--attempts", "2",
                "--output", str(output)]
        def interrupted(*args):
            args[7](RunRecord("c", "candidate", 1, "error", 1))
            raise RuntimeError("interrupted")
        with patch.dict(os.environ, {"ANYBENCH_TEST_KEY": "present"}), \
             patch("anybench.cli.preflight_run"), patch("anybench.cli._image_ids",
                  return_value={"anybench-sandbox:latest": "sha256:fixed"}), \
             patch("anybench.cli.run_sweep", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                main(argv)
        self.assertTrue(manifest_path(output).exists())
        self.assertEqual(len(read_jsonl(output)), 1)
        def resumed(*args):
            self.assertEqual(args[10], {("c", "candidate", 2, 1)})
            record = RunRecord("c", "candidate", 2, "completed", 1)
            args[7](record)
            return [record]
        with patch.dict(os.environ, {"ANYBENCH_TEST_KEY": "present"}), \
             patch("anybench.cli.preflight_run"), patch("anybench.cli._image_ids",
                  return_value={"anybench-sandbox:latest": "sha256:fixed"}), \
             patch("anybench.cli.run_sweep", side_effect=resumed):
            main(argv + ["--resume"])
        self.assertEqual([r.status for r in read_jsonl(output)], ["error", "completed"])
        with patch.dict(os.environ, {"ANYBENCH_TEST_KEY": "present"}), \
             patch("anybench.cli.preflight_run"), patch("anybench.cli._image_ids",
                  return_value={"anybench-sandbox:latest": "sha256:changed"}), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(argv + ["--resume"])
        self.assertEqual(len(read_jsonl(output)), 2)

    def test_run_refuses_explicit_judge_role_before_execution(self):
        cases = self.root / "cases.csv"
        write_cases(cases, [Case("c", str(self.repo), self.base, self.target, "Fix", "", "")])
        models = self._models()
        data = json.loads(models.read_text())
        data[0]["role"] = "judge"
        models.write_text(json.dumps(data))
        output = self.root / "attempts.jsonl"
        with contextlib.redirect_stderr(io.StringIO()), \
             patch("anybench.cli.preflight_run", side_effect=AssertionError("should not run")):
            with self.assertRaises(SystemExit):
                main(["run", str(cases), "--models", str(models), "--output", str(output)])
        self.assertFalse(output.exists())

    def test_summary_counts_exhaustion_as_failed_local_attempt(self):
        case = Case("c", str(self.repo), self.base, self.target, "Fix", "", "", "check")
        records = [RunRecord("c", "candidate", 1, "completed", 1, test_passed=True),
                   RunRecord("c", "candidate", 2, "exhausted", 1, test_passed=True)]
        group = _summary(records, [case])["groups"][0]
        self.assertEqual(group["test_passed"], 1)
        self.assertEqual(group["test_failed"], 1)
        self.assertEqual(group["test_accuracy"], 0.5)

    def test_resume_jsonl_keeps_complete_unterminated_record(self):
        path = self.root / "records.jsonl"
        path.write_text('{"id":1}\n{"id":2}')
        self.assertEqual([item["id"] for item in read_complete_jsonl(path, lambda x: x, repair=True)],
                         [1, 2])
        self.assertTrue(path.read_bytes().endswith(b"\n"))
        path.write_text('{"id":1}\n{"id":')
        self.assertEqual([item["id"] for item in read_complete_jsonl(path, lambda x: x, repair=True)], [1])

    def test_evaluate_resume_only_missing_judge_result(self):
        cases = self.root / "cases.csv"
        write_cases(cases, [Case("c", str(self.repo), self.base, self.target, "Fix", "", "")])
        attempts = self.root / "attempts.jsonl"
        write_jsonl(attempts, [RunRecord("c", "candidate", 1, "completed", 1),
                               RunRecord("c", "candidate", 2, "completed", 1)])
        models = self._models()
        output = self.root / "scored.jsonl"
        argv = ["evaluate", str(cases), str(attempts), "--models", str(models),
                "--judge", "candidate", "--output", str(output)]
        calls = []
        def interrupted(records, _cases, _client):
            calls.append(records[0].attempt)
            if len(calls) == 2:
                raise RuntimeError("interrupted")
            records[0].judge_score = 1.0
            return records
        with patch.dict(os.environ, {"ANYBENCH_TEST_KEY": "present"}), \
             patch("anybench.cli.evaluate", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                main(argv)
        self.assertEqual(len(read_jsonl(output)), 1)
        def finish(records, _cases, _client):
            self.assertEqual(records[0].attempt, 2)
            records[0].judge_reason = "Judge error: unavailable"
            return records
        with patch.dict(os.environ, {"ANYBENCH_TEST_KEY": "present"}), \
             patch("anybench.cli.evaluate", side_effect=finish):
            main(argv + ["--resume"])
        self.assertEqual(len(read_jsonl(output)), 2)
        with patch.dict(os.environ, {"ANYBENCH_TEST_KEY": "present"}), \
             patch("anybench.cli.evaluate", side_effect=AssertionError("should skip")):
            main(argv + ["--resume"])

    def test_build_journal_skips_rejections_and_recorded_errors(self):
        (self.repo / "app.py").write_text("value = 3\n")
        commit(self.repo, "Set value to three")
        models = self._models()
        output = self.root / "builder-cases.csv"
        argv = ["build", str(self.repo), "--models", str(models),
                "--builder", "candidate", "--commits", "3", "--max-cases", "1",
                "--output", str(output)]
        class Client:
            def __init__(self, *replies):
                self.replies = list(replies)
                self.calls = 0

            def complete(self, messages, tools=None):
                self.calls += 1
                item = self.replies.pop(0)
                if isinstance(item, Exception):
                    raise item
                return Reply({"content": json.dumps(item)})

        first = Client({"eligible": False}, RuntimeError("interrupted"))
        with patch.dict(os.environ, {"ANYBENCH_TEST_KEY": "present"}), \
             patch("anybench.cli.ChatClient", return_value=first):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                main(argv)
        journal = output.with_name(output.name + ".journal.jsonl")
        self.assertEqual(len(journal.read_text().splitlines()), 1)
        second = Client({"eligible": True, "problem_statement": "Set value to two",
                         "hint": "", "test_command": "", "external_validation": False,
                         "external_validation_reason": ""})
        with patch.dict(os.environ, {"ANYBENCH_TEST_KEY": "present"}), \
             patch("anybench.cli.ChatClient", return_value=second):
            main(argv + ["--resume"])
        self.assertEqual(second.calls, 0)
        self.assertEqual(read_cases(output), [])

    def test_structured_validation_selects_verified_subset(self):
        cases = self.root / "cases.csv"
        rows = [Case("a", str(self.repo), self.base, self.target, "Fix", "", "", "check"),
                Case("b", str(self.repo), self.base, self.target, "Fix", "", "")]
        write_cases(cases, rows)
        results = self.root / "validation.json"
        verified = self.root / "verified.csv"
        with patch("anybench.cli.validate_dataset", return_value=[]), \
             patch("anybench.cli.validation_results", return_value=[
                 {"case_id": "a", "status": "verified", "reason": ""},
                 {"case_id": "b", "status": "skipped", "reason": "no local test command"}]):
            main(["validate", str(cases), "--check-tests", "--json-output", str(results),
                  "--verified-output", str(verified)])
        self.assertEqual([case.case_id for case in read_cases(verified)], ["a"])
        self.assertEqual(json.loads(results.read_text())["skipped"], 1)

    def test_structured_validation_continues_with_good_subset(self):
        cases = self.root / "cases.csv"
        rows = [Case("a", str(self.repo), self.base, self.target, "Fix", "", "", "check"),
                Case("b", str(self.repo), self.base, self.target, "Fix", "", "", "bad")]
        write_cases(cases, rows)
        verified = self.root / "verified.csv"
        with patch("anybench.cli.validate_dataset", return_value=[]), \
             patch("anybench.cli.validation_results", return_value=[
                 {"case_id": "a", "status": "verified", "reason": ""},
                 {"case_id": "b", "status": "invalid", "reason": "gold failed"}]), \
             contextlib.redirect_stderr(io.StringIO()):
            main(["validate", str(cases), "--check-tests", "--verified-output", str(verified)])
        self.assertEqual([case.case_id for case in read_cases(verified)], ["a"])

    def test_docker_guided_pipeline_from_unrelated_directory(self):
        ready = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                               capture_output=True)
        image = subprocess.run(["docker", "image", "inspect", "anybench-sandbox:latest"],
                               capture_output=True)
        if ready.returncode or image.returncode:
            if os.environ.get("ANYBENCH_REQUIRE_DOCKER") == "1":
                self.fail("Docker daemon or sandbox image unavailable")
            self.skipTest("Docker daemon or sandbox image unavailable")
        session = self.root / "session"
        contexts = session / "contexts.jsonl"
        annotations = session / "annotations.jsonl"
        cases = session / "cases.csv"
        verified = session / "verified.csv"
        validation = session / "validation.json"
        attempts = session / "attempts.jsonl"
        report = session / "report.html"
        old_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            main(["prepare", str(self.repo), "--commits", "2", "--output", str(contexts)])
            self.assertEqual(session.stat().st_mode & 0o777, 0o700)
            context = json.loads(contexts.read_text().splitlines()[0])
            annotations.write_text(json.dumps({"case_id": context["case_id"],
                "eligible": True, "problem_statement": "Set value to two", "hint": "",
                "test_command": "python -c 'import app; assert app.value == 2'",
                "external_validation": False, "external_validation_reason": ""}) + "\n")
            main(["import", str(contexts), str(annotations), "--output", str(cases)])
            main(["validate", str(cases), "--check-tests", "--json-output", str(validation),
                  "--verified-output", str(verified)])
            self.assertEqual(json.loads(validation.read_text())["verified"], 1)
            self.assertEqual(len(read_cases(verified)), 1)
            models = self._models()
            class Candidate:
                def __init__(self):
                    self.replies = [Reply({"role": "assistant", "content": None,
                        "tool_calls": [{"id": "1", "type": "function", "function": {
                            "name": "Write", "arguments": json.dumps({
                                "file_path": "app.py", "content": "value = 2\n"})}}]}),
                        Reply({"role": "assistant", "content": "Done"})]
                def complete(self, messages, tools=None):
                    return self.replies.pop(0)
            with patch.dict(os.environ, {"ANYBENCH_TEST_KEY": "present"}), \
                 patch("anybench.runner.ChatClient", return_value=Candidate()):
                main(["run", str(verified), "--models", str(models), "--max-steps", "3",
                      "--output", str(attempts)])
            record = read_jsonl(attempts)[0]
            self.assertEqual(record.status, "completed", record.error)
            self.assertTrue(record.test_passed)
            main(["report", str(attempts), "--output", str(report)])
            self.assertIn("candidate / enhanced", report.read_text())
            self.assertIn("100.0%", report.read_text())
        finally:
            os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
