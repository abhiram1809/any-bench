from pathlib import Path
import tempfile
import unittest

from anybench.metrics import active_seconds, compare, summary
from anybench.model import Case, RunRecord
from anybench.report import report


class MetricsTests(unittest.TestCase):
    def test_resumed_throughput_excludes_idle_gap(self):
        records = [RunRecord('a', 'm', 1, 'completed', 5, started_at=10, finished_at=15, test_passed=True),
                   RunRecord('b', 'm', 1, 'completed', 5, started_at=1000, finished_at=1005, test_passed=False)]
        self.assertEqual(active_seconds(records), 10)
        group = summary(records)['groups'][0]
        self.assertEqual(group['throughput'], 720)
        self.assertEqual(group['solved_per_hour'], 360)

    def test_judge_only_results_are_scored_separately(self):
        group = summary([RunRecord('a', 'm', 1, 'completed', 1, judge_score=1)])['groups'][0]
        self.assertEqual(group['unscored'], 0)
        self.assertIsNone(group['test_accuracy'])
        self.assertEqual(group['judge_mean'], 1)
        self.assertEqual(group['test_coverage'], 0)

    def test_repeated_attempts_exhaustion_and_partial_runs(self):
        case = Case('a', 'repo', 'base', 'target', 'Fix', '', '', 'check')
        records = [RunRecord('a', 'm', 1, 'completed', 1, test_passed=False),
                   RunRecord('a', 'm', 2, 'completed', 1, test_passed=True),
                   RunRecord('a', 'm', 3, 'exhausted', 1, test_passed=True)]
        group = summary(records, [case], {'inputs': {'kind': 'run', 'cases': [{}], 'attempts': 4}})['groups'][0]
        self.assertFalse(group['complete'])
        self.assertEqual(group['test_accuracy'], 1/3)
        self.assertEqual(group['solve_within_k']['1']['rate'], 0)
        self.assertEqual(group['solve_within_k']['2']['rate'], 1)
        self.assertIsNone(group['test_confidence_95'])

    def test_shared_metrics_report_and_safe_aggregate_export(self):
        records = [RunRecord('PRIVATE_CASE', 'm', 1, 'completed', 1, test_passed=True,
                             diff='<script>private patch</script>', error='private error')]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'report.html'
            self.assertEqual(report(records, path), summary(records))
            self.assertNotIn('private patch', path.read_text())
            report(records, path, include_private=True)
            self.assertIn('&lt;script&gt;private patch', path.read_text())
            report(records, path, aggregate_only=True, include_private=True)
            self.assertNotIn('PRIVATE_CASE', path.read_text())
            self.assertNotIn('private patch', path.read_text())

    def test_paired_comparison_rejects_mismatched_cases(self):
        left = [RunRecord('a', 'old', 1, 'completed', 1, test_passed=True)]
        right = [RunRecord('a', 'new', 1, 'completed', 1, test_passed=False)]
        result = compare(left, right)
        self.assertEqual(result['newly_failing_cases'], ['a'])
        self.assertEqual(result['accuracy_delta'], -1)
        right[0].case_id = 'b'
        with self.assertRaisesRegex(ValueError, 'matching cases'):
            compare(left, right)

    def test_missing_usage_keeps_cost_unknown(self):
        record = RunRecord('a', 'm', 1, 'completed', 1, model_id='id', usage_available=False)
        rates = {'id': {'input': 1, 'output': 2}}
        group = summary([record], rates=rates)['groups'][0]
        self.assertIsNone(group['estimated_cost'])
        record.usage_available = True
        record.prompt_tokens, record.completion_tokens, record.cached_prompt_tokens = 100, 20, 0
        self.assertAlmostEqual(summary([record], rates=rates)['groups'][0]['estimated_cost'], .00014)
