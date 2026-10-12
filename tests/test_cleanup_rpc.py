from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mediavpn.seedbox import Seedbox


def test_cleanup_marker_and_unlimited_limits_are_narrowly_allowed(tmp_path):
    controller = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    controller.reconcile = lambda: None
    controller.status = lambda: {"ready": True}
    controller.child = lambda: "synthetic"
    controller.execute = lambda child, path, method, payload, content: payload
    payload = {"hashes": "a" * 40, "ratioLimit": "-1", "seedingTimeLimit": "-1", "inactiveSeedingTimeLimit": "-1", "shareLimitAction": "Stop"}
    assert controller.rpc("torrents/setShareLimits", "POST", payload) == payload
    for patch in ({"ratioLimit": "nan"}, {"seedingTimeLimit": "-2"}, {"inactiveSeedingTimeLimit": "0"}):
        with pytest.raises(HTTPException):
            controller.rpc("torrents/setShareLimits", "POST", {**payload, **patch})
    with pytest.raises(HTTPException):
        controller.rpc("torrents/setShareLimits", "POST", {**payload, "shareLimitAction": "RemoveWithContent"})
    add = {"urls": "magnet:?xt=urn:btih:" + "a" * 40, "paused": "true", "stopped": "true", "savepath": "/downloads", "ratioLimit": "1", "seedingTimeLimit": "2940", "tags": "MediaSeed-Cleanup-" + "a" * 24}
    assert controller.rpc("torrents/add", "POST", add)["tags"] == add["tags"]
    with pytest.raises(HTTPException):
        controller.rpc("torrents/add", "POST", {**add, "tags": "arbitrary,tag"})
    controller.status = lambda: {"ready": False}
    with pytest.raises(HTTPException) as error:
        controller.rpc("torrents/setShareLimits", "POST", payload)
    assert error.value.status_code == 409
