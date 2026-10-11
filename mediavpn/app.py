import hashlib
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from mediavpn.auth import install_auth
from mediavpn.configuration import parse_wireguard
from mediavpn.runtime import Runtime

STATIC = Path(__file__).parent / "static"


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f]+$")
    configuration: str = Field(min_length=20, max_length=16384)


def create_app(data_dir=None, base_url=None, runtime=None):
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
    lock = threading.Lock()

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
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[origin.hostname])

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        return JSONResponse({"detail": "Kontroller felterne og proev igen. Konfigurationen er ikke gemt."}, status_code=422)

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin") not in {None, base}:
            return Response(status_code=403)
        if request.headers.get("content-length"):
            try:
                if int(request.headers["content-length"]) > 24000:
                    return Response(status_code=413)
            except ValueError:
                return Response(status_code=400)
        response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'"})
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    authorized = install_auth(app, database, base)

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
        return {"enabled": runtime.enabled, "mode": "docker" if runtime.enabled else "preview", "relayEnabled": False, "mediaSeedConnected": False}

    @app.get("/api/profiles")
    def listing(session=Depends(authorized)):
        with lock, database() as connection:
            rows = connection.execute("SELECT id,name,endpoint,port FROM profiles ORDER BY name").fetchall()
            return [{**dict(row), **runtime.status(row["id"])} for row in rows]

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
                connection.execute("INSERT INTO profiles VALUES (?,?,?,?,?)", (identifier, body.name.strip(), fingerprint, summary["endpoint"], summary["port"]))
            except Exception:
                path.unlink(missing_ok=True)
                raise
        return {"id": identifier, "name": body.name.strip(), "state": "stopped"}

    @app.post("/api/profiles/{identifier}/{action}")
    def action(identifier: str, action: str, session=Depends(authorized)):
        if action not in {"start", "stop"}:
            raise HTTPException(404, "Ukendt handling")
        with lock:
            find(identifier)
            if action == "start":
                runtime.start(identifier)
            else:
                runtime.stop(identifier)
            return runtime.status(identifier)

    @app.delete("/api/profiles/{identifier}")
    def remove(identifier: str, session=Depends(authorized)):
        with lock:
            find(identifier)
            runtime.stop(identifier, remove=True)
            with database() as connection:
                connection.execute("DELETE FROM profiles WHERE id=?", (identifier,))
            (profiles / f"{identifier}.conf").unlink(missing_ok=True)
        return {"removed": True}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app