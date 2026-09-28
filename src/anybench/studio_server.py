"""Optional localhost API for Experimental AnyBench Studio."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
import tomllib
import threading
import uuid
import urllib.request
import webbrowser
from pathlib import Path

from .live import ROOT, RunObserver, snapshot, submit_control
from .model import read_jsonl, read_cases, ModelConfig
from .workflow import private_json


_SUMMARY_CACHE: dict[tuple, dict] = {}
_SUMMARY_LOCK = threading.Lock()


def token() -> str:
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = ROOT / "studio.token"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    return path.read_text().strip()


def worker_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        status = Path(f"/proc/{pid}/stat").read_text()
        return status.rsplit(") ", 1)[1][:1] != "Z"
    except (OSError, IndexError):
        return True


def attach(path: Path) -> str:
    selected = path.expanduser().resolve(strict=True)
    ident = uuid.uuid4().hex
    run = RunObserver(ident)
    run.set_meta("state", "historical")
    run.set_meta("read_only", True)
    run.set_meta("session_path" if selected.is_dir() else "output_path", str(selected))
    run.emit("session.attached", "session", {"path": str(selected)})
    return ident


def create_app():
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
    except ImportError as exc:
        raise RuntimeError('Studio requires pip install "any-bench[studio]"') from exc
    globals()["Request"] = Request

    app = FastAPI(title="AnyBench Studio — Experimental", docs_url=None, redoc_url=None)
    key = token()
    assets = Path(__file__).with_name("studio_assets")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in {"localhost", "127.0.0.1", "[::1]"}:
            return HTMLResponse("Invalid host", status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != f"{request.url.scheme}://{request.headers['host']}":
                return HTMLResponse("Invalid origin", status_code=403)
        return await call_next(request)

    def authorized(request: Request) -> None:
        if not secrets.compare_digest(request.cookies.get("anybench_studio", ""), key):
            raise HTTPException(401, "Open the URL printed by anybench studio")

    def run_path(ident: str) -> Path:
        if not ident.isalnum() or len(ident) != 32:
            raise HTTPException(404, "Unknown run")
        path = ROOT / ident / "live.sqlite3"
        if not path.is_file():
            raise HTTPException(404, "Unknown run")
        return path

    def live_state(path: Path) -> dict:
        state = snapshot(path, limit=0)
        meta = state["meta"]
        pid = meta.get("worker_pid")
        if meta.get("state") in {"starting", "running"} and isinstance(pid, int) and not worker_alive(pid):
            run = RunObserver(path.parent.name, root=path.parent.parent)
            run.set_meta("state", "interrupted")
            run.emit("session.interrupted", "session", {"reason": "Worker process ended unexpectedly"})
            return snapshot(path, limit=0)
        return state

    @app.get("/health")
    def health():
        return {"anybench_studio": True, "experimental": True,
                "workspace": str(Path.cwd().resolve())}

    @app.get("/auth")
    def auth(credential: str, run: str = ""):
        if not secrets.compare_digest(credential, key):
            raise HTTPException(403, "Invalid token")
        destination = "/" + ("?run=" + run if run.isalnum() and len(run) == 32 else "")
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie("anybench_studio", key, httponly=True, samesite="strict")
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/")
    def index(request: Request):
        authorized(request)
        path = assets / "index.html"
        if not path.exists():
            return HTMLResponse("Studio assets are missing. Build studio_web first.", status_code=503)
        return FileResponse(path)

    @app.get("/assets/{name:path}")
    def static(name: str, request: Request):
        authorized(request)
        target = (assets / "assets" / name).resolve()
        if not target.is_relative_to((assets / "assets").resolve()) or not target.is_file():
            raise HTTPException(404, "Asset unavailable")
        return FileResponse(target)

    prefix = "/api/experimental/v1"

    @app.get(prefix + "/config")
    def config(request: Request):
        authorized(request)
        path = Path(".anybench/config.json")
        if not path.exists():
            return {"repositories": [], "models": []}
        data = json.loads(path.read_text())
        for item in data.get("models", []):
            if item.get("api_key"):
                item["api_key"] = "[CONFIGURED]"
        return data

    @app.put(prefix + "/config")
    async def put_config(request: Request):
        authorized(request)
        document = await request.json()
        from .guided import _validate_config
        if not isinstance(document, dict):
            raise HTTPException(400, "Expected a configuration object")
        try:
            _validate_config(document)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        path = Path(".anybench/config.json")
        previous = json.loads(path.read_text()) if path.is_file() else {}
        existing = {item.get("name"): item.get("api_key") for item in previous.get("models", [])}
        for item in document["models"]:
            if item.get("api_key") == "[CONFIGURED]":
                original = existing.get(item.get("name"))
                if not original:
                    raise HTTPException(400, "Configured credential is missing")
                item["api_key"] = original
        private_json(path, document)
        return {"saved": str(path)}

    @app.post(prefix + "/config/import")
    async def import_config(request: Request):
        authorized(request)
        body = await request.json()
        source = body.get("text")
        kind = body.get("format")
        if not isinstance(source, str) or len(source) > 1_000_000 or kind not in {"json", "toml"}:
            raise HTTPException(400, "Provide JSON or TOML under 1 MB")
        try:
            document = json.loads(source) if kind == "json" else tomllib.loads(source)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if isinstance(document, dict) and {"repositories", "models"} <= document.keys():
            from .guided import _validate_config
            try:
                _validate_config(document)
            except (ValueError, TypeError, KeyError) as exc:
                raise HTTPException(400, str(exc)) from exc
            return {"config": document}
        if kind == "json" and isinstance(document, list) and document and all(
                isinstance(item, dict) for item in document):
            try:
                for item in document:
                    ModelConfig(**item)
            except (ValueError, TypeError) as exc:
                raise HTTPException(400, str(exc)) from exc
            directory = Path(".anybench/studio_imports")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / (uuid.uuid4().hex + ".json")
            private_json(path, document)
            return {"model_path": str(path)}
        if (kind == "toml" and isinstance(document, dict) and
                any(name in document for name in ("build", "validate", "run", "evaluate"))):
            directory = Path(".anybench/studio_imports")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / (uuid.uuid4().hex + ".toml")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(source)
            return {"experiment_path": str(path),
                    "command": next(name for name in ("build", "validate", "run", "evaluate")
                                    if name in document)}
        raise HTTPException(400, "Import a guided configuration, model JSON, or command TOML")

    @app.post(prefix + "/datasets/import")
    async def import_dataset(request: Request):
        authorized(request)
        body = await request.json()
        source = body.get("text") if isinstance(body, dict) else None
        if not isinstance(source, str) or len(source.encode("utf-8")) > 10_000_000:
            raise HTTPException(400, "Provide an AnyBench cases CSV under 10 MB")
        directory = Path(".anybench/studio_imports")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (uuid.uuid4().hex + ".csv")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(source)
            cases = read_cases(path)
            if not cases:
                raise ValueError("Dataset has no cases")
            if len({case.case_id for case in cases}) != len(cases):
                raise ValueError("Dataset case IDs must be unique")
        except (OSError, UnicodeError, ValueError) as exc:
            path.unlink(missing_ok=True)
            raise HTTPException(400, str(exc)) from exc
        return {"path": str(path), "cases": len(cases),
                "repositories": list(dict.fromkeys(case.repository for case in cases))}

    @app.get(prefix + "/runs")
    def runs(request: Request):
        authorized(request)
        result = []
        for path in sorted(ROOT.glob("*/live.sqlite3"), reverse=True):
            try:
                state = live_state(path)
                result.append({"id": path.parent.name, "meta": state["meta"],
                               "latest": state["latest"]})
            except (OSError, ValueError):
                continue
        return result

    @app.post(prefix + "/runs/attach")
    async def attach_run(request: Request):
        authorized(request)
        body = await request.json()
        try:
            ident = attach(Path(body["path"]))
        except (KeyError, OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": ident}

    def launch_plan(body: dict) -> dict:
        mode = body.get("mode", "guided")
        if mode == "guided":
            repositories = body.get("repositories") or []
            dataset_path = body.get("dataset_path")
            dataset_cases = None
            if dataset_path:
                if not isinstance(dataset_path, str):
                    raise HTTPException(400, "Dataset path must be a string")
                try:
                    dataset_cases = read_cases(Path(dataset_path).expanduser().resolve(strict=True))
                except (OSError, ValueError) as exc:
                    raise HTTPException(400, str(exc)) from exc
                if not dataset_cases or len({case.case_id for case in dataset_cases}) != len(dataset_cases):
                    raise HTTPException(400, "Dataset needs unique, nonempty case IDs")
                repositories = repositories or list(dict.fromkeys(case.repository for case in dataset_cases))
            if not isinstance(repositories, list) or not repositories or not all(
                    isinstance(item, str) and item for item in repositories):
                raise HTTPException(400, "Provide repository paths or Git URLs")
            if dataset_cases and any(case.repository not in repositories for case in dataset_cases):
                raise HTTPException(400, "Dataset contains a repository outside this run")
            config_path = Path(body.get("config_path") or ".anybench/config.json")
            if not config_path.is_file():
                raise HTTPException(400, "Configuration file is missing")
            from .guided import _validate_config
            try:
                configuration = json.loads(config_path.read_text())
                _validate_config(configuration)
            except (ValueError, KeyError, TypeError) as exc:
                raise HTTPException(400, str(exc)) from exc
            command = ["start", *repositories, "--config", str(config_path), "--yes"]
            if dataset_path:
                command.extend(["--dataset", str(Path(dataset_path).expanduser().resolve())])
            for field, flag in (("commits", "--commits"), ("max_problems", "--max-problems")):
                value = body.get(field)
                if value is not None:
                    if type(value) is not int or value < 1:
                        raise HTTPException(400, field + " must be positive")
                    command.extend([flag, str(value)])
            workload = {"repositories": len(repositories),
                        "maximum_commits": 0 if dataset_cases else len(repositories) * (body.get("commits") or 50),
                        "custom_dataset_cases": len(dataset_cases) if dataset_cases else None,
                        "maximum_problems": body.get("max_problems"),
                        "candidate_models": sum(item.get("role") == "candidate"
                                                for item in configuration["models"]),
                        "judge": any(item.get("role") == "judge" for item in configuration["models"]),
                        "harnesses": {item["name"]: item.get("harness", "anybench")
                                      for item in configuration["models"] if item.get("role") == "candidate"},
                        "budget_usd": configuration.get("budget_usd")}
            if dataset_cases:
                workload["test_commands"] = [
                    {"case_id": case.case_id, "command": case.test_command}
                    for case in dataset_cases[:20]]
                workload["test_commands_shown"] = min(20, len(dataset_cases))
        elif mode == "advanced":
            command = body.get("argv")
            if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
                raise HTTPException(400, "Advanced argv must be a string array")
            if command[0] not in {"build", "validate", "run", "evaluate"} or "--live" in command:
                raise HTTPException(400, "Unsupported advanced command")
            workload = {"command": command[0], "arguments": command[1:],
                        "provider_calls_possible": command[0] in {"build", "run", "evaluate"}}
        else:
            raise HTTPException(400, "Unknown launch mode")
        return {"command": command, "workload": workload,
                "confirmation": "Builder and candidate calls may incur provider charges. Docker setup may download dependencies."}

    @app.post(prefix + "/runs/validate")
    async def validate_launch(request: Request):
        authorized(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected launch settings")
        return launch_plan(body)

    @app.post(prefix + "/runs")
    async def create_run(request: Request):
        authorized(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected launch settings")
        if body.get("confirmed") is not True:
            raise HTTPException(400, "Confirm the workload and possible provider charges")
        command = launch_plan(body)["command"]
        run = RunObserver()
        run.set_meta("command", command[0])
        run.set_meta("arguments", command)
        run.set_meta("state", "starting")
        environment = dict(os.environ, ANYBENCH_LIVE_ID=run.run_id,
                           ANYBENCH_STUDIO_PORT=str(request.url.port or 8765))
        try:
            process = subprocess.Popen([sys.executable, "-m", "anybench.cli", *command, "--live"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env=environment, start_new_session=True)
        except OSError as exc:
            run.set_meta("state", "failed")
            run.emit("session.failed", "session", {"error": str(exc)})
            raise HTTPException(500, "Could not start benchmark worker") from exc
        run.set_meta("worker_pid", process.pid)
        return {"id": run.run_id, "pid": process.pid}

    @app.post(prefix + "/runs/{ident}/resume")
    async def resume_run(ident: str, request: Request):
        authorized(request)
        path = run_path(ident)
        state = live_state(path)
        meta = state["meta"]
        session_path = meta.get("session_path")
        if not session_path or meta.get("read_only"):
            raise HTTPException(409, "Only guided Studio runs can be resumed here")
        if meta.get("state") not in {"stopped", "failed", "interrupted"}:
            raise HTTPException(409, "Run is not stopped")
        session = Path(session_path)
        if not (session / "session.json").is_file():
            raise HTTPException(409, "Saved session metadata is missing")
        completed = set()
        terminal_attempts = set()
        attempts = session / "attempts.jsonl"
        if attempts.is_file():
            for item in read_jsonl(attempts):
                completed.add(f"attempt:{item.case_id}:{item.model}:{item.concurrency}:{item.attempt}")
                terminal_attempts.add((item.case_id, item.model, item.concurrency, item.attempt))
        terminal_scored = set()
        scored = session / "scored.jsonl"
        if scored.is_file():
            terminal_scored = {(item.case_id, item.model, item.concurrency, item.attempt)
                               for item in read_jsonl(scored)}
        from .live import connection
        with connection(path) as db:
            operation_events = {json.loads(row["data"]).get("payload", {}).get("operation_id"):
                                json.loads(row["data"]) for row in
                                db.execute("SELECT data FROM events WHERE kind='operation.started'")}
        uncertain = []
        for item in state["unresolved_operations"]:
            ident_value = item["id"]
            if ident_value in completed:
                continue
            details = operation_events.get(ident_value, {})
            if ident_value.startswith("api:"):
                key_tuple = (details.get("case_id"), details.get("model"),
                             details.get("concurrency"), details.get("attempt"))
                terminal = terminal_scored if item["stage"] == "judge" else terminal_attempts
                if key_tuple in terminal:
                    continue
            uncertain.append(ident_value)
        if uncertain:
            raise HTTPException(409, "Interrupted model work has unknown billing state; start a new run")
        with connection(path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM meta WHERE key='state'").fetchone()
            current = json.loads(row[0]) if row else None
            if current not in {"stopped", "failed", "interrupted"}:
                raise HTTPException(409, "Run is already resuming")
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('state',?)",
                       (json.dumps("resuming"),))
        try:
            environment = dict(os.environ, ANYBENCH_LIVE_ID=ident,
                               ANYBENCH_STUDIO_PORT=str(request.url.port or 8765))
            process = subprocess.Popen([sys.executable, "-m", "anybench.cli", "start",
                "--resume", str(session), "--yes", "--live"], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=environment, start_new_session=True)
        except OSError as exc:
            RunObserver(ident, root=path.parent.parent).set_meta("state", "failed")
            raise HTTPException(500, str(exc)) from exc
        run = RunObserver(ident, root=path.parent.parent)
        run.set_meta("worker_pid", process.pid)
        run.set_meta("state", "starting")
        return {"id": ident, "pid": process.pid}

    @app.get(prefix + "/runs/{ident}/snapshot")
    def get_snapshot(ident: str, request: Request, after: int = 0, limit: int = 500):
        authorized(request)
        if after < 0 or not 1 <= limit <= 1000:
            raise HTTPException(400, "Invalid event page")
        path = run_path(ident)
        live_state(path)
        return snapshot(path, after, limit)

    @app.get(prefix + "/runs/{ident}/events")
    async def get_events(ident: str, request: Request, after: int = 0):
        authorized(request)
        path = run_path(ident)
        last = request.headers.get("last-event-id")
        if last and last.isdecimal():
            after = int(last)
        async def stream():
            cursor = after
            while not await request.is_disconnected():
                state = snapshot(path, cursor, 200)
                if state["events"]:
                    for item in state["events"]:
                        cursor = item["seq"]
                        yield f"id: {cursor}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                else:
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(.35)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post(prefix + "/runs/{ident}/controls")
    async def control(ident: str, request: Request):
        authorized(request)
        path = run_path(ident)
        meta = live_state(path)["meta"]
        if meta.get("read_only"):
            raise HTTPException(409, "Historical run is read-only")
        if meta.get("state") not in {"starting", "running"}:
            raise HTTPException(409, "Worker is not accepting controls")
        body = await request.json()
        action = body.get("action")
        value = body.get("value")
        if action not in {"concurrency", "pause", "resume", "stop"}:
            raise HTTPException(400, "Invalid control")
        if action == "concurrency" and (type(value) is not int or not 1 <= value <= 64):
            raise HTTPException(400, "Concurrency must be between 1 and 64")
        ident_value = body.get("request_id") or uuid.uuid4().hex
        if not isinstance(ident_value, str) or len(ident_value) > 100:
            raise HTTPException(400, "Invalid request ID")
        result = submit_control(path, action, value, ident_value)
        if result["action"] != action or result["value"] != value:
            raise HTTPException(409, "Request ID has a different command")
        return result

    @app.get(prefix + "/runs/{ident}/artifact/{artifact_id}")
    def artifact(ident: str, artifact_id: str, request: Request):
        authorized(request)
        path = run_path(ident)
        if not artifact_id.isalnum() or len(artifact_id) != 32:
            raise HTTPException(404, "Artifact unavailable")
        from .live import connection
        with connection(path) as db:
            registered = db.execute("SELECT 1 FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if not registered:
            raise HTTPException(404, "Artifact unavailable")
        target = path.parent / "artifacts" / (artifact_id + ".txt")
        if target.is_symlink() or not target.is_file():
            raise HTTPException(404, "Artifact unavailable")
        return FileResponse(target, media_type="text/plain")

    @app.get(prefix + "/runs/{ident}/logs")
    def logs(ident: str, request: Request, after: int = 0, limit: int = 200,
             case_id: str = ""):
        authorized(request)
        if after < 0 or not 1 <= limit <= 500 or len(case_id) > 200:
            raise HTTPException(400, "Invalid log page")
        from .live import connection
        with connection(run_path(ident)) as db:
            query = ("SELECT seq,at,stage,kind,data FROM events WHERE seq>? "
                     "AND kind IN ('process.output','process.output.truncated')")
            arguments: list = [after]
            if case_id:
                query += " AND json_extract(data,'$.case_id')=?"
                arguments.append(case_id)
            rows = db.execute(query + " ORDER BY seq LIMIT ?", [*arguments, limit + 1]).fetchall()
        page = [{"seq": item["seq"], "at": item["at"], "stage": item["stage"],
                 "kind": item["kind"], "data": json.loads(item["data"])}
                for item in rows[:limit]]
        return {"logs": page, "cursor": page[-1]["seq"] if page else after,
                "has_more": len(rows) > limit}

    def results_path(meta: dict) -> Path | None:
        if meta.get("session_path"):
            session = Path(meta["session_path"])
            return session / ("scored.jsonl" if (session / "scored.jsonl").exists() else "attempts.jsonl")
        if meta.get("output_path") and meta.get("command") not in {"build", "validate"}:
            return Path(meta["output_path"])
        return None

    def result_records(meta: dict) -> list:
        """Keep completed candidate attempts visible while judging is in progress."""
        output = results_path(meta)
        if output is None:
            return []
        if not meta.get("session_path"):
            return read_jsonl(output) if output.is_file() else []
        session = Path(meta["session_path"])
        attempts = session / "attempts.jsonl"
        scored = session / "scored.jsonl"
        candidates = read_jsonl(attempts) if attempts.is_file() else []
        if not scored.is_file():
            return candidates
        by_identity = {(item.case_id, item.model, item.concurrency, item.attempt): item
                       for item in candidates}
        for item in read_jsonl(scored):
            by_identity[(item.case_id, item.model, item.concurrency, item.attempt)] = item
        return list(by_identity.values())

    @app.get(prefix + "/runs/{ident}/records")
    def records(ident: str, request: Request, offset: int = 0, limit: int = 100,
                case_id: str = ""):
        authorized(request)
        state = snapshot(run_path(ident), limit=0)
        meta = state["meta"]
        output = results_path(meta)
        if not output or not output.is_file():
            return {"total": 0, "records": []}
        data = result_records(meta)
        if case_id:
            data = [item for item in data if item.case_id == case_id]
        from dataclasses import asdict
        return {"total": len(data), "records": [asdict(item) for item in data[max(0, offset):max(0, offset) + min(200, max(1, limit))]]}

    @app.get(prefix + "/runs/{ident}/summary")
    def run_summary(ident: str, request: Request):
        authorized(request)
        state = snapshot(run_path(ident), limit=0)
        meta = state["meta"]
        output = results_path(meta)
        if not output or not output.is_file():
            return {"groups": []}
        sources = [output]
        if meta.get("session_path"):
            attempts = Path(meta["session_path"]) / "attempts.jsonl"
            if attempts != output:
                sources.append(attempts)
        file_versions = tuple((str(path.resolve()), path.stat().st_mtime_ns,
                               path.stat().st_size) for path in sources if path.is_file())
        cache_key = (str(output.resolve()), file_versions,
                     json.dumps(meta.get("variable_groups", []), sort_keys=True))
        with _SUMMARY_LOCK:
            cached = _SUMMARY_CACHE.get(cache_key)
        if cached is not None:
            return cached
        from .metrics import summary
        from .commands import dataset_context, load_manifest
        from .live import variable_groups_for
        groups = summary(result_records(meta), dataset_context(output), load_manifest(output))
        variable = variable_groups_for(output)
        for group in groups["groups"]:
            group["variable_concurrency"] = (group["model"], group["concurrency"]) in variable
        with _SUMMARY_LOCK:
            for key in list(_SUMMARY_CACHE):
                if key[0] == cache_key[0] and key != cache_key:
                    del _SUMMARY_CACHE[key]
            _SUMMARY_CACHE[cache_key] = groups
        return groups

    @app.get(prefix + "/runs/{ident}/problems")
    def problems(ident: str, request: Request, offset: int = 0, limit: int = 100,
                 query: str = "", model: str = "", harness: str = "",
                 state: str = "", test_result: str = "", judge_min: float | None = None):
        authorized(request)
        if any(len(value) > 200 for value in (query, model, harness, state, test_result)):
            raise HTTPException(400, "Problem filter is too long")
        if test_result not in {"", "passed", "failed", "unscored"} or judge_min is not None and not 0 <= judge_min <= 1:
            raise HTTPException(400, "Invalid result filter")
        meta = snapshot(run_path(ident), limit=0)["meta"]
        source = None
        session = None
        if meta.get("session_path"):
            session = Path(meta["session_path"])
            source = session / ("cases.csv" if (session / "cases.csv").exists() else "verified.csv")
        elif meta.get("arguments") and meta["arguments"][0] in {"run", "validate", "evaluate"}:
            source = Path(meta["arguments"][1])
        elif meta.get("arguments") and meta["arguments"][0] == "build" and meta.get("output_path"):
            source = Path(meta["output_path"])
        from dataclasses import asdict
        from .metadata import case_metadata
        if source and source.is_file():
            cases = read_cases(source)
        elif meta.get("output_path"):
            from .commands import dataset_context
            cases = dataset_context(Path(meta["output_path"])) or []
        else:
            cases = []
        outcomes = {}
        if session and (session / "validation.json").is_file():
            outcomes = {item.get("case_id"): item for item in json.loads((session / "validation.json").read_text())}
        selected = set()
        if session and (session / "verified.csv").is_file():
            selected = {item.case_id for item in read_cases(session / "verified.csv")}
        output = results_path(meta)
        by_case: dict[str, list] = {}
        if output and output.is_file():
            for record in result_records(meta):
                by_case.setdefault(record.case_id, []).append(record)
        def matching(item) -> bool:
            attempts = by_case.get(item.case_id, [])
            status = outcomes.get(item.case_id, case_metadata(item).get("validation", {})).get("status", "pending")
            if state and status != state:
                return False
            if query and query.casefold() not in (item.case_id + " " + item.repository + " " +
                    item.problem_statement + " " + " ".join(record.model + " " + record.harness
                    for record in attempts)).casefold():
                return False
            if model and not any(model.casefold() in record.model.casefold() for record in attempts):
                return False
            if harness and not any(harness.casefold() in record.harness.casefold() for record in attempts):
                return False
            if test_result and not any((record.test_passed is True if test_result == "passed" else
                    record.test_passed is False if test_result == "failed" else
                    record.test_passed is None) for record in attempts):
                return False
            if judge_min is not None and not any(record.judge_score is not None and
                    record.judge_score >= judge_min for record in attempts):
                return False
            return True
        cases = [item for item in cases if matching(item)]
        page = []
        for item in cases[max(0, offset):max(0, offset) + min(200, max(1, limit))]:
            validation = outcomes.get(item.case_id, case_metadata(item).get("validation", {}))
            page.append({**{k: v for k, v in asdict(item).items() if k != "gold_diff"},
                         "state": validation.get("status", "pending"),
                         "reason": validation.get("reason", ""),
                         "in_verified_dataset": item.case_id in selected if session else None,
                         "attempts": [{"case_id": record.case_id, "model": record.model,
                                       "harness": record.harness, "attempt": record.attempt,
                                       "concurrency": record.concurrency, "status": record.status,
                                       "test_passed": record.test_passed,
                                       "judge_score": record.judge_score}
                                      for record in by_case.get(item.case_id, [])]})
        return {"total": len(cases), "problems": page}

    return app


def serve(port: int = 8765, attached: Path | None = None, *, open_browser: bool = True) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("Invalid port")
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError('Studio requires pip install "any-bench[studio]"') from exc
    ident = attach(attached) if attached else ""
    address = f"http://127.0.0.1:{port}/auth?credential={token()}" + (f"&run={ident}" if ident else "")
    print("Experimental AnyBench Studio: " + address, flush=True)
    if open_browser:
        import threading
        threading.Timer(.8, webbrowser.open, args=(address,)).start()
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")


def ensure_server(run_id: str) -> str:
    """Start a detached local server for CLI live mode if one is not running."""
    from importlib.util import find_spec
    if find_spec("fastapi") is None or find_spec("uvicorn") is None:
        raise RuntimeError('Live mode requires pip install "any-bench[studio]"')
    port = int(os.environ.get("ANYBENCH_STUDIO_PORT", "8765"))
    base = f"http://127.0.0.1:{port}"
    def ready() -> bool:
        try:
            with urllib.request.urlopen(base + "/health", timeout=.5) as response:
                data = json.load(response)
                if data.get("anybench_studio") is True and data.get("workspace") != str(Path.cwd().resolve()):
                    raise RuntimeError("Studio port belongs to another workspace; set ANYBENCH_STUDIO_PORT")
                return data.get("anybench_studio") is True
        except (OSError, ValueError):
            return False
    if not ready():
        ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        subprocess.Popen([sys.executable, "-m", "anybench.cli", "studio", "--port",
                          str(port), "--no-open"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(25):
            if ready():
                break
            time.sleep(.2)
        else:
            raise RuntimeError("Could not start Experimental Studio on localhost")
    return base + "/auth?credential=" + token() + "&run=" + run_id
