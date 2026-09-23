from __future__ import annotations

import argparse
import json
from pathlib import Path

from .dataset import build_dataset, validate_dataset
from .evaluate import evaluate, validate_test_commands
from .llm import ChatClient
from .model import (ModelConfig, append_case, append_jsonl, read_cases, read_jsonl,
                    write_cases, write_jsonl)
from .report import report
from .runner import preflight_run, run_sweep
from .sandbox import validate_size


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="anybench")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Generate cases from historical commits")
    build.add_argument("repositories", nargs="+")
    build.add_argument("--models", type=Path, required=True)
    build.add_argument("--builder", required=True, help="Model name from --models")
    build.add_argument("--commits", type=int, default=50)
    build.add_argument("--max-cases", type=int)
    build.add_argument("--resume", action="store_true", help="Append new cases to an existing CSV")
    build.add_argument("--output", type=Path, required=True)
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
    score = commands.add_parser("evaluate", help="Judge patches and preserve test results")
    score.add_argument("dataset", type=Path)
    score.add_argument("results", type=Path)
    score.add_argument("--models", type=Path)
    score.add_argument("--judge", help="Model name from --models")
    score.add_argument("--output", type=Path, required=True)
    chart = commands.add_parser("report", help="Create a standalone HTML report")
    chart.add_argument("results", type=Path)
    chart.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "build":
        config = next(c for c in _configs(args.models) if c.name == args.builder)
        existing = read_cases(args.output) if args.resume and args.output.exists() else []
        if not args.resume or not args.output.exists():
            write_cases(args.output, [])
        cases = build_dataset(args.repositories, ChatClient(config), args.commits,
                              args.max_cases, existing, lambda case: append_case(args.output, case))
        print(f"Wrote {len(cases) - len(existing)} new cases ({len(cases)} total) to {args.output}")
    elif args.command == "validate":
        cases = read_cases(args.dataset)
        errors = validate_dataset(cases)
        if args.check_tests and not errors:
            image_map = _image_map(args.image_map)
            errors.extend(validate_test_commands(cases, args.image, image_map,
                                                 args.workspace_size, args.memory))
        if errors:
            parser.exit(1, "\n".join(errors) + "\n")
        print(f"Validated {len(cases)} cases")
    elif args.command == "run":
        levels = args.concurrencies if args.concurrencies else [args.concurrency]
        image_map = _image_map(args.image_map)
        if args.dataset.resolve() == args.output.resolve():
            parser.error("dataset and output paths must differ")
        if not levels or any(level < 1 for level in levels):
            parser.error("concurrency levels must be positive")
        if args.max_steps < 1:
            parser.error("max-steps must be positive")
        cases = read_cases(args.dataset)
        configs = _configs(args.models)
        if not cases:
            parser.error("Dataset contains no cases")
        if not configs:
            parser.error("Model config contains no candidate models")
        try:
            preflight_run(cases, configs, args.image, image_map)
        except (RuntimeError, ValueError, OSError) as exc:
            parser.exit(2, f"Preflight failed: {exc}\n")
        write_jsonl(args.output, [])
        records = run_sweep(cases, configs, levels,
                            args.attempts, args.image, args.max_steps, image_map,
                            lambda record: append_jsonl(args.output, record),
                            args.workspace_size, args.memory)
        print(f"Wrote {len(records)} attempts to {args.output}")
    elif args.command == "evaluate":
        configs = _configs(args.models) if args.models else []
        client = ChatClient(next(c for c in configs if c.name == args.judge)) if args.judge else None
        records = evaluate(read_jsonl(args.results), read_cases(args.dataset), client)
        write_jsonl(args.output, records)
        print(f"Wrote {len(records)} evaluated attempts to {args.output}")
    elif args.command == "report":
        report(read_jsonl(args.results), args.output)
        print(f"Wrote report to {args.output}")


if __name__ == "__main__":
    main()
