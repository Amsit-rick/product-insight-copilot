"""指标导入（`app.services.metric_import.MetricImporter`）单元测试。

链路：CSV 字节 → 字段映射 → 口径校验 → 标准化写入 metric_snapshots → 记录批次与数据版本。
用例全部使用临时 SQLite（见 `tests/conftest.py`），CSV 数据在用例内现造。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.models.metrics import MetricImport, MetricSnapshot
from app.services.metric_import import MetricImportError, MetricImporter
from tests.conftest import TENANT

HEADER = "metric_name,platform,app_version,stat_date,value,sample_size"


def _csv(rows: list[str]) -> bytes:
    """把若干行文本拼成 CSV 字节（含表头行）。"""
    return ("\n".join(rows) + "\n").encode("utf-8")


def _import(importer: MetricImporter, rows: list[str], **kwargs) -> dict:
    return importer.import_bytes(
        tenant_id=kwargs.pop("tenant_id", TENANT),
        filename=kwargs.pop("filename", "metrics.csv"),
        data=_csv(rows),
        **kwargs,
    )


def _snapshots(session, **filters) -> list[MetricSnapshot]:
    query = session.query(MetricSnapshot)
    for key, value in filters.items():
        query = query.filter(getattr(MetricSnapshot, key) == value)
    return query.all()


def _issue_codes(result: dict) -> list[str]:
    return [issue["code"] for issue in result["issues"]]


@pytest.fixture
def importer(session) -> MetricImporter:
    return MetricImporter(session)


@pytest.fixture
def defined_metric(importer) -> None:
    """登记一个比例类指标口径，避免用例被"未登记"告警干扰。"""
    importer.ensure_definition(
        tenant_id=TENANT, name="retention_d1", display_name="次日留存", unit="ratio"
    )


# --------------------------------------------------------------------------- 正常路径
def test_正常导入写入快照并记录数据版本(importer, session, defined_metric):
    result = _import(
        importer,
        [
            HEADER,
            "retention_d1,android,3.2.0,2024-03-01,0.42,1200",
            "retention_d1,ios,3.2.0,2024-03-01,0.38,900",
        ],
        dataset_version="ds-20240301",
    )

    assert result["status"] == "success"
    assert result["rows_total"] == 2
    assert result["rows_valid"] == 2
    assert result["rows_invalid"] == 0
    assert result["duplicates"] == 0
    assert result["written"] == 2
    assert result["quality_flags"] == []
    assert result["issues"] == []
    assert result["metric_names"] == ["retention_d1"]
    assert result["date_range"] == {"min": "2024-03-01", "max": "2024-03-01"}
    assert result["defined_metrics_unknown"] == []

    rows = _snapshots(session, tenant_id=TENANT)
    assert len(rows) == 2
    android = next(row for row in rows if row.platform == "android")
    assert android.value == pytest.approx(0.42)
    assert android.sample_size == 1200
    assert android.app_version == "3.2.0"
    assert android.stat_date == date(2024, 3, 1)
    assert android.dataset_version == "ds-20240301"
    # 没有单独给来源版本时，来源版本落到数据版本
    assert android.source_version == "ds-20240301"

    # 批次记录与审计同时落库
    batch = session.query(MetricImport).one()
    assert batch.status == "success"
    assert batch.rows_valid == 2
    assert batch.dataset_version == "ds-20240301"


def test_不传数据版本时自动生成(importer, defined_metric):
    result = _import(importer, [HEADER, "retention_d1,android,,2024-03-01,0.42,1200"])

    assert result["dataset_version"].startswith("metric_snapshot_")
    assert result["status"] == "success"


# --------------------------------------------------------------------------- 列别名映射
def test_中文列名别名映射(importer, session, defined_metric):
    result = _import(
        importer,
        ["指标名,平台,日期,指标值", "retention_d1,android,2024/03/01,0.42"],
    )

    assert result["status"] == "success"
    assert result["written"] == 1
    row = _snapshots(session, tenant_id=TENANT)[0]
    assert row.metric_name == "retention_d1"
    assert row.platform == "android"
    assert row.stat_date == date(2024, 3, 1)
    assert row.value == pytest.approx(0.42)

    # 映射关系会记录在导入批次里，便于追溯"这列到底被当成什么"
    batch = session.query(MetricImport).one()
    assert sorted(batch.report["mapping"].values()) == [
        "metric_name",
        "platform",
        "stat_date",
        "value",
    ]


def test_未识别的列忽略但留info(importer, defined_metric):
    result = _import(
        importer,
        ["指标名,平台,日期,指标值,备注", "retention_d1,android,2024-03-01,0.42,口径调整"],
    )

    assert result["written"] == 1
    unknown = [issue for issue in result["issues"] if issue["code"] == "unknown_columns"]
    assert len(unknown) == 1
    assert unknown[0]["level"] == "info"
    assert "备注" in unknown[0]["message"]
    # 只要有任何 issue（哪怕只是 info），批次就标为 partial 而不是 success
    assert result["status"] == "partial"


def test_缺少必需列的报错信息带上表头与别名提示(importer):
    with pytest.raises(MetricImportError) as excinfo:
        _import(importer, ["指标名,平台,日期", "retention_d1,android,2024-03-01"])

    message = str(excinfo.value)
    assert "缺少必需列" in message
    assert "value" in message
    assert "指标名" in message


def test_空文件报错(importer):
    with pytest.raises(MetricImportError, match="没有可解析的数据行"):
        importer.import_bytes(tenant_id=TENANT, filename="empty.csv", data=b"")


def test_不支持的文件类型报错(importer, defined_metric):
    with pytest.raises(MetricImportError, match="不支持的指标文件类型"):
        importer.import_bytes(
            tenant_id=TENANT,
            filename="metrics.json",
            data=_csv([HEADER, "retention_d1,android,2024-03-01,0.42,1200"]),
        )


# --------------------------------------------------------------------------- 逐行校验
def test_非法行产生error且不写入(importer, session, defined_metric):
    result = _import(
        importer,
        [
            HEADER,
            "retention_d1,android,,2024-03-01,0.42,1200",
            "retention_d1,ios,,2024-13-45,0.38,900",
            "retention_d1,harmony,,2024-03-01,not-a-number,800",
        ],
    )

    assert result["status"] == "partial"
    assert result["rows_valid"] == 1
    assert result["rows_invalid"] == 2
    errors = {issue["code"]: issue for issue in result["issues"] if issue["level"] == "error"}
    assert set(errors) == {"invalid_date", "invalid_value"}
    assert errors["invalid_date"]["row"] == 3  # 表头占第 1 行，行号即文件行号
    assert "2024-13-45" in errors["invalid_date"]["message"]
    assert "not-a-number" in errors["invalid_value"]["message"]

    rows = _snapshots(session, tenant_id=TENANT)
    assert [(row.platform, row.value) for row in rows] == [("android", pytest.approx(0.42))]


def test_缺少指标名的行直接丢弃(importer, defined_metric):
    result = _import(
        importer,
        [HEADER, "retention_d1,android,,2024-03-01,0.42,1200", ",ios,,2024-03-01,0.5,900"],
    )

    assert result["rows_valid"] == 1
    assert "missing_metric_name" in _issue_codes(result)


def test_比例类指标超出合理上界被拒绝写入(importer, session, defined_metric):
    """口径异常的行按 error 拒绝入库（只保留合法行），否则脏数据会污染指标查询。"""
    result = _import(
        importer,
        [
            HEADER,
            "retention_d1,android,,2024-03-01,0.42,1200",
            "retention_d1,ios,,2024-03-01,2.5,1200",
        ],
    )

    assert result["status"] == "partial"
    assert result["rows_valid"] == 1
    assert result["rows_invalid"] == 1
    errors = [issue for issue in result["issues"] if issue["code"] == "value_out_of_range"]
    assert len(errors) == 1
    assert errors[0]["level"] == "error"
    assert errors[0]["row"] == 3
    assert "超出合理上界" in errors[0]["message"]

    rows = _snapshots(session, tenant_id=TENANT)
    assert [row.platform for row in rows] == ["android"]


def test_比例类指标不允许负值(importer, session, defined_metric):
    result = _import(
        importer,
        [
            HEADER,
            "retention_d1,android,,2024-03-01,0.42,1200",
            "retention_d1,ios,,2024-03-01,-0.1,1200",
        ],
    )

    assert "negative_value" in _issue_codes(result)
    assert result["rows_invalid"] == 1
    assert [row.platform for row in _snapshots(session, tenant_id=TENANT)] == ["android"]


def test_未登记指标按原始口径入库但给出告警(importer, session):
    result = _import(importer, [HEADER, "brand_new_metric,android,,2024-03-01,0.42,1200"])

    assert result["status"] == "partial"
    assert result["defined_metrics_unknown"] == ["brand_new_metric"]
    assert "metric_not_defined" in _issue_codes(result)
    assert result["quality_flags"] == ["undefined_metric"]
    assert len(_snapshots(session, tenant_id=TENANT)) == 1


def test_维度未在口径中声明时只告警不拒绝(importer, session):
    importer.ensure_definition(
        tenant_id=TENANT, name="retention_d1", unit="ratio", allowed_dimensions=["platform"]
    )
    result = _import(importer, [HEADER, "retention_d1,android,3.2.0,2024-03-01,0.42,1200"])

    assert result["status"] == "partial"
    assert "dimension_not_allowed" in _issue_codes(result)
    assert "dimension_not_allowed:app_version" in result["quality_flags"]
    # 只告警不拒绝：原始值照常入库，口径外的维度由下游按指标声明决定是否使用
    assert result["written"] == 1
    assert _snapshots(session, tenant_id=TENANT)[0].app_version == "3.2.0"


def test_全部行不合法时整批拒绝(importer, defined_metric):
    with pytest.raises(MetricImportError, match="所有数据行都未通过校验"):
        _import(importer, [HEADER, "retention_d1,android,,bad-date,0.42,1200"])


# --------------------------------------------------------------------------- 重复与缺口
def test_重复维度日期保留后一行并计数(importer, session, defined_metric):
    result = _import(
        importer,
        [
            HEADER,
            "retention_d1,android,,2024-03-01,0.40,1200",
            "retention_d1,android,,2024-03-01,0.90,1300",
        ],
    )

    assert result["duplicates"] == 1
    assert result["rows_valid"] == 1
    assert result["rows_invalid"] == 0  # 重复行不算"无效行"
    assert "duplicate_rows" in result["quality_flags"]
    warnings = [issue for issue in result["issues"] if issue["code"] == "duplicate_row"]
    assert len(warnings) == 1 and warnings[0]["level"] == "warning"
    assert "保留最新" in warnings[0]["message"]

    rows = _snapshots(session, tenant_id=TENANT)
    assert len(rows) == 1
    assert rows[0].value == pytest.approx(0.9)  # 后一行胜出


def test_范围内缺失的日期被报出来并打上质量标记(importer, defined_metric):
    result = _import(
        importer,
        [
            HEADER,
            "retention_d1,android,,2024-03-01,0.42,1200",
            "retention_d1,ios,,2024-03-03,0.38,900",
        ],
    )

    assert result["missing_days"] == ["2024-03-02"]
    assert "missing_days" in result["quality_flags"]
    assert result["date_range"] == {"min": "2024-03-01", "max": "2024-03-03"}


def test_只有一天数据时不判定缺失日期(importer, defined_metric):
    result = _import(importer, [HEADER, "retention_d1,android,,2024-03-01,0.42,1200"])

    assert result["missing_days"] == []
    assert result["quality_flags"] == []


def test_样本量过小打上small_sample(importer, defined_metric):
    result = _import(importer, [HEADER, "retention_d1,android,,2024-03-01,0.42,120"])

    assert "small_sample" in result["quality_flags"]
    assert "small_sample" in _issue_codes(result)
    assert result["status"] == "partial"


def test_样本量非整数只告警不影响入库(importer, session, defined_metric):
    result = _import(importer, [HEADER, "retention_d1,android,,2024-03-01,0.42,约一千"])

    assert "invalid_sample_size" in _issue_codes(result)
    assert result["written"] == 1
    assert _snapshots(session, tenant_id=TENANT)[0].sample_size is None


# --------------------------------------------------------------------------- 幂等
def test_同一文件同一数据版本重复导入幂等(importer, session, defined_metric):
    rows = [
        HEADER,
        "retention_d1,android,,2024-03-01,0.42,1200",
        "retention_d1,ios,,2024-03-02,0.38,900",
    ]

    first = _import(importer, rows, dataset_version="ds-1")
    second = _import(importer, rows, dataset_version="ds-1")

    assert first["written"] == 2 and second["written"] == 2
    snapshots = _snapshots(session, tenant_id=TENANT)
    assert len(snapshots) == 2  # 覆盖而非新增
    assert {row.dataset_version for row in snapshots} == {"ds-1"}
    # 但导入批次必须留两条痕迹，便于审计"这份数据被导入过几次"
    assert session.query(MetricImport).count() == 2


def test_改了值之后重复导入以最新为准(importer, session, defined_metric):
    _import(importer, [HEADER, "retention_d1,android,,2024-03-01,0.42,1200"], dataset_version="ds-1")
    _import(importer, [HEADER, "retention_d1,android,,2024-03-01,0.55,1200"], dataset_version="ds-2")

    rows = _snapshots(session, tenant_id=TENANT)
    assert len(rows) == 1
    assert rows[0].value == pytest.approx(0.55)
    assert rows[0].dataset_version == "ds-2"


def test_不同租户互不覆盖(importer, session, defined_metric):
    _import(importer, [HEADER, "retention_d1,android,,2024-03-01,0.42,1200"], tenant_id="tenant-a")
    _import(importer, [HEADER, "retention_d1,android,,2024-03-01,0.9,1200"], tenant_id="tenant-b")

    assert len(_snapshots(session, tenant_id="tenant-a")) == 1
    assert len(_snapshots(session, tenant_id="tenant-b")) == 1


# --------------------------------------------------------------------------- 指标字典
def test_ensure_definition_可重复调用且更新口径(importer, session):
    first = importer.ensure_definition(
        tenant_id=TENANT, name="retention_d1", display_name="次日留存", unit="ratio"
    )
    second = importer.ensure_definition(
        tenant_id=TENANT, name="retention_d1", display_name="次日留存率", unit="ratio", period="week"
    )

    assert first.id == second.id
    assert second.display_name == "次日留存率"
    assert second.period == "week"
    assert session.query(type(second)).count() == 1
