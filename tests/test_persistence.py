from pathlib import Path
import tempfile
import unittest

from anybench.commands import expand_config, invoke
from anybench.workflow import output_lock, read_complete_jsonl


class PersistenceTests(unittest.TestCase):
    def test_read_only_jsonl_does_not_repair_implicitly(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'results.jsonl'
            path.write_text('{"a": 1}')
            self.assertEqual(read_complete_jsonl(path, lambda x: x), [{'a': 1}])
            self.assertEqual(path.read_text(), '{"a": 1}')
            path.write_text('{"a": 1}\n{"a":')
            with self.assertRaisesRegex(ValueError, 'Torn'):
                read_complete_jsonl(path, lambda x: x)
            self.assertEqual(read_complete_jsonl(path, lambda x: x, repair=True), [{'a': 1}])
            self.assertEqual(path.read_text(), '{"a": 1}\n')

    def test_second_writer_cannot_acquire_output(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'output'
            with output_lock(path):
                with self.assertRaisesRegex(ValueError, 'locked'):
                    with output_lock(path):
                        self.fail('second lock acquired')
            with output_lock(path):
                pass

    def test_reserved_sidecar_cannot_be_an_input(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'results.jsonl'
            with self.assertRaisesRegex(ValueError, 'reserved'):
                invoke(['run', str(output) + '.manifest.json', '--output', str(output)],
                       lambda args: self.fail('handler invoked'))

    def test_toml_defaults_resolve_paths_and_cli_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'experiment.toml'
            path.write_text('[run]\ndataset="cases.csv"\nmodels="models.json"\nconcurrency=2\noutput="out.jsonl"\n')
            args = expand_config(['run', '--config', str(path), '--concurrency', '4'])
            self.assertEqual(args[1], str(Path(temp) / 'cases.csv'))
            self.assertEqual(args[args.index('--concurrency') + 1], '4')
            self.assertEqual(args.count('--concurrency'), 1)
            args = expand_config(['run', 'different.csv', '--config', str(path)])
            self.assertNotIn(str(Path(temp) / 'cases.csv'), args)
