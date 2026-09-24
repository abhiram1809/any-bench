import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from anybench.evaluate import evaluate_patch, validate_test_commands
from anybench.model import Case
from anybench.outcomes import classify
from anybench.repository import OutputLimitError, git
from anybench.sandbox import Sandbox, prepare_snapshot
from support import LocalSandbox, commit


class BoundaryTests(unittest.TestCase):
    def test_validation_replays_the_recorded_reference_patch(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, 'init', '-q')
            (repo / 'file.txt').write_text('one\ntwo\n')
            base = commit(repo, 'Base')
            (repo / 'file.txt').write_text('one\nTWO\n')
            target = commit(repo, 'Fix')
            diff = git(repo, 'diff', '--binary', base, target)
            case = Case('c', str(repo), base, target, 'Fix', '', diff, 'grep -q TWO file.txt')
            with patch('anybench.evaluate.Sandbox', LocalSandbox):
                self.assertEqual(validate_test_commands([case]), [])
                case.gold_diff = ''
                self.assertIn('test fails on gold commit', validate_test_commands([case])[0])

    def test_git_output_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, 'init', '-q')
            (repo / 'large.txt').write_text('x' * 1000)
            commit(repo, 'Add large file')
            with self.assertRaises(OutputLimitError):
                git(repo, 'show', 'HEAD:large.txt', max_output_bytes=100)

    def test_committed_staged_untracked_and_deleted_changes_use_trusted_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'origin'
            repo.mkdir()
            git(repo, 'init', '-q')
            for name in ('committed', 'staged', 'unstaged', 'deleted'):
                (repo / name).write_text('before\n')
            base = commit(repo, 'Base')
            sandbox = Sandbox(Case('c', str(repo), base, base, 'Fix', '', ''))
            sandbox.root = root / 'seed'
            prepare_snapshot(str(repo), base, sandbox.root)
            sandbox.baseline = sandbox.root
            candidate = root / 'candidate'
            shutil.copytree(sandbox.root, candidate)
            (candidate / 'committed').write_text('after\n')
            commit(candidate, 'Candidate commits the fix')
            (candidate / 'staged').write_text('after\n')
            git(candidate, 'add', 'staged')
            (candidate / 'unstaged').write_text('after\n')
            (candidate / 'deleted').unlink()
            (candidate / 'new').write_text('created\n')
            marker = root / 'host-executed'
            # If candidate metadata reaches host git add/status, this benign hook leaves evidence.
            git(candidate, 'config', 'core.fsmonitor', f'!touch {marker}')
            content = io.BytesIO()
            with tarfile.open(fileobj=content, mode='w') as archive:
                archive.add(candidate, arcname='.')
            class Process:
                stdout = io.BytesIO(content.getvalue())
                def wait(self, *args, **kwargs): return 0
                def kill(self): pass
            original = subprocess.Popen
            def popen(argv, *args, **kwargs):
                return Process() if argv[0] == 'docker' else original(argv, *args, **kwargs)
            sandbox.container = 'fixture'
            with patch('anybench.sandbox.subprocess.Popen', side_effect=popen):
                diff = sandbox.collect_container_diff(trusted_baseline=False)
            for name in ('committed', 'staged', 'unstaged', 'deleted', 'new'):
                self.assertIn(name, diff)
            self.assertFalse(marker.exists())
            self.assertNotIn('host-executed', (sandbox.root / '.git/config').read_text())

    def test_missing_tests_setup_errors_zero_tests_and_timeouts_are_not_regressions(self):
        fixtures = [(2, "python: can't open file 'test_new.py': No such file", 'setup_error'),
                    (1, 'ModuleNotFoundError: dependency', 'setup_error'),
                    (0, 'Ran 0 tests\nOK', 'no_tests'), (5, 'no tests ran', 'no_tests'),
                    (124, '', 'timeout'), (137, '', 'resource_exhaustion'),
                    (1, 'AssertionError: wrong value', 'assertion_failure'),
                    (1, '1 failed in 0.1s', 'assertion_failure'),
                    (0, 'Ran 2 tests\nOK', 'passed'), (0, '', 'unverified')]
        for code, output, status in fixtures:
            with self.subTest(status=status, output=output):
                self.assertEqual(classify('python test.py', code, output).status, status)

    def test_candidate_test_tampering_is_removed_in_fresh_evaluation(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / 'repo'
            repo.mkdir()
            git(repo, 'init', '-q')
            (repo / 'app.py').write_text('value = 1\n')
            (repo / 'test_app.py').write_text('import app\nassert app.value == 2\n')
            base = commit(repo, 'Base')
            (repo / 'test_app.py').write_text('pass\n')
            diff = git(repo, 'diff', '--', 'test_app.py')
            case = Case('c', str(repo), base, base, 'Set value to two', '', '', 'python test_app.py')
            with patch('anybench.evaluate.Sandbox', LocalSandbox):
                outcome = evaluate_patch(case, diff)
            self.assertEqual(outcome.status, 'assertion_failure')

    def test_candidate_added_tests_and_collection_hooks_are_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / 'repo'
            repo.mkdir()
            git(repo, 'init', '-q')
            (repo / 'app.py').write_text('value = 1\n')
            base = commit(repo, 'Base')
            (repo / 'tests').mkdir()
            (repo / 'tests' / 'test_fake.py').write_text('def test_fake(): assert True\n')
            (repo / 'conftest.py').write_text('raise SystemExit(0)\n')
            git(repo, 'add', '--intent-to-add', 'tests/test_fake.py', 'conftest.py')
            diff = git(repo, 'diff', '--binary')
            case = Case('c', str(repo), base, base, 'Fix', '', '')
            with LocalSandbox(case, evaluation_patch=diff) as sandbox:
                self.assertFalse((sandbox.root / 'tests' / 'test_fake.py').exists())
                self.assertFalse((sandbox.root / 'conftest.py').exists())

    def test_evaluation_container_is_readonly_networkless_and_has_no_credentials(self):
        commands = []
        def run(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, 'container\n', '')
        case = Case('c', '/fake', 'a', 'b', 'Fix', '', '')
        with patch('anybench.sandbox.prepare_snapshot', side_effect=lambda a, b, root: root.mkdir()), \
             patch('anybench.sandbox._run', side_effect=run):
            with Sandbox(case, evaluation_patch='', evaluation_files={'test.py': 'assert True'}):
                pass
        command = commands[0]
        self.assertEqual(command[command.index('--network') + 1], 'none')
        self.assertTrue(any('dst=/repo,readonly' in arg for arg in command))
        self.assertTrue(any('dst=/evaluation,readonly' in arg for arg in command))
        self.assertFalse(any('API_KEY' in arg for arg in command))

    @unittest.skipUnless(shutil.which('node'), 'Node fixture runtime unavailable')
    def test_node_test_adapter_runs_independent_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / 'repo'
            repo.mkdir()
            git(repo, 'init', '-q')
            (repo / 'app.cjs').write_text('exports.add = (a,b) => a;\n')
            base = commit(repo, 'Base')
            (repo / 'app.cjs').write_text('exports.add = (a,b) => a+b;\n')
            diff = git(repo, 'diff', '--', 'app.cjs')
            case = Case('node', str(repo), base, base, 'Add both inputs', '', diff,
                        'node --test /evaluation/task.test.cjs')
            case._metadata = {'environment': {'adapter': 'node'}, 'evaluation_files': {
                'task.test.cjs': "const test=require('node:test'); const assert=require('node:assert/strict'); "
                "const {add}=require(process.cwd()+'/app.cjs'); test('adds',()=>assert.equal(add(2,3),5));"}}
            with patch('anybench.evaluate.Sandbox', LocalSandbox):
                before = evaluate_patch(case, '')
                after = evaluate_patch(case, diff)
            self.assertEqual(before.status, 'assertion_failure', before.output)
            self.assertEqual(after.status, 'passed', after.output)

    def test_real_docker_evaluation_rejects_asset_writes(self):
        ready = subprocess.run(['docker', 'image', 'inspect', 'anybench-sandbox:latest'], capture_output=True)
        if ready.returncode:
            if os.environ.get('ANYBENCH_REQUIRE_DOCKER') == '1':
                self.fail('Docker integration prerequisites unavailable')
            self.skipTest('Docker integration prerequisites unavailable')
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / 'repo'
            repo.mkdir()
            git(repo, 'init', '-q')
            (repo / 'app.py').write_text('value = 1\n')
            base = commit(repo, 'Base')
            case = Case('c', str(repo), base, base, 'Fix', '', '')
            with Sandbox(case, evaluation_patch='', evaluation_files={'test.py': 'assert True'}) as sandbox:
                result = sandbox.command(['sh', '-c', 'echo changed > /evaluation/test.py'])
                self.assertNotEqual(result.returncode, 0)
                result = sandbox.command(['sh', '-c', 'echo changed > /repo/app.py'])
                self.assertNotEqual(result.returncode, 0)
