from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mediavpn.seedbox import Seedbox


@pytest.mark.parametrize("resource_mode", ["unlimited", "cfs", "nano"])
@pytest.mark.parametrize("project", ["mediahub-mediaseed", "mediaseed-preview"])
@pytest.mark.parametrize("bad_owner", [False, True])
@pytest.mark.parametrize("media", ["none", "approved", "mismatch", "readonly", "nas"])
def test_client_creation_resolves_owned_compose_services_and_volumes(tmp_path, monkeypatch, project, bad_owner, media, resource_mode):
    import json
    monkeypatch.setenv("MEDIAVPN_MEDIASEED_PROJECT", project)
    events = []
    def container(service, mounts):
        return SimpleNamespace(labels={"com.docker.compose.project":project,"com.docker.compose.service":service},
            attrs={"Image":"sha256:reviewed-client", "Mounts":mounts},reload=lambda:None,
            stop=lambda **kwargs:events.append("stop source"))
    def mount(name, target):
        return {"Type":"volume","Name":project+"_"+name,"Destination":target}
    source = container("qbittorrent", [mount("config","/config"),mount("downloads","/downloads")])
    resources = {} if resource_mode == "unlimited" else {"Memory": 3*1024**3, "MemorySwap": 4*1024**3, "CpusetCpus": "0-3", **({"NanoCpus": 1500000000} if resource_mode == "nano" else {"CpuQuota": 150000, "CpuPeriod": 100000})}
    source.attrs["HostConfig"] = resources
    app = container("app", [mount("secrets","/secrets")])
    if media != "none":
        storage_id = "nas-" + "a" * 24 if media == "nas" else "movies"
        monkeypatch.setenv("MEDIAVPN_MEDIASEED_STORAGE", json.dumps([{"id":storage_id,"label":"Movies","source":"/mnt/media/movies","path":"/downloads/media/" + storage_id}]))
        source.attrs["Mounts"].append({"Type":"bind","Source":"/mnt/media/movies","Destination":"/downloads/media/" + storage_id,"RW":True})
        app.attrs["Mounts"].append({"Type":"bind","Source":"/wrong" if media == "mismatch" else "/mnt/media/movies","Destination":"/downloads/media/" + storage_id,"RW":media != "readonly"})
    def listing(all, filters):
        assert "com.docker.compose.project="+project in filters["label"]
        return [source if "com.docker.compose.service=qbittorrent" in filters["label"] else app]
    prep = SimpleNamespace(wait=lambda **kwargs:{"StatusCode":0},remove=lambda **kwargs:events.append("remove preparation"))
    created = []
    client = SimpleNamespace(containers=SimpleNamespace(list=listing,run=lambda *args,**kwargs:prep,
        create=lambda *args,**kwargs:created.append(kwargs)),
        volumes=SimpleNamespace(get=lambda name:SimpleNamespace(attrs={"Labels":{"com.docker.compose.project":"unowned" if bad_owner else project}})))
    controller = Seedbox(SimpleNamespace(docker=lambda:client,host_data=tmp_path),tmp_path)
    if bad_owner or media in {"mismatch", "readonly"}:
        with pytest.raises(ValueError,match="ownership changed"):
            controller.create(SimpleNamespace(id="vpn-owned"),"vpn-generation",35458)
        assert not events and not created
    else:
        controller.create(SimpleNamespace(id="vpn-owned"),"vpn-generation",35458)
        assert events == ["stop source","remove preparation"]
        assert created[0]["network_mode"] == "container:vpn-owned"
        assert created[0]["volumes"][project+"_downloads"]["bind"] == "/downloads"
        assert created[0]["volumes"][project+"_secrets"]["mode"] == "ro"
        if resource_mode != "unlimited":
            assert created[0]["mem_limit"] == 3*1024**3
            assert created[0]["memswap_limit"] == 4*1024**3
            assert created[0]["cpuset_cpus"] == "0-3"
            assert created[0].get("nano_cpus") == (1500000000 if resource_mode == "nano" else None)
            assert created[0].get("cpu_quota") == (150000 if resource_mode == "cfs" else None)
        else:
            assert "mem_limit" not in created[0] and "nano_cpus" not in created[0]
        if media in {"approved", "nas"}:
            assert created[0]["volumes"]["/mnt/media/movies"] == {"bind":"/downloads/media/" + storage_id, "mode":"rw,rslave" if media == "nas" else "rw"}


def test_unicode_download_paths_stay_inside_approved_mounts(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("MEDIAVPN_MEDIASEED_STORAGE", json.dumps([{"id":"movies","source":"/mnt/media/movies","path":"/downloads/media/movies"}]))
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    assert controller.valid_download_path("/downloads/media/movies/Danske film Ã¦")
    for path in ["/downloads/../etc", "/downloads/media/unapproved", "/downloads//folder", "/downloads/media/movies/../tv", "/downloads/x\nsecret", "/downloads/a\\b"]:
        assert not controller.valid_download_path(path)
    vpn = SimpleNamespace(id="vpn", attrs={"State":{"StartedAt":"now"}})
    assert controller.generation(vpn).startswith("vpn:now:")


def test_missing_vpn_port_stops_client_and_never_creates(tmp_path):
    calls = []
    child = SimpleNamespace(attrs={"State": {"Running": True}}, reload=lambda: None, stop=lambda **kwargs: calls.append("stop"))
    runtime = SimpleNamespace(enabled=True, status=lambda identifier: {"healthy": True, "forwardedPort": None, "publicIp": "8.8.8.8"})
    controller = Seedbox(runtime, tmp_path)
    controller.child = lambda: child
    controller.path.write_text('{"enabled":true,"profileId":"' + "a" * 24 + '"}')
    controller.create = lambda *args: calls.append("create")
    controller.reconcile()
    assert calls == ["stop"]
    assert controller.error is not None


@pytest.mark.parametrize("saved_rss", [None, 10, 30])
@pytest.mark.parametrize("saved_limit", [8, 3, 25])
def test_reconnect_replaces_network_namespace_and_updates_port(tmp_path, saved_limit, saved_rss):
    import base64
    import json
    events = []
    vpn = SimpleNamespace(id="new-vpn", attrs={"State": {"StartedAt": "new-generation"}}, reload=lambda: None)
    runtime = SimpleNamespace(enabled=True, container=lambda identifier: vpn,
        status=lambda identifier: {"healthy": True, "forwardedPort": 54321, "publicIp": "8.8.8.8"})
    controller = Seedbox(runtime, tmp_path)
    controller.path.write_text(json.dumps({"enabled": True, "profileId": "a" * 24, "maxActiveDownloads": saved_limit, **({"rssRefreshMinutes": saved_rss} if saved_rss is not None else {})}))
    child = SimpleNamespace(labels={"org.mediahub.mediavpn.generation": "old-vpn:old-generation"},
        attrs={"State": {"Running": True}}, reload=lambda: None,
        stop=lambda **kwargs: events.append("stop old client"), remove=lambda: events.append("remove old container"))
    current = [child]
    controller.child = lambda: current[0]
    def create(new_vpn, generation, port):
        events.append(("create", new_vpn.id, generation, port))
        current[0] = SimpleNamespace(labels={"org.mediahub.mediavpn.generation": generation},
            attrs={"State": {"Running": False}}, reload=lambda: None,
            start=lambda: current[0].attrs["State"].update(Running=True))
    controller.create = create
    preferences = {"current_network_interface": "tun0", "listen_port": 12345, "upnp": False,
        "max_active_downloads": 8, "queueing_enabled": True, "rss_processing_enabled": True, "rss_auto_downloading_enabled": False}
    def execute(child, path, method, payload):
        if path == "app/setPreferences":
            preferences.update(json.loads(payload["json"]))
            events.append(("port updated", preferences["listen_port"]))
        return {"body": base64.b64encode(json.dumps(preferences).encode()).decode()}
    controller.execute = execute
    controller.reconcile()
    assert events == ["stop old client", "remove old container", ("create", "new-vpn", "new-vpn:new-generation", 54321)]
    controller.reconcile()
    assert ("port updated", 54321) in events
    assert controller.status()["ready"]
    assert not any("torrents/" in str(event) for event in events)  # User-paused torrents stay paused.


    assert preferences["rss_refresh_interval"] == (saved_rss or 5)
    assert preferences["max_active_downloads"] == saved_limit
    assert controller.download_limit() == saved_limit

def test_rpc_is_restricted_and_requires_readiness(tmp_path):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    with pytest.raises(HTTPException) as caught:
        controller.rpc("auth/login", "POST", {})
    assert caught.value.status_code == 403
    controller.reconcile = lambda: None
    controller.status = lambda: {"ready": False}
    with pytest.raises(HTTPException) as caught:
        controller.rpc("torrents/start", "POST", {"hashes": "a" * 40})
    assert caught.value.status_code == 409
    controller.status = lambda: {"ready": True}
    controller.child = lambda: "synthetic"
    controller.execute = lambda child, path, method, payload, content: payload
    assert controller.rpc("torrents/delete", "POST", {"hashes": "a" * 40, "deleteFiles": "true"})["deleteFiles"] == "true"
    assert "deleteFiles" not in controller.rpc("torrents/delete", "POST", {"hashes": "a" * 40})
    with pytest.raises(HTTPException) as caught:
        controller.rpc("torrents/delete", "POST", {"hashes": "a" * 40, "deleteFiles": "maybe"})
    assert caught.value.status_code == 422


def test_settings_and_machine_token_survive_restart(tmp_path):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    original = controller.token_path.read_text()
    controller.configure(None, False)
    restarted = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    assert restarted.token_path.read_text() == original
    assert restarted.settings() == {"enabled": False, "profileId": None}


def test_recheck_rpc_accepts_one_hash_only_and_keeps_vpn_gate(tmp_path):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    controller.reconcile = lambda: None
    controller.status = lambda: {"ready": True}
    controller.child = lambda: "synthetic"
    calls = []
    controller.execute = lambda child, path, method, payload, content: calls.append((path, payload)) or {"accepted": True}
    assert controller.rpc("torrents/recheck", "POST", {"hashes": "a" * 40}) == {"accepted": True}
    assert calls == [("torrents/recheck", {"hashes": "a" * 40})]
    for payload in [{"hashes": "all"}, {"hashes": "a" * 40 + "|" + "b" * 40}, {"hashes": "a" * 40, "deleteFiles": "true"}]:
        with pytest.raises(HTTPException) as error:
            controller.rpc("torrents/recheck", "POST", payload)
        assert error.value.status_code == 422
    controller.status = lambda: {"ready": False}
    with pytest.raises(HTTPException) as error:
        controller.rpc("torrents/recheck", "POST", {"hashes": "a" * 40})
    assert error.value.status_code == 409
    assert len(calls) == 1


def test_disconnect_removes_only_owned_client_and_preserves_volumes(tmp_path):
    calls = []
    child = SimpleNamespace(
        attrs={"State": {"Running": True}}, reload=lambda: None,
        stop=lambda **kwargs: calls.append(("stop", kwargs)),
        remove=lambda **kwargs: calls.append(("remove", kwargs)),
    )
    controller = Seedbox(SimpleNamespace(enabled=True), tmp_path)
    controller.child = lambda: child if not any(call[0] == "remove" for call in calls) else None
    result = controller.configure("a" * 24, False)
    assert result["enabled"] is False and not result["ready"]
    assert calls == [("stop", {"timeout": 10}), ("remove", {"v": False, "force": False})]
    assert controller.settings()["profileId"] == "a" * 24
    controller.reconcile()
    assert len(calls) == 2


def test_embedded_commands_compile_and_write_correct_interface_keys(tmp_path):
    from mediavpn.seedbox import RPC_CODE, PREPARE_CODE
    import sys
    compile(RPC_CODE, "rpc", "exec")
    path = tmp_path / "qBittorrent.conf"
    path.write_text("[Preferences]\nWebUI\\Username=mediaseed\n")
    code = PREPARE_CODE.replace("/config/qBittorrent/qBittorrent.conf", str(path).replace("\\", "/"))
    previous = sys.argv
    try:
        sys.argv = ["prepare", "12345"]
        exec(compile(code, "prepare", "exec"), {})
    finally:
        sys.argv = previous
    written = path.read_text()
    assert "Session\\Interface=tun0" in written and "Session\\Port=12345" in written
    assert "WebUI\\Address=127.0.0.1" in written
    assert path.with_name(path.name + ".pre-mediavpn").exists()


def test_machine_api_cannot_start_all_or_escape_download_directory(tmp_path):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    for path, payload in [("torrents/start", {"hashes": "all"}), ("torrents/add", {"urls": "magnet:?test", "stopped": "true", "paused": "true", "savepath": "/downloads/../etc"})]:
        with pytest.raises(HTTPException) as caught:
            controller.rpc(path, "POST", payload)
        assert caught.value.status_code == 422


@pytest.mark.parametrize("paused", ["true", "false"])
def test_add_mode_is_validated_and_both_modes_use_fresh_vpn_gate(tmp_path, paused):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    controller.reconcile = lambda: None
    controller.status = lambda: {"ready": False}
    calls = []
    controller.child = lambda: "synthetic"
    controller.execute = lambda child, path, method, payload, content: calls.append(payload)
    payload = {"stopped": paused, "paused": paused, "urls": "magnet:?test", "savepath": "/downloads"}
    with pytest.raises(HTTPException) as caught:
        controller.rpc("torrents/add", "POST", payload)
    assert caught.value.status_code == 409 and not calls
    controller.status = lambda: {"ready": True}
    controller.rpc("torrents/add", "POST", payload)
    assert calls == [{**payload, "shareLimitAction": "Stop"}]
    with pytest.raises(HTTPException) as caught:
        controller.rpc("torrents/add", "POST", {**payload, "paused": "maybe"})
    assert caught.value.status_code == 422


@pytest.mark.parametrize("url", [
    "https://nordicbytes.org/torrent/download/synthetic",
    "https://nordicbytes.org/torrents/download/12/synthetic",
])
def test_rss_download_formats_keep_fresh_vpn_gate(tmp_path, url):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    controller.reconcile = lambda: None
    controller.status = lambda: {"ready": False}
    calls = []
    controller.child = lambda: "synthetic"
    controller.execute = lambda *args: calls.append(args)
    with pytest.raises(HTTPException) as error:
        controller.rpc("rss/torrent", "GET", {"url": url})
    assert error.value.status_code == 409 and not calls
    controller.status = lambda: {"ready": True}
    controller.rpc("rss/torrent", "GET", {"url": url})
    assert calls[0][1:4] == ("rss/torrent", "GET", {"url": url})


@pytest.mark.parametrize("url", [
    "http://nordicbytes.org/torrent/download/synthetic",
    "https://nordicbytes.org.evil.invalid/torrent/download/synthetic",
    "https://user@nordicbytes.org/torrent/download/synthetic",
    "https://nordicbytes.org/torrent/download/",
    "https://nordicbytes.org/torrent/details/synthetic",
    "https://nordicbytes.org/torrent/download/synthetic#fragment",
])
def test_rss_download_rejects_unapproved_addresses_before_vpn(tmp_path, url):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    controller.reconcile = lambda: pytest.fail("Invalid address reached runtime")
    with pytest.raises(HTTPException) as error:
        controller.rpc("rss/torrent", "GET", {"url": url})
    assert error.value.status_code == 422


def test_rss_fetch_identifies_actual_client_and_keeps_redirects_disabled(monkeypatch, capsys):
    import base64
    import io
    import json
    import sys
    import urllib.request
    from pathlib import Path
    from mediavpn.seedbox import RPC_CODE

    class Opener:
        def __init__(self, external=False):
            self.external = external
        def open(self, request, timeout):
            if self.external:
                assert request.get_header("User-agent") == "qBittorrent/5.2.0"
                assert request.full_url == "https://nordicbytes.org/torrent/download/synthetic"
                content = b"dee"
            else:
                content = b"v5.2.0" if request.full_url.endswith("app/version") else b"Ok."
            response = io.BytesIO(content)
            response.status = 200
            return response
    created = []
    def build(*handlers):
        external = any(isinstance(h, urllib.request.HTTPRedirectHandler) for h in handlers)
        if external:
            handler = next(h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler))
            assert handler.redirect_request(None, None, None, None, None, None) is None
        created.append(external)
        return Opener(external)
    monkeypatch.setattr(urllib.request, "build_opener", build)
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *args, **kwargs: "synthetic-password" if self.as_posix() == "/secrets/qbit-password" else original(self, *args, **kwargs))
    body = {"path": "rss/torrent", "method": "GET", "payload": {"url": "https://nordicbytes.org/torrent/download/synthetic"}}
    monkeypatch.setattr(sys, "argv", ["rpc", base64.b64encode(json.dumps(body).encode()).decode()])
    exec(compile(RPC_CODE, "rpc", "exec"), {})
    result = json.loads(capsys.readouterr().out)
    assert result == {"status": 200, "body": base64.b64encode(b"dee").decode()}
    assert created == [False, True]
