import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mediavpn.app import create_app
from mediavpn.seedbox import Seedbox


@pytest.mark.parametrize("older", [False, True])
def test_rss_default_persistence_and_unapplied_settings(tmp_path, older):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    if older:
        controller.path.write_text(json.dumps({"enabled": False, "profileId": None, "maxActiveDownloads": 3, "preserved": "value"}))
    controller.reconcile = lambda: None
    controller.status = lambda: {"ready": True}
    controller.child = lambda: "synthetic"
    preferences = {"rss_refresh_interval": 5}
    writes = []
    apply = True

    def execute(child, path, method, payload):
        if path == "app/setPreferences":
            value = json.loads(payload["json"])
            writes.append(value)
            if apply:
                preferences.update(value)
        return {"body": base64.b64encode(json.dumps(preferences).encode()).decode()}

    controller.execute = execute
    token = controller.token_path.read_text()
    assert controller.rss_interval() == 5
    assert controller.rss_settings(10) == {"rssRefreshMinutes": 10}
    assert writes == [{"rss_refresh_interval": 10}]
    restarted = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    restarted.reconcile = lambda: None
    restarted.configure(None, False)
    assert restarted.rss_interval() == 10
    assert restarted.token_path.read_text() == token
    if older:
        assert restarted.download_limit() == 3
        assert restarted.settings()["preserved"] == "value"
    apply = False
    with pytest.raises(HTTPException) as error:
        controller.rss_settings(15)
    assert error.value.status_code == 503
    assert controller.rss_interval() == 15  # Desired value survives for reconciliation.


def test_machine_rss_routes_validate_auth_and_strict_input(tmp_path, monkeypatch):
    monkeypatch.setattr(Seedbox, "reconcile", lambda self: None)
    monkeypatch.setattr(Seedbox, "rss_settings", lambda self, minutes=None: {"rssRefreshMinutes": minutes or 5})
    app = create_app(tmp_path, "http://testserver", runtime=SimpleNamespace(enabled=True))
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    headers = {"X-MediaSeed-Key": controller.token_path.read_text()}
    with TestClient(app) as client:
        assert client.get("/api/integration/rss").status_code == 401
        assert client.put("/api/integration/rss", json={"rssRefreshMinutes": 10}).status_code == 401
        assert client.get("/api/integration/rss", headers=headers).json() == {"rssRefreshMinutes": 5}
        for value in [0, -1, 1441, True, 2.5, "5"]:
            assert client.put("/api/integration/rss", json={"rssRefreshMinutes": value}, headers=headers).status_code == 422
        assert client.put("/api/integration/rss", json={"rssRefreshMinutes": 5, "listen_port": 80}, headers=headers).status_code == 422
        assert client.put("/api/integration/rss", json={"rssRefreshMinutes": 10}, headers=headers).json() == {"rssRefreshMinutes": 10}


def test_unready_rss_update_preserves_old_settings(tmp_path):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    controller.reconcile = lambda: None
    controller.status = lambda: {"ready": False}
    with pytest.raises(HTTPException) as error:
        controller.rss_settings(10)
    assert error.value.status_code == 409
    assert controller.rss_interval() == 5
