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