"""指标服务（`app.metrics.service.SqlMetricService`）单元测试。

全部用例跑在临时 SQLite 上（见 `tests/conftest.py`），不接触开发库。
重点验证四类受控能力的真实语义：加权日聚合、版本对比与发布日切分、
维度分群与占比、异常识别，以及版本基线挑选等辅助能力。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.metrics.schemas import (
    AnomalyRequest,
    BreakdownRequest,
    TrendRequest,
    VersionCompareRequest,
)
from app.metrics.service import SqlMetricService, pick_baseline_version
from tests.conftest import TENANT

D1 = date(2024, 3, 1)


@pytest.fixture
def service(session) -> SqlMetricService:
    return SqlMetricService(session, tenant_id=TENANT)


# --------------------------------------------------------------------------- 时间趋势
def test_get_trend_同日多维度按样本量加权平均(service, add_snapshot):
    """同一天 Android/ iOS 两行按 sample_size 加权，而不是简单平均。"""
    add_snapshot(stat_date=D1, value=0.4, platform="android", sample_size=100)
    add_snapshot(stat_date=D1, value=0.6, platform="ios", sample_size=300)
    add_snapshot(stat_date=date(2024, 3, 2), value=0.5, platform="android")

    result = service.get_trend(
        TrendRequest(metric_name="retention_d1", start_date=D1, end_date=date(2024, 3, 3))
    )

    assert [row["stat_date"] for row in result.rows] == ["2024-03-01", "2024-03-02"]
    # (0.4*100 + 0.6*300) / 400 = 0.55
    assert result.rows[0]["value"] == pytest.approx(0.55)
    assert result.rows[0]["points"] == 2
    assert result.rows[1]["value"] == pytest.approx(0.5)
    assert result.rows[1]["points"] == 1
    assert result.capability == "get_trend"
    assert result.available is True


def test_get_trend_摘要字段与首末差值(service, add_snapshot):
    add_snapshot(stat_date=D1, value=0.5)
    add_snapshot(stat_date=date(2024, 3, 2), value=0.4)
    add_snapshot(stat_date=date(2024, 3, 3), value=0.3)

    summary = service.get_trend(
        TrendRequest(metric_name="retention_d1", start_date=D1, end_date=date(2024, 3, 4))
    ).summary

    assert summary["points"] == 3
    assert summary["start_value"] == pytest.approx(0.5)
    assert summary["end_value"] == pytest.approx(0.3)
    assert summary["delta"] == pytest.approx(-0.2)
    assert summary["pct_change"] == pytest.approx(-0.4)
    assert summary["min_value"] == pytest.approx(0.3)
    assert summary["max_value"] == pytest.approx(0.5)
    assert summary["avg_value"] == pytest.approx(0.4)


def test_get_trend_无数据时给出缺失告警(service):
    result = service.get_trend(
        TrendRequest(metric_name="not_exist", start_date=D1, end_date=date(2024, 3, 5))
    )

    assert result.rows == []
    assert result.available is False
    assert result.summary == {"points": 0}
    assert result.warnings == ["指定条件下没有指标数据（数据缺失）"]


def test_get_trend_按维度过滤(service, add_snapshot):
    add_snapshot(stat_date=D1, value=0.4, platform="android")
    add_snapshot(stat_date=D1, value=0.6, platform="ios")

    only_android = service.get_trend(
        TrendRequest(metric_name="retention_d1", start_date=D1, end_date=D1, platform="android")
    )

    assert [row["value"] for row in only_android.rows] == [0.4]


def test_get_trend_按租户隔离(service, add_snapshot):
    """别的租户的数据不能出现在结果里。"""
    add_snapshot(stat_date=D1, value=0.4)
    add_snapshot(stat_date=D1, value=0.9, tenant_id="other-tenant")

    rows = service.get_trend(
        TrendRequest(metric_name="retention_d1", start_date=D1, end_date=D1)
    ).rows

    assert len(rows) == 1
    assert rows[0]["value"] == pytest.approx(0.4)


def test_get_trend_带出数据版本与质量标记(service, add_snapshot):
    add_snapshot(stat_date=D1, value=0.4, dataset_version="ds-002", source_version="src-1")
    add_snapshot(stat_date=D1, value=0.4, platform="ios", dataset_version="ds-001")
    add_snapshot(
        stat_date=date(2024, 3, 2), value=0.4, dataset_version="ds-001", quality_flags=["small_sample"]
    )

    result = service.get_trend(
        TrendRequest(metric_name="retention_d1", start_date=D1, end_date=date(2024, 3, 3))
    )

    assert result.dataset_version == "ds-002"  # 取字典序最大的版本
    assert result.source_version == "src-1"
    assert result.quality_flags == ["small_sample"]


def test_get_trend_时间窗外的数据不参与(service, add_snapshot):
    add_snapshot(stat_date=date(2024, 2, 28), value=0.1)
    add_snapshot(stat_date=D1, value=0.4)
    add_snapshot(stat_date=date(2024, 3, 9), value=0.9)

    rows = service.get_trend(
        TrendRequest(metric_name="retention_d1", start_date=D1, end_date=date(2024, 3, 3))
    ).rows

    assert [row["value"] for row in rows] == [0.4]


# --------------------------------------------------------------------------- 版本对比
@pytest.fixture
def versioned(service, add_snapshot):
    """3.1.0（均值 0.51）与 3.2.0（均值 0.41，发布日 3-04 后掉到 0.40）两版数据。"""
    add_snapshot(stat_date=D1, value=0.50, app_version="3.1.0")
    add_snapshot(stat_date=date(2024, 3, 2), value=0.52, app_version="3.1.0")
    add_snapshot(stat_date=date(2024, 3, 3), value=0.42, app_version="3.2.0")
    add_snapshot(stat_date=date(2024, 3, 4), value=0.40, app_version="3.2.0")
    return service


def test_compare_versions_基线目标均值与百分比变化(versioned):
    result = versioned.compare_versions(
        VersionCompareRequest(
            metric_name="retention_d1",
            baseline_version="3.1.0",
            target_version="3.2.0",
            start_date=D1,
            end_date=date(2024, 3, 4),
        )
    )

    summary = result.summary
    assert summary["baseline_avg"] == pytest.approx(0.51)
    assert summary["target_avg"] == pytest.approx(0.41)
    assert summary["baseline_points"] == 2
    assert summary["target_points"] == 2
    assert summary["delta"] == pytest.approx(-0.1)
    assert summary["pct_change"] == pytest.approx(-0.1 / 0.51, abs=1e-6)
    assert result.warnings == []
    assert result.available is True
    # 两个版本的行都返回，并标明所属版本
    assert {row["app_version"] for row in result.rows} == {"3.1.0", "3.2.0"}


def test_compare_versions_发布日切分前后(versioned):
    summary = versioned.compare_versions(
        VersionCompareRequest(
            metric_name="retention_d1",
            baseline_version="3.1.0",
            target_version="3.2.0",
            start_date=D1,
            end_date=date(2024, 3, 4),
            release_date=date(2024, 3, 4),
        )
    ).summary

    assert summary["pre_release_avg"] == pytest.approx(0.42)  # 03-03
    assert summary["post_release_avg"] == pytest.approx(0.40)  # 03-04
    assert summary["release_delta"] == pytest.approx(-0.02)


def test_compare_versions_缺少一侧数据时告警且不给差值(versioned):
    result = versioned.compare_versions(
        VersionCompareRequest(
            metric_name="retention_d1",
            baseline_version="2.0.0",
            target_version="3.2.0",
            start_date=D1,
            end_date=date(2024, 3, 4),
        )
    )

    assert result.summary["baseline_avg"] is None
    assert "delta" not in result.summary
    assert result.warnings == ["版本对比数据不完整，无法给出可靠差值"]


# --------------------------------------------------------------------------- 维度分群
def test_breakdown_分组均值求和与占比(service, add_snapshot):
    add_snapshot(stat_date=D1, value=0.4, platform="android")
    add_snapshot(stat_date=date(2024, 3, 2), value=0.5, platform="android")
    add_snapshot(stat_date=D1, value=0.3, platform="ios")

    result = service.breakdown(
        BreakdownRequest(
            metric_name="retention_d1",
            dimension="platform",
            start_date=D1,
            end_date=date(2024, 3, 3),
        )
    )

    top = result.rows[0]
    assert top == {
        "dimension": "platform",
        "key": "android",
        "avg_value": pytest.approx(0.45),
        "sum_value": pytest.approx(0.9),
        "share": pytest.approx(0.75),
        "points": 2,
    }
    assert [row["key"] for row in result.rows] == ["android", "ios"]  # 按 sum_value 降序
    assert result.summary["groups"] == 2
    assert result.summary["top_key"] == "android"
    assert result.summary["top_share"] == pytest.approx(0.75)
    assert result.summary["total_points"] == 3
    assert result.warnings == []


def test_breakdown_top_n_截断但summary仍报全部组数(service, add_snapshot):
    add_snapshot(stat_date=D1, value=0.5, platform="android")
    add_snapshot(stat_date=D1, value=0.3, platform="ios")
    add_snapshot(stat_date=D1, value=0.2, platform="harmony")

    result = service.breakdown(
        BreakdownRequest(
            metric_name="retention_d1",
            dimension="platform",
            start_date=D1,
            end_date=D1,
            top_n=2,
        )
    )

    assert [row["key"] for row in result.rows] == ["android", "ios"]
    assert result.summary["groups"] == 3


def test_breakdown_按机型分组(service, add_snapshot):
    add_snapshot(stat_date=D1, value=0.4, device="Pixel 8")
    add_snapshot(stat_date=D1, value=0.2, device="iPhone 15")

    result = service.breakdown(
        BreakdownRequest(
            metric_name="retention_d1", dimension="device", start_date=D1, end_date=D1
        )
    )

    assert [row["key"] for row in result.rows] == ["Pixel 8", "iPhone 15"]
    assert result.rows[0]["dimension"] == "device"


def test_breakdown_无数据时告警(service):
    result = service.breakdown(
        BreakdownRequest(metric_name="not_exist", dimension="platform", start_date=D1, end_date=D1)
    )

    assert result.rows == []
    assert result.summary["groups"] == 0
    assert result.summary["top_key"] is None
    assert result.warnings == ["没有可分解的指标数据"]


# --------------------------------------------------------------------------- 异常识别
@pytest.fixture
def series_with_outlier(service, add_snapshot):
    """5 个平稳点 + 1 个植入的骤降点（03-06 的 0.1）。"""
    for offset, value in enumerate([0.5, 0.5, 0.5, 0.5, 0.5, 0.1]):
        add_snapshot(stat_date=date(2024, 3, 1 + offset), value=value)
    return service


def test_detect_anomaly_zscore_识别植入的异常点(series_with_outlier):
    result = series_with_outlier.detect_anomaly(
        AnomalyRequest(
            metric_name="retention_d1",
            start_date=D1,
            end_date=date(2024, 3, 6),
            method="zscore",
            threshold=2.0,
        )
    )

    assert result.summary["points"] == 6
    assert result.summary["anomaly_count"] == 1
    anomaly = result.rows[0]
    assert anomaly["stat_date"] == "2024-03-06"
    assert anomaly["value"] == pytest.approx(0.1)
    assert anomaly["direction"] == "drop"
    assert anomaly["zscore"] < -2.0
    assert result.summary["first_anomaly"] == "2024-03-06"
    assert result.summary["mean"] == pytest.approx(0.433333, abs=1e-6)
    assert result.capability == "detect_anomaly"


def test_detect_anomaly_阈值调高后不再报异常(series_with_outlier):
    result = series_with_outlier.detect_anomaly(
        AnomalyRequest(
            metric_name="retention_d1",
            start_date=D1,
            end_date=date(2024, 3, 6),
            method="zscore",
            threshold=5.0,
        )
    )

    assert result.rows == []
    assert result.summary["anomaly_count"] == 0
    assert result.summary["first_anomaly"] is None


def test_detect_anomaly_iqr_方法同样能抓到(series_with_outlier):
    result = series_with_outlier.detect_anomaly(
        AnomalyRequest(
            metric_name="retention_d1",
            start_date=D1,
            end_date=date(2024, 3, 6),
            method="iqr",
        )
    )

    assert [row["stat_date"] for row in result.rows] == ["2024-03-06"]
    assert result.rows[0]["direction"] == "drop"
    assert len(result.rows[0]["bound"]) == 2


def test_detect_anomaly_点数不足三个不做判定(service, add_snapshot):
    """两天的数据算不出 z 分数，静默跳过而不是硬报异常。"""
    add_snapshot(stat_date=D1, value=0.5)
    add_snapshot(stat_date=date(2024, 3, 2), value=0.1)

    result = service.detect_anomaly(
        AnomalyRequest(metric_name="retention_d1", start_date=D1, end_date=date(2024, 3, 2))
    )

    assert result.rows == []
    assert result.summary["points"] == 2
    assert result.summary["anomaly_count"] == 0


def test_detect_anomaly_空序列给出缺失告警(service):
    result = service.detect_anomaly(
        AnomalyRequest(metric_name="not_exist", start_date=D1, end_date=date(2024, 3, 6))
    )

    assert result.rows == []
    assert result.summary["points"] == 0
    assert result.summary["mean"] is None
    assert result.warnings == ["指定条件下没有指标数据（数据缺失）"]


# --------------------------------------------------------------------------- 辅助能力
@pytest.fixture
def three_versions(service, add_snapshot):
    """3.1.0（3 行）、3.2.0（2 行）、3.0.0（1 行），另有一行没有版本号。"""
    for offset in range(3):
        add_snapshot(stat_date=date(2024, 3, 1 + offset), value=0.5, app_version="3.1.0")
    for offset in range(2):
        add_snapshot(stat_date=date(2024, 3, 4 + offset), value=0.4, app_version="3.2.0")
    add_snapshot(stat_date=date(2024, 3, 6), value=0.3, app_version="3.0.0")
    add_snapshot(stat_date=date(2024, 3, 7), value=0.2)
    return service


def test_list_versions_按数据量降序并给出覆盖区间(three_versions):
    versions = three_versions.list_versions("retention_d1")

    assert [item["app_version"] for item in versions] == ["3.1.0", "3.2.0", "3.0.0"]
    assert versions[0]["rows"] == 3
    assert versions[0]["min_date"] == "2024-03-01"
    assert versions[0]["max_date"] == "2024-03-03"
    # 没有版本号的行不进版本清单
    assert all(item["app_version"] for item in versions)


def test_list_versions_支持时间窗过滤(three_versions):
    versions = three_versions.list_versions(
        "retention_d1", start=date(2024, 3, 4), end=date(2024, 3, 6)
    )

    assert [item["app_version"] for item in versions] == ["3.2.0", "3.0.0"]
    assert sum(item["rows"] for item in versions) == 3


def test_pick_baseline_version_优先取更早且数据量更大的版本(three_versions):
    versions = three_versions.list_versions("retention_d1")

    assert pick_baseline_version(versions, "3.2.0") == "3.1.0"


def test_pick_baseline_version_没有更早版本时退化为数据量最大者(three_versions):
    versions = three_versions.list_versions("retention_d1")

    assert pick_baseline_version(versions, "3.1.0") == "3.0.0"


def test_pick_baseline_version_不给目标时取第一顺位(three_versions):
    versions = three_versions.list_versions("retention_d1")

    assert pick_baseline_version(versions, None) == "3.1.0"


def test_pick_baseline_version_无候选时返回None():
    assert pick_baseline_version([], "3.2.0") is None
    assert pick_baseline_version([{"app_version": "3.2.0", "rows": 3}], "3.2.0") is None
    assert pick_baseline_version([{"app_version": None, "rows": 3}], None) is None


def test_coverage_给出数据覆盖区间与行数(three_versions):
    coverage = three_versions.coverage("retention_d1")

    assert coverage == {
        "metric_name": "retention_d1",
        "min_date": "2024-03-01",
        "max_date": "2024-03-07",
        "rows": 7,
    }


def test_coverage_无数据时全为空(service):
    coverage = service.coverage("not_exist")

    assert coverage["min_date"] is None
    assert coverage["max_date"] is None
    assert coverage["rows"] == 0


def test_list_metrics_covered_列出有数据的指标(service, add_snapshot):
    add_snapshot(stat_date=D1, value=0.4, metric_name="retention_d1")
    add_snapshot(stat_date=D1, value=0.1, metric_name="crash_rate")
    add_snapshot(stat_date=D1, value=0.2, metric_name="retention_d1")

    assert service.list_metrics_covered() == ["crash_rate", "retention_d1"]
