import hashlib
import logging
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager, asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from mediavpn.auth import install_auth
from mediavpn.configuration import parse_wireguard
from mediavpn.runtime import Runtime
from mediavpn.seedbox import Seedbox
from mediavpn.proton import ProtonSelectionError, ProtonSelector, selected_configuration

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
        settings = seedbox.settings()
        if runtime.enabled and settings["enabled"] and settings["profileId"] == identifier:
            seedbox.stop()

    @asynccontextmanager
    async def lifespan(app):
        stopping = threading.Event()
        def supervise():
            while not stopping.is_set():
                wakeup.clear()
                try:
                    with lock, database() as connection:
                        desired = connection.execute("SELECT id,port_forwarding,auto_country FROM profiles WHERE desired=1").fetchall()
                        for profile in desired:
                            start_profile(profile)
                    seedbox.reconcile()
                except Exception:
                    try:
                        seedbox.stop()
                    except Exception:
                        pass
                    seedbox.error = "VPN-supervisor kunne ikke verificere forbindelsen"
                wakeup.wait(5)
        worker = threading.Thread(target=supervise, name="mediavpn-supervisor", daemon=True) if runtime.enabled else None
        if worker:
            worker.start()
        yield
        stopping.set()
        wakeup.set()
        if worker:
            worker.join(timeout=20)
            try:
                seedbox.stop()
            except Exception:
                pass

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
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
                limit = 4 * 1024 * 1024 if request.url.path == "/api/integration/upload" else 24000
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

    def find(identifier):
        with database() as connection:
            row = connection.execute("SELECT * FROM profiles WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise HTTPException(404, "Profilen findes ikke")
        return row

    @app.get("/healthz")
    def health():
        return {"status": "healthy", "version": "0.1.0"}

    @app.get("/api/runtime")
    def runtime_status(session=Depends(authorized)):
        state = seedbox.status()
        return {"enabled": runtime.enabled, "mode": "docker" if runtime.enabled else "preview", "relayEnabled": False, "mediaSeedConnected": state["ready"], "seedbox": state}

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
    async def integration_upload(request: Request, ratio: float = Query(default=1, ge=1, le=1000, allow_inf_nan=False), savepath: str = Query(default="/downloads", pattern=r"^/downloads(?:/[A-Za-z0-9_-]+)*$", max_length=200), category: str = Query(default="", max_length=100), tags: str = Query(default="", max_length=64, pattern=r"^(?:MediaSeed-Cleanup-[a-f0-9]{24})?$"), start: bool = Query(default=False), session=Depends(machine_authorized)):
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
