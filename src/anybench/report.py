from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path
from statistics import median

from .model import RunRecord, _private_opener


def report(records: list[RunRecord], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    groups: dict[tuple[str, int], list[RunRecord]] = defaultdict(list)
    for record in records:
        groups[(record.model, record.concurrency)].append(record)
    rows = []
    points = []
    for (model, concurrency), items in sorted(groups.items()):
        completed = [r for r in items if r.status == "completed"]
        duration = sum(r.seconds for r in items)
        wall = (max(r.finished_at for r in items) - min(r.started_at for r in items)
                if items and all(r.started_at and r.finished_at for r in items) else duration)
        throughput = len(completed) / wall * 3600 if wall > 0 else 0
        scores: list[float | None] = []
        for item in items:
            if item.status != "completed":
                scores.append(0.0)
            else:
                available = [score for score in (
                    item.judge_score,
                    float(item.test_passed) if item.test_passed is not None else None,
                ) if score is not None]
                scores.append(min(available) if available else None)
        known = [score for score in scores if score is not None]
        accuracy = sum(known) / len(known) if known else None
        coverage = len(known) / len(items) if items else 0
        avg_seconds = duration / len(items) if items else 0
        model_seconds = sum(r.model_seconds for r in items)
        output_rate = (sum(r.completion_tokens for r in items) / model_seconds
                       if model_seconds > 0 else 0)
        rows.append((model, concurrency, len(items), len(completed), accuracy, coverage,
                     throughput, avg_seconds,
                     sum(r.prompt_tokens + r.completion_tokens for r in items), output_rate))
        if accuracy is not None:
            points.append({"model": model, "concurrency": concurrency,
                           "accuracy": accuracy, "throughput": throughput})
    baselines: dict[str, tuple[int, float]] = {}
    for row in rows:
        if row[0] not in baselines or row[1] < baselines[row[0]][0]:
            baselines[row[0]] = (row[1], row[6])
    rows = [(*row,
             row[6] / baselines[row[0]][1] if baselines[row[0]][1] > 0 else None,
             ((row[6] / baselines[row[0]][1]) / (row[1] / baselines[row[0]][0])
              if baselines[row[0]][1] > 0 else None)) for row in rows]
    max_speed = max((row[6] for row in rows), default=1) or 1
    x_mid = median(p["throughput"] for p in points) if points else 0
    y_mid = median(p["accuracy"] for p in points) if points else 0.5
    x_line = 80 + 620 * x_mid / max_speed
    y_line = 390 - 330 * y_mid
    colors = ["#2563eb", "#dc2626", "#059669", "#9333ea", "#ea580c"]
    circles = "".join(
        f'<circle cx="{80 + 620 * p["throughput"] / max_speed:.1f}" '
        f'cy="{390 - 330 * p["accuracy"]:.1f}" r="9" fill="{colors[i % len(colors)]}">'
        f'<title>{html.escape(p["model"])} at {p["concurrency"]} workers: {p["accuracy"]:.1%} accuracy, '
        f'{p["throughput"]:.1f} cases/hour</title></circle>'
        for i, p in enumerate(points))
    bars = "".join(
        f'<text x="0" y="{35 + i * 48}">{html.escape(row[0])} ({row[1]})</text>'
        f'<rect x="190" y="{17 + i * 48}" width="{500 * row[6] / max_speed:.1f}" '
        f'height="25" fill="{colors[i % len(colors)]}"/>'
        f'<text x="{205 + 500 * row[6] / max_speed:.1f}" y="{35 + i * 48}">'
        f'{row[6]:.1f}</text>' for i, row in enumerate(rows))
    table = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(x))}</td>" for x in
                           (r[0], r[1], r[2], r[3], f"{r[4]:.1%}" if r[4] is not None else "N/A",
                            f"{r[5]:.1%}", f"{r[6]:.1f}", f"{r[7]:.1f}", r[8],
                            f"{r[9]:.1f}", f"{r[10]:.2f}×" if r[10] is not None else "N/A",
                            f"{r[11]:.0%}" if r[11] is not None else "N/A")) + "</tr>" for r in rows)
    data = json.dumps([{
        "case": r.case_id, "model": r.model, "concurrency": r.concurrency,
        "attempt": r.attempt, "status": r.status,
        "seconds": round(r.seconds, 2), "test_passed": r.test_passed,
        "setup_seconds": round(r.setup_seconds, 2),
        "model_seconds": round(r.model_seconds, 2),
        "test_seconds": round(r.test_seconds, 2),
        "judge_score": r.judge_score, "tokens": r.prompt_tokens + r.completion_tokens,
        "judge_reason": r.judge_reason, "error": r.error,
    } for r in records]).replace("<", "\\u003c")
    document = f'''<!doctype html><html lang="en"><meta charset="utf-8">
<title>AnyBench report</title><style>body{{font:16px system-ui;max-width:1100px;margin:3rem auto;padding:0 1rem;color:#172033}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:.6rem;border-bottom:1px solid #ddd;text-align:left}}
svg{{max-width:100%;background:#f8fafc;border:1px solid #ddd}}.muted{{color:#64748b}}</style>
<h1>AnyBench report</h1><p class="muted">Accuracy is the mean score of evaluated attempts.
Failed attempts score zero; when both evaluators ran, the lower score is used. Coverage is the share of attempts with a score.
Throughput is completed cases divided by elapsed wall time from the first start to the last finish per model and concurrency.</p>
<p class="muted">Speedup compares each worker count with that model's lowest measured worker count.
Efficiency divides speedup by the worker-count increase.</p>
<h2>Models</h2><table><thead><tr><th>Model</th><th>Workers</th><th>Attempts</th><th>Completed</th>
<th>Accuracy</th><th>Coverage</th><th>Cases/hour</th><th>Mean seconds</th><th>Tokens</th><th>Output tokens/s</th><th>Speedup</th><th>Efficiency</th></tr></thead><tbody>{table}</tbody></table>
<h2>Accuracy vs speed</h2><svg viewBox="0 0 760 440" role="img" aria-label="Accuracy versus throughput">
<path d="M80 40 V390 H710" fill="none" stroke="#334155"/><text x="20" y="50">100%</text>
<text x="35" y="390">0%</text><text x="575" y="420">Cases/hour</text>
<path d="M{x_line:.1f} 40 V390 M80 {y_line:.1f} H710" stroke="#94a3b8" stroke-dasharray="5 5"/>
<text x="90" y="55" fill="#64748b">Accurate, slower</text><text x="535" y="55" fill="#64748b">Accurate, faster</text>
<text x="90" y="375" fill="#64748b">Less accurate, slower</text><text x="515" y="375" fill="#64748b">Less accurate, faster</text>
{circles}</svg>
<p class="muted">Quadrants split at the median accuracy and throughput of the plotted runs. Hover over each point for details.</p>
<h2>Throughput by worker count</h2><svg viewBox="0 0 760 {max(65, len(rows) * 48 + 20)}" role="img" aria-label="Throughput by worker count">{bars}</svg>
<h2>Attempts</h2><div id="attempts"></div><script>
const data={data};document.getElementById('attempts').innerHTML='<table><tr><th>Case</th><th>Model</th><th>Workers</th><th>Attempt</th><th>Status</th><th>Seconds</th><th>Test</th><th>Judge</th><th>Reason / error</th></tr>'+data.map(r=>'<tr>'+[r.case,r.model,r.concurrency,r.attempt,r.status,r.seconds,r.test_passed,r.judge_score,r.error||r.judge_reason].map(x=>'<td>'+String(x).replaceAll('&','&amp;').replaceAll('<','&lt;')+'</td>').join('')+'</tr>').join('')+'</table>';
</script></html>'''
    with open(output, "w", encoding="utf-8", opener=_private_opener) as stream:
        stream.write(document)
