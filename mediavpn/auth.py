import hashlib
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

HASHER = PasswordHasher()
COOKIE = "mediavpn_session"


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=256)


def install_auth(app, database, base):
    def authorized(request: Request):
        digest = hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest()
        with database() as connection:
            session = connection.execute("SELECT * FROM sessions WHERE digest=? AND expires>?", (digest, time.time())).fetchone()
        if not session:
            raise HTTPException(401, "Log ind for at fortsaette")
        if request.method not in {"GET", "HEAD"} and not secrets.compare_digest(request.headers.get("X-MediaVPN-CSRF", ""), session["csrf"]):
            raise HTTPException(403, "Sessionen kunne ikke verificeres")
        return dict(session)

    @app.get("/api/auth/status")
    def status():
        with database() as connection:
            return {"needsSetup": connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0}

    @app.post("/api/auth/setup")
    def setup(body: Credentials):
        with database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
                raise HTTPException(409, "Administrator findes allerede")
            connection.execute("INSERT INTO accounts VALUES (?,?)", (body.username, HASHER.hash(body.password)))
        return {"created": True}

    @app.post("/api/auth/login")
    def login(body: Credentials, request: Request, response: Response):
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
        return {"csrf": session["csrf"]}

    @app.post("/api/auth/logout")
    def logout(response: Response, session=Depends(authorized)):
        with database() as connection:
            connection.execute("DELETE FROM sessions WHERE digest=?", (session["digest"],))
        response.delete_cookie(COOKIE)
        return {"signedOut": True}

    return authorized