"""Run the VPN-local Traefik only for a fresh, matching provider generation."""

import base64
import json
import os
import signal
import socket
import ssl
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

try:
    from .config import traefik_config
except ImportError:
    from config import traefik_config


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def fresh(value, now=None):
    return (
        value.get("healthy") is True
        and 0 <= (time.time() if now is None else now) - value.get("checkedAt", 0) < 30
        and type(value.get("publicPort")) is int
        and 1024 <= value["publicPort"] <= 65535
    )


def acme_certificate(host, storage=Path("/state/acme.json"), now=None):
    """Inspect public certificate bytes only; account/certificate keys never escape."""
    if storage.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Oversized certificate store")
    data = json.loads(storage.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("gateway", {}), dict):
        raise TypeError("Invalid certificate store")
    certificates = data.get("gateway", {}).get("Certificates", [])
    if not isinstance(certificates, list):
        raise TypeError("Invalid certificate list")
    instant = now or datetime.now(UTC)
    for row in certificates:
        if not isinstance(row, dict) or not isinstance(row.get("domain", {}), dict):
            raise TypeError("Invalid certificate metadata")
        domain = row.get("domain", {})
        if host not in [domain.get("main"), *(domain.get("sans") or [])]:
            continue
        pem = base64.b64decode(row["certificate"], validate=True)
        cert = x509.load_pem_x509_certificate(pem)
        names = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
        if (
            host in names
            and cert.not_valid_before_utc <= instant
            and cert.not_valid_after_utc > instant + timedelta(minutes=5)
        ):
            return cert.public_bytes(serialization.Encoding.DER)
    return None


def certificate_ready(host, port):
    expected = acme_certificate(host)
    if not expected:
        return False
    context = ssl.create_default_context()
    with (
        socket.create_connection(("127.0.0.1", port), timeout=2) as conn,
        context.wrap_socket(conn, server_hostname=host) as secured,
    ):
        # Confirm the proxy loaded this unexpired certificate and that the real
        # served chain validates against public trust for this exact hostname.
        return secured.getpeercert(binary_form=True) == expected


def stop_process(process):
    if process:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main():
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    process, current = None, None
    output = Path("/state/status.json")
    try:
        while not stop.is_set():
            report = {
                "ready": False,
                "phase": "waiting-vpn",
                "routes": [],
                "checkedAt": time.time(),
            }
            try:
                value = json.loads(Path("/config/settings.json").read_text())
                if not fresh(value):
                    raise ValueError("Stale provider")
                signature = (
                    value["generation"],
                    value["revision"],
                    value["publicIp"],
                    value["publicPort"],
                )
                if (
                    signature != current
                    or process is None
                    or process.poll() is not None
                ):
                    stop_process(process)
                    process = None
                    static, dynamic = traefik_config(
                        value["routes"], value["publicPort"]
                    )
                    # JSON is a YAML subset. Traefik selects its YAML parser by
                    # the suffix; serialization cannot interpolate rule syntax.
                    write(Path("/state/traefik.yaml"), static)
                    write(Path("/state/routes.yaml"), dynamic)
                    acme = Path("/state/acme.json")
                    try:
                        descriptor = os.open(
                            acme, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                        )
                        with os.fdopen(descriptor, "w") as stream:
                            stream.write("{}")
                    except FileExistsError:
                        acme.chmod(0o600)
                    # Do not expose ACME request bodies or token expansion in logs.
                    process = subprocess.Popen(
                        ["traefik", "--configFile=/state/traefik.yaml"],
                        env={
                            **os.environ,
                            "CF_DNS_API_TOKEN_FILE": "/config/cloudflare-token",
                            "CF_ZONE_API_TOKEN_FILE": "/config/cloudflare-token",
                        },
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    current = signature

                def check(row, port=value["publicPort"]):
                    ready = False
                    try:
                        ready = certificate_ready(row["hostname"], port)
                    except (
                        OSError,
                        ValueError,
                        KeyError,
                        TypeError,
                        x509.ExtensionNotFound,
                    ):
                        pass
                    return {
                        "id": row["id"],
                        "hostname": row["hostname"],
                        "certificateReady": ready,
                    }

                with ThreadPoolExecutor(max_workers=8) as pool:
                    checked = list(pool.map(check, value["routes"]))
                report.update(
                    generation=value["generation"],
                    revision=value["revision"],
                    publicIp=value["publicIp"],
                    publicPort=value["publicPort"],
                    routes=checked,
                    ready=bool(
                        checked
                        and all(r["certificateReady"] for r in checked)
                        and process.poll() is None
                    ),
                    phase="serving" if process.poll() is None else "proxy-failed",
                )
            except (OSError, ValueError, KeyError, TypeError):
                stop_process(process)
                process, current = None, None
            report["checkedAt"] = time.time()
            write(output, report)
            stop.wait(2)
    finally:
        stop_process(process)
        write(output, {"ready": False, "phase": "stopped", "checkedAt": time.time()})


if __name__ == "__main__":
    if os.environ.get("GATEWAY_HEALTH") == "1":
        report = json.loads(Path("/state/status.json").read_text())
        raise SystemExit(
            0 if report.get("ready") and time.time() - report["checkedAt"] < 30 else 1
        )
    main()
