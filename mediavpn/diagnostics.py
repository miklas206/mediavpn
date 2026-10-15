"""Bounded durable diagnostics. Never serialize exceptions, configs or Docker output."""

import contextlib
import contextvars
import json
import logging
import re
import sqlite3
import time
import uuid
from functools import wraps
from pathlib import Path

operation = contextvars.ContextVar("lifecycle_operation", default=None)
FIELDS = {
    "reason",
    "origin",
    "actor",
    "profile",
    "container",
    "previousContainer",
    "generation",
    "previousGeneration",
    "health",
    "running",
    "exitCode",
    "oomKilled",
    "signal",
    "errorType",
    "errorCode",
    "httpStatus",
    "publicIp",
    "previousPublicIp",
    "port",
    "previousPort",
    "internalPort",
    "handshakeAge",
    "desired",
    "mounted",
    "vpnReady",
    "enabled",
    "action",
    "changed",
    "phase",
}
FIELDS.add("errorSite")
FIELDS.update({"sourceTimestamp", "selectedServer", "expectedGeneration"})


@contextlib.contextmanager
def cause(origin, reason):
    token = operation.set(
        {"operationId": uuid.uuid4().hex, "origin": origin, "reason": reason}
    )
    try:
        yield
    finally:
        operation.reset(token)


def trace_async(origin, reason):
    def decorate(function):
        @wraps(function)
        async def traced(*args, **kwargs):
            with contextlib.nullcontext() if operation.get() else cause(origin, reason):
                return await function(*args, **kwargs)

        return traced

    return decorate


def trace(origin, reason):
    def decorate(function):
        @wraps(function)
        def traced(*args, **kwargs):
            with contextlib.nullcontext() if operation.get() else cause(origin, reason):
                return function(*args, **kwargs)

        return traced

    return decorate


class Diagnostics:
    def __init__(self, directory, component="vpn"):
        self.path = Path(directory) / "lifecycle.sqlite"
        self.component = component
        self.repeated = {}

    @contextlib.contextmanager
    def connect(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise OSError("Diagnostic file must not be a symlink")
        connection = sqlite3.connect(self.path, timeout=2)
        self.path.chmod(0o600)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"
            )
            with connection:
                yield connection
        finally:
            connection.close()

    def record(self, event, **fields):
        # Deliberate scalar allowlist; no free-form messages, tracebacks or headers.
        safe = {}
        for key, value in fields.items():
            if key not in FIELDS or value is None:
                continue
            if isinstance(value, (bool, int, float)):
                safe[key] = value
            elif isinstance(value, str) and re.fullmatch(
                r"[A-Za-z0-9_.:, -]{1,180}", value
            ):
                safe[key] = value
        if not re.fullmatch(r"[a-z][a-z0-9_.]{1,80}", event):
            return
        if "error" in event or "blocked" in event:
            marker = (event, json.dumps(safe, sort_keys=True))
            now = time.monotonic()
            if now - self.repeated.get(marker, -60) < 60:
                return
            self.repeated = {
                key: stamp for key, stamp in self.repeated.items() if now - stamp < 60
            }
            self.repeated[marker] = now
        entry = {
            "timestamp": time.time(),
            "type": self.component + "." + event,
            "severity": "warning"
            if "error" in event or "blocked" in event or "failed" in event
            else "info",
            "operationId": uuid.uuid4().hex,
            **(operation.get() or {}),
            **safe,
        }
        if safe.get("action") in {"oom", "health_status: unhealthy"} or (
            safe.get("action") == "die" and str(safe.get("exitCode", "0")) != "0"
        ):
            entry["severity"] = "warning"
        entry["message"] = (
            entry["type"]
            + " "
            + json.dumps(
                {
                    key: value
                    for key, value in entry.items()
                    if key not in {"timestamp", "type", "severity"}
                },
                sort_keys=True,
            )
        )
        try:
            with self.connect() as db:
                db.execute("INSERT INTO entries(body) VALUES (?)", (json.dumps(entry),))
                db.execute(
                    "DELETE FROM entries WHERE id <= (SELECT MAX(id)-2000 FROM entries)"
                )
        except (OSError, sqlite3.Error):
            # Observability failure must not prevent a safety stop.
            logging.getLogger("mediahub.diagnostics").error(
                "Lifecycle diagnostic storage unavailable"
            )

    def entries(self, limit=200):
        try:
            with self.connect() as db:
                return [
                    json.loads(row[0])
                    for row in reversed(
                        db.execute(
                            "SELECT body FROM entries ORDER BY id DESC LIMIT ?",
                            (min(2000, max(1, limit)),),
                        ).fetchall()
                    )
                ]
        except (OSError, sqlite3.Error):
            return [
                {
                    "timestamp": time.time(),
                    "type": "diagnostics.storage_failed",
                    "severity": "error",
                    "message": "Persistent lifecycle diagnostic storage unavailable",
                }
            ]


def container_fields(container):
    attrs = getattr(container, "attrs", {})
    state = attrs.get("State", {})
    return {
        "container": getattr(container, "id", attrs.get("Id")),
        "generation": state.get("StartedAt"),
        "running": state.get("Running"),
        "health": state.get("Health", {}).get("Status"),
        "exitCode": state.get("ExitCode"),
        "oomKilled": state.get("OOMKilled"),
    }


def error_fields(error):
    # Exception text often contains Docker commands, URLs, headers or uploaded keys.
    trace = error.__traceback__
    while trace and trace.tb_next:
        trace = trace.tb_next
    site = (
        (
            Path(trace.tb_frame.f_code.co_filename).name
            + ":"
            + str(trace.tb_lineno)
            + ":"
            + trace.tb_frame.f_code.co_name
        )
        if trace
        else None
    )
    return {
        "errorType": type(error).__name__,
        "errorCode": getattr(error, "code", None),
        "httpStatus": getattr(error, "status_code", None),
        "errorSite": site,
    }


def emit(runtime, event, **fields):
    diagnostics = getattr(runtime, "diagnostics", None)
    if diagnostics:
        diagnostics.record(event, **fields)


class DockerObserver:
    """Passive owner-scoped events, kept after Docker removes the old container."""

    actions = {
        "create",
        "start",
        "stop",
        "die",
        "destroy",
        "kill",
        "restart",
        "oom",
        "pause",
        "unpause",
    }

    def __init__(self, runtime, diagnostics):
        self.runtime, self.diagnostics = runtime, diagnostics
        self.stream = None

    def event(self, event):
        if event.get("Type") != "container":
            return
        actor = event.get("Actor", {})
        attrs = actor.get("Attributes", {})
        if not (
            attrs.get("org.mediahub.mediavpn.owner") == "mediavpn"
            or attrs.get("org.mediahub.vpn.relay") in {"plex", "jellyfin"}
            or attrs.get("org.mediahub.package") == "org.mediahub.plex"
        ):
            return
        action = event.get("Action", "")
        if action not in self.actions and not action.startswith("health_status:"):
            return
        fields = {
            "container": actor.get("ID"),
            "action": action,
            "origin": "docker_observer",
            "reason": "docker_event_caller_not_reported",
            "exitCode": attrs.get("exitCode"),
            "signal": attrs.get("signal"),
            "sourceTimestamp": event.get("time"),
        }
        try:
            row = self.runtime.docker().containers.get(actor["ID"])
            fields.update({key: value for key, value in container_fields(row).items() if value is not None})
            if attrs.get("exitCode") is not None:
                fields["exitCode"] = attrs["exitCode"]
            if attrs.get("org.mediahub.vpn.relay") in {
                "plex",
                "jellyfin",
            } and action.startswith("health_status:"):
                result = row.exec_run(["wg", "show", "wg-hub", "latest-handshakes"])
                if result.exit_code == 0:
                    stamps = [
                        int(line.split()[-1])
                        for line in result.output.decode().splitlines()
                    ]
                    fields["handshakeAge"] = (
                        round(time.time() - max(stamps), 2)
                        if stamps and max(stamps) > 0
                        else -1
                    )
        except Exception:
            pass  # die/destroy attributes still survive a removal race.
        self.diagnostics.record("docker.event", **fields)

    def run(self, stopping):
        since = int(time.time())
        last = None
        self.diagnostics.record(
            "observer.started",
            origin="docker_observer",
            reason="observation_window_started",
        )
        while not stopping.is_set():
            try:
                self.stream = self.runtime.docker().events(
                    since=since,
                    until=int(time.time()) + 15,
                    decode=True,
                    filters={"type": "container", "event": [*sorted(self.actions), "health_status"]},
                )
                for event in self.stream:
                    if stopping.is_set():
                        break
                    marker = (
                        event.get("timeNano"),
                        event.get("Actor", {}).get("ID"),
                        event.get("Action"),
                    )
                    if (
                        last is None
                        or marker[0] > last[0]
                        or (marker[0] == last[0] and marker != last)
                    ):
                        self.event(event)
                        last = marker
                    since = max(since, event.get("time", since))
                    if stopping.is_set():
                        break
            except Exception as error:
                self.diagnostics.record(
                    "observer.error",
                    origin="docker_observer",
                    reason="event_stream_interrupted",
                    **error_fields(error),
                )
                stopping.wait(5)
            finally:
                if self.stream is not None:
                    with contextlib.suppress(Exception):
                        self.stream.close()
                    self.stream = None
