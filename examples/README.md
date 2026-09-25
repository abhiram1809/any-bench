# Public library benchmark examples

On 2026-09-25, AnyBench inspected the five then-recent commits in each of [Humanize](humanize/README.md) and [Boltons](boltons/README.md). GLM-5.3 Flash built six cases from ten commits. We reviewed the generated problem statements and test commands, verified five cases in Docker, and skipped one translation workflow that needed unavailable gettext tooling. Qwen3.8-27B completed and passed three of the five verified cases. The two other attempts reached the token budget. GLM-5.3 Flash judged the three completed attempts with a mean score of 0.90.

The checked-in `cases.csv` files contain the reviewed problems, private evaluator commands, and historical reference diffs. Their `repository` fields use public Git URLs so they can be replayed outside the original machine. `results.json` records the commits, changed files, parent/reference validation, and candidate outcomes. These are a small diagnostic sample, not a stable model ranking.

## How the run was handled

1. Exported the five most recent commit contexts from each repository and asked GLM-5.3 Flash to select self-contained behavior changes. The builder saw commit messages, changed files, and patches.
2. Reviewed every accepted case. Some generated commands referenced tests added by the target commit, used invalid syntax, or assumed unavailable tools. Replaced those with focused checks against code present in both parent and target snapshots.
3. Built [Dockerfile.python-libs](Dockerfile.python-libs). The default sandbox lacked pytest, freezegun, and Humanize's generated version module. Used the same image for validation and candidate runs.
4. Ran `validate --check-tests`. All five selected checks failed on the parent snapshot and passed after the reference patch. The translation case was skipped with its reason recorded.
5. Ran one Qwen candidate attempt per verified case with the enhanced built-in harness, concurrency 1, `--max-steps 20`, a 250,000 total-token cap, a 65,536-token context window, and a 300-second attempt timeout. GLM judged completed attempts. The same OpenRouter model session was reused within each process to favor cache routing.

The run recorded 991,282 prompt tokens, 19,745 completion tokens, and 598,976 cached prompt tokens across scored records. OpenRouter key usage after exploratory and completed calls was $0.408 of the $9 limit. No API key is included here.

## Replay the pinned cases

From the repository root, build the default and example images:

```sh
docker build -t anybench-sandbox:latest .
docker build -f examples/Dockerfile.python-libs -t anybench-python-libs:example .
```

For either `humanize` or `boltons`, replace `REPO` below and run:

```sh
REPO=humanize
anybench validate "examples/$REPO/cases.csv" --check-tests \
  --image anybench-python-libs:example \
  --json-output ".anybench/$REPO-validation.json" \
  --verified-output ".anybench/$REPO-verified.csv"
anybench run ".anybench/$REPO-verified.csv" \
  --models examples/models/candidate.json --image anybench-python-libs:example \
  --concurrency 1 --max-steps 20 --output ".anybench/$REPO-attempts.jsonl"
anybench evaluate ".anybench/$REPO-verified.csv" ".anybench/$REPO-attempts.jsonl" \
  --models examples/models/judge.json --judge glm-judge \
  --output ".anybench/$REPO-scored.jsonl"
anybench report ".anybench/$REPO-scored.jsonl" \
  --output ".anybench/$REPO-report.html"
```

Set `OPENROUTER_API_KEY` in your environment before paid model calls. The [model files](models/) name that environment variable and contain no credential. Replaying validation uses Docker and Git but does not call a model. Replaying `run` and `evaluate` incurs new API usage; results may vary.

To sample the *current* latest five commits instead of replaying these pinned cases, use `anybench build REPOSITORY_URL --commits 5 --models examples/models/builder.json --builder glm-builder --output .anybench/new-cases.csv`, then review its commands and validate them before a candidate run.

## Framework lessons

This run led to bounded builder inspection, clearer test-command guidance, support for common flat line ranges and file-path search, newline preservation in edits, cache-file exclusion from candidate diffs, container Git ownership configuration, an end-to-end API response deadline, and low reasoning effort support. One Boltons attempt had a passing evaluator check on its partial patch but did not finish before its token cap, so the framework counted it as unsuccessful. Dataset checks still require human review.
