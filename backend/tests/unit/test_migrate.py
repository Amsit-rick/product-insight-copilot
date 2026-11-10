"""迁移执行器单元测试（同步用例）。

覆盖点：
- SQLite 上执行 `apply_migrations` 必须明确拒绝并提示改用 `python -m app.db.seed`；
- SQLite 上 `status()` 仍然能列出迁移文件（只读，不建表）；
- 语句拆分器对注释、多语句、美元引用块、行尾空白的处理；
- 已应用迁移的内容被修改后，校验和不一致必须报错（不静默重放）；
- 生成的 001_init.sql 覆盖模型中的全部表。

注意：所有数据库都建在 pytest 的 tmp_path 下，绝不触碰 backend/data/pic.db。
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import UniqueConstraint, create_engine, inspect, text

from app.db.migrate import (
    MIGRATIONS_DIR,
    MigrationError,
    apply_migrations,
    discover_migrations,
    file_checksum,
    split_sql_statements,
    status,
)
from app.db.seed import init_db
from app.models.base import Base

# 触发所有模型注册到 Base.metadata
from app.models import evaluation, identity, knowledge, metrics, workflow  # noqa: F401


@pytest.fixture()
def sqlite_engine(tmp_path):
    """隔离的 SQLite 引擎（仅指向 tmp_path）。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'migrate_test.db'}")
    try:
        yield engine
    finally:
        engine.dispose()


def _write_sql(directory, name: str, body: str):
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- 方言拒绝
def test_apply_migrations_refuses_on_sqlite(sqlite_engine, tmp_path):
    """SQLite 下 apply 必须拒绝，并提示使用 seed 脚本。"""
    with pytest.raises(MigrationError) as excinfo:
        apply_migrations(sqlite_engine, directory=tmp_path)

    message = str(excinfo.value)
    assert "SQLite" in message
    assert "app.db.seed" in message
    # 拒绝发生在执行之前：不允许建出任何业务表
    assert inspect(sqlite_engine).get_table_names() == []


def test_apply_migrations_refuses_on_sqlite_even_with_default_directory(sqlite_engine):
    """默认目录（backend/migrations，Postgres DDL）在 SQLite 上同样拒绝。"""
    with pytest.raises(MigrationError) as excinfo:
        apply_migrations(sqlite_engine)
    assert "python -m app.db.seed" in str(excinfo.value)


def test_init_db_upgrades_legacy_sqlite_tool_call_replay_columns(sqlite_engine):
    """存量本地库不能因 create_all 不补列而在读取回放时崩溃。"""
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE tool_calls ("
                "id VARCHAR(36) PRIMARY KEY, task_id VARCHAR(36) NOT NULL, "
                "tool_name VARCHAR(64) NOT NULL, arguments JSON NOT NULL, "
                "result_summary TEXT, status VARCHAR(32) NOT NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE analysis_tasks ("
                "id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL, "
                "user_id VARCHAR(36) NOT NULL, query TEXT NOT NULL, "
                "filters JSON NOT NULL, status VARCHAR(32) NOT NULL"
                ")"
            )
        )

    init_db(sqlite_engine)

    columns = {item["name"] for item in inspect(sqlite_engine).get_columns("tool_calls")}
    assert {
        "result",
        "result_digest",
        "replay_policy",
        "side_effect_level",
        "idempotency_key",
        "replayed",
        "approval_reference",
    } <= columns
    task_columns = {
        item["name"] for item in inspect(sqlite_engine).get_columns("analysis_tasks")
    }
    assert {
        "current_phase",
        "checkpoint_version",
        "phase_attempt",
        "resume_count",
        "resumed_from_checkpoint",
        "wake_reason",
        "state_revision",
    } <= task_columns


def test_init_db_backfills_execution_root_for_existing_sqlite_task(sqlite_engine):
    """SQLite 升级后，存量任务必须拥有可查询的持久化根执行单元。"""
    workflow.AnalysisTask.__table__.create(sqlite_engine)
    with sqlite_engine.begin() as connection:
        connection.execute(
            workflow.AnalysisTask.__table__.insert().values(
                id="legacy-task-1",
                tenant_id="tenant-legacy",
                user_id="user-legacy",
                title="存量诊断任务",
                query="检查历史任务执行树",
                filters={},
                status="completed",
            )
        )

    init_db(sqlite_engine)

    with sqlite_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT id, task_id, owner_type, owner_ref_id, status "
                "FROM task_execution_units WHERE task_id = :task_id"
            ),
            {"task_id": "legacy-task-1"},
        ).mappings().one()

    assert row == {
        "id": "legacy-task-1",
        "task_id": "legacy-task-1",
        "owner_type": "workflow",
        "owner_ref_id": "legacy-task-1",
        "status": "succeeded",
    }


# --------------------------------------------------------------------------- status
def test_status_on_sqlite_lists_files_without_writing(sqlite_engine):
    """status 是只读操作：SQLite 下也能列出文件，且不创建任何表。"""
    rows = status(sqlite_engine)

    assert [row["version"] for row in rows] == [p.name for p in discover_migrations()]
    assert "001_init.sql" in [row["version"] for row in rows]
    for row in rows:
        assert row["applied"] is False
        assert row["applied_at"] is None
        assert row["changed"] is False
        assert row["statements"] is None
        assert len(row["checksum"]) == 64  # sha256 十六进制长度

    # 未连接写事务，schema_migrations 与业务表都不应存在
    assert inspect(sqlite_engine).get_table_names() == []


def test_status_reports_applied_and_changed(sqlite_engine, tmp_path):
    """应用后 status 显示 applied；文件被改动后 changed=True。"""
    path = _write_sql(tmp_path, "001_demo.sql", "CREATE TABLE demo_one (id TEXT PRIMARY KEY);\n")

    assert apply_migrations(sqlite_engine, directory=tmp_path, allow_sqlite=True) == ["001_demo.sql"]

    rows = status(sqlite_engine, directory=tmp_path)
    assert rows[0]["applied"] is True
    assert rows[0]["applied_at"] is not None
    assert rows[0]["changed"] is False
    assert rows[0]["statements"] == 1

    path.write_text("CREATE TABLE demo_one (id TEXT PRIMARY KEY, note TEXT);\n", encoding="utf-8")
    rows = status(sqlite_engine, directory=tmp_path)
    assert rows[0]["changed"] is True


# --------------------------------------------------------------------------- 语句拆分
def test_split_statements_basic_and_trailing_whitespace():
    sql = "\n\nCREATE TABLE a (id TEXT);\n\n   \nCREATE TABLE b (id TEXT)\n;\n\n"
    assert split_sql_statements(sql) == [
        "CREATE TABLE a (id TEXT);",
        "CREATE TABLE b (id TEXT);",
    ]


def test_split_statements_ignores_semicolon_in_line_comment():
    sql = """
-- 注释里的分号不拆: ; ;
CREATE TABLE a (id TEXT); -- 行尾注释; 也不拆
CREATE TABLE b (id TEXT);
"""
    statements = split_sql_statements(sql)
    assert len(statements) == 2
    assert statements[0].startswith("-- 注释里的分号不拆")
    assert "CREATE TABLE a (id TEXT);" in statements[0]
    # 行尾注释属于下一条语句的前导注释，不影响可执行性
    assert statements[1].endswith("CREATE TABLE b (id TEXT);")
    assert "-- 行尾注释; 也不拆" in statements[1]


def test_split_statements_ignores_semicolon_in_block_comment_and_literal():
    sql = """
/* 块注释: ; still comment */
CREATE TABLE a (note TEXT DEFAULT '分号;在字符串里');
"""
    statements = split_sql_statements(sql)
    assert len(statements) == 1
    assert "'分号;在字符串里'" in statements[0]


def test_split_statements_keeps_dollar_quoted_block_intact():
    sql = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE FUNCTION f() RETURNS void AS $$
BEGIN
    PERFORM 1;
    PERFORM 2;
END;
$$ LANGUAGE plpgsql;
CREATE TABLE t (id TEXT);
"""
    statements = split_sql_statements(sql)
    assert len(statements) == 3
    assert statements[0] == "CREATE EXTENSION IF NOT EXISTS vector;"
    assert statements[1].count("PERFORM") == 2
    assert statements[1].rstrip().endswith("LANGUAGE plpgsql;")
    assert statements[2] == "CREATE TABLE t (id TEXT);"


def test_split_statements_tags_and_comments_only_input():
    assert split_sql_statements("-- 只有注释\n\n") == []
    assert split_sql_statements("") == []
    tagged = split_sql_statements("DO $body$ BEGIN PERFORM 1; END $body$;")
    assert len(tagged) == 1
    assert tagged[0].endswith("$body$;")


# --------------------------------------------------------------------------- 校验和
def test_apply_is_idempotent_and_uses_temp_dir(sqlite_engine, tmp_path):
    """重复 apply 不重放；返回空列表。"""
    _write_sql(
        tmp_path,
        "001_demo.sql",
        "-- 初始迁移\nCREATE TABLE demo_one (id TEXT PRIMARY KEY);\n",
    )
    _write_sql(tmp_path, "002_demo.sql", "CREATE TABLE demo_two (id TEXT PRIMARY KEY);\n")

    first = apply_migrations(sqlite_engine, directory=tmp_path, allow_sqlite=True)
    assert first == ["001_demo.sql", "002_demo.sql"]
    assert sorted(inspect(sqlite_engine).get_table_names()) == [
        "demo_one",
        "demo_two",
        "schema_migrations",
    ]

    second = apply_migrations(sqlite_engine, directory=tmp_path, allow_sqlite=True)
    assert second == []


def test_checksum_change_fails_loudly(sqlite_engine, tmp_path):
    """已应用的迁移被修改后，必须报错而不是静默重放。"""
    path = _write_sql(
        tmp_path,
        "001_demo.sql",
        "CREATE TABLE demo_one (id TEXT PRIMARY KEY);\n",
    )
    apply_migrations(sqlite_engine, directory=tmp_path, allow_sqlite=True)
    recorded = file_checksum(path)

    # 修改文件内容（等价于"应用后有人手工改了迁移文件"）
    path.write_text(
        "CREATE TABLE demo_one (id TEXT PRIMARY KEY);\nCREATE TABLE demo_extra (id TEXT);\n",
        encoding="utf-8",
    )
    assert file_checksum(path) != recorded

    with pytest.raises(MigrationError) as excinfo:
        apply_migrations(sqlite_engine, directory=tmp_path, allow_sqlite=True)

    message = str(excinfo.value)
    assert "001_demo.sql" in message
    assert recorded in message
    assert file_checksum(path) in message
    # 报错后不得写入新内容（demo_extra 表不应该被建出来）
    assert "demo_extra" not in inspect(sqlite_engine).get_table_names()


def test_dry_run_writes_nothing(sqlite_engine, tmp_path):
    _write_sql(tmp_path, "001_demo.sql", "CREATE TABLE demo_one (id TEXT PRIMARY KEY);\n")

    planned = apply_migrations(sqlite_engine, directory=tmp_path, dry_run=True, allow_sqlite=True)

    assert planned == ["001_demo.sql"]
    assert inspect(sqlite_engine).get_table_names() == []


def test_dry_run_still_detects_changed_checksum(sqlite_engine, tmp_path):
    path = _write_sql(tmp_path, "001_demo.sql", "CREATE TABLE demo_one (id TEXT);\n")
    apply_migrations(sqlite_engine, directory=tmp_path, allow_sqlite=True)
    path.write_text("CREATE TABLE demo_one (id TEXT, extra TEXT);\n", encoding="utf-8")

    with pytest.raises(MigrationError):
        apply_migrations(sqlite_engine, directory=tmp_path, dry_run=True, allow_sqlite=True)


# --------------------------------------------------------------------------- 生成 DDL 的一致性
def test_init_migration_covers_all_model_tables():
    """001_init.sql 必须覆盖 Base.metadata 的全部表（防止模型改了迁移没跟上）。"""
    sql_path = MIGRATIONS_DIR / "001_init.sql"
    assert sql_path.exists()

    sql = sql_path.read_text(encoding="utf-8")
    in_file = set(re.findall(r"CREATE TABLE (\w+) \(", sql))
    expected = {table.name for table in Base.metadata.sorted_tables}

    # 表数写死是**故意的**：新增表时强制来这里改一次，提醒同步迁移与文档
    # 23 张（诊断平台基线）+ 6 张（售后工单域）+ 2 张（知识库问答会话）
    # + 1 张（不可变任务回放快照）+ 1 张（反思灰度发布状态）
    # + 1 张（可处理运行告警）+ 1 张（告警外部投递 outbox）
    # + 1 张（持久化父子执行单元）+ 1 张（单实例调度租约）= 37
    assert len(expected) == 37
    assert in_file == expected


def test_init_migration_contains_every_column():
    """逐表逐列比对：生成文件中的列名必须与模型完全一致。"""
    sql = (MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")
    # 表级约束行不是列定义
    constraint_keywords = {"PRIMARY", "UNIQUE", "FOREIGN", "CONSTRAINT", "CHECK"}

    for table in Base.metadata.sorted_tables:
        match = re.search(rf"CREATE TABLE {table.name} \((.*?)\n\);", sql, re.DOTALL)
        assert match, f"001_init.sql 中缺少表 {table.name}"
        body = match.group(1)
        names = set(re.findall(r"^\t(\w+) ", body, re.MULTILINE)) - constraint_keywords
        assert names == {column.name for column in table.columns}, f"表 {table.name} 列不一致"


def test_init_migration_contains_every_index():
    """模型上定义的每个索引都必须出现在生成文件中。"""
    sql = (MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")
    in_file = set(re.findall(r"CREATE (?:UNIQUE )?INDEX (\w+) ON", sql))

    expected = {index.name for table in Base.metadata.sorted_tables for index in table.indexes}
    assert expected
    assert expected <= in_file


def test_init_migration_places_named_unique_constraints_on_the_correct_table():
    """约束名和字段都必须出现在所属表体内，避免合法文本被拼到相邻表。"""
    sql = (MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")

    for table in Base.metadata.sorted_tables:
        match = re.search(rf"CREATE TABLE {table.name} \((.*?)\n\);", sql, re.DOTALL)
        assert match, f"001_init.sql 中缺少表 {table.name}"
        body = match.group(1)
        for constraint in table.constraints:
            if not isinstance(constraint, UniqueConstraint) or not constraint.name:
                continue
            columns = ", ".join(column.name for column in constraint.columns)
            expected = f"CONSTRAINT {constraint.name} UNIQUE ({columns})"
            assert expected in body, f"唯一约束 {constraint.name} 未出现在表 {table.name}"


def test_pgvector_migration_matches_pgvector_store():
    """002_pgvector.sql 的列与 PgVectorStore.ensure_schema 保持一致。"""
    sql = (MIGRATIONS_DIR / "002_pgvector.sql").read_text(encoding="utf-8")
    executable = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )

    assert "CREATE EXTENSION IF NOT EXISTS vector" in executable
    assert "CREATE TABLE IF NOT EXISTS chunk_vectors" in executable
    assert "chunk_id TEXT PRIMARY KEY" in executable
    assert "tenant_id TEXT NOT NULL" in executable
    assert "kb_code TEXT NOT NULL" in executable
    assert "security_level TEXT NOT NULL" in executable
    assert "embedding vector(1024)" in executable
    assert "CREATE INDEX IF NOT EXISTS ix_chunk_vectors_kb ON chunk_vectors (tenant_id, kb_code)" in executable
    # 近邻索引必须有示例但默认不启用（需在数据灌入后创建）
    assert "ivfflat" in sql and "hnsw" in sql


def test_incremental_add_column_migrations_are_safe_after_fresh_init():
    """001 已包含的新列，后续兼容迁移必须可在全新 PostgreSQL 上重复遇到。"""
    incremental = [path for path in discover_migrations() if path.name != "001_init.sql"]
    unsafe: list[str] = []
    add_column = re.compile(
        r"ALTER\s+TABLE\s+\S+\s+ADD\s+COLUMN\s+(?!IF\s+NOT\s+EXISTS)",
        re.IGNORECASE,
    )
    for path in incremental:
        if add_column.search(path.read_text(encoding="utf-8")):
            unsafe.append(path.name)
    assert unsafe == [], f"增量 ADD COLUMN 缺少 IF NOT EXISTS，fresh schema 会重复加列：{unsafe}"


# --------------------------------------------------------------------------- schema_migrations 记录
def test_schema_migrations_records_checksum_and_statement_count(sqlite_engine, tmp_path):
    _write_sql(
        tmp_path,
        "001_demo.sql",
        "-- 注释\nCREATE TABLE demo_one (id TEXT PRIMARY KEY);\n\nCREATE INDEX ix_demo ON demo_one (id);\n",
    )
    apply_migrations(sqlite_engine, directory=tmp_path, allow_sqlite=True)

    with sqlite_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT version, applied_at, checksum, statements FROM schema_migrations")
        ).fetchall()

    assert len(rows) == 1
    version, applied_at, checksum, statements = rows[0]
    assert version == "001_demo.sql"
    assert applied_at is not None
    assert checksum == file_checksum(tmp_path / "001_demo.sql")
    assert statements == 2  # CREATE TABLE + CREATE INDEX
