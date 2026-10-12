from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from mediavpn.runtime import Runtime
from mediavpn.location import ExitLocation


def test_requires_recent_successful_healthcheck(tmp_path):
    def execute(command):
        if command[0] == "ip":
            return SimpleNamespace(exit_code=0, output=b'[{"ifname":"tun0","flags":["UP"],"addr_info":[{"family":"inet"}]}]')
        if command[-1].endswith("/portforwarded"):
            return SimpleNamespace(exit_code=0, output=b'{"port":43210}')
        return SimpleNamespace(exit_code=0, output=b'{"status":"running"}' if command[-1].endswith("/status") else b'{"public_ip":"8.8.8.8","country":"Sweden"}')
    container = SimpleNamespace(attrs={"State": {"Running": True, "Health": {"Status": "healthy", "Log": []}}}, reload=lambda: None, exec_run=execute)
    runtime = Runtime(tmp_path, True, "/opt/mediavpn-data", SimpleNamespace(containers=SimpleNamespace(list=lambda **kwargs: [container])), location=ExitLocation(fetch=lambda: b'8.8.8.0/24,DK\n'))
    assert runtime.status("synthetic")["healthy"] is False
    for age, expected in [(0, True), (150, False)]:
        container.attrs["State"]["Health"]["Log"] = [{"End": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(), "ExitCode": 0}]
        assert runtime.status("synthetic")["healthy"] is expected
        if expected:
            assert runtime.status("synthetic")["forwardedPort"] == 43210
            assert runtime.status("synthetic")["country"] == "DK"  # Provider overrides misleading generic GeoIP.
        else:
            assert runtime.status("synthetic").get("country") is None
    container.attrs["State"]["Health"]["Log"] = [{"End": datetime.now(timezone.utc).isoformat(), "ExitCode": 0}]
    runtime.location = ExitLocation(fetch=lambda: b'broken')
    assert runtime.status("synthetic")["healthy"] is True
    assert runtime.status("synthetic")["country"] is None
    def failed_location(address):
        raise RuntimeError("Location unavailable")
    runtime.location = SimpleNamespace(country=failed_location)
    assert runtime.status("synthetic")["healthy"] is True
    assert runtime.status("synthetic")["country"] is None
    container.exec_run = lambda command: SimpleNamespace(exit_code=0, output=b'[]' if command[0] == "ip" else b'{"status":"running","public_ip":"8.8.8.8"}')
    assert runtime.status("synthetic")["healthy"] is False


def test_healthy_container_without_public_egress_is_not_connected(tmp_path):
    container = SimpleNamespace(attrs={"State": {"Running": True, "Health": {"Status": "healthy", "Log": [{"End": datetime.now(timezone.utc).isoformat(), "ExitCode": 0}]}}}, reload=lambda: None, exec_run=lambda command: SimpleNamespace(exit_code=0, output=b'{"status":"running","public_ip":"192.168.1.1"}'))
    runtime = Runtime(tmp_path, True, "/opt/mediavpn-data", SimpleNamespace(containers=SimpleNamespace(list=lambda **kwargs: [container])))
    assert runtime.status("synthetic")["healthy"] is False


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
    assert options["volumes"]["/opt/mediavpn-data/profiles/synthetic.conf"] == {"bind": "/gluetun/wireguard/wg0.conf", "mode": "ro"}
    assert options["volumes"]["/opt/mediavpn-data/gluetun-control.toml"]["bind"] == "/gluetun/auth/config.toml"
    assert "ports" not in options
    assert "privileged" not in options
    assert "WIREGUARD_PRIVATE_KEY" not in options["environment"]
    assert options["environment"]["VPN_PORT_FORWARDING"] == "off"
    runtime.start("synthetic", port_forwarding=True)
    assert calls[-1][1]["environment"]["VPN_PORT_FORWARDING"] == "on"
    assert calls[-1][1]["environment"]["VPN_PORT_FORWARDING_PROVIDER"] == "protonvpn"
