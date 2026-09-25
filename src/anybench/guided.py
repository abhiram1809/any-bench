"""A small, resumable first-run path over the existing benchmark primitives."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from getpass import getpass
from html import escape
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

from .dataset import _repository, git
from .evaluate import validation_results
from .llm import ChatClient, parse_json_object
from .metadata import write_metadata
from .model import ModelConfig, read_cases, write_cases
from .privacy import redact
from .workflow import ensure_private_parent, fingerprint, private_json


DEFAULT_CONFIG = Path(".anybench/config.json")
MANIFEST_NAMES = {"requirements.txt", "pyproject.toml", "poetry.lock", "uv.lock",
                  "Pipfile", "Pipfile.lock", "package.json", "package-lock.json",
                  "pnpm-lock.yaml", "yarn.lock", "go.mod", "go.sum", "Cargo.toml",
                  "Cargo.lock", "pom.xml", "build.gradle", "build.gradle.kts",
                  "Gemfile", "Gemfile.lock", "composer.json", "composer.lock",
                  "Makefile", "README.md"}
BASE_IMAGE = "python:3.12-slim"


def _ignore_local_state(path: Path) -> None:
    parts = path.parts
    if ".anybench" not in parts:
        return
    directory = Path(*parts[:parts.index(".anybench") + 1])
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    ignore = directory / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n!.gitignore\n")


def _ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{label}{suffix}: ").strip()
    return answer or default


def _model(role: str, current: dict | None = None) -> dict:
    current = current or {}
    print(f"\n{role.title()} model")
    name = _ask("Name", current.get("name", role))
    api = _ask("API (chat_completions / responses / anthropic)",
               current.get("api", "chat_completions"))
    if api not in {"chat_completions", "responses", "anthropic"}:
        raise ValueError("API must be chat_completions, responses, or anthropic")
    default_url = "https://api.anthropic.com/v1" if api == "anthropic" else "https://api.openai.com/v1"
    base_url = _ask("Base URL", current.get("base_url", default_url))
    model = _ask("Model ID", current.get("model", ""))
    if not model:
        raise ValueError("Model ID is required")
    choice = _ask("Credential: env / save / prompt (env recommended)",
                  "save" if "api_key" in current else "prompt" if current.get("api_key_prompt") else "env")
    entry = {"name": name, "role": role, "api": api, "base_url": base_url, "model": model}
    if choice == "env":
        entry["api_key_env"] = _ask("Environment variable name",
                                    current.get("api_key_env", f"{role.upper()}_API_KEY"))
    elif choice == "save":
        value = getpass("API key (stored in private config; Enter to keep existing): ")
        entry["api_key"] = value or current.get("api_key", "")
        if not entry["api_key"]:
            raise ValueError("API key is required")
    elif choice == "prompt":
        entry["api_key_prompt"] = True
    else:
        raise ValueError("Credential choice must be env, save, or prompt")
    _public_model(entry)  # Validate endpoint and model fields before saving.
    return entry


def _public_model(entry: dict) -> dict:
    source = [key for key in ("api_key_env", "api_key", "api_key_prompt") if entry.get(key)]
    if len(source) != 1:
        raise ValueError("Each model needs exactly one credential source")
    data = {key: value for key, value in entry.items()
            if key not in {"api_key", "api_key_prompt"}}
    data.setdefault("api_key_env", "ANYBENCH_PRIVATE_KEY")
    ModelConfig(**data)
    return {key: value for key, value in data.items() if key != "api_key_env" or source == ["api_key_env"]}


def _validate_config(config: dict) -> None:
    if not isinstance(config.get("repositories"), list) or not all(
            isinstance(value, str) and value for value in config["repositories"]):
        raise ValueError("Config repositories must be a list of paths or Git URLs")
    models = config.get("models")
    if not isinstance(models, list):
        raise ValueError("Config models must be a list")
    roles = [item.get("role") for item in models]
    if roles.count("builder") != 1 or roles.count("candidate") < 1 or roles.count("judge") > 1:
        raise ValueError("Configure one builder, at least one candidate, and at most one judge")
    if len({item.get("name") for item in models}) != len(models):
        raise ValueError("Model names must be unique")
    for item in models:
        _public_model(item)


def configure(path: Path = DEFAULT_CONFIG, repositories: list[str] | None = None) -> dict:
    """Create or interactively edit the private guided configuration."""
    path = path.expanduser()
    config = json.loads(path.read_text()) if path.exists() else {"repositories": [], "models": []}
    if repositories:
        config["repositories"] = repositories
    if not config["models"]:
        if not config["repositories"]:
            config["repositories"] = _ask("Repositories (space separated)").split()
        config["models"].append(_model("builder"))
        config["models"].append(_model("candidate"))
        while _ask("Add another candidate? (y/N)", "n").lower() == "y":
            number = sum(item["role"] == "candidate" for item in config["models"]) + 1
            config["models"].append(_model("candidate", {"name": f"candidate-{number}"}))
        if _ask("Add an optional judge? (y/N)", "n").lower() == "y":
            config["models"].append(_model("judge"))
    else:
        while True:
            print("\n1. Set repositories  2. Add model  3. Edit model  4. Remove model  5. Save")
            selected = _ask("Choose", "5")
            if selected == "1":
                config["repositories"] = _ask("Repositories (space separated)",
                                               " ".join(config["repositories"])).split()
            elif selected == "2":
                role = _ask("Role (builder / candidate / judge)", "candidate")
                if role not in {"builder", "candidate", "judge"}:
                    raise ValueError("Unknown model role")
                number = sum(item["role"] == role for item in config["models"]) + 1
                config["models"].append(_model(role, {"name": f"{role}-{number}"}))
            elif selected in {"3", "4"}:
                names = [item["name"] for item in config["models"]]
                name = _ask(f"Model name ({', '.join(names)})")
                index = names.index(name)
                if selected == "3":
                    config["models"][index] = _model(config["models"][index]["role"],
                                                      config["models"][index])
                else:
                    config["models"].pop(index)
            elif selected == "5":
                break
            else:
                raise ValueError("Choose 1, 2, 3, 4, or 5")
    _validate_config(config)
    if any(item.get("api_key") for item in config["models"]) and ".anybench" not in path.parts:
        raise ValueError("Save literal API keys only in a private .anybench directory")
    if ".anybench" in path.parts:
        _ignore_local_state(path)
    private_json(path, config)
    path.chmod(0o600)
    print(f"Saved private configuration to {path}")
    return config


def _role_files(config: dict, session: Path) -> dict[str, Path]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for index, entry in enumerate(config["models"]):
        data = _public_model(entry)
        if "api_key_env" not in data:
            name = f"ANYBENCH_SESSION_KEY_{index}"
            value = entry.get("api_key") or getpass(f"API key for {entry['name']}: ")
            if not value:
                raise ValueError(f"API key required for {entry['name']}")
            os.environ[name] = value
            data["api_key_env"] = name
        elif not os.environ.get(data["api_key_env"]):
            raise ValueError(f"Missing credential environment variable: {data['api_key_env']}")
        grouped[entry["role"]].append(data)
    result = {}
    for role, models in grouped.items():
        path = session / f"{role}.json"
        private_json(path, models)
        result[role] = path
    return result


def _prerequisites(repositories: list[str]) -> None:
    for command in ("git", "docker"):
        if shutil.which(command) is None:
            raise ValueError(f"{command} is required; install it before running AnyBench")
    daemon = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                            capture_output=True, text=True, timeout=30)
    if daemon.returncode:
        raise ValueError("Docker daemon is unavailable: " + daemon.stderr.strip()[:300])
    for spec in repositories:
        try:
            with _repository(spec) as repo:
                if not git(repo, "rev-parse", "HEAD").strip():
                    raise ValueError(f"Repository has no commits: {spec}")
        except subprocess.CalledProcessError as exc:
            raise ValueError(f"Cannot read Git history for {spec}: {exc.stderr[:300]}") from exc


def _case_context(repository: str, base: str) -> str:
    with _repository(repository) as repo:
        files = git(repo, "ls-tree", "-r", "--name-only", base).splitlines()
        chosen = [name for name in files if name in MANIFEST_NAMES or
                  name.startswith(".github/workflows/") and name.endswith((".yml", ".yaml"))][:25]
        parts = []
        for name in chosen:
            try:
                content = git(repo, "show", f"{base}:{name}")
            except subprocess.CalledProcessError:
                continue
            parts.append(f"{name}:\n{content[:5000]}")
        return "\n\n".join(parts)[:30000]


def _recipe(client: ChatClient, context: str, error: str = "", previous: list[str] | None = None) -> list[str]:
    prompt = ("Prepare an offline-capable Docker test environment for a Git repository. "
              "The image starts FROM python:3.12-slim and already has git, jq, grep and coreutils. "
              "Return JSON only: {\"commands\":[\"one shell command per item\"]}. "
              "Install the language runtime and public test dependencies needed by the repository. "
              "The build context contains only the Dockerfile: inline dependency names and "
              "do not refer to repository files, COPY, or ADD. "
              "No repository source or API credentials may be placed in the image. "
              "Use at most 12 bounded, noninteractive commands. Do not start services. "
              "Repository manifests and CI at the pre-change revision:\n" + context)
    if error:
        prompt += "\nPrevious commands: " + json.dumps(previous) + "\nFailure: " + error[:3000]
    result = parse_json_object(client.complete([{"role": "user", "content": prompt}]).message.get("content") or "")
    commands = result.get("commands")
    if not isinstance(commands, list) or len(commands) > 12 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 2000 or "\n" in item
            for item in commands):
        raise ValueError("Builder returned an invalid Docker setup recipe")
    return commands


def _dockerfile(commands: list[str]) -> str:
    lines = [f"FROM {BASE_IMAGE}",
             "RUN apt-get update && apt-get install -y --no-install-recommends git jq grep coreutils && rm -rf /var/lib/apt/lists/*"]
    lines.extend("RUN /bin/sh -lc " + shlex.quote(command) for command in commands)
    return "\n".join(lines) + "\n"


def _build_image(tag: str, recipe: Path) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="anybench-image-") as temp:
        (Path(temp) / "Dockerfile").write_text(recipe.read_text())
        result = subprocess.run(["docker", "build", "-t", tag, temp],
                                capture_output=True, text=True, timeout=1800)
    output = (result.stdout + result.stderr)[-12000:]
    return result.returncode == 0, output


def _image_id(tag: str) -> str:
    result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                            capture_output=True, text=True, timeout=30)
    return result.stdout.strip() if result.returncode == 0 else ""


def _prepare_image(repository: str, cases: list, builder: ModelConfig, session: Path,
                   repair_error: str = "") -> tuple[str | None, str]:
    key = fingerprint(repository)[:12]
    state_path = session / f"image-{key}.json"
    recipe_path = session / f"image-{key}.Dockerfile"
    tag = f"anybench-guided:{key}-{fingerprint([item.base_commit for item in cases])[:12]}"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get("phase", "").endswith("pending"):
        raise ValueError(f"Environment builder call for {repository} has unknown billing state; start a new session to retry")
    if state.get("phase") == "failed":
        return None, state.get("error", "Environment setup failed")
    if state.get("phase") == "built" and not repair_error:
        if _image_id(tag) != state["image_id"]:
            raise ValueError(f"Saved Docker image for {repository} changed or disappeared; start a new session")
        return tag, ""
    if repair_error and state.get("repaired"):
        return None, repair_error
    if state.get("phase") == "recipe" and not repair_error:
        commands = state["commands"]
    else:
        context = _case_context(repository, cases[0].base_commit)
        hints = [{"test_command": item.test_command[:500],
                  "changed_paths": re.findall(r"^diff --git a/(.*?) b/", item.gold_diff,
                                              re.MULTILINE)[:12]}
                 for item in cases[:8]]
        context += "\n\nVerification command and language hints:\n" + json.dumps(hints)
        old_commands = state.get("commands", [])
        phase = "repair_pending" if repair_error or state.get("phase") == "build_failed" else "planning_pending"
        private_json(state_path, {**state, "phase": phase})
        client = ChatClient(builder)
        commands = _recipe(client, context, repair_error or state.get("error", ""), old_commands)
        for command in commands:
            if any(value and value in command for value in (os.environ.get(builder.api_key_env),)):
                raise ValueError("Builder recipe contains a credential")
        recipe_path.write_text(_dockerfile(commands))
        recipe_path.chmod(0o600)
        state = {"phase": "recipe", "commands": commands, "repaired": phase == "repair_pending",
                 "prior_image_id": state.get("image_id", "")}
        private_json(state_path, state)
    ok, output = _build_image(tag, recipe_path)
    if not ok and not state["repaired"]:
        private_json(state_path, {**state, "phase": "build_failed", "error": redact(output, builder)})
        return _prepare_image(repository, cases, builder, session)
    if not ok:
        error = redact(output, builder)[-1000:]
        if state.get("prior_image_id") and _image_id(tag) == state["prior_image_id"]:
            private_json(state_path, {**state, "phase": "built", "error": error,
                                      "image_id": state["prior_image_id"]})
            return tag, error
        private_json(state_path, {**state, "phase": "failed", "error": error})
        return None, error
    private_json(state_path, {**state, "phase": "built", "image_id": _image_id(tag)})
    return tag, ""


def _setup_failure(outcomes: list[dict]) -> str:
    for outcome in outcomes:
        if "test validation error" in outcome.get("reason", ""):
            return outcome["reason"]
        for trial in outcome.get("trials", []):
            for side in ("base", "reference"):
                if trial.get(side, {}).get("status") in {"setup_error", "execution_error"}:
                    return str(trial[side])[:1500]
    return ""


def _diagnostic_report(session: Path, diagnostics: list[dict]) -> Path:
    path = session / "report.html"
    rows = "".join("<tr><td>" + escape(item.get("repository", "")) + "</td><td>" +
                   escape(item.get("case_id", "")) + "</td><td>" +
                   escape(item.get("status", "")) + "</td><td>" +
                   escape(item.get("reason", "")) + "</td></tr>" for item in diagnostics)
    path.write_text("<!doctype html><meta charset='utf-8'><title>AnyBench setup report</title>"
                    "<h1>No verified problems yet</h1><p>Candidate models were not run.</p>"
                    "<table border='1'><tr><th>Repository</th><th>Problem</th><th>Status</th><th>Reason</th></tr>"
                    + rows + "</table>")
    path.chmod(0o600)
    return path


def start(repositories: list[str], config_path: Path = DEFAULT_CONFIG,
          commits: int | None = None, max_problems: int | None = None,
          resume: Path | None = None) -> Path:
    if commits is not None and commits < 1 or max_problems is not None and max_problems < 1:
        raise ValueError("--commits and --max-problems must be positive")
    config_path = config_path.expanduser()
    if resume:
        resume = resume.expanduser()
    if resume and config_path == DEFAULT_CONFIG:
        saved = json.loads((resume.resolve() / "session.json").read_text())
        config_path = Path(saved["config_path"])
    if not config_path.exists() and resume is None:
        config = configure(config_path, repositories)
    else:
        config = json.loads(config_path.read_text())
    _validate_config(config)
    if any(item.get("api_key") for item in config["models"]):
        if ".anybench" not in config_path.parts:
            raise ValueError("Literal API keys require a private .anybench config path")
        _ignore_local_state(config_path)
        config_path.chmod(0o600)
    if resume:
        session = resume.resolve()
        settings = json.loads((session / "session.json").read_text())
        if repositories or commits is not None or max_problems is not None:
            raise ValueError("Resume uses frozen repositories and limits; omit those options")
        if settings["models_sha256"] != fingerprint([_public_model(item) for item in config["models"]]):
            raise ValueError("Model settings changed since this session started")
        repositories = settings["repositories"]
        commits = settings["commits"]
        max_problems = settings["max_problems"]
    else:
        repositories = repositories or config["repositories"]
        if not repositories:
            raise ValueError("Provide at least one repository")
        commits = commits or 50
        session = Path(".anybench/runs") / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        _ignore_local_state(session)
        ensure_private_parent(session / "session.json")
        session.mkdir(parents=True, mode=0o700, exist_ok=True)
    _prerequisites(repositories)
    role_files = _role_files(config, session)
    if not resume:
        print(f"\nRepositories: {len(repositories)}; up to {commits} commits each")
        print(f"Candidate models: {sum(item['role'] == 'candidate' for item in config['models'])}; "
              f"maximum problems: {max_problems or 'all verified'}")
        print("Builder and candidate calls may incur provider charges. Docker setup may download public dependencies.")
        if _ask("Start benchmark? (y/N)", "n").lower() != "y":
            raise ValueError("Benchmark cancelled before paid model calls")
        private_json(session / "session.json", {"repositories": repositories, "commits": commits,
                     "max_problems": max_problems,
                     "config_path": str(config_path.resolve()),
                     "models_sha256": fingerprint([_public_model(item) for item in config["models"]])})
    from .cli import main
    cases_path = session / "cases.csv"
    if not cases_path.exists() or (session / "cases.csv.manifest.json").exists() and not (session / "build.done").exists():
        args = ["build", *repositories, "--models", str(role_files["builder"]), "--builder",
                next(item["name"] for item in config["models"] if item["role"] == "builder"),
                "--commits", str(commits), "--output", str(cases_path)]
        if cases_path.exists():
            args.append("--resume")
        main(args)
        (session / "build.done").touch(mode=0o600)
    cases = read_cases(cases_path)
    diagnostics: list[dict] = []
    verified_path = session / "verified.csv"
    image_map_path = session / "images.json"
    if not verified_path.exists():
        by_repo: dict[str, list] = defaultdict(list)
        for case in cases:
            by_repo[case.repository].append(case)
        builder = ModelConfig(**json.loads(role_files["builder"].read_text())[0])
        images: dict[str, str] = {}
        outcomes: list[dict] = []
        for repository, repo_cases in by_repo.items():
            print(f"Preparing test environment for {repository}")
            image, error = _prepare_image(repository, repo_cases, builder, session)
            if image is None:
                outcomes.extend({"repository": repository, "case_id": case.case_id,
                                 "status": "skipped", "reason": "Environment setup failed: " + error}
                                for case in repo_cases)
                continue
            results = validation_results(repo_cases, image, {repository: image})
            failure = _setup_failure(results)
            if failure:
                repaired, error = _prepare_image(repository, repo_cases, builder, session, failure)
                if repaired and not error:
                    image = repaired
                    results = validation_results(repo_cases, image, {repository: image})
                else:
                    for item in results:
                        if item["status"] == "invalid" and _setup_failure([item]):
                            item["reason"] += "; environment repair failed: " + error
            images[repository] = image
            outcomes.extend({"repository": repository, **item} for item in results)
        write_metadata(cases, cases_path)
        private_json(session / "validation.json", outcomes)
        private_json(image_map_path, images)
        selected = {item["case_id"] for item in outcomes if item["status"] == "verified"}
        chosen = [case for case in cases if case.case_id in selected]
        if max_problems is not None:
            chosen = chosen[:max_problems]
        write_cases(verified_path, chosen)
        diagnostics = outcomes
    else:
        diagnostics = json.loads((session / "validation.json").read_text())
    verified = read_cases(verified_path)
    if not verified:
        report_path = _diagnostic_report(session, diagnostics or [{"status": "skipped", "reason": "No suitable historical commits"}])
        print(f"No verified problems. Diagnostic report: {report_path}")
        return report_path
    attempts = session / "attempts.jsonl"
    args = ["run", str(verified_path), "--models", str(role_files["candidate"]),
            "--image-map", str(image_map_path), "--output", str(attempts)]
    if attempts.exists():
        args.append("--resume")
    main(args)
    results = attempts
    if "judge" in role_files:
        results = session / "scored.jsonl"
        args = ["evaluate", str(verified_path), str(attempts), "--models", str(role_files["judge"]),
                "--judge", next(item["name"] for item in config["models"] if item["role"] == "judge"),
                "--output", str(results)]
        if results.exists():
            args.append("--resume")
        main(args)
    report_path = session / "report.html"
    main(["report", str(results), "--dataset", str(verified_path), "--output", str(report_path)])
    print(f"Verified {len(verified)} problems. Report: {report_path}")
    return report_path
