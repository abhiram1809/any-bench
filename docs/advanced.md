# AnyBench

AnyBench builds coding benchmarks from **repositories you supply at runtime**. It turns historical commits into tasks, runs candidate models against pre-change checkouts, evaluates their patches, and writes a standalone HTML report. There is no built-in benchmark repository or model provider.

## Agent skill: a guided workflow

The bundled [AnyBench skill](../skills/anybench/SKILL.md) lets Claude Code or an agent harness that reads portable Agent Skills guide setup, case authoring, verification, candidate runs, and reporting from a natural language request. Install it from any working directory after installing the Python package:

```sh
anybench skill install --agent claude --scope user
anybench skill install --agent portable --scope project --project /path/to/project
```

Claude installs under `.claude/skills/anybench`; the portable form installs under `.agents/skills/anybench`, used by [Cursor](https://cursor.com/docs/skills), [OpenCode](https://opencode.ai/docs/skills/), and [Oh My Pi](https://github.com/can1357/oh-my-pi/blob/main/docs/skills.md). The source follows the [Agent Skills specification](https://agentskills.io/specification). Reinstalling the same version is safe; a modified installed skill is preserved unless `--overwrite` is given. Project installations keep the skill with the project; user installations put it under the home directory. `skills/anybench` in this repository points at the package's canonical skill files.

This manual host-agent example reviews the latest **50** commits and uses every verified case, one candidate attempt and concurrency **2**, and gives the enhanced candidate a **200,000-token** context window with **30 shared calls**. Host-agent case authoring needs no separate builder API key. A builder endpoint remains optional.

```sh
anybench doctor --json --models .anybench/session/candidates.json --output-dir .anybench/session
anybench prepare /path/to/repo --commits 50 --output .anybench/session/contexts.jsonl
# The host agent reviews contexts and writes annotations.jsonl.
anybench import .anybench/session/contexts.jsonl .anybench/session/annotations.jsonl --output .anybench/session/cases.csv
anybench validate .anybench/session/cases.csv --check-tests --json-output .anybench/session/validation.json --verified-output .anybench/session/verified.csv
anybench run .anybench/session/verified.csv --models .anybench/session/candidates.json --artifact-dir .anybench/session/artifacts --output .anybench/session/attempts.jsonl
anybench summary .anybench/session/attempts.jsonl --dataset .anybench/session/verified.csv --json
anybench report .anybench/session/attempts.jsonl --output .anybench/session/report.html
```

Each annotation names `case_id`, `eligible`, `problem_statement`, `hint`, `test_command`, `external_validation`, and `external_validation_reason`. A rejected commit needs only `case_id`, `eligible:false`, and an optional `reason`. Base commits and reference patches come from Git; annotations cannot override them. Review commands before `validate --check-tests`, which runs them in network-disabled Docker on the base snapshot and the reconstructed reference solution. The JSON validation file marks every case `verified`, `invalid`, or `skipped` with a reason; only verified cases enter `verified.csv`. Tasks with no usable local test are kept visible as skipped. These are private historical tasks, not the official SWE-bench dataset schema or test patch format.

`run --resume`, `evaluate --resume`, and `build --resume` use frozen input manifests. They skip recorded attempts and judge outcomes, including errors, so resuming does not silently repay failed calls. A changed dataset, model configuration, image ID, or budget is rejected. Start a new output path or use `--overwrite` for a deliberate rerun. Keep separate model JSON files for candidate, optional API builder, and optional judge roles. Set `"role":"candidate"`, `"role":"builder"`, or `"role":"judge"` in new entries; `run` refuses entries marked builder or judge. Omitted roles remain candidate for old configs. `doctor` checks named credential variables without printing their values or calling a model. `summary` separates completed, failed, exhausted, locally passed, and unscored attempts; CLI success is not a claim of benchmark accuracy.

## 1. Install and prepare Docker

You need Python 3.11+, Git, a POSIX host, and a working Docker daemon. From this repository:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
docker build -t anybench-sandbox:latest .
docker info
anybench --help
```

If you prefer not to install the package, replace `anybench` in the commands below with `PYTHONPATH=src python3 -m anybench.cli`. Run the CLI as a user with Docker access. Running the entire CLI with `sudo` also makes its containers run as root.

The default image supports basic Python tasks. For another language or repository-specific dependencies, build an image and pass `--image your-image:tag`. It must contain `sh`, `sleep`, `cp`, `mkdir`, `cat`, `grep`, `wc`, and `jq`, plus the tools required by that repository's tests. For multiple repositories with different images, pass `--image-map .anybench/images.json`. That JSON file maps each dataset `repository` value exactly to an image name, for example `{ "/absolute/path/to/repo": "custom-image:latest" }`.
The default image does not include `pytest` or repository-specific build dependencies. Use the same custom image for test validation and candidate runs; verified cases record the image ID.

## 2. Configure the model endpoints

Keep local inputs and outputs in the ignored `.anybench/` directory:

```sh
mkdir -p .anybench
```

Create `.anybench/builder.json`:

```json
[{"name":"builder","role":"builder","base_url":"https://provider.example/v1","model":"builder-model","api_key_env":"BUILDER_API_KEY"}]
```

Create `.anybench/candidates.json`:

```json
[{"name":"candidate","role":"candidate","base_url":"https://provider.example/v1","model":"candidate-model","api_key_env":"CANDIDATE_API_KEY"}]
```

If you want optional model judging, create a separate `.anybench/judge.json` with the same fields, `"name":"judge"`, and `"role":"judge"`. Local tests do not require a judge endpoint.

Each file is a JSON array. `api_key_env` is the **environment variable name**, not the key. Set those variables with your shell or secret manager before running. In Bash, `read -rsp 'Builder API key: ' BUILDER_API_KEY; echo; export BUILDER_API_KEY` prompts without echoing the value. Repeat for `CANDIDATE_API_KEY`.

By default, endpoints use OpenAI-compatible Chat Completions and tool calls. Set `"api":"responses"` for an OpenAI-compatible Responses endpoint, or `"api":"anthropic"` for an Anthropic-compatible Messages endpoint. Each builder, judge, and candidate entry can choose its own API, endpoint, and key variable. Anthropic entries can set `"max_output_tokens":4096`; this is the default. Set `"prompt_cache":false` if an Anthropic-compatible endpoint does not implement Anthropic cache controls. Use HTTPS; plain HTTP is accepted only for `localhost` or loopback IPs. Redirects are blocked, so set `base_url` to the provider's final API URL. Private repository content is sent to the configured model providers: the builder sees commit context and patches, the candidate can request files, and the judge sees reference and candidate patches. Use providers authorized to receive that code.
Built-in Chat Completions entries can set `"reasoning_effort":"low"` when supported by the model. OpenRouter Chat Completions calls use a stable session ID per model within one process for cache routing; reports record cached prompt tokens when the endpoint supplies them. `max_total_tokens` and `attempt_timeout` bound built-in candidate attempts.

### Built-in context orchestration

The built-in harness defaults to `"context_profile":"enhanced"` and a **200,000-token**
working context window. Set `context_window_tokens` per model when its endpoint has a
different limit. This is the total working window: output space (`max_output_tokens`,
default 4096) and a 10% estimation reserve are subtracted before allocating input.
Context maintenance starts at 80% of that input allowance (140,723 tokens with defaults).
The estimate uses UTF-8 length and is calibrated upward using provider token usage;
opaque provider output blocks retain their reported token allowance.

```json
[
  {"name":"candidate-enhanced","base_url":"https://provider.example/v1","model":"your-model-id","api_key_env":"CANDIDATE_API_KEY","context_profile":"enhanced","context_window_tokens":200000},
  {"name":"candidate-legacy","base_url":"https://provider.example/v1","model":"your-model-id","api_key_env":"CANDIDATE_API_KEY","context_profile":"legacy"}
]
```

Enhanced behavior:

- `Read` returns the **whole file** unless the agent supplies `lines_range`. Results
  include line numbers and file size. If a read cannot fit after reclaiming older
  context, the agent receives an explicit error and chooses its own ranges. Full
  reads have a 2 MB source-file limit; ranged reads can inspect larger files.
- `List` and literal-text `Search` discover relevant files. Root `AGENTS.md` loads
  automatically, with `CLAUDE.md` as a fallback. Nested instructions load on access,
  ancestor first. An edit discovering new instructions is deferred for one turn
  so the model can review them. Guidance comes only from the immutable base snapshot.
- Stable system instructions and tool definitions keep normal requests append-only.
  Older observed outputs can become artifact references; compaction preserves the
  original task, loaded instructions, recent complete tool exchanges, file-operation
  records, and a handoff summary. Large histories are summarized in bounded chunks.
- `Artifact` reads/searches collected output or historical transcripts. Command and
  search output previews include both ends and a retrieval handle. Individual output
  collection is bounded at 2 MB and reports truncation; search also stops after
  10,000 entries. Attempt artifacts have a 64 MB total cap, private files, and a
  private directory under `.anybench/artifacts/`. They never enter the candidate diff.
- `Agent` runs sequential, read-only exploration/review in a separate context, with
  no commands, edits, or nested delegation. It returns findings and a transcript
  handle. Each child has at most 10 agent turns and leaves one main-agent call for its
  parent when compaction overhead allows. All main, child, and compaction calls share `--max-steps` (default 30).
- `Run` executes agent-selected tests/checks inside Docker. Its timeout defaults to
  120 seconds, capped at 300. Timed-out commands and descendants are terminated;
  output includes exit status, duration, and workspace status. All enhanced file
  tools use the live container checkout, including files changed by commands.
  Custom enhanced sandbox images need Python 3.11+ and Git.
- Exhaustion is recorded as `status: "exhausted"` with `stop_reason: "step_limit"`
  or `"context_limit"`, preserving partial diffs and diagnostics. Scoring treats
  exhausted attempts as failures even if a final evaluator test happens to pass.

Reports distinguish profiles and include the effective window, harness version,
model calls by purpose, compactions, peak context estimate, verification counts,
stop reason, and local artifact directory. Existing result files load as legacy;
metrics not recorded in those files show N/A. Legacy retains its historical step
semantics, so compare actual usage as well as the configured main-step allowance.
External harnesses keep their own orchestration. No memory is shared across attempts.

To compare profiles, put the two entries above in `.anybench/comparison-models.json`,
use the same endpoint/model/settings and image, and run:

```bash
anybench run .anybench/cases.csv --models .anybench/comparison-models.json \
  --attempts 3 --concurrency 1 --max-steps 30 --output .anybench/comparison.jsonl
anybench report .anybench/comparison.jsonl --output .anybench/comparison.html
```

Use `evaluate` with the same judge for both profiles when judge scoring is needed.
Compare accuracy and failures alongside tokens, cache reads, time, and model calls;
fixture tests establish correctness, not live-model quality gains.

Design references: [Cursor's dynamic context discovery](https://cursor.com/blog/dynamic-context-discovery),
[Claude Code's context window](https://code.claude.com/docs/en/how-claude-code-works#the-context-window),
[Oh My Pi compaction source](https://github.com/can1357/oh-my-pi/blob/0898ccda8d70761906392a42c99196c18b060ce5/packages/agent/src/compaction/compaction.ts),
and [OpenCode compaction](https://opencode.ai/docs/config/#compaction).

### Headless candidate harnesses

The built-in harness remains the default. For Codex, Claude Code, OpenCode, or a custom CLI, add a candidate entry with `harness`, `image`, and `allowed_hosts`. The image must already contain the selected CLI, `sh`, `cp`, `sleep`, `tar`, and the repository's test dependencies. Build or pull `python:3.11-slim` for the allowlist proxy. For example:

```json
[{"name":"codex-example","harness":"codex","model":"your-model-id","api_key_env":"OPENAI_API_KEY","image":"your-codex-image:tag","allowed_hosts":["api.openai.com"]}]
```

`api_key_env` names the host variable containing the credential. Pass additional required variables with `"env":{"CONTAINER_VARIABLE":"HOST_VARIABLE"}` and list any further HTTPS hosts in `allowed_hosts`. The runner creates a fresh checkout and an internal Docker network for every attempt. The CLI reaches the listed HTTPS hosts through a proxy. The headless CLIs run without interactive approval prompts inside that container. An external attempt runs until it exits or `harness_timeout` seconds elapse (default 1800).

A custom harness uses `"harness":"custom"` and `"command":["your-cli","--headless"]`. AnyBench runs that argument array in `/repo` and sets `ANYBENCH_TASK_FILE=/tmp/task.json` and `ANYBENCH_USAGE_FILE=/tmp/usage.json`. The task file contains `case_id`, `base_commit`, `problem_statement`, and `repository` (`/repo`); it does not expose the reference patch. The custom command edits `/repo`, exits zero on success, and may write usage JSON with `prompt_tokens`, `completion_tokens`, `cached_prompt_tokens`, `cache_creation_tokens`, `tool_calls`, and `version`. Missing usage appears as N/A in the report. AnyBench collects the resulting diff and runs the dataset test command afterward. Codex, Claude Code, and OpenCode usage is parsed from their structured event output when available.

## 3. Build and check a dataset

Supply one or more local paths or HTTPS/SSH Git URLs. For example, `anybench build /repo-a https://github.com/org/repo-b.git ...` uses both repositories. The builder reviews 50 commits per repository by default and writes each accepted case immediately:

```sh
anybench build /absolute/path/to/repo --models .anybench/builder.json --builder builder --commits 50 --max-cases 20 --output .anybench/cases.csv
anybench validate .anybench/cases.csv
```

Open `.anybench/cases.csv` and review the problem statements and `test_command` values. Each row includes the base and target commits, the exact reference patch, and an optional test command. Once reviewed, check that each local test fails before the change and passes after it:

```sh
anybench validate .anybench/cases.csv --check-tests
```

`--check-tests` runs those commands in isolated containers. To continue an interrupted build without duplicating completed commits, add `--resume` to the same `build` command. Remote repositories must be accessible to Git on the host during both building and running.

## 4. Run, evaluate, and report

```sh
anybench run .anybench/cases.csv --models .anybench/candidates.json --concurrency 2 --output .anybench/attempts.jsonl
anybench evaluate .anybench/cases.csv .anybench/attempts.jsonl --models .anybench/judge.json --judge judge --output .anybench/scored.jsonl
anybench report .anybench/scored.jsonl --output .anybench/report.html
```

`run` uses two concurrent containers by default. Use `--concurrencies 1 2 4` for a sequential scaling sweep, or `--attempts 3` for repeated attempts. It appends completed attempts to JSONL as they finish and refuses to replace an existing output unless `--overwrite` is explicit. Use `--resume` with unchanged inputs to run only missing attempts. The HTML report shows accuracy, coverage, throughput, scaling, harness identity, token and cache usage, tool calls, and attempt details. CSV, JSONL, and HTML outputs are created with private `0600` permissions. Older model configs and result files remain readable as built-in Chat Completions runs.

Containers have dropped capabilities, a read-only root filesystem, and CPU, memory, and PID limits. Built-in harness containers have no network; external harness containers join an internal network with an allowlist proxy. Each editable checkout is copied into a size-limited temporary filesystem; the host snapshot is mounted read-only. The default checkout limit is `512m` and container memory limit is `1g`. For a larger repository, pass both `--workspace-size 1g --memory 2g` to `validate --check-tests` and `run`. The host still needs enough disk space to clone the source repository before the container starts. The enhanced built-in candidate uses `Run` for sandboxed test/check commands. The legacy profile retains its restricted `Bash` tool (`cat`, `grep`, `glob`, `wc`, and `jq`). The dataset's evaluator command stays private and runs separately after the attempt.

## Common problems

| Message or symptom | What to do |
| --- | --- |
| Docker daemon unavailable | Start Docker and give your user Docker access; check `docker info`. |
| Sandbox image unavailable | Run `docker build -t anybench-sandbox:latest .` or select an existing image with `--image`. |
| Missing API key environment variable | Export the variable named by `api_key_env`. Manual role JSON files contain variable names; the guided private config can hold a literal key if requested. |
| LLM API redirect blocked | Set `base_url` to the final API endpoint, not a redirecting URL. |
| Snapshot does not fit the workspace | Increase `--workspace-size` and `--memory` together. |
| No useful cases | Review more commits with `--commits` or try another repository. |

## Development

Run `PYTHONPATH=src python3 -m unittest discover -s tests -v`. The Docker integration test skips when the daemon or image is unavailable. To require it, run `ANYBENCH_REQUIRE_DOCKER=1 PYTHONPATH=src python3 -m unittest discover -s tests -v` after building the image.


## Multi-commit bug reconstruction (opt-in pilot)

A bug can require an initial fix and several follow-up corrections. `--grouped`
creates one task from selected historical fixes, even when unrelated commits lie
between them. It replays the selected changes onto the snapshot preceding the
first fix. Conflicts and missing dependencies are retained as review decisions;
AnyBench does not resolve them by importing unrelated work.

Both host-agent and API authoring use the same reconstruction and verification
pipeline. The existing single-commit workflow remains available.

```sh
anybench prepare /path/to/repo --grouped --commits 50 --output .anybench/history.jsonl
# Write one proposal per JSONL line using the format below.
anybench import .anybench/history.jsonl .anybench/groups.jsonl --grouped --output .anybench/grouped.csv
anybench validate .anybench/grouped.csv --check-tests --verified-output .anybench/verified.csv
anybench inspect .anybench/grouped.csv
```

A proposal has the following shape. Use full commit IDs from the exported history
and an exact exported repository identifier:

```json
{
  "repository": "/path/to/repo",
  "selected_commits": ["FIRST_FIX_SHA", "FOLLOWUP_FIX_SHA"],
  "evidence": [
    {"commit": "FIRST_FIX_SHA", "paths": ["app.py"], "reason": "Initial correction for the same documented bug."},
    {"commit": "FOLLOWUP_FIX_SHA", "paths": ["app.py"], "reason": "Completes the missing case from the initial correction."}
  ],
  "problem_statement": "Normalize surrounding whitespace and case.",
  "evaluation_files": {
    "test_bug.py": "import unittest\nfrom app import clean\nclass Bug(unittest.TestCase):\n    def test_normalization(self): self.assertEqual(clean(' Hello '), 'hello')\nif __name__ == '__main__': unittest.main()\n"
  },
  "evaluation_command": "python /evaluation/test_bug.py",
  "regression_command": "python -m unittest discover -s tests",
  "test_provenance": "host-authored independent regression"
}
```

Evaluation files mount read-only at `/evaluation`. The repository is `/repo`, also
read-only during evaluation; temporary files belong in `/tmp`. Python receives
`PYTHONPATH=/repo`. Node tests can use `node --test /evaluation/task.test.cjs` and
load code from `/repo`. Existing test files are restored from the base before
scoring, so editing those files in a candidate patch does not change the test oracle.
Additional authoritative repository paths can be listed as `protected_paths` in
case metadata.

For API authoring:

```sh
anybench build /path/to/repo --grouped --models .anybench/builder.json --builder builder \
  --commits 50 --image anybench-sandbox:latest --output .anybench/grouped.csv
```

The builder records grouping evidence against real changed paths, inspects
bounded patch/file ranges, and authors independent tests in a separate request
that exposes only the problem and base interfaces. Every accepted group must
reconstruct reproducibly. The regression must fail through an actual assertion
on the base and pass on the reference; unchanged regression checks must pass on
both. Grouped tasks repeat these checks three times in fresh containers. A flaky,
unexecutable, or incompletely reconstructed task is excluded from verified output.
`run` refuses grouped tasks whose saved verification is absent or stale.

`prepare` and `build` accept `--revision`, `--since`, `--until`, `--paths`,
`--max-patch-bytes` (default 500,000), and `--seed`. Grouped discovery includes
merge commits as their first-parent change and prevents overlap with already
selected constituent changes. It scans only the frozen window; it never silently
widens the search. For datasets spanning repositories, `split` uses seeded
repository-level partitions so related histories stay together:

```sh
anybench split .anybench/cases.csv --seed 42 --train-fraction 0.8 --output .anybench/splits
```

CSV columns and attempt JSONL fields have not changed. Rich task metadata lives
in `cases.csv.metadata.json`; keep that file with a grouped dataset. Its metadata
contains replay order, tree hash, evaluation assets, and verification evidence.
`target_commit` identifies the final selected historical fix; `gold_diff` is the
focused reconstructed patch, which may differ from the complete historical range.
Old single-commit CSVs remain readable without a sidecar.

## Experiment configuration and recovery

Optional TOML defaults are resolved relative to the configuration file. Explicit
CLI flags override them. Existing model JSON files remain the endpoint source.

```toml
[run]
dataset = "verified.csv"
models = "candidates.json"
output = "attempts.jsonl"
concurrency = 2
attempts = 3
max_steps = 30
```

```sh
anybench run --config .anybench/experiment.toml --concurrency 4
```

A repository alias JSON file maps the dataset's original repository value to a
new local checkout. Pass it with `--repo-map` to `validate` or `run`; case identity
stays stable. `--environment-profiles` accepts per-repository entries such as:

```json
{
  "/original/project": {
    "image": "project-tests:latest",
    "adapter": "python",
    "setup_check": "python -c 'import required_dependency'",
    "test_command": "python -m unittest discover -s tests"
  }
}
```

Adapters recognize Python unittest/pytest and Node test summaries. A custom check
can emit `ANYBENCH_RESULT {"tests": 3}` and return zero for success or one for a
regression failure. Missing files/dependencies, zero collected tests, timeouts,
and resource exhaustion do not establish a valid base regression. Simple explicit
assertion commands are also supported. Unsupported/unrecognized checks remain
unverified rather than being accepted merely because they return zero.

Model entries optionally accept `provider_concurrency`, `max_total_tokens`, and
`attempt_timeout`. Token limits apply to the built-in harness and stop further
model calls once recorded usage reaches the limit; an in-flight response can
exceed the remaining allowance. Time limits bound model requests and external
harness execution. External harnesses reject token limits they cannot enforce.
Loopback HTTP endpoints may omit an API key. External adapters reject unsupported
temperature overrides instead of silently ignoring them.

Run manifests freeze resolved configuration, source/version identity, evaluation
metadata, and Docker image IDs, including the egress proxy. Execution uses those
IDs. A bounded scheduler queues only active work, honors provider concurrency,
and writes progress events beside the results. Snapshot caches contain private
source code under `.anybench/cache/snapshots`; set `ANYBENCH_CACHE_DIR` to relocate
them or `ANYBENCH_NO_CACHE=1` to disable caching. Each attempt receives its own copy.

Output locks prevent concurrent writers. Resume explicitly repairs a torn final
JSONL record and rejects corruption in completed lines. Ordinary reads never
repair files. Recorded builder errors are not repaid on resume. An interrupted
builder call with unknown billing state requires a deliberate new output path.

Retry failed candidate attempts into a new, linked experiment:

```sh
anybench retry .anybench/attempts.jsonl --status error exhausted --output .anybench/retries.jsonl
```

Original outcomes remain intact. Retry records retain their original case,
model, worker-count, and attempt identities; do not concatenate them into the
original JSONL, which would introduce duplicates.

## Reports and regression comparisons

Reports use the same calculations as JSON summaries. They separate execution
success, local test success, model judging, coverage, and missing usage. The
previous combined score remains labeled **Legacy combined accuracy**. Throughput
uses the union of active attempt intervals, excluding resume downtime. Repeated
attempt statistics include solve-within-first-k, per-case consistency, and
confidence intervals that resample cases. Fewer than two cases yields no interval.

```sh
anybench report .anybench/attempts.jsonl --output .anybench/report.html \
  --metrics-output .anybench/metrics.json
anybench report .anybench/attempts.jsonl --aggregate-only --output .anybench/share.html
anybench report .anybench/attempts.jsonl --include-private --output .anybench/debug.html
anybench compare .anybench/baseline.jsonl .anybench/candidate.jsonl \
  --max-regressions 0 --max-accuracy-drop 0.02 --output .anybench/comparison.json
```

Offline HTML includes searchable/filterable attempts, sortable tables, case
outcomes, context diagnostics, and an accuracy/throughput plot. Patches, detailed
traces, paths, and evaluation evidence appear only with `--include-private`.
Aggregate-only exports omit case identities and attempt details. No CDN or remote
assets are loaded.

Comparisons require matching frozen datasets and execution settings, one candidate
and worker count per input, matching attempt identities, and complete runs with
local test outcomes. They report paired wins/losses and return exit code 1 when
regression thresholds are exceeded. An unknown or partial experiment does not
qualify as a finalized comparison.

`--rates` on `summary` or `report` accepts a JSON map from model ID to per-million-
token prices, for example `{"model-id":{"input":1,"output":2,"cache_read":0.1}}`.
`cache_write` is optional. Costs remain unknown when required usage is missing;
these are candidate estimates from your supplied rates, not provider billing.

## Verification for contributors

```sh
python -m pip install -e '.[dev]'
ruff check src tests
mypy
PYTHONPATH=src python -m unittest discover -s tests -v
python -m build --wheel
```

CI runs Python 3.11–3.14, checks wheel/skill installation from an unrelated working
directory, and separately requires real Docker integration. Unit fixtures exercise
multi-commit replay, missing-test rejection, candidate Git metadata isolation,
immutable test restoration, Python and Node tests, resume recovery, and shared
report calculations. Docker integration remains necessary to verify actual
container permissions and network/process isolation.
