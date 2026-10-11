import os
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
                state = "connected" if status.get("Running") and health.get("Status") == "healthy" and recent else "unverified" if status.get("Running") else "stopped"
            return {"state": state, "healthy": state == "connected", "checkedAt": time.time(), "publicIp": None, "forwardedPort": None}
        except HTTPException:
            raise
        except Exception:
            return {"state": "unavailable", "healthy": False, "checkedAt": time.time(), "publicIp": None, "forwardedPort": None}

    def start(self, identifier):
        try:
            container = self.container(identifier)
            if container:
                container.start()
                return
            self.docker().containers.run(
                self.image, name=f"mediavpn-{identifier}", detach=True,
                labels={OWNER: "mediavpn", "org.mediahub.mediavpn.profile": identifier},
                environment={"VPN_SERVICE_PROVIDER": "custom", "VPN_TYPE": "wireguard", "FIREWALL": "on", "UPDATER_PERIOD": "0", "HTTP_CONTROL_SERVER_ADDRESS": "127.0.0.1:8000"},
                volumes={str(self.host_data / "profiles" / f"{identifier}.conf"): {"bind": "/gluetun/wireguard/wg0.conf", "mode": "ro"}},
                cap_add=["NET_ADMIN"], devices=["/dev/net/tun:/dev/net/tun:rwm"],
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