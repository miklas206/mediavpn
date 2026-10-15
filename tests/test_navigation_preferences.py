import sqlite3
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediavpn.appearance import install_theme_preferences, navigation_override, theme_override


def test_navigation_is_per_account_additive_and_inheritance_is_restorable(tmp_path):
    @contextmanager
    def database():
        with sqlite3.connect(tmp_path / "preferences.sqlite") as db:
            yield db
    user = {"id": "first", "role": "viewer"}
    app = FastAPI()
    install_theme_preferences(app, database, lambda: user, True)
    with TestClient(app) as client:
        assert navigation_override(database, "first") is None
        assert client.put("/api/auth/preferences", json={"themeOverride": "light"}).status_code == 200
        for value in ["sidebar", "topbar", "dock-top", "dock-bottom", "launcher"]:
            assert client.put("/api/auth/navigation", json={"navigationOverride": value}).status_code == 200
            assert navigation_override(database, "first") == value
        assert theme_override(database, "first") == "light"
        for value in ["unknown", True, 1]:
            assert client.put("/api/auth/navigation", json={"navigationOverride": value}).status_code == 422
        user["id"] = "second"
        assert navigation_override(database, "second") is None
        assert client.put("/api/auth/navigation", json={"navigationOverride": "topbar"}).status_code == 200
        user["id"] = "first"
        assert client.put("/api/auth/navigation", json={"navigationOverride": None}).status_code == 200
        assert navigation_override(database, "first") is None
        assert navigation_override(database, "second") == "topbar"
    install_theme_preferences(FastAPI(), database, lambda: user, True)
    assert navigation_override(database, "second") == "topbar"
    assert theme_override(database, "first") == "light"
