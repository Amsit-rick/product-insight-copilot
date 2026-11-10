"""接口文档暴露的收口：本地开着、生产强制关。

为什么值得一个测试：/docs、/redoc、/openapi.json 会把**全部端点与数据结构**交给任何能访问
服务的人（本项目里还有 /api/v1/agents、/api/v1/workflow 这类"能力清单"接口）。
这不是"配置洁癖"，而是最省事的一份资产地图 —— 上线前必须关掉，且不能靠人记得改。
"""

from __future__ import annotations

import importlib

import pytest

from app.config import get_settings


def _build_app(monkeypatch, *, env: str, expose: bool):
    monkeypatch.setenv("APP_ENV", env)
    monkeypatch.setenv("EXPOSE_DOCS", "true" if expose else "false")
    get_settings.cache_clear()
    import app.config as config_module

    config_module.settings = config_module.get_settings()
    main_module = importlib.reload(importlib.import_module("app.main"))
    try:
        return main_module.create_app()
    finally:
        get_settings.cache_clear()


def test_本地环境开放接口文档(monkeypatch):
    app = _build_app(monkeypatch, env="local", expose=True)
    assert app.docs_url == "/docs"
    assert app.openapi_url == "/openapi.json"
    assert app.redoc_url == "/redoc"


def test_生产环境即使显式要求也强制关闭(monkeypatch):
    """关键：`EXPOSE_DOCS=true` 在 prod 下**也**不能打开 —— 否则"忘了设环境变量"就成了漏点。"""
    app = _build_app(monkeypatch, env="prod", expose=True)
    assert app.docs_url is None
    assert app.openapi_url is None, "openapi.json 也必须关：它是最省事的端点清单"
    assert app.redoc_url is None


def test_非生产环境可以显式关闭(monkeypatch):
    app = _build_app(monkeypatch, env="dev", expose=False)
    assert app.docs_url is None
    assert app.openapi_url is None


@pytest.fixture(autouse=True)
def _restore_module():
    """测试结束后把 app.main 恢复成默认环境，避免影响其它用例。"""
    yield
    import os

    os.environ.pop("APP_ENV", None)
    os.environ.pop("EXPOSE_DOCS", None)
    get_settings.cache_clear()
    import app.config as config_module

    config_module.settings = config_module.get_settings()
    importlib.reload(importlib.import_module("app.main"))
