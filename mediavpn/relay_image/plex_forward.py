"""Forward only the explicitly registered Plex peer through its own provider."""
import ipaddress
import subprocess
import time

NAT = "MH_PROFILE_PLEX_NAT"
FILTER = "MH_PROFILE_PLEX_IN"
previous = object()


def validate(value):
    forward = value.get("plexForward")
    if forward is None:
        return None
    peer = ipaddress.IPv4Address(forward["address"])
    if (value.get("mode") != "gateway"
            or str(peer) not in {p["address"] for p in value.get("peers", [])}
            or type(forward.get("publicPort")) is not int
            or not 1024 <= forward["publicPort"] <= 65535
            or type(forward.get("expiresAt")) not in (int, float)):
        raise ValueError("Plex forwarding requires an existing peer and a fresh provider port")
    return forward


def run(*args, check=True):
    return subprocess.run(["iptables", *args], capture_output=True, check=check)


def ensure(table, chain, *rule, insert=False):
    if run("-t", table, "-C", chain, *rule, check=False).returncode:
        run("-t", table, "-I" if insert else "-A", chain, *rule)


def apply(value):
    global previous
    forward = validate(value)
    if forward and forward["expiresAt"] <= time.time():
        forward = None
    signature = (forward["address"], forward["publicPort"]) if forward else None
    for table, chain in [("nat", NAT), ("filter", FILTER)]:
        run("-t", table, "-N", chain, check=False)
        if signature != previous:
            run("-t", table, "-F", chain)
    ensure("nat", "PREROUTING", "-i", "tun0", "-j", NAT, insert=True)
    ensure("filter", "FORWARD", "-i", "tun0", "-o", "wg-hub", "-j", FILTER, insert=True)
    if forward:
        ensure("nat", NAT, "-p", "tcp", "--dport", str(forward["publicPort"]),
               "-j", "DNAT", "--to-destination", forward["address"] + ":32400")
        ensure("filter", FILTER, "-p", "tcp", "-d", forward["address"], "--dport", "32400",
               "-m", "conntrack", "--ctstate", "DNAT", "-j", "ACCEPT")
    previous = signature
