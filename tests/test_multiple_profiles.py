"""Concurrent provider contracts, using synthetic Docker and VPN data."""
import base64
import json
import threading
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_app import login
from test_configuration import CONFIG, KEY
from test_web_gateway import Docker

from mediavpn.app import create_app
from mediavpn.runtime import Runtime
from mediavpn.seedbox import Seedbox
from mediavpn.shared_gateway import SharedGateway
from mediavpn.web_gateway import WebGateway


def assert_desired(path):
    with sqlite3.connect(path / "mediavpn.sqlite") as db:
        assert db.execute("SELECT desired FROM profiles").fetchall() == [(1,), (1,)]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    started, stopped, seed_stops = [], [], []
    runtime = SimpleNamespace(enabled=False, start=lambda identifier, **kw: started.append(identifier),
        stop=lambda identifier, **kw: stopped.append(identifier), container=lambda _: None,
        status=lambda _: {"healthy": False, "state": "stopped", "forwardedPort": None})
    monkeypatch.setattr(Seedbox, "reconcile", lambda self: None)
    monkeypatch.setattr(Seedbox, "status", lambda self: {**self.settings(), "ready": False})
    monkeypatch.setattr(Seedbox, "stop", lambda self: seed_stops.append(self.settings()["profileId"]))
    monkeypatch.setattr(SharedGateway, "reconcile", lambda self: None)
    monkeypatch.setattr(SharedGateway, "stop_helper", lambda self: None)
    monkeypatch.setattr(WebGateway, "reconcile", lambda self: None)
    monkeypatch.setattr(WebGateway, "stop", lambda self: None)
    app = create_app(tmp_path, "http://192.168.1.50", runtime=runtime)
    client = TestClient(app, base_url="http://192.168.1.50")
    login(client)
    ids = []
    for index in (1, 2):
        key = base64.b64encode(bytes([index]) * 32).decode()
        result = client.post("/api/profiles", json={"name": str(index), "configuration": CONFIG.replace(KEY, key), "port_forwarding": True})
        assert result.status_code == 201
        ids.append(result.json()["id"])
    return app, client, runtime, ids, started, stopped, seed_stops


def test_existing_seed_settings_survive_shared_selection_and_restart(setup, tmp_path):
    app, client, runtime, (seed, plex), started, stopped, seed_stops = setup
    assert client.put("/api/seedbox", json={"enabled": True, "profileId": seed}).status_code == 200
    controller = Seedbox(runtime, tmp_path)
    original = {**controller.settings(), "maxActiveDownloads": 10, "rssRefreshMinutes": 1}
    controller.path.write_text(json.dumps(original))
    assert client.post(f"/api/profiles/{seed}/start").status_code == 200
    result = client.put("/api/shared", json={"enabled": True, "profileId": plex})
    assert result.status_code == 200, result.text
    assert Seedbox(runtime, tmp_path).settings() == original
    assert set(started) == {seed, plex} and not stopped and not seed_stops
    assert_desired(tmp_path)
    restarted = TestClient(create_app(tmp_path, "http://192.168.1.50", runtime=runtime), base_url="http://192.168.1.50")
    restarted.post("/api/auth/login", json={"username": "synthetic", "password": "synthetic-test-password"})
    assert_desired(tmp_path)
    assert Seedbox(runtime, tmp_path).settings() == original
    assert client.put("/api/seedbox", json={"enabled": True, "profileId": seed}).status_code == 200


def test_web_gateway_does_not_reassign_seed_or_stop_other_profile(setup, monkeypatch, tmp_path):
    app, client, runtime, (seed, plex), started, stopped, seed_stops = setup
    client.put("/api/seedbox", json={"enabled": True, "profileId": seed})
    client.post(f"/api/profiles/{seed}/start")
    web = app.state.web_gateway
    monkeypatch.setattr(web, "prerequisites", lambda **kw: {"ready": True})
    token = (tmp_path / "integrations/mediahub.token").read_text()
    result = client.put("/api/integration/web-gateway", headers={"X-MediaHub-Key": token}, json={
        "enabled": True, "profileId": plex, "routes": [{"id": "test", "hostname": "app.example.com", "upstream": "http://192.168.1.51:8080"}], "cloudflareToken": "synthetic-cloudflare-token", "plex": {"address": "192.168.1.51", "port": 32400}})
    assert result.status_code == 200, result.text
    assert Seedbox(runtime, tmp_path).settings()["profileId"] == seed
    assert_desired(tmp_path)
    assert not stopped and not seed_stops


def test_provider_replacement_and_stop_are_scoped(tmp_path):
    docker = Docker()
    runtime = Runtime(tmp_path, True, "/approved", docker)
    runtime.shared_profile = lambda: "plex"
    runtime.shared_endpoint = lambda: ("192.168.1.50", 18804)
    runtime.web_targets = lambda: ["192.168.1.51/32"]
    calls = []
    runtime.stop_web = lambda: calls.append("web")
    runtime.stop_shared = lambda: calls.append("shared")
    runtime.stop_dependents = lambda profile: calls.append(profile)
    runtime.start("plex")
    plex = runtime.container("plex")
    runtime.start("seed")
    runtime.start("seed", port_forwarding=True)
    runtime.stop("seed")
    assert calls == ["seed", "seed"]
    assert plex.attrs["State"]["Running"]
    assert "ports" not in docker.created[-1]
    assert docker.created[-1]["environment"]["FIREWALL_OUTBOUND_SUBNETS"] == ""


def test_dedicated_seed_uses_own_forwarded_port(tmp_path):
    runtime = SimpleNamespace(enabled=False, status=lambda _: {"healthy": True, "forwardedPort": 45001})
    seed = Seedbox(runtime, tmp_path)
    web = WebGateway(runtime, tmp_path)
    web.path.write_text(json.dumps({"enabled": True, "profileId": "plex"}))
    seed.web_gateway = web
    assert seed.observed("seed")["forwardedPort"] == 45001
    assert "webGateway" not in seed.observed("seed")


def test_supervisor_continues_other_profile_when_one_start_fails(setup, monkeypatch):
    app, client, runtime, (seed, plex), started, stopped, seed_stops = setup
    client.put("/api/seedbox", json={"enabled": True, "profileId": seed})
    client.post(f"/api/profiles/{seed}/start")
    client.put("/api/shared", json={"enabled": True, "profileId": plex})
    observed = threading.Event()
    def start(identifier, **kw):
        if identifier == seed:
            raise HTTPException(503, "synthetic provider unavailable")
        observed.set()
    runtime.start = start
    runtime.enabled = True
    monkeypatch.setattr(WebGateway, "close", lambda self: None)
    with client:
        assert observed.wait(2)
        assert seed in seed_stops
        assert plex not in stopped


def test_upgrade_shared_seed_can_move_to_dedicated_profile(setup, tmp_path):
    app, client, runtime, (seed, plex), started, stopped, seed_stops = setup
    # Existing releases used one profile for both consumers.
    client.put("/api/seedbox", json={"enabled": True, "profileId": plex})
    assert client.put("/api/shared", json={"enabled": True, "profileId": plex}).status_code == 200
    original_keys = {p.name: p.read_bytes() for p in (tmp_path / "profiles").glob("*.conf")}
    assert client.post(f"/api/profiles/{seed}/start").status_code == 200
    assert client.put("/api/seedbox", json={"enabled": True, "profileId": seed}).status_code == 200
    assert app.state.shared_gateway.profile() == plex
    assert Seedbox(runtime, tmp_path).settings()["profileId"] == seed
    assert_desired(tmp_path)
    assert {p.name: p.read_bytes() for p in (tmp_path / "profiles").glob("*.conf")} == original_keys
    runtime.enabled = True
    assert client.post(f"/api/profiles/{plex}/stop").status_code == 200
    assert seed not in stopped and not seed_stops


def test_gateway_switch_releases_old_port_before_starting_new_profile(setup):
    app, client, runtime, (old, new), started, stopped, seed_stops = setup
    assert client.put("/api/shared", json={"enabled": True, "profileId": old}).status_code == 200
    started.clear()
    assert client.put("/api/shared", json={"enabled": True, "profileId": new}).status_code == 200
    assert started == [old, new]
    assert app.state.shared_gateway.profile() == new
    assert not stopped
