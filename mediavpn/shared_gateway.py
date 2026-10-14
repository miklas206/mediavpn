"""One provider tunnel with private WireGuard relay peers for managed app hosts."""
# ruff: noqa: BLE001, S110
# Docker exceptions may contain secret-bearing create payloads. Report only
# fixed messages and fail closed instead of logging the exception or payload.

import base64
import hashlib
import ipaddress
import json
import os
import secrets
import threading
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import HTTPException

IMAGE = "mediahub-vpn-relay:1"
LABEL = "org.mediahub.vpn.shared-gateway"


def new_key():
    private = X25519PrivateKey.generate()
    return base64.b64encode(private.private_bytes_raw()).decode(), base64.b64encode(
        private.public_key().public_bytes_raw()
    ).decode()


def save(path, value):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output)
    temporary.replace(path)


class SharedGateway:
    def __init__(self, runtime, data, origin):
        self.runtime = runtime
        self.data = Path(data) / "shared"
        self.data.mkdir(mode=0o700, exist_ok=True)
        self.path = self.data / "settings.json"
        self.config = self.data / "gateway.json"
        self.lock = threading.RLock()
        self.error = None
        self.origin = origin
        self.image = os.environ.get("MEDIAVPN_RELAY_IMAGE", IMAGE)
        runtime.shared_profile = self.profile
        runtime.shared_endpoint = self.endpoint
        runtime.stop_shared = self.stop_helper
        self.token_path = Path(data) / "integrations/mediahub.token"
        self.token_path.parent.mkdir(mode=0o700, exist_ok=True)
        if not self.token_path.exists():
            descriptor = os.open(
                self.token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_urlsafe(48))

    def settings(self):
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"enabled": False, "profileId": None, "peers": []}

    def profile(self):
        value = self.settings()
        return value["profileId"] if value["enabled"] else None

    def endpoint(self):
        from urllib.parse import urlsplit

        raw = os.environ.get(
            "MEDIAVPN_SHARED_ADDRESS", urlsplit(self.origin).hostname or ""
        )
        try:
            ip = ipaddress.ip_address(raw)
            port = int(os.environ.get("MEDIAVPN_SHARED_PORT", "18804"))
            if (
                ip.version != 4
                or not any(
                    ip in ipaddress.ip_network(network)
                    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
                )
                or ip.is_loopback
                or ip.is_link_local
                or not 1024 <= port <= 65535
            ):
                raise ValueError()
            return str(ip), port
        except ValueError:
            raise HTTPException(
                409, "Configure the private LAN address for the shared VPN gateway"
            ) from None

    def configure(self, profile, enabled):
        with self.lock:
            state = self.settings()
            if enabled:
                self.endpoint()
                if not (self.data / "keys.json").exists():
                    private, public = new_key()
                    save(
                        self.data / "keys.json",
                        {"privateKey": private, "publicKey": public},
                    )
            if state["profileId"] != profile or not enabled:
                self.stop_helper()
            state.update(enabled=enabled, profileId=profile)
            save(self.path, state)
            return self.public()

    def relay(self):
        rows = self.runtime.docker().containers.list(
            all=True, filters={"label": f"{LABEL}=mediavpn"}
        )
        if len(rows) > 1:
            raise HTTPException(
                409, "Multiple shared VPN gateways require operator review"
            )
        return rows[0] if rows else None

    def stop_helper(self):
        if not self.runtime.enabled:
            return
        row = self.relay()
        if row:
            row.stop(timeout=10)
            row.remove(v=False)

    def peer(self, identifier, public_key, name):
        try:
            if len(base64.b64decode(public_key, validate=True)) != 32:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(422, "Invalid relay public key") from None
        with self.lock:
            state = self.settings()
            if not state["enabled"]:
                raise HTTPException(
                    409, "Select and enable one shared VPN profile first"
                )
            peer = next((p for p in state["peers"] if p["id"] == identifier), None)
            if peer and peer["publicKey"] != public_key:
                raise HTTPException(
                    409, "Relay identity changed; remove its old registration first"
                )
            if not peer:
                if any(p["publicKey"] == public_key for p in state["peers"]):
                    raise HTTPException(
                        409, "Each app relay requires a separate private identity"
                    )
                if len(state["peers"]) >= 200:
                    raise HTTPException(409, "Shared VPN relay limit reached")
                used = {p["address"] for p in state["peers"]}
                address = next(
                    "10.250.87." + str(i)
                    for i in range(2, 202)
                    if "10.250.87." + str(i) not in used
                )
                peer = {
                    "id": identifier,
                    "name": name,
                    "publicKey": public_key,
                    "address": address,
                }
                state["peers"].append(peer)
                save(self.path, state)
            self.write_config(state)
            ip, port = self.endpoint()
            keys = json.loads((self.data / "keys.json").read_text())
            return {
                "peerId": identifier,
                "profileId": state["profileId"],
                "address": peer["address"] + "/24",
                "endpointIp": ip,
                "endpointPort": port,
                "gatewayKey": keys["publicKey"],
                "ready": self.public()["ready"],
            }

    def remove_peer(self, identifier):
        with self.lock:
            state = self.settings()
            state["peers"] = [p for p in state["peers"] if p["id"] != identifier]
            save(self.path, state)
            if (self.data / "keys.json").exists():
                self.write_config(state)
            return self.public()

    def write_config(self, state):
        keys = json.loads((self.data / "keys.json").read_text())
        save(
            self.config,
            {
                "mode": "gateway",
                "privateKey": keys["privateKey"],
                "address": "10.250.87.1/24",
                "listenPort": 51879,
                "peers": [
                    {"publicKey": p["publicKey"], "address": p["address"]}
                    for p in state["peers"]
                ],
            },
        )

    def ensure_image(self):
        client = self.runtime.docker()
        source = Path(__file__).parent / "relay_image"
        fingerprint = hashlib.sha256(
            b"".join(
                (source / name).read_bytes().replace(b"\r\n", b"\n")
                for name in ["Dockerfile", "runtime.py"]
            )
        ).hexdigest()
        try:
            image = client.images.get(self.image)
            if image.labels.get("org.mediahub.vpn.relay-source") == fingerprint:
                return image.id
        except Exception:
            pass
        image, _ = client.images.build(
            path=str(source),
            tag=self.image,
            rm=True,
            labels={"org.mediahub.vpn.relay-source": fingerprint, "org.mediahub.managed-component": "vpn-relay"},
            timeout=600,
        )
        return image.id

    def reconcile(self):
        if not self.runtime.enabled:
            return
        with self.lock:
            state = self.settings()
            if not state["enabled"]:
                self.stop_helper()
                return
            try:
                observed = self.runtime.status(state["profileId"])
                if not observed.get("healthy"):
                    self.stop_helper()
                    self.error = (
                        "Shared VPN is not verified; app internet traffic stays blocked"
                    )
                    return
                vpn = self.runtime.container(state["profileId"])
                vpn.reload()
                self.write_config(state)
                row = self.relay()
                image = self.ensure_image()
                if row:
                    row.reload()
                    if (
                        row.attrs["HostConfig"]["NetworkMode"] != "container:" + vpn.id
                        or row.image.id != image
                        or row.attrs["State"].get("Health", {}).get("Status")
                        == "unhealthy"
                    ):
                        self.stop_helper()
                        row = None
                if not row:
                    row = self.runtime.docker().containers.run(
                        image,
                        name="mediavpn-shared-gateway",
                        detach=True,
                        network_mode="container:" + vpn.id,
                        volumes={
                            str(self.runtime.host_data / "shared"): {
                                "bind": "/config",
                                "mode": "ro",
                            }
                        },
                        command=[],
                        labels={
                            LABEL: "mediavpn",
                            "org.mediahub.mediavpn.owner": "mediavpn",
                            "org.mediahub.mediavpn.profile": state["profileId"],
                        },
                        environment={"RELAY_CONFIG": "/config/gateway.json"},
                        cap_drop=["ALL"],
                        devices=["/dev/net/tun:/dev/net/tun:rwm"],
                        cap_add=["NET_ADMIN"],
                        read_only=True,
                        tmpfs={
                            "/run": "rw,noexec,nosuid,nodev,size=1m",
                            "/tmp": "rw,noexec,nosuid,nodev,size=1m",
                        },
                        security_opt=["no-new-privileges:true"],
                        restart_policy={"Name": "no"},
                        log_config={"Type": "none", "Config": {}},
                        healthcheck={
                            "Test": [
                                "CMD",
                                "python3",
                                "/opt/relay/runtime.py",
                                "health",
                            ],
                            "Interval": 10_000_000_000,
                            "Timeout": 3_000_000_000,
                            "Retries": 3,
                        },
                    )
                elif not row.attrs["State"].get("Running"):
                    row.start()
                self.error = None
            except Exception:
                self.error = "Shared VPN gateway could not be verified; app internet traffic stays blocked"
                try:
                    self.stop_helper()
                except Exception:
                    pass

    def public(self):
        state = self.settings()
        ready = False
        observed = {}
        if state["enabled"] and self.runtime.enabled:
            observed = self.runtime.status(state["profileId"])
            try:
                row = self.relay()
                if row:
                    row.reload()
                    ready = (
                        observed.get("healthy", False)
                        and row.attrs["State"].get("Running")
                        and row.attrs["State"].get("Health", {}).get("Status")
                        == "healthy"
                    )
            except Exception:
                pass
        return {
            "enabled": state["enabled"],
            "profileId": state["profileId"],
            "ready": bool(ready),
            "publicIp": observed.get("publicIp"),
            "forwardedPort": observed.get("forwardedPort"),
            "maximumProviderConnections": 1,
            "peers": [{"id": p["id"], "name": p["name"]} for p in state["peers"]],
            "message": self.error,
            "relayAvailable": self.runtime.enabled,
        }
