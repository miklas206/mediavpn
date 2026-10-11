from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mediavpn.seedbox import Seedbox


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
    with pytest.raises(HTTPException) as caught:
        controller.rpc("torrents/delete", "POST", {"hashes": "a" * 40, "deleteFiles": "true"})
    assert caught.value.status_code == 403


def test_settings_and_machine_token_survive_restart(tmp_path):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    original = controller.token_path.read_text()
    controller.configure(None, False)
    restarted = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    assert restarted.token_path.read_text() == original
    assert restarted.settings() == {"enabled": False, "profileId": None}


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