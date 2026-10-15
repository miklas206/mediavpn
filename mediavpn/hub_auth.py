import base64
import hashlib
import ipaddress
import os
import secrets
import ssl
import time
from urllib.parse import urlencode, urlsplit

from mediavpn.http_pool import HttpPool

import httpx
from fastapi import HTTPException, Query, Request
from fastapi.responses import RedirectResponse

PACKAGE = "org.mediahub.mediavpn"


class HubLogin:
    def __init__(self, app, database, base, cookie, transport=None):
        self.database, self.base, self.cookie = database, base, cookie
        self.hub = os.environ.get("MEDIAVPN_HUB_URL", "").rstrip("/")
        self.browser_hub = os.environ.get("MEDIAVPN_HUB_BROWSER_URL", self.hub).rstrip("/")
        self.enabled = bool(self.hub)
        self.transport = transport
        self.pool = None
        self.verify = True
        if not self.enabled:
            return
        for origin in (self.hub, self.browser_hub, base):
            parsed = urlsplit(origin)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise ValueError("Managed SSO requires HTTPS hub and app origins")
        ca = os.environ.get("MEDIAVPN_HUB_CA_FILE")
        if ca:
            self.verify = ssl.create_default_context(cafile=ca)
        self.pool = HttpPool(base_url=self.hub, verify=self.verify, timeout=5, follow_redirects=False, trust_env=False, transport=self.transport)
        with database() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS hub_attempts (digest TEXT PRIMARY KEY, verifier TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_sessions (digest TEXT PRIMARY KEY, token TEXT NOT NULL, user_id TEXT NOT NULL);
            """)

        @app.get("/auth/hub/start")
        async def start(request: Request):
            origin = await self.origin(request)
            if request.headers.get("host", "").lower() != urlsplit(origin).netloc.lower():
                return RedirectResponse(origin + "/auth/hub/start", status_code=303, headers={"Cache-Control": "no-store"})
            state, verifier = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            with database() as db:
                db.execute("DELETE FROM hub_attempts WHERE expires<=?", (time.time(),))
                if db.execute("SELECT COUNT(*) FROM hub_attempts").fetchone()[0] >= 1000:
                    raise HTTPException(429, "Try again later")
                db.execute("INSERT INTO hub_attempts VALUES (?,?,?)", (hashlib.sha256(state.encode()).hexdigest(), verifier, time.time() + 300))
            try:
                local = origin == self.base and ipaddress.ip_address(urlsplit(origin).hostname).is_private
            except ValueError:
                local = False
            browser_hub = self.hub if local else self.browser_hub
            response = RedirectResponse(browser_hub + "/api/v1/sso/authorize?" + urlencode({"packageId": PACKAGE, "state": state, "challenge": challenge, "appOrigin": origin}), status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
            response.set_cookie("mediavpn_sso_state", state, httponly=True, secure=True, samesite="lax", max_age=300, path="/auth/hub")
            return response

        @app.get("/auth/hub/callback")
        async def callback(request: Request, code: str = Query(min_length=40, max_length=128), state: str = Query(min_length=43, max_length=128)):
            if not secrets.compare_digest(state, request.cookies.get("mediavpn_sso_state", "")):
                raise HTTPException(403, "Login verification failed")
            with database() as db:
                attempt = db.execute("DELETE FROM hub_attempts WHERE digest=? AND expires>? RETURNING verifier", (hashlib.sha256(state.encode()).hexdigest(), time.time())).fetchone()
            if not attempt:
                raise HTTPException(401, "Login attempt expired")
            identity = await self.request("exchange", "POST", {"packageId": PACKAGE, "code": code, "verifier": attempt["verifier"]})
            public = identity.get("user", {})
            if not isinstance(identity.get("token"), str) or not isinstance(identity.get("expires"), int) or not isinstance(public.get("id"), str) or public.get("role") not in {"administrator", "user", "viewer"}:
                raise HTTPException(503, "Invalid MediaHub identity")
            token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode()).hexdigest()
            with database() as db:
                db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
                db.execute("DELETE FROM hub_sessions WHERE digest NOT IN (SELECT digest FROM sessions)")
                db.execute("INSERT INTO sessions VALUES (?,?,?)", (digest, csrf, min(identity["expires"], time.time() + 43200)))
                db.execute("INSERT INTO hub_sessions VALUES (?,?,?)", (digest, identity["token"], public["id"]))
            response = RedirectResponse("/", status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
            response.set_cookie(cookie, token, httponly=True, secure=True, samesite="strict", max_age=43200)
            response.delete_cookie("mediavpn_sso_state", path="/auth/hub")
            return response

    async def origin(self, request=None):
        if request is not None and request.headers.get("host", "").lower() == urlsplit(self.base).netloc.lower():
            return self.base
        value = (await self.request("origin?packageId=" + PACKAGE)).get("origin", "")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise HTTPException(503, "Invalid MediaHub app origin")
        return value

    async def request(self, path, method="GET", payload=None, token=None, timeout=5, coalesce=False):
        try:
            response = await self.pool.request(method, "/api/v1/sso/" + path, coalesce=coalesce, json=payload, headers={"Authorization": "Bearer " + token} if token else {}, timeout=timeout)
            if response.status_code in {401, 403}:
                raise HTTPException(response.status_code, "MediaHub access denied")
            if path.startswith("vpn-gateway") and response.status_code in {400, 409, 422, 429}:
                detail = response.json().get("error", {}).get("message")
                if isinstance(detail, str) and 0 < len(detail) <= 500:
                    raise HTTPException(response.status_code, detail)
            response.raise_for_status()
            return response.json()["data"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(503, "MediaHub authentication unavailable") from None

    async def vpn_apps(self, digest, path="apps", method="GET", payload=None):
        if not self.enabled:
            raise HTTPException(409, "Connect MediaVPN to MediaHub to select installed apps")
        with self.database() as db:
            managed = db.execute("SELECT token FROM hub_sessions WHERE digest=?", (digest,)).fetchone()
        if not managed:
            raise HTTPException(401, "Sign in through MediaHub")
        return await self.request("vpn/" + path, method, payload, managed["token"], timeout=180)

    async def identity(self, digest, fresh=False):
        with self.database() as db:
            managed = db.execute("SELECT * FROM hub_sessions WHERE digest=?", (digest,)).fetchone()
        if not managed:
            raise HTTPException(401, "Sign in through MediaHub")
        public = await self.request("session?packageId=" + PACKAGE, token=managed["token"], coalesce=not fresh)
        if public.get("id") != managed["user_id"] or public.get("role") not in {"administrator", "user", "viewer"}:
            raise HTTPException(403, "MediaHub access denied")
        return public

    async def web_gateway(self, digest, path="", method="GET", payload=None):
        if not self.enabled:
            raise HTTPException(409, "Install MediaVPN through MediaHub to use guided app domains")
        with self.database() as db:
            managed = db.execute("SELECT token FROM hub_sessions WHERE digest=?", (digest,)).fetchone()
        if not managed:
            raise HTTPException(401, "Sign in through MediaHub")
        suffix = "/" + path.lstrip("/") if path else ""
        return await self.request("vpn-gateway" + suffix, method, payload, managed["token"], timeout=180)
