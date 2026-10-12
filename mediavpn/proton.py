"""Bounded selection of Proton P2P endpoints using the public Gluetun catalog.

Latency is a TCP connect measurement, not a bandwidth or VPN-health claim.
Private keys never leave the original protected profile or its derived file.
"""
import base64
import configparser
import io
import ipaddress
import json
import random
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen

CATALOG_URL = "https://raw.githubusercontent.com/qdm12/gluetun-servers/main/pkg/servers/protonvpn.json"
MAX_BYTES = 2 * 1024 * 1024


class ProtonSelector:
    def __init__(self, fetch=None, probe=None, clock=time.time, sample=None):
        self.fetch = fetch or self.download
        self.probe = probe or self.latency
        self.clock = clock
        self.sample = sample or random.SystemRandom().sample
        self.lock = threading.Lock()
        self.rows = []
        self.expires = 0
        self.measure_lock = threading.Lock()
        self.recommended = None
        self.measured_until = 0

    @staticmethod
    def download():
        with urlopen(CATALOG_URL, timeout=5) as response:
            return response.read(MAX_BYTES + 1)

    @staticmethod
    def latency(address):
        samples = []
        for _ in range(2):
            started = time.monotonic()
            try:
                with socket.create_connection((address, 443), timeout=1.5):
                    samples.append((time.monotonic() - started) * 1000)
            except OSError:
                return None
        return sum(samples) / len(samples)

    def catalog(self):
        with self.lock:
            if self.rows and self.clock() < self.expires:
                return self.rows
            raw = self.fetch()
            if len(raw) > MAX_BYTES:
                raise ValueError("Serverlisten er for stor")
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("timestamp"), (int, float)) or not 0 <= self.clock() - data["timestamp"] < 120 * 86400:
                raise ValueError("Serverlisten er for gammel eller ugyldig")
            servers = data.get("servers")
            if not isinstance(servers, list) or len(servers) > 20000:
                raise ValueError("Ugyldig serverliste")
            rows = []
            seen = set()
            for row in servers:
                if not isinstance(row, dict) or row.get("vpn") != "wireguard" or row.get("port_forward") is not True:
                    continue
                country, hostname, key = row.get("country"), row.get("hostname"), row.get("wgpubkey")
                if not isinstance(country, str) or not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{0,59}", country):
                    continue
                if not isinstance(hostname, str) or not re.fullmatch(r"[a-z0-9-]{1,90}\.protonvpn\.(?:net|com)", hostname):
                    continue
                try:
                    if len(base64.b64decode(key, validate=True)) != 32:
                        continue
                except (ValueError, TypeError):
                    continue
                ips = row.get("ips", [])
                if not isinstance(ips, list):
                    continue
                for value in ips[:8]:
                    try:
                        address = ipaddress.ip_address(value)
                    except (ValueError, TypeError):
                        continue
                    if address.version != 4 or not address.is_global or str(address) in seen:
                        continue
                    seen.add(str(address))
                    rows.append({"country": country, "hostname": hostname, "key": key, "ip": str(address)})
            if not rows:
                raise ValueError("Ingen Proton P2P-servere i listen")
            self.rows, self.expires = rows, self.clock() + 21600
            return self.rows

    def countries(self):
        return sorted({row["country"] for row in self.catalog()})

    def measure(self, country):
        rows = self.catalog()
        if country == "*":
            # Distribute a bounded global sample across countries, not large fleets.
            countries = sorted({row["country"] for row in rows})
            selected = self.sample(countries, min(24, len(countries)))
            candidates = []
            for name in selected:
                pool = [row for row in rows if row["country"] == name]
                candidates.extend(self.sample(pool, min(2, len(pool))))
        else:
            pool = [row for row in rows if row["country"] == country]
            candidates = self.sample(pool, min(24, len(pool)))
        if not candidates:
            raise ValueError("Ingen Proton P2P-servere i det valgte land")
        with ThreadPoolExecutor(max_workers=12) as pool:
            measurements = list(pool.map(lambda row: self.probe(row["ip"]), candidates))
        ranked = sorted([(latency, row) for row, latency in zip(candidates, measurements) if type(latency) in (int, float) and 0 <= latency < 10000], key=lambda pair: pair[0])
        if not ranked:
            raise ValueError("Ingen af de maalte Proton-servere svarede")
        return ranked, len(candidates)

    def recommendations(self):
        with self.measure_lock:
            if self.recommended and self.clock() < self.measured_until:
                return self.recommended
            ranked, tested = self.measure("*")
            countries = {}
            for latency, row in ranked:
                countries.setdefault(row["country"], {"country": row["country"], "server": row["hostname"], "latencyMs": round(latency, 1)})
            self.recommended = {"recommendations": list(countries.values()), "tested": tested, "measuredAt": self.clock()}
            self.measured_until = self.clock() + 60
            return self.recommended

    def choose(self, country):
        ranked, tested = self.measure(country)
        latency, selected = ranked[0]
        return {**selected, "latencyMs": round(latency, 1), "tested": tested}


def selected_configuration(original, server):
    from .configuration import parse_wireguard
    normalized, _ = parse_wireguard(original)
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read_string(normalized)
    if "PresharedKey" in parser["Peer"]:
        raise ValueError("Automatisk Proton-valg understotter ikke PresharedKey")
    parser["Peer"]["Endpoint"] = server["ip"] + ":51820"
    parser["Peer"]["PublicKey"] = server["key"]
    output = io.StringIO()
    parser.write(output)
    return parse_wireguard(output.getvalue())[0]
