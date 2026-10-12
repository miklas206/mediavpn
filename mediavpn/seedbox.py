import base64
import json
import io
import os
import re
import secrets
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

CLIENT_OWNER = "org.mediahub.mediavpn.seedbox"
CLIENT_NAME = "mediavpn-mediaseed-client"
ALLOWED_READS = {"app/version", "app/preferences", "torrents/info", "torrents/categories", "transfer/info", "torrents/properties", "torrents/files", "torrents/trackers", "sync/torrentPeers", "rss/items", "rss/torrent"}
ALLOWED_WRITES = {"torrents/add", "torrents/start", "torrents/stop", "torrents/delete", "torrents/setShareLimits", "torrents/setFilePriority", "torrents/createCategory", "torrents/topPrio", "rss/addFeed", "rss/removeItem", "rss/refreshItem"}

RPC_CODE = """
import base64,http.cookiejar,json,pathlib,sys,urllib.request,urllib.parse,urllib.error
body=json.loads(base64.b64decode(sys.argv[1]))
endpoint='http://127.0.0.1:8080/api/v2/'
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def send(path,data=None,content_type='application/x-www-form-urlencoded'):
    request=urllib.request.Request(endpoint+path,data=data,headers={'Referer':'http://127.0.0.1:8080','Content-Type':content_type})
    with opener.open(request,timeout=8) as response:
        raw=response.read(8*1024*1024+1)
        if len(raw)>8*1024*1024:raise ValueError('Client response too large')
        return {'status':response.status,'body':base64.b64encode(raw).decode()}
try:
    password=pathlib.Path('/secrets/qbit-password').read_text().strip()
    send('auth/login',urllib.parse.urlencode({'username':'mediaseed','password':password}).encode())
    payload=body.get('payload') or {}
    if body['path']=='rss/torrent':
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args):return None
        fetcher=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with fetcher.open(payload['url'],timeout=8) as response:
            raw=response.read(4*1024*1024+1)
            if len(raw)>4*1024*1024 or not raw.startswith(b'd') or not raw.endswith(b'e'):raise ValueError('Invalid torrent')
            result={'status':response.status,'body':base64.b64encode(raw).decode()}
    elif body['method']=='GET':
        result=send(body['path']+('?' + urllib.parse.urlencode(payload) if payload else ''))
    elif body.get('file_path'):
        boundary='mediaseed-internal-upload'
        chunks=[]
        for key,value in payload.items():
            chunks.append(('--'+boundary+'\\r\\nContent-Disposition: form-data; name="'+key+'"\\r\\n\\r\\n'+str(value)+'\\r\\n').encode())
        chunks.append(('--'+boundary+'\\r\\nContent-Disposition: form-data; name="torrents"; filename="upload.torrent"\\r\\nContent-Type: application/x-bittorrent\\r\\n\\r\\n').encode()+pathlib.Path(body['file_path']).read_bytes()+b'\\r\\n')
        chunks.append(('--'+boundary+'--\\r\\n').encode())
        result=send(body['path'],b''.join(chunks),'multipart/form-data; boundary='+boundary)
    else:
        result=send(body['path'],urllib.parse.urlencode(payload).encode())
    print(json.dumps(result))
except urllib.error.HTTPError as error:
    print(json.dumps({'status':error.code,'body':''}))
except Exception:
    print(json.dumps({'status':503,'body':''}))
finally:
    if body.get('file_path'):pathlib.Path(body['file_path']).unlink(missing_ok=True)
"""

PREPARE_CODE = """
import configparser,sys,pathlib,shutil
path='/config/qBittorrent/qBittorrent.conf'
backup=pathlib.Path(path+'.pre-mediavpn')
if not backup.exists():shutil.copy2(path,backup)
parser=configparser.ConfigParser(interpolation=None,strict=False)
parser.optionxform=str
parser.read(path)
for section in ('Preferences','BitTorrent'):
    if not parser.has_section(section):parser.add_section(section)
parser['Preferences'].update({'WebUI\\\\Address':'127.0.0.1','WebUI\\\\ServerDomains':'127.0.0.1;localhost','WebUI\\\\LocalHostAuth':'true','WebUI\\\\CSRFProtection':'true','WebUI\\\\HostHeaderValidation':'true'})
parser['BitTorrent'].update({'Session\\\\Interface':'tun0','Session\\\\InterfaceName':'tun0','Session\\\\Port':sys.argv[1],'Session\\\\UPnP':'false','Session\\\\UseRandomPort':'false'})
with open(path,'w') as output:parser.write(output,space_around_delimiters=False)
"""


class Seedbox:
    def __init__(self, runtime, data):
        self.runtime = runtime
        self.data = Path(data)
        self.path = self.data / "seedbox.json"
        self.lock = threading.RLock()
        self.error = None
        self.token_path = self.data / "integrations" / "mediaseed.token"
        self.token_path.parent.mkdir(mode=0o700, exist_ok=True)
        if not self.token_path.exists():
            descriptor = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(secrets.token_urlsafe(48))

    def settings(self):
        if not self.path.exists():
            return {"enabled": False, "profileId": None}
        return json.loads(self.path.read_text())

    def configure(self, profile_id, enabled):
        if enabled and not re.fullmatch(r"[a-f0-9]{24}", profile_id or ""):
            raise HTTPException(422, "Vaelg en VPN-profil")
        with self.lock:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"enabled": enabled, "profileId": profile_id}))
            temporary.chmod(0o600)
            temporary.replace(self.path)
            self.reconcile()
        return self.status()

    def child(self):
        rows = self.runtime.docker().containers.list(all=True, filters={"label": f"{CLIENT_OWNER}=mediaseed"})
        if len(rows) > 1:
            raise ValueError("Multiple managed torrent clients")
        return rows[0] if rows else None

    def stop(self, remove=False):
        child = self.child()
        if child:
            child.reload()
            if child.attrs.get("State", {}).get("Running"):
                child.stop(timeout=10)
            if remove:
                child.remove(v=False, force=False)

    def reconcile(self):
        with self.lock:
            settings = self.settings()
            if not settings["enabled"]:
                if self.runtime.enabled:
                    self.stop(remove=True)
                self.error = None
                return
            try:
                observed = self.runtime.status(settings["profileId"])
                if not observed["healthy"] or not observed["forwardedPort"]:
                    self.stop()
                    self.error = "VPN eller tildelt port er ikke verificeret"
                    return
                vpn = self.runtime.container(settings["profileId"])
                vpn.reload()
                generation = vpn.id + ":" + vpn.attrs["State"]["StartedAt"]
                child = self.child()
                if child and child.labels.get("org.mediahub.mediavpn.generation") != generation:
                    child.stop(timeout=10)
                    child.remove()
                    child = None
                if child is None:
                    self.create(vpn, generation, observed["forwardedPort"])
                    child = self.child()
                child.reload()
                if not child.attrs.get("State", {}).get("Running"):
                    child.start()
                    self.error = "Torrentklienten starter i VPN-netvaerket"
                    return
                response = self.execute(child, "app/preferences", "GET", {})
                preferences = json.loads(base64.b64decode(response["body"]))
                if preferences.get("current_network_interface") != "tun0" or preferences.get("listen_port") != observed["forwardedPort"] or preferences.get("upnp") or preferences.get("max_active_downloads") != 8 or not preferences.get("queueing_enabled"):
                    self.execute(child, "app/setPreferences", "POST", {"json": json.dumps({"current_network_interface": "tun0", "listen_port": observed["forwardedPort"], "upnp": False, "random_port": False, "max_active_downloads": 8, "max_active_torrents": -1, "max_active_uploads": -1, "queueing_enabled": True, "rss_processing_enabled": True, "rss_auto_downloading_enabled": False})})
                    response = self.execute(child, "app/preferences", "GET", {})
                    preferences = json.loads(base64.b64decode(response["body"]))
                if preferences.get("current_network_interface") != "tun0" or preferences.get("listen_port") != observed["forwardedPort"] or preferences.get("upnp") or preferences.get("max_active_downloads") != 8 or not preferences.get("queueing_enabled") or not preferences.get("rss_processing_enabled") or preferences.get("rss_auto_downloading_enabled"):
                    raise ValueError("Torrent interface or port not bound")
                self.error = None
            except Exception:
                child = self.child()
                if child:
                    child.reload()
                    state = child.attrs.get("State", {})
                    try:
                        current = self.runtime.status(settings["profileId"])
                        current_vpn = self.runtime.container(settings["profileId"])
                        current_vpn.reload()
                        current_generation = current_vpn.id + ":" + current_vpn.attrs["State"]["StartedAt"]
                        age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00"))).total_seconds()
                        if current["healthy"] and current["forwardedPort"] and child.labels.get("org.mediahub.mediavpn.generation") == current_generation and state.get("Running") and 0 <= age < 30:
                            self.error = "Torrentklienten starter; status er ikke klar"
                            return
                    except Exception:
                        pass
                try:
                    self.stop()
                finally:
                    self.error = "Torrentklienten er stoppet; VPN-tilkoblingen er ikke klar"

    def create(self, vpn, generation, port):
        client = self.runtime.docker()
        source = client.containers.get("mediaseed-preview-qbittorrent-1")
        app = client.containers.get("mediaseed-preview-app-1")
        for container, service in ((source, "qbittorrent"), (app, "app")):
            if container.labels.get("com.docker.compose.project") != "mediaseed-preview" or container.labels.get("com.docker.compose.service") != service:
                raise ValueError("Unexpected MediaSeed installation")
            container.reload()
        volumes = {}
        for container, target, writable in ((source, "/config", True), (source, "/downloads", True), (app, "/secrets", False)):
            mount = next((row for row in container.attrs["Mounts"] if row["Destination"] == target), None)
            if not mount or mount.get("Type") != "volume" or not mount.get("Name", "").startswith("mediaseed-preview_"):
                raise ValueError("Unverified MediaSeed storage")
            volumes[mount["Name"]] = {"bind": target, "mode": "rw" if writable else "ro"}
        source.stop(timeout=15)
        resolver = self.data / "seedbox-resolv.conf"
        resolver.write_text("nameserver 127.0.0.1\noptions timeout:2 attempts:2\n")
        resolver.chmod(0o644)
        volumes[str(self.runtime.host_data / resolver.name)] = {"bind": "/etc/resolv.conf", "mode": "ro"}
        prep = client.containers.run(source.attrs["Image"], entrypoint=["python3", "-c", PREPARE_CODE, str(port)], user="1000:1000", volumes={name: value for name, value in volumes.items() if value["bind"] == "/config"}, network_mode="none", detach=True, labels={CLIENT_OWNER: "preparation"})
        try:
            if prep.wait(timeout=20).get("StatusCode") != 0:
                raise ValueError("Torrent preparation failed")
        finally:
            prep.remove(force=True)
        client.containers.create(source.attrs["Image"], name=CLIENT_NAME, environment={"PUID": "1000", "PGID": "1000", "WEBUI_PORT": "8080", "TZ": "Europe/Copenhagen"}, volumes=volumes, network_mode="container:" + vpn.id, labels={CLIENT_OWNER: "mediaseed", "org.mediahub.mediavpn.generation": generation}, restart_policy={"Name": "no"}, security_opt=["no-new-privileges:true"], log_config={"Type": "json-file", "Config": {"max-size": "5m", "max-file": "2"}})

    def status(self):
        with self.lock:
            settings = self.settings()
            observed = self.runtime.status(settings["profileId"]) if settings["enabled"] else {"healthy": False, "forwardedPort": None, "publicIp": None}
            child = self.child() if self.runtime.enabled else None
            running = False
            if child:
                child.reload()
                vpn = self.runtime.container(settings["profileId"]) if settings["enabled"] else None
                if vpn:
                    vpn.reload()
                    generation = vpn.id + ":" + vpn.attrs["State"]["StartedAt"]
                    running = child.attrs.get("State", {}).get("Running", False) and child.labels.get("org.mediahub.mediavpn.generation") == generation
            ready = bool(settings["enabled"] and observed["healthy"] and observed["forwardedPort"] and running and not self.error)
            if ready:
                try:
                    response = self.execute(child, "app/preferences", "GET", {})
                    preferences = json.loads(base64.b64decode(response["body"]))
                    ready = preferences.get("current_network_interface") == "tun0" and preferences.get("listen_port") == observed["forwardedPort"] and not preferences.get("upnp")
                except Exception:
                    ready = False
            return {**settings, "ready": ready, "downloadsEnabled": ready, "vpn": "verified" if ready else "unverified", "publicIp": observed["publicIp"], "forwardedPort": observed["forwardedPort"], "country": observed.get("country") if ready else None, "checkedAt": observed.get("checkedAt", time.time()), "reason": self.error or (None if ready else "Vaelg og start en VPN-profil med tildelt port")}

    @staticmethod
    def execute(child, path, method, payload, content=None):
        file_path = None
        if content is not None:
            if len(content) > 4 * 1024 * 1024:
                raise HTTPException(413, "Torrent file exceeds 4 MiB")
            filename = "mediaseed-" + secrets.token_hex(16) + ".torrent"
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as writer:
                entry = tarfile.TarInfo(filename)
                entry.size = len(content)
                entry.mode = 0o600
                entry.uid = entry.gid = 1000
                writer.addfile(entry, io.BytesIO(content))
            if not child.put_archive("/tmp", archive.getvalue()):
                raise HTTPException(503, "Torrent upload unavailable")
            file_path = "/tmp/" + filename
        body = {"path": path, "method": method, "payload": payload, "file_path": file_path}
        encoded = base64.b64encode(json.dumps(body).encode()).decode()
        result = child.exec_run(["python3", "-c", RPC_CODE, encoded], user="1000:1000")
        if result.exit_code or len(result.output) > 12 * 1024 * 1024:
            raise HTTPException(503, "Torrent client unavailable")
        response = json.loads(result.output)
        if response.get("status", 503) >= 400:
            raise HTTPException(422 if response["status"] == 415 else 503, "Torrent client rejected the request")
        return response

    def rpc(self, path, method, payload, content=None):
        if path not in (ALLOWED_READS if method == "GET" else ALLOWED_WRITES):
            raise HTTPException(403, "Torrent operation not allowed")
        if len(json.dumps(payload)) > 12000:
            raise HTTPException(413, "Torrent request too large")
        if path.startswith("rss/"):
            from urllib.parse import urlsplit
            allowed = {"rss/items": {"withData"}, "rss/torrent": {"url"}, "rss/addFeed": {"url", "path"}, "rss/removeItem": {"path"}, "rss/refreshItem": {"itemPath"}}
            if not set(payload).issubset(allowed[path]):
                raise HTTPException(422, "Unsupported RSS fields")
            if "url" in payload:
                address = urlsplit(str(payload["url"]))
                if address.scheme != "https" or address.netloc != "nordicbytes.org" or address.fragment:
                    raise HTTPException(422, "Only NordicBytes HTTPS RSS sources are enabled")
                if path == "rss/torrent" and not address.path.startswith("/torrents/download/"):
                    raise HTTPException(422, "Invalid RSS torrent address")
            item_path = payload.get("path", payload.get("itemPath"))
            if item_path is not None and not re.fullmatch(r"MediaSeed-RSS-[a-f0-9]{24}", str(item_path)):
                raise HTTPException(422, "Invalid RSS feed path")
        if method == "POST":
            fields = {
                "torrents/add": {"urls", "stopped", "paused", "savepath", "ratioLimit", "seedingTimeLimit", "category"},
                "torrents/start": {"hashes"}, "torrents/stop": {"hashes"},
                "torrents/delete": {"hashes", "deleteFiles"},
                "torrents/setShareLimits": {"hashes", "ratioLimit", "seedingTimeLimit", "inactiveSeedingTimeLimit"},
                "torrents/setFilePriority": {"hash", "id", "priority"},
                "rss/addFeed": {"url", "path"}, "rss/removeItem": {"path"}, "rss/refreshItem": {"itemPath"},
                "torrents/createCategory": {"category", "savePath"}, "torrents/topPrio": {"hashes"},
            }
            if not set(payload).issubset(fields[path]):
                raise HTTPException(422, "Unsupported torrent fields")
            if path == "torrents/createCategory":
                if not re.fullmatch(r"MediaSeed-RSS-[a-f0-9]{24}", str(payload.get("category", ""))) or not re.fullmatch(r"/downloads(?:/[A-Za-z0-9_-]+)*", str(payload.get("savePath", ""))):
                    raise HTTPException(422, "Invalid RSS category")
            elif not path.startswith("rss/") and path != "torrents/add":
                identifier = str(payload.get("hashes", payload.get("hash", "")))
                if not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", identifier):
                    raise HTTPException(422, "Invalid torrent hash")
            elif path == "torrents/add":
                if payload.get("stopped") != "true" or payload.get("paused") != "true":
                    raise HTTPException(422, "New torrents must be stopped")
                if not re.fullmatch(r"/downloads(?:/[A-Za-z0-9_-]+)*", str(payload.get("savepath", ""))):
                    raise HTTPException(422, "Invalid download path")
                if content is None and not str(payload.get("urls", "")).startswith("magnet:?"):
                    raise HTTPException(422, "Expected a magnet or torrent file")
            if "ratioLimit" in payload:
                try:
                    if not 1 <= float(payload["ratioLimit"]) <= 1000 or str(payload.get("seedingTimeLimit")) != "2940":
                        raise ValueError()
                except (ValueError, TypeError):
                    raise HTTPException(422, "Invalid seeding policy") from None
        with self.lock:
            self.reconcile()
            if not self.status()["ready"]:
                raise HTTPException(409, "VPN is not verified; torrent operation blocked")
            if path == "torrents/delete" and payload.get("deleteFiles", "false") not in {"false", "true"}:
                raise HTTPException(422, "Choose whether download files should be deleted")
            return self.execute(self.child(), path, method, payload, content)
