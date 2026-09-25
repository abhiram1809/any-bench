from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
import os
import shutil
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

from .author import commit_contexts, import_annotations
from .dataset import _repository, build_dataset, git, validate_dataset
from .evaluate import evaluate, validation_results
from .llm import ChatClient
from .model import (Case, ModelConfig, RunRecord, append_case, append_jsonl, read_cases, read_jsonl,
                    write_cases, write_jsonl)
from .report import report
from .runner import preflight_run, run_sweep
from .sandbox import validate_size
from .metadata import case_metadata, write_metadata, verification_identity
from .harness import PROXY_IMAGE
from .skill_install import install_skill
from .workflow import (append_dict_jsonl, ensure_private_parent, fingerprint, manifest_path, prepare_output,
                       private_json, read_complete_jsonl, record_key)


def _configs(path: Path) -> list[ModelConfig]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Model config must be a JSON array")
    configs = [ModelConfig(**item) for item in data]
    if len({config.name for config in configs}) != len(configs):
        raise ValueError("Model names must be unique")
    return configs


def _image_map(path: Path | None) -> dict[str, str] | None:
    if path is None:
        return None
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                         for k, v in data.items()):
        raise ValueError("Image map must be a JSON object of repository to image")
    return data


def _jsonl_dicts(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _distinct_paths(*paths: Path | None) -> bool:
    selected = [path.resolve() for path in paths if path is not None]
    return len(selected) == len(set(selected))


def _frozen_revisions(repositories: list[str], commits: int, args=None) -> dict[str, list[str]]:
    revisions = {}
    with ExitStack() as stack:
        for spec in repositories:
            repo = stack.enter_context(_repository(spec))
            source = str(repo.resolve()) if Path(spec).expanduser().exists() else spec
            options = []
            if args is not None:
                options += [f"--since={args.since}"] if args.since else []
                options += [f"--until={args.until}"] if args.until else []
            revisions[source] = git(repo, "log", f"-{commits}", "--first-parent", *options,
                                    "--format=%H", args.revision if args else "HEAD",
                                    "--", *((args.paths or []) if args else [])).splitlines()
    return revisions


def _image_ids(images: set[str]) -> dict[str, str]:
    ids = {}
    for image in sorted(images):
        result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError(f"Image unavailable: {image}")
        ids[image] = result.stdout.strip()
    return ids


def _doctor(models: Path | None, image: str, output_dir: Path | None) -> dict:
    checks = {}
    checks["python"] = {"ok": sys.version_info >= (3, 11), "version": sys.version.split()[0]}
    for executable in ("git", "docker"):
        checks[executable] = {"ok": shutil.which(executable) is not None}
    if checks["docker"]["ok"]:
        result = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                                capture_output=True, text=True, timeout=30)
        checks["docker_daemon"] = {"ok": result.returncode == 0,
                                    "version": result.stdout.strip() if result.returncode == 0 else ""}
        found = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                               capture_output=True, text=True, timeout=30)
        checks["image"] = {"ok": found.returncode == 0, "name": image,
                            "id": found.stdout.strip() if found.returncode == 0 else ""}
    else:
        checks["docker_daemon"] = {"ok": False}
        checks["image"] = {"ok": False, "name": image}
    if models is not None:
        try:
            configs = _configs(models)
            checks["model_config"] = {"ok": bool(configs), "names": [c.name for c in configs]}
            required = sorted({name for c in configs for name in
                               ([c.api_key_env] if c.api_key_env else []) + list(c.env.values())})
            checks["credentials"] = {"ok": all(os.environ.get(k) for k in required),
                                      "variables": {k: bool(os.environ.get(k)) for k in required}}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            checks["model_config"] = {"ok": False, "error": str(exc)}
    if output_dir is not None:
        parent = output_dir
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        checks["output_dir"] = {"ok": parent.is_dir() and os.access(parent, os.W_OK),
                                "path": str(output_dir.resolve())}
    return {"ok": all(item["ok"] for item in checks.values()), "checks": checks}


def _summary(records: list, cases: list | None = None) -> dict:
    from .metrics import summary
    return summary(records, cases)


def _main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="anybench")
    commands = parser.add_subparsers(dest="command", required=True)
    guided = commands.add_parser("start", help="Set up and run a benchmark from repositories")
    guided.add_argument("repositories", nargs="*")
    guided.add_argument("--config", type=Path, default=Path(".anybench/config.json"))
    guided.add_argument("--commits", type=int)
    guided.add_argument("--max-problems", type=int)
    guided.add_argument("--resume", type=Path, metavar="SESSION")
    setup = commands.add_parser("configure", help="Add or edit repositories and model endpoints")
    setup.add_argument("repositories", nargs="*")
    setup.add_argument("--config", type=Path, default=Path(".anybench/config.json"))
    build = commands.add_parser("build", help="Generate cases from historical commits")
    build.add_argument("repositories", nargs="+")
    build.add_argument("--models", type=Path, required=True)
    build.add_argument("--builder", required=True, help="Model name from --models")
    build.add_argument("--commits", type=int, default=50)
    build.add_argument("--max-cases", type=int)
    build.add_argument("--resume", action="store_true", help="Append new cases to an existing CSV")
    build.add_argument("--overwrite", action="store_true")
    build.add_argument("--output", type=Path, required=True)
    prepare = commands.add_parser("prepare", help="Export Git commit contexts for host-agent case authoring")
    prepare.add_argument("repositories", nargs="+")
    prepare.add_argument("--commits", type=int, default=50)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--overwrite", action="store_true")
    author = commands.add_parser("import", help="Import host-authored annotations as Git-derived cases")
    author.add_argument("contexts", type=Path)
    author.add_argument("annotations", type=Path)
    author.add_argument("--output", type=Path, required=True)
    author.add_argument("--overwrite", action="store_true")
    check = commands.add_parser("validate", help="Verify dataset rows against Git history")
    check.add_argument("dataset", type=Path)
    check.add_argument("--check-tests", action="store_true",
                       help="Check each test fails on base and passes on gold in Docker")
    check.add_argument("--image", default="anybench-sandbox:latest")
    check.add_argument("--image-map", type=Path)
    check.add_argument("--workspace-size", type=validate_size, default="512m",
                       help="Editable checkout limit (default: 512m)")
    check.add_argument("--memory", type=validate_size, default="1g",
                       help="Container memory limit (default: 1g)")
    check.add_argument("--json-output", type=Path, help="Write per-case verification outcomes")
    check.add_argument("--verified-output", type=Path, help="Write only locally verified cases")
    check.add_argument("--max-verified", type=int, default=None,
                       help="Maximum verified cases in output (default: all)")
    check.add_argument("--overwrite", action="store_true")
    run = commands.add_parser("run", help="Run cases in isolated containers")
    run.add_argument("dataset", type=Path)
    run.add_argument("--models", type=Path, required=True)
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument("--concurrencies", type=int, nargs="+",
                     help="Run a sequential concurrency sweep, for example 1 2 4")
    run.add_argument("--attempts", type=int, default=1)
    run.add_argument("--image", default="anybench-sandbox:latest")
    run.add_argument("--image-map", type=Path,
                     help="JSON object mapping dataset repository values to Docker images")
    run.add_argument("--workspace-size", type=validate_size, default="512m",
                     help="Editable checkout limit (default: 512m)")
    run.add_argument("--memory", type=validate_size, default="1g",
                     help="Container memory limit (default: 1g)")
    run.add_argument("--max-steps", type=int, default=30,
                     help="Shared model-call budget for enhanced runs; main-loop steps for legacy (default: 30)")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--artifact-dir", type=Path)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--overwrite", action="store_true")
    score = commands.add_parser("evaluate", help="Judge patches and preserve test results")
    score.add_argument("dataset", type=Path)
    score.add_argument("results", type=Path)
    score.add_argument("--models", type=Path)
    score.add_argument("--judge", help="Model name from --models")
    score.add_argument("--output", type=Path, required=True)
    score.add_argument("--resume", action="store_true")
    score.add_argument("--overwrite", action="store_true")
    chart = commands.add_parser("report", help="Create a standalone HTML report")
    chart.add_argument("results", type=Path)
    chart.add_argument("--output", type=Path, required=True)
    summary = commands.add_parser("summary", help="Summarize saved attempt outcomes as JSON")
    summary.add_argument("results", type=Path)
    summary.add_argument("--dataset", type=Path)
    summary.add_argument("--output", type=Path)
    summary.add_argument("--json", action="store_true")
    doctor = commands.add_parser("doctor", help="Check prerequisites without paid model calls")
    doctor.add_argument("--models", type=Path)
    doctor.add_argument("--image", default="anybench-sandbox:latest")
    doctor.add_argument("--output-dir", type=Path)
    doctor.add_argument("--json", action="store_true")
    skill = commands.add_parser("skill", help="Install the portable AnyBench agent skill")
    skill_sub = skill.add_subparsers(dest="skill_command", required=True)
    install = skill_sub.add_parser("install")
    install.add_argument("--agent", choices=["claude", "portable"], required=True)
    install.add_argument("--scope", choices=["user", "project"], default="project")
    install.add_argument("--project", type=Path)
    install.add_argument("--home", type=Path)
    install.add_argument("--overwrite", action="store_true")
    from .commands import add_options, dispatch
    add_options(commands, build, prepare, author, check, run, chart, summary)
    args = parser.parse_args(argv)
    if dispatch(args, parser):
        return
    if args.command == "configure":
        from .guided import configure
        configure(args.config, args.repositories)
    elif args.command == "start":
        from .guided import start
        start(args.repositories, args.config, args.commits, args.max_problems, args.resume)
    elif args.command == "build":
        if not _distinct_paths(args.models, args.output):
            parser.error("Build model config and output paths must differ")
        configs = _configs(args.models)
        config = next((c for c in configs if c.name == args.builder), None)
        if config is None:
            parser.error(f"Builder {args.builder} is absent from --models")
        if config.role == "judge":
            parser.error("A judge role cannot author cases")
        if args.commits < 1 or args.max_cases is not None and args.max_cases < 1:
            parser.error("commits and max-cases must be positive")
        if config.api_key_env and not os.environ.get(config.api_key_env):
            parser.error(f"Missing builder credential variable: {config.api_key_env}")
        if args.resume:
            manifest = manifest_path(args.output)
            if not manifest.exists():
                parser.error("Resume requires an existing build manifest")
            old_inputs = json.loads(manifest.read_text())["inputs"]
            revisions = old_inputs["revisions"]
        else:
            revisions = _frozen_revisions(args.repositories, args.commits, args)
        inputs = {"kind": "build", "repositories": args.repositories, "builder": asdict(config),
                  "commits": args.commits, "max_cases": args.max_cases,
                  "revisions": revisions, "selection": {"revision": args.revision,
                  "since": args.since, "until": args.until, "paths": args.paths,
                  "seed": args.seed, "max_patch_bytes": args.max_patch_bytes}}
        try:
            resumed = prepare_output(args.output, inputs, resume=args.resume,
                                     overwrite=args.overwrite)
        except ValueError as exc:
            parser.error(str(exc))
        journal = args.output.with_name(args.output.name + ".journal.jsonl")
        events = args.output.with_name(args.output.name + ".events.jsonl")
        if not resumed:
            journal.unlink(missing_ok=True)
            events.unlink(missing_ok=True)
            write_cases(args.output, [])
        existing = read_cases(args.output) if resumed else []
        event_log = read_complete_jsonl(events, lambda item: item, repair=True) if resumed and events.exists() else []
        terminal = {(item["repository"], item["commit"]): item for item in event_log
                    if item["event"] in {"commit_completed", "commit_error"}}
        started = {(item["repository"], item["commit"]) for item in event_log if item["event"] == "commit_started"}
        if started - terminal.keys():
            parser.error("Interrupted builder call has unknown billing state; use a new output for a deliberate retry")
        saved = {(case.repository, case.target_commit) for case in existing}
        for key, event in terminal.items():
            if event.get("case") and key not in saved:
                existing.append(Case(**event["case"]))
                saved.add(key)
        if resumed and terminal:
            write_cases(args.output, existing)
        decisions = read_complete_jsonl(journal, lambda item: item, repair=True) if resumed and journal.exists() else []
        accepted = {(case.repository, case.target_commit) for case in existing}
        if any((item["repository"], item["target_commit"]) not in accepted
               for item in decisions if item["accepted"]):
            parser.error("Build journal records a missing accepted case")
        completed = {(item["repository"], item["target_commit"]) for item in decisions}
        completed.update(terminal)
        cases = build_dataset(args.repositories, ChatClient(config), args.commits,
                              args.max_cases, existing, lambda case: append_case(args.output, case),
                              lambda source, commit, accepted: append_dict_jsonl(journal,
                                  {"repository": source, "target_commit": commit,
                                   "accepted": accepted}), completed, revisions, args.max_patch_bytes,
                              lambda event: append_dict_jsonl(events, event))
        print(f"Wrote {len(cases) - len(existing)} new cases ({len(cases)} total) to {args.output}")
    elif args.command == "prepare":
        if args.commits < 1:
            parser.error("commits must be positive")
        contexts = commit_contexts(args.repositories, args.commits,
                                   revisions=_frozen_revisions(args.repositories, args.commits, args),
                                   max_patch_bytes=args.max_patch_bytes, seed=args.seed)
        if args.output.exists() and not args.overwrite:
            parser.error("Output exists; use --overwrite")
        ensure_private_parent(args.output)
        args.output.unlink(missing_ok=True)
        for item in contexts:
            append_dict_jsonl(args.output, item)
        if not args.output.exists():
            args.output.touch(mode=0o600)
        print(f"Exported {len(contexts)} commit contexts to {args.output}")
    elif args.command == "import":
        if not _distinct_paths(args.contexts, args.annotations, args.output):
            parser.error("Import input and output paths must differ")
        if args.output.exists() and not args.overwrite:
            parser.error("Output exists; use --overwrite")
        contexts = _jsonl_dicts(args.contexts)
        annotations = _jsonl_dicts(args.annotations)
        cases, decisions = import_annotations(contexts, annotations)
        errors = validate_dataset(cases)
        if errors:
            parser.exit(1, "\n".join(errors) + "\n")
        ensure_private_parent(args.output)
        write_cases(args.output, cases)
        private_json(args.output.with_name(args.output.name + ".decisions.json"), decisions)
        print(f"Imported {len(cases)} cases; {len(decisions) - len(cases)} rejected")
    elif args.command == "validate":
        if args.max_verified is not None and args.max_verified < 1:
            parser.error("max-verified must be positive")
        if (args.json_output or args.verified_output) and not args.check_tests:
            parser.error("Structured validation outputs require --check-tests")
        destinations = [path for path in (args.json_output, args.verified_output) if path]
        if not _distinct_paths(args.dataset, args.image_map, *destinations):
            parser.error("Validation input and output paths must differ")
        if any(path.exists() for path in destinations) and not args.overwrite:
            parser.error("Validation output exists; use --overwrite")
        from .commands import runtime_cases
        cases = runtime_cases(read_cases(args.dataset), args.repo_map, args.environment_profiles)
        errors = validate_dataset(cases)
        selected_ids: list[str] = []
        if args.check_tests and not errors:
            image_map = _image_map(args.image_map)
            outcomes = validation_results(cases, args.image, image_map,
                                          args.workspace_size, args.memory)
            write_metadata(cases, args.dataset)
            errors.extend(f"{item['case_id']}: {item['reason']}" for item in outcomes
                          if item["status"] == "invalid")
            selected_ids = [item["case_id"] for item in outcomes
                            if item["status"] == "verified"][:args.max_verified]
            if args.json_output:
                private_json(args.json_output, {"cases": outcomes,
                             "verified": sum(item["status"] == "verified" for item in outcomes),
                             "invalid": sum(item["status"] == "invalid" for item in outcomes),
                             "skipped": sum(item["status"] == "skipped" for item in outcomes),
                             "selected_case_ids": selected_ids})
            if args.verified_output:
                verified = set(selected_ids)
                ensure_private_parent(args.verified_output)
                write_cases(args.verified_output, [case for case in cases
                                                   if case.case_id in verified])
        if errors:
            if args.check_tests and (args.json_output or args.verified_output) and selected_ids:
                print("\n".join(errors), file=sys.stderr)
                print(f"Selected {len(selected_ids)} verified cases; "
                      f"{len(errors)} invalid cases recorded")
                return
            parser.exit(1, "\n".join(errors) + "\n")
        print(f"Validated {len(cases)} cases")
    elif args.command == "run":
        from .commands import runtime_cases, provenance
        levels = args.concurrencies if args.concurrencies else [args.concurrency]
        image_map = _image_map(args.image_map)
        if not _distinct_paths(args.dataset, args.models, args.image_map, args.output):
            parser.error("Run input and output paths must differ")
        if not levels or any(level < 1 for level in levels) or len(set(levels)) != len(levels):
            parser.error("concurrency levels must be positive and unique")
        if args.attempts < 1:
            parser.error("attempts must be positive")
        if args.max_steps < 1:
            parser.error("max-steps must be positive")
        artifact_base = (args.artifact_dir or args.output.parent / "artifacts").resolve()
        if artifact_base == args.output.resolve() or artifact_base == args.dataset.resolve():
            parser.error("artifact directory cannot be an input or output file")
        cases = runtime_cases(read_cases(args.dataset), args.repo_map, args.environment_profiles)
        configs = _configs(args.models)
        if not cases:
            parser.error("Dataset contains no cases")
        if not configs:
            parser.error("Model config contains no candidate models")
        if any(config.role != "candidate" for config in configs):
            parser.error("run accepts only candidate role configs")
        if any(case_metadata(case).get("kind") == "group" and
               (case_metadata(case).get("validation", {}).get("status") != "verified" or
                case_metadata(case).get("validation", {}).get("inputs_sha256") != verification_identity(case))
               for case in cases):
            parser.error("Grouped cases require successful validate --check-tests before running")
        image_map = dict(image_map or {})
        for case in cases:
            selected = case_metadata(case).get("environment", {}).get("image")
            if selected:
                image_map[case.repository] = selected
        try:
            preflight_run(cases, configs, args.image, image_map)
            images = ({(image_map or {}).get(case.repository, args.image) for case in cases}
                      if any(config.harness == "anybench" for config in configs) else set())
            images.update(config.image for config in configs if config.harness != "anybench")
            if any(config.harness != "anybench" for config in configs):
                images.add(PROXY_IMAGE)
            image_ids = _image_ids(images)
            for case in cases:
                validation = case_metadata(case).get("validation", {})
                checked = validation.get("image_id")
                selected = image_map.get(case.repository, args.image)
                if (validation.get("status") == "verified" and checked and
                        image_ids.get(selected) != checked):
                    raise ValueError(f"{case.case_id}: evaluation image changed since validation")
        except (RuntimeError, ValueError, OSError) as exc:
            parser.exit(2, f"Preflight failed: {exc}\n")
        inputs = {"kind": "run", "cases": [asdict(case) for case in cases],
                  "configs": [asdict(config) for config in configs], "levels": levels,
                  "attempts": args.attempts, "image_ids": image_ids,
                  "image_map": image_map, "max_steps": args.max_steps,
                  "workspace_size": args.workspace_size, "memory": args.memory,
                  "artifact_base": str(artifact_base), "provenance": provenance(),
                  "case_metadata": {case.case_id: case_metadata(case) for case in cases},
                  "default_image": args.image}
        try:
            resumed = prepare_output(args.output, inputs, resume=args.resume,
                                     overwrite=args.overwrite)
        except ValueError as exc:
            parser.error(str(exc))
        if not resumed:
            write_jsonl(args.output, [])
        prior = read_complete_jsonl(args.output, lambda item: RunRecord(**item), repair=True) if resumed else []
        completed = {record_key(record) for record in prior}
        expected = {(case.case_id, config.name, level, attempt)
                    for case in cases for config in configs for level in levels
                    for attempt in range(1, args.attempts + 1)}
        if len(completed) != len(prior) or not completed <= expected:
            parser.error("Resume output has duplicate or unexpected attempts")
        pinned = [replace(config, image=image_ids.get(config.image, config.image)) for config in configs]
        for config in pinned:
            config._proxy_image = image_ids.get(PROXY_IMAGE, PROXY_IMAGE)
        pinned_map = {source: image_ids.get(selected, selected) for source, selected in image_map.items()}
        events = args.output.with_name(args.output.name + ".events.jsonl")
        if not resumed:
            events.unlink(missing_ok=True)
        append_dict_jsonl(events, {"event": "session_started", "time": __import__('time').time(),
                                  "remaining": len(expected - completed)})
        def save_record(record):
            append_jsonl(args.output, record)
            append_dict_jsonl(events, {"event": "attempt_completed", "key": record_key(record),
                                      "status": record.status, "time": record.finished_at})
            print(f"{record.model}: {record.case_id} #{record.attempt}: {record.status}", file=sys.stderr)
        new_records = run_sweep(cases, pinned, levels,
                            args.attempts, image_ids.get(args.image, args.image), args.max_steps, pinned_map,
                            save_record,
                            args.workspace_size, args.memory, completed, artifact_base)
        append_dict_jsonl(events, {"event": "session_finished", "time": __import__('time').time()})
        records = prior + new_records
        print(f"Wrote {len(records)} attempts to {args.output}")
    elif args.command == "evaluate":
        configs = _configs(args.models) if args.models else []
        if args.judge and not args.models:
            parser.error("--judge requires --models")
        selected = next((c for c in configs if c.name == args.judge), None)
        if args.judge and selected is None:
            parser.error(f"Judge {args.judge} is absent from --models")
        if selected and selected.role == "builder":
            parser.error("A builder role cannot judge attempts")
        if selected and selected.api_key_env and not os.environ.get(selected.api_key_env):
            parser.error(f"Missing judge credential variable: {selected.api_key_env}")
        if not _distinct_paths(args.dataset, args.results, args.models, args.output):
            parser.error("Evaluation input and output paths must differ")
        candidate_records = read_jsonl(args.results)
        cases = read_cases(args.dataset)
        by_id = {case.case_id: case for case in cases}
        if len(by_id) != len(cases) or any(record.case_id not in by_id for record in candidate_records):
            parser.error("Evaluation records do not match a unique dataset case")
        keys = [record_key(record) for record in candidate_records]
        if len(set(keys)) != len(keys):
            parser.error("Candidate attempt identities are duplicated")
        from .commands import load_manifest, provenance
        source_manifest = load_manifest(args.results)
        inputs = {"kind": "evaluate", "cases_sha256": fingerprint(cases),
                  "records_sha256": fingerprint(candidate_records),
                  "judge": asdict(selected) if selected else None,
                  "cases": [asdict(case) for case in cases],
                  "case_metadata": {case.case_id: case_metadata(case) for case in cases},
                  "run_inputs": source_manifest["inputs"] if source_manifest else None,
                  "provenance": provenance()}
        try:
            resumed = prepare_output(args.output, inputs, resume=args.resume,
                                     overwrite=args.overwrite)
        except ValueError as exc:
            parser.error(str(exc))
        if not resumed:
            write_jsonl(args.output, [])
        previous = read_complete_jsonl(args.output, lambda item: RunRecord(**item), repair=True) if resumed else []
        completed = {record_key(record) for record in previous}
        if len(completed) != len(previous) or not completed <= set(keys):
            parser.error("Resume output has duplicate or unexpected judged attempts")
        client = ChatClient(selected) if selected else None
        for record in candidate_records:
            if record_key(record) in completed:
                continue
            evaluated = evaluate([record], cases, client)[0]
            append_jsonl(args.output, evaluated)
        records = read_jsonl(args.output)
        print(f"Wrote {len(records)} evaluated attempts to {args.output}")
    elif args.command == "report":
        if not _distinct_paths(args.results, args.output):
            parser.error("Report input and output paths must differ")
        from .commands import load_manifest, read_rates, dataset_context
        metrics = report(read_jsonl(args.results), args.output,
                         cases=dataset_context(args.results, args.dataset),
                         manifest=load_manifest(args.results), rates=read_rates(args.rates),
                         aggregate_only=args.aggregate_only, include_private=args.include_private)
        if args.metrics_output:
            private_json(args.metrics_output, metrics)
        print(f"Wrote report to {args.output}")
    elif args.command == "summary":
        if not _distinct_paths(args.results, args.dataset, args.output):
            parser.error("Summary input and output paths must differ")
        from .commands import load_manifest, read_rates, dataset_context
        from .metrics import summary as summarize
        result = summarize(read_jsonl(args.results), dataset_context(args.results, args.dataset),
                           load_manifest(args.results), read_rates(args.rates))
        if args.output:
            private_json(args.output, result)
        print(json.dumps(result, indent=2))
    elif args.command == "doctor":
        result = _doctor(args.models, args.image, args.output_dir)
        print(json.dumps(result, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
    elif args.command == "skill" and args.skill_command == "install":
        try:
            destination, status = install_skill(args.agent, args.scope, args.project,
                                                args.home, args.overwrite)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"{status}: {destination}")


def main(argv: list[str] | None = None) -> None:
    from .commands import invoke
    try:
        invoke(list(sys.argv[1:] if argv is None else argv), _main)
    except (ValueError, OSError) as exc:
        print(f"anybench: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
