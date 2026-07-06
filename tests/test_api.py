from fastapi.testclient import TestClient

from app.main import app


def test_login_and_dashboard():
    with TestClient(app) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        response = client.post("/login", data={"username": "admin", "password": "test-admin-password"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert client.get("/").status_code == 200


def test_api_requires_login():
    with TestClient(app) as client:
        response = client.get("/api/jobs/does-not-exist", follow_redirects=False)
        assert response.status_code == 303

