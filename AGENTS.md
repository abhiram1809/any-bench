# Repository Guidelines

## Project Structure & Module Organization

AnyBench is a Python package under `src/anybench/`. `cli.py` defines the `anybench` commands; `dataset.py` builds cases from Git history; `runner.py` and `sandbox.py` execute attempts in Docker; `evaluate.py` and `report.py` score and present results. Shared records and model configuration live in `model.py`, and API calls in `llm.py`. Experimental Studio uses `live.py` for observation and control, `studio_server.py` for the optional localhost API, and `studio_web/` for its React frontend. Bundled assets live in `src/anybench/studio_assets/`. Python tests are in `tests/`; browser tests are in `studio_web/tests/`. The root `Dockerfile` defines the default sandbox image, and `.github/workflows/ci.yml` runs CI.

## Build, Test, and Development Commands

- `pip install -e .` installs the package and `anybench` CLI locally. Python 3.11+ is required.
- `docker build -t anybench-sandbox:latest .` builds the image used by default for test and benchmark containers.
- `PYTHONPATH=src python -m unittest discover -s tests -v` runs the test suite; the Docker integration test skips if its prerequisites are unavailable.
- `ANYBENCH_REQUIRE_DOCKER=1 PYTHONPATH=src python -m unittest discover -s tests -v` requires the real Docker test, as CI does.
- `anybench --help` lists commands. See `README.md` for the `build`, `validate`, `run`, `evaluate`, and `report` workflow.
- `pip install -e '.[studio,dev]'` installs the optional Studio backend. `anybench studio` serves the workspace on loopback. The default CLI remains usable without the `studio` extra.
- `cd studio_web && npm ci && npm run build && npm test && npm run test:e2e` builds bundled assets and checks the frontend. Build the frontend before a Python wheel containing Studio assets; users of the wheel need no Node installation.

## Coding Style & Naming Conventions

Use four spaces for Python indentation, `snake_case` for modules/functions/variables, and `PascalCase` for classes. Keep type hints on public interfaces and follow the existing standard-library-first import style. No formatter or linter is configured; keep changes consistent with nearby code and avoid adding dependencies without a clear need.

## Testing Guidelines

Python tests use `unittest`. Add focused methods named `test_<behavior>` to the relevant test module. Cover dataset serialization, CLI behavior, sandbox boundaries, scoring logic, and Studio authorization or event integrity when changing those paths. Run the full suite before a pull request; build the image and run the required Docker variant for sandbox changes. Run the frontend unit and browser tests for Studio changes. No numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

Use short, imperative commit subjects such as `Validate sandbox test commands`. In pull requests, explain the behavior changed, link an issue when applicable, and list the tests run. Include a report screenshot when changing rendered HTML.

## Security & Configuration

Pass provider credentials through the environment variable named by `api_key_env` in model configuration; never commit keys. Keep generated datasets, attempts, and reports under the ignored `.anybench/` directory. They may contain private repository code. Review generated test commands before running them against a repository.

Studio listens on `127.0.0.1`, authenticates with a private local token, and stores private events and artifacts under `.anybench/live/`. Preserve Host/Origin checks, registered artifact IDs, credential redaction, and durable paid-operation markers when changing its API or observer. Never bundle session data or credentials into `studio_assets/`.

## Local Future Notes

When the user refers to "my notes" or future AnyBench plans, read the root `NOTES.md` before acting. The file is intentionally Git-ignored and may be absent in other clones.
