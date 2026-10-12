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
