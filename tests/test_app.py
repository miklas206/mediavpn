from fastapi.testclient import TestClient

from mediavpn.app import create_app
from test_configuration import CONFIG, KEY


def login(client):
    body = {"username": "synthetic", "password": "synthetic-test-password"}
    assert client.post("/api/auth/setup", json=body).status_code == 200
    result = client.post("/api/auth/login", json=body)
    client.headers["X-MediaVPN-CSRF"] = result.json()["csrf"]


def test_profile_lifecycle_without_secrets(tmp_path):
    client = TestClient(create_app(tmp_path, "http://testserver"))
    assert client.get("/api/profiles").status_code == 401
    login(client)
    result = client.post("/api/profiles", json={"name": "Test", "configuration": CONFIG})
    assert result.status_code == 201
    identifier = result.json()["id"]
    assert KEY not in client.get("/api/profiles").text
    assert client.post("/api/profiles", json={"name": "Duplicate", "configuration": CONFIG}).status_code == 409
    assert client.post(f"/api/profiles/{identifier}/start").status_code == 409
    assert client.get("/api/profiles").json()[0]["healthy"] is False
    assert client.delete(f"/api/profiles/{identifier}").status_code == 200
    assert not list((tmp_path / "profiles").glob("*.conf"))


def test_csrf_origin_and_secret_validation(tmp_path):
    client = TestClient(create_app(tmp_path, "http://testserver"))
    login(client)
    assert client.post("/api/profiles", json={"name": "Test", "configuration": CONFIG}, headers={"Origin": "http://evil.test"}).status_code == 403
    client.headers.pop("X-MediaVPN-CSRF")
    assert client.post("/api/profiles", json={"name": "Test", "configuration": CONFIG}).status_code == 403
    assert client.get("/api/profiles/../../secret").status_code != 200


def test_invalid_request_never_echoes_key(tmp_path):
    client = TestClient(create_app(tmp_path, "http://testserver"))
    login(client)
    response = client.post("/api/profiles", json={"name": "Test", "configuration": CONFIG, "extra": KEY})
    assert response.status_code == 422
    assert KEY not in response.text
    assert client.post("/api/profiles", json={"name": "   ", "configuration": CONFIG}).status_code == 422


def test_forwarding_choice_preserves_profile_key_and_survives_restart(tmp_path):
    with TestClient(create_app(tmp_path, "http://testserver")) as client:
        login(client)
        identifier = client.post("/api/profiles", json={"name": "Test", "configuration": CONFIG}).json()["id"]
        path = tmp_path / "profiles" / f"{identifier}.conf"
        original = path.read_bytes()
        assert client.put(f"/api/profiles/{identifier}/forwarding", json={"enabled": True}).status_code == 200
        assert client.get("/api/profiles").json()[0]["port_forwarding"] is True
        assert path.read_bytes() == original
        client.headers["X-MediaVPN-CSRF"] = "invalid"
        assert client.put(f"/api/profiles/{identifier}/forwarding", json={"enabled": False}).status_code == 403
    with TestClient(create_app(tmp_path, "http://testserver")) as restarted:
        response = restarted.post("/api/auth/login", json={"username": "synthetic", "password": "synthetic-test-password"})
        assert response.status_code == 200
        assert restarted.get("/api/profiles").json()[0]["port_forwarding"] is True


def test_machine_api_rejects_missing_or_wrong_key_without_disclosing_profiles(tmp_path):
    with TestClient(create_app(tmp_path, "http://testserver")) as client:
        assert client.get("/api/integration/status").status_code == 401
        assert client.post("/api/integration/rpc", json={"path": "auth/login", "method": "POST"}, headers={"X-MediaSeed-Key": "wrong"}).status_code == 401
        assert client.get("/api/seedbox").status_code == 401
        login(client)
        state = client.get("/api/seedbox").json()
        assert state["ready"] is False and state["profiles"] == []


def test_machine_upload_passes_explicit_start_choice_through_gate(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mediavpn.seedbox import Seedbox
    captured = []
    monkeypatch.setattr(Seedbox, "rpc", lambda self, path, method, payload, content=None: captured.append((payload, content)) or {"status": 204, "body": ""})
    monkeypatch.setattr(Seedbox, "reconcile", lambda self: None)
    with TestClient(create_app(tmp_path, "http://testserver", runtime=SimpleNamespace(enabled=True))) as client:
        token = (tmp_path / "integrations" / "mediaseed.token").read_text().strip()
        for query, expected in [("", "true"), ("?start=true", "false"), ("?start=false", "true"), ("?tags=MediaSeed-Cleanup-" + "a" * 24, "true")]:
            response = client.post("/api/integration/upload" + query, content=b"dee", headers={"X-MediaSeed-Key": token})
            assert response.status_code == 200
            assert captured[-1][0]["stopped"] == captured[-1][0]["paused"] == expected
            assert captured[-1][1] == b"dee"
            if "tags=" in query:
                assert captured[-1][0]["tags"] == "MediaSeed-Cleanup-" + "a" * 24


def test_theme_favicon_loads_without_allowing_external_images_or_inline_scripts(tmp_path):
    with TestClient(create_app(tmp_path, "http://testserver")) as client:
        response = client.get("/")
        policy = dict(part.strip().split(" ", 1) for part in response.headers["Content-Security-Policy"].split(";"))
        assert policy["img-src"].split() == ["'self'", "data:"]
        assert policy["script-src"] == "'self'"
        assert policy["style-src"] == "'self'"
        assert policy["connect-src"] == "'self'"
        assert 'data-brand-favicon' in response.text
