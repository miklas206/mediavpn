import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_app import login
from test_configuration import CONFIG

from mediavpn.app import create_app
from mediavpn.diagnostics import Diagnostics, DockerObserver, cause, error_fields


@pytest.mark.parametrize("upgrade", [False, True])
def test_fresh_and_upgrade_persist_without_changing_existing_data(tmp_path, upgrade):
    existing = tmp_path / "existing-settings.json"
    if upgrade:
        existing.write_text('{"desired":true,"privateKey":"preserved"}')
    before = existing.read_bytes() if upgrade else None
    first = Diagnostics(tmp_path)
    with cause("user", "server_selection_saved"):
        first.record("request.selection", profile="a" * 24)
        Diagnostics(tmp_path).record("provider.stop_requested", container="b" * 64)
    rows = Diagnostics(tmp_path).entries()
    assert len(rows) == 2
    assert rows[0]["operationId"] == rows[1]["operationId"]
    assert rows[1]["origin"] == "user"
    assert rows[0]["timestamp"] <= rows[1]["timestamp"]
    if upgrade:
        assert existing.read_bytes() == before


def test_diagnostics_reject_credentials_and_exception_content(tmp_path):
    diagnostic = Diagnostics(tmp_path)
    try:
        raise ValueError("password=do-not-log https://secret.example/?token=leak")
    except ValueError as error:
        diagnostic.record(
            "supervisor.error",
            **error_fields(error),
            password="do-not-log",
            configuration={"key": "leak"},
            reason="https://secret.example/?token=leak",
        )
    raw = json.dumps(diagnostic.entries())
    assert "do-not-log" not in raw and "secret.example" not in raw and "leak" not in raw
    assert "ValueError" in raw and "test_diagnostics.py" in raw


def test_bounded_retention_and_repeated_failure_dedup(tmp_path):
    diagnostic = Diagnostics(tmp_path)
    with diagnostic.connect() as db:
        db.executemany(
            "INSERT INTO entries(body) VALUES (?)",
            [(json.dumps({"old": True}),)] * 2010,
        )
    diagnostic.record("provider.created", container="a" * 64)
    with sqlite3.connect(diagnostic.path) as db:
        assert db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2000
    diagnostic.record("provider.error", errorType="ValueError")
    diagnostic.record("provider.error", errorType="ValueError")
    assert (
        len([r for r in diagnostic.entries() if r.get("type") == "vpn.provider.error"])
        == 1
    )


def test_disk_failure_does_not_block_safety_action(tmp_path, caplog):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("preserve")
    diagnostic = Diagnostics(blocked)
    diagnostic.record("provider.stop_requested")
    assert diagnostic.entries()[0]["severity"] == "error"
    assert "storage unavailable" in caplog.text


def test_docker_observer_retains_removed_container_and_ignores_exec_payloads(tmp_path):
    def removed(identifier):
        raise RuntimeError("Docker response containing credentials")

    diagnostic = Diagnostics(tmp_path)
    observer = DockerObserver(
        SimpleNamespace(
            docker=lambda: SimpleNamespace(containers=SimpleNamespace(get=removed))
        ),
        diagnostic,
    )
    base = {
        "Type": "container",
        "Actor": {
            "ID": "a" * 64,
            "Attributes": {
                "org.mediahub.mediavpn.owner": "mediavpn",
                "exitCode": "137",
                "password": "private",
            },
        },
    }
    observer.event({**base, "Action": "die"})
    observer.event({**base, "Action": "exec_create: password=private"})
    observer.event(
        {
            "Type": "container",
            "Action": "die",
            "Actor": {"Attributes": {"name": "unowned"}},
        }
    )
    rows = diagnostic.entries()
    assert len(rows) == 1 and rows[0]["exitCode"] == "137"
    assert rows[0]["reason"] == "docker_event_caller_not_reported"
    assert rows[0]["severity"] == "warning"
    assert "private" not in json.dumps(rows)


def test_authorized_action_has_cause_and_rejected_action_is_not_logged_as_accepted(tmp_path):
    client = TestClient(create_app(tmp_path, "http://testserver"))
    login(client)
    identifier = client.post("/api/profiles", json={"name": "Synthetic", "configuration": CONFIG}).json()["id"]
    assert client.post(f"/api/profiles/{identifier}/stop").status_code == 200
    rows = client.get("/api/diagnostics/logs").json()["entries"]
    accepted = [row for row in rows if row["type"] == "vpn.request.profile_action"]
    assert len(accepted) == 1 and accepted[0]["origin"] == "user" and accepted[0]["action"] == "stop"
    client.headers.pop("X-MediaVPN-CSRF")
    assert client.post(f"/api/profiles/{identifier}/stop").status_code == 403
    assert client.get("/api/diagnostics/logs").json()["entries"] == rows


def test_nonadministrator_cannot_read_diagnostics(tmp_path):
    app = create_app(tmp_path, "http://testserver")
    route = next(route for route in app.routes if route.path == "/api/diagnostics/logs")
    app.dependency_overrides[route.dependant.dependencies[0].call] = lambda: {"role": "viewer"}
    assert TestClient(app).get("/api/diagnostics/logs").status_code == 403


def test_observer_stream_filters_lifecycle_and_closes_on_shutdown(tmp_path):
    stopping = threading.Event()
    calls = []

    class Stream:
        closed = False

        def __iter__(self):
            yield {"Type": "container", "Action": "die", "time": 100, "timeNano": 100000000000,
                   "Actor": {"ID": "a" * 64, "Attributes": {"org.mediahub.mediavpn.owner": "mediavpn", "exitCode": "1"}}}
            stopping.set()

        def close(self):
            self.closed = True

    stream = Stream()

    def events(**options):
        calls.append(options)
        return stream

    client = SimpleNamespace(events=events, containers=SimpleNamespace(get=lambda identifier: SimpleNamespace(attrs={}, id=identifier)))
    diagnostics = Diagnostics(tmp_path)
    DockerObserver(SimpleNamespace(docker=lambda: client), diagnostics).run(stopping)
    assert stream.closed
    assert "die" in calls[0]["filters"]["event"] and "exec_create" not in calls[0]["filters"]["event"]
    assert [row["type"] for row in diagnostics.entries()] == ["vpn.observer.started", "vpn.docker.event"]


def test_log_api_requires_auth_and_survives_app_recreation(tmp_path):
    client = TestClient(create_app(tmp_path, "http://testserver"))
    assert client.get("/api/diagnostics/logs").status_code == 401
    assert client.get("/api/integration/diagnostics/logs").status_code == 401
    login(client)
    Diagnostics(tmp_path).record("provider.created", container="b" * 64)
    assert (
        client.get("/api/diagnostics/logs").json()["entries"][0]["container"]
        == "b" * 64
    )
    restarted = TestClient(create_app(tmp_path, "http://testserver"))
    restarted.cookies.update(client.cookies)
    assert (
        restarted.get("/api/diagnostics/logs").json()["entries"][0]["container"]
        == "b" * 64
    )
