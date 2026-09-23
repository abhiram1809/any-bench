# AnyBench

AnyBench accepts repositories and model endpoints at runtime, builds coding tasks from past commits, and runs models against fresh pre-change checkouts in Docker. It writes a CSV dataset, JSONL attempt records, and a standalone HTML report.

## Setup

Python 3.11+ and a working Docker daemon are required. Run with `PYTHONPATH=src python -m anybench.cli` or install the package locally with `pip install -e .`. Build the default sandbox image with `docker build -t anybench-sandbox:latest .`. For repositories needing another runtime, supply a different image with `--image`; it should contain the repository's test dependencies and the read-only command tools. For a multi-repository dataset with different runtimes, pass `--image-map images.json`, where `images.json` maps each dataset `repository` value to its image.

Create `models.json`:

```json
[
  {"name":"builder","base_url":"https://provider.example/v1","model":"strong-model","api_key_env":"BUILDER_API_KEY"},
  {"name":"candidate","base_url":"https://provider.example/v1","model":"candidate-model","api_key_env":"CANDIDATE_API_KEY"}
]
```

Set the named environment variables before running. The endpoint must implement OpenAI compatible chat completions and tool calls.
The builder reviews 50 commits per repository by default. The runner uses two concurrent sandboxes by default. `candidate.json` below is a model config file containing only the candidate entries; the builder and judge may use a separate provider.

```sh
anybench build /path/to/repo-a /path/to/repo-b --models models.json --builder builder --commits 50 --max-cases 20 --output cases.csv
anybench build /path/to/repo-a /path/to/repo-b --models models.json --builder builder --commits 50 --max-cases 20 --output cases.csv --resume
anybench validate cases.csv
anybench validate cases.csv --check-tests --image anybench-sandbox:latest
anybench run cases.csv --models candidate.json --concurrency 2 --output attempts.jsonl
anybench run cases.csv --models candidate.json --concurrencies 1 2 4 --output sweep.jsonl
anybench evaluate cases.csv attempts.jsonl --models models.json --judge builder --output scored.jsonl
anybench report scored.jsonl --output report.html
```

Repository inputs can be local paths or HTTPS/SSH Git URLs. Remote repositories must be accessible to Git on the host both when building and running. Review generated tasks and test commands before running. `validate --check-tests` checks that each local test fails on the base revision and passes on the gold revision. A row contains the repository, base and target commits, problem statement, hint, exact gold diff, test command, and external validation flag. The runner checks out the base commit; the target patch is shown only to the judge. Each attempt gets a separate clone and container. Containers have no network, all capabilities dropped, a PID and memory limit, and only the checkout mounted. The Bash tool accepts `cat`, `grep`, `glob`, `wc`, and `jq`; test commands run separately inside the same container. Tool ranges are 1-based and inclusive. A main agent can delegate to one subagent, which cannot delegate again.

The builder writes each case as it finishes; `--resume` skips commits already in the CSV. The runner checks API key environment variables, Docker access, and sandbox images before starting. It writes each attempt to JSONL as it finishes, so interrupted runs retain completed results. A new `run` command replaces its output file after preflight succeeds. The report's cases/hour metric uses elapsed wall time, including checkout, container setup, and queue time. It also shows speedup and scaling efficiency relative to each model's lowest measured worker count. Output tokens/second uses model call time and excludes setup and test execution. Accuracy is the mean of evaluated attempts: failed attempts score zero, and the lower score is used when the test and judge disagree. Coverage shows how many attempts had a usable score. The benchmark's test checker uses each row's test command; a blank test command means no test result. Cases marked for external validation skip the local test. The LLM judge scores behavior against the reference patch on a 0–1 scale.

Run `PYTHONPATH=src python -m unittest discover -s tests -v` for unit and integration tests. The Docker integration test runs when the daemon and sandbox image are available; otherwise it is skipped.
