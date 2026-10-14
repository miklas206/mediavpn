import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_web_gateway import controlled, settings  # noqa: F401 - shared pytest fixture

from mediavpn.gateway_image import plex_port
from mediavpn.web_gateway import PlexTarget


def value():
    return {
        "healthy": True,
        "checkedAt": time.time(),
        "generation": "one",
        "revision": "current",
        "publicIp": "8.8.8.8",
        "publicPort": 45000,
        "plexInternalPort": 60000,
        "reservedInternalPorts": [45000, 39000, 32400, 51879],
        "plex": {"address": "192.168.1.50", "port": 32400},
    }


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "8.8.8.8", "0.0.0.0", "169.254.1.2", "::1"]
)
def test_plex_target_rejects_unapproved_networks(address):
    with pytest.raises(ValueError):
        PlexTarget(address=address)
    with pytest.raises(ValueError):
        plex_port.target({**value(), "plex": {"address": address, "port": 32400}})


def test_plex_listener_cannot_reuse_other_internal_ports():
    with pytest.raises(ValueError):
        plex_port.target({**value(), "plexInternalPort": 39000})
    with pytest.raises(ValueError):
        PlexTarget(address="192.168.1.50", port=8080)


def test_plex_helper_uses_third_port_and_closes_listener_on_disable(
    tmp_path, monkeypatch
):
    config = tmp_path / "config"
    config.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    torrent = tmp_path / "torrent"
    torrent.mkdir()
    data = value()
    (config / "settings.json").write_text(json.dumps(data))
    (torrent / "status.json").write_text(
        json.dumps(
            {
                "ready": True,
                "generation": "one",
                "publicIp": "8.8.8.8",
                "publicPort": 55000,
                "expiresAt": time.time() + 60,
            }
        )
    )
    monkeypatch.setattr(plex_port, "Path", lambda p: tmp_path / str(p).lstrip("/"))

    class Event:
        def __init__(self):
            self.stopped = False
            self.ticks = 0

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, _):
            self.ticks += 1
            if self.ticks == 1:
                snapshot = json.loads((state / "status.json").read_text())
                assert snapshot["ready"] and snapshot["publicPort"] == 55001
                data["plex"] = None
                (config / "settings.json").write_text(json.dumps(data))
            else:
                self.stopped = True

    event = Event()
    monkeypatch.setattr(plex_port, "threading", SimpleNamespace(Event=lambda: event))
    monkeypatch.setattr(plex_port.signal, "signal", lambda *_: None)
    process = SimpleNamespace(poll=lambda: None)
    start = Mock(return_value=process)
    stop = Mock()
    firewall = Mock()
    calls = []

    def exchange(opcode, private, public, lifetime, forbidden):
        calls.append((opcode, private, public, lifetime, set(forbidden)))
        if lifetime:
            assert forbidden == {45000, 55000}
        return {"internalPort": private, "publicPort": 55001, "lifetime": 60}

    monkeypatch.setattr(plex_port, "request", exchange)
    monkeypatch.setattr(plex_port, "tunnel_address", lambda: "10.2.0.2")
    monkeypatch.setattr(plex_port, "start_relay", start)
    monkeypatch.setattr(plex_port, "stop_relay", stop)
    monkeypatch.setattr(plex_port, "firewall", firewall)
    plex_port.main()
    assert calls == [(2, 60000, 0, 60, {45000, 55000}), (2, 60000, 55001, 0, set())]
    start.assert_called_once_with("192.168.1.50", 60000, "10.2.0.2")
    assert any(c.args == (process,) for c in stop.call_args_list)
    assert json.loads((state / "status.json").read_text())["ready"] is False


def test_plex_helper_shares_provider_and_is_stopped_when_vpn_fails(controlled):
    gateway, _runtime, docker, _, observed = controlled
    gateway.configure(settings(plex={"address": "192.168.1.50", "port": 32400}))
    gateway.reconcile()
    helper = next(row for row in docker.created if row["name"] == "mediavpn-plex-lease")
    assert helper["network_mode"] == "container:provider"
    assert helper["cap_add"] == ["NET_ADMIN"]
    assert "ports" not in helper
    assert any(
        m["bind"] == "/torrent" and m["mode"] == "ro"
        for m in helper["volumes"].values()
    )
    observed["healthy"] = False
    gateway.reconcile()
    assert not docker.rows


def test_old_settings_do_not_enable_plex_implicitly(controlled):
    gateway, _, docker, _, _ = controlled
    gateway.reconcile()
    assert not any(row["name"] == "mediavpn-plex-lease" for row in docker.created)
    assert gateway.public()["plexPort"]["enabled"] is False


def test_proton_zero_port_acknowledges_only_explicit_deletion():
    import struct
    from mediavpn.gateway_image.natpmp import mapping
    packet = struct.pack("!BBHIHHI", 0, 130, 0, 123, 63213, 0, 0)
    assert mapping(packet, 2, 63213, set(), deleting=True)["lifetime"] == 0
    with pytest.raises(ValueError):
        mapping(packet, 2, 63213, set())
    with pytest.raises(ValueError):
        mapping(packet, 2, 0, set(), deleting=True)
