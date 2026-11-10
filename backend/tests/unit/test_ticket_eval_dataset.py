"""外部独立标注工单集的契约、脱敏与泄漏防护。"""

from __future__ import annotations

import json

import pytest

from app.services.ticket_eval_dataset import (
    TicketDatasetValidationError,
    load_external_ticket_dataset,
)
from app.services.ticket_evaluation_service import CASES_PATH


def _case(index: int, *, content: str | None = None) -> dict:
    return {
        "case_id": f"external-{index:03d}",
        "group": "normal",
        "subject": f"外部标注工单 {index}",
        "content": content or f"这是独立脱敏后的售后问题样本 {index}",
        "user": "ops",
        "order_id": f"REAL-ORDER-{index:03d}",
        "customer_id": f"ANON-{index:03d}",
        "expect": {"issue_type": "other", "max_risk_tier": "L2"},
    }


def _payload(*, cases: list[dict] | None = None, **provenance_overrides) -> dict:
    provenance = {
        "source_name": "merchant-a-quality-team",
        "exported_at": "2026-09-25T18:00:00+08:00",
        "annotation_method": "double_human_review",
        "independent_from_system": True,
        "deidentified": True,
        "split": "holdout",
    }
    provenance.update(provenance_overrides)
    return {
        "schema_version": "ticket-eval-v1",
        "dataset_version": "merchant-a-holdout-2026-09-25",
        "provenance": provenance,
        "cases": cases or [_case(1), _case(2)],
    }


def _write(tmp_path, payload: dict):
    path = tmp_path / "external.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_external_dataset_accepts_independent_deidentified_holdout(tmp_path):
    dataset = load_external_ticket_dataset(
        _write(tmp_path, _payload()), min_cases=2, internal_dataset_path=CASES_PATH
    )

    assert dataset.dataset_version == "merchant-a-holdout-2026-09-25"
    assert dataset.kind == "external_holdout"
    assert dataset.case_count == 2
    assert len(dataset.sha256) == 64
    assert dataset.provenance["annotation_method"] == "double_human_review"
    assert "source_path" not in dataset.safe_metadata()


@pytest.mark.parametrize(
    "override",
    [
        {"independent_from_system": False},
        {"deidentified": False},
        {"split": "train"},
        {"annotation_method": "model_generated"},
    ],
)
def test_external_dataset_rejects_non_holdout_or_non_human_provenance(tmp_path, override):
    with pytest.raises(TicketDatasetValidationError):
        load_external_ticket_dataset(
            _write(tmp_path, _payload(**override)), min_cases=2, internal_dataset_path=CASES_PATH
        )


def test_external_dataset_rejects_too_small_duplicate_and_sensitive_data(tmp_path):
    with pytest.raises(TicketDatasetValidationError, match="至少 2"):
        load_external_ticket_dataset(
            _write(tmp_path, _payload(cases=[_case(1)])),
            min_cases=2,
            internal_dataset_path=CASES_PATH,
        )

    duplicate = [_case(1), {**_case(2), "content": _case(1)["content"], "order_id": _case(1)["order_id"]}]
    with pytest.raises(TicketDatasetValidationError, match="重复"):
        load_external_ticket_dataset(
            _write(tmp_path, _payload(cases=duplicate)),
            min_cases=2,
            internal_dataset_path=CASES_PATH,
        )

    sensitive = [_case(1, content="我的手机号是 13812345678"), _case(2)]
    with pytest.raises(TicketDatasetValidationError, match="敏感"):
        load_external_ticket_dataset(
            _write(tmp_path, _payload(cases=sensitive)),
            min_cases=2,
            internal_dataset_path=CASES_PATH,
        )


def test_external_dataset_rejects_sensitive_field_names_and_internal_overlap(tmp_path):
    with_sensitive_field = [_case(1), {**_case(2), "attributes": {"receiver_phone": "masked"}}]
    with pytest.raises(TicketDatasetValidationError, match="敏感字段"):
        load_external_ticket_dataset(
            _write(tmp_path, _payload(cases=with_sensitive_field)),
            min_cases=2,
            internal_dataset_path=CASES_PATH,
        )

    internal = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"][0]
    overlap = [_case(1, content=internal["content"]), _case(2)]
    with pytest.raises(TicketDatasetValidationError, match="内部生成集重合"):
        load_external_ticket_dataset(
            _write(tmp_path, _payload(cases=overlap)),
            min_cases=2,
            internal_dataset_path=CASES_PATH,
        )


def test_external_dataset_requires_unique_ids_and_nonempty_expectations(tmp_path):
    duplicate_ids = [_case(1), {**_case(2), "case_id": "external-001"}]
    with pytest.raises(TicketDatasetValidationError, match="case_id"):
        load_external_ticket_dataset(
            _write(tmp_path, _payload(cases=duplicate_ids)),
            min_cases=2,
            internal_dataset_path=CASES_PATH,
        )

    missing_label = [_case(1), {**_case(2), "expect": {}}]
    with pytest.raises(TicketDatasetValidationError, match="expect"):
        load_external_ticket_dataset(
            _write(tmp_path, _payload(cases=missing_label)),
            min_cases=2,
            internal_dataset_path=CASES_PATH,
        )
