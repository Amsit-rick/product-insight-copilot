"""指标数据导入与质量校验（设计文档 8.2 节）。

    CSV/Excel -> 文件与格式检查 -> 字段映射 -> 指标口径校验
              -> 空值/重复/时间检查 -> 标准化写入 metric_snapshots -> 记录数据版本

设计取舍：
- 第一版用标准库解析 CSV（`pandas` 不在本地零依赖环境里，且这里只做逐行校验）；
- Excel 走 `openpyxl` 或 `rag.office` 的标准库回退实现；
- 有问题的行不会静默丢弃：逐行给出 issue，导入批次状态为 partial，
  质量标记写入 `quality_flags`，指标 Agent 必须在结论里如实报告。
"""

from __future__ import annotations

import csv
import io
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.logging_utils import get_logger
from app.models.enums import ImportStatus
from app.models.identity import User
from app.models.metrics import MetricDefinition, MetricImport, MetricSnapshot
from app.services.audit import record_audit

logger = get_logger(__name__)

CANONICAL_COLUMNS = (
    "metric_name",
    "platform",
    "app_version",
    "channel",
    "device",
    "stat_date",
    "value",
    "sample_size",
    "source_version",
)
DIMENSION_COLUMNS = ("platform", "app_version", "channel", "device")
REQUIRED_COLUMNS = ("metric_name", "stat_date", "value")

# 允许的列名别名 → 标准列名（企业数据源字段名千奇百怪，映射集中在这里）
COLUMN_ALIASES: dict[str, str] = {
    "metric": "metric_name",
    "metric_code": "metric_name",
    "indicator": "metric_name",
    "指标": "metric_name",
    "指标名": "metric_name",
    "dt": "stat_date",
    "date": "stat_date",
    "日期": "stat_date",
    "val": "value",
    "指标值": "value",
    "取值": "value",
    "样本量": "sample_size",
    "sample": "sample_size",
    "平台": "platform",
    "版本": "app_version",
    "渠道": "channel",
    "机型": "device",
    "设备": "device",
}


class MetricImportError(Exception):
    pass


class MetricImporter:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()

    # ------------------------------------------------------------------ 指标字典
    def ensure_definition(
        self,
        *,
        tenant_id: str,
        name: str,
        display_name: str | None = None,
        description: str | None = None,
        unit: str = "ratio",
        period: str = "day",
        allowed_dimensions: list[str] | None = None,
        higher_is_better: bool = True,
        source_version: str | None = None,
        numerator: str | None = None,
        denominator: str | None = None,
    ) -> MetricDefinition:
        row = (
            self.session.query(MetricDefinition)
            .filter(MetricDefinition.tenant_id == tenant_id)
            .filter(MetricDefinition.name == name)
            .first()
        )
        if row is None:
            row = MetricDefinition(tenant_id=tenant_id, name=name)
            self.session.add(row)
        row.display_name = display_name or row.display_name or name
        row.description = description or row.description
        row.unit = unit
        row.period = period
        row.allowed_dimensions = allowed_dimensions or row.allowed_dimensions or list(DIMENSION_COLUMNS)
        row.higher_is_better = higher_is_better
        row.source_version = source_version or row.source_version
        if numerator:
            row.numerator = numerator
        if denominator:
            row.denominator = denominator
        row.is_active = True
        self.session.flush()
        return row

    # ------------------------------------------------------------------ 主流程
    def import_bytes(
        self,
        *,
        tenant_id: str,
        filename: str,
        data: bytes,
        dataset_version: str | None = None,
        user: User | None = None,
    ) -> dict:
        dataset_version = dataset_version or f"metric_snapshot_{datetime.now():%Y%m%d%H%M%S}"
        rows = self._read_rows(filename, data)
        if not rows:
            raise MetricImportError("文件中没有可解析的数据行")

        header, records = rows[0], rows[1:]
        mapping, unknown_columns = _map_columns(header)
        missing = [c for c in REQUIRED_COLUMNS if c not in mapping.values()]
        if missing:
            raise MetricImportError(
                f"缺少必需列 {missing}；实际表头为 {header}（支持别名映射：{sorted(COLUMN_ALIASES)}）"
            )

        definitions = {
            row.name: row
            for row in self.session.query(MetricDefinition)
            .filter(MetricDefinition.tenant_id == tenant_id)
            .all()
        }

        issues: list[dict] = []
        parsed: list[dict] = []
        seen_keys: dict[tuple, int] = {}
        duplicates = 0

        for offset, record in enumerate(records, start=2):  # 表头占第 1 行
            mapped = _apply_mapping(record, mapping)
            if not any(str(v).strip() for v in mapped.values()):
                continue
            item, row_issues = self._validate_row(
                mapped, line=offset, definitions=definitions, dataset_version=dataset_version
            )
            issues.extend(row_issues)
            if item is None:
                continue
            key = (
                item["metric_name"],
                item["platform"],
                item["app_version"],
                item["channel"],
                item["device"],
                item["stat_date"],
            )
            if key in seen_keys:
                duplicates += 1
                issues.append(
                    {
                        "row": offset,
                        "level": "warning",
                        "code": "duplicate_row",
                        "message": f"与第 {seen_keys[key]} 行的维度组合+日期重复，按「保留最新」处理",
                        "raw": mapped,
                    }
                )
                parsed = [p for p in parsed if p["_key"] != key]
            seen_keys[key] = offset
            parsed.append(item)

        if unknown_columns:
            issues.append(
                {
                    "row": None,
                    "level": "info",
                    "code": "unknown_columns",
                    "message": f"以下列未识别，已忽略：{unknown_columns}",
                }
            )

        if not parsed:
            raise MetricImportError("所有数据行都未通过校验，未写入任何指标数据")

        missing_days = _missing_days(parsed)
        quality_flags = _aggregate_flags(parsed, missing_days, duplicates)

        import_row = MetricImport(
            tenant_id=tenant_id,
            file_name=filename,
            dataset_version=dataset_version,
            status=ImportStatus.PENDING.value,
            rows_total=len(records),
            rows_valid=len(parsed),
            rows_invalid=len(records) - len(parsed) - duplicates,
            report={
                "issues": issues[:500],
                "missing_days": missing_days,
                "duplicates": duplicates,
                "quality_flags": quality_flags,
                "unknown_columns": unknown_columns,
                "header": header,
                "mapping": mapping,
            },
            created_by=user.id if user else None,
        )
        self.session.add(import_row)
        self.session.flush()

        written = self._write_snapshots(tenant_id, parsed, dataset_version)
        has_errors = any(i["level"] == "error" for i in issues)
        import_row.status = (
            ImportStatus.PARTIAL.value
            if has_errors or issues
            else ImportStatus.SUCCESS.value
        )
        self.session.flush()

        record_audit(
            self.session,
            action="metric_imported",
            tenant_id=tenant_id,
            actor_id=user.id if user else None,
            resource_type="metric_import",
            resource_id=import_row.id,
            outcome=import_row.status,
            detail={
                "file_name": filename,
                "dataset_version": dataset_version,
                "rows_total": len(records),
                "rows_valid": len(parsed),
                "rows_invalid": import_row.rows_invalid,
                "duplicates": duplicates,
                "missing_days": missing_days,
                "quality_flags": quality_flags,
            },
        )
        logger.info(
            "指标导入完成 file=%s rows=%s/%s duplicates=%s flags=%s",
            filename,
            len(parsed),
            len(records),
            duplicates,
            quality_flags,
        )
        return {
            "import_id": import_row.id,
            "file_name": filename,
            "dataset_version": dataset_version,
            "status": import_row.status,
            "rows_total": len(records),
            "rows_valid": len(parsed),
            "rows_invalid": import_row.rows_invalid,
            "duplicates": duplicates,
            "missing_days": missing_days,
            "quality_flags": quality_flags,
            "issues": issues[:200],
            "metric_names": sorted({p["metric_name"] for p in parsed}),
            "date_range": _date_range(parsed),
            "defined_metrics_unknown": sorted(
                {p["metric_name"] for p in parsed if p["metric_name"] not in definitions}
            ),
            "written": written,
        }

    # ------------------------------------------------------------------ 解析
    def _read_rows(self, filename: str, data: bytes) -> list[list[str]]:
        suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        if suffix in {"csv", "txt"}:
            text = _decode(data)
            reader = csv.reader(io.StringIO(text))
            return [[cell.strip() for cell in row] for row in reader]
        if suffix in {"xlsx", "xlsm"}:
            from app.rag.office import OfficeParseError, extract_xlsx

            try:
                sheets = extract_xlsx_bytes(data)
            except OfficeParseError as exc:
                raise MetricImportError(str(exc)) from exc
            rows: list[list[str]] = []
            for _title, sheet_rows in sheets.items():
                rows.extend(sheet_rows)
            return rows
        raise MetricImportError(f"不支持的指标文件类型: {filename}")

    def _validate_row(
        self,
        mapped: dict[str, str],
        *,
        line: int,
        definitions: dict[str, MetricDefinition],
        dataset_version: str,
    ) -> tuple[dict | None, list[dict]]:
        issues: list[dict] = []
        metric_name = (mapped.get("metric_name") or "").strip()
        if not metric_name:
            return None, [_issue(line, "error", "missing_metric_name", "缺少指标名", mapped)]

        stat_date, date_error = _parse_date(mapped.get("stat_date"))
        if date_error:
            return None, [_issue(line, "error", "invalid_date", date_error, mapped)]

        value, value_error = _parse_float(mapped.get("value"))
        if value_error:
            return None, [_issue(line, "error", "invalid_value", value_error, mapped)]

        sample_size = None
        if mapped.get("sample_size"):
            sample_size, sample_error = _parse_int(mapped.get("sample_size"))
            if sample_error:
                issues.append(_issue(line, "warning", "invalid_sample_size", sample_error, mapped))

        definition = definitions.get(metric_name)
        quality_flags: list[str] = list(mapped.get("_flags") or [])
        if definition is None:
            issues.append(
                _issue(
                    line,
                    "warning",
                    "metric_not_defined",
                    f"指标 {metric_name} 未在指标字典中登记，已按原始口径入库",
                    mapped,
                )
            )
            quality_flags.append("undefined_metric")
        else:
            # 口径校验：违反指标声明的取值范围时**拒绝该行**而不是"标记后入库"，
            # 否则错误数据会直接污染指标查询与 Agent 结论。
            if definition.unit == "ratio" and value > self.settings.metric_ratio_bounds:
                return None, [
                    _issue(
                        line,
                        "error",
                        "value_out_of_range",
                        f"比例类指标 {metric_name} 取值 {value} 超出合理上界 "
                        f"{self.settings.metric_ratio_bounds}，已拒绝该行",
                        mapped,
                    )
                ]
            if definition.unit == "ratio" and value < 0:
                return None, [
                    _issue(
                        line,
                        "error",
                        "negative_value",
                        f"指标 {metric_name} 取值 {value} 为负数，已拒绝该行",
                        mapped,
                    )
                ]
            allowed = definition.allowed_dimensions or list(DIMENSION_COLUMNS)
            for dimension in DIMENSION_COLUMNS:
                if mapped.get(dimension) and dimension not in allowed:
                    quality_flags.append(f"dimension_not_allowed:{dimension}")
                    issues.append(
                        _issue(
                            line,
                            "warning",
                            "dimension_not_allowed",
                            f"指标 {metric_name} 的口径未声明维度 {dimension}：保留原值以保证数据"
                            f"保真，但该维度上的分群结论不可信（已记质量标记）",
                            mapped,
                        )
                    )

        if sample_size is not None and sample_size < 500:
            quality_flags.append("small_sample")
            issues.append(
                _issue(
                    line,
                    "warning",
                    "small_sample",
                    f"样本量仅 {sample_size}，该点统计上不显著",
                    mapped,
                )
            )

        item = {
            "metric_name": metric_name,
            "platform": _blank_to_none(mapped.get("platform")),
            "app_version": _blank_to_none(mapped.get("app_version")),
            "channel": _blank_to_none(mapped.get("channel")),
            "device": _blank_to_none(mapped.get("device")),
            "stat_date": stat_date,
            "value": value,
            "sample_size": sample_size,
            "source_version": _blank_to_none(mapped.get("source_version")) or dataset_version,
            "quality_flags": sorted(set(quality_flags)),
        }
        item["_key"] = (
            item["metric_name"],
            item["platform"],
            item["app_version"],
            item["channel"],
            item["device"],
            item["stat_date"],
        )
        return item, issues

    def _write_snapshots(self, tenant_id: str, parsed: list[dict], dataset_version: str) -> int:
        """同一维度组合+日期已存在时覆盖（幂等重跑同一批数据不会产生重复行）。"""
        keys = [p["_key"] for p in parsed]
        existing: dict[tuple, MetricSnapshot] = {}
        chunk = 200
        for start in range(0, len(keys), chunk):
            batch = keys[start : start + chunk]
            names = {k[0] for k in batch}
            dates = {k[5] for k in batch}
            rows = (
                self.session.query(MetricSnapshot)
                .filter(MetricSnapshot.tenant_id == tenant_id)
                .filter(MetricSnapshot.metric_name.in_(names))
                .filter(MetricSnapshot.stat_date.in_(dates))
                .all()
            )
            for row in rows:
                existing[
                    (
                        row.metric_name,
                        row.platform,
                        row.app_version,
                        row.channel,
                        row.device,
                        row.stat_date,
                    )
                ] = row

        written = 0
        for item in parsed:
            key = item["_key"]
            row = existing.get(key)
            if row is None:
                row = MetricSnapshot(tenant_id=tenant_id, metric_name=item["metric_name"], stat_date=item["stat_date"])
                self.session.add(row)
                existing[key] = row
            row.platform = item["platform"]
            row.app_version = item["app_version"]
            row.channel = item["channel"]
            row.device = item["device"]
            row.value = item["value"]
            row.sample_size = item["sample_size"]
            row.source_version = item["source_version"]
            row.dataset_version = dataset_version
            row.quality_flags = item["quality_flags"]
            written += 1
        self.session.flush()
        return written


# --------------------------------------------------------------------------- 辅助
def extract_xlsx_bytes(data: bytes):
    """从内存字节解析 xlsx（复用 rag.office 的实现，避免先落盘）。"""
    import tempfile
    import os
    from pathlib import Path

    from app.rag.office import extract_xlsx

    directory = Path(os.environ.get("PIC_TMP_DIR") or tempfile.gettempdir())
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"metric_{uuid.uuid4().hex}.xlsx"
    try:
        path.write_bytes(data)
        return extract_xlsx(path)
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:  # pragma: no cover
            pass


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _map_columns(header: list[str]) -> tuple[dict[int, str], list[str]]:
    mapping: dict[int, str] = {}
    unknown: list[str] = []
    for index, raw in enumerate(header):
        name = (raw or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in CANONICAL_COLUMNS:
            mapping[index] = key
        elif key in COLUMN_ALIASES:
            mapping[index] = COLUMN_ALIASES[key]
        elif name in COLUMN_ALIASES:
            mapping[index] = COLUMN_ALIASES[name]
        else:
            unknown.append(name)
    return mapping, unknown


def _apply_mapping(record: list[str], mapping: dict[int, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for index, column in mapping.items():
        out[column] = record[index] if index < len(record) else ""
    return out


def _parse_date(value) -> tuple[date | None, str | None]:
    text = str(value or "").strip()
    if not text:
        return None, "缺少日期"
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10] if fmt != "%Y%m%d" else text[:8], fmt).date(), None
        except ValueError:
            continue
    return None, f"日期格式无法识别: {text}"


def _parse_float(value) -> tuple[float | None, str | None]:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None, "缺少指标值"
    try:
        return float(text.rstrip("%")) / (100 if text.endswith("%") else 1), None
    except ValueError:
        return None, f"指标值不是数字: {text}"


def _parse_int(value) -> tuple[int | None, str | None]:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None, None
    try:
        return int(float(text)), None
    except ValueError:
        return None, f"样本量不是整数: {text}"


def _blank_to_none(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def _issue(line: int | None, level: str, code: str, message: str, raw: dict | None = None) -> dict:
    return {"row": line, "level": level, "code": code, "message": message, "raw": raw}


def _missing_days(parsed: list[dict]) -> list[str]:
    """在已有数据的日期区间内，找出完全没有数据的日期。"""
    dates = sorted({p["stat_date"] for p in parsed if p["stat_date"]})
    if len(dates) < 2:
        return []
    present = set(dates)
    missing: list[str] = []
    cursor = dates[0]
    while cursor <= dates[-1]:
        if cursor not in present:
            missing.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return missing


def _aggregate_flags(parsed: list[dict], missing_days: list[str], duplicates: int) -> list[str]:
    flags: set[str] = set()
    for item in parsed:
        flags.update(item.get("quality_flags") or [])
    if missing_days:
        flags.add("missing_days")
    if duplicates:
        flags.add("duplicate_rows")
    by_coverage: dict[str, set] = defaultdict(set)
    for item in parsed:
        by_coverage[item["metric_name"]].add(item["platform"])
    return sorted(flags)


def _date_range(parsed: list[dict]) -> dict[str, str | None]:
    dates = sorted({p["stat_date"] for p in parsed if p["stat_date"]})
    if not dates:
        return {"min": None, "max": None}
    return {"min": dates[0].isoformat(), "max": dates[-1].isoformat()}
