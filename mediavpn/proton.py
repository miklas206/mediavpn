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
from urllib.request import Request, urlopen
from urllib.error import HTTPError

CATALOG_URL = "https://raw.githubusercontent.com/qdm12/gluetun-servers/main/pkg/servers/protonvpn.json"
MAX_BYTES = 2 * 1024 * 1024
LOAD_URL = "https://api.protonvpn.ch/vpn/v1/logicals?SecureCoreFilter=all&WithState=true"
MAX_LOAD_BYTES = 24 * 1024 * 1024


class ProtonSelectionError(ValueError):
    """Safe, user-facing failure of the public server catalog or probes."""


class ProtonSelector:
    def __init__(self, fetch=None, probe=None, clock=time.time, sample=None, fetch_load=None):
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
        self.fetch_load = fetch_load or self.download_load
        self.load_lock = threading.Lock()
        self.loads = {}
        self.loads_until = 0
        self.loads_at = None
        self.load_status = "unavailable"

    @staticmethod
    def download_load():
        request = Request(LOAD_URL, headers={"User-Agent": "MediaVPN/0.1.0", "Accept": "application/json", "x-pm-appversion": "Other"})
        with urlopen(request, timeout=5) as response:
            return response.read(MAX_LOAD_BYTES + 1)

    def server_loads(self, force=False):
        """Optional provider telemetry. Missing load is never invented as 0%."""
        with self.load_lock:
            if not force and self.clock() < self.loads_until:
                return self.loads
            loads = {}
            self.load_status = "unavailable"
            try:
                raw = self.fetch_load()
                if len(raw) > MAX_LOAD_BYTES:
                    raise ValueError("Load response too large")
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("Code") in (401, 403):
                    self.load_status = "authorization_required"
                rows = data.get("LogicalServers")
                if data.get("Code") != 1000 or not isinstance(rows, list) or len(rows) > 30000:
                    raise ValueError("Invalid load response")
                for row in rows:
                    if not isinstance(row, dict) or type(row.get("Load")) not in (int, float) or not 0 <= row["Load"] <= 100:
                        continue
                    if type(row.get("Features")) is not int or not row["Features"] & 4 or row.get("Status") != 1:
                        continue
                    servers = row.get("Servers")
                    if not isinstance(servers, list):
                        continue
                    for server in servers[:32]:
                        if not isinstance(server, dict) or server.get("Status") != 1:
                            continue
                        identity = (server.get("EntryIP"), server.get("X25519PublicKey"))
                        if all(isinstance(value, str) for value in identity):
                            # An endpoint may serve multiple logical servers;
                            # use the highest reported load rather than understate it.
                            loads[identity] = max(loads.get(identity, 0), row["Load"])
            except HTTPError as error:
                if error.code in (401, 403):
                    self.load_status = "authorization_required"
                loads = {}
            except (OSError, ValueError, TypeError, AttributeError):
                loads = {}  # No stale percentages or account login dependencies.
            self.loads = loads
            if loads:
                self.load_status = "available"
            self.loads_at = self.clock() if loads else None
            self.loads_until = self.clock() + 60
            return loads

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

    def catalog(self, force=False):
        with self.lock:
            if not force and self.rows and self.clock() < self.expires:
                return self.rows
            try:
                raw = self.fetch()
            except OSError:
                raise ProtonSelectionError("Protons serverliste kunne ikke hentes. Kontrollér serverens internetforbindelse og prøv igen.") from None
            if len(raw) > MAX_BYTES:
                raise ProtonSelectionError("Serverlisten er for stor")
            try:
                data = json.loads(raw)
            except ValueError:
                raise ProtonSelectionError("Protons serverliste indeholder ugyldige data. Prøv at opdatere anbefalingerne senere.") from None
            if not isinstance(data, dict) or not isinstance(data.get("timestamp"), (int, float)) or not 0 <= self.clock() - data["timestamp"] < 120 * 86400:
                raise ProtonSelectionError("Serverlisten er for gammel eller ugyldig")
            servers = data.get("servers")
            if not isinstance(servers, list) or len(servers) > 20000:
                raise ProtonSelectionError("Ugyldig serverliste")
            rows = []
            seen = set()
            for row in servers:
                if not isinstance(row, dict) or row.get("vpn") != "wireguard" or row.get("port_forward") is not True:
                    continue
                country, hostname, key = row.get("country"), row.get("hostname"), row.get("wgpubkey")
                if not isinstance(country, str) or not re.fullmatch(r"[A-Za-z][A-Za-z .,'-]{0,59}", country):
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
                raise ProtonSelectionError("Ingen Proton P2P-servere i listen")
            self.rows, self.expires = rows, self.clock() + 21600
            return self.rows

    def countries(self):
        return sorted({row["country"] for row in self.catalog()})

    def measure(self, country):
        rows = self.catalog()
        loads = self.server_loads()
        rows = [{**row, "loadPercent": loads.get((row["ip"], row["key"]))} for row in rows]
        def candidates_for(pool, count):
            known = sorted([row for row in pool if row["loadPercent"] is not None], key=lambda row: row["loadPercent"])
            # Check the least loaded candidates first; ties are randomized.
            if known:
                random_pool = self.sample(known, len(known))
                return sorted(random_pool, key=lambda row: row["loadPercent"])[:count]
            return self.sample(pool, min(count, len(pool)))
        if country == "*":
            # Cover every country rather than accidentally omit a nearby one.
            # Still probe only a bounded endpoint sample within each country.
            countries = sorted({row["country"] for row in rows})
            if len(countries) > 256:
                raise ProtonSelectionError("Serverlisten indeholder for mange lande")
            per_country = 2 if len(countries) <= 24 else 1
            candidates = []
            for name in countries:
                pool = [row for row in rows if row["country"] == name]
                candidates.extend(candidates_for(pool, per_country))
        else:
            pool = [row for row in rows if row["country"] == country]
            candidates = candidates_for(pool, 24)
        if not candidates:
            raise ProtonSelectionError("Ingen Proton P2P-servere i det valgte land")
        with ThreadPoolExecutor(max_workers=24 if country == "*" else 12) as pool:
            measurements = list(pool.map(lambda row: self.probe(row["ip"]), candidates))
        ranked = sorted([(latency, row) for row, latency in zip(candidates, measurements) if type(latency) in (int, float) and 0 <= latency < 10000], key=lambda pair: (pair[1]["loadPercent"] if pair[1]["loadPercent"] is not None else 101, pair[0]))
        if not ranked:
            raise ProtonSelectionError("Ingen af de målte Proton-servere svarede på forbindelsestesten fra din normale internetforbindelse. Prøv at opdatere anbefalingerne igen.")
        return ranked, len(candidates)

    def recommendations(self, force=False):
        with self.measure_lock:
            if not force and self.recommended and self.clock() < self.measured_until:
                return self.recommended
            if force:
                self.catalog(force=True)
                self.server_loads(force=True)
            ranked, tested = self.measure("*")
            countries = {}
            for latency, row in ranked:
                countries.setdefault(row["country"], {"country": row["country"], "server": row["hostname"], "latencyMs": round(latency, 1), "loadPercent": row["loadPercent"]})
            self.recommended = {"recommendations": list(countries.values()), "tested": tested, "totalCountries": len(self.countries()), "measuredAt": self.clock(), "loadMeasuredAt": self.loads_at, "loadAvailable": self.loads_at is not None, "loadStatus": self.load_status}
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
