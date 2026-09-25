# AnyBench

AnyBench turns the Git history of repositories you supply into coding problems, verifies them in Docker, runs coding models, and creates an HTML report. You do not need to know the repository's language or write benchmark cases yourself.

See the [Humanize and Boltons examples](examples/README.md) for a measured run on public library commits, including reviewed datasets and model outcomes.

## First run

Install Python 3.11+, Git, and Docker, then start the Docker daemon. Install AnyBench:

```sh
python -m pip install any-bench
```

Run it with one or more local repository paths or Git URLs:

```sh
anybench start /path/to/repository
anybench start /path/to/repo-a https://github.com/org/repo-b.git
```

On the first run, the wizard asks for a **builder** model, at least one **candidate** model, and an optional **judge**. Enter each model's API type, base URL, model ID, and credential. It checks the prerequisites and shows the maximum workload before any paid model calls. Confirm once to begin. The report path appears when the run finishes.

The builder drafts problems from up to 50 recent commits **per repository**. AnyBench prepares a Docker test environment, then retains only problems whose test fails before the historical fix and passes after it. Every verified problem is run once per candidate by default. Unverified problems are listed with reasons; they are never counted as solved or unsolved.

To set a smaller or larger workload:

```sh
anybench start /path/to/repo --commits 20 --max-problems 10
```

Interrupted sessions can resume without repeating recorded paid calls:

```sh
anybench start --resume .anybench/runs/SESSION_NAME
```

If a model call was interrupted with unknown billing state, AnyBench stops and asks for a new session rather than silently paying for it again.

## Models and API keys

The wizard recommends an **environment variable name** for each credential, such as `OPENAI_API_KEY`. Set it in your shell before starting. You can also enter a key at a hidden prompt for the current run or save a literal `api_key` in the private `.anybench/config.json` file. Saved configs use `0600` permissions and `.anybench/` is Git-ignored. Literal keys are excluded from run manifests, reports, and generated model files. To edit repositories or add, edit, and remove models later, run:

```sh
anybench configure
```

The supported endpoint protocols are OpenAI-compatible Chat Completions, OpenAI-compatible Responses, and Anthropic-compatible Messages. The judge is optional; verified local tests provide the main score.

## How setup works

Docker is required for isolated verification and candidate execution. The builder proposes dependency setup for the repository's language and test tooling. AnyBench builds the image and allows one automated repair attempt. Image builds may download public dependencies; verification and benchmark containers have no network access. If the repository needs a private registry, external service, or another unsupported setup, the report explains why its problems were not verified. The generated Docker recipes and run files are under `.anybench/runs/`.

For custom images, headless harnesses, detailed metrics, and the individual `build`, `validate`, `run`, `evaluate`, and `report` commands, see the [advanced guide](https://github.com/abhiram1809/any-bench/blob/main/docs/advanced.md).
