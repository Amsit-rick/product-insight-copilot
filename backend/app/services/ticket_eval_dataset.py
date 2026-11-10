"""External ticket evaluation dataset validation and provenance controls."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.masking import SENSITIVE_KEYS, contains_sensitive

SCHEMA_VERSION = "ticket-eval-v1"
HUMAN_ANNOTATION_METHODS = {
    "human_review",
    "double_human_review",
    "expert_review",
    "adjudicated_human_review",
}
REQUIRED_CASE_FIELDS = {"case_id", "group", "subject", "content", "expect"}


class TicketDatasetValidationError(ValueError):
    """Raised before evaluation when an external dataset is not safe or independent."""


@dataclass(frozen=True)
class TicketDataset:
    dataset_version: str
    cases: list[dict]
    provenance: dict[str, Any]
    sha256: str
    kind: str = "external_holdout"

    @property
    def case_count(self) -> int:
        return len(self.cases)

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "dataset_version": self.dataset_version,
            "dataset_kind": self.kind,
            "dataset_sha256": self.sha256,
            "case_count": self.case_count,
            "provenance": dict(self.provenance),
        }


def _read_json(path: Path, *, label: str) -> tuple[dict, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TicketDatasetValidationError(f"无法读取{label}: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TicketDatasetValidationError(f"{label}不是有效 UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TicketDatasetValidationError(f"{label}顶层必须是 JSON 对象")
    return payload, raw


def _validate_provenance(payload: dict) -> dict[str, Any]:
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        raise TicketDatasetValidationError("provenance 必须是对象")
    if provenance.get("annotation_method") not in HUMAN_ANNOTATION_METHODS:
        raise TicketDatasetValidationError("annotation_method 必须是受支持的人工标注方法")
    if provenance.get("independent_from_system") is not True:
        raise TicketDatasetValidationError("数据集必须独立于当前系统生成和判定")
    if provenance.get("deidentified") is not True:
        raise TicketDatasetValidationError("数据集必须完成脱敏")
    if provenance.get("split") != "holdout":
        raise TicketDatasetValidationError("真实基线只接受 holdout 数据集")
    for key in ("source_name", "exported_at"):
        if not isinstance(provenance.get(key), str) or not provenance[key].strip():
            raise TicketDatasetValidationError(f"provenance.{key} 不能为空")
    try:
        datetime.fromisoformat(provenance["exported_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise TicketDatasetValidationError("provenance.exported_at 必须是 ISO-8601 时间") from exc
    return {
        key: provenance[key]
        for key in (
            "source_name",
            "exported_at",
            "annotation_method",
            "independent_from_system",
            "deidentified",
            "split",
        )
    }


def _find_sensitive_key(value: Any, *, location: str) -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key).lower()
            if key_text in SENSITIVE_KEYS:
                return f"{location}.{key}"
            found = _find_sensitive_key(nested, location=f"{location}.{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found = _find_sensitive_key(nested, location=f"{location}[{index}]")
            if found:
                return found
    return None


def _contains_sensitive_value(value: Any) -> bool:
    if isinstance(value, str):
        return contains_sensitive(value)
    if isinstance(value, dict):
        return any(_contains_sensitive_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_sensitive_value(item) for item in value)
    return False


def _internal_contents(path: Path | None) -> set[str]:
    if path is None:
        return set()
    payload, _ = _read_json(path, label="内部回归集")
    return {
        str(case.get("content", "")).strip()
        for case in payload.get("cases", [])
        if isinstance(case, dict) and str(case.get("content", "")).strip()
    }


def load_external_ticket_dataset(
    path: str | Path,
    *,
    min_cases: int = 200,
    internal_dataset_path: str | Path | None = None,
) -> TicketDataset:
    """Load an independent holdout set after provenance, PII, and leakage checks."""
    dataset_path = Path(path)
    payload, raw = _read_json(dataset_path, label="外部评测集")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise TicketDatasetValidationError(f"schema_version 必须是 {SCHEMA_VERSION}")
    dataset_version = payload.get("dataset_version")
    if not isinstance(dataset_version, str) or not dataset_version.strip():
        raise TicketDatasetValidationError("dataset_version 不能为空")
    provenance = _validate_provenance(payload)
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise TicketDatasetValidationError("cases 必须是数组")
    if len(cases) < min_cases:
        raise TicketDatasetValidationError(f"真实基线至少 {min_cases} 条，当前 {len(cases)} 条")

    internal_contents = _internal_contents(Path(internal_dataset_path) if internal_dataset_path else None)
    seen_ids: set[str] = set()
    seen_inputs: set[tuple[str, str | None]] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise TicketDatasetValidationError(f"cases[{index}] 必须是对象")
        missing = REQUIRED_CASE_FIELDS - set(case)
        if missing:
            raise TicketDatasetValidationError(f"cases[{index}] 缺少字段: {sorted(missing)}")
        case_id = str(case.get("case_id", "")).strip()
        if not case_id or case_id in seen_ids:
            raise TicketDatasetValidationError(f"case_id 为空或重复: {case_id or index}")
        seen_ids.add(case_id)
        expect = case.get("expect")
        if not isinstance(expect, dict) or not expect:
            raise TicketDatasetValidationError(f"{case_id} 的 expect 必须是非空对象")
        content = str(case.get("content", "")).strip()
        if not content:
            raise TicketDatasetValidationError(f"{case_id} 的 content 不能为空")
        input_key = (content, case.get("order_id"))
        if input_key in seen_inputs:
            raise TicketDatasetValidationError(f"{case_id} 的输入与其他用例重复")
        seen_inputs.add(input_key)
        sensitive_key = _find_sensitive_key(case, location=f"cases[{index}]")
        if sensitive_key:
            raise TicketDatasetValidationError(f"发现敏感字段: {sensitive_key}")
        if _contains_sensitive_value(case):
            raise TicketDatasetValidationError(f"{case_id} 包含未脱敏的敏感信息")
        if content in internal_contents:
            raise TicketDatasetValidationError(f"{case_id} 与内部生成集重合，不能作为独立基线")

    return TicketDataset(
        dataset_version=dataset_version.strip(),
        cases=cases,
        provenance=provenance,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


__all__ = ["TicketDataset", "TicketDatasetValidationError", "load_external_ticket_dataset"]
