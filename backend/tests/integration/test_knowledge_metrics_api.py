"""集成测试：知识库/文档与指标接口（含数据质量与失败路径）。"""

from __future__ import annotations

METRIC_CSV = (
    "metric_name,platform,app_version,channel,device,stat_date,value,sample_size,source_version\n"
    "order_conversion_rate,Android,3.2.0,channel_a,high_end,2026-08-10,0.0412,30000,v-test\n"
    "order_conversion_rate,Android,3.2.0,channel_a,high_end,2026-08-11,0.0398,31000,v-test\n"
    "order_conversion_rate,Android,3.2.0,channel_a,high_end,2026-08-13,0.0405,31500,v-test\n"
    "order_conversion_rate,Android,3.2.0,channel_b,high_end,2026-08-10,0.0521,22000,v-test\n"
)
BROKEN_CSV = (
    "metric_name,stat_date,value\n"
    "next_day_retention,2026-13-05,0.27\n"
    "next_day_retention,2026-08-10,N/A\n"
    "next_day_retention,2026-08-11,1.87\n"
)
ALIASED_CSV = "指标名,平台,日期,指标值\nnext_day_retention,iOS,2026-08-10,0.33\n"


def test_document_upload_index_and_delete(client, auth):
    content = (
        "# 3.2.0 埋点口径补充\n\n"
        "## 次日留存\n"
        "次日留存 = 次日活跃去重用户数 / 当日新增用户数，低端机需要单独观察。\n"
    ).encode()
    resp = client.post(
        "/api/v1/documents",
        headers=auth["pm"],
        data={
            "kb_code": "operation_kb",
            "name": "埋点口径补充说明",
            "document_version": "1.2.0",
            "app_version": "3.2.0",
            "doc_updated_at": "2026-08-30",
        },
        files={"file": ("tracking_note.md", content, "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["chunks"] >= 1
    assert payload["index_status"] == "indexed"
    document_id = payload["document"]["id"]

    listed = client.get("/api/v1/documents?kb_code=operation_kb", headers=auth["pm"]).json()
    assert any(item["id"] == document_id for item in listed["items"])

    reindex = client.post(
        "/api/v1/knowledge-bases/operation_kb/reindex",
        headers=auth["pm"],
        json={"force": True},
    )
    assert reindex.status_code == 200
    assert reindex.json()["failed"] == []

    # 删除是管理员专属动作：产品经理只应有 kb:reindex / document:upload
    denied = client.delete(f"/api/v1/documents/{document_id}", headers=auth["pm"])
    assert denied.status_code == 403

    deleted = client.delete(f"/api/v1/documents/{document_id}", headers=auth["admin"])
    assert deleted.status_code == 200
    assert deleted.json()["deleted_chunks"] >= 1


def test_deleted_document_disappears_from_list_and_counts(client, auth):
    """删除后：默认列表看不到、总数不算它、知识库计数回落；显式查已删除仍可见。

    回归测试，对应 2026-09-15 清理误传文档时暴露的两个真实缺陷：

    1. `GET /documents` 在**不传 status** 时根本不过滤，于是删掉的文档一直挂在列表
       和 `total` 里，界面上"删除成功但列表还在"，与手册"列表消失"的预期不符；
    2. `_refresh_kb_stats` 在 `autoflush=False` 的会话里**先统计后落库**，
       把刚置为 deleted 的文档继续算进 `document_count`，知识库概览的数字降不下来。

    删除是软删除（保留审计），所以这里同时断言"显式 status=deleted 还能查到"。
    """

    def kb_doc_count(code: str) -> int:
        kbs = client.get("/api/v1/knowledge-bases", headers=auth["admin"]).json()
        return next(item["document_count"] for item in kbs if item["code"] == code)

    def doc_total(query: str = "") -> int:
        resp = client.get(f"/api/v1/documents?page_size=50{query}", headers=auth["admin"])
        assert resp.status_code == 200, resp.text
        return resp.json()["meta"]["total"]

    before_count = kb_doc_count("operation_kb")
    before_total = doc_total()

    upload = client.post(
        "/api/v1/documents",
        headers=auth["pm"],
        data={"kb_code": "operation_kb", "name": "删除回归测试文档", "document_version": "1.0.0"},
        files={"file": ("delete_regression.md", "# 临时文档\n\n用于验证删除后的可见性与计数。\n".encode(), "text/markdown")},
    )
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["document"]["id"]
    # 上传之后计数要涨（两侧都要涨，否则后面的"回落"断言没有意义）
    assert kb_doc_count("operation_kb") == before_count + 1
    assert doc_total() == before_total + 1

    assert client.delete(f"/api/v1/documents/{document_id}", headers=auth["admin"]).status_code == 200

    # 1) 默认列表不再包含它 —— 界面上"全部状态"就应该看不到
    default_list = client.get("/api/v1/documents?page_size=50", headers=auth["admin"]).json()
    assert all(item["id"] != document_id for item in default_list["items"])
    assert default_list["meta"]["total"] == before_total
    scoped = client.get("/api/v1/documents?kb_code=operation_kb&page_size=50", headers=auth["admin"]).json()
    assert all(item["id"] != document_id for item in scoped["items"])

    # 2) 知识库计数回落（曾经因为 autoflush=False 统计到旧状态而降不下来）
    assert kb_doc_count("operation_kb") == before_count

    # 3) 软删除可审计：显式按 status=deleted 查仍能看到，且切片已清空
    deleted_list = client.get("/api/v1/documents?status=deleted&page_size=50", headers=auth["admin"]).json()
    found = [item for item in deleted_list["items"] if item["id"] == document_id]
    assert found, "显式查 status=deleted 应仍能看到已删除文档（软删除保留审计痕迹）"
    assert found[0]["status"] == "deleted"
    assert (found[0].get("chunk_count") or 0) == 0


def test_metric_import_reports_quality_issues(client, auth):
    resp = client.post(
        "/api/v1/metrics/import",
        headers=auth["pm"],
        data={"dataset_version": "metric_snapshot_itest"},
        files={"file": ("order_conversion_rate.csv", METRIC_CSV.encode(), "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["rows_valid"] == 4
    assert payload["metric_names"] == ["order_conversion_rate"]
    # 2026-08-12 在区间内完全没有数据
    assert "2026-08-12" in payload["missing_days"]
    assert "missing_days" in payload["quality_flags"]
    assert payload["date_range"] == {"min": "2026-08-10", "max": "2026-08-13"}


def test_metric_import_rejects_bad_rows_and_column_aliases(client, auth):
    broken = client.post(
        "/api/v1/metrics/import",
        headers=auth["pm"],
        data={"dataset_version": "metric_snapshot_itest_bad"},
        files={"file": ("broken.csv", BROKEN_CSV.encode(), "text/csv")},
    )
    assert broken.status_code == 400
    assert "未通过校验" in broken.text or "metric_import_failed" in broken.text

    aliased = client.post(
        "/api/v1/metrics/import",
        headers=auth["pm"],
        data={"dataset_version": "metric_snapshot_itest_alias"},
        files={"file": ("aliased.csv", ALIASED_CSV.encode("utf-8"), "text/csv")},
    )
    assert aliased.status_code == 201, aliased.text
    assert aliased.json()["rows_valid"] == 1


def test_metric_endpoints_return_governed_results(client, auth):
    trend = client.get(
        "/api/v1/metrics/trend",
        headers=auth["pm"],
        params={
            "metric_name": "next_day_retention",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "platform": "Android",
            "app_version": "3.2.0",
        },
    )
    assert trend.status_code == 200, trend.text
    body = trend.json()
    assert body["available"] is True
    assert body["dataset_version"]
    assert len(body["rows"]) >= 5

    compare = client.get(
        "/api/v1/metrics/compare",
        headers=auth["pm"],
        params={
            "metric_name": "crash_rate",
            "baseline_version": "3.1.9",
            "target_version": "3.2.0",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "platform": "Android",
        },
    ).json()
    assert compare["summary"]["target_avg"] > compare["summary"]["baseline_avg"]
    assert compare["summary"]["pct_change"] > 0

    breakdown = client.get(
        "/api/v1/metrics/breakdown",
        headers=auth["pm"],
        params={
            "metric_name": "next_day_retention",
            "dimension": "channel",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "platform": "Android",
            "app_version": "3.2.0",
        },
    ).json()
    assert breakdown["summary"]["groups"] >= 2

    anomaly = client.get(
        "/api/v1/metrics/anomaly",
        headers=auth["pm"],
        params={
            "metric_name": "next_day_retention",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "platform": "Android",
            "app_version": "3.2.0",
            "method": "iqr",
        },
    )
    assert anomaly.status_code == 200

    definitions = client.get("/api/v1/metrics/definitions", headers=auth["pm"]).json()
    names = {item["name"] for item in definitions}
    assert {"next_day_retention", "crash_rate", "dau"} <= names

    coverage = client.get(
        "/api/v1/metrics/coverage",
        headers=auth["pm"],
        params={"metric_name": "next_day_retention"},
    ).json()
    assert coverage["rows"] > 0
    assert coverage["versions"]


def test_missing_slice_is_reported_not_invented(client, auth):
    """iOS + low_end 没有数据：必须如实报告缺失，而不是给出编造的数值。"""
    resp = client.get(
        "/api/v1/metrics/trend",
        headers=auth["pm"],
        params={
            "metric_name": "next_day_retention",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "platform": "iOS",
            "device": "low_end",
        },
    ).json()
    assert resp["available"] is False
    assert resp["rows"] == []
    assert resp["warnings"], "数据缺失必须给出 warning"
