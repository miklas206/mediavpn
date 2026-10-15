import json
import struct
import time
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_app import login
from test_configuration import CONFIG

from mediavpn.app import create_app
from mediavpn.gateway_image.config import routes, traefik_config
from mediavpn.gateway_image.natpmp import lease, mapping
from mediavpn.gateway_image.observer import fresh
from mediavpn.runtime import Runtime
from mediavpn.seedbox import PREPARE_CODE, Seedbox
from mediavpn.shared_gateway import save
from mediavpn.web_gateway import ROLE, WebGateway, WebSettings, public_routes

PROFILE = "a" * 24
TOKEN = "synthetic-cloudflare-secret-token"
ROUTE = {
    "id": "app-1",
    "hostname": "app.example.com",
    "upstream": "http://192.168.1.50:8080",
}


def settings(**values):
    return WebSettings(
        enabled=True, profileId=PROFILE, routes=[ROUTE], cloudflareToken=TOKEN, **values
    )


class Container:
    def __init__(self, owner, ident, kwargs):
        self.owner, self.id = owner, ident
        self.labels = kwargs.get("labels", {})
        self.attrs = {
            "State": {"Running": True, "StartedAt": "2026-10-14T10:00:00Z"},
            "Image": kwargs.get("image", "sha256:image"),
            "HostConfig": {"NetworkMode": kwargs.get("network_mode")},
        }

    def reload(self):
        pass

    def stop(self, **kwargs):
        self.attrs["State"]["Running"] = False

    def start(self):
        self.attrs["State"]["Running"] = True

    def remove(self, **kwargs):
        assert not kwargs.get("v")
        self.owner.rows.remove(self)


class Docker:
    def __init__(self):
        self.rows, self.created, self.builds = [], [], []
        self.containers = self
        self.images = SimpleNamespace(get=self.missing, build=self.build)

    def missing(self, *_):
        raise ValueError("Not built")

    def build(self, **kwargs):
        self.builds.append(kwargs)
        return SimpleNamespace(id="sha256:gateway"), []

    def list(self, all=False, filters=None):
        labels = (filters or {}).get("label", [])
        if isinstance(labels, str):
            labels = [labels]
        return [row for row in self.rows if all_filter(row, labels)]

    def run(self, image, **kwargs):
        self.created.append({"image": image, **kwargs})
        row = Container(self, kwargs["name"], {"image": image, **kwargs})
        self.rows.append(row)
        return row


def all_filter(row, labels):
    return all(
        row.labels.get(item.partition("=")[0]) == item.partition("=")[2]
        for item in labels
    )


@pytest.fixture
def controlled(tmp_path):
    docker = Docker()
    vpn = Container(docker, "provider", {})
    observation = {"healthy": True, "publicIp": "8.8.8.8", "forwardedPort": 45000}
    runtime = SimpleNamespace(
        enabled=True,
        docker=lambda: docker,
        host_data=Path("/approved"),
        status=lambda _: observation.copy(),
        container=lambda _: vpn,
    )
    gateway = WebGateway(runtime, tmp_path)
    gateway.configure(settings())
    gateway.torrent_enabled = lambda: True
    return gateway, runtime, docker, vpn, observation


@pytest.mark.parametrize(
    "upstream",
    [
        "https://example.com",
        "http://8.8.8.8",
        "http://127.0.0.1:80",
        "http://169.254.169.254",
        "http://192.168.1.50/path",
        "https://user:secret@192.168.1.50",
        "http://[::1]",
    ],
)
def test_routes_reject_unapproved_origins(upstream):
    with pytest.raises(ValueError):
        routes([{**ROUTE, "upstream": upstream}])


def test_traefik_uses_dns01_exact_hosts_strict_tls_cloudflare_peer_and_route_marker():
    static, dynamic = traefik_config(
        [{**ROUTE, "upstream": "https://192.168.1.50:8443"}], 45000
    )
    assert TOKEN not in json.dumps((static, dynamic))
    assert static["entryPoints"]["gateway"]["address"] == ":45000"
    assert set(static["providers"]) == {"file"}
    assert (
        static["certificatesResolvers"]["gateway"]["acme"]["dnsChallenge"]["provider"]
        == "cloudflare"
    )
    assert (
        static["certificatesResolvers"]["gateway"]["acme"]["storage"]
        == "/state/acme.json"
    )
    assert "api" not in static
    http = dynamic["http"]
    assert len(http["routers"]) == 1
    name, router = next(iter(http["routers"].items()))
    assert router["rule"] == "Host(`app.example.com`)"
    assert router["middlewares"][0] == "cloudflare-only"
    assert "ipStrategy" not in http["middlewares"]["cloudflare-only"]["ipAllowList"]
    headers = http["middlewares"][router["middlewares"][1]]["headers"]
    assert headers["customResponseHeaders"]["X-MediaVPN-Route"] == "app-1"
    assert headers["customRequestHeaders"]["X-Forwarded-Host"] == "app.example.com"
    assert http["services"][name]["loadBalancer"]["passHostHeader"] is True
    assert http["serversTransports"][name] == {
        "serverName": "192.168.1.50",
        "insecureSkipVerify": False,
    }
    assert dynamic["tls"]["options"][router["tls"]["options"]]["sniStrict"] is True


def test_each_domain_has_its_own_tls_option_identity_to_block_sni_host_mismatch():
    _, dynamic = traefik_config(
        [ROUTE, {**ROUTE, "id": "app-2", "hostname": "other.example.com"}], 45000
    )
    routers = list(dynamic["http"]["routers"].values())
    assert routers[0]["tls"]["options"] != routers[1]["tls"]["options"]
    assert dynamic["tls"]["options"]["default"]["sniStrict"] is True


def test_private_ca_validation_and_danish_hostname(tmp_path):
    from datetime import datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "Test CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    checked = routes(
        [
            {
                **ROUTE,
                "hostname": "æøå.example.com",
                "upstream": "https://192.168.1.50",
                "caPem": pem,
            }
        ]
    )
    value = traefik_config(checked, 45000)
    assert checked[0]["hostname"].startswith("xn--")
    assert '"rootCAs": ["/config/ca-' in json.dumps(value)
    assert "BEGIN CERTIFICATE" not in json.dumps(value)
    with pytest.raises(ValueError):
        routes([{**ROUTE, "caPem": "-----BEGIN PRIVATE KEY-----"}])


@pytest.mark.parametrize(
    "problem",
    [
        None,
        "wrong-san",
        "wrong-metadata",
        "expired",
        "not-yet-valid",
        "renewal-overdue",
    ],
)
def test_traefik_acme_certificate_checks_metadata_san_and_validity_without_exposing_keys(
    tmp_path, problem
):
    import base64
    from datetime import datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from mediavpn.gateway_image.observer import acme_certificate

    now = datetime.now(UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [x509.NameAttribute(x509.NameOID.COMMON_NAME, "app.example.com")]
    )
    before = (
        now + timedelta(hours=1)
        if problem == "not-yet-valid"
        else now - timedelta(days=2)
    )
    after = (
        now - timedelta(minutes=1)
        if problem == "expired"
        else now + timedelta(minutes=2)
        if problem == "renewal-overdue"
        else now + timedelta(days=1)
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(42)
        .not_valid_before(before)
        .not_valid_after(after)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(
                        "other.example.com"
                        if problem == "wrong-san"
                        else "app.example.com"
                    )
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    storage = tmp_path / "acme.json"
    storage.write_text(
        json.dumps(
            {
                "gateway": {
                    "Account": {"PrivateKey": "private-account-secret"},
                    "Certificates": [
                        {
                            "domain": {
                                "main": "other.example.com"
                                if problem == "wrong-metadata"
                                else "app.example.com"
                            },
                            "certificate": base64.b64encode(
                                cert.public_bytes(serialization.Encoding.PEM)
                            ).decode(),
                            "key": "private-certificate-secret",
                        }
                    ],
                }
            }
        )
    )
    public = acme_certificate("app.example.com", storage, now)
    if problem:
        assert public is None
    else:
        assert public == cert.public_bytes(serialization.Encoding.DER)
        assert (
            b"private-certificate-secret" not in public
            and b"private-account-secret" not in public
        )


def test_certificate_ready_requires_trusted_served_certificate_to_match_acme_store(
    monkeypatch,
):
    from mediavpn.gateway_image import observer

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def getpeercert(self, binary_form=False):
            assert binary_form
            return b"served-leaf"

    connection = Connection()
    monkeypatch.setattr(observer, "acme_certificate", lambda _: b"served-leaf")
    monkeypatch.setattr(
        observer.socket, "create_connection", lambda *_, **__: connection
    )
    seen = []

    def wrap(conn, server_hostname):
        seen.append(server_hostname)
        return connection

    monkeypatch.setattr(
        observer.ssl,
        "create_default_context",
        lambda: SimpleNamespace(wrap_socket=wrap),
    )
    assert observer.certificate_ready("app.example.com", 45000)
    assert seen == ["app.example.com"]
    monkeypatch.setattr(observer, "acme_certificate", lambda _: b"different-new-leaf")
    assert not observer.certificate_ready("app.example.com", 45000)


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"gateway": []},
        {"gateway": {"Certificates": {}}},
        {"gateway": {"Certificates": [None]}},
    ],
)
def test_malformed_acme_store_is_a_safe_pending_certificate_failure(tmp_path, value):
    from mediavpn.gateway_image.observer import acme_certificate

    path = tmp_path / "acme.json"
    path.write_text(json.dumps(value))
    with pytest.raises(TypeError, match="Invalid certificate"):
        acme_certificate("app.example.com", path)


def test_observer_starts_native_traefik_with_file_credentials_and_private_acme_store(
    tmp_path, monkeypatch
):
    from mediavpn.gateway_image import observer

    for folder in ("config", "state"):
        (tmp_path / folder).mkdir()
    save(
        tmp_path / "config/settings.json",
        {
            "healthy": True,
            "checkedAt": time.time(),
            "publicPort": 45000,
            "publicIp": "8.8.8.8",
            "generation": "synthetic-generation",
            "revision": "test",
            "routes": [ROUTE],
        },
    )

    class Stop:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _):
            self.stopped = True

        def set(self):
            self.stopped = True

    calls = []
    process = SimpleNamespace(
        poll=lambda: None, terminate=lambda: None, wait=lambda **_: 0
    )
    monkeypatch.setattr(observer, "Path", lambda value: tmp_path / value.lstrip("/"))
    monkeypatch.setattr(observer.threading, "Event", Stop)
    monkeypatch.setattr(observer.signal, "signal", lambda *_: None)
    monkeypatch.setattr(
        observer.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append((args, kwargs)) or process,
    )
    monkeypatch.setattr(observer, "certificate_ready", lambda *_: False)

    # Avoid changing threading.Event underneath ThreadPoolExecutor internals.
    class Pool:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def map(self, function, values):
            return map(function, values)

    monkeypatch.setattr(observer, "ThreadPoolExecutor", Pool)
    observer.main()
    assert calls[0][0] == ["traefik", "--configFile=/state/traefik.yaml"]
    assert calls[0][1]["env"]["CF_DNS_API_TOKEN_FILE"] == "/config/cloudflare-token"
    assert calls[0][1]["env"]["CF_ZONE_API_TOKEN_FILE"] == "/config/cloudflare-token"
    assert "CF_DNS_API_TOKEN" not in calls[0][1]["env"]
    assert (tmp_path / "state/acme.json").read_text() == "{}"
    assert (
        json.loads((tmp_path / "state/status.json").read_text())["phase"] == "stopped"
    )


def test_helpers_share_one_namespace_and_lease_helper_has_no_token(controlled):
    gateway, _, docker, _, _ = controlled
    gateway.torrent_enabled = lambda: True
    gateway.reconcile()
    assert len(docker.builds) == 1 and len(docker.created) == 2
    http, nat = docker.created
    assert http["network_mode"] == nat["network_mode"] == "container:provider"
    assert http["cap_drop"] == ["ALL"] and http["cap_add"] == []
    assert nat["cap_add"] == ["NET_ADMIN"]
    assert "http-config" not in json.dumps(nat)
    assert TOKEN not in json.dumps(docker.created)
    assert not any(
        "wireguard" in source for spec in docker.created for source in spec["volumes"]
    )
    gateway.reconcile()
    assert len(docker.created) == 2
    assert TOKEN not in json.dumps(gateway.public())


def test_configuration_and_shutdown_do_not_wait_for_native_image_build(controlled):
    import threading

    gateway, _, docker, _, _ = controlled
    entered, release = threading.Event(), threading.Event()

    def build():
        entered.set()
        assert release.wait(5)
        return "sha256:gateway"

    gateway.ensure_image = build
    worker = threading.Thread(target=gateway.reconcile)
    worker.start()
    assert entered.wait(2)
    try:
        gateway.configure(WebSettings(enabled=False, profileId=PROFILE))
        gateway.close()
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert not docker.created


def test_without_installed_selected_torrent_app_no_lease_helper_is_started(controlled):
    gateway, _, docker, _, _ = controlled
    gateway.torrent_enabled = lambda: False
    gateway.reconcile()
    assert len(docker.created) == 1
    assert docker.created[0]["labels"][ROLE] == "web"


def test_wrong_shared_profile_cannot_expose_another_provider(controlled):
    gateway, runtime, docker, _, _ = controlled
    runtime.shared_profile = lambda: "b" * 24
    gateway.reconcile()
    assert not docker.created
    assert not gateway.public()["providerReady"]


def test_unavailable_provider_still_exposes_sanitized_prerequisite_diagnostics(
    controlled,
):
    gateway, runtime, _, _, _ = controlled

    def unavailable(_):
        raise HTTPException(409, "Synthetic private Docker details")

    runtime.status = unavailable
    value = gateway.public()
    assert value["publicIp"] is None and not value["ready"]
    assert value["prerequisites"]
    assert "Synthetic private" not in json.dumps(value)


def test_reconnect_replaces_helpers_and_stale_ready_is_rejected(controlled):
    gateway, _, docker, vpn, observed = controlled
    gateway.reconcile()
    state = gateway.settings()
    report = {
        "ready": True,
        "generation": gateway.generation(vpn),
        "revision": state["revision"],
        "publicIp": observed["publicIp"],
        "publicPort": observed["forwardedPort"],
        "checkedAt": time.time(),
        "routes": [{"id": "app-1", "certificateReady": True}],
    }
    save(gateway.directory / "http-state/status.json", report)
    assert gateway.public()["ready"]
    vpn.attrs["State"]["StartedAt"] = "new-provider-start"
    assert not gateway.public()["ready"]
    gateway.reconcile()
    assert len(docker.created) == 4 and len(docker.rows) == 2
    observed["healthy"] = False
    state = gateway.public()
    assert (
        not state["providerReady"]
        and state["publicIp"] is None
        and state["publicPort"] is None
    )
    gateway.reconcile()
    assert not docker.rows


def test_web_guard_and_upstream_routes_do_not_add_provider_sessions(tmp_path):
    docker = Docker()
    runtime = Runtime(tmp_path, True, "/approved", docker)
    runtime.shared_profile = lambda: PROFILE
    runtime.shared_endpoint = lambda: ("192.168.1.50", 18804)
    runtime.web_targets = lambda: ["192.168.1.51/32", "192.168.1.52/32"]
    runtime.start(PROFILE, port_forwarding=True)
    assert (
        docker.created[0]["environment"]["FIREWALL_OUTBOUND_SUBNETS"]
        == "192.168.1.51/32,192.168.1.52/32"
    )
    runtime.start("b" * 24)
    assert len(docker.created) == 2
    assert "ports" not in docker.created[1]
    assert docker.created[1]["environment"]["FIREWALL_OUTBOUND_SUBNETS"] == ""
    docker.run(
        "sha256:gateway",
        name="helper",
        labels={
            "org.mediahub.mediavpn.owner": "mediavpn",
            "org.mediahub.mediavpn.profile": PROFILE,
            ROLE: "web",
        },
    )
    assert runtime.container(PROFILE).id.startswith("mediavpn-")


def test_natpmp_uses_one_explicit_private_port_and_matching_tcp_udp():
    calls = []

    def exchange(opcode, private, public, lifetime, forbidden):
        calls.append((opcode, private, public, lifetime))
        return {"publicPort": 55000, "internalPort": private, "lifetime": 60}

    value = lease(39000, 0, {45000}, exchange)
    assert value == {"internalPort": 39000, "publicPort": 55000, "lifetime": 60}
    assert calls == [(2, 39000, 0, 60), (1, 39000, 55000, 60)]


@pytest.mark.parametrize(
    "reply,inside,outside,lifetime",
    [
        (129, 39000, 55000, 60),
        (130, 1, 55000, 60),
        (130, 39000, 45000, 60),
        (130, 39000, 55000, 0),
    ],
)
def test_natpmp_rejects_other_lease_and_web_port(reply, inside, outside, lifetime):
    packet = struct.pack("!BBHIHHI", 0, reply, 0, 10, inside, outside, lifetime)
    with pytest.raises(ValueError):
        mapping(packet, 2, 39000, {45000})


def test_failed_udp_mapping_releases_only_own_explicit_port():
    calls = []

    def exchange(opcode, private, public, lifetime, forbidden):
        calls.append((opcode, private, public, lifetime))
        return {"publicPort": 55000 if opcode == 2 else 55001, "lifetime": 60}

    with pytest.raises(ValueError):
        lease(39000, 0, {45000}, exchange)
    assert calls[-2:] == [(1, 39000, 55000, 0), (2, 39000, 55000, 0)]
    assert all(row[1] == 39000 for row in calls)


def test_torrent_gate_rejects_missing_expired_or_reserved_mapping(controlled):
    gateway, runtime, _, vpn, observed = controlled
    gateway.reconcile()
    controller = Seedbox(runtime, gateway.directory.parent)
    controller.web_gateway = gateway
    save(controller.path, {"enabled": True, "profileId": PROFILE})
    assert controller.observed(PROFILE)["forwardedPort"] is None
    report = {
        "ready": True,
        "generation": gateway.generation(vpn),
        "revision": gateway.torrent_revision(gateway.settings()),
        "publicIp": "8.8.8.8",
        "webPort": 45000,
        "internalPort": 39000,
        "publicPort": 55000,
        "expiresAt": time.time() + 60,
        "checkedAt": time.time(),
    }
    save(gateway.directory / "nat-state/status.json", report)
    assert controller.observed(PROFILE)["forwardedPort"] == 39000
    assert controller.observed(PROFILE)["publicTorrentPort"] == 55000
    assert controller.generation(vpn).endswith(":web:39000:55000")
    report["publicPort"] = observed["forwardedPort"]
    save(gateway.directory / "nat-state/status.json", report)
    assert controller.observed(PROFILE)["forwardedPort"] is None
    stopped = []
    child = SimpleNamespace(
        attrs={"State": {"Running": True}},
        reload=lambda: None,
        stop=lambda **_: stopped.append(True),
    )
    controller.child = lambda: child
    controller.create = lambda *_: pytest.fail(
        "A missing lease must never start a torrent child"
    )
    controller.reconcile()
    assert stopped == [True]
    report.update(publicPort=55000, expiresAt=time.time() - 1)
    save(gateway.directory / "nat-state/status.json", report)
    assert controller.observed(PROFILE)["forwardedPort"] is None


def test_prepare_writes_distinct_listen_and_announce_ports(tmp_path, monkeypatch):
    path = tmp_path / "qBittorrent.conf"
    path.write_text("[Preferences]\nWebUI\\Username=mediaseed\n")
    code = PREPARE_CODE.replace(
        "/config/qBittorrent/qBittorrent.conf", str(path).replace("\\", "/")
    )
    monkeypatch.setattr("sys.argv", ["prepare", "39000", "55000"])
    exec(compile(code, "prepare", "exec"), {})  # noqa: S102 - execute our reviewed helper on a temporary config
    assert "Session\\Port=39000" in path.read_text()
    assert "Session\\AnnouncePort=55000" in path.read_text()


def test_provider_heartbeat_expires_closed():
    state = {"healthy": True, "publicPort": 45000, "checkedAt": 100}
    assert fresh(state, now=101)
    assert not fresh(state, now=131)
    assert not fresh({**state, "healthy": False}, now=101)


@pytest.mark.parametrize(
    "architecture,ready",
    [
        ("amd64", True),
        ("x86_64", True),
        ("arm64", True),
        ("aarch64", True),
        ("sparc", False),
    ],
)
def test_prerequisites_validate_native_arch_tun_and_storage_without_starting_vpn(
    controlled, architecture, ready, monkeypatch
):
    gateway, _, docker, _, _ = controlled
    calls = []
    docker.info = lambda: (
        calls.append("info") or {"OSType": "linux", "Architecture": architecture}
    )
    monkeypatch.setattr(gateway, "tun_check", lambda: True)
    result = gateway.prerequisites(force=True)
    assert result["ready"] is ready
    assert result["architecture"] == architecture
    assert {row["id"] for row in result["checks"]} == {
        "runtime",
        "docker",
        "architecture",
        "tun",
        "storage",
    }
    assert gateway.prerequisites() == result
    assert calls == ["info"]
    assert not docker.created and not docker.builds


def test_missing_tun_and_unwritable_private_storage_report_actionable_blockers(
    controlled, monkeypatch
):
    gateway, _, docker, _, _ = controlled
    docker.info = lambda: {"OSType": "linux", "Architecture": "aarch64"}
    monkeypatch.setattr(gateway, "tun_check", lambda: False)

    def denied(**_):
        raise OSError("Synthetic permission failure")

    monkeypatch.setattr("mediavpn.web_gateway.tempfile.TemporaryFile", denied)
    value = gateway.prerequisites(force=True)
    blocked = {
        row["id"]: row["message"]
        for row in value["checks"]
        if row["state"] == "blocked"
    }
    assert set(blocked) == {"tun", "storage"}
    assert "Proxmox" in blocked["tun"] and "/dev/net/tun" in blocked["tun"]
    assert "private data mount" in blocked["storage"]
    assert not docker.created


def test_tun_probe_opens_character_device_without_creating_interface(monkeypatch):
    import stat

    from mediavpn import web_gateway

    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(
            web_gateway.os, "stat", lambda _: SimpleNamespace(st_mode=stat.S_IFCHR)
        )
        patch.setattr(
            web_gateway.os,
            "open",
            lambda path, flags: calls.append((path, flags)) or 123,
        )
        patch.setattr(
            web_gateway.os, "close", lambda descriptor: calls.append(descriptor)
        )
        assert WebGateway.tun_check()
    assert calls[0][0] == "/dev/net/tun" and calls[0][1] & web_gateway.os.O_RDWR
    assert calls[1] == 123


def test_public_trust_fingerprint_is_stable_without_exposing_pem():
    import hashlib

    rows = public_routes(
        [
            {
                **ROUTE,
                "tlsServerName": "private.example.com",
                "caPem": "  synthetic\r\ncertificate\r\n",
            }
        ]
    )
    assert (
        rows[0]["caFingerprint"]
        == hashlib.sha256(b"synthetic\ncertificate").hexdigest()
    )
    assert rows[0]["tlsServerName"] == "private.example.com"
    assert "synthetic" not in json.dumps(rows) and "caPem" not in json.dumps(rows)


def test_machine_api_auth_pending_response_and_profile_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIAVPN_SHARED_ADDRESS", "192.168.1.50")
    runtime = SimpleNamespace(
        enabled=True, docker=lambda: Docker(), status=lambda _: {"healthy": False}
    )
    app = create_app(tmp_path, "http://testserver", runtime=runtime)
    monkeypatch.setattr(
        app.state.web_gateway,
        "prerequisites",
        lambda **_: {"ready": True, "checks": [], "architecture": "x86_64"},
    )
    client = TestClient(app)
    login(client)
    profile = client.post(
        "/api/profiles",
        json={"name": "Synthetic", "configuration": CONFIG, "port_forwarding": True},
    ).json()["id"]
    key = (tmp_path / "integrations/mediahub.token").read_text()
    torrent_key = (tmp_path / "integrations/mediaseed.token").read_text()
    body = {
        "enabled": True,
        "profileId": profile,
        "routes": [ROUTE],
        "cloudflareToken": TOKEN,
    }
    assert client.put("/api/integration/web-gateway", json=body).status_code == 401
    assert (
        client.put(
            "/api/integration/web-gateway",
            json=body,
            headers={"X-MediaHub-Key": torrent_key},
        ).status_code
        == 401
    )
    response = client.put(
        "/api/integration/web-gateway", json=body, headers={"X-MediaHub-Key": key}
    )
    assert response.status_code == 200, response.text
    assert response.json()["phase"] == "applying" and TOKEN not in response.text
    assert response.json()["routes"] == public_routes(routes([ROUTE]))
    assert app.state.shared_gateway.profile() == profile
    assert client.put("/api/shared", json={"enabled": False}).status_code == 409
    response = client.get(
        "/api/integration/web-gateway", headers={"X-MediaHub-Key": key}
    )
    assert (
        response.status_code == 200
        and TOKEN not in response.text
        and "caPem" not in response.text
    )
    assert not response.json()["providerReady"]


def test_preview_reports_runtime_blocker_to_hub_without_allowing_activation(tmp_path):
    client = TestClient(create_app(tmp_path, "http://testserver"))
    key = (tmp_path / "integrations/mediahub.token").read_text()
    headers = {"X-MediaHub-Key": key}
    value = client.get("/api/integration/web-gateway", headers=headers).json()
    assert (
        next(row for row in value["prerequisites"] if row["id"] == "runtime")["state"]
        == "blocked"
    )
    response = client.put(
        "/api/integration/web-gateway",
        headers=headers,
        json={
            "enabled": True,
            "profileId": PROFILE,
            "routes": [ROUTE],
            "cloudflareToken": TOKEN,
        },
    )
    assert response.status_code == 409
    assert "MediaHub" in response.json()["detail"]
    assert not (tmp_path / "web-gateway/settings.json").exists()


def test_browser_proxy_requires_session_csrf_and_rejects_raw_origins(tmp_path):
    app = create_app(tmp_path, "http://testserver")
    client = TestClient(app)
    assert client.get("/api/hub-web-gateway").status_code == 401
    login(client)
    app.state.hub_login.web_gateway = AsyncMock(return_value={"enabled": False})
    response = client.put(
        "/api/hub-web-gateway/config",
        json={
            "enabled": True,
            "profileId": PROFILE,
            "routes": [
                {
                    "packageId": "org.mediahub.mediaseed",
                    "zoneId": "b" * 32,
                    "label": "seed",
                    "upstream": "http://8.8.8.8",
                }
            ],
        },
    )
    assert response.status_code == 422
    app.state.hub_login.web_gateway.assert_not_awaited()
    response = client.put(
        "/api/hub-web-gateway/credentials", json={"accountId": "b" * 32, "token": TOKEN}
    )
    assert response.status_code == 200
    assert app.state.hub_login.web_gateway.await_args.args[-1]["token"] == TOKEN
    client.headers.pop("X-MediaVPN-CSRF")
    assert client.post("/api/hub-web-gateway/reconcile").status_code == 403


def test_plex_and_route_changes_do_not_invalidate_torrent_lease(controlled):
    gateway, runtime, _, vpn, observed = controlled
    gateway.reconcile()
    state = gateway.settings()
    before = gateway.torrent_revision(state)
    report = {"ready": True, "generation": gateway.generation(vpn), "revision": before,
              "publicIp": observed["publicIp"], "webPort": observed["forwardedPort"],
              "internalPort": state["internalPort"], "publicPort": 55000,
              "expiresAt": time.time() + 60, "checkedAt": time.time()}
    save(gateway.directory / "nat-state/status.json", report)
    assert gateway.torrent(PROFILE, observed)["ready"]
    changed = settings().model_dump() | {"plex": {"address": "192.168.1.50", "port": 32400},
        "routes": [ROUTE | {"hostname": "changed.example.com"}]}
    gateway.configure(WebSettings.model_validate(changed))
    assert gateway.settings()["revision"] != state["revision"]
    assert gateway.torrent_revision(gateway.settings()) == before
    assert gateway.torrent(PROFILE, observed)["ready"]
    for key, value in [("profileId", "b" * 24), ("internalPort", 35001), ("enabled", False)]:
        assert gateway.torrent_revision(state | {key: value}) != before
    assert not gateway.torrent(PROFILE, observed | {"healthy": False})["ready"]
