"""Private machine-controlled HTTPS gateway, sharing one provider connection."""

# ruff: noqa: BLE001, S110
# Docker failures can contain credential-bearing create payloads. Fail closed
# with fixed public messages; never log their raw exception details.
import hashlib
import ipaddress
import json
import os
import secrets
import stat
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .gateway_image.config import ca_name
from .gateway_image.config import routes as validate_routes
from .shared_gateway import save

ROLE = "org.mediahub.mediavpn.helper"


def public_routes(values, certificates=None):
    certificates = certificates or {}
    result = []
    for row in values:
        ca = (row.get("caPem") or "").replace("\r\n", "\n").strip()
        result.append(
            {
                "id": row["id"],
                "hostname": row["hostname"],
                "upstream": row["upstream"],
                "tlsServerName": row.get("tlsServerName"),
                "caFingerprint": hashlib.sha256(ca.encode()).hexdigest()
                if ca
                else None,
                "certificateReady": certificates.get(row["id"], False),
            }
        )
    return result


class WebRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    hostname: str = Field(min_length=3, max_length=253)
    upstream: str = Field(min_length=8, max_length=500)
    tlsServerName: str | None = Field(default=None, max_length=253)
    caPem: str | None = Field(default=None, max_length=16384)


class PlexTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    address: str
    port: int = Field(default=32400, ge=32400, le=32400)

    @field_validator("address")
    @classmethod
    def private_address(cls, value):
        address = ipaddress.IPv4Address(value)
        if not any(address in ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
            raise ValueError("Plex needs an approved private IPv4 address")
        return str(address)


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    enabled: bool
    profileId: str | None = Field(default=None, pattern=r"^[a-f0-9]{24}$")
    routes: list[WebRoute] = Field(default_factory=list, max_length=32)
    plex: PlexTarget | None = None
    cloudflareToken: SecretStr | None = Field(
        default=None, min_length=20, max_length=512
    )


class WebGateway:
    def __init__(self, runtime, data):
        self.runtime = runtime
        self.directory = Path(data) / "web-gateway"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        if self.directory.is_symlink():
            raise ValueError("Gateway state must not be a symbolic link")
        self.directory.chmod(0o700)
        for name in ("http-config", "http-state", "nat-config", "nat-state", "plex-config", "plex-state"):
            path = self.directory / name
            path.mkdir(mode=0o700, exist_ok=True)
            if path.is_symlink():
                raise ValueError("Gateway state must not be a symbolic link")
            path.chmod(0o700)
        self.path = self.directory / "settings.json"
        self.lock = threading.RLock()
        self.error = None
        self.image_id = None
        self.closed = False
        self.preflight_cache = None
        self.preflight_at = 0
        self.torrent_enabled = lambda: False
        runtime.web_targets = self.targets
        runtime.stop_web = self.stop

    def settings(self):
        return (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {
                "enabled": False,
                "profileId": None,
                "routes": [],
                "revision": "",
            }
        )

    def targets(self):
        state = self.settings()
        return (
            sorted(
                {urlsplit(row["upstream"]).hostname + "/32" for row in state["routes"]}
                | ({state["plex"]["address"] + "/32"} if state.get("plex") else set())
            )
            if state["enabled"]
            else []
        )

    def validate(self, body):
        if body.enabled:
            if not body.profileId:
                raise HTTPException(422, "Choose a gateway VPN profile")
            try:
                normalized = validate_routes([row.model_dump() for row in body.routes])
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise HTTPException(
                    422, "Choose valid unique domains and private installed app origins"
                ) from None
            if (
                not body.cloudflareToken
                and not (self.directory / "http-config/cloudflare-token").exists()
            ):
                raise HTTPException(
                    409, "Connect Cloudflare before enabling the web gateway"
                )
            if body.cloudflareToken and any(
                char.isspace() for char in body.cloudflareToken.get_secret_value()
            ):
                raise HTTPException(
                    422, "Enter a valid Cloudflare API token without whitespace"
                )
            return normalized
        return []

    def configure(self, body):
        with self.lock:
            normalized = self.validate(body)
            previous = self.settings()
            if body.cloudflareToken:
                path = self.directory / "http-config/cloudflare-token"
                fd = os.open(
                    path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_TRUNC
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(body.cloudflareToken.get_secret_value())
                path.chmod(0o600)
            for row in normalized:
                if row["caPem"]:
                    path = self.directory / "http-config" / ca_name(row["caPem"])
                    path.write_text(row["caPem"], encoding="utf-8")
                    path.chmod(0o600)
            settings = {
                "enabled": body.enabled,
                "profileId": body.profileId,
                "routes": normalized,
                "plex": body.plex.model_dump() if body.plex and body.enabled else None,
                "plexInternalPort": previous.get("plexInternalPort", 55000 + secrets.randbelow(9000)),
                "internalPort": previous.get(
                    "internalPort", 35000 + secrets.randbelow(20000)
                ),
            }
            signature = hashlib.sha256(
                json.dumps(settings, sort_keys=True).encode()
                + (self.directory / "http-config/cloudflare-token").read_bytes()
                if (self.directory / "http-config/cloudflare-token").exists()
                else json.dumps(settings).encode()
            ).hexdigest()
            save(self.path, {**settings, "revision": signature})
            self.error = None

    def helper(self, role):
        rows = self.runtime.docker().containers.list(
            all=True,
            filters={
                "label": [
                    "org.mediahub.mediavpn.owner=mediavpn",
                    ROLE + "=" + role,
                ]
            },
        )
        if len(rows) > 1:
            raise ValueError("Multiple gateway helpers have the same owner")
        return rows[0] if rows else None

    @staticmethod
    def torrent_revision(state):
        # A web route or Plex target edit does not change the torrent mapping.
        return hashlib.sha256(json.dumps({key: state.get(key) for key in
            ("enabled", "profileId", "internalPort")}, sort_keys=True).encode()).hexdigest()

    def stop(self):
        if not self.runtime.enabled:
            return
        with self.lock:
            for role in ("web", "torrent-lease", "plex-lease"):
                row = self.helper(role)
                if row:
                    row.stop(timeout=8)
                    row.remove(v=False)
            for folder in ("http-state", "nat-state", "plex-state"):
                save(
                    self.directory / folder / "status.json",
                    {"ready": False, "checkedAt": time.time()},
                )

    def ensure_image(self):
        if self.image_id:
            return self.image_id
        source = Path(__file__).parent / "gateway_image"
        fingerprint = hashlib.sha256(
            b"".join(
                (source / name).read_bytes().replace(b"\r\n", b"\n")
                for name in ("Dockerfile", "config.py", "observer.py", "natpmp.py", "plex_port.py")
            )
        ).hexdigest()
        client = self.runtime.docker()
        tag = "mediahub-web-gateway:" + fingerprint[:16]
        try:
            image = client.images.get(tag)
            if image.labels.get("org.mediahub.gateway.source") != fingerprint:
                raise ValueError("Gateway image ownership changed")
        except Exception:
            image, _ = client.images.build(
                path=str(source),
                tag=tag,
                rm=True,
                timeout=900,
                labels={"org.mediahub.gateway.source": fingerprint, "org.mediahub.managed-component": "vpn-gateway"},
            )
        self.image_id = image.id
        return image.id

    @staticmethod
    def generation(vpn):
        return vpn.id + ":" + vpn.attrs["State"]["StartedAt"]

    def selected(self, state):
        return (
            getattr(self.runtime, "shared_profile", lambda: state["profileId"])()
            == state["profileId"]
        )

    def reconcile(self):
        if not self.runtime.enabled:
            return
        # Pulling the native proxy image can take time. Keep configuration and
        # shutdown independent from image preparation; re-read desired state after it finishes.
        state = self.settings()
        image = None
        if state["enabled"] and not self.closed and self.selected(state):
            try:
                observed = self.runtime.status(state["profileId"])
                if observed.get("healthy") and observed.get("forwardedPort"):
                    image = self.ensure_image()
            except Exception:
                self.error = "Gateway image could not be built; check Docker build and download access"
                self.stop()
                return
        with self.lock:
            state = self.settings()
            try:
                if not state["enabled"] or self.closed or not self.selected(state):
                    self.stop()
                    return
                observed = self.runtime.status(state["profileId"])
                if not observed.get("healthy") or not observed.get("forwardedPort"):
                    self.stop()
                    self.error = (
                        "Waiting for a verified Proton connection and forwarded port"
                    )
                    return
                vpn = self.runtime.container(state["profileId"])
                vpn.reload()
                generation = self.generation(vpn)
                if not image:
                    return  # Provider recovered after the initial observation; next tick builds.
                port = observed["forwardedPort"]
                internal = state["internalPort"]
                while internal in {port, 51820, 51879}:
                    internal = 35000 + ((internal - 34999) % 20000)
                common = {
                    "healthy": True,
                    "checkedAt": time.time(),
                    "generation": generation,
                    "revision": state["revision"],
                    "publicIp": observed["publicIp"],
                    "publicPort": port,
                }
                save(
                    self.directory / "http-config/settings.json",
                    {**common, "routes": state["routes"]},
                )
                save(
                    self.directory / "nat-config/settings.json",
                    {
                        **common,
                        "revision": self.torrent_revision(state),
                        "internalPort": internal,
                        "torrentEnabled": bool(self.torrent_enabled()),
                    },
                )
                plex_internal = state.get("plexInternalPort", 60000)
                reserved = {port, internal, 32400, 51820, 51879}
                while plex_internal in reserved:
                    plex_internal = 55000 + ((plex_internal - 54999) % 9000)
                save(self.directory / "plex-config/settings.json", {
                    **common, "plex": state.get("plex"), "plexInternalPort": plex_internal,
                    "reservedInternalPorts": sorted(reserved),
                })
                for role, prefix, script in (
                    ("web", "http", "observer.py"),
                    ("torrent-lease", "nat", "natpmp.py"),
                    ("plex-lease", "plex", "plex_port.py"),
                ):
                    row = self.helper(role)
                    if (role == "torrent-lease" and not self.torrent_enabled()) or (role == "plex-lease" and not state.get("plex")):
                        if row:
                            row.stop(timeout=8)
                            row.remove(v=False)
                        save(
                            self.directory / (prefix + "-state/status.json"),
                            {"ready": False, "checkedAt": time.time()},
                        )
                        continue
                    if row:
                        row.reload()
                        if (
                            row.labels.get("org.mediahub.mediavpn.generation")
                            != generation
                            or row.attrs.get("Image") != image
                        ):
                            row.stop(timeout=8)
                            row.remove(v=False)
                            row = None
                    if not row:
                        save(
                            self.directory / (prefix + "-state/status.json"),
                            {"ready": False, "checkedAt": time.time()},
                        )
                        host = self.runtime.host_data / "web-gateway"
                        row = self.runtime.docker().containers.run(
                            image,
                            name="mediavpn-" + role,
                            command=["python3", "/opt/gateway/" + script],
                            detach=True,
                            network_mode="container:" + vpn.id,
                            labels={
                                "org.mediahub.mediavpn.owner": "mediavpn",
                                ROLE: role,
                                "org.mediahub.mediavpn.profile": state["profileId"],
                                "org.mediahub.mediavpn.generation": generation,
                            },
                            volumes={
                                **({str(host / "nat-state"): {"bind": "/torrent", "mode": "ro"}} if role == "plex-lease" else {}),
                                **({str(host / "plex-state"): {"bind": "/plex", "mode": "ro"}} if role == "torrent-lease" else {}),
                                str(host / (prefix + "-config")): {
                                    "bind": "/config",
                                    "mode": "ro",
                                },
                                str(host / (prefix + "-state")): {
                                    "bind": "/state",
                                    "mode": "rw",
                                },
                            },
                            cap_drop=["ALL"],
                            cap_add=["NET_ADMIN"] if role in {"torrent-lease", "plex-lease"} else [],
                            read_only=True,
                            tmpfs={"/tmp": "rw,noexec,nosuid,nodev,size=16m"},
                            security_opt=["no-new-privileges:true"],
                            restart_policy={"Name": "no"},
                            healthcheck={
                                "Test": [
                                    "CMD",
                                    "python3",
                                    "-c",
                                    "import json,time; s=json.load(open('/state/status.json')); raise SystemExit(0 if (s.get('ready') or s.get('phase') == 'serving') and 0 <= time.time()-s.get('checkedAt',0) < 30 else 1)",
                                ],
                                "Interval": 10_000_000_000,
                                "Timeout": 3_000_000_000,
                                "Retries": 3,
                            },
                            log_config={
                                "Type": "local",
                                "Config": {"max-size": "2m", "max-file": "2"},
                            },
                        )
                    elif not row.attrs["State"].get("Running"):
                        row.start()
                self.error = None
            except Exception:
                self.error = "Gateway runtime could not be prepared; check Docker, TUN and image build access"
                try:
                    self.stop()
                except Exception:
                    pass

    def close(self):
        self.closed = True
        self.stop()

    def report(self, kind, state, observed):
        try:
            value = json.loads(
                (self.directory / (kind + "-state/status.json")).read_text()
            )
            vpn = self.runtime.container(state["profileId"])
            vpn.reload()
            helper = self.helper("web" if kind == "http" else "torrent-lease")
            if not helper:
                return {}
            helper.reload()
            if not helper.attrs.get("State", {}).get("Running") or helper.labels.get(
                "org.mediahub.mediavpn.generation"
            ) != self.generation(vpn):
                return {}
            if (
                not observed.get("healthy")
                or value.get("revision") != (self.torrent_revision(state) if kind == "nat" else state["revision"])
                or value.get("generation") != self.generation(vpn)
                or value.get("publicIp") != observed.get("publicIp")
                or not 0 <= time.time() - value.get("checkedAt", 0) < 15
            ):
                return {}
            port = value.get("webPort") if kind in {"nat", "plex"} else value.get("publicPort")
            if port != observed.get("forwardedPort"):
                return {}
            return value
        except Exception:
            return {}

    def torrent(self, profile, observed):
        state = self.settings()
        if not state["enabled"] or state["profileId"] != profile:
            return None
        if not self.selected(state):
            return {"ready": False}
        value = self.report("nat", state, observed)
        if not value.get("ready") or value.get("expiresAt", 0) <= time.time() + 5:
            return {"ready": False}
        if value.get("publicPort") == observed.get("forwardedPort"):
            return {"ready": False}
        return {
            "ready": True,
            "internalPort": value["internalPort"],
            "publicPort": value["publicPort"],
        }

    def plex_status(self, state, observed):
        value = self.report("plex", state, observed) if state.get("plex") else {}
        torrent = self.report("nat", state, observed)
        valid = bool(value.get("ready") and value.get("expiresAt", 0) > time.time() + 5
                     and value.get("publicPort") not in {observed.get("forwardedPort"), torrent.get("publicPort")}
                     and value.get("targetAddress") == state.get("plex", {}).get("address")
                     and value.get("targetPort") == 32400)
        return {"enabled": bool(state.get("plex")), "ready": valid,
                "publicPort": value.get("publicPort") if valid else None,
                "publicIp": observed.get("publicIp") if valid else None,
                "message": None if valid or not state.get("plex") else "Waiting for a separate verified Plex TCP port"}

    def public(self):
        state = self.settings()
        prerequisites = self.prerequisites()
        status_message = None
        try:
            observed = (
                self.runtime.status(state["profileId"])
                if state["enabled"] and self.runtime.enabled
                else {}
            )
        except Exception:
            observed = {}
            status_message = (
                "VPN status is unavailable; review the runtime prerequisite checks"
            )
        provider = bool(
            self.selected(state)
            and observed.get("healthy")
            and observed.get("forwardedPort")
        )
        report = self.report("http", state, observed) if provider else {}
        ready = bool(report.get("ready"))
        certs = {
            row["id"]: row.get("certificateReady", False)
            for row in report.get("routes", [])
        }
        torrent = self.torrent(state["profileId"], observed) if provider else None
        return {
            "enabled": state["enabled"],
            "profileId": state["profileId"],
            "providerReady": provider,
            "ready": ready,
            "phase": "ready"
            if ready
            else "waiting-certificate"
            if provider
            else "waiting-vpn"
            if state["enabled"]
            else "disabled",
            "message": self.error
            or status_message
            or (
                "Gateway TLS is ready; public domain checks are performed by MediaHub"
                if ready
                else "Waiting for VPN, gateway or HTTPS certificates"
            ),
            "publicIp": observed.get("publicIp") if provider else None,
            "publicPort": observed.get("forwardedPort") if provider else None,
            "routes": public_routes(state["routes"], certs),
            "checkedAt": time.time(),
            "maximumProviderConnections": 1,
            "plex": state.get("plex"),
            "plexPort": self.plex_status(state, observed),
            "torrentReady": bool(torrent and torrent.get("ready")),
            "torrentPort": torrent.get("publicPort") if torrent else None,
            "prerequisites": prerequisites["checks"],
            "architecture": prerequisites.get("architecture"),
        }

    @staticmethod
    def tun_check():
        try:
            if not stat.S_ISCHR(os.stat("/dev/net/tun").st_mode):
                return False
            descriptor = os.open(
                "/dev/net/tun", os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            )
            os.close(descriptor)
            return True
        except OSError:
            return False

    def prerequisites(self, force=False):
        if not force and self.preflight_cache and time.monotonic() < self.preflight_at:
            return self.preflight_cache
        checks = []

        def add(identifier, ready, ok, blocked):
            checks.append(
                {
                    "id": identifier,
                    "state": "ready" if ready else "blocked",
                    "message": ok if ready else blocked,
                }
            )

        add(
            "runtime",
            self.runtime.enabled,
            "Managed Docker runtime is enabled",
            "Install or update MediaVPN through MediaHub to prepare its managed Docker runtime",
        )
        info = {}
        if self.runtime.enabled:
            try:
                info = self.runtime.docker().info()
            except Exception:
                pass
        add(
            "docker",
            bool(info),
            "Docker is reachable",
            "Docker is unavailable; check the MediaHub installation and Docker socket mapping",
        )
        architecture = info.get("Architecture")
        supported = info.get("OSType") == "linux" and architecture in {
            "amd64",
            "x86_64",
            "arm64",
            "aarch64",
        }
        add(
            "architecture",
            supported,
            "Native Linux architecture is supported",
            "This gateway requires Linux Docker on amd64 or arm64",
        )
        tun = self.tun_check() if self.runtime.enabled else False
        add(
            "tun",
            tun,
            "TUN character device is readable and writable",
            "TUN mapping is missing or inaccessible; update the MediaHub Proxmox installation to map /dev/net/tun into the LXC and MediaVPN container",
        )
        writable = True
        try:
            for name in ("http-config", "http-state", "nat-config", "nat-state", "plex-config", "plex-state"):
                path = self.directory / name
                if path.is_symlink():
                    raise OSError("Symbolic link")
                with tempfile.TemporaryFile(dir=path) as probe:
                    probe.write(b"gateway-check")
                    probe.flush()
        except OSError:
            writable = False
        add(
            "storage",
            writable,
            "Private gateway configuration and certificate storage are writable",
            "Gateway storage is not writable; repair the MediaVPN private data mount through MediaHub",
        )
        blocked = next(
            (row["message"] for row in checks if row["state"] == "blocked"), None
        )
        value = {
            "ready": blocked is None,
            "runtime": self.runtime.enabled,
            "os": info.get("OSType"),
            "architecture": architecture,
            "tun": tun,
            "checks": checks,
            "message": blocked or "Gateway prerequisites are ready",
        }
        self.preflight_cache, self.preflight_at = value, time.monotonic() + 15
        return value
