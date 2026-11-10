"""前端静态托管契约测试。

后端在 `frontend/dist` 存在时把它挂到根路径，且不能影响 API 路由。
构建产物不存在时跳过（CI 里没跑前端构建也不应该失败）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

DIST_DIR = Path(__file__).resolve().parents[3] / "frontend" / "dist"


@pytest.mark.skipif(not (DIST_DIR / "index.html").exists(), reason="未构建前端（frontend/dist 不存在）")
def test_frontend_index_is_served_at_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="app"' in response.text


@pytest.mark.skipif(not (DIST_DIR / "index.html").exists(), reason="未构建前端（frontend/dist 不存在）")
def test_frontend_assets_are_served(client):
    import re

    html = client.get("/").text
    assets = re.findall(r'/assets/([^"]+)', html)
    assert assets, "index.html 里应当引用构建产物"
    for asset in assets[:2]:
        response = client.get(f"/assets/{asset}")
        assert response.status_code == 200
        assert len(response.content) > 100


@pytest.mark.skipif(not (DIST_DIR / "index.html").exists(), reason="未构建前端（frontend/dist 不存在）")
def test_api_routes_still_work_with_frontend_mounted(client, auth):
    """静态挂载在根路径上，不能吞掉 /api/**。"""
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/agents", headers=auth["admin"]).status_code == 200
    assert client.post(
        "/api/v1/auth/login", json={"username": "pm", "password": "pm123"}
    ).status_code == 200
