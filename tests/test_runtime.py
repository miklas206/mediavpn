from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from mediavpn.runtime import Runtime


def test_requires_recent_successful_healthcheck(tmp_path):
    container = SimpleNamespace(attrs={"State": {"Running": True, "Health": {"Status": "healthy", "Log": []}}}, reload=lambda: None)
    runtime = Runtime(tmp_path, True, "/opt/mediavpn-data", SimpleNamespace(containers=SimpleNamespace(list=lambda **kwargs: [container])))
    assert runtime.status("synthetic")["healthy"] is False
    for age, expected in [(0, True), (150, False)]:
        container.attrs["State"]["Health"]["Log"] = [{"End": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(), "ExitCode": 0}]
        assert runtime.status("synthetic")["healthy"] is expected


def test_preview_never_claims_health(tmp_path):
    assert Runtime(tmp_path).status("synthetic")["state"] == "disabled"


def test_start_container_contract(tmp_path):
    calls = []
    containers = SimpleNamespace(list=lambda **kwargs: [], run=lambda *args, **kwargs: calls.append((args, kwargs)))
    runtime = Runtime(tmp_path, True, "/opt/mediavpn-data", SimpleNamespace(containers=containers))
    runtime.start("synthetic")
    options = calls[0][1]
    assert options["environment"]["FIREWALL"] == "on"
    assert options["cap_add"] == ["NET_ADMIN"]
    assert options["devices"] == ["/dev/net/tun:/dev/net/tun:rwm"]
    assert options["volumes"] == {"/opt/mediavpn-data/profiles/synthetic.conf": {"bind": "/gluetun/wireguard/wg0.conf", "mode": "ro"}}
    assert "ports" not in options
    assert "privileged" not in options
    assert "WIREGUARD_PRIVATE_KEY" not in options["environment"]