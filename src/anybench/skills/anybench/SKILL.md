---
name: anybench
description: Build private coding benchmarks from a user's Git repositories and evaluate candidate LLMs with AnyBench. Use when asked to create SWE-bench-style tasks, compare coding models, or resume an AnyBench evaluation.
---

# AnyBench workflow

Work from any directory. Keep session files in a private `.anybench/` directory owned by the user. For a new user, start with `anybench start REPOSITORY [REPOSITORY ...]`; it configures models, checks prerequisites, builds cases, prepares test images, verifies cases, runs candidates, and writes a report. Use `anybench configure` to add or edit model endpoints. The default scans up to 50 commits per repository and uses every verified case; `--commits` and `--max-problems` adjust that workload. A judge model is optional. Resume with `anybench start --resume SESSION`.

The guided setup recommends environment variables for credentials. It also supports a hidden prompt or a literal `api_key` in the private `.anybench/config.json`. Never echo credentials or put them in manifests, logs, reports, prompts, or Docker images. The wizard asks once before paid calls. If no cases verify, present the diagnostic report and do not run candidates.

For manual case authoring or advanced benchmark control, read [workflow.md](references/workflow.md) and use the individual commands below. Inspect the target repository's own instructions and test setup before drafting cases.

1. Run `anybench doctor --json` with candidate configuration and desired image. Resolve missing Git, Docker, image, credentials, or output access before spending model calls.
2. Create a session directory under `.anybench/`. Export the latest 50 commits with `anybench prepare REPOSITORY --commits 50 --output SESSION/contexts.jsonl`. Review commit messages, patches, source, and test conventions. Draft `SESSION/annotations.jsonl` with one JSON object per considered `case_id`. Supply `eligible`, `problem_statement`, `hint`, `test_command`, `external_validation`, and `external_validation_reason`; for rejected commits, `{"case_id":"...","eligible":false,"reason":"..."}` suffices. Do not put the historical solution in the problem statement. Prefer local tests available on both parent and target snapshots.
3. Import with `anybench import SESSION/contexts.jsonl SESSION/annotations.jsonl --output SESSION/cases.csv`. AnyBench reconstructs base and reference patch from Git. Inspect all task statements and commands before verification. Run `anybench validate SESSION/cases.csv --check-tests --json-output SESSION/validation.json --verified-output SESSION/verified.csv`. Use all verified cases unless the user sets a limit. Report skipped or invalid cases explicitly. Do not silently replace failed cases or expand the search beyond 50 commits.
4. Run only candidate model entries: `anybench run SESSION/verified.csv --models SESSION/candidates.json --attempts 1 --concurrency 2 --max-steps 30 --artifact-dir SESSION/artifacts --output SESSION/attempts.jsonl`. Resume an interrupted run with the *same arguments* and `--resume`; changed inputs are refused. Never supply the builder or judge config to `run`.
5. Optional judging: `anybench evaluate SESSION/verified.csv SESSION/attempts.jsonl --models SESSION/judge.json --judge JUDGE_NAME --output SESSION/scored.jsonl`. Resume with `--resume`. Local verified tests remain the default scoring signal. Generate `anybench summary ... --json` and `anybench report ... --output SESSION/report.html`. Report failures, unscored attempts, calls, tokens, cache usage, latency, and test accuracy; a successful CLI exit alone does not mean candidate success.

`api_key_env` in manual model files holds only an environment variable name. Keep candidate access limited to the base snapshot; do not put gold patches, host notes, or evaluator commands in its task context. Read [configuration.md](references/configuration.md) for model roles, custom images, and recovery rules.
