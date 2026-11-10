"""浏览器会话安全契约：HttpOnly Cookie、双提交 CSRF 与 Bearer 兼容。"""

from __future__ import annotations

from uuid import uuid4


def _login(client, username: str = "ops", password: str = "ops123"):
    client.cookies.clear()
    response = client.post("/api/v1/auth/session", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response


def _csrf(client) -> str:
    return client.cookies.get("pic_csrf", "")


def test_login_sets_http_only_session_and_readable_csrf_cookie(client):
    response = _login(client)
    cookies = [item.lower() for item in response.headers.get_list("set-cookie")]
    session = next(item for item in cookies if item.startswith("pic_session="))
    csrf = next(item for item in cookies if item.startswith("pic_csrf="))

    assert "httponly" in session
    assert "httponly" not in csrf
    assert "samesite=lax" in session
    assert "samesite=lax" in csrf
    assert "max-age=43200" in session
    assert "access_token" not in response.json()
    assert response.json()["csrf_cookie_name"] == "pic_csrf"
    assert response.json()["csrf_header_name"] == "X-CSRF-Token"
    assert response.json()["user"]["username"] == "ops"
    assert response.headers["Cache-Control"] == "no-store"


def test_me_accepts_cookie_without_exposing_token_to_request(client):
    _login(client)
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200
    assert response.json()["username"] == "ops"
    assert response.json()["csrf_cookie_name"] == "pic_csrf"
    assert response.json()["csrf_header_name"] == "X-CSRF-Token"


def test_cookie_write_requires_csrf_but_bearer_remains_compatible(client):
    _login(client)
    denied = client.post(
        "/api/v1/tickets",
        json={"external_id": f"COOKIE-CSRF-{uuid4().hex}", "subject": "CSRF", "content": "CSRF"},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "csrf_failed"

    allowed = client.post(
        "/api/v1/tickets",
        headers={"X-CSRF-Token": _csrf(client)},
        json={"external_id": f"COOKIE-CSRF-{uuid4().hex}", "subject": "CSRF", "content": "CSRF"},
    )
    assert allowed.status_code == 201, allowed.text

    token_response = client.post(
        "/api/v1/auth/login", json={"username": "ops", "password": "ops123"}
    )
    assert token_response.status_code == 200
    assert token_response.headers["Cache-Control"] == "no-store"
    assert not any(
        item.lower().startswith("pic_session=")
        for item in token_response.headers.get_list("set-cookie")
    )
    token = token_response.json()["access_token"]
    client.cookies.clear()
    bearer = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert bearer.status_code == 200
    assert bearer.json()["username"] == "ops"


def test_custom_csrf_cookie_and_header_names_are_returned_and_accepted(client, monkeypatch):
    from app.api import deps, routes_auth
    from app.config import get_settings

    settings = get_settings().model_copy(
        update={
            "auth_cookie_name": "custom_session",
            "auth_csrf_cookie_name": "custom_csrf",
            "auth_csrf_header_name": "X-Custom-CSRF",
        }
    )
    monkeypatch.setattr(routes_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    response = client.post(
        "/api/v1/auth/session", json={"username": "ops", "password": "ops123"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["csrf_cookie_name"] == "custom_csrf"
    assert response.json()["csrf_header_name"] == "X-Custom-CSRF"
    # 页面刷新时前端会重新请求 /me，必须能够恢复非默认 CSRF 名称。
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["csrf_cookie_name"] == "custom_csrf"
    assert me.json()["csrf_header_name"] == "X-Custom-CSRF"
    csrf = client.cookies.get("custom_csrf", "")
    assert csrf
    created = client.post(
        "/api/v1/tickets",
        headers={"X-Custom-CSRF": csrf},
        json={"external_id": f"CUSTOM-CSRF-{uuid4().hex}", "subject": "CSRF", "content": "CSRF"},
    )
    assert created.status_code == 201, created.text


def test_logout_clears_session_and_csrf_cookies(client):
    _login(client)
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 204
    cookies = [item.lower() for item in response.headers.get_list("set-cookie")]
    assert any(item.startswith("pic_session=") and "max-age=0" in item for item in cookies)
    assert any(item.startswith("pic_csrf=") and "max-age=0" in item for item in cookies)
    assert client.get("/api/v1/auth/me").status_code == 401
