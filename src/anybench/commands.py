"""Optional workflow commands and compatibility-preserving CLI extensions."""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict, replace
import json
from pathlib import Path
import random
import subprocess
import time
import tomllib

from .dataset import _repository, analyze_commit
from .evaluate import validation_results
from .grouped import discover, import_groups
from .llm import ChatClient, parse_json_object
from .metadata import case_metadata
from .metrics import compare
from .model import ModelConfig, append_jsonl, read_cases, read_jsonl, write_cases, write_jsonl
from .repository import git
from .workflow import (append_dict_jsonl, fingerprint, manifest_path, output_lock,
                       prepare_output, private_json, reserved_paths)


def dataset_context(results: Path, dataset: Path | None = None) -> list | None:
    from .model import Case
    manifest = load_manifest(results)
    if dataset:
        selected = read_cases(dataset)
        if manifest and "cases" in manifest["inputs"]:
            if fingerprint(selected) != fingerprint(manifest["inputs"]["cases"]):
                raise ValueError("Report dataset differs from the experiment manifest")
            for case in selected:
                # Evaluation evidence comes from the frozen experiment, not later revalidation.
                case._metadata = manifest["inputs"].get("case_metadata", {}).get(case.case_id, {})
        return selected
    if not manifest or "cases" not in manifest["inputs"]:
        return None
    cases = [Case(**item) for item in manifest["inputs"]["cases"]]
    for case in cases:
        case._metadata = manifest["inputs"].get("case_metadata", {}).get(case.case_id, {})
    return cases


def provenance() -> dict:
    from . import __version__
    return {"anybench_version": __version__, "source_sha256": fingerprint({
        path.name: path.read_text() for path in sorted(Path(__file__).parent.glob("*.py"))}),
        "evaluator_version": "isolated-v1", "builder_prompt_version": "grouped-v1"}


def load_manifest(path: Path) -> dict | None:
    manifest = manifest_path(path)
    if not manifest.exists():
        return None
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if document["inputs"].get("kind") == "evaluate" and document["inputs"].get("run_inputs"):
        document["inputs"] = {**document["inputs"]["run_inputs"], "judge": document["inputs"].get("judge")}
    return document


def read_rates(path: Path | None) -> dict | None:
    if path is None:
        return None
    rates = json.loads(path.read_text())
    if not isinstance(rates, dict):
        raise ValueError("Rate card must map model IDs to per-million-token prices")
    for values in rates.values():
        if (not isinstance(values, dict) or not {"input", "output"} <= values.keys() or
                any(type(v) not in (int, float) or v < 0 for v in values.values())):
            raise ValueError("Rate entries require nonnegative input and output prices")
    return rates


def expand_config(argv: list[str]) -> list[str]:
    if "--config" not in argv:
        return argv
    index = argv.index("--config")
    path = Path(argv[index + 1]).resolve()
    argv = argv[:index] + argv[index + 2:]
    if not argv:
        raise ValueError("Specify a command with --config")
    document = tomllib.loads(path.read_text())
    options = document.get(argv[0], {})
    if not isinstance(options, dict):
        raise ValueError("Experiment command configuration must be a TOML table")
    positional = {"run": ["dataset"], "validate": ["dataset"], "report": ["results"],
                  "summary": ["results"], "evaluate": ["dataset", "results"],
                  "build": ["repositories"], "prepare": ["repositories"]}.get(argv[0], [])
    paths = {"models", "output", "image_map", "artifact_dir", "dataset", "results", "rates",
             "repo_map", "environment_profiles", "json_output", "verified_output", "metrics_output"}
    defaults, positionals = [], []
    for key, value in options.items():
        flag = "--" + key.replace("_", "-")
        if flag in argv:
            continue
        values = value if isinstance(value, list) else [value]
        if key in paths:
            values = [str((path.parent / str(item)).resolve()) for item in values]
        if key in positional:
            positionals.extend(map(str, values))
        elif value is True:
            defaults.append(flag)
        elif value is not False:
            defaults.extend([flag, *map(str, values)])
    # Explicit positional inputs replace config positionals; option values are skipped.
    explicit_positionals = bool(len(argv) > 1 and not argv[1].startswith("-"))
    return [argv[0], *(positionals if not explicit_positionals else []), *defaults, *argv[1:]]


def invoke(argv: list[str], callback) -> None:
    if not argv or argv[0] not in {"start", "configure"}:
        argv = expand_config(argv)
    outputs = []
    output_indices = set()
    for flag in ("--output", "--json-output", "--verified-output", "--metrics-output"):
        if flag in argv:
            index = argv.index(flag)
            if index + 1 < len(argv):
                outputs.append(Path(argv[index + 1]))
                output_indices.add(index + 1)
    reserved = set()
    for output in outputs:
        current = reserved_paths(output)
        if reserved & current:
            raise ValueError("Output and sidecar destinations overlap")
        reserved.update(current)
    for index, token in enumerate(argv):
        if token.startswith("-") or index in output_indices:
            continue
        path = Path(token).resolve()
        if path in reserved:
            raise ValueError("An input aliases a reserved output sidecar")
    with ExitStack() as stack:
        if argv and argv[0] == "validate" and len(argv) > 1 and not argv[1].startswith("-"):
            stack.enter_context(output_lock(Path(argv[1])))
        for output in sorted(outputs):
            stack.enter_context(output_lock(output))
        callback(argv)


def add_options(commands, build, prepare, author, check, run, chart, summary) -> None:
    for parser in (build, prepare):
        parser.add_argument("--grouped", action="store_true", help="Opt-in multi-commit task reconstruction")
        parser.add_argument("--revision", default="HEAD")
        parser.add_argument("--since")
        parser.add_argument("--until")
        parser.add_argument("--paths", nargs="*")
        parser.add_argument("--max-patch-bytes", type=int, default=500000)
        parser.add_argument("--seed", type=int, default=0)
    build.add_argument("--image", default="anybench-sandbox:latest")
    author.add_argument("--grouped", action="store_true")
    for parser in (run, check):
        parser.add_argument("--repo-map", type=Path)
        parser.add_argument("--environment-profiles", type=Path)
    for parser in (chart, summary):
        parser.add_argument("--rates", type=Path)
    chart.add_argument("--dataset", type=Path)
    chart.add_argument("--metrics-output", type=Path)
    chart.add_argument("--aggregate-only", action="store_true")
    chart.add_argument("--include-private", action="store_true")
    inspect = commands.add_parser("inspect", help="Inspect dataset quality and authoring decisions")
    inspect.add_argument("dataset", type=Path)
    inspect.add_argument("--output", type=Path)
    split = commands.add_parser("split", help="Split datasets without separating related tasks")
    split.add_argument("dataset", type=Path)
    split.add_argument("--output", type=Path, required=True, help="Output directory")
    split.add_argument("--seed", type=int, default=0)
    split.add_argument("--train-fraction", type=float, default=.8)
    split.add_argument("--overwrite", action="store_true")
    comparison = commands.add_parser("compare", help="Compare matching experiments and enforce regression limits")
    comparison.add_argument("baseline", type=Path)
    comparison.add_argument("candidate", type=Path)
    comparison.add_argument("--output", type=Path)
    comparison.add_argument("--max-regressions", type=int, default=0)
    comparison.add_argument("--max-accuracy-drop", type=float, default=0)
    retry = commands.add_parser("retry", help="Retry selected failures into a linked new experiment")
    retry.add_argument("results", type=Path)
    retry.add_argument("--output", type=Path, required=True)
    retry.add_argument("--status", nargs="+", choices=["error", "exhausted"], default=["error"])
    retry.add_argument("--overwrite", action="store_true")


def runtime_cases(cases: list, repo_map: Path | None, profiles: Path | None) -> list:
    aliases = json.loads(repo_map.read_text()) if repo_map else {}
    environments = json.loads(profiles.read_text()) if profiles else {}
    if not isinstance(aliases, dict) or not isinstance(environments, dict):
        raise ValueError("Repository aliases and environment profiles must be JSON objects")
    for case in cases:
        metadata = dict(case_metadata(case))
        if case.repository in aliases:
            value = aliases[case.repository]
            if not isinstance(value, str) or not Path(value).is_dir():
                raise ValueError("Repository alias must point to a local checkout")
            metadata["repository_alias"] = str(Path(value).resolve())
        if case.repository in environments:
            profile = environments[case.repository]
            if not isinstance(profile, dict) or set(profile) - {"image", "setup_check", "adapter", "test_command"}:
                raise ValueError("Unknown environment profile fields")
            if profile.get("adapter", "custom") not in {"python", "node", "custom"}:
                raise ValueError("Unknown test adapter")
            metadata["environment"] = profile
        case._metadata = metadata
    return cases


def propose_groups(client: ChatClient, contexts: list[dict]) -> list[dict]:
    from .context import estimate_tokens
    allowed = {(entry["repository"], entry["commit"]): entry for entry in contexts}
    tools = [{"type": "function", "function": {"name": "InspectHistory",
              "description": "Read a frozen commit patch or a file at its base/target, using bounded character ranges.",
              "parameters": {"type": "object", "properties": {
                  "repository": {"type": "string"}, "commit": {"type": "string"},
                  "file": {"type": "string"}, "base": {"type": "boolean"},
                  "start": {"type": "integer"}}, "required": ["repository", "commit"]}}}]
    overview = [{**entry, "message": entry.get("message", "")[:1000],
                 "patch_excerpt": entry.get("patch_excerpt", "")[:2000],
                 "changed_files": entry.get("changed_files", [])[:30],
                 "symbols": entry.get("symbols", [])[:10]} for entry in contexts]
    messages = [{"role": "user", "content":
        'Find bugs whose resolution spans one or several commits. Exclude unrelated changes. '
        'Return {"groups":[{"repository":...,"selected_commits":[oldest,...,newest],'
        '"evidence":[{"commit":...,"paths":[actual changed paths],"reason":causal evidence}],'
        '"problem_statement":behavior without solution,"hint":"",'
        '"regression_command":unchanged regression checks that pass at base and reference}]}. '
        'Use InspectHistory for truncated patches or missing context. Do not group by message similarity alone. '
        'A merge patch already contains its constituents. Do not include both. '
        'Do not invent a runnable unchanged regression check. Leave unsuitable commits unselected. '
        'These are bounded overviews; inspect the full patch and file contents before choosing groups.\n' + json.dumps(overview)}]
    budget = int(client.config.context_window_tokens * .9) - client.config.max_output_tokens
    for _ in range(20):
        if estimate_tokens([messages, tools]) > budget:
            raise ValueError("Builder context budget reached; reduce --commits or increase its context window")
        reply = client.complete(messages, tools)
        messages.append(reply.message)
        calls = reply.message.get("tool_calls") or []
        if not calls:
            groups = parse_json_object(reply.message.get("content") or "").get("groups")
            if not isinstance(groups, list):
                raise ValueError("Builder must return a groups array")
            return groups
        for call in calls:
            try:
                args = json.loads(call["function"]["arguments"])
                entry = allowed[(args["repository"], args["commit"])]
                start = args.get("start", 0)
                if type(start) is not int or start < 0:
                    raise ValueError("start must be a nonnegative character offset")
                with _repository(args["repository"]) as repo:
                    if args.get("file"):
                        path = Path(args["file"])
                        if path.is_absolute() or ".." in path.parts:
                            raise ValueError("Unsafe file path")
                        revision = entry["base_commit"] if args.get("base") else entry["commit"]
                        text = git(repo, "show", f"{revision}:{path}")
                    else:
                        text = git(repo, "diff", "--no-ext-diff", "--no-textconv", entry["base_commit"], entry["commit"], "--")
                result = f"Characters {start}:{start+20000} of {len(text)}\n{text[start:start+20000]}"
            except (ValueError, KeyError, subprocess.SubprocessError) as exc:
                result = f"Inspection error: {exc}"
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    raise ValueError("Grouping exceeded 20 model calls")


def author_tests(client: ChatClient, proposal: dict, contexts: list[dict]) -> dict:
    entry = next(item for item in contexts if item["repository"] == proposal["repository"]
                 and item["commit"] == proposal["selected_commits"][0])
    base = entry["base_commit"]
    with _repository(proposal["repository"]) as repo:
        prompt = ('Author independent regression tests for this behavior using only the base interfaces. '
                  'ReadRevision parent and target both expose the same pre-fix snapshot. '
                  'Return JSON with evaluation_files mapping relative filenames to test source, '
                  'and evaluation_command that executes these files under /evaluation with the '
                  'repository at /repo, emitting normal unittest, pytest, Node test, or assertion output. '
                  'Prefer extracting adequate historical tests visible at the base; otherwise generate them. '
                  'The filesystem is read-only except /tmp. Do not assume the solution.\nTask:\n' + proposal["problem_statement"])
        result = analyze_commit(client, repo, base, base, prompt)
    return {**proposal, "evaluation_files": result.get("evaluation_files", {}),
            "evaluation_command": result.get("evaluation_command", ""),
            "test_provenance": "independent-base-only-authoring"}


def grouped_build(args, parser) -> None:
    configs = [ModelConfig(**item) for item in json.loads(args.models.read_text())]
    config = next((item for item in configs if item.name == args.builder), None)
    if config is None or config.role == "judge" or config.harness != "anybench":
        parser.error("Grouped authoring requires a builder API configuration")
    context_path = args.output.with_name(args.output.name + ".contexts.json")
    proposal_path = args.output.with_name(args.output.name + ".proposals.json")
    event_path = args.output.with_name(args.output.name + ".events.jsonl")
    inputs = {"kind": "grouped-build", "repositories": args.repositories, "commits": args.commits,
              "revision": args.revision, "since": args.since, "until": args.until, "paths": args.paths,
              "max_patch_bytes": args.max_patch_bytes, "seed": args.seed, "max_cases": args.max_cases,
              "builder": asdict(config), "image": args.image, "provenance": provenance()}
    resumed = prepare_output(args.output, inputs, resume=args.resume, overwrite=args.overwrite)
    if not resumed:
        for path in (proposal_path, event_path):
            path.unlink(missing_ok=True)
        write_cases(args.output, [])
        contexts = discover(args.repositories, args.commits, args.revision, args.since, args.until,
                            args.paths, args.max_patch_bytes, args.seed)
        private_json(context_path, contexts)
    else:
        contexts = json.loads(context_path.read_text())
    if proposal_path.exists():
        state = json.loads(proposal_path.read_text())
    else:
        if event_path.exists():
            parser.error("Interrupted grouping call has unknown billing state; use a new output for a deliberate retry")
        append_dict_jsonl(event_path, {"event": "grouping_started", "time": time.time()})
        client = ChatClient(config)
        try:
            proposals = propose_groups(client, contexts)
        except Exception as exc:
            append_dict_jsonl(event_path, {"event": "grouping_failed", "error": str(exc),
                                          "prompt_tokens": client.prompt_tokens,
                                          "completion_tokens": client.completion_tokens})
            raise
        state = {"proposals": proposals, "authored": []}
        private_json(proposal_path, state)
        append_dict_jsonl(event_path, {"event": "grouping_completed", "prompt_tokens": client.prompt_tokens,
                                       "completion_tokens": client.completion_tokens})
    for index, proposal in enumerate(state["proposals"]):
        if index < len(state["authored"]):
            continue
        if state.get("pending_test_author") is not None:
            parser.error("Interrupted test-author call requires a deliberate new output; it will not be repaid silently")
        state["pending_test_author"] = index
        private_json(proposal_path, state)
        client = ChatClient(config)
        try:
            authored = author_tests(client, proposal, contexts)
        except Exception as exc:
            authored = {**proposal, "author_error": str(exc)}
        state["authored"].append(authored)
        state.pop("pending_test_author", None)
        private_json(proposal_path, state)
        append_dict_jsonl(event_path, {"event": "tests_authored", "proposal": index,
                                      "prompt_tokens": client.prompt_tokens, "completion_tokens": client.completion_tokens,
                                      "error": authored.get("author_error")})
    cases, decisions = import_groups(contexts, state["authored"])
    if args.max_cases:
        deferred = {case.case_id for case in cases[args.max_cases:]}
        for decision in decisions:
            if decision.get("case_id") in deferred:
                decision.update(accepted=False, reason="outside max_cases limit")
        cases = cases[:args.max_cases]
    outcomes = validation_results(cases, args.image)
    write_cases(args.output, cases)
    private_json(args.output.with_name(args.output.name + ".decisions.json"), decisions)
    private_json(args.output.with_name(args.output.name + ".validation.json"), outcomes)
    print(f"Reconstructed {len(cases)} tasks; {sum(item['status'] == 'verified' for item in outcomes)} verified")


def dispatch(args, parser) -> bool:
    if args.command in {"prepare", "build"} and args.grouped:
        if args.command == "build":
            grouped_build(args, parser)
        else:
            if args.output.exists() and not args.overwrite:
                parser.error("Output exists; use --overwrite")
            contexts = discover(args.repositories, args.commits, args.revision, args.since, args.until,
                                args.paths, args.max_patch_bytes, args.seed)
            args.output.unlink(missing_ok=True)
            for entry in contexts:
                append_dict_jsonl(args.output, entry)
            if not contexts:
                args.output.touch(mode=0o600)
            print(f"Exported {len(contexts)} frozen commit contexts")
        return True
    if args.command == "import" and args.grouped:
        if args.output.exists() and not args.overwrite:
            parser.error("Output exists; use --overwrite")
        contexts = [json.loads(line) for line in args.contexts.read_text().splitlines() if line.strip()]
        proposals = [json.loads(line) for line in args.annotations.read_text().splitlines() if line.strip()]
        cases, decisions = import_groups(contexts, proposals)
        write_cases(args.output, cases)
        private_json(args.output.with_name(args.output.name + ".decisions.json"), decisions)
        print(f"Reconstructed {len(cases)} tasks; run validate --check-tests before candidate execution")
        return True
    if args.command == "inspect":
        cases = read_cases(args.dataset)
        decisions_path = args.dataset.with_name(args.dataset.name + ".decisions.json")
        decisions = json.loads(decisions_path.read_text()) if decisions_path.exists() else []
        result = {"cases": len(cases), "repositories": dict(Counter(c.repository for c in cases)),
                  "grouped": sum(case_metadata(c).get("kind") == "group" for c in cases),
                  "local_tests": sum(bool(c.test_command) and not c.external_validation for c in cases),
                  "validation": dict(Counter(case_metadata(c).get("validation", {}).get("status", "unknown") for c in cases)),
                  "rejection_reasons": dict(Counter(d.get("reason", "unknown") for d in decisions if not d.get("accepted"))),
                  "authoring_decisions": len(decisions),
                  "acceptance_yield": sum(bool(d.get("accepted")) for d in decisions) / len(decisions) if decisions else None,
                  "provenance": {c.case_id: case_metadata(c) for c in cases}}
        if args.output:
            private_json(args.output, result)
        print(json.dumps(result, indent=2))
        return True
    if args.command == "split":
        if not 0 < args.train_fraction < 1:
            parser.error("train-fraction must be between zero and one")
        cases = read_cases(args.dataset)
        # Repository-level splits conservatively keep all related histories together.
        repositories = sorted({case.repository for case in cases})
        random.Random(args.seed).shuffle(repositories)
        if len(repositories) < 2:
            parser.error("At least two repositories are required for leakage-safe splits")
        cutoff = max(1, min(len(repositories) - 1, round(len(repositories) * args.train_fraction)))
        training = set(repositories[:cutoff])
        for name, subset in (("train", [c for c in cases if c.repository in training]),
                             ("test", [c for c in cases if c.repository not in training])):
            path = args.output / f"{name}.csv"
            if path.exists() and not args.overwrite:
                parser.error("Split output exists; use --overwrite")
        for name, subset in (("train", [c for c in cases if c.repository in training]),
                             ("test", [c for c in cases if c.repository not in training])):
            write_cases(args.output / f"{name}.csv", subset)
        private_json(args.output / "split.json", {"seed": args.seed, "source": fingerprint(cases),
                                                  "train_repositories": sorted(training)})
        return True
    if args.command == "compare":
        from .metrics import summary
        if args.max_regressions < 0 or not 0 <= args.max_accuracy_drop <= 1:
            parser.error("Regression thresholds must be nonnegative; accuracy drop is a fraction up to one")
        left, right = load_manifest(args.baseline), load_manifest(args.candidate)
        if not left or not right:
            parser.error("Comparison requires experiment manifests")
        for field in ("cases", "case_metadata", "image_ids", "image_map", "memory", "workspace_size", "max_steps", "provenance"):
            if left["inputs"].get(field) != right["inputs"].get(field):
                parser.error(f"Experiments differ in {field}")
        baseline, candidate = read_jsonl(args.baseline), read_jsonl(args.candidate)
        for records, manifest in ((baseline, left), (candidate, right)):
            if not records or any(group["complete"] is not True for group in summary(records, manifest=manifest)["groups"]):
                parser.error("Comparison requires complete experiments")
        result = compare(baseline, candidate)
        if args.output:
            private_json(args.output, result)
        print(json.dumps(result, indent=2))
        if len(result["losses"]) > args.max_regressions or result["accuracy_delta"] < -args.max_accuracy_drop:
            raise SystemExit(1)
        return True
    if args.command == "retry":
        from .runner import preflight_run, run_one
        from .model import Case
        manifest = load_manifest(args.results)
        if not manifest or manifest["inputs"].get("kind") != "run":
            parser.error("Retry requires a run manifest")
        inputs = manifest["inputs"]
        cases = {item["case_id"]: Case(**item) for item in inputs["cases"]}
        for case in cases.values():
            case._metadata = inputs.get("case_metadata", {}).get(case.case_id, {})
        configs = {item["name"]: ModelConfig(**item) for item in inputs["configs"]}
        selected = [record for record in read_jsonl(args.results) if record.status in args.status]
        prepare_output(args.output, {"kind": "retry", "source": str(args.results.resolve()),
                       "source_fingerprint": manifest["fingerprint"], "selected": [asdict(r) for r in selected],
                       "provenance": provenance()}, overwrite=args.overwrite)
        write_jsonl(args.output, [])
        for record in selected:
            original = configs[record.model]
            config = replace(original, image=inputs.get("image_ids", {}).get(original.image, original.image))
            from .harness import PROXY_IMAGE
            config._proxy_image = inputs.get("image_ids", {}).get(PROXY_IMAGE, PROXY_IMAGE)
            case = cases[record.case_id]
            image = (inputs.get("image_map") or {}).get(case.repository,
                     inputs.get("default_image", "anybench-sandbox:latest"))
            image = inputs.get("image_ids", {}).get(image, image)
            preflight_run([case], [config], image)
            retried = run_one(case, config, record.attempt, image, inputs["max_steps"],
                              inputs["workspace_size"], inputs["memory"], args.output.parent / "artifacts")
            retried.concurrency = record.concurrency
            append_jsonl(args.output, retried)
        print(f"Wrote {len(selected)} linked retries; original outcomes preserved")
        return True
    return False
