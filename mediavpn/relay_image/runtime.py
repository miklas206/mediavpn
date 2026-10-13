"""Reviewed WireGuard relay; provider private keys never enter this component."""
# ruff: noqa: BLE001
# Runtime failures must not print exceptions containing WireGuard keys.

import base64
import ipaddress
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

CONFIG = Path(os.environ.get("RELAY_CONFIG", "/config/settings.json"))
stop = False


def command(*args, check=True):
    return subprocess.run(list(args), check=check, capture_output=True, text=True).stdout.strip()


def key(value):
    if not isinstance(value, str) or len(base64.b64decode(value, validate=True)) != 32:
        raise ValueError("Invalid relay key")
    return value


def validate(value):
    if value.get("mode") not in {"gateway", "client"}:
        raise ValueError("Invalid relay mode")
    key(value["privateKey"])
    address = ipaddress.ip_interface(value["address"])
    if address.version != 4 or address.network.prefixlen != 24 or not address.ip.is_private:
        raise ValueError("Relay requires a private IPv4 subnet")
    if not isinstance(value.get("listenPort"), int) or not 1024 <= value["listenPort"] <= 65535:
        raise ValueError("Invalid relay port")
    if value["mode"] == "client":
        key(value["gatewayKey"])
        target = ipaddress.ip_address(value["endpointIp"])
        if (
            target.version != 4
            or not any(
                target in ipaddress.ip_network(network)
                for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            )
            or target.is_loopback
            or target.is_link_local
        ):
            raise ValueError("Relay gateway requires a reachable private IPv4 address")
        if (
            not isinstance(value.get("endpointPort"), int)
            or not 1024 <= value["endpointPort"] <= 65535
        ):
            raise ValueError("Invalid relay endpoint port")
    else:
        if len(value.get("peers", [])) > 200:
            raise ValueError("Too many peers")
        seen = set()
        for peer in value.get("peers", []):
            key(peer["publicKey"])
            ip = ipaddress.ip_address(peer["address"])
            if ip not in address.network or ip == address.ip or str(ip) in seen:
                raise ValueError("Invalid or duplicate peer address")
            seen.add(str(ip))
    return value


def wireguard(value):
    lines = [
        "[Interface]",
        "PrivateKey = " + key(value["privateKey"]),
        "ListenPort = " + str(value["listenPort"]),
        "FwMark = 51879",
    ]
    if value["mode"] == "client":
        lines += [
            "[Peer]",
            "PublicKey = " + key(value["gatewayKey"]),
            "Endpoint = " + value["endpointIp"] + ":" + str(value["endpointPort"]),
            "AllowedIPs = 0.0.0.0/0",
            "PersistentKeepalive = 25",
        ]
    else:
        for peer in value.get("peers", []):
            lines += [
                "[Peer]",
                "PublicKey = " + key(peer["publicKey"]),
                "AllowedIPs = " + peer["address"] + "/32",
            ]
    return "\n".join(lines) + "\n"


def firewall(value):
    if value["mode"] == "gateway":
        network = str(ipaddress.ip_interface(value["address"]).network)
        return [
            *[
                [
                    "iptables",
                    "-I",
                    "OUTPUT",
                    "1",
                    "-o",
                    "eth0",
                    "-d",
                    subnet,
                    "-p",
                    "udp",
                    "--sport",
                    "51879",
                    "-j",
                    "ACCEPT",
                ]
                for subnet in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            ],
            ["iptables", "-I", "FORWARD", "1", "-i", "wg-hub", "!", "-o", "tun0", "-j", "DROP"],
            [
                "iptables",
                "-I",
                "FORWARD",
                "1",
                "-i",
                "wg-hub",
                "-o",
                "tun0",
                "-s",
                network,
                "-j",
                "ACCEPT",
            ],
            [
                "iptables",
                "-I",
                "FORWARD",
                "1",
                "-i",
                "tun0",
                "-o",
                "wg-hub",
                "-d",
                network,
                "-m",
                "conntrack",
                "--ctstate",
                "ESTABLISHED,RELATED",
                "-j",
                "ACCEPT",
            ],
            [
                "iptables",
                "-t",
                "nat",
                "-A",
                "POSTROUTING",
                "-s",
                network,
                "-o",
                "tun0",
                "-j",
                "MASQUERADE",
            ],
        ]
    return [
        ["iptables", "-P", "OUTPUT", "DROP"],
        ["iptables", "-P", "FORWARD", "DROP"],
        ["ip6tables", "-P", "OUTPUT", "DROP"],
        ["ip6tables", "-P", "FORWARD", "DROP"],
        [
            "iptables",
            "-t",
            "mangle",
            "-A",
            "PREROUTING",
            "-i",
            "eth0",
            "-m",
            "conntrack",
            "--ctstate",
            "NEW",
            "-j",
            "CONNMARK",
            "--set-mark",
            "51879",
        ],
        ["iptables", "-t", "mangle", "-A", "OUTPUT", "-j", "CONNMARK", "--restore-mark"],
        ["iptables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"],
        ["iptables", "-A", "OUTPUT", "-o", "wg-hub", "-j", "ACCEPT"],
        [
            "iptables",
            "-A",
            "OUTPUT",
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED,RELATED",
            "-m",
            "mark",
            "--mark",
            "51879",
            "-j",
            "ACCEPT",
        ],
        [
            "iptables",
            "-A",
            "OUTPUT",
            "-o",
            "eth0",
            "-d",
            value["endpointIp"],
            "-p",
            "udp",
            "--dport",
            str(value["endpointPort"]),
            "-j",
            "ACCEPT",
        ],
    ]


def apply_firewall(value):
    # Never flush Gluetun's rules. Check individual owned rules before inserting
    # them so a helper restart cannot accumulate ACCEPT rules or change ordering.
    for args in firewall(value):
        if "-P" in args:
            command(*args)
            continue
        probe = list(args)
        position = probe.index("-I") if "-I" in probe else probe.index("-A")
        probe[position] = "-C"
        if "-I" in args:
            del probe[position + 2]
        result = subprocess.run(probe, capture_output=True, check=False)
        if result.returncode:
            command(*args)


def health():
    try:
        value = validate(json.loads(CONFIG.read_text()))
        command("ip", "link", "show", "wg-hub")
        if value["mode"] == "gateway":
            command("ip", "link", "show", "tun0")
            return True
        stamps = command("wg", "show", "wg-hub", "latest-handshakes").splitlines()
        return any(0 <= time.time() - int(line.split()[-1]) < 90 for line in stamps)
    except Exception:
        return False


def main():
    if sys.argv[1:] == ["health"]:
        return 0 if health() else 1
    value = validate(json.loads(CONFIG.read_text()))
    interface = "wg-hub"
    # Output is blocked before any application may share this namespace.
    apply_firewall(value)
    # This namespace is exclusively owned by this relay. Recover a stale
    # interface after a helper crash; firewall remains installed throughout.
    command("ip", "link", "delete", interface, check=False)
    userspace = None
    try:
        command("ip", "link", "add", interface, "type", "wireguard")
    except subprocess.CalledProcessError:
        # Existing LXCs cannot load modules into their hypervisor. The bundled
        # userspace implementation needs only the already provisioned TUN device.
        userspace = subprocess.Popen(
            ["wireguard-go", "-f", interface],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if userspace.poll() is not None:
                raise ValueError("Userspace WireGuard could not start")
            try:
                command("ip", "link", "show", interface)
                break
            except subprocess.CalledProcessError:
                time.sleep(0.1)
        else:
            userspace.kill()
            userspace.wait()
            raise ValueError("Userspace WireGuard interface timed out")
    current = Path("/run/relay.conf")
    descriptor = os.open(current, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(wireguard(value))
    command("wg", "setconf", interface, str(current))
    command("ip", "address", "add", value["address"], "dev", interface)
    command("ip", "link", "set", interface, "up")
    # Overlay transport and replies to locally published app ports retain the
    # physical route. Only app-initiated internet flows use the VPN table.
    command("ip", "rule", "del", "priority", "80", check=False)
    command("ip", "rule", "add", "priority", "80", "fwmark", "51879", "lookup", "main")
    if value["mode"] == "gateway":
        command("ip", "rule", "del", "priority", "81", check=False)
        command(
            "ip",
            "rule",
            "add",
            "priority",
            "81",
            "to",
            str(ipaddress.ip_interface(value["address"]).network),
            "lookup",
            "main",
        )
        if command("cat", "/proc/sys/net/ipv4/ip_forward") != "1":
            raise ValueError("Gateway forwarding must be enabled by its provider container")
    else:
        # The encrypted relay transport keeps a specific physical route; all
        # other internet traffic uses wg-hub, including public DNS queries.
        command("ip", "route", "replace", "default", "dev", interface, "table", "51879")
        command("ip", "rule", "del", "priority", "81", check=False)
        command(
            "ip",
            "rule",
            "add",
            "priority",
            "81",
            "to",
            value["endpointIp"] + "/32",
            "lookup",
            "main",
        )
        command("ip", "rule", "del", "priority", "82", check=False)
        command("ip", "rule", "add", "priority", "82", "lookup", "51879")
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("stop", True))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("stop", True))
    fingerprint = CONFIG.read_bytes()
    while not stop:
        time.sleep(1)
        if value["mode"] == "gateway":
            apply_firewall(value)
        incoming = CONFIG.read_bytes()
        if incoming != fingerprint:
            updated = validate(json.loads(incoming))
            # Only gateway peer membership is hot-reloaded. Client endpoint or
            # key changes require replacing the namespace and reattaching apps.
            if (
                updated["mode"] != "gateway"
                or value["mode"] != "gateway"
                or any(updated[k] != value[k] for k in ["privateKey", "address", "listenPort"])
            ):
                raise ValueError("Relay namespace replacement required")
            current.write_text(wireguard(updated))
            command("wg", "syncconf", interface, str(current))
            value, fingerprint = updated, incoming
    # Keep the namespace's DROP rules; stopping a relay never enables direct egress.
    command("ip", "link", "delete", interface, check=False)
    if userspace:
        userspace.terminate()
        try:
            userspace.wait(timeout=3)
        except subprocess.TimeoutExpired:
            userspace.kill()
            userspace.wait()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Configuration includes keys: neither exceptions nor values are logged.
        print("VPN relay could not be verified; internet traffic remains blocked", file=sys.stderr)
        sys.exit(1)
