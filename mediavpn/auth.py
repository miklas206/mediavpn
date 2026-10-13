import hashlib
import secrets
import time
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from mediavpn.appearance import install_theme_preferences, theme_override
from mediavpn.hub_auth import HubLogin

HASHER = PasswordHasher()
COOKIE = "mediavpn_session"


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=256)
    language: Literal["en", "da"] = "en"


def install_auth(app, database, base, hub_transport=None):
    hub = HubLogin(app, database, base, COOKIE, hub_transport)
    app.state.hub_login = hub

    async def authorized(request: Request):
        digest = hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest()
        with database() as connection:
            session = connection.execute("SELECT * FROM sessions WHERE digest=? AND expires>?", (digest, time.time())).fetchone()
        if not session:
            raise HTTPException(401, "Log ind for at fortsaette")
        with database() as connection:
            account = connection.execute("SELECT language FROM accounts LIMIT 1").fetchone()
        public = await hub.identity(digest) if hub.enabled else {"role": "administrator", "language": account[0] if account else "en"}
        if request.method not in {"GET", "HEAD"} and request.url.path not in {"/api/auth/logout", "/api/auth/preferences"} and public["role"] != "administrator":
            raise HTTPException(403, "App administrator access required")
        if request.method not in {"GET", "HEAD"} and not secrets.compare_digest(request.headers.get("X-MediaVPN-CSRF", ""), session["csrf"]):
            raise HTTPException(403, "Sessionen kunne ikke verificeres")
        return {**dict(session), **public}

    install_theme_preferences(app, database, authorized, hub.enabled)

    @app.get("/api/auth/status")
    async def status():
        if hub.enabled:
            return {"needsSetup": False, "managed": True, "loginUrl": await hub.origin() + "/auth/hub/start"}
        with database() as connection:
            return {"needsSetup": connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0}

    @app.post("/api/auth/setup")
    def setup(body: Credentials):
        if hub.enabled:
            raise HTTPException(403, "Users are managed in MediaHub")
        with database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
                raise HTTPException(409, "Administrator findes allerede")
            connection.execute("INSERT INTO accounts (username,password,language) VALUES (?,?,?)", (body.username, HASHER.hash(body.password), body.language))
        return {"created": True}

    @app.post("/api/auth/login")
    def login(body: Credentials, request: Request, response: Response):
        if hub.enabled:
            raise HTTPException(403, "Sign in through MediaHub")
        address = request.client.host if request.client else "local"
        now = time.time()
        with database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM attempts WHERE started<?", (now - 300,))
            attempt = connection.execute("SELECT count FROM attempts WHERE address=?", (address,)).fetchone()
            if attempt and attempt[0] >= 10:
                raise HTTPException(429, "For mange loginforsoeg. Proev igen om lidt.")
            connection.execute("INSERT INTO attempts VALUES (?,1,?) ON CONFLICT(address) DO UPDATE SET count=count+1", (address, now))
            account = connection.execute("SELECT password FROM accounts WHERE username=?", (body.username,)).fetchone()
        try:
            if not account or not HASHER.verify(account[0], body.password):
                raise HTTPException(401, "Brugernavn eller adgangskode er forkert")
        except VerificationError:
            raise HTTPException(401, "Brugernavn eller adgangskode er forkert") from None
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with database() as connection:
            connection.execute("DELETE FROM sessions WHERE expires<?", (now,))
            connection.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), csrf, now + 43200))
        response.set_cookie(COOKIE, token, httponly=True, secure=base.startswith("https:"), samesite="strict", max_age=43200)
        return {"csrf": csrf}

    from fastapi import Depends

    @app.get("/api/auth/me")
    def me(session=Depends(authorized)):
        return {"managed": hub.enabled, "themeOverride": theme_override(database, session.get("id")), **{key: session[key] for key in ("csrf", "id", "username", "role", "language", "appearance", "theme") if key in session}}

    @app.post("/api/auth/logout")
    def logout(response: Response, session=Depends(authorized)):
        with database() as connection:
            connection.execute("DELETE FROM sessions WHERE digest=?", (session["digest"],))
            if hub.enabled:
                connection.execute("DELETE FROM hub_sessions WHERE digest=?", (session["digest"],))
        response.delete_cookie(COOKIE)
        return {"signedOut": True}

    return authorized
