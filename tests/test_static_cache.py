from fastapi.testclient import TestClient

from mediavpn.app import create_app


def test_app_shell_and_scripts_revalidate_after_upgrade(tmp_path):
    client = TestClient(create_app(tmp_path, "http://testserver"))
    for path in ("/static/index.html", "/static/gateway.js?v=profiles-2", "/static/app.js?v=profiles-2", "/static/app-shell.css?v=4"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-cache"
        # Cached bytes can be reused only after server validation.
        cached = client.get(path, headers={"If-None-Match": response.headers["etag"]})
        assert cached.status_code == 304
        assert cached.headers["Cache-Control"] == "no-cache"
    assert client.get("/api/auth/status").headers["Cache-Control"] == "no-store"
