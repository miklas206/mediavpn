import base64
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from mediavpn.move_location import PREPARE_MOVE_CODE
from mediavpn.seedbox import Seedbox


def test_rpc_rejects_unapproved_or_staging_destinations_before_contacting_client(tmp_path):
    seed = Seedbox(type("Runtime", (), {})(), tmp_path)
    for value in ["/etc", "/downloads/../etc", "/downloads/media/unknown", "/downloads/.incomplete"]:
        with pytest.raises(HTTPException) as error:
            seed.rpc("torrents/setLocation", "POST", {"hashes": "a" * 40, "location": value})
        assert error.value.status_code == 422
    with pytest.raises(HTTPException):
        seed.rpc("torrents/setLocation", "POST", {"hashes": "all", "location": "/downloads"})


def test_client_preflight_refuses_collisions_and_incomplete_torrents(tmp_path, monkeypatch):
    namespace = {}
    exec(PREPARE_MOVE_CODE, namespace)
    source, destination = tmp_path / "source", tmp_path / "target"
    source.mkdir();destination.mkdir();(source / "movie.mkv").write_bytes(b"film")
    row = {"hash": "a" * 40, "progress": 1, "state": "stalledUP", "save_path": str(source)}
    monkeypatch.setattr(Path, "is_relative_to", lambda *args: True)
    def send(path):
        data = [row] if path.startswith('torrents/info') else [{"name": "movie.mkv", "size": 4, "progress": 1}]
        return {"body": base64.b64encode(json.dumps(data).encode()).decode()}
    namespace['prepare_move']("a" * 40, destination, send)
    (destination / "movie.mkv").write_bytes(b"keep")
    with pytest.raises(ValueError):namespace['prepare_move']("a" * 40, destination, send)
    assert (destination / "movie.mkv").read_bytes() == b"keep"
    (destination / "movie.mkv").unlink();row["progress"] = .9
    with pytest.raises(ValueError):namespace['prepare_move']("a" * 40, destination, send)
