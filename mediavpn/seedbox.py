import base64
import hashlib
import io
import json
import os
import re
import secrets
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

from mediavpn.diagnostics import container_fields, emit, error_fields, trace

CLIENT_OWNER = "org.mediahub.mediavpn.seedbox"
CLIENT_NAME = "mediavpn-mediaseed-client"
ALLOWED_READS = {"app/version", "app/preferences", "torrents/info", "torrents/categories", "transfer/info", "torrents/properties", "torrents/files", "torrents/trackers", "sync/torrentPeers", "rss/items", "rss/torrent"}
ALLOWED_WRITES = {"torrents/add", "torrents/start", "torrents/stop", "torrents/recheck", "torrents/delete", "torrents/setShareLimits", "torrents/setFilePriority", "torrents/createCategory", "torrents/topPrio", "rss/addFeed", "rss/removeItem", "rss/refreshItem"}

RPC_CODE = """
import base64,http.cookiejar,json,pathlib,re,sys,urllib.request,urllib.parse,urllib.error
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
    destination=payload.get('savepath',payload.get('savePath'))
    if destination:
        target=pathlib.Path(destination)
        if target.resolve()!=target:raise ValueError('Download directory contains symlinks')
    if body['path']=='rss/torrent':
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args):return None
        fetcher=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        version=base64.b64decode(send('app/version')['body']).decode().strip().removeprefix('v')
        if not re.fullmatch(r'[0-9]+[.][0-9]+[.][0-9]+[A-Za-z0-9.-]{0,32}',version):raise ValueError('Invalid client version')
        request=urllib.request.Request(payload['url'],headers={'User-Agent':'qBittorrent/'+version})
        with fetcher.open(request,timeout=8) as response:
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
parser['BitTorrent']['Session\\\\AnnouncePort']=sys.argv[2] if len(sys.argv)>2 else '0'
with open(path,'w') as output:parser.write(output,space_around_delimiters=False)
"""


class Seedbox:
    def __init__(self, runtime, data):
        self.runtime = runtime
        self.data = Path(data)
        self.path = self.data / "seedbox.json"
        self.lock = threading.RLock()
        self.error = None
        self.web_gateway = None
        self.seed_project = os.environ.get("MEDIAVPN_MEDIASEED_PROJECT", "mediaseed-preview")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,80}", self.seed_project):
            raise ValueError("Invalid MediaSeed Compose project")
        self.media_storage = json.loads(os.environ.get("MEDIAVPN_MEDIASEED_STORAGE", "[]"))
        if not isinstance(self.media_storage, list) or len(self.media_storage) > 32:
            raise ValueError("Invalid approved MediaSeed storage")
        for row in self.media_storage:
            path = PurePosixPath(row["source"])
            if (not re.fullmatch(r"[A-Za-z0-9-]{1,64}", row["id"]) or row["path"] != "/downloads/media/" + row["id"]
                    or not path.is_absolute() or len(path.parts) < 3 or ".." in path.parts):
                raise ValueError("Invalid approved MediaSeed storage")
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

    def observed(self, profile):
        value = self.runtime.status(profile)
        lease = self.web_gateway.torrent(profile, value) if self.web_gateway else None
        if lease is not None:
            return {**value, "forwardedPort": lease.get("internalPort") if lease.get("ready") else None,
                    "publicTorrentPort": lease.get("publicPort") if lease.get("ready") else None,
                    "webGateway": True}
        return value

    def configure(self, profile_id, enabled):
        if enabled and not re.fullmatch(r"[a-f0-9]{24}", profile_id or ""):
            raise HTTPException(422, "Vaelg en VPN-profil")
        with self.lock:
            temporary = self.path.with_suffix(".tmp")
            settings = {**self.settings(), "enabled": enabled, "profileId": profile_id}
            temporary.write_text(json.dumps(settings))
            temporary.chmod(0o600)
            temporary.replace(self.path)
            self.reconcile()
        return self.status()

    def child(self):
        rows = self.runtime.docker().containers.list(all=True, filters={"label": f"{CLIENT_OWNER}=mediaseed"})
        if len(rows) > 1:
            raise ValueError("Multiple managed torrent clients")
        return rows[0] if rows else None

    def download_limit(self):
        value = self.settings().get("maxActiveDownloads", 8)
        if type(value) is not int or not 1 <= value <= 100:
            raise ValueError("Invalid saved download limit")
        return value

    def rss_interval(self):
        value = self.settings().get("rssRefreshMinutes", 5)
        if type(value) is not int or not 1 <= value <= 1440:
            raise ValueError("Invalid saved RSS interval")
        return value

    def rss_settings(self, minutes=None):
        with self.lock:
            self.reconcile()
            if not self.status()["ready"]:
                raise HTTPException(409, "Connect a verified MediaVPN torrent client before changing RSS settings")
            child = self.child()
            if minutes is not None:
                if type(minutes) is not int or not 1 <= minutes <= 1440:
                    raise HTTPException(422, "Choose an RSS interval between 1 and 1440 minutes")
                settings = {**self.settings(), "rssRefreshMinutes": minutes}
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(settings))
                temporary.chmod(0o600)
                temporary.replace(self.path)
                self.execute(child, "app/setPreferences", "POST", {"json": json.dumps({"rss_refresh_interval": minutes})})
            result = self.execute(child, "app/preferences", "GET", {})
            try:
                applied = json.loads(base64.b64decode(result["body"]))["rss_refresh_interval"]
                if type(applied) is not int or applied != self.rss_interval():
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise HTTPException(503, "The saved RSS interval could not be verified. Refresh after reconnecting the torrent client.") from None
            return {"rssRefreshMinutes": applied}

    def queue_settings(self, limit=None):
        with self.lock:
            self.reconcile()
            if not self.status()["ready"]:
                raise HTTPException(409, "Connect a verified MediaVPN torrent client before changing download settings")
            child = self.child()
            if limit is not None:
                if type(limit) is not int or not 1 <= limit <= 100:
                    raise HTTPException(422, "Choose between 1 and 100 simultaneous downloads")
                settings = {**self.settings(), "maxActiveDownloads": limit}
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(settings))
                temporary.chmod(0o600)
                temporary.replace(self.path)
                self.execute(child, "app/setPreferences", "POST", {"json": json.dumps({"max_active_downloads": limit, "queueing_enabled": True})})
            result = self.execute(child, "app/preferences", "GET", {})
            try:
                preferences = json.loads(base64.b64decode(result["body"]))
                applied = preferences["max_active_downloads"]
                if preferences.get("queueing_enabled") is not True or type(applied) is not int or applied != self.download_limit():
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise HTTPException(503, "The saved download limit could not be verified. Refresh after reconnecting the torrent client.") from None
            return {"maxActiveDownloads": applied, "queueingEnabled": True}

    def stop(self, remove=False):
        child = self.child()
        if child:
            child.reload()
            if child.attrs.get("State", {}).get("Running"):
                emit(self.runtime, "torrent.stop_requested", **container_fields(child))
                child.stop(timeout=10)
            if remove:
                emit(self.runtime, "torrent.remove_requested", **container_fields(child))
                child.remove(v=False, force=False)

    @trace("supervisor", "torrent_reconcile")
    def reconcile(self):
        with self.lock:
            settings = self.settings()
            if not settings["enabled"]:
                if self.runtime.enabled:
                    self.stop(remove=True)
                self.error = None
                return
            try:
                observed = self.observed(settings["profileId"])
                if not observed["healthy"] or not observed["forwardedPort"]:
                    emit(self.runtime, "torrent.blocked", reason="provider_or_forwarded_port_unverified", profile=settings["profileId"],
                         health=observed.get("state"), port=observed.get("forwardedPort"))
                    self.stop()
                    self.error = "VPN eller tildelt port er ikke verificeret"
                    return
                vpn = self.runtime.container(settings["profileId"])
                vpn.reload()
                generation = self.generation(vpn, observed)
                child = self.child()
                if child and child.labels.get("org.mediahub.mediavpn.generation") != generation:
                    emit(self.runtime, "torrent.recreate_requested", reason="provider_generation_changed",
                         previousGeneration=child.labels.get("org.mediahub.mediavpn.generation"), **container_fields(child))
                    child.stop(timeout=10)
                    child.remove()
                    child = None
                if child is None:
                    emit(self.runtime, "torrent.create_requested", reason="desired_client_missing", port=observed["forwardedPort"])
                    self.create(vpn, generation, observed["forwardedPort"])
                    child = self.child()
                child.reload()
                if not child.attrs.get("State", {}).get("Running"):
                    emit(self.runtime, "torrent.start_requested", reason="verified_client_stopped", **container_fields(child))
                    child.start()
                    self.error = "Torrentklienten starter i VPN-netvaerket"
                    return
                response = self.execute(child, "app/preferences", "GET", {})
                preferences = json.loads(base64.b64decode(response["body"]))
                if observed.get("webGateway") and preferences.get("announce_port") != observed["publicTorrentPort"]:
                    emit(self.runtime, "torrent.recreate_requested", reason="public_announce_port_changed", port=observed["publicTorrentPort"],
                         previousPort=preferences.get("announce_port"), **container_fields(child))
                    # libtorrent applies announce_port only at startup. Recreate the
                    # owned child with its saved files; never claim a live setting took effect.
                    child.stop(timeout=10)
                    child.remove(v=False)
                    self.create(vpn, generation, observed["forwardedPort"])
                    self.error = "Torrent client is restarting with its separate public port"
                    return
                if preferences.get("current_network_interface") != "tun0" or preferences.get("listen_port") != observed["forwardedPort"] or preferences.get("upnp") or preferences.get("max_active_downloads") != self.download_limit() or not preferences.get("queueing_enabled") or preferences.get("rss_refresh_interval") != self.rss_interval() or not preferences.get("rss_processing_enabled") or preferences.get("rss_auto_downloading_enabled"):
                    self.execute(child, "app/setPreferences", "POST", {"json": json.dumps({"current_network_interface": "tun0", "listen_port": observed["forwardedPort"], "upnp": False, "random_port": False, "max_active_downloads": self.download_limit(), "max_active_torrents": -1, "max_active_uploads": -1, "queueing_enabled": True, "rss_processing_enabled": True, "rss_auto_downloading_enabled": False, "rss_refresh_interval": self.rss_interval()})})
                    response = self.execute(child, "app/preferences", "GET", {})
                    preferences = json.loads(base64.b64decode(response["body"]))
                if preferences.get("current_network_interface") != "tun0" or preferences.get("listen_port") != observed["forwardedPort"] or preferences.get("upnp") or preferences.get("max_active_downloads") != self.download_limit() or not preferences.get("queueing_enabled") or not preferences.get("rss_processing_enabled") or preferences.get("rss_auto_downloading_enabled") or preferences.get("rss_refresh_interval") != self.rss_interval():
                    raise ValueError("Torrent interface or port not bound")
                self.error = None
            except Exception as error:
                emit(self.runtime, "torrent.reconcile_error", reason="client_verification_failed", **error_fields(error))
                child = self.child()
                if child:
                    child.reload()
                    state = child.attrs.get("State", {})
                    try:
                        current = self.observed(settings["profileId"])
                        current_vpn = self.runtime.container(settings["profileId"])
                        current_vpn.reload()
                        current_generation = self.generation(current_vpn, current)
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
        def owned_service(service):
            rows = client.containers.list(all=True, filters={"label": [
                "com.docker.compose.project=" + self.seed_project,
                "com.docker.compose.service=" + service,
            ]})
            if len(rows) != 1:
                raise ValueError("Expected exactly one owned MediaSeed service")
            container = rows[0]
            container.reload()
            if container.labels.get("com.docker.compose.project") != self.seed_project or container.labels.get("com.docker.compose.service") != service:
                raise ValueError("Unexpected MediaSeed installation")
            return container
        source = owned_service("qbittorrent")
        app = owned_service("app")
        volumes = {}
        for container, target, writable in ((source, "/config", True), (source, "/downloads", True), (app, "/secrets", False)):
            mount = next((row for row in container.attrs["Mounts"] if row["Destination"] == target), None)
            if not mount or mount.get("Type") != "volume" or not mount.get("Name", "").startswith(self.seed_project + "_"):
                raise ValueError("Unverified MediaSeed storage")
            volume = client.volumes.get(mount["Name"])
            if volume.attrs.get("Labels", {}).get("com.docker.compose.project") != self.seed_project:
                raise ValueError("MediaSeed volume ownership changed")
            volumes[mount["Name"]] = {"bind": target, "mode": "rw" if writable else "ro"}
        for row in self.media_storage:
            for container in (source, app):
                matches = [m for m in container.attrs["Mounts"] if m["Destination"] == row["path"]]
                if (len(matches) != 1 or matches[0].get("Type") != "bind"
                        or matches[0].get("Source") != row["source"] or not matches[0].get("RW")):
                    raise ValueError("MediaSeed media mount ownership changed")
            volumes[row["source"]] = {"bind": row["path"], "mode": "rw"}
        source.stop(timeout=15)
        resolver = self.data / "seedbox-resolv.conf"
        resolver.write_text("nameserver 127.0.0.1\noptions timeout:2 attempts:2\n")
        resolver.chmod(0o644)
        volumes[str(self.runtime.host_data / resolver.name)] = {"bind": "/etc/resolv.conf", "mode": "ro"}
        settings = self.settings()
        observed = self.observed(settings["profileId"]) if self.web_gateway and settings["enabled"] else {}
        announce = observed.get("publicTorrentPort", 0)
        prep = client.containers.run(source.attrs["Image"], entrypoint=["python3", "-c", PREPARE_CODE, str(port), str(announce)], user="1000:1000", volumes={name: value for name, value in volumes.items() if value["bind"] == "/config"}, network_mode="none", detach=True, labels={CLIENT_OWNER: "preparation"})
        try:
            if prep.wait(timeout=20).get("StatusCode") != 0:
                raise ValueError("Torrent preparation failed")
        finally:
            prep.remove(force=True)
        host = source.attrs.get("HostConfig", {})
        resource_options = {}
        if host.get("Memory", 0):
            resource_options["mem_limit"] = host["Memory"]
            resource_options["memswap_limit"] = host.get("MemorySwap") or host["Memory"]
        if host.get("NanoCpus", 0):
            resource_options["nano_cpus"] = host["NanoCpus"]
        elif host.get("CpuQuota", 0) > 0:
            resource_options.update(cpu_quota=host["CpuQuota"], cpu_period=host.get("CpuPeriod") or 100000)
        if host.get("CpusetCpus"):
            resource_options["cpuset_cpus"] = host["CpusetCpus"]
        client.containers.create(source.attrs["Image"], name=CLIENT_NAME, environment={"PUID": "1000", "PGID": "1000", "WEBUI_PORT": "8080", "TZ": "Europe/Copenhagen"}, volumes=volumes, network_mode="container:" + vpn.id, labels={CLIENT_OWNER: "mediaseed", "org.mediahub.mediavpn.generation": generation}, **resource_options, restart_policy={"Name": "no"}, security_opt=["no-new-privileges:true"], log_config={"Type": "json-file", "Config": {"max-size": "5m", "max-file": "2"}})

    def status(self):
        with self.lock:
            settings = self.settings()
            observed = self.observed(settings["profileId"]) if settings["enabled"] else {"healthy": False, "forwardedPort": None, "publicIp": None}
            child = self.child() if self.runtime.enabled else None
            running = False
            if child:
                child.reload()
                vpn = self.runtime.container(settings["profileId"]) if settings["enabled"] else None
                if vpn:
                    vpn.reload()
                    generation = self.generation(vpn, observed)
                    running = child.attrs.get("State", {}).get("Running", False) and child.labels.get("org.mediahub.mediavpn.generation") == generation
            ready = bool(settings["enabled"] and observed["healthy"] and observed["forwardedPort"] and running and not self.error)
            if ready:
                try:
                    response = self.execute(child, "app/preferences", "GET", {})
                    preferences = json.loads(base64.b64decode(response["body"]))
                    ready = preferences.get("current_network_interface") == "tun0" and preferences.get("listen_port") == observed["forwardedPort"] and not preferences.get("upnp")
                    if observed.get("webGateway"):
                        ready = ready and preferences.get("announce_port") == observed["publicTorrentPort"]
                except Exception:
                    ready = False
            return {**settings, "ready": ready, "downloadsEnabled": ready, "vpn": "verified" if ready else "unverified", "publicIp": observed["publicIp"], "forwardedPort": observed.get("publicTorrentPort", observed["forwardedPort"]), "internalPort": observed["forwardedPort"], "country": observed.get("country") if ready else None, "checkedAt": observed.get("checkedAt", time.time()), "reason": self.error or (None if ready else "Vaelg og start en VPN-profil med tildelt port")}

    def generation(self, vpn, observed=None):
        suffix = ":" + hashlib.sha256(json.dumps(self.media_storage, sort_keys=True).encode()).hexdigest()[:16] if self.media_storage else ""
        if self.web_gateway:
            settings = self.settings()
            observed = observed if observed is not None else self.observed(settings["profileId"]) if settings["enabled"] else {}
            if observed.get("webGateway"):
                suffix += ":web:" + str(observed.get("forwardedPort")) + ":" + str(observed.get("publicTorrentPort"))
        return vpn.id + ":" + vpn.attrs["State"]["StartedAt"] + suffix

    def valid_download_path(self, value):
        if not isinstance(value, str) or len(value) > 1100 or "\\" in value or any(ord(c) < 32 for c in value):
            return False
        parts = value.split("/")
        if parts[:2] != ["", "downloads"] or any(p in {"", ".", ".."} for p in parts[2:]):
            return False
        if parts[2:3] == ["media"]:
            return any(value == row["path"] or value.startswith(row["path"] + "/") for row in self.media_storage)
        return True

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
                if path == "rss/torrent" and not re.match(r"^/torrents?/download/[^/]+", address.path):
                    raise HTTPException(422, "Invalid RSS torrent address")
            item_path = payload.get("path", payload.get("itemPath"))
            if item_path is not None and not re.fullmatch(r"MediaSeed-RSS-[a-f0-9]{24}", str(item_path)):
                raise HTTPException(422, "Invalid RSS feed path")
        if method == "POST":
            fields = {
                "torrents/add": {"urls", "stopped", "paused", "savepath", "ratioLimit", "seedingTimeLimit", "shareLimitAction", "category", "tags"},
                "torrents/start": {"hashes"}, "torrents/stop": {"hashes"},
                "torrents/recheck": {"hashes"},
                "torrents/delete": {"hashes", "deleteFiles"},
                "torrents/setShareLimits": {"hashes", "ratioLimit", "seedingTimeLimit", "inactiveSeedingTimeLimit", "shareLimitAction"},
                "torrents/setFilePriority": {"hash", "id", "priority"},
                "rss/addFeed": {"url", "path"}, "rss/removeItem": {"path"}, "rss/refreshItem": {"itemPath"},
                "torrents/createCategory": {"category", "savePath"}, "torrents/topPrio": {"hashes"},
            }
            if not set(payload).issubset(fields[path]):
                raise HTTPException(422, "Unsupported torrent fields")
            if path in {"torrents/add", "torrents/setShareLimits"}:
                if payload.get("shareLimitAction", "Stop") != "Stop":
                    raise HTTPException(422, "Native share limits may only stop and preserve files")
                # qBittorrent 5.2 requires this field for setShareLimits. Keep
                # older callers compatible while preventing global auto-delete.
                payload = {**payload, "shareLimitAction": "Stop"}
            if path == "torrents/createCategory":
                if not re.fullmatch(r"MediaSeed-RSS-[a-f0-9]{24}", str(payload.get("category", ""))) or not self.valid_download_path(payload.get("savePath", "")):
                    raise HTTPException(422, "Invalid RSS category")
            elif not path.startswith("rss/") and path != "torrents/add":
                identifier = str(payload.get("hashes", payload.get("hash", "")))
                if not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", identifier):
                    raise HTTPException(422, "Invalid torrent hash")
            elif path == "torrents/add":
                if not re.fullmatch(r"(?:MediaSeed-Cleanup-[a-f0-9]{24})?", str(payload.get("tags", ""))):
                    raise HTTPException(422, "Invalid cleanup marker")
                if payload.get("stopped") not in {"true", "false"} or payload.get("paused") != payload.get("stopped"):
                    raise HTTPException(422, "Choose a consistent torrent start mode")
                if not self.valid_download_path(payload.get("savepath", "")):
                    raise HTTPException(422, "Invalid download path")
                if content is None and not str(payload.get("urls", "")).startswith("magnet:?"):
                    raise HTTPException(422, "Expected a magnet or torrent file")
            if "ratioLimit" in payload:
                try:
                    unlimited = path == "torrents/setShareLimits" and str(payload["ratioLimit"]) == "-1" and str(payload.get("seedingTimeLimit")) == "-1" and str(payload.get("inactiveSeedingTimeLimit")) == "-1"
                    if not unlimited and (not 1 <= float(payload["ratioLimit"]) <= 1000 or str(payload.get("seedingTimeLimit")) != "2940"):
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
