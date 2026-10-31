import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from mediavpn.app import create_app
from mediavpn.seedbox import Seedbox
from mediavpn.transfer_settings import TransferSettings

VALUES = dict(maxConnections=500, maxConnectionsPerTorrent=100, maxUploadSlots=20,
              maxUploadSlotsPerTorrent=4, maxActiveUploads=-1, maxActiveDownloads=8)

@pytest.mark.parametrize('older', [False, True])
def test_transfer_settings_preserve_existing_defaults_and_survive_restart(tmp_path, older):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    if older:
        controller.path.write_text(json.dumps({'enabled': False, 'profileId': None, 'preserved': 'value'}))
    controller.status = lambda: {'ready': True}
    controller.child = lambda: 'synthetic'
    preferences = {**TransferSettings(**VALUES).preferences(), 'listen_port': 54321, 'up_limit': 0}
    writes = []
    def execute(child, path, method, payload):
        if path == 'app/setPreferences':
            fields = json.loads(payload['json']);writes.append(fields);preferences.update(fields)
        return {'status': 200, 'body': base64.b64encode(json.dumps(preferences).encode()).decode()}
    controller.execute = execute
    before = controller.path.read_bytes() if controller.path.exists() else None
    token = controller.token_path.read_bytes()
    assert controller.saved_transfer_preferences() == {}
    assert controller.transfer_settings() == VALUES
    assert not writes
    assert (controller.path.read_bytes() if controller.path.exists() else None) == before
    desired = TransferSettings(**{**VALUES, 'maxUploadSlotsPerTorrent': 16, 'maxActiveUploads': 12, 'maxActiveDownloads': -1})
    assert controller.transfer_settings(desired) == desired.model_dump()
    assert writes == [desired.preferences()]
    assert preferences['listen_port'] == 54321 and preferences['up_limit'] == 0
    restarted = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    assert restarted.download_limit() == -1
    assert restarted.saved_transfer_preferences()['max_uploads_per_torrent'] == 16
    assert restarted.saved_transfer_preferences()['max_active_uploads'] == 12
    assert restarted.token_path.read_bytes() == token
    if older: assert restarted.settings()['preserved'] == 'value'


def test_failed_apply_rolls_back_without_persisting(tmp_path):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    controller.status = lambda: {'ready': True}
    controller.child = lambda: 'synthetic'
    preferences = TransferSettings(**VALUES).preferences()
    writes = []
    def execute(child, path, method, payload):
        if path == 'app/setPreferences':
            fields = json.loads(payload['json']); writes.append(fields)
            preferences.update({k:v for k,v in fields.items() if k != 'max_uploads_per_torrent'})
        return {'body': base64.b64encode(json.dumps(preferences).encode()).decode()}
    controller.execute = execute
    with pytest.raises(HTTPException) as error:
        controller.transfer_settings(TransferSettings(**{**VALUES, 'maxConnections': 800, 'maxUploadSlotsPerTorrent': 16}))
    assert error.value.status_code == 503
    assert len(writes) == 2
    assert preferences == TransferSettings(**VALUES).preferences()
    assert controller.saved_transfer_preferences() == {}


def test_machine_routes_auth_validation_and_readiness(tmp_path, monkeypatch):
    monkeypatch.setattr(Seedbox, 'reconcile', lambda self: None)
    writes = []
    def settings(self, body=None):
        if body is not None: writes.append(body)
        return body.model_dump() if body is not None else VALUES
    monkeypatch.setattr(Seedbox, 'transfer_settings', settings)
    with TestClient(create_app(tmp_path, 'http://testserver', runtime=SimpleNamespace(enabled=True))) as client:
        url = '/api/integration/transfer'
        assert client.get(url).status_code == 401
        assert client.put(url, json=VALUES).status_code == 401
        headers = {'X-MediaSeed-Key': (tmp_path/'integrations/mediaseed.token').read_text()}
        assert client.get(url, headers=headers).json() == VALUES
        for value in [0, -2, 1001, True, 1.5, '16']:
            assert client.put(url, headers=headers, json={**VALUES, 'maxUploadSlotsPerTorrent': value}).status_code == 422
        assert client.put(url, headers=headers, json={**VALUES, 'listen_port': 80}).status_code == 422
        assert not writes
        assert client.put(url, headers=headers, json=VALUES).json() == VALUES


def test_not_ready_cannot_write_settings(tmp_path):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    controller.status = lambda: {'ready': False}
    with pytest.raises(HTTPException) as error:
        controller.transfer_settings(TransferSettings(**VALUES))
    assert error.value.status_code == 409 and controller.saved_transfer_preferences() == {}
