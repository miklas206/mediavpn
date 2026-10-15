from types import SimpleNamespace

from test_multiple_profiles import setup  # noqa: F401
from test_web_gateway import Docker

from mediavpn.profile_relays import ProfileRelays
from mediavpn.runtime import Runtime
from mediavpn.shared_gateway import SharedGateway, new_key


def test_fresh_and_upgrade_relays_keep_keys_peers_and_ports(tmp_path):
    runtime = SimpleNamespace(enabled=False)
    legacy = SharedGateway(runtime, tmp_path, "https://192.168.1.50")
    first, second = "a" * 24, "b" * 24
    legacy.configure(first, True)
    _, key = new_key()
    original = legacy.peer("plex", key, "Plex")
    registry = ProfileRelays(runtime, tmp_path, legacy.origin, legacy)
    assert registry.get(first, True) is legacy
    relay = registry.get(second, True)
    relay.configure(second, True)
    added = relay.peer("jellyfin", key, "Jellyfin")
    assert added["endpointPort"] != original["endpointPort"]
    assert added["gatewayKey"] != original["gatewayKey"]
    reopened = ProfileRelays(runtime, tmp_path, legacy.origin, legacy)
    assert reopened.get(second).peer("jellyfin", key, "Jellyfin") == added
    reopened.get(second).remove_peer("jellyfin")
    assert legacy.peer("plex", key, "Plex") == original
    assert reopened.get(second).settings()["peers"] == []


def test_two_profiles_publish_distinct_private_ports_without_web_exceptions(tmp_path):
    docker = Docker()
    runtime = Runtime(tmp_path, True, "/approved", docker)
    legacy = SharedGateway(runtime, tmp_path, "https://192.168.1.50")
    registry = ProfileRelays(runtime, tmp_path, legacy.origin, legacy)
    for identifier in ("a" * 24, "b" * 24):
        registry.get(identifier, True).configure(identifier, True)
        runtime.start(identifier)
    assert docker.created[0]["ports"] != docker.created[1]["ports"]
    assert all(row["environment"]["FIREWALL_OUTBOUND_SUBNETS"] == "" for row in docker.created)


def test_profile_api_requires_machine_key_and_preserves_other_profiles(setup, tmp_path):  # noqa: F811
    _app, client, runtime, ids, _started, stopped, _ = setup
    path = "/api/integration/shared/profiles/"
    assert client.put(path + ids[0]).status_code == 401
    runtime.enabled = True
    headers = {"X-MediaHub-Key": (tmp_path / "integrations/mediahub.token").read_text()}
    _, key = new_key()
    for identifier in ids:
        assert client.put(path + identifier, headers=headers).status_code == 200
        result = client.post(path + identifier + "/peers", headers=headers, json={"id": identifier, "name": "Synthetic", "publicKey": key})
        assert result.status_code == 200
    assert client.delete(path + ids[0] + "/peers/" + ids[0], headers=headers).status_code == 200
    assert client.get(path + ids[1], headers=headers).json()["peers"]
    assert not stopped
    assert client.put(path + "invalid", headers=headers).status_code in (404, 422)
    assert client.delete("/api/integration/shared/app-peers/" + ids[1], headers=headers).status_code == 200
    assert not client.get(path + ids[1], headers=headers).json()["peers"]


def test_stopping_one_relay_preserves_other_and_legacy_helpers(tmp_path):
    docker = Docker()
    runtime = Runtime(tmp_path, True, "/approved", docker)
    legacy = SharedGateway(runtime, tmp_path, "https://192.168.1.50")
    registry = ProfileRelays(runtime, tmp_path, legacy.origin, legacy)
    helpers = []
    for scope in (None, "a" * 24, "b" * 24):
        helpers.append(docker.run("synthetic", name=scope or "legacy", labels={"org.mediahub.vpn.shared-gateway": "mediavpn", **({"org.mediahub.vpn.relay-scope": scope} if scope else {})}))
    registry.get("a" * 24, True).stop_helper()
    assert helpers[0] in docker.rows and helpers[2] in docker.rows
    assert helpers[1] not in docker.rows
    assert legacy.relay() is helpers[0]
