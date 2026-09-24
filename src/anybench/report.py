"""Standalone, escaped, offline reports backed by the shared metric layer."""
from __future__ import annotations

import html
import json
from pathlib import Path

from .metadata import case_metadata
from .metrics import summary
from .model import RunRecord, _private_opener


def report(records: list[RunRecord], output: Path, *, cases: list | None = None,
           manifest: dict | None = None, rates: dict | None = None,
           aggregate_only: bool = False, include_private: bool = False) -> dict:
    metrics = summary(records, cases, manifest, rates)
    escape = lambda value: html.escape(str(value) if value is not None else 'N/A')
    percent = lambda value: f'{value:.1%}' if value is not None else 'N/A'
    number = lambda value: f'{value:.1f}' if value is not None else 'N/A'
    groups = metrics['groups']
    baselines = {}
    for group in groups:
        identity = (group['model'], group['harness'], group['profile'], group['model_id'])
        previous = baselines.get(identity)
        if previous is None or group['concurrency'] < previous['concurrency']:
            baselines[identity] = group
    rows, points = [], []
    maximum = max([g['throughput'] for g in groups] or [1]) or 1
    for group in groups:
        label = f"{group['model']} ({group['harness']}) [{group['profile']}]"
        if group['context_window_tokens'] is not None:
            label += f" {group['context_window_tokens']:,} tokens"
        base = baselines[(group['model'], group['harness'], group['profile'], group['model_id'])]
        speedup = group['throughput'] / base['throughput'] if base['throughput'] else None
        efficiency = speedup / (group['concurrency'] / base['concurrency']) if speedup is not None else None
        ci = group['test_confidence_95']
        interval = f'{ci[0]:.1%}–{ci[1]:.1%}' if ci else 'N/A (fewer than 2 cases)'
        model_records = [r for r in records if r.model == group['model'] and r.harness == group['harness']
                         and r.context_profile == group['profile'] and r.concurrency == group['concurrency']
                         and r.model_id == group['model_id'] and r.harness_version == group['harness_version']]
        duration = sum(r.model_seconds for r in model_records if r.usage_available)
        output_rate = sum(r.completion_tokens for r in model_records if r.usage_available) / duration if duration else None
        values = [label, group['model_id'], group['concurrency'], group['attempts'],
                  'Complete' if group['complete'] else 'Partial' if group['complete'] is False else 'Unknown',
                  percent(group['execution_success']), percent(group['test_accuracy']), interval,
                  percent(group['test_coverage']), percent(group['judge_mean']), percent(group['judge_coverage']),
                  percent(group['legacy_accuracy']), number(group['throughput']), number(group['solved_per_hour']),
                  number(group['latency_p50']), number(group['latency_p95']),
                  number(output_rate), f'{speedup:.2f}×' if speedup is not None else 'N/A',
                  percent(efficiency), group['usage_missing_attempts'],
                  f"${group['estimated_cost']:.4f}" if group['estimated_cost'] is not None else 'N/A']
        rows.append('<tr>' + ''.join(f'<td>{escape(value)}</td>' for value in values) + '</tr>')
        score = group['test_accuracy']
        if score is not None:
            points.append(f'<circle cx="{60 + group["throughput"] / maximum * 620:.1f}" '
                          f'cy="{310 - score * 260:.1f}" r="7" fill="#2563eb"><title>'
                          f'{escape(label)}: {percent(score)}, {number(group["throughput"])} attempts/hour</title></circle>')
    details = ''
    if not aggregate_only:
        attempt_rows = []
        case_map = {case.case_id: case for case in cases or []}
        for record in records:
            case = case_map.get(record.case_id)
            outcome = next((event['evaluation']['status'] for event in record.trace if 'evaluation' in event), '')
            detail = {'case': record.case_id, 'candidate': record.model, 'attempt': record.attempt,
                      'context_window_tokens': record.context_window_tokens, 'harness_version': record.harness_version,
                      'model_calls_by_purpose': record.model_calls_by_purpose, 'compactions': record.compactions,
                      'Compaction calls': (record.model_calls_by_purpose or {}).get('compaction'),
                      'peak_context_tokens': record.peak_context_tokens, 'stop_reason': record.stop_reason,
                      'verification_runs': len(record.verification_runs) if record.verification_runs is not None else None}
            if case:
                detail.update(grouped_commits=len(case_metadata(case).get('selected_commits', [])),
                              dataset_verification=case_metadata(case).get('validation', {}).get('status', 'unknown'))
            if include_private:
                detail.update(diff=record.diff, trace=record.trace, error=record.error,
                              judge_reason=record.judge_reason, artifact_directory=record.artifact_directory,
                              provenance=case_metadata(case) if case else {})
            values = [record.case_id, f'{record.model} / {record.context_profile}', record.attempt,
                      record.concurrency, record.status, outcome or record.test_passed,
                      record.judge_score, number(record.seconds), record.stop_reason or record.status]
            attempt_rows.append(f'<tr data-status="{escape(record.status)}">' +
                                ''.join(f'<td>{escape(value)}</td>' for value in values) +
                                '<td><details><summary>Inspect</summary><pre>' +
                                escape(json.dumps(detail, ensure_ascii=False, indent=2)) + '</pre></details></td></tr>')
        matrix = {}
        def candidate_label(record):
            return f'{record.model} / {record.harness} / {record.context_profile} / {record.concurrency} workers'
        for record in records:
            matrix.setdefault(record.case_id, {}).setdefault(candidate_label(record), []).append(
                '✓' if record.status == 'completed' and record.test_passed else
                '—' if record.test_passed is None else '✗')
        names = sorted({candidate_label(r) for r in records})
        matrix_rows = ''.join('<tr><th>' + escape(case) + '</th>' + ''.join(
            '<td>' + escape(' '.join(values.get(name, ['—']))) + '</td>' for name in names) + '</tr>'
            for case, values in sorted(matrix.items()))
        details = '<h2>Case outcomes</h2><div class="scroll"><table><thead><tr><th>Case</th>' + ''.join(
            '<th>' + escape(name) + '</th>' for name in names) + '</tr></thead><tbody>' + matrix_rows + '</tbody></table></div>'
        details += '''<h2>Attempts and context</h2><div class="controls"><label>Search <input id="search" type="search"></label>
<label>Status <select id="status"><option value="">All</option><option>completed</option><option>error</option><option>exhausted</option></select></label></div>
<div class="scroll"><table id="attempts"><thead><tr>''' + ''.join('<th>' + value + '</th>' for value in
            ['Case', 'Candidate', 'Attempt', 'Workers', 'Status', 'Test outcome', 'Judge', 'Seconds', 'Stop reason', 'Details']) + '</tr></thead><tbody>' + ''.join(attempt_rows) + '</tbody></table></div>'
    dataset_section = ''
    if cases is not None:
        verified = sum(case_metadata(case).get('validation', {}).get('status') == 'verified' for case in cases)
        grouped = sum(case_metadata(case).get('kind') == 'group' for case in cases)
        dataset_section = f'<h2>Dataset quality</h2><p>{len(cases)} cases · {grouped} reconstructed groups · {verified} with saved verification evidence</p>'
    statistical = [{key: group[key] for key in ('model', 'profile', 'concurrency', 'case_count',
                    'solve_within_k', 'failure_categories', 'retry_count', 'setup_seconds', 'model_seconds', 'test_seconds')}
                   for group in groups]
    columns = ['Candidate', 'Model ID', 'Workers', 'Attempts', 'Completeness', 'Execution success',
               'Test success', '95% case bootstrap interval', 'Test Coverage', 'Judge mean', 'Judge Coverage',
               'Legacy combined accuracy', 'Attempts/hour', 'Solved/hour', 'Latency p50', 'Latency p95',
               'Output tokens/s', 'Speedup', 'Efficiency', 'Missing usage', 'Estimated candidate cost']
    document = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AnyBench report</title><style>
:root{color-scheme:light}body{font:15px system-ui;margin:0;background:#f3f6fb;color:#172033}
main{max-width:1440px;margin:40px auto;padding:0 24px}h1{font-size:34px;margin-bottom:8px}h2{margin-top:32px}
p{max-width:1000px;line-height:1.6}.muted{color:#526279}.scroll{overflow:auto;background:white;border:1px solid #dbe2ed;border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:12px;text-align:left;border-bottom:1px solid #e5eaf2;white-space:nowrap}
th{background:#eaf0f9;cursor:pointer}th:focus{outline:2px solid #2563eb}pre{white-space:pre-wrap;word-break:break-word;max-width:850px;max-height:450px;overflow:auto}
.controls{display:flex;gap:20px;margin:12px 0}input,select{padding:8px;border:1px solid #b9c6da;border-radius:4px}svg{background:white;max-width:760px;width:100%;border:1px solid #dbe2ed;border-radius:8px}
.badge{display:inline-block;padding:5px 10px;background:#dbeafe;border-radius:20px;color:#1e40af}summary{cursor:pointer}
</style></head><body><main><span class="badge">Offline experiment report</span><h1>AnyBench</h1>
<p class="muted">Execution, local tests, and model judging are separate measurements. Exhausted attempts do not count as solved.
Legacy combined accuracy retains the earlier minimum-of-evaluators calculation. Partial and unverified experiments are labeled;
confidence intervals resample cases, not repeated attempts. N/A means unavailable.</p>
<h2>Models</h2><div class="scroll"><table id="models"><thead><tr>''' + ''.join('<th>' + c + '</th>' for c in columns) + '</tr></thead><tbody>' + ''.join(rows) + '''</tbody></table></div>
<p class="muted">Throughput uses the union of active attempt intervals, excluding resume downtime. Speedup compares the lowest measured worker count.
Efficiency divides speedup by the worker-count increase. Cost uses the supplied per-million-token rate card and excludes judging.</p>
<h2>Accuracy vs speed</h2><svg viewBox="0 0 760 360" role="img" aria-label="Local test success versus completed attempts per active hour">
<path d="M60 30 V310 H710" fill="none" stroke="#64748b"/><text x="8" y="50">100%</text><text x="20" y="310">0%</text><text x="460" y="345">Completed attempts / active hour</text>''' + ''.join(points) + '''</svg>
<p class="muted">Each point shows local test success and completed attempts per active hour. Hover for candidate details.</p>
''' + dataset_section + '<h2>Repeated attempts and diagnostics</h2><details><summary>Show statistical details</summary><pre>' + escape(json.dumps(statistical, indent=2)) + '</pre></details>' + details + '''
<p class="muted">''' + ('Aggregate-only export.' if aggregate_only else 'Private patches and logs included.' if include_private else 'Private patches and logs omitted. Use --include-private for local debugging.') + '''</p>
</main><script>
const input=document.getElementById('search'),status=document.getElementById('status');
function filter(){document.querySelectorAll('#attempts tbody tr').forEach(r=>{r.hidden=!(r.textContent.toLowerCase().includes(input.value.toLowerCase())&&(!status.value||r.dataset.status===status.value))})}
if(input){input.addEventListener('input',filter);status.addEventListener('change',filter)}
document.querySelectorAll('table thead th').forEach((th)=>{th.tabIndex=0;th.setAttribute('role','button');
function sort(){const table=th.closest('table'),body=table.tBodies[0],index=Array.from(th.parentNode.children).indexOf(th);const dir=th.dataset.direction==='up'?-1:1;th.dataset.direction=dir===1?'up':'down';th.setAttribute('aria-sort',dir===1?'ascending':'descending');
Array.from(body.rows).sort((a,b)=>a.cells[index].textContent.localeCompare(b.cells[index].textContent,undefined,{numeric:true})*dir).forEach(row=>body.appendChild(row))}
th.addEventListener('click',sort);th.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();sort()}})})
</script></body></html>'''
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, 'w', encoding='utf-8', opener=_private_opener) as stream:
        stream.write(document)
    return metrics
