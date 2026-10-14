"""One explicitly owned TCP lease and bounded Plex relay in the provider namespace."""

import ipaddress
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

try:
    from .natpmp import iptables, request
    from .observer import fresh, write
except ImportError:
    from natpmp import iptables, request
    from observer import fresh, write

CHAIN = "MH_PLEX_REMOTE"


def target(value):
    address = ipaddress.IPv4Address(value["plex"]["address"])
    allowed = any(
        address in ipaddress.ip_network(n)
        for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    )
    if (
        not allowed
        or address.is_unspecified
        or address.is_multicast
        or value["plex"].get("port") != 32400
    ):
        raise ValueError("Plex needs its approved private address and TCP port 32400")
    private = value["plexInternalPort"]
    if (
        type(private) is not int
        or not 1024 <= private <= 65535
        or private in value.get("reservedInternalPorts", [])
    ):
        raise ValueError("Plex listener conflicts with another service")
    return str(address), private


def other_port(path, value):
    try:
        state = json.loads(Path(path).read_text())
        if (
            state.get("ready") is True
            and state.get("generation") == value["generation"]
            and state.get("publicIp") == value["publicIp"]
            and state.get("expiresAt", 0) > time.time()
            and type(state.get("publicPort")) is int
        ):
            return state["publicPort"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def firewall(port=None):
    iptables("-N", CHAIN, check=False)
    if iptables("-C", "INPUT", "-i", "tun0", "-j", CHAIN, check=False).returncode:
        iptables("-I", "INPUT", "1", "-i", "tun0", "-j", CHAIN)
    iptables("-F", CHAIN)
    if port:
        iptables("-A", CHAIN, "-p", "tcp", "--dport", str(port), "-j", "ACCEPT")


def tunnel_address():
    rows = json.loads(
        subprocess.check_output(
            ["ip", "-j", "-4", "addr", "show", "dev", "tun0"], timeout=5
        )
    )
    addresses = [
        a["local"]
        for row in rows
        for a in row.get("addr_info", [])
        if a.get("family") == "inet"
    ]
    if len(addresses) != 1:
        raise ValueError("Provider tunnel address is unavailable")
    return str(ipaddress.IPv4Address(addresses[0]))


def start_relay(address, private, bind):
    # No shell, HTTP parsing, TLS termination, credentials or fallback listener.
    return subprocess.Popen(
        [
            "socat",
            "-T300",
            f"TCP4-LISTEN:{private},bind={bind},reuseaddr,fork,max-children=64",
            f"TCP4:{address}:32400,connect-timeout=5",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def stop_relay(process):
    if process is None:
        return
    # Include forked streaming sessions, even if the listener has already died.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


def main():
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    output = Path("/state/status.json")
    process, signature, granted, renewed, previous, private = (
        None,
        None,
        None,
        0,
        0,
        None,
    )

    def close():
        nonlocal process, granted, previous
        firewall()
        stop_relay(process)
        process = None
        if private and previous:
            try:
                request(2, private, previous, 0, set())
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        granted, previous = None, 0

    try:
        firewall()
        while not stop.is_set():
            report = {"ready": False, "checkedAt": time.time()}
            try:
                value = json.loads(Path("/config/settings.json").read_text())
                if not fresh(value) or not value.get("plex"):
                    raise ValueError(
                        "Plex forwarding is disabled or provider status is stale"
                    )
                address, candidate = target(value)
                bind = tunnel_address()
                current = (
                    value["generation"],
                    value["revision"],
                    value["publicIp"],
                    address,
                    candidate,
                    bind,
                )
                if current != signature:
                    close()
                    signature, private = current, candidate
                forbidden = {value["publicPort"]}
                torrent = other_port("/torrent/status.json", value)
                if torrent:
                    forbidden.add(torrent)
                if granted and granted["publicPort"] in forbidden:
                    raise ValueError(
                        "Plex port conflicts with web or torrent forwarding"
                    )
                if granted is None or time.monotonic() - renewed >= min(
                    30, granted["lifetime"] / 2
                ):
                    granted = request(2, private, previous, 60, forbidden)
                    previous, renewed = granted["publicPort"], time.monotonic()
                if time.monotonic() - renewed >= granted["lifetime"] - 5:
                    raise ValueError("Plex port lease expired")
                if process is None:
                    process = start_relay(address, private, bind)
                if process.poll() is not None:
                    raise ValueError("Plex TCP listener is unavailable")
                firewall(private)
                report.update(
                    ready=True,
                    generation=value["generation"],
                    revision=value["revision"],
                    publicIp=value["publicIp"],
                    webPort=value["publicPort"],
                    internalPort=private,
                    publicPort=granted["publicPort"],
                    targetAddress=address,
                    targetPort=32400,
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
                close()
                report["message"] = (
                    "Separate Plex port is unavailable; Plex forwarding remains blocked"
                )
            write(output, report)
            stop.wait(2)
    finally:
        close()
        write(output, {"ready": False, "checkedAt": time.time()})


if __name__ == "__main__":
    main()
