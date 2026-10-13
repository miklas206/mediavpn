import os
import ipaddress
import json
import time
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath

from fastapi import HTTPException
from .location import ExitLocation
from .proton import ProtonSelector, selected_configuration

OWNER = "org.mediahub.mediavpn.owner"


class Runtime:
    def __init__(self, data, enabled=False, host_data=None, client=None, location=None):
        self.data = data
        self.enabled = enabled
        self.host_data = PurePosixPath(host_data) if host_data else None
        self.client = client
        self.location = location or ExitLocation()
        self.selector = ProtonSelector()
        self.image = os.environ.get("MEDIAVPN_GLUETUN_IMAGE", "qmcgaw/gluetun:v3.40.0")
        self.shared_profile = lambda: None
        self.shared_endpoint = lambda: None
        self.stop_shared = lambda: None

    def docker(self):
        if not self.enabled or not self.host_data or not self.host_data.is_absolute() or ".." in self.host_data.parts:
            raise HTTPException(409, "VPN-runtime er ikke aktiveret. Preview starter ingen tunneler.")
        if self.client is None:
            import docker
            self.client = docker.from_env(timeout=10)
        return self.client

    def container(self, identifier):
        client = self.docker()
        rows = client.containers.list(all=True, filters={"label": [f"{OWNER}=mediavpn", f"org.mediahub.mediavpn.profile={identifier}"]})
        rows = [row for row in rows if getattr(row, "labels", {}).get("org.mediahub.vpn.shared-gateway") != "mediavpn"]
        if len(rows) > 1:
            raise HTTPException(409, "Profilen har flere runtime-containere; kontroller installationen")
        return rows[0] if rows else None

    def status(self, identifier):
        if not self.enabled:
            return {"state": "disabled", "healthy": False, "checkedAt": time.time(), "publicIp": None, "forwardedPort": None}
        try:
            container = self.container(identifier)
            if not container:
                state = "stopped"
            else:
                container.reload()
                status = container.attrs.get("State", {})
                health = status.get("Health", {})
                entries = health.get("Log", [])
                recent = False
                if entries:
                    try:
                        timestamp = datetime.fromisoformat(entries[-1]["End"].replace("Z", "+00:00"))
                        recent = 0 <= (datetime.now(timezone.utc) - timestamp).total_seconds() < 90 and entries[-1].get("ExitCode") == 0
                    except (KeyError, ValueError, TypeError):
                        recent = False
                state = "unverified" if status.get("Running") else "stopped"
                if status.get("Running") and health.get("Status") == "healthy" and recent:
                    tunnel = self.control(container, "/v1/vpn/status")
                    address = self.control(container, "/v1/publicip/ip")
                    public_ip = ipaddress.ip_address(address.get("public_ip", ""))
                    interface = container.exec_run(["ip", "-j", "address", "show", "tun0"])
                    if interface.exit_code or len(interface.output) > 16384:
                        raise ValueError("VPN interface unavailable")
                    interfaces = json.loads(interface.output)
                    interface_ready = isinstance(interfaces, list) and any(isinstance(row, dict) and row.get("ifname") == "tun0" and "UP" in row.get("flags", []) and any(item.get("family") == "inet" for item in row.get("addr_info", []) if isinstance(item, dict)) for row in interfaces)
                    if tunnel.get("status") == "running" and public_ip.version == 4 and public_ip.is_global and interface_ready:
                        forwarded_port = None
                        try:
                            forwarding = self.control(container, "/v1/openvpn/portforwarded")
                            port = forwarding.get("port")
                            if type(port) is int and 1 <= port <= 65535:
                                forwarded_port = port
                        except Exception:
                            pass
                        try:
                            country = self.location.country(str(public_ip))
                        except Exception:
                            country = None  # Informational location cannot close a verified tunnel gate.
                        labels = getattr(container, "labels", {})
                        if not country:
                            reported = address.get("country")
                            if isinstance(reported, str) and re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,59}", reported) and reported.lower() not in {"unknown", "n/a", "none"}:
                                country = reported
                        return {"state": "connected", "healthy": True, "checkedAt": time.time(), "publicIp": str(public_ip), "forwardedPort": forwarded_port, "country": country or None, "selectedServer": labels.get("org.mediahub.mediavpn.server"), "latencyMs": labels.get("org.mediahub.mediavpn.latency")}
            return {"state": state, "healthy": False, "checkedAt": time.time(), "publicIp": None, "forwardedPort": None}
        except HTTPException:
            raise
        except Exception:
            return {"state": "unavailable", "healthy": False, "checkedAt": time.time(), "publicIp": None, "forwardedPort": None}

    @staticmethod
    def control(container, path):
        result = container.exec_run(["wget", "-q", "-T", "4", "-O", "-", "http://127.0.0.1:8000" + path])
        if result.exit_code or len(result.output) > 16384:
            raise ValueError("VPN control response unavailable")
        data = json.loads(result.output)
        if not isinstance(data, dict):
            raise ValueError("Invalid VPN control response")
        return data

    def start(self, identifier, port_forwarding=False, auto_country=None):
        shared = self.shared_profile()
        if shared and identifier != shared:
            raise HTTPException(409, "Only the selected shared VPN profile may connect")
        endpoint = self.shared_endpoint() if shared else None
        marker = str(endpoint) if endpoint else ""
        try:
            container = self.container(identifier)
            mode = auto_country or ""
            if container:
                container.reload()
                expected = "on" if port_forwarding else "off"
                environment = container.attrs.get("Config", {}).get("Env", [])
                compatible = container.labels.get("org.mediahub.mediavpn.auto-country", "") == mode
                compatible = compatible and container.labels.get("org.mediahub.mediavpn.shared", "") == marker
                running = container.attrs.get("State", {}).get("Running")
                if compatible and (not mode or running) and f"VPN_PORT_FORWARDING={expected}" in environment and container.labels.get("org.mediahub.mediavpn.runtime") == "2":
                    if not container.attrs.get("State", {}).get("Running"):
                        container.start()
                    return
                container.stop(timeout=10)
                if shared:
                    self.stop_shared()
                container.remove()
            filename = f"{identifier}.conf"
            labels = {OWNER: "mediavpn", "org.mediahub.mediavpn.profile": identifier, "org.mediahub.mediavpn.runtime": "2"}
            labels["org.mediahub.mediavpn.shared"] = marker
            if auto_country:
                if not port_forwarding:
                    raise HTTPException(409, "Automatisk servervalg kræver Proton NAT-PMP")
                server = self.selector.choose(auto_country)
                configuration = selected_configuration((self.data / "profiles" / filename).read_text(), server)
                filename = f"{identifier}.auto.conf"
                descriptor = os.open(self.data / "profiles" / filename, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "w") as output:
                    output.write(configuration)
                (self.data / "profiles" / filename).chmod(0o600)
                labels.update({"org.mediahub.mediavpn.auto-country": auto_country, "org.mediahub.mediavpn.server": server["hostname"], "org.mediahub.mediavpn.latency": str(server["latencyMs"])})
            control_config = self.data / "gluetun-control.toml"
            control_config.write_text('[[roles]]\nname = "mediavpn-status"\nroutes = ["GET /v1/vpn/status", "GET /v1/publicip/ip", "GET /v1/openvpn/portforwarded"]\nauth = "none"\n')
            control_config.chmod(0o600)
            extra = {}
            if endpoint:
                extra["ports"] = {"51879/udp": (endpoint[0], endpoint[1])}
            self.docker().containers.run(
                self.image, name=f"mediavpn-{identifier}", detach=True,
                labels=labels,
                environment={"VPN_SERVICE_PROVIDER": "custom", "VPN_TYPE": "wireguard", "FIREWALL": "on", "UPDATER_PERIOD": "0", "HTTP_CONTROL_SERVER_ADDRESS": "127.0.0.1:8000", "VPN_PORT_FORWARDING": "on" if port_forwarding else "off", "VPN_PORT_FORWARDING_PROVIDER": "protonvpn", "FIREWALL_INPUT_PORTS": "51879" if endpoint else ""},
                volumes={str(self.host_data / "profiles" / filename): {"bind": "/gluetun/wireguard/wg0.conf", "mode": "ro"}, str(self.host_data / control_config.name): {"bind": "/gluetun/auth/config.toml", "mode": "ro"}},
                cap_add=["NET_ADMIN"], devices=["/dev/net/tun:/dev/net/tun:rwm"],
                sysctls={"net.ipv6.conf.all.disable_ipv6": "1", "net.ipv6.conf.default.disable_ipv6": "1", **({"net.ipv4.ip_forward": "1"} if endpoint else {})},
                security_opt=["no-new-privileges:true"], restart_policy={"Name": "no"},
                log_config={"Type": "json-file", "Config": {"max-size": "5m", "max-file": "2"}},
                **extra,
            )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "VPN kunne ikke startes. Kontroller Docker, image og TUN-adgang.") from None

    def stop(self, identifier, remove=False):
        if not self.enabled:
            return
        try:
            if self.shared_profile() == identifier:
                self.stop_shared()
            container = self.container(identifier)
            if container:
                container.stop(timeout=10)
                if remove:
                    container.remove()
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "VPN kunne ikke stoppes; profilen og noeglen er bevaret") from None
