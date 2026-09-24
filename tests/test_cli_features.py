import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from anybench.cli import main
from anybench.llm import Reply
from anybench.model import read_cases, read_jsonl, write_cases
from support import LocalSandbox
import test_grouped


class FeatureWorkflowTests(unittest.TestCase):
    setUp = test_grouped.GroupedHistoryTests.setUp

    def test_grouped_cli_to_verified_run_report_and_comparison(self):
        root = Path(self.temp.name)
        contexts, annotations = root / 'contexts.jsonl', root / 'annotations.jsonl'
        cases, verified = root / 'cases.csv', root / 'verified.csv'
        attempts, page, metrics = root / 'attempts.jsonl', root / 'report.html', root / 'metrics.json'
        annotations.write_text(json.dumps(self.proposal) + '\n')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            main(['prepare', str(self.repo), '--grouped', '--output', str(contexts)])
            main(['import', str(contexts), str(annotations), '--grouped', '--output', str(cases)])
            with patch('anybench.evaluate.Sandbox', LocalSandbox):
                main(['validate', str(cases), '--check-tests', '--verified-output', str(verified)])
            self.assertEqual(len(read_cases(verified)), 1)
            config = root / 'models.json'
            config.write_text(json.dumps([{'name': 'fixture', 'model': 'fixture', 'api_key_env': 'FIXTURE_KEY',
                                          'base_url': 'https://fixture.invalid', 'context_profile': 'legacy'}]))
            selected = read_cases(verified)
            selected[0]._metadata['validation']['image_id'] = 'sha256:previous'
            write_cases(verified, selected)
            with patch('anybench.cli.preflight_run'), \
                 patch('anybench.cli._image_ids', return_value={'anybench-sandbox:latest': 'sha256:fixture'}):
                with self.assertRaises(SystemExit):
                    main(['run', str(verified), '--models', str(config), '--output', str(attempts)])
            selected[0]._metadata['validation']['image_id'] = None
            write_cases(verified, selected)
            class Client:
                def __init__(self): self.calls = 0
                def complete(self, messages, tools=None):
                    self.calls += 1
                    if self.calls == 1:
                        return Reply({'role': 'assistant', 'content': None, 'tool_calls': [
                            {'id': 'fix', 'function': {'name': 'Write', 'arguments': json.dumps({
                                'file_path': 'app.py', 'content': 'def clean(value):\n    return value.strip().lower()\n'})}}]})
                    return Reply({'role': 'assistant', 'content': 'Done'})
            with patch('anybench.runner.Sandbox', LocalSandbox), patch('anybench.evaluate.Sandbox', LocalSandbox), \
                 patch('anybench.runner.ChatClient', return_value=Client()), patch('anybench.cli.preflight_run'), \
                 patch('anybench.cli._image_ids', return_value={'anybench-sandbox:latest': 'sha256:fixture'}), \
                 patch.dict(os.environ, {'FIXTURE_KEY': 'secret'}):
                main(['run', str(verified), '--models', str(config), '--output', str(attempts)])
            record = read_jsonl(attempts)[0]
            self.assertEqual(record.status, 'completed', record.error)
            self.assertTrue(record.test_passed)
            self.assertNotIn('unrelated.txt', record.diff)
            main(['report', str(attempts), '--output', str(page), '--metrics-output', str(metrics)])
            group = json.loads(metrics.read_text())['groups'][0]
            self.assertTrue(group['complete'])
            self.assertEqual(group['verified_test_accuracy'], 1)
            main(['compare', str(attempts), str(attempts)])
            main(['inspect', str(verified)])
        self.assertIn('reconstructed groups', page.read_text())

    def test_grouped_input_alias_is_rejected_before_overwrite(self):
        path = Path(self.temp.name) / 'input.jsonl'
        path.write_text('keep me')
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main(['import', str(path), str(path), '--grouped', '--output', str(path), '--overwrite'])
        self.assertEqual(path.read_text(), 'keep me')
