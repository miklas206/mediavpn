"""Persistent, isolated app relays; the legacy web gateway keeps its identity."""
import json
import re
import threading
from pathlib import Path

from fastapi import HTTPException

from mediavpn.shared_gateway import SharedGateway, save


class ProfileRelays:
    def __init__(self, runtime, data, origin, legacy):
        self.runtime, self.data, self.origin, self.legacy = runtime, Path(data), origin, legacy
        self.path = self.data / "profile-relays.json"
        self.lock = threading.RLock()
        self.instances = {}
        self.ports = json.loads(self.path.read_text()) if self.path.exists() else {}
        for identifier, port in self.ports.items():
            self.instances[identifier] = SharedGateway(runtime, data, origin, identifier, port)
        runtime.relay_endpoint = self.endpoint

    def get(self, identifier, create=False):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{24}", identifier):
            raise HTTPException(422, "Choose a VPN profile")
        with self.lock:
            if self.legacy.profile() == identifier:
                return self.legacy
            if identifier not in self.instances and create:
                used = {*self.ports.values(), self.legacy.endpoint()[1]}
                port = next((value for value in range(18805, 19805) if value not in used), None)
                if port is None:
                    raise HTTPException(409, "No private app relay ports available")
                self.ports[identifier] = port
                save(self.path, self.ports)
                self.instances[identifier] = SharedGateway(self.runtime, self.data, self.origin, identifier, port)
            return self.instances.get(identifier)

    def all(self):
        with self.lock:
            return list(self.instances.values())

    def endpoint(self, identifier):
        relay = self.get(identifier)
        return relay.endpoint() if relay and relay.profile() == identifier else None

    def stop(self, identifier):
        relay = self.get(identifier)
        if relay:
            relay.stop_helper()
