from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mediavpn.incomplete import PREPARE_INCOMPLETE_CODE
from mediavpn.seedbox import Seedbox


def prepare(destination):
    namespace = {}
    exec(PREPARE_INCOMPLETE_CODE, namespace)
    return namespace["prepare_incomplete"](str(destination))


@pytest.mark.parametrize("older", [False, True])
def test_client_prepares_staging_and_same_filesystem_rename_preserves_data(tmp_path, older):
    destination = tmp_path / "NAS/Film æ"
    destination.mkdir(parents=True)
    (destination / "existing").write_bytes(b"keep")
    if older:
        (destination / ".incomplete").mkdir()
    assert prepare(destination) == str(destination / ".incomplete")
    partial = destination / ".incomplete/probe"
    partial.write_bytes(b"synthetic completed data")
    inode = partial.stat().st_ino
    prepare(destination)
    partial.rename(destination / "probe")
    assert (destination / "probe").stat().st_ino == inode
    assert (destination / "existing").read_bytes() == b"keep"


@pytest.mark.parametrize("obstacle", ["symlink", "file", "mount", "missing-parent", "io-error"])
def test_client_rejects_unusable_staging(tmp_path, monkeypatch, obstacle):
    target = tmp_path / ".incomplete"
    destination = tmp_path
    if obstacle == "symlink":
        other = tmp_path / "other"
        other.mkdir()
        target.symlink_to(other)
    elif obstacle == "file":
        target.write_text("preserve")
    elif obstacle == "mount":
        monkeypatch.setattr("os.path.ismount", lambda path: path == target)
    elif obstacle == "missing-parent":
        destination = tmp_path / "missing"
    else:
        def failed_write(**kwargs):
            raise OSError(5, "Synthetic network I/O failure")
        monkeypatch.setattr("tempfile.TemporaryFile", failed_write)
    with pytest.raises(OSError):
        prepare(destination)


def test_old_and_new_callers_cannot_select_unrelated_temporary_storage(tmp_path):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    controller.reconcile = lambda: None
    controller.status = lambda: {"ready": True}
    controller.child = lambda: "synthetic"
    controller.execute = lambda child, path, method, payload, content: payload
    payload = {"urls": "magnet:?synthetic", "savepath": "/downloads/Film æ", "stopped": "true", "paused": "true"}
    expected = {**payload, "shareLimitAction": "Stop", "downloadPath": "/downloads/Film æ/.incomplete", "useDownloadPath": "true", "autoTMM": "false"}
    assert controller.rpc("torrents/add", "POST", payload) == expected
    assert controller.rpc("torrents/add", "POST", expected) == expected
    for change in [{"downloadPath": "/downloads/temp"}, {"useDownloadPath": "false"}, {"autoTMM": "true"}, {"savepath": "/downloads/.incomplete"}]:
        with pytest.raises(HTTPException) as error:
            controller.rpc("torrents/add", "POST", {**payload, **change})
        assert error.value.status_code == 422
