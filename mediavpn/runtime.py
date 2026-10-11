import os
import ipaddress
import json
import time
from datetime import datetime, timezone
from pathlib import PurePosixPath

from fastapi import HTTPException

OWNER = "org.mediahub.mediavpn.owner"


class Runtime:
    def __init__(self, data, enabled=False, host_data=None, client=None):
        self.data = data
        self.enabled = enabled
        self.host_data = PurePosixPath(host_data) if host_data else None
        self.client = client
        self.image = os.environ.get("MEDIAVPN_GLUETUN_IMAGE", "qmcgaw/gluetun:v3.40.0")

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
                            forwarding = self.control(container, "/v1/portforward")
                            port = forwarding.get("port")
                            if type(port) is int and 1 <= port <= 65535:
                                forwarded_port = port
                        except Exception:
                            pass
                        return {"state": "connected", "healthy": True, "checkedAt": time.time(), "publicIp": str(public_ip), "forwardedPort": forwarded_port}
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

    def start(self, identifier, port_forwarding=False):
        try:
            container = self.container(identifier)
            if container:
                container.reload()
                expected = "on" if port_forwarding else "off"
                environment = container.attrs.get("Config", {}).get("Env", [])
                if f"VPN_PORT_FORWARDING={expected}" in environment:
                    if not container.attrs.get("State", {}).get("Running"):
                        container.start()
                    return
                container.stop(timeout=10)
                container.remove()
            control_config = self.data / "gluetun-control.toml"
            control_config.write_text('[[roles]]\nname = "mediavpn-status"\nroutes = ["GET /v1/vpn/status", "GET /v1/publicip/ip", "GET /v1/portforward"]\nauth = "none"\n')
            control_config.chmod(0o600)
            self.docker().containers.run(
                self.image, name=f"mediavpn-{identifier}", detach=True,
                labels={OWNER: "mediavpn", "org.mediahub.mediavpn.profile": identifier},
                environment={"VPN_SERVICE_PROVIDER": "custom", "VPN_TYPE": "wireguard", "FIREWALL": "on", "UPDATER_PERIOD": "0", "HTTP_CONTROL_SERVER_ADDRESS": "127.0.0.1:8000", "VPN_PORT_FORWARDING": "on" if port_forwarding else "off", "VPN_PORT_FORWARDING_PROVIDER": "protonvpn"},
                volumes={str(self.host_data / "profiles" / f"{identifier}.conf"): {"bind": "/gluetun/wireguard/wg0.conf", "mode": "ro"}, str(self.host_data / control_config.name): {"bind": "/gluetun/auth/config.toml", "mode": "ro"}},
                cap_add=["NET_ADMIN"], devices=["/dev/net/tun:/dev/net/tun:rwm"],
                sysctls={"net.ipv6.conf.all.disable_ipv6": "1", "net.ipv6.conf.default.disable_ipv6": "1"},
                security_opt=["no-new-privileges:true"], restart_policy={"Name": "no"},
                log_config={"Type": "json-file", "Config": {"max-size": "5m", "max-file": "2"}},
            )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "VPN kunne ikke startes. Kontroller Docker, image og TUN-adgang.") from None

    def stop(self, identifier, remove=False):
        if not self.enabled:
            return
        try:
            container = self.container(identifier)
            if container:
                container.stop(timeout=10)
                if remove:
                    container.remove()
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "VPN kunne ikke stoppes; profilen og noeglen er bevaret") from None