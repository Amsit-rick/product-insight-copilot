"""测试公共夹具。

三条硬性约定：

1. **绝不触碰开发库**（`backend/data/pic.db`）：每个用例都在临时目录下新建
   独立的 SQLite 库，用完即弃，互不干扰；
2. **临时目录放在工作区内**：本机的平台临时目录（`%TEMP%`）在当前沙箱里
   不可枚举，pytest 内置 `tmp_path` 一创建就 `PermissionError`，
   因此这里用工作区的 `.tmp/` 覆盖它；
3. 用例全部同步执行（Windows 下不引入 async 驱动），需要数据库时直接用
   `build_engine` / `build_session_factory` 手搓会话。
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path

import pytest

# The full suite launches multiple Python subprocesses. Keep native numeric
# runtimes single-threaded so OpenBLAS does not reserve one worker arena per CPU
# in every child process and fail nondeterministically on constrained machines.
for _thread_env in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[_thread_env] = "1"

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import build_engine, build_session_factory
from app.models import Base  # 导入包即注册全部表，create_all 才能建全
from app.models.metrics import MetricSnapshot

# 指标快照的默认租户：与 `SqlMetricService` / 导入器保持一致
TENANT = "*"

# 工作区内的临时目录根（backend/.tmp，已在 pytest.ini 的 norecursedirs 里排除）
_TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp"


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """覆盖 pytest 内置的 `tmp_path`：改用工作区目录，规避平台临时目录权限。

    与内置行为一致：每个用例一个唯一目录，用例结束后删除。
    """
    path = _TMP_ROOT / f"pytest-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def db_settings(tmp_path: Path) -> Settings:
    """把数据库指向临时文件的配置副本，防止误连开发库。"""
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    return get_settings().model_copy(update={"database_url": url})


@pytest.fixture
def engine(db_settings: Settings) -> Iterator[Engine]:
    engine = build_engine(db_settings)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    db = build_session_factory(engine)()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture
def add_snapshot(session: Session) -> Callable[..., MetricSnapshot]:
    """写入一条指标快照的便捷工厂（默认租户 `*`，返回已 flush 的行）。"""

    def _add(
        *,
        stat_date: date,
        value: float,
        metric_name: str = "retention_d1",
        tenant_id: str = TENANT,
        platform: str | None = None,
        app_version: str | None = None,
        channel: str | None = None,
        device: str | None = None,
        sample_size: int | None = None,
        dataset_version: str | None = None,
        source_version: str | None = None,
        quality_flags: list[str] | None = None,
    ) -> MetricSnapshot:
        row = MetricSnapshot(
            tenant_id=tenant_id,
            metric_name=metric_name,
            platform=platform,
            app_version=app_version,
            channel=channel,
            device=device,
            stat_date=stat_date,
            value=value,
            sample_size=sample_size,
            dataset_version=dataset_version,
            source_version=source_version,
            quality_flags=quality_flags or [],
        )
        session.add(row)
        session.flush()
        return row

    return _add
