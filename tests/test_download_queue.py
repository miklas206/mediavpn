import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mediavpn.app import create_app
from mediavpn.seedbox import Seedbox


def test_fresh_and_older_queue_config_preserves_settings_and_survives_restart(tmp_path):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    controller.path.write_text(
        json.dumps({"enabled": False, "profileId": None, "preserved": "value"})
    )
    controller.reconcile = lambda: None
    controller.status = lambda: {"ready": True}
    controller.child = lambda: "synthetic"
    preferences = {"max_active_downloads": 8, "queueing_enabled": True}
    writes = []

    def execute(child, path, method, payload, content=None):
        if path == "app/setPreferences":
            value = json.loads(payload["json"])
            writes.append(value)
            preferences.update(value)
        return {
            "status": 200,
            "body": base64.b64encode(json.dumps(preferences).encode()).decode(),
        }

    controller.execute = execute
    assert controller.download_limit() == 8
    assert controller.queue_settings(3) == {
        "maxActiveDownloads": 3,
        "queueingEnabled": True,
    }
    assert writes == [{"max_active_downloads": 3, "queueing_enabled": True}]
    restarted = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    assert restarted.download_limit() == 3
    assert restarted.settings()["preserved"] == "value"
    restarted.reconcile = lambda: None
    restarted.configure(None, False)
    assert restarted.download_limit() == 3
    assert restarted.settings()["preserved"] == "value"
    assert restarted.token_path.read_text() == controller.token_path.read_text()


def test_machine_queue_routes_validate_auth_and_bounded_integers(tmp_path, monkeypatch):
    monkeypatch.setattr(Seedbox, "reconcile", lambda self: None)
    monkeypatch.setattr(
        Seedbox,
        "queue_settings",
        lambda self, limit=None: {
            "maxActiveDownloads": limit or 8,
            "queueingEnabled": True,
        },
    )
    with TestClient(
        create_app(tmp_path, "http://testserver", runtime=SimpleNamespace(enabled=True))
    ) as client:
        assert client.get("/api/integration/queue").status_code == 401
        assert (
            client.put(
                "/api/integration/queue", json={"maxActiveDownloads": 3}
            ).status_code
            == 401
        )
        token = (tmp_path / "integrations/mediaseed.token").read_text().strip()
        headers = {"X-MediaSeed-Key": token}
        for value in [0, 101, True, 1.5, "3"]:
            assert (
                client.put(
                    "/api/integration/queue",
                    json={"maxActiveDownloads": value},
                    headers=headers,
                ).status_code
                == 422
            )
        assert (
            client.put(
                "/api/integration/queue",
                json={"maxActiveDownloads": 3, "listen_port": 80},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.put(
                "/api/integration/queue",
                json={"maxActiveDownloads": 3},
                headers=headers,
            ).json()["maxActiveDownloads"]
            == 3
        )


def test_queue_readiness_failure_does_not_save_new_limit(tmp_path):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    controller.reconcile = lambda: None
    controller.status = lambda: {"ready": False}
    with pytest.raises(HTTPException) as error:
        controller.queue_settings(3)
    assert error.value.status_code == 409
    assert controller.download_limit() == 8
