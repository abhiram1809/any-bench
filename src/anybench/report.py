"""Standalone, escaped, offline reports backed by the shared metric layer."""
from __future__ import annotations

import html
import json
from pathlib import Path

from .branding import catalog
from .metadata import case_metadata
from .metrics import summary
from .model import RunRecord, _private_opener


def report(records: list[RunRecord], output: Path, *, cases: list | None = None,
           manifest: dict | None = None, rates: dict | None = None,
           aggregate_only: bool = False, include_private: bool = False,
           variable_groups: set[tuple[str, int]] | None = None) -> dict:
    metrics = summary(records, cases, manifest, rates)
    escape = lambda value: html.escape(str(value) if value is not None else 'N/A')
    percent = lambda value: f'{value:.1%}' if value is not None else 'N/A'
    number = lambda value: f'{value:.1f}' if value is not None else 'N/A'
    groups = metrics['groups']
    variable_groups = variable_groups or set()
    for group in groups:
        if (group['model'], group['concurrency']) in variable_groups:
            group['variable_concurrency'] = True
    baselines = {}
    for group in groups:
        if group.get('variable_concurrency'):
            continue
        identity = (group['model'], group['harness'], group['profile'], group['model_id'])
        previous = baselines.get(identity)
        if previous is None or group['concurrency'] < previous['concurrency']:
            baselines[identity] = group
    rows, compact_rows, points, cards, legend = [], [], [], [], []
    palette = ['#547b36', '#5279a4', '#b48043', '#9877ae', '#42998a', '#bd6c70']

    def brand_chip(brand: dict, kind: str) -> str:
        icon = catalog()['icons'].get(brand['icon'], catalog()['icons']['generic'])
        return (f'<span class="brand-chip" title="{kind}: {escape(brand["label"])}">'
                f'<img src="{icon}" alt="" width="25" height="25">'
                f'<span class="sr-only">{kind}: </span>{escape(brand["label"])}</span>')

    def brand_row(group: dict) -> str:
        brands = group['branding']
        return ('<div class="brand-row">' + brand_chip(brands['harness'], 'Harness') +
                ''.join(brand_chip(brand, 'Provider') for brand in brands['providers']) + '</div>')
    maximum = max([g['throughput'] for g in groups] or [1]) or 1
    for index, group in enumerate(groups):
        color = palette[index % len(palette)]
        label = f"{group['model']} ({group['harness']}) [{group['profile']}]"
        if group['context_window_tokens'] is not None:
            label += f" {group['context_window_tokens']:,} tokens"
        base = baselines.get((group['model'], group['harness'], group['profile'], group['model_id']))
        speedup = (group['throughput'] / base['throughput'] if base and base['throughput'] and
                   not group.get('variable_concurrency') else None)
        efficiency = speedup / (group['concurrency'] / base['concurrency']) if speedup is not None and base else None
        ci = group['test_confidence_95']
        interval = f'{ci[0]:.1%}–{ci[1]:.1%}' if ci else 'N/A (fewer than 2 cases)'
        model_records = [r for r in records if r.model == group['model'] and r.harness == group['harness']
                         and r.context_profile == group['profile'] and r.concurrency == group['concurrency']
                         and r.model_id == group['model_id'] and r.harness_version == group['harness_version']]
        duration = sum(r.model_seconds for r in model_records if r.usage_available)
        output_rate = sum(r.completion_tokens for r in model_records if r.usage_available) / duration if duration else None
        values = [label, group['model_id'], str(group['concurrency']) +
                  (' (variable)' if group.get('variable_concurrency') else ''), group['attempts'],
                  'Complete' if group['complete'] else 'Partial' if group['complete'] is False else 'Unknown',
                  percent(group['execution_success']), percent(group['test_accuracy']), interval,
                  percent(group['test_coverage']), percent(group['judge_mean']), percent(group['judge_coverage']),
                  percent(group['legacy_accuracy']), number(group['throughput']), number(group['solved_per_hour']),
                  number(group['latency_p50']), number(group['latency_p95']),
                  number(output_rate), f'{speedup:.2f}×' if speedup is not None else 'N/A',
                  percent(efficiency), group['usage_missing_attempts'],
                  f"${group['estimated_cost']:.4f}" if group['estimated_cost'] is not None else 'N/A']
        identity_cell = f'<td><strong>{escape(label)}</strong>{brand_row(group)}</td>'
        rows.append('<tr>' + identity_cell + ''.join(f'<td>{escape(value)}</td>' for value in values[1:]) + '</tr>')
        compact_rows.append('<tr>' + identity_cell + ''.join(f'<td>{escape(values[i])}</td>'
                            for i in [2, 3, 4, 6, 8, 10, 12, 14, 20]) + '</tr>')
        accuracy = group['test_accuracy']
        width = (accuracy or 0) * 100
        cards.append(f'<article class="candidate-card" style="--series:{color}">'
                     f'<div class="candidate-head"><h3>{escape(group["model"])}</h3>'
                     f'<span class="badge">{escape(values[2])} workers</span></div>'
                     f'<div class="model-id">{escape(group["model_id"] or "Model ID unavailable")}</div>'
                     f'{brand_row(group)}<div class="candidate-score"><strong>{percent(accuracy)}</strong>'
                     '<span>local test success</span></div>'
                     f'<div class="score-track" aria-hidden="true"><span style="width:{width:.2f}%"></span></div>'
                     f'<div class="candidate-meta"><span>Attempts<strong>{group["attempts"]}</strong></span>'
                     f'<span>Throughput<strong>{number(group["throughput"])}/h</strong></span>'
                     f'<span>Latency p50<strong>{number(group["latency_p50"])}s</strong></span></div>'
                     f'<p class="candidate-context">[{escape(group["profile"])}] · {escape(values[4])} · '
                     f'Test coverage {percent(group["test_coverage"])}</p></article>')
        legend.append(f'<span><i class="legend-dot" style="--series:{color}"></i>{escape(label)}</span>')
        score = group['test_accuracy']
        if score is not None:
            points.append(f'<circle cx="{60 + group["throughput"] / maximum * 620:.1f}" '
                          f'cy="{310 - score * 260:.1f}" r="7" class="chart-point" fill="{color}"><title>'
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
                                '<td class="attempt-details"><details><summary>Inspect</summary><pre>' +
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
        details = '<div class="section-heading"><div><span class="eyebrow">CASE MATRIX</span><h2>Case outcomes</h2></div></div><div class="scroll"><table><thead><tr><th>Case</th>' + ''.join(
            '<th>' + escape(name) + '</th>' for name in names) + '</tr></thead><tbody>' + matrix_rows + '</tbody></table></div>'
        details += '''<div class="section-heading"><div><span class="eyebrow">EVIDENCE</span><h2>Attempts and context</h2></div></div><div class="controls"><label>Search <input id="search" type="search"></label>
<label>Status <select id="status"><option value="">All</option><option>completed</option><option>error</option><option>exhausted</option></select></label><output id="result-count" aria-live="polite"></output></div>
<div class="scroll"><table id="attempts"><thead><tr>''' + ''.join('<th>' + value + '</th>' for value in
            ['Case', 'Candidate', 'Attempt', 'Workers', 'Status', 'Test outcome', 'Judge', 'Seconds', 'Stop reason', 'Details']) + '</tr></thead><tbody>' + ''.join(attempt_rows) + '</tbody></table></div>'
    dataset_section = ''
    if cases is not None:
        verified = sum(case_metadata(case).get('validation', {}).get('status') == 'verified' for case in cases)
        grouped = sum(case_metadata(case).get('kind') == 'group' for case in cases)
        dataset_section = f'<section class="dataset-quality"><h2>Dataset quality</h2><p>{len(cases)} cases · {grouped} reconstructed groups · {verified} with saved verification evidence</p></section>'
    statistical = [{key: group[key] for key in ('model', 'profile', 'concurrency', 'case_count',
                    'solve_within_k', 'failure_categories', 'retry_count', 'setup_seconds', 'model_seconds', 'test_seconds')}
                   for group in groups]
    columns = ['Candidate', 'Model ID', 'Workers', 'Attempts', 'Completeness', 'Execution success',
               'Test success', '95% case bootstrap interval', 'Test Coverage', 'Judge mean', 'Judge Coverage',
               'Legacy combined accuracy', 'Attempts/hour', 'Solved/hour', 'Latency p50', 'Latency p95',
               'Output tokens/s', 'Speedup', 'Efficiency', 'Missing usage', 'Estimated candidate cost']
    compact_columns = [columns[i] for i in [0, 2, 3, 4, 6, 8, 10, 12, 14, 20]]
    headers = lambda names: ''.join('<th scope="col">' + name + '</th>' for name in names)
    css = (Path(__file__).parent / 'report_assets' / 'report.css').read_text()
    anybench_logo = catalog()['icons']['anybench']
    logo_license = (Path(__file__).parent / 'brand_assets' / 'LICENSE.lobe-icons').read_text()
    completed = sum(r.status == 'completed' for r in records)
    passed = sum(r.status == 'completed' and r.test_passed is True for r in records)
    scored = sum(r.test_passed is not None for r in records)
    overview = ''.join(f'<div class="stat"><small>{name}</small><strong>{value}</strong><span>{note}</span></div>'
                       for name, value, note in [
                           ('Candidate groups', len(groups), 'Model, harness, context and workers'),
                           ('Recorded attempts', len(records), f'{completed} completed executions'),
                           ('Passing attempts', passed, f'{scored} attempts with local test outcomes'),
                           ('Cases observed', len({r.case_id for r in records}), 'Distinct cases in these results')])
    grid = ''.join(f'<path class="grid" d="M60 {310 - score * 260:.0f} H710"/>'
                   f'<text x="45" y="{314 - score * 260:.0f}" text-anchor="end">{score:.0%}</text>'
                   for score in [0, .25, .5, .75, 1])
    ticks = ''.join(f'<text x="{60 + fraction * 620:.0f}" y="332" text-anchor="middle">'
                    f'{maximum * fraction:.0f}</text>' for fraction in [0, .25, .5, .75, 1])
    privacy_note = ('Aggregate-only export.' if aggregate_only else 'Private patches and logs included.'
                    if include_private else 'Private patches and logs omitted. Use --include-private for local debugging.')
    candidate_section = ('<div class="candidate-grid">' + ''.join(cards) + '</div>' if groups else
                         '<div class="empty-report">No attempts recorded yet. Run a benchmark to compare candidates.</div>')
    document = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<!-- Brand assets from Lobe Icons. {logo_license} -->
<title>AnyBench · Benchmark report</title><style>{css}</style></head><body>
<header class="report-topbar"><a href="#overview" class="wordmark"><img src="{anybench_logo}" alt="" width="27" height="27">AnyBench <span>Report</span></a>
<nav aria-label="Report sections"><a href="#models">Candidates</a><a href="#analysis">Analysis</a><a href="#diagnostics">Diagnostics</a></nav></header>
<main id="overview"><div class="hero"><div><span class="badge">Offline experiment report</span><h1>Benchmark results</h1>
<p>Compare how your candidates perform. Follow every result from its harness and API provider to the tests that measured it.</p></div><div class="hero-icon"><img src="{anybench_logo}" alt="" width="39" height="39"></div></div>
<section class="stats" aria-label="Experiment overview">{overview}</section>
<section aria-labelledby="candidate-heading"><div class="section-heading"><div><span class="eyebrow">CANDIDATE PERFORMANCE</span><h2 id="candidate-heading">Models</h2><p>Harness and provider identities follow the recorded configuration.</p></div><span class="badge">{len(groups)} groups</span></div>
{candidate_section}<div class="scroll" tabindex="0" role="region" aria-label="Candidate metrics"><table id="models"><caption>Core metrics · select a column heading to sort</caption><thead><tr>{headers(compact_columns)}</tr></thead><tbody>{''.join(compact_rows)}</tbody></table></div>
<p class="table-hint">Scroll horizontally for all metrics. Providers with unknown or custom endpoints use a neutral icon; older runs may not include provider information.</p>
<details class="expanded-metrics"><summary>All measurements · coverage, confidence, cost and scaling</summary><div class="scroll" tabindex="0" role="region" aria-label="All candidate measurements"><table><thead><tr>{headers(columns)}</tr></thead><tbody>{''.join(rows)}</tbody></table></div></details></section>
<section class="analysis-grid" id="analysis"><div class="chart-card"><span class="eyebrow">PERFORMANCE FRONTIER</span><h2>Accuracy vs speed</h2>
<svg class="chart" viewBox="0 0 760 375" role="img" aria-label="Local test success versus completed attempts per active hour">
{grid}<path class="axis" d="M60 30 V310 H710" fill="none"/>{ticks}<text x="385" y="365" text-anchor="middle">Completed attempts / active hour</text>{''.join(points)}</svg>
<div class="chart-legend">{''.join(legend)}</div><p class="muted">Each point shows local test success and completed attempts per active hour. Hover for candidate details.</p></div>
<aside class="method-card"><span class="eyebrow">READING THESE RESULTS</span><h2>Separate signals. Clear evidence.</h2><dl>
<dt>Execution, tests and judging</dt><dd>Execution, local tests, and model judging are separate measurements. Exhausted attempts do not count as solved. Legacy combined accuracy retains the earlier minimum-of-evaluators calculation.</dd>
<dt>Coverage and confidence</dt><dd>Partial and unverified experiments are labeled. Confidence intervals resample cases, not repeated attempts. N/A means unavailable.</dd>
<dt>Throughput and scaling</dt><dd>Throughput uses the union of active attempt intervals, excluding resume downtime. Speedup compares the lowest measured worker count. Efficiency divides speedup by the worker-count increase.</dd>
<dt>Estimated cost</dt><dd>Cost uses the supplied per-million-token rate card and excludes judging.</dd></dl><span class="badge">Logos embedded · works offline</span></aside></section>
{dataset_section}<section id="diagnostics"><div class="section-heading"><div><span class="eyebrow">RELIABILITY</span><h2>Repeated attempts and diagnostics</h2></div></div>
<details class="expanded-metrics"><summary>Show statistical details</summary><pre>{escape(json.dumps(statistical, indent=2))}</pre></details>{details}</section>
<footer><span>{privacy_note}</span><span>AnyBench · Brand assets from Lobe Icons (MIT)</span></footer></main>
''' + '''<script>
const input=document.getElementById('search'),status=document.getElementById('status');
function filter(){let visible=0;const rows=document.querySelectorAll('#attempts tbody tr');rows.forEach(r=>{r.hidden=!(r.textContent.toLowerCase().includes(input.value.toLowerCase())&&(!status.value||r.dataset.status===status.value));if(!r.hidden)visible++});document.getElementById('result-count').textContent=`${visible} of ${rows.length} attempts`}
if(input){input.addEventListener('input',filter);status.addEventListener('change',filter);filter()}
document.querySelectorAll('table thead th').forEach(th=>{const button=document.createElement('button');button.type='button';button.textContent=th.textContent;button.setAttribute('aria-label',th.textContent);th.replaceChildren(button);
function sort(){const table=th.closest('table'),body=table.tBodies[0],index=Array.from(th.parentNode.children).indexOf(th);const dir=th.getAttribute('aria-sort')==='ascending'?-1:1;table.querySelectorAll('thead th').forEach(h=>h.removeAttribute('aria-sort'));th.setAttribute('aria-sort',dir===1?'ascending':'descending');
Array.from(body.rows).sort((a,b)=>a.cells[index].textContent.localeCompare(b.cells[index].textContent,undefined,{numeric:true})*dir).forEach(row=>body.appendChild(row))}
button.addEventListener('click',sort)})
</script></body></html>'''
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, 'w', encoding='utf-8', opener=_private_opener) as stream:
        stream.write(document)
    return metrics
