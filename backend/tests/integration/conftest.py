"""集成测试环境：独立的 SQLite 库 + 同步执行工作流。

为什么要在导入应用之前改环境变量：
- `app.config.Settings` 被 `lru_cache` 缓存，`app.db.session` 在导入时就创建了引擎；
- 集成测试必须**完全不碰**开发库 `data/pic.db`，否则会污染演示数据。
因此这里先设环境变量、清缓存、重新绑定引擎与 Session 工厂，
再重新加载那些在导入期捕获了 `SessionLocal` 的模块。
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

# Never reuse or delete a stable workspace database: each pytest process owns a fresh temp DB.
_TEST_DB_DIR = tempfile.TemporaryDirectory(prefix="deal-risk-agent-itest-")
TEST_DB = Path(_TEST_DB_DIR.name) / "itest.db"


def pytest_sessionfinish(session, exitstatus):
    """Close SQLite handles before Windows removes the process-owned test directory."""
    from app.db import session as db_session

    db_session.engine.dispose()
    _TEST_DB_DIR.cleanup()

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
os.environ["TASK_RUN_INLINE"] = "true"
os.environ["AUTO_INIT_DB"] = "true"
os.environ["AUTO_SEED"] = "true"
os.environ["LLM_PROVIDER"] = "stub"
os.environ["EMBEDDING_PROVIDER"] = "stub"
# 安全兜底：测试**绝不能**打到真实付费模型。
# provider=stub 本身就会让网关判定为"未配置"，这里再把密钥与路由清空，
# 即使有人在本地 backend/.env 里填了真实密钥也不会被测试使用。
os.environ["LLM_API_KEY"] = ""
os.environ["AGENT_MODEL_OVERRIDES"] = "{}"
os.environ["STORAGE_BACKEND"] = "local"
os.environ["CACHE_BACKEND"] = "memory"
os.environ["JWT_SECRET"] = "integration-test-secret-at-least-32-bytes-long"

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402


def _rebind_database() -> None:
    """把配置与引擎重新绑定到测试库（必须在导入业务模块前调用）。"""
    get_settings.cache_clear()
    import app.config as config_module

    config_module.settings = config_module.get_settings()

    from app.db import session as db_session

    db_session._settings = config_module.settings
    db_session.engine = db_session.build_engine(config_module.settings)
    db_session.SessionLocal = db_session.build_session_factory(db_session.engine)

    # 这些模块在导入期 `from app.db.session import SessionLocal`，需要重新绑定
    for name in (
        "app.graph.runtime",
        "app.services.workflow_runner",
        "app.services.evaluation_service",
        "app.db.seed",
    ):
        module = sys.modules.get(name) or importlib.import_module(name)
        importlib.reload(module)


_rebind_database()


@pytest.fixture(scope="session")
def seeded_db():
    """建表 + 播种演示数据（文档 9 篇、指标 3 个、评测集 50 条）。"""
    from app.db.seed import init_db, seed_all
    from app.db.session import SessionLocal

    init_db(drop=True)
    session = SessionLocal()
    try:
        summary = seed_all(session)
    finally:
        session.close()
    return summary


@pytest.fixture(scope="session")
def client(seeded_db):
    """带数据的 TestClient；`task_run_inline=true` 让任务在请求内同步跑完。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def isolate_browser_cookies(client):
    """每个集成用例隔离浏览器会话，避免登录接口新增 Cookie 污染未登录断言。"""

    client.cookies.clear()
    yield
    client.cookies.clear()


def _login(client, username: str, password: str) -> str:
    # 集成测试大多显式使用 Bearer；清理登录响应下发的浏览器 Cookie，
    # 避免共享 TestClient 让后续“未登录”断言被隐式会话污染。
    client.cookies.clear()
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    client.cookies.clear()
    return token


@pytest.fixture(scope="session")
def tokens(client) -> dict[str, str]:
    return {
        "admin": _login(client, "admin", "admin123"),
        "pm": _login(client, "pm", "pm123"),
        "ops": _login(client, "ops", "ops123"),
        "eng": _login(client, "eng", "eng123"),
        "reviewer": _login(client, "reviewer", "reviewer123"),
    }


@pytest.fixture(scope="session")
def auth(tokens) -> dict[str, dict[str, str]]:
    return {role: {"Authorization": f"Bearer {token}"} for role, token in tokens.items()}
