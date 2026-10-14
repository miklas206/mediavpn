import hashlib
import ipaddress
import logging
import os
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from mediavpn.auth import install_auth
from mediavpn.configuration import parse_wireguard
from mediavpn.proton import ProtonSelectionError, ProtonSelector, selected_configuration
from mediavpn.runtime import Runtime
from mediavpn.seedbox import Seedbox
from mediavpn.shared_gateway import SharedGateway
from mediavpn.shared_gateway import save as save_private
from mediavpn.web_gateway import WebGateway, WebSettings, public_routes

STATIC = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f]+$")
    configuration: str = Field(min_length=20, max_length=16384)
    port_forwarding: bool = False


class Forwarding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class ServerSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    country: str | None = Field(default=None, min_length=1, max_length=60)


class SeedboxSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    profileId: str | None = Field(default=None, pattern=r"^[a-f0-9]{24}$")


class TorrentRpc(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(max_length=100)
    method: str = Field(pattern="^(GET|POST)$")
    payload: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=20)


class RelayPeer(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=100)
    publicKey: str = Field(min_length=44, max_length=44)


class GatewayCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    accountId: str = Field(pattern=r"^[a-fA-F0-9]{32}$")
    token: SecretStr = Field(min_length=20, max_length=512)


class HubGatewayRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: str | None = Field(default=None, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    packageId: str = Field(min_length=1, max_length=120)
    zoneId: str = Field(pattern=r"^[a-fA-F0-9]{32}$")
    label: str = Field(min_length=1, max_length=100)


class HubGatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    enabled: bool
    profileId: str | None = Field(default=None, pattern=r"^[a-f0-9]{24}$")
    routes: list[HubGatewayRoute] = Field(default_factory=list, max_length=32)


def create_app(data_dir=None, base_url=None, runtime=None, hub_transport=None, selector=None):
    data = Path(data_dir or os.environ.get("MEDIAVPN_DATA", "/data"))
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    data.chmod(0o700)
    profiles = data / "profiles"
    profiles.mkdir(mode=0o700, exist_ok=True)
    base = (base_url or os.environ.get("MEDIAVPN_BASE_URL", "http://127.0.0.1:18802")).rstrip("/")
    origin = urlsplit(base)
    if origin.scheme not in {"http", "https"} or not origin.hostname or origin.path or origin.username or origin.query or origin.fragment:
        raise ValueError("Expected an HTTP(S) origin")
    runtime = runtime or Runtime(data, os.environ.get("MEDIAVPN_RUNTIME") == "docker", os.environ.get("MEDIAVPN_HOST_DATA"))
    selector = selector or ProtonSelector()
    if isinstance(runtime, Runtime):
        runtime.selector = selector
    lock = threading.Lock()
    wakeup = threading.Event()

    @contextmanager
    def database():
        with sqlite3.connect(data / "mediavpn.sqlite", timeout=10) as connection:
            connection.row_factory = sqlite3.Row
            yield connection

    with database() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (username TEXT PRIMARY KEY, password TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (address TEXT PRIMARY KEY, count INTEGER NOT NULL, started REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, name TEXT NOT NULL, fingerprint TEXT UNIQUE NOT NULL, endpoint TEXT NOT NULL, port INTEGER NOT NULL);
        """)
        if "language" not in {row["name"] for row in connection.execute("PRAGMA table_info(accounts)")}:
            connection.execute("ALTER TABLE accounts ADD COLUMN language TEXT NOT NULL DEFAULT 'da'")
    (data / "mediavpn.sqlite").chmod(0o600)
    with database() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(profiles)")}
        if "port_forwarding" not in columns:
            connection.execute("ALTER TABLE profiles ADD COLUMN port_forwarding INTEGER NOT NULL DEFAULT 0")
        if "desired" not in columns:
            connection.execute("ALTER TABLE profiles ADD COLUMN desired INTEGER NOT NULL DEFAULT 0")
        if "auto_country" not in columns:
            connection.execute("ALTER TABLE profiles ADD COLUMN auto_country TEXT")
    seedbox = Seedbox(runtime, data)
    shared = SharedGateway(runtime, data, base)
    web = WebGateway(runtime, data)
    seedbox.web_gateway = web
    web.torrent_enabled = lambda: bool(seedbox.settings()["enabled"] and seedbox.settings()["profileId"] == web.settings()["profileId"])

    def start_profile(profile):
        with seedbox.lock:
            options = {"port_forwarding": bool(profile["port_forwarding"])}
            if profile["auto_country"]:
                options["auto_country"] = profile["auto_country"]
                container = runtime.container(profile["id"])
                if container:
                    container.reload()
                if not container or not container.attrs.get("State", {}).get("Running"):
                    stop_attached(profile["id"])
            runtime.start(profile["id"], **options)

    def stop_attached(identifier):
        if web.settings()["profileId"] == identifier:
            web.stop()
        if shared.profile() == identifier:
            shared.stop_helper()
        settings = seedbox.settings()
        if runtime.enabled and settings["enabled"] and settings["profileId"] == identifier:
            seedbox.stop()

    runtime.stop_dependents = stop_attached

    @asynccontextmanager
    async def lifespan(app):
        stopping = threading.Event()
        def supervise():
            while not stopping.is_set():
                wakeup.clear()
                try:
                    with lock, database() as connection:
                        if shared.profile():
                            for other in connection.execute("SELECT id FROM profiles WHERE id != ?", (shared.profile(),)):
                                candidate = runtime.container(other["id"])
                                if candidate:
                                    candidate.reload()
                                    if candidate.attrs.get("State", {}).get("Running"):
                                        runtime.stop(other["id"])
                        desired = connection.execute("SELECT id,port_forwarding,auto_country FROM profiles WHERE desired=1").fetchall()
                        for profile in desired:
                            start_profile(profile)
                    web.reconcile()
                    if stopping.is_set():
                        break
                    seedbox.reconcile()
                    shared.reconcile()
                except Exception:
                    try:
                        seedbox.stop()
                    except Exception:
                        pass
                    seedbox.error = "VPN-supervisor kunne ikke verificere forbindelsen"
                    try:
                        shared.stop_helper()
                    except Exception:
                        pass
                    try:
                        web.stop()
                    except Exception:
                        pass
                wakeup.wait(5)
        worker = threading.Thread(target=supervise, name="mediavpn-supervisor", daemon=True) if runtime.enabled else None
        if worker:
            worker.start()
        yield
        stopping.set()
        wakeup.set()
        try:
            web.close()
        except Exception:
            pass
        if worker:
            worker.join(timeout=20)
            try:
                seedbox.stop()
            except Exception:
                pass
            try:
                web.stop()
            except Exception:
                pass

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.shared_gateway = shared
    app.state.web_gateway = web
    if not os.environ.get("MEDIAVPN_HUB_URL"):
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=[origin.hostname])

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        return JSONResponse({"detail": "Kontroller felterne og proev igen. Konfigurationen er ikke gemt."}, status_code=422)

    @app.middleware("http")
    async def security(request: Request, call_next):
        origins = {base}
        hub = app.state.hub_login
        if hub.enabled and (request.url.hostname != origin.hostname or request.headers.get("origin") not in {None, base}):
            try:
                origins.add(await hub.origin())
            except HTTPException as error:
                return JSONResponse({"detail": error.detail}, status_code=error.status_code)
        if hub.enabled and request.url.hostname not in {urlsplit(value).hostname for value in origins}:
            return Response(status_code=400)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin") not in {None, *origins}:
            return Response(status_code=403)
        if request.headers.get("content-length"):
            try:
                limit = 4 * 1024 * 1024 if request.url.path == "/api/integration/upload" else 1024 * 1024 if request.url.path == "/api/integration/web-gateway" else 24000
                if int(request.headers["content-length"]) > limit:
                    return Response(status_code=413)
            except ValueError:
                return Response(status_code=400)
        response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'"})
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    authorized = install_auth(app, database, base, hub_transport)

    def machine_authorized(key: str = Header(default="", alias="X-MediaSeed-Key")):
        if not runtime.enabled or not key or not secrets.compare_digest(key, seedbox.token_path.read_text().strip()):
            raise HTTPException(401, "MediaSeed integration authentication required")

    def shared_machine_authorized(key: str = Header(default="", alias="X-MediaHub-Key")):
        if not runtime.enabled or not key or not secrets.compare_digest(key, shared.token_path.read_text().strip()):
            raise HTTPException(401, "MediaHub management authentication required")

    def gateway_machine_authorized(key: str = Header(default="", alias="X-MediaHub-Key")):
        # Permit authenticated prerequisite inspection in preview mode. Enabling
        # is separately blocked by the runtime preflight before any mutation.
        if not key or not secrets.compare_digest(key, shared.token_path.read_text().strip()):
            raise HTTPException(401, "MediaHub management authentication required")

    def find(identifier):
        with database() as connection:
            row = connection.execute("SELECT * FROM profiles WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise HTTPException(404, "Profilen findes ikke")
        return row

    @app.get("/healthz")
    def health():
        return {"status": "healthy", "version": "0.2.11"}

    @app.get("/api/runtime")
    def runtime_status(session=Depends(authorized)):
        state = seedbox.status()
        return {"enabled": runtime.enabled, "mode": "docker" if runtime.enabled else "preview", "relayEnabled": shared.public()["ready"], "mediaSeedConnected": state["ready"], "seedbox": state, "shared": shared.public()}

    def shared_overview():
        with database() as connection:
            profiles = [dict(row) for row in connection.execute("SELECT id,name FROM profiles ORDER BY name")]
        return {**shared.public(), "profiles": profiles}

    @app.get("/api/shared")
    def shared_status(session=Depends(authorized)):
        return shared_overview()

    @app.get("/api/hub-apps")
    async def hub_apps(session=Depends(authorized)):
        return await app.state.hub_login.vpn_apps(session["digest"])

    @app.put("/api/hub-apps/{app_id}")
    async def hub_app_choice(app_id: str, body: Forwarding, session=Depends(authorized)):
        if app_id not in {"mediaseed", "plex", "jellyfin"}:
            raise HTTPException(422, "Choose an installed MediaHub app")
        return await app.state.hub_login.vpn_apps(session["digest"], "apps/" + app_id, "PUT", body.model_dump())

    @app.put("/api/hub-gateway")
    async def hub_gateway_choice(body: SeedboxSettings, session=Depends(authorized)):
        return await app.state.hub_login.vpn_apps(session["digest"], "gateway", "PUT", body.model_dump())

    @app.get("/api/hub-web-gateway")
    async def hub_web_gateway(session=Depends(authorized)):
        return await app.state.hub_login.web_gateway(session["digest"])

    @app.put("/api/hub-web-gateway/credentials")
    async def hub_web_credentials(body: GatewayCredentials, session=Depends(authorized)):
        return await app.state.hub_login.web_gateway(session["digest"], "credentials", "PUT",
            {"accountId": body.accountId, "token": body.token.get_secret_value()})

    @app.put("/api/hub-web-gateway/config")
    async def hub_web_config(body: HubGatewayConfig, session=Depends(authorized)):
        return await app.state.hub_login.web_gateway(session["digest"], "config", "PUT", body.model_dump())

    @app.put("/api/hub-web-gateway/plex-port")
    async def hub_plex_port(body: Forwarding, session=Depends(authorized)):
        return await app.state.hub_login.web_gateway(session["digest"], "plex-port", "PUT", body.model_dump())

    @app.post("/api/hub-web-gateway/reconcile")
    async def hub_web_reconcile(session=Depends(authorized)):
        return await app.state.hub_login.web_gateway(session["digest"], "reconcile", "POST")

    @app.get("/api/integration/shared")
    def shared_machine_status(session=Depends(shared_machine_authorized)):
        return shared_overview()

    @app.get("/api/integration/web-gateway")
    def web_gateway_status(session=Depends(gateway_machine_authorized)):
        return web.public()

    @app.get("/api/integration/web-gateway/prerequisites")
    def web_gateway_prerequisites(session=Depends(gateway_machine_authorized)):
        return web.prerequisites()

    @app.put("/api/integration/web-gateway")
    def web_gateway_settings(body: WebSettings, session=Depends(gateway_machine_authorized)):
        with lock, seedbox.lock:
            routes = web.validate(body)
            if body.enabled:
                prerequisites = web.prerequisites(force=True)
                if not prerequisites["ready"]:
                    raise HTTPException(409, prerequisites["message"])
                if not find(body.profileId)["port_forwarding"]:
                    raise HTTPException(409, "Choose a Proton profile with NAT-PMP port forwarding enabled")
                shared.endpoint()
                _, metadata = parse_wireguard((profiles / f"{body.profileId}.conf").read_text())
                overlay = ipaddress.ip_network("10.250.87.0/24")
                if any(ipaddress.ip_interface(address).version == 4 and ipaddress.ip_interface(address).network.overlaps(overlay) for address in metadata["addresses"]):
                    raise HTTPException(409, "The provider profile overlaps the private app relay network")
                for row in routes:
                    target = ipaddress.ip_address(urlsplit(row["upstream"]).hostname)
                    if target in overlay or target in ipaddress.ip_network("10.2.0.0/24"):
                        raise HTTPException(409, "An app origin overlaps the provider or private relay network")
                current = seedbox.settings()
                if current["enabled"] and current["profileId"] != body.profileId:
                    seedbox.stop()
                    save_private(seedbox.path, {"enabled": True, "profileId": body.profileId})
                shared.configure(body.profileId, True)
                web.configure(body)
                with database() as connection:
                    connection.execute("UPDATE profiles SET desired=0 WHERE id != ?", (body.profileId,))
                    connection.execute("UPDATE profiles SET desired=1,port_forwarding=1 WHERE id=?", (body.profileId,))
            else:
                # Stop only inbound helpers; outgoing selected apps and their tunnel
                # remain configured. DNS removal is owned by MediaHub's journal.
                web.configure(body)
            wakeup.set()
        return {"enabled": body.enabled, "profileId": body.profileId, "ready": False,
                "providerReady": False, "phase": "applying", "message": "Gateway configuration saved; preparing the runtime",
                "publicIp": None, "publicPort": None,
                "routes": public_routes(routes), "plex": body.plex.model_dump() if body.plex and body.enabled else None,
                "checkedAt": time.time()}

    def configure_shared(body):
        with lock, seedbox.lock:
            if web.settings()["enabled"] and (not body.enabled or body.profileId != web.settings()["profileId"]):
                raise HTTPException(409, "Change or disable the web gateway before changing its shared VPN profile")
            if body.enabled:
                selected = find(body.profileId)
                _, metadata = parse_wireguard((profiles / f"{body.profileId}.conf").read_text())
                overlay = ipaddress.ip_network("10.250.87.0/24")
                if any(ipaddress.ip_interface(address).version == 4 and ipaddress.ip_interface(address).network.overlaps(overlay) for address in metadata["addresses"]):
                    raise HTTPException(409, "The VPN profile address overlaps the private app relay network")
                current = seedbox.settings()
                if current["enabled"] and current["profileId"] != body.profileId:
                    raise HTTPException(409, "Select the same profile for MediaSeed before sharing the VPN")
                with database() as connection:
                    others = connection.execute("SELECT id FROM profiles WHERE id != ?", (body.profileId,)).fetchall()
                    connection.execute("UPDATE profiles SET desired=0 WHERE id != ?", (body.profileId,))
                for row in others:
                    runtime.stop(row["id"])
                stop_attached(body.profileId)
                shared.configure(body.profileId, True)
                with database() as connection:
                    connection.execute("UPDATE profiles SET desired=1 WHERE id=?", (body.profileId,))
                start_profile(selected)
            else:
                previous = shared.profile()
                shared.configure(body.profileId, False)
                if previous:
                    stop_attached(previous)
                    runtime.stop(previous)
                    with database() as connection:
                        connection.execute("UPDATE profiles SET desired=0 WHERE id=?", (previous,))
            wakeup.set()
        return shared_overview()

    @app.put("/api/shared")
    def shared_settings(body: SeedboxSettings, session=Depends(authorized)):
        return configure_shared(body)

    @app.put("/api/integration/shared")
    def shared_machine_settings(body: SeedboxSettings, session=Depends(shared_machine_authorized)):
        return configure_shared(body)

    @app.post("/api/integration/shared/peers")
    def shared_peer(body: RelayPeer, session=Depends(shared_machine_authorized)):
        return shared.peer(body.id, body.publicKey, body.name)

    @app.delete("/api/integration/shared/peers/{identifier}")
    def shared_peer_remove(identifier: str, session=Depends(shared_machine_authorized)):
        return shared.remove_peer(identifier)

    def seedbox_overview():
        state = seedbox.status()
        with database() as connection:
            profiles = connection.execute("SELECT id,name,port_forwarding FROM profiles ORDER BY name").fetchall()
        return {**state, "profiles": [{**dict(profile), **runtime.status(profile["id"])} for profile in profiles]}

    @app.get("/api/seedbox")
    def seedbox_status(session=Depends(authorized)):
        return seedbox_overview()

    @app.get("/api/hub/overview")
    async def hub_overview(request: Request):
        hub = app.state.hub_login
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if not hub.enabled or scheme.lower() != "bearer" or not 40 <= len(token) <= 128:
            raise HTTPException(401, "MediaHub overview authorization required")
        identity = await hub.request("session?packageId=org.mediahub.mediavpn", token=token)
        if identity.get("scope") != "overview" or identity.get("role") not in {"administrator", "user", "viewer"}:
            raise HTTPException(403, "MediaHub overview access denied")
        # Docker health checks are synchronous; keep them off the ASGI event loop.
        import asyncio
        def snapshot():
            with database() as connection:
                rows = connection.execute("SELECT id,name,auto_country FROM profiles ORDER BY name LIMIT 100").fetchall()
            state = seedbox.status()
            public = []
            for profile in rows:
                observed = runtime.status(profile["id"])
                public.append({"id": profile["id"], "name": profile["name"], "state": observed.get("state", "unknown"), "healthy": bool(observed.get("healthy")), "country": observed.get("country"), "automatic": bool(profile["auto_country"])})
            consumers = [{"name": "MediaSeed", "profileId": state.get("profileId"), "ready": bool(state.get("ready"))}] if state.get("enabled") else []
            return {"checkedAt": time.time(), "enabled": runtime.enabled, "profiles": public, "consumers": consumers}
        return await asyncio.to_thread(snapshot)

    @app.get("/api/integration/status")
    def integration_status(session=Depends(machine_authorized)):
        return seedbox_overview()

    def configure_seedbox(body):
        if body.enabled:
            if shared.profile() and shared.profile() != body.profileId:
                raise HTTPException(409, "MediaSeed must use the selected shared VPN profile")
            profile = find(body.profileId)
            if not profile["port_forwarding"]:
                raise HTTPException(409, "Aktiver Proton port-forwarding paa profilen foerst")
        return seedbox.configure(body.profileId, body.enabled)

    @app.put("/api/seedbox")
    def seedbox_settings(body: SeedboxSettings, session=Depends(authorized)):
        return configure_seedbox(body)

    @app.put("/api/integration/settings")
    def integration_settings(body: SeedboxSettings, session=Depends(machine_authorized)):
        return configure_seedbox(body)

    @app.post("/api/integration/rpc")
    def integration_rpc(body: TorrentRpc, session=Depends(machine_authorized)):
        return seedbox.rpc(body.path, body.method, body.payload)

    @app.post("/api/integration/upload")
    async def integration_upload(request: Request, ratio: float = Query(default=1, ge=1, le=1000, allow_inf_nan=False), savepath: str = Query(default="/downloads", max_length=1100), category: str = Query(default="", max_length=100), tags: str = Query(default="", max_length=64, pattern=r"^(?:MediaSeed-Cleanup-[a-f0-9]{24})?$"), start: bool = Query(default=False), session=Depends(machine_authorized)):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 4 * 1024 * 1024:
                raise HTTPException(413, "Torrent file exceeds 4 MiB")
        paused = "false" if start else "true"
        return seedbox.rpc("torrents/add", "POST", {"stopped": paused, "paused": paused, "savepath": savepath, "ratioLimit": str(ratio), "seedingTimeLimit": "2940", "category": category, "tags": tags}, bytes(content))

    @app.get("/api/profiles")
    def listing(session=Depends(authorized)):
        with lock, database() as connection:
            rows = connection.execute("SELECT id,name,endpoint,port,port_forwarding,auto_country FROM profiles ORDER BY name").fetchall()
            return [{**dict(row), "port_forwarding": bool(row["port_forwarding"]), **runtime.status(row["id"])} for row in rows]

    @app.get("/api/proton/countries")
    def countries(session=Depends(authorized)):
        try:
            return {"countries": selector.countries()}
        except Exception:
            raise HTTPException(503, "Protons serverliste kunne ikke hentes. Prøv igen senere.") from None

    @app.put("/api/profiles/{identifier}/server-selection")
    def server_selection(identifier: str, body: ServerSelection, session=Depends(authorized)):
        with lock, seedbox.lock:
            profile = find(identifier)
            if body.country:
                if not profile["port_forwarding"]:
                    raise HTTPException(409, "Aktivér NAT-PMP/port-forwarding på Proton-profilen først")
                try:
                    if body.country != "*" and body.country not in selector.countries():
                        raise ValueError()
                    selected_configuration((profiles / f"{identifier}.conf").read_text(), selector.catalog()[0])
                except Exception:
                    raise HTTPException(422, "Land eller Proton-konfiguration kan ikke bruges til automatisk servervalg") from None
            reconnecting = bool(runtime.enabled and (profile["desired"] or runtime.status(identifier)["state"] in {"connected", "unverified", "starting"}))
            # Keep the user's running/stopped intent. The supervisor starts the
            # replacement and reconciles attached clients only after health and
            # NAT-PMP have supplied a verified port. Re-saving an active choice
            # deliberately measures/selects a new server too.
            if profile["auto_country"] != body.country or reconnecting:
                stop_attached(identifier)
                runtime.stop(identifier)
                with database() as connection:
                    connection.execute("UPDATE profiles SET auto_country=?,desired=? WHERE id=?", (body.country, int(reconnecting), identifier))
                wakeup.set()
            return {"saved": True, "auto_country": body.country, "restartRequired": False, "reconnecting": reconnecting}

    @app.get("/api/proton/recommendations")
    def recommendations(session=Depends(authorized)):
        try:
            return selector.recommendations()
        except ProtonSelectionError as error:
            logger.warning("Proton recommendations failed: %s", error)
            raise HTTPException(503, str(error)) from None
        except Exception as error:
            logger.warning("Proton recommendations failed unexpectedly (%s)", type(error).__name__)
            raise HTTPException(503, "Servermålingen kunne ikke gennemføres. Prøv igen senere.") from None

    @app.post("/api/proton/recommendations/refresh")
    def refresh_recommendations(session=Depends(authorized)):
        try:
            return selector.recommendations(force=True)
        except ProtonSelectionError as error:
            logger.warning("Proton recommendation refresh failed: %s", error)
            raise HTTPException(503, str(error)) from None
        except Exception as error:
            logger.warning("Proton recommendation refresh failed unexpectedly (%s)", type(error).__name__)
            raise HTTPException(503, "Servermålingen kunne ikke opdateres. Prøv igen senere.") from None

    @app.post("/api/profiles", status_code=201)
    def create(body: Profile, session=Depends(authorized)):
        if not body.name.strip():
            raise HTTPException(422, "Profilen skal have et navn")
        try:
            normalized, summary = parse_wireguard(body.configuration)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        private_line = next(line for line in normalized.splitlines() if line.startswith("PrivateKey"))
        fingerprint = hashlib.sha256(private_line.split("=", 1)[1].strip().encode()).hexdigest()
        identifier = secrets.token_hex(12)
        with lock, database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM profiles WHERE fingerprint=?", (fingerprint,)).fetchone():
                raise HTTPException(409, "Denne WireGuard-noegle findes allerede. Brug en ny konfiguration.")
            if connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] >= 20:
                raise HTTPException(409, "Der kan hoejst oprettes 20 profiler")
            path = profiles / f"{identifier}.conf"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "w") as output:
                    output.write(normalized)
                connection.execute("INSERT INTO profiles (id,name,fingerprint,endpoint,port,port_forwarding) VALUES (?,?,?,?,?,?)", (identifier, body.name.strip(), fingerprint, summary["endpoint"], summary["port"], int(body.port_forwarding)))
            except Exception:
                path.unlink(missing_ok=True)
                raise
        return {"id": identifier, "name": body.name.strip(), "state": "stopped"}

    @app.post("/api/profiles/{identifier}/{action}")
    def action(identifier: str, action: str, session=Depends(authorized)):
        if action not in {"start", "stop"}:
            raise HTTPException(404, "Ukendt handling")
        with lock, seedbox.lock:
            profile = find(identifier)
            if action == "start":
                start_profile(profile)
            else:
                stop_attached(identifier)
                runtime.stop(identifier)
            with database() as connection:
                connection.execute("UPDATE profiles SET desired=? WHERE id=?", (int(action == "start"), identifier))
            seedbox.reconcile()
            return runtime.status(identifier)

    @app.put("/api/profiles/{identifier}/forwarding")
    def forwarding(identifier: str, body: Forwarding, session=Depends(authorized)):
        with lock, seedbox.lock:
            if not body.enabled and web.settings()["enabled"] and web.settings()["profileId"] == identifier:
                raise HTTPException(409, "The web gateway requires Proton port forwarding")
            find(identifier)
            stop_attached(identifier)
            runtime.stop(identifier)
            with database() as connection:
                connection.execute("UPDATE profiles SET port_forwarding=?,desired=0,auto_country=CASE WHEN ?=0 THEN NULL ELSE auto_country END WHERE id=?", (int(body.enabled), int(body.enabled), identifier))
            seedbox.reconcile()
        return {"saved": True, "port_forwarding": body.enabled, "restartRequired": True}

    @app.delete("/api/profiles/{identifier}")
    def remove(identifier: str, session=Depends(authorized)):
        with lock, seedbox.lock:
            find(identifier)
            if shared.profile() == identifier:
                raise HTTPException(409, "Disable shared VPN before deleting its profile")
            stop_attached(identifier)
            runtime.stop(identifier, remove=True)
            with database() as connection:
                connection.execute("DELETE FROM profiles WHERE id=?", (identifier,))
            (profiles / f"{identifier}.conf").unlink(missing_ok=True)
            (profiles / f"{identifier}.auto.conf").unlink(missing_ok=True)
        return {"removed": True}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
