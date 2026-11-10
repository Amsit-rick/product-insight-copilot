"""SQL 迁移执行器（不依赖 alembic）。

背景：本项目未安装 alembic（离线环境无法 pip install），因此用"SQL 文件 + 版本表"
这一最小实现完成版本化迁移：

- `backend/migrations/*.sql` 按文件名顺序执行，文件名即版本号（如 `001_init.sql`）；
- 已执行的版本记录在 `schema_migrations` 表（不存在则自动创建），记录 sha256 校验和；
- 若某个已应用文件的内容事后被修改（校验和不一致），**直接报错退出**，绝不静默重放；
- 方言判断基于 SQLAlchemy Engine 的 dialect（`engine.dialect.name`），不依赖配置字符串。

本地 / 测试环境默认使用 SQLite，而 `migrations/*.sql` 是 Postgres DDL，
因此在 SQLite 上执行 `apply` 会被明确拒绝，并提示改用 `python -m app.db.seed`。

用法：
    python -m app.db.migrate apply [--dry-run] [--directory DIR]
    python -m app.db.migrate status [--directory DIR]
    python -m app.db.migrate list [--directory DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.config import BACKEND_DIR
from app.logging_utils import get_logger

logger = get_logger(__name__)

# 迁移文件默认目录：backend/migrations
MIGRATIONS_DIR = BACKEND_DIR / "migrations"

# 版本登记表（Postgres / SQLite 通用 DDL）
SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMP,
    checksum TEXT,
    statements INTEGER
)
"""

_SQL_SUFFIX = ".sql"
# 美元引用块标记：$$ 或 $tag$
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


class MigrationError(RuntimeError):
    """迁移失败（方言不匹配、校验和不一致、SQL 执行失败等）。"""


# --------------------------------------------------------------------------- 语句拆分
def split_sql_statements(sql: str) -> list[str]:
    """把一段 SQL 脚本拆成可逐条执行的语句列表。

    处理规则（与 psql 的常见行为保持一致）：
    - `--` 行注释与 `/* */` 块注释中的 `;` 不参与拆分，注释内容原样保留；
    - 单引号 / 双引号字符串中的 `;` 不拆分，`''` 与 `""` 视为转义；
    - `$$ ... $$`（或 `$tag$ ... $tag$`）美元引用块整体保留，不拆分
      （函数体 / DO 块里的分号不会被误切）；
    - 末尾缺少 `;` 的最后一条语句同样会被返回；
    - 纯注释或纯空白的片段被丢弃。
    """
    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]

        # ---- 行注释：一直到行尾 ----
        if char == "-" and sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end == -1 else end
            buffer.append(sql[index:end])
            index = end
            continue

        # ---- 块注释（支持嵌套一层即可，Postgres 允许多层嵌套，这里按标准实现）----
        if char == "/" and sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            end = length if end == -1 else end + 2
            buffer.append(sql[index:end])
            index = end
            continue

        # ---- 单引号字符串 ----
        if char == "'":
            end = _scan_quoted(sql, index, "'")
            buffer.append(sql[index:end])
            index = end
            continue

        # ---- 双引号标识符 ----
        if char == '"':
            end = _scan_quoted(sql, index, '"')
            buffer.append(sql[index:end])
            index = end
            continue

        # ---- 美元引用块 ----
        if char == "$":
            match = _DOLLAR_TAG.match(sql, index)
            if match:
                tag = match.group(0)
                close = sql.find(tag, match.end())
                end = length if close == -1 else close + len(tag)
                buffer.append(sql[index:end])
                index = end
                continue

        # ---- 语句结束符 ----
        if char == ";":
            _flush(statements, buffer)
            index += 1
            continue

        buffer.append(char)
        index += 1

    _flush(statements, buffer)
    return statements


def _scan_quoted(sql: str, start: int, quote: str) -> int:
    """返回从 `start` 开始的引号字符串的结束位置（不含结束引号后的字符）。"""
    index = start + 1
    length = len(sql)
    while index < length:
        if sql[index] == quote:
            if index + 1 < length and sql[index + 1] == quote:  # 转义：'' 或 ""
                index += 2
                continue
            return index + 1
        index += 1
    return length


def _flush(statements: list[str], buffer: list[str]) -> None:
    """把缓冲区内容作为一条语句收尾（纯注释 / 空白则丢弃）。"""
    raw = "".join(buffer).strip()
    buffer.clear()
    if not raw:
        return
    if not _has_sql_content(raw):
        return
    statements.append(raw if raw.endswith(";") else raw + ";")


def _has_sql_content(chunk: str) -> bool:
    """判断片段除注释与空白外是否还有真实内容。"""
    stripped = re.sub(r"/\*.*?\*/", " ", chunk, flags=re.DOTALL)
    stripped = re.sub(r"--[^\n]*", " ", stripped)
    return bool(stripped.strip())


# --------------------------------------------------------------------------- 文件与校验
def discover_migrations(directory: str | Path | None = None) -> list[Path]:
    """按文件名升序列出迁移目录下的所有 `.sql` 文件。"""
    base = Path(directory) if directory is not None else MIGRATIONS_DIR
    if not base.exists():
        return []
    return sorted((p for p in base.iterdir() if p.is_file() and p.suffix == _SQL_SUFFIX), key=lambda p: p.name)


def file_checksum(path: str | Path) -> str:
    """迁移文件内容的 sha256（用于检测"应用后被改动"）。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _default_engine() -> Engine:
    """惰性获取默认引擎（避免导入本模块时连带创建数据库连接）。"""
    from app.db.session import engine as default_engine

    return default_engine


def _dialect_name(engine: Engine) -> str:
    return engine.dialect.name


def _is_sqlite(engine: Engine) -> bool:
    """方言判断只看 Engine，不看配置字符串。"""
    return _dialect_name(engine) == "sqlite"


def _fetch_applied(conn) -> dict[str, dict]:
    """读取 schema_migrations；表不存在时返回空字典。"""
    if not inspect(conn).has_table("schema_migrations"):
        return {}
    rows = conn.execute(
        text("SELECT version, applied_at, checksum, statements FROM schema_migrations")
    ).fetchall()
    return {
        str(row[0]): {
            "applied_at": row[1],
            "checksum": row[2],
            "statements": row[3],
        }
        for row in rows
    }


def _record_applied(conn, version: str, checksum: str, statements: int, applied_at) -> None:
    conn.execute(
        text(
            "INSERT INTO schema_migrations (version, applied_at, checksum, statements) "
            "VALUES (:version, :applied_at, :checksum, :statements)"
        ),
        {
            "version": version,
            "applied_at": applied_at,
            "checksum": checksum,
            "statements": statements,
        },
    )


def _to_iso(value) -> str | None:
    """把 applied_at 统一成字符串（Postgres 返回 datetime，SQLite 直接返回字符串）。"""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat(sep=" ")
    return str(value)


def _verify_checksums(files: list[Path], applied: dict[str, dict]) -> list[str]:
    """返回已应用且内容未被改动的版本列表；内容被改动则抛错。"""
    untouched: list[str] = []
    for path in files:
        record = applied.get(path.name)
        if record is None:
            continue
        current = file_checksum(path)
        recorded = record.get("checksum")
        if recorded and recorded != current:
            raise MigrationError(
                f"迁移文件 {path.name} 在应用之后被修改过，拒绝继续执行：\n"
                f"  已记录的 sha256: {recorded}\n"
                f"  当前文件的 sha256: {current}\n"
                "迁移文件一旦应用即为不可变历史，请新增一个迁移文件（例如 003_xxx.sql）"
                "来承载新的变更，或由管理员核对数据库实际结构后手工修正 schema_migrations 记录。"
            )
        untouched.append(path.name)
    return untouched


# --------------------------------------------------------------------------- 核心接口
def apply_migrations(
    engine: Engine | None = None,
    *,
    directory: str | Path | None = None,
    dry_run: bool = False,
    allow_sqlite: bool = False,
) -> list[str]:
    """执行尚未应用的迁移，返回本次应用的版本（文件名）列表。

    - `engine` 为空时使用 app.db.session 的默认引擎；
    - `directory` 指定迁移目录，默认 backend/migrations；
    - `dry_run=True` 只做校验与打印，不写入数据库；
    - `allow_sqlite=True` 仅供单元测试使用：在 SQLite 上强制执行
      （SQLite 下不会执行 Postgres DDL，正常业务请用 `python -m app.db.seed`）。
    """
    target = engine if engine is not None else _default_engine()

    if _is_sqlite(target) and not allow_sqlite:
        raise MigrationError(
            "检测到当前数据库方言为 SQLite，已拒绝执行 SQL 迁移：\n"
            "  migrations/*.sql 是 PostgreSQL 专用 DDL（pgvector、JSON、特定索引等）。\n"
            "  本地 / 测试环境的 SQLite 请改用：python -m app.db.seed\n"
            "  （该命令通过 Base.metadata.create_all 建表并写入演示数据，幂等可重复执行。）\n"
            "  生产环境请将 DATABASE_URL 指向 PostgreSQL 后再执行："
            "python -m app.db.migrate apply"
        )

    files = discover_migrations(directory)
    if not files:
        logger.warning("迁移目录中没有 .sql 文件: %s", directory or MIGRATIONS_DIR)
        return []

    planned: list[str] = []

    def _run(conn) -> None:
        applied = _fetch_applied(conn)
        _verify_checksums(files, applied)  # 先校验历史，再考虑新增
        for path in files:
            if path.name in applied:
                continue
            statements = split_sql_statements(path.read_text(encoding="utf-8"))
            checksum = file_checksum(path)
            if dry_run:
                logger.info("[dry-run] 将应用 %s（%d 条语句）", path.name, len(statements))
                planned.append(path.name)
                continue
            for statement in statements:
                conn.exec_driver_sql(statement)
            _record_applied(
                conn,
                path.name,
                checksum,
                len(statements),
                datetime.now(timezone.utc).replace(tzinfo=None),
            )
            logger.info("已应用迁移 %s（%d 条语句）", path.name, len(statements))
            planned.append(path.name)

    if dry_run:
        with target.connect() as conn:
            _run(conn)
    else:
        with target.begin() as conn:
            conn.exec_driver_sql(SCHEMA_MIGRATIONS_DDL)
            _run(conn)
    return planned


def status(engine: Engine | None = None, *, directory: str | Path | None = None) -> list[dict]:
    """列出每个迁移文件的应用状态（不修改数据库）。

    返回字段：version / applied / applied_at / changed / statements / checksum / path。
    `changed=True` 表示该文件已应用但内容被改动过（`apply` 会因此报错）。
    """
    target = engine if engine is not None else _default_engine()
    files = discover_migrations(directory)
    with target.connect() as conn:
        applied = _fetch_applied(conn)

    result: list[dict] = []
    for path in files:
        record = applied.get(path.name)
        current = file_checksum(path)
        recorded = record.get("checksum") if record else None
        result.append(
            {
                "version": path.name,
                "path": str(path),
                "applied": record is not None,
                "applied_at": _to_iso(record.get("applied_at") if record else None),
                "changed": bool(record is not None and recorded and recorded != current),
                "statements": record.get("statements") if record else None,
                "checksum": current,
            }
        )
    return result


# --------------------------------------------------------------------------- CLI
def _print_status(rows: list[dict], directory: str | Path | None) -> None:
    print(f"迁移目录: {Path(directory) if directory is not None else MIGRATIONS_DIR}")
    if not rows:
        print("  （目录中没有任何 .sql 文件）")
        return
    for row in rows:
        if row["changed"]:
            mark = "已应用但文件被修改!"
        elif row["applied"]:
            mark = f"已应用于 {row['applied_at']}"
        else:
            mark = "待应用"
        print(f"  [{mark}] {row['version']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.db.migrate",
        description="SQL 迁移工具（Postgres 路径；本地 SQLite 请用 python -m app.db.seed）",
    )
    parser.add_argument("--directory", default=None, help="迁移目录（默认 backend/migrations）")
    sub = parser.add_subparsers(dest="command", required=True)

    apply_parser = sub.add_parser("apply", help="应用尚未执行的迁移")
    apply_parser.add_argument("--dry-run", action="store_true", help="只校验并打印，不写入数据库")

    sub.add_parser("status", help="查看每个迁移文件的应用状态")
    sub.add_parser("list", help="仅列出迁移文件与语句数")

    args = parser.parse_args(argv)

    try:
        if args.command == "apply":
            engine = _default_engine()
            print(f"目标数据库方言: {_dialect_name(engine)}")
            if args.dry_run:
                print("模式: dry-run（不会写入数据库）")
            applied = apply_migrations(
                engine, directory=args.directory, dry_run=args.dry_run
            )
            if not applied:
                print("没有待应用的迁移，数据库已是最新版本。")
            else:
                verb = "将要应用" if args.dry_run else "已应用"
                print(f"{verb} {len(applied)} 个迁移：")
                for name in applied:
                    print(f"  - {name}")
            return 0

        if args.command == "status":
            rows = status(_default_engine(), directory=args.directory)
            _print_status(rows, args.directory)
            changed = [r["version"] for r in rows if r["changed"]]
            pending = [r["version"] for r in rows if not r["applied"]]
            if changed:
                print(f"警告：{len(changed)} 个已应用迁移的内容被修改：{', '.join(changed)}")
            print(f"共 {len(rows)} 个迁移，待应用 {len(pending)} 个。")
            return 1 if changed else 0

        files = discover_migrations(args.directory)
        print(f"迁移目录: {Path(args.directory) if args.directory else MIGRATIONS_DIR}")
        for path in files:
            count = len(split_sql_statements(path.read_text(encoding="utf-8")))
            print(f"  {path.name}  ({count} 条语句)")
        print(f"共 {len(files)} 个迁移文件。")
        return 0

    except MigrationError as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
