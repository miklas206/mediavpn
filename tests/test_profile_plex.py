import json
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_multiple_profiles import setup  # noqa: F401

from mediavpn.relay_image import plex_forward
from mediavpn.shared_gateway import SharedGateway, new_key


def test_fresh_upgrade_and_port_rotation_keep_profile_identity(tmp_path):
    observed = {"healthy": True, "publicIp": "8.8.8.8", "forwardedPort": 50001}
    runtime = SimpleNamespace(enabled=False, status=lambda _: observed)
    profile = "a" * 24
    relay = SharedGateway(runtime, tmp_path, "https://192.168.1.50", profile, 18805)
    relay.configure(profile, True)
    _, key = new_key()
    relay.peer("owned-plex", key, "Plex")
    keys = (relay.data / "keys.json").read_bytes()
    relay.configure_plex("owned-plex")
    config = json.loads(relay.config.read_text())
    assert config["plexForward"]["publicPort"] == 50001
    assert config["plexForward"]["address"] == "10.250.87.2"
    upgraded = SharedGateway(runtime, tmp_path, relay.origin, profile, 18805)
    observed["forwardedPort"] = 50002
    upgraded.write_config(upgraded.settings())
    assert json.loads(relay.config.read_text())["plexForward"]["publicPort"] == 50002
    assert (relay.data / "keys.json").read_bytes() == keys
    observed["healthy"] = False
    upgraded.write_config(upgraded.settings())
    assert json.loads(relay.config.read_text())["plexForward"] is None
    relay.remove_peer("owned-plex")
    assert json.loads(relay.config.read_text())["plexForward"] is None


def test_only_existing_dedicated_peer_can_own_port(tmp_path):
    relay = SharedGateway(SimpleNamespace(enabled=False), tmp_path, "https://192.168.1.50", "a" * 24, 18805)
    relay.configure("a" * 24, True)
    with pytest.raises(HTTPException):
        relay.configure_plex("foreign-peer")


def test_forward_rules_expire_rotate_and_never_allow_general_forwarding(monkeypatch):
    calls, rules = [], set()

    def run(*args, check=True):
        calls.append(args)
        if "-C" in args:
            return SimpleNamespace(returncode=int(args not in rules))
        if "-F" in args:
            rules.clear()
        if "-A" in args or "-I" in args:
            probe = tuple("-C" if a in ("-A", "-I") else a for a in args)
            rules.add(probe)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(plex_forward, "run", run)
    monkeypatch.setattr(plex_forward, "previous", object())
    value = {"mode": "gateway", "peers": [{"address": "10.250.87.2"}], "plexForward": {
        "address": "10.250.87.2", "publicPort": 50001, "expiresAt": time.time() + 45}}
    plex_forward.apply(value)
    assert any("10.250.87.2:32400" in c for c in calls)
    assert all(c[-1] != "ACCEPT" or ("32400" in c and "DNAT" in c) for c in calls)
    calls.clear()
    value["plexForward"]["publicPort"] = 50002
    plex_forward.apply(value)
    assert any("-F" in c for c in calls)
    assert any("50002" in c for c in calls)
    calls.clear()
    value["plexForward"]["expiresAt"] = time.time() - 1
    plex_forward.apply(value)
    assert any("-F" in c for c in calls)
    assert not any("--to-destination" in c for c in calls)
    value["plexForward"]["address"] = "192.168.1.1"
    with pytest.raises(ValueError):
        plex_forward.apply(value)


def test_machine_api_auth_and_conflicting_torrent_port(setup, tmp_path):  # noqa: F811
    app, client, runtime, ids, *_ = setup
    runtime.enabled = True
    path = "/api/integration/shared/profiles/" + ids[0]
    headers = {"X-MediaHub-Key": (tmp_path / "integrations/mediahub.token").read_text()}
    assert client.put(path + "/plex", json={"peerId": "owned"}).status_code == 401
    assert client.put(path, headers=headers).status_code == 200
    _, key = new_key()
    assert client.post(path + "/peers", headers=headers, json={"id": "owned", "name": "Plex", "publicKey": key}).status_code == 200
    assert client.put(path + "/plex", headers=headers, json={"peerId": "foreign"}).status_code == 409
    assert client.put(path + "/plex", headers=headers, json={"peerId": "owned", "address": "8.8.8.8"}).status_code == 422
    assert client.put(path + "/plex", headers=headers, json={"peerId": "owned"}).status_code == 200
    assert client.put("/api/seedbox", json={"enabled": True, "profileId": ids[0]}).status_code == 409
    assert client.delete(path + "/plex", headers=headers).status_code == 200
    assert client.put("/api/seedbox", json={"enabled": True, "profileId": ids[0]}).status_code == 200
    assert client.put(path + "/plex", headers=headers, json={"peerId": "owned"}).status_code == 409
