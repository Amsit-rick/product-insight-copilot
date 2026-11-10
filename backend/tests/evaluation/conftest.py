"""评测测试共用夹具：复用集成测试的“独立测试库 + 带数据的 TestClient”。

评测必须跑真实工作流，因此这里直接复用 `tests/integration/conftest.py` 里的夹具，
保证集成测试与评测测试看到的是同一套演示数据与同一个临时数据库。
"""

from __future__ import annotations

from tests.integration.conftest import (  # noqa: F401  （pytest 夹具导入）
    auth,
    client,
    seeded_db,
    tokens,
)
