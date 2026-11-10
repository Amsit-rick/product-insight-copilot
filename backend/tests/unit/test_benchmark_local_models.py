from scripts.benchmark_local_models import build_report, measure_mixed, positive_int_list


def test_positive_int_list_requires_positive_unique_values():
    assert positive_int_list("1,2,4", name="concurrency") == [1, 2, 4]


def test_report_separates_execution_from_acceptance_and_production():
    report = build_report(
        health={
            "status": "ok",
            "embedding_model": "embedding",
            "reranker_model": "reranker",
            "device": "cpu-onnx",
            "model_threads": 4,
            "inference_policy": "shared_serial",
        },
        rows=[{"error_count": 0}],
        requests=4,
        concurrencies=[1],
        embedding_batches=[1],
        rerank_documents=2,
    )

    assert report["passed"] is True
    assert report["execution_passed"] is True
    assert report["acceptance_evaluated"] is False
    assert report["acceptance_passed"] is None
    assert report["quality_claim_allowed"] is False
    assert report["production_ready"] is False
    assert report["models"]["inference_policy"] == "shared_serial"


def test_report_fails_when_any_request_errors():
    report = build_report(
        health={
            "status": "ok",
            "embedding_model": "embedding",
            "reranker_model": "reranker",
        },
        rows=[{"error_count": 2}],
        requests=4,
        concurrencies=[1],
        embedding_batches=[1],
        rerank_documents=2,
    )

    assert report["passed"] is False
    assert report["error_count"] == 2


def test_mixed_measurement_keeps_component_latency_visible():
    row = measure_mixed(
        lambda _index: None,
        lambda _index: None,
        requests=4,
        concurrency=2,
        rerank_documents=8,
    )

    assert row["completed"] == 4
    assert row["error_count"] == 0
    assert row["components"]["embedding"]["completed"] == 2
    assert row["components"]["reranker"]["completed"] == 2
