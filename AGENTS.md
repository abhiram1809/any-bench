# Repository Guidelines

## Project Structure & Module Organization

AnyBench is a Python package under `src/anybench/`. `cli.py` defines the `anybench` commands; `dataset.py` builds cases from Git history; `runner.py` and `sandbox.py` execute attempts in Docker; `evaluate.py` and `report.py` score and present results. Shared records and model configuration live in `model.py`, and API calls in `llm.py`. Tests are in `tests/test_anybench.py`. The root `Dockerfile` defines the default sandbox image, and `.github/workflows/ci.yml` runs CI. There is no separate assets directory; reports are generated as HTML.

## Build, Test, and Development Commands

- `pip install -e .` installs the package and `anybench` CLI locally. Python 3.11+ is required.
- `docker build -t anybench-sandbox:latest .` builds the image used by default for test and benchmark containers.
- `PYTHONPATH=src python -m unittest discover -s tests -v` runs the test suite; the Docker integration test skips if its prerequisites are unavailable.
- `ANYBENCH_REQUIRE_DOCKER=1 PYTHONPATH=src python -m unittest discover -s tests -v` requires the real Docker test, as CI does.
- `anybench --help` lists commands. See `README.md` for the `build`, `validate`, `run`, `evaluate`, and `report` workflow.

## Coding Style & Naming Conventions

Use four spaces for Python indentation, `snake_case` for modules/functions/variables, and `PascalCase` for classes. Keep type hints on public interfaces and follow the existing standard-library-first import style. No formatter or linter is configured; keep changes consistent with nearby code and avoid adding dependencies without a clear need.

## Testing Guidelines

Tests use `unittest`. Add focused methods named `test_<behavior>` to the relevant test class in `tests/test_anybench.py`. Cover dataset serialization, CLI behavior, sandbox boundaries, and scoring logic when changing those paths. Run the full suite before a pull request; build the image and run the required Docker variant for sandbox changes. No numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

This repository has no commits yet, so there is no established message convention. Use short, imperative subjects such as `Validate sandbox test commands`. In pull requests, explain the behavior changed, link an issue when applicable, and list the tests run. Include a report screenshot when changing rendered HTML.

## Security & Configuration

Pass provider credentials through the environment variable named by `api_key_env` in model configuration; never commit keys. Treat generated CSV datasets and JSONL attempts as potentially containing private repository code. Review generated test commands before running them against a repository.
