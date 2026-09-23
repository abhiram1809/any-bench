# AnyBench

AnyBench builds coding benchmarks from **repositories you supply at runtime**. It turns historical commits into tasks, runs candidate models against pre-change checkouts, evaluates their patches, and writes a standalone HTML report. There is no built-in benchmark repository or model provider.

## 1. Install and prepare Docker

You need Python 3.11+, Git, and a working Docker daemon. From this repository:

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

## 2. Configure the model endpoints

Keep local inputs and outputs in the ignored `.anybench/` directory:

```sh
mkdir -p .anybench
```

Create `.anybench/builder.json`:

```json
[{"name":"builder","base_url":"https://provider.example/v1","model":"builder-model","api_key_env":"BUILDER_API_KEY"}]
```

Create `.anybench/candidates.json`:

```json
[{"name":"candidate","base_url":"https://provider.example/v1","model":"candidate-model","api_key_env":"CANDIDATE_API_KEY"}]
```

Each file is a JSON array. `api_key_env` is the **environment variable name**, not the key. Set those variables with your shell or secret manager before running. In Bash, `read -rsp 'Builder API key: ' BUILDER_API_KEY; echo; export BUILDER_API_KEY` prompts without echoing the value. Repeat for `CANDIDATE_API_KEY`.

Endpoints must support OpenAI-compatible chat completions and tool calls. Use HTTPS; plain HTTP is accepted only for `localhost` or loopback IPs. Redirects are blocked, so set `base_url` to the provider's final API URL. Private repository content is sent to the configured model providers: the builder sees commit context and patches, the candidate can request files, and the judge sees reference and candidate patches. Use providers authorized to receive that code.

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
anybench evaluate .anybench/cases.csv .anybench/attempts.jsonl --models .anybench/builder.json --judge builder --output .anybench/scored.jsonl
anybench report .anybench/scored.jsonl --output .anybench/report.html
```

`run` uses two concurrent containers by default. Use `--concurrencies 1 2 4` for a sequential scaling sweep, or `--attempts 3` for repeated attempts. It appends completed attempts to JSONL as they finish, but a **new** `run` command replaces its output file after preflight; choose a new filename to keep an earlier run. The HTML report shows accuracy, coverage, throughput, scaling, and attempt details. CSV, JSONL, and HTML outputs are created with private `0600` permissions.

Each container has no network, dropped capabilities, a read-only root filesystem, and CPU, memory, and PID limits. Its editable checkout is copied into a size-limited temporary filesystem; the host snapshot is mounted read-only. The default checkout limit is `512m` and container memory limit is `1g`. For a larger repository, pass both `--workspace-size 1g --memory 2g` to `validate --check-tests` and `run`. The host still needs enough disk space to clone the source repository before the container starts. The candidate's Bash tool accepts only `cat`, `grep`, `glob`, `wc`, and `jq`; the dataset's test command runs separately in the container.

## Common problems

| Message or symptom | What to do |
| --- | --- |
| Docker daemon unavailable | Start Docker and give your user Docker access; check `docker info`. |
| Sandbox image unavailable | Run `docker build -t anybench-sandbox:latest .` or select an existing image with `--image`. |
| Missing API key environment variable | Export the variable named by `api_key_env`; keep the key out of JSON files. |
| LLM API redirect blocked | Set `base_url` to the final API endpoint, not a redirecting URL. |
| Snapshot does not fit the workspace | Increase `--workspace-size` and `--memory` together. |
| No useful cases | Review more commits with `--commits` or try another repository. |

## Development

Run `PYTHONPATH=src python3 -m unittest discover -s tests -v`. The Docker integration test skips when the daemon or image is unavailable. To require it, run `ANYBENCH_REQUIRE_DOCKER=1 PYTHONPATH=src python3 -m unittest discover -s tests -v` after building the image.
