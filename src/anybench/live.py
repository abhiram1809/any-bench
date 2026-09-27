"""Experimental, opt-in observation and control for local benchmark runs."""
from __future__ import annotations

import contextvars
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .privacy import redact_value


ROOT = Path(".anybench/live")
_ACTIVE: contextvars.ContextVar[RunObserver | None] = contextvars.ContextVar("anybench_live", default=None)
_SCOPE: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("anybench_live_scope", default={})
_STAGE: contextvars.ContextVar[str] = contextvars.ContextVar("anybench_live_stage", default="subprocess")
_SPAN: contextvars.ContextVar[tuple[str, str | None] | None] = contextvars.ContextVar(
    "anybench_live_span", default=None)


def observer() -> RunObserver | None:
    return _ACTIVE.get()


def emit(kind: str, stage: str, payload: dict[str, Any] | None = None, **identity: Any) -> None:
    active = observer()
    if active:
        active.emit(kind, stage, payload or {}, **identity)


def checkpoint() -> bool:
    active = observer()
    return active is None or active.controller.checkpoint()


@contextmanager
def activate(active: RunObserver):
    token = _ACTIVE.set(active)
    try:
        yield active
    finally:
        _ACTIVE.reset(token)


@contextmanager
def scope(**identity: Any):
    token = _SCOPE.set({**_SCOPE.get(), **identity})
    try:
        yield
    finally:
        _SCOPE.reset(token)


@contextmanager
def stage_scope(name: str):
    token = _STAGE.set(name)
    active = observer()
    parent = _SPAN.get()
    span_id = uuid.uuid4().hex if active else ""
    span_token = _SPAN.set((span_id, parent[0] if parent else None)) if active else None
    started = time.monotonic()
    try:
        if active:
            active.emit("span.started", name, {})
        yield
    except BaseException as exc:
        if active:
            active.emit("span.failed", name, {"seconds": time.monotonic() - started,
                                                "error": str(exc)})
        raise
    else:
        if active:
            active.emit("span.finished", name, {"seconds": time.monotonic() - started})
    finally:
        if span_token is not None:
            _SPAN.reset(span_token)
        _STAGE.reset(token)


def current_stage() -> str:
    return _STAGE.get()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


@contextmanager
def connection(path: Path):
    db = _connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with connection(path) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                kind TEXT NOT NULL, stage TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS controls (
                id TEXT PRIMARY KEY, action TEXT NOT NULL, value INTEGER,
                status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                requested_at REAL NOT NULL, applied_at REAL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, stage TEXT NOT NULL, state TEXT NOT NULL,
                at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY, bytes INTEGER NOT NULL, truncated INTEGER NOT NULL
            );
        """)
    os.chmod(path, 0o600)


class RunController:
    def __init__(self, owner: RunObserver):
        self.owner = owner
        self.target = 2
        self.paused = False
        self.stopping = False
        self.level = 2
        self.model = ""
        self.provider_cap: int | None = None
        self.variable_groups: set[tuple[str, int]] = set()
        self._busy = 0
        self._requests = 0
        self._waiting = 0
        self._condition = threading.Condition()
        self._poll_lock = threading.Lock()

    def set_level(self, value: int, provider_cap: int | None = None, model: str = "") -> None:
        with self._condition:
            self.level = value
            self.target = value
            self.provider_cap = provider_cap
            self.model = model
            self._condition.notify_all()
        self.owner.set_meta("concurrency", {"configured": value, "target": value,
                            "effective": min(value, provider_cap or value), "model": model})
        self.owner.emit("concurrency.level", "candidate", {"configured": value,
                         "target": value, "effective": min(value, provider_cap or value),
                         "model": model})

    def _poll(self) -> None:
        applied = []
        with self._poll_lock:
            with connection(self.owner.path) as db:
                commands = db.execute("SELECT * FROM controls WHERE status='pending' ORDER BY requested_at").fetchall()
                for command in commands:
                    action = command["action"]
                    value = command["value"]
                    status, reason = "applied", ""
                    if action == "concurrency" and (type(value) is not int or value < 1):
                        status, reason = "rejected", "Concurrency must be positive"
                    elif action not in {"concurrency", "pause", "resume", "stop"}:
                        status, reason = "rejected", "Unknown control"
                    else:
                        with self._condition:
                            if action == "concurrency":
                                if value != self.target:
                                    self.variable_groups.add((self.model, self.level))
                                self.target = value
                            elif action == "pause":
                                self.paused = True
                            elif action == "resume":
                                self.paused = False
                            else:
                                self.stopping = True
                            self._condition.notify_all()
                    db.execute("UPDATE controls SET status=?, reason=?, applied_at=? WHERE id=?",
                               (status, reason, time.time(), command["id"]))
                    applied.append((status, command["id"], action, value, reason, self.target, self.level))
            for status, ident, action, value, reason, target, level in applied:
                if status == "applied" and action == "concurrency":
                    self.owner.set_meta("variable_groups", sorted([list(item) for item in self.variable_groups]))
                    self.owner.set_meta("concurrency", {"configured": level, "target": target,
                                         "effective": min(target, self.provider_cap or target),
                                         "model": self.model})
                self.owner.emit("control." + status, "session",
                                {"id": ident, "action": action, "value": value,
                                 "reason": reason, "target": target, "level": level,
                                 "model": self.model})

    def checkpoint(self) -> bool:
        while True:
            self._poll()
            with self._condition:
                if self.stopping:
                    return False
                if not self.paused:
                    return True
                self._condition.wait(.25)

    @contextmanager
    def request(self, endpoint: str):
        waiting = False
        while True:
            self._poll()
            announce_wait = False
            with self._condition:
                if self._requests < min(self.target, self.provider_cap or self.target):
                    self._requests += 1
                    if waiting:
                        self._waiting -= 1
                    break
                if not waiting:
                    self._waiting += 1
                    waiting = True
                    announce_wait = True
                self._condition.wait(.1)
            if announce_wait:
                self.owner.emit("request.waiting", "model", {"endpoint": endpoint,
                                "waiting": self._waiting, "active": self._requests})
        self.owner.emit("request.admitted", "model", {"endpoint": endpoint,
                        "active": self._requests, "waiting": self._waiting})
        try:
            yield
        finally:
            with self._condition:
                self._requests -= 1
                self._condition.notify_all()
            self.owner.emit("request.finished", "model", {"endpoint": endpoint,
                            "active": self._requests, "waiting": self._waiting})


class RunObserver:
    def __init__(self, run_id: str | None = None, root: Path = ROOT):
        self.run_id = run_id or uuid.uuid4().hex
        self.path = root / self.run_id / "live.sqlite3"
        database(self.path)
        self._lock = threading.RLock()
        self._configurations: list[Any] = []
        self._containers: dict[str, str] = {}
        self._sample_stop = threading.Event()
        artifact_dir = self.path.parent / "artifacts"
        self._artifact_bytes = sum(item.stat().st_size for item in artifact_dir.glob("*.txt")) if artifact_dir.exists() else 0
        self.controller = RunController(self)
        self.set_meta("run_id", self.run_id)
        previous = snapshot(self.path, limit=0)["meta"].get("variable_groups", [])
        self.controller.variable_groups = {(str(model), int(level)) for model, level in previous}

    def set_configs(self, configs: list[Any]) -> None:
        by_name = {item.name: item for item in self._configurations}
        by_name.update({item.name: item for item in configs})
        self._configurations = list(by_name.values())

    def sanitize(self, value: Any) -> Any:
        for config in self._configurations:
            value = redact_value(value, config)
        return value

    def artifact(self, value: Any, *, limit: int = 2_000_000) -> dict[str, Any]:
        contents = json.dumps(self.sanitize(value), ensure_ascii=False, default=str).encode()
        truncated = len(contents) > limit
        if truncated:
            contents = contents[:limit].decode(errors="ignore").encode()
        with self._lock:
            remaining = max(0, 128_000_000 - self._artifact_bytes)
            if not remaining:
                return {"bytes": 0, "truncated": True, "unavailable": "artifact budget reached"}
            if len(contents) > remaining:
                contents = contents[:remaining].decode(errors="ignore").encode()
                truncated = True
            ident = uuid.uuid4().hex
            target = self.path.parent / "artifacts"
            target.mkdir(mode=0o700, exist_ok=True)
            path = target / (ident + ".txt")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(contents)
            with connection(self.path) as db:
                db.execute("INSERT INTO artifacts(id,bytes,truncated) VALUES(?,?,?)",
                           (ident, len(contents), int(truncated)))
            self._artifact_bytes += len(contents)
            return {"id": ident, "bytes": len(contents), "truncated": truncated}

    def output_stream(self, stage: str):
        return OutputStream(self, stage)

    def register_container(self, container: str, purpose: str) -> None:
        with self._lock:
            self._containers[container] = purpose
        self.emit("container.started", "docker", {"container": container, "purpose": purpose})

    def unregister_container(self, container: str) -> None:
        with self._lock:
            self._containers.pop(container, None)
        self.emit("container.finished", "docker", {"container": container})

    def start_sampling(self) -> None:
        threading.Thread(target=self._sample_loop, daemon=True).start()

    def stop_sampling(self) -> None:
        self._sample_stop.set()

    def _sample_loop(self) -> None:
        while not self._sample_stop.wait(2):
            with self._lock:
                containers = list(self._containers.items())
            for container, purpose in containers:
                try:
                    result = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}",
                                             container], capture_output=True, text=True, timeout=3)
                    if result.returncode == 0 and result.stdout.strip():
                        self.emit("resource.sample", "docker", {"container": container,
                                  "purpose": purpose, "reported": json.loads(result.stdout.splitlines()[0])})
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
            if shutil.which("nvidia-smi"):
                try:
                    result = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                                             "--format=csv,noheader,nounits"], capture_output=True,
                                            text=True, timeout=3)
                    if result.returncode == 0:
                        self.emit("resource.sample", "host_gpu", {"reported": result.stdout.strip()})
                except (OSError, subprocess.TimeoutExpired):
                    pass

    def emit(self, kind: str, stage: str, payload: dict[str, Any], **identity: Any) -> None:
        span = _SPAN.get()
        data = self.sanitize({"v": 1, "run_id": self.run_id,
                              "span_id": span[0] if span else None,
                              "parent_span_id": span[1] if span else None,
                              **_SCOPE.get(), **identity,
                              "payload": payload})
        with self._lock, connection(self.path) as db:
            db.execute("INSERT INTO events(at,kind,stage,data) VALUES(?,?,?,?)",
                       (time.time(), kind, stage, json.dumps(data, ensure_ascii=False, default=str)))

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock, connection(self.path) as db:
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                       (key, json.dumps(self.sanitize(value), ensure_ascii=False, default=str)))

    def operation_start(self, operation_id: str, stage: str) -> None:
        with self._lock, connection(self.path) as db:
            db.execute("INSERT OR REPLACE INTO operations(id,stage,state,at) VALUES(?,?,?,?)",
                       (operation_id, stage, "started", time.time()))
        self.emit("operation.started", stage, {"operation_id": operation_id})

    def operation_end(self, operation_id: str, stage: str, outcome: str) -> None:
        with self._lock, connection(self.path) as db:
            db.execute("UPDATE operations SET state=? WHERE id=?", (outcome, operation_id))
        self.emit("operation." + outcome, stage, {"operation_id": operation_id})


class OutputStream:
    """Delay a suffix so configured secrets split across chunks cannot leak."""

    def __init__(self, owner: RunObserver, stage: str):
        self.owner, self.stage = owner, stage
        self.tail = ""
        self.emitted = 0
        self.truncated = False
        names = [name for config in owner._configurations for name in
                 ([config.api_key_env] if config.api_key_env else []) + list(config.env.values())]
        self.keep = max((len(os.environ[name]) - 1 for name in names if os.environ.get(name)), default=0)

    def write(self, value: str) -> None:
        combined = self.owner.sanitize(self.tail + value)
        if self.keep:
            self.tail = combined[-self.keep:]
            ready = combined[:-self.keep]
        else:
            self.tail = ""
            ready = combined
        self._emit(ready)

    def close(self) -> None:
        self._emit(self.tail)
        self.tail = ""

    def _emit(self, value: str) -> None:
        allowed = max(0, 8_000_000 - self.emitted)
        if len(value) > allowed and not self.truncated:
            self.truncated = True
            self.owner.emit("process.output.truncated", self.stage, {"limit": 8_000_000})
        value = value[:allowed]
        self.emitted += len(value)
        for start in range(0, len(value), 4096):
            self.owner.emit("process.output", self.stage, {"text": value[start:start + 4096]})


def submit_control(path: Path, action: str, value: int | None, request_id: str) -> dict:
    with connection(path) as db:
        db.execute("INSERT OR IGNORE INTO controls(id,action,value,status,requested_at) VALUES(?,?,?,'pending',?)",
                   (request_id, action, value, time.time()))
        row = db.execute("SELECT * FROM controls WHERE id=?", (request_id,)).fetchone()
    return dict(row) if row else {}


def snapshot(path: Path, after: int = 0, limit: int = 500) -> dict:
    with connection(path) as db:
        cursor = db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        events = [dict(row) for row in db.execute(
            "SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)).fetchall()]
        meta = {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT * FROM meta")}
        controls = [dict(row) for row in db.execute("SELECT * FROM controls ORDER BY requested_at DESC LIMIT 30")]
        operations = [dict(row) for row in db.execute("SELECT * FROM operations WHERE state='started'")]
    for event in events:
        event["data"] = json.loads(event["data"])
    return {"cursor": events[-1]["seq"] if events else after,
            "latest": cursor, "has_more": bool(events and events[-1]["seq"] < cursor),
            "events": events, "meta": meta,
            "controls": controls, "unresolved_operations": operations}


def variable_groups_for(results: Path) -> set[tuple[str, int]]:
    selected = results.resolve()
    for path in ROOT.glob("*/live.sqlite3"):
        try:
            meta = snapshot(path, limit=0)["meta"]
            output = Path(meta["output_path"]).resolve() if meta.get("output_path") else None
            session = Path(meta["session_path"]).resolve() if meta.get("session_path") else None
            if selected == output or session and selected.parent == session:
                return {(str(model), int(level)) for model, level in meta.get("variable_groups", [])}
        except (OSError, ValueError, KeyError):
            continue
    return set()
