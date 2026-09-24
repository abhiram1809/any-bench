import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from anybench.dataset import validate_dataset
from anybench.evaluate import validation_results
from anybench.grouped import discover, import_groups, reconstruct
from anybench.metadata import metadata_path
from anybench.model import read_cases, write_cases
from anybench.repository import git
from support import LocalSandbox, commit


class GroupedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'project'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        (self.repo / 'app.py').write_text('def clean(value):\n    return value\n')
        (self.repo / 'test_existing.py').write_text(
            'import unittest\nfrom app import clean\nclass Existing(unittest.TestCase):\n'
            '    def test_plain(self): self.assertEqual(clean("hello"), "hello")\n'
            'if __name__ == "__main__": unittest.main()\n')
        commit(self.repo, 'Initial')
        (self.repo / 'anchor.txt').write_text('anchor\n')
        self.base = commit(self.repo, 'Prepare release')
        (self.repo / 'app.py').write_text('def clean(value):\n    return value.strip()\n')
        self.first = commit(self.repo, 'Fix #42: remove surrounding spaces')
        (self.repo / 'unrelated.txt').write_text('unrelated feature\n')
        self.unrelated = commit(self.repo, 'Another feature')
        (self.repo / 'app.py').write_text('def clean(value):\n    return value.strip().lower()\n')
        self.last = commit(self.repo, 'Complete #42: normalize case too')
        self.contexts = discover([str(self.repo)])
        self.proposal = {'repository': str(self.repo), 'selected_commits': [self.first, self.last],
            'evidence': [{'commit': c, 'paths': ['app.py'], 'reason': 'Partial and final fixes for #42'}
                         for c in [self.first, self.last]],
            'problem_statement': 'Normalize surrounding whitespace and case.',
            'evaluation_files': {'test_bug.py': 'import unittest\nfrom app import clean\n'
                'class Bug(unittest.TestCase):\n'
                '    def test_bug(self): self.assertEqual(clean(" Hello "), "hello")\n'
                'if __name__ == "__main__": unittest.main()\n'},
            'evaluation_command': 'python /evaluation/test_bug.py',
            'regression_command': 'python test_existing.py'}

    def test_separated_fixes_become_one_verified_task(self):
        cases, decisions = import_groups(self.contexts, [self.proposal])
        self.assertEqual(len(cases), 1, decisions)
        case = cases[0]
        self.assertEqual(case.base_commit, self.base)
        self.assertEqual(case.target_commit, self.last)
        self.assertNotIn('unrelated.txt', case.gold_diff)
        self.assertIn('value.strip().lower()', case.gold_diff)
        self.assertEqual(validate_dataset(cases), [])
        with patch('anybench.evaluate.Sandbox', LocalSandbox):
            outcomes = validation_results(cases)
        self.assertEqual(outcomes[0]['status'], 'verified', outcomes)
        self.assertEqual(len(outcomes[0]['trials']), 3)
        path = Path(self.temp.name) / 'cases.csv'
        write_cases(path, cases)
        loaded = read_cases(path)[0]
        self.assertEqual(loaded._metadata['validation']['status'], 'verified')
        metadata_path(path).unlink()
        with self.assertRaisesRegex(ValueError, 'sidecar'):
            read_cases(path)

    def test_conflict_requires_omitted_dependency(self):
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            reconstruct(self.repo, [self.base, self.last])

    def test_duplicate_overlap_and_invalid_evidence_rejected(self):
        cases, decisions = import_groups(self.contexts, [self.proposal, self.proposal])
        self.assertEqual(len(cases), 1)
        self.assertIn('overlaps', decisions[1]['reason'])
        invalid = {**self.proposal, 'evidence': [{'commit': c, 'paths': ['missing.py'], 'reason': 'similar message'}
                                              for c in [self.first, self.last]]}
        cases, decisions = import_groups(self.contexts, [invalid])
        self.assertFalse(cases)
        self.assertIn('actual changed paths', decisions[0]['reason'])

    def test_partial_fix_cannot_pass_independent_regression(self):
        proposal = {**self.proposal, 'selected_commits': [self.first], 'evidence': self.proposal['evidence'][:1]}
        cases, _ = import_groups(self.contexts, [proposal])
        with patch('anybench.evaluate.Sandbox', LocalSandbox):
            outcomes = validation_results(cases)
        self.assertEqual(outcomes[0]['status'], 'invalid')

    def test_revert_net_zero_rejected(self):
        (self.repo / 'app.py').write_text('def clean(value):\n    return value\n')
        reverted = commit(self.repo, 'Revert normalization')
        with self.assertRaisesRegex(ValueError, 'no net change'):
            reconstruct(self.repo, [self.first, self.last, reverted])

    def test_merge_uses_first_parent_change(self):
        git(self.repo, 'checkout', '-qb', 'fix')
        (self.repo / 'merge.py').write_text('fixed = True\n')
        constituent = commit(self.repo, 'Fix merge issue')
        git(self.repo, 'checkout', '-q', '-')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=t@invalid', 'merge', '--no-ff', '-qm', 'Merge fix', 'fix')
        merged = git(self.repo, 'rev-parse', 'HEAD').strip()
        contexts = discover([str(self.repo)])
        entry = next(c for c in contexts if c['commit'] == merged)
        self.assertIn(constituent, entry['merge_constituents'])
        base, diff, _ = reconstruct(self.repo, [merged])
        self.assertEqual(base, self.last)
        self.assertIn('merge.py', diff)

    def test_metadata_drift_and_missing_assets_rejected(self):
        cases, _ = import_groups(self.contexts, [self.proposal])
        path = Path(self.temp.name) / 'cases.csv'
        write_cases(path, cases)
        doc = json.loads(metadata_path(path).read_text())
        doc['dataset_sha256'] = 'invalid'
        metadata_path(path).write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError, 'does not match'):
            read_cases(path)

    def test_filter_keeps_window_frozen(self):
        contexts = discover([str(self.repo)], limit=2, revision_name=self.unrelated, paths=['app.py'])
        self.assertNotIn(self.last, [entry['commit'] for entry in contexts])
        self.assertTrue(all(entry['frozen_head'] == self.unrelated for entry in contexts))

    def test_large_patch_is_visible_as_rejected_candidate(self):
        contexts = discover([str(self.repo)], max_patch_bytes=10)
        entry = next(item for item in contexts if item['commit'] == self.first)
        self.assertFalse(entry['eligible'])
        self.assertEqual(entry['reason'], 'patch too large')
