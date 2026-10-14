"""Traefik's only public entry is an explicit hostname from a Cloudflare socket peer."""

import hashlib
import ipaddress
import re
import ssl
from urllib.parse import urlsplit

CLOUDFLARE = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)


def hostname(value):
    name = value.strip().rstrip(".").encode("idna").decode("ascii").lower()
    if (
        len(name) > 253
        or "." not in name
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
            for part in name.split(".")
        )
    ):
        raise ValueError("Invalid hostname")
    return name


def routes(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise ValueError("Select between one and 32 installed apps")
    result, ids, names = [], set(), set()
    for row in value:
        ident = row["id"]
        if not isinstance(ident, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,120}", ident
        ):
            raise ValueError("Invalid app identity")
        name = hostname(row["hostname"])
        parsed = urlsplit(row["upstream"])
        address = ipaddress.ip_address(parsed.hostname or "")
        if (
            address.version != 4
            or not any(
                address in ipaddress.ip_network(net)
                for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            )
            or parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not 1
            <= (parsed.port or (443 if parsed.scheme == "https" else 80))
            <= 65535
            or ident in ids
            or name in names
        ):
            raise ValueError(
                "Choose unique installed apps with private HTTP(S) origins"
            )
        server = row.get("tlsServerName") or str(address)
        try:
            ipaddress.ip_address(server)
        except ValueError:
            server = hostname(server)
        ca = row.get("caPem") or ""
        if ca:
            if len(ca) > 16384 or "PRIVATE KEY" in ca:
                raise ValueError("Invalid certificate authority")
            ssl.create_default_context(cadata=ca)
        result.append(
            {
                "id": ident,
                "hostname": name,
                "upstream": row["upstream"].rstrip("/"),
                "tlsServerName": server,
                "caPem": ca,
            }
        )
        ids.add(ident)
        names.add(name)
    return result


def ca_name(pem):
    return "ca-" + hashlib.sha256(pem.encode()).hexdigest() + ".pem"


def traefik_config(targets, port, ranges=CLOUDFLARE):
    """Separate static and file-provider configs; never enable Docker discovery."""
    if not 1024 <= port <= 65535 or not ranges:
        raise ValueError("Invalid gateway port or trusted peers")
    targets = routes(targets)
    dynamic = {
        "http": {
            "routers": {},
            "services": {},
            "serversTransports": {},
            "middlewares": {
                "cloudflare-only": {"ipAllowList": {"sourceRange": list(ranges)}}
            },
        },
        "tls": {
            "options": {"default": {"sniStrict": True, "minVersion": "VersionTLS12"}}
        },
    }
    for row in targets:
        name = "app-" + hashlib.sha256(row["id"].encode()).hexdigest()[:24]
        headers = name + "-headers"
        tls_options = name + "-tls"
        # Distinct option identities make Traefik reject a different known
        # Host than TLS SNI with HTTP 421, even when both domains have certs.
        dynamic["tls"]["options"][tls_options] = {
            "sniStrict": True,
            "minVersion": "VersionTLS12",
        }
        dynamic["http"]["routers"][name] = {
            "entryPoints": ["gateway"],
            "rule": "Host(`" + row["hostname"] + "`)",
            "service": name,
            "middlewares": ["cloudflare-only", headers],
            "tls": {
                "options": tls_options,
                "certResolver": "gateway",
                "domains": [{"main": row["hostname"]}],
            },
        }
        dynamic["http"]["middlewares"][headers] = {
            "headers": {
                "customRequestHeaders": {
                    "X-Forwarded-Host": row["hostname"],
                    "X-Forwarded-Proto": "https",
                },
                "customResponseHeaders": {"X-MediaVPN-Route": row["id"]},
            }
        }
        service = {
            "loadBalancer": {
                "servers": [{"url": row["upstream"]}],
                "passHostHeader": True,
            }
        }
        if row["upstream"].startswith("https:"):
            transport = {
                "serverName": row["tlsServerName"],
                "insecureSkipVerify": False,
            }
            if row["caPem"]:
                transport["rootCAs"] = ["/config/" + ca_name(row["caPem"])]
            dynamic["http"]["serversTransports"][name] = transport
            service["loadBalancer"]["serversTransport"] = name
        dynamic["http"]["services"][name] = service
    static = {
        "global": {"checkNewVersion": False, "sendAnonymousUsage": False},
        "log": {"level": "ERROR"},
        "entryPoints": {
            "gateway": {
                "address": ":" + str(port),
                "forwardedHeaders": {"insecure": False},
            }
        },
        "providers": {"file": {"filename": "/state/routes.yaml", "watch": False}},
        "certificatesResolvers": {
            "gateway": {
                "acme": {
                    "storage": "/state/acme.json",
                    "keyType": "EC256",
                    "dnsChallenge": {
                        "provider": "cloudflare",
                        "resolvers": ["1.1.1.1:53", "8.8.8.8:53"],
                    },
                }
            }
        },
    }
    return static, dynamic
