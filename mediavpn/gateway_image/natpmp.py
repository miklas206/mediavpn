"""Independent torrent lease in the existing provider namespace; never port zero cleanup."""

import json
import signal
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

try:
    from .observer import fresh, write
except ImportError:
    from observer import fresh, write

CHAIN = "MH_WEB_TORRENT"


def mapping(packet, opcode, private, forbidden, deleting=False):
    if len(packet) != 16:
        raise ValueError("Invalid NAT-PMP response length")
    version, reply, result, epoch, inside, outside, lifetime = struct.unpack(
        "!BBHIHHI", packet
    )
    if (
        version != 0
        or reply != opcode + 128
        or result != 0
        or inside != private
        or not 1024 <= private <= 65535
        or not 1 <= outside <= 65535
        or outside in forbidden
        or (not deleting and not 30 <= lifetime <= 7200)
    ):
        raise ValueError("NAT-PMP rejected the separate torrent mapping")
    return {
        "internalPort": inside,
        "publicPort": outside,
        "lifetime": lifetime,
        "epoch": epoch,
    }


def request(opcode, private, public, lifetime, forbidden, gateway="10.2.0.1"):
    if opcode not in (1, 2) or not 1024 <= private <= 65535:
        raise ValueError("Invalid explicit NAT-PMP mapping")
    # This is Proton's documented internal NAT-PMP endpoint, not a home router.
    # Refuse if routing has fallen back to the physical interface.
    route = subprocess.run(
        ["ip", "-j", "route", "get", gateway],
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )
    if not any(row.get("dev") == "tun0" for row in json.loads(route.stdout)):
        raise ValueError("NAT-PMP endpoint is outside the provider tunnel")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stream:
        stream.settimeout(3)
        stream.connect((gateway, 5351))
        stream.send(struct.pack("!BBHHHI", 0, opcode, 0, private, public, lifetime))
        payload = stream.recv(64)
    return mapping(payload, opcode, private, forbidden, deleting=lifetime == 0)


def lease(private, previous, forbidden, exchange=request):
    tcp = exchange(2, private, previous, 60, forbidden)
    try:
        udp = exchange(1, private, tcp["publicPort"], 60, forbidden)
        if tcp["publicPort"] != udp["publicPort"]:
            raise ValueError("TCP and UDP received different public ports")
        return {
            "internalPort": private,
            "publicPort": tcp["publicPort"],
            "lifetime": min(tcp["lifetime"], udp["lifetime"]),
        }
    except Exception:
        # Release only this explicit private port. Never release all provider mappings.
        for opcode in (1, 2):
            try:
                exchange(opcode, private, tcp["publicPort"], 0, forbidden)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        raise


def iptables(*args, check=True):
    return subprocess.run(
        ["iptables", "-w", "3", *args], capture_output=True, check=check, timeout=5
    )


def firewall(port=None):
    iptables("-N", CHAIN, check=False)
    if iptables("-C", "INPUT", "-i", "tun0", "-j", CHAIN, check=False).returncode:
        iptables("-I", "INPUT", "1", "-i", "tun0", "-j", CHAIN)
    iptables("-F", CHAIN)
    if port:
        for protocol in ("tcp", "udp"):
            iptables("-A", CHAIN, "-p", protocol, "--dport", str(port), "-j", "ACCEPT")


def main():
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    current, granted, renewed, previous = None, None, 0, 0
    output = Path("/state/status.json")
    try:
        firewall()
        while not stop.is_set():
            report = {"ready": False, "checkedAt": time.time()}
            try:
                value = json.loads(Path("/config/settings.json").read_text())
                if not fresh(value) or not value.get("torrentEnabled"):
                    raise ValueError("Torrent lease not requested or provider stale")
                signature = (
                    value["generation"],
                    value["revision"],
                    value["publicIp"],
                    value["publicPort"],
                    value["internalPort"],
                )
                if signature != current:
                    firewall()
                    current, granted, previous = signature, None, 0
                if granted is None or time.monotonic() - renewed >= min(
                    30, granted["lifetime"] / 2
                ):
                    granted = lease(
                        value["internalPort"], previous, {value["publicPort"]}
                    )
                    previous, renewed = granted["publicPort"], time.monotonic()
                if time.monotonic() - renewed >= granted["lifetime"] - 5:
                    raise ValueError("Torrent lease expired")
                firewall(granted["internalPort"])
                report.update(
                    ready=True,
                    generation=value["generation"],
                    revision=value["revision"],
                    publicIp=value["publicIp"],
                    webPort=value["publicPort"],
                    internalPort=granted["internalPort"],
                    publicPort=granted["publicPort"],
                    expiresAt=time.time()
                    + granted["lifetime"]
                    - (time.monotonic() - renewed),
                )
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                subprocess.SubprocessError,
            ):
                granted = None
                report["message"] = (
                    "Separate torrent mapping is unavailable; torrent traffic remains blocked"
                )
                firewall()
            write(output, report)
            stop.wait(2)
    finally:
        firewall()
        if current and previous:
            for opcode in (1, 2):
                try:
                    request(opcode, current[-1], previous, 0, {current[-2]})
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
        write(output, {"ready": False, "checkedAt": time.time()})


if __name__ == "__main__":
    main()
