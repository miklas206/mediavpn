"""Provider-published country for the verified exit IP (FjordVPN reference).

Fetch the complete Proton geofeed, never send a user's IP as a query.
Location is informational and must not affect tunnel readiness.
"""
import csv
import io
import ipaddress
import threading
import time
from urllib.request import urlopen

GEOFEED_URL = "https://ip.me/static/geofeeds/geofeed-mm.csv"
MAX_BYTES = 262144


class ExitLocation:
    def __init__(self, fetch=None, clock=time.monotonic):
        self.fetch = fetch or self.download
        self.clock = clock
        self.lock = threading.Lock()
        self.networks = []
        self.expires = 0
        self.retry = 0

    @staticmethod
    def download():
        with urlopen(GEOFEED_URL, timeout=2) as response:
            return response.read(MAX_BYTES + 1)

    def country(self, address):
        try:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                return None
        except ValueError:
            return None
        with self.lock:
            now = self.clock()
            if now >= self.expires:
                if now < self.retry:
                    return None
                self.retry = now + 60
                try:
                    raw = self.fetch()
                    if len(raw) > MAX_BYTES:
                        return None
                    rows = csv.reader(io.StringIO(raw.decode("utf-8-sig")))
                    networks = []
                    for row in rows:
                        if len(row) < 2 or row[0].lstrip().startswith("#"):
                            continue
                        code = row[1].strip().upper()
                        if len(code) != 2 or not code.isascii() or not code.isalpha():
                            continue
                        try:
                            networks.append((ipaddress.ip_network(row[0].strip()), code))
                        except ValueError:
                            continue
                    if not networks:
                        return None
                    self.networks = sorted(networks, key=lambda pair: pair[0].prefixlen, reverse=True)
                    self.expires = now + 21600
                except (OSError, ValueError, TypeError, csv.Error):
                    return None
            return next((code for network, code in self.networks if ip.version == network.version and ip in network), None)
