import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "backend" / "scripts" / "write_remote_ci_contract_evidence.py"
SPEC = importlib.util.spec_from_file_location("remote_ci_evidence_under_test", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _write_junit(path: Path, *, tests: int, failures: int = 0, errors: int = 0) -> None:
    path.write_text(
        (
            '<testsuites><testsuite name="remote" '
            f'tests="{tests}" failures="{failures}" errors="{errors}" skipped="0" />'
            "</testsuites>"
        ),
        encoding="utf-8",
    )


def test_remote_ci_evidence_records_passing_contract_tests(tmp_path: Path):
    junit = tmp_path / "junit.xml"
    _write_junit(junit, tests=40)

    evidence = MODULE.build_evidence(
        junit_xml=junit,
        source_sha="a" * 40,
        provider="gitee_go",
        executor_runtime="gitee-go/python-build:3.12",
    )

    assert evidence["passed"] is True
    assert evidence["test_count"] == 40
    assert evidence["executor_runtime_immutable"] is False
    assert evidence["image_supply_chain_executed"] is False
    assert evidence["production_ready"] is False


def test_remote_ci_evidence_preserves_test_failures(tmp_path: Path):
    junit = tmp_path / "junit.xml"
    _write_junit(junit, tests=40, failures=1)

    evidence = MODULE.build_evidence(
        junit_xml=junit,
        source_sha="b" * 40,
        provider="gitee_go",
        executor_runtime="python@sha256:" + "c" * 64,
    )

    assert evidence["passed"] is False
    assert evidence["failure_count"] == 1
    assert evidence["executor_runtime_immutable"] is True


@pytest.mark.parametrize("source_sha", ["", "abc", "A" * 40, "g" * 40])
def test_remote_ci_evidence_rejects_invalid_source_sha(tmp_path: Path, source_sha: str):
    junit = tmp_path / "junit.xml"
    _write_junit(junit, tests=1)

    with pytest.raises(ValueError, match="source SHA"):
        MODULE.build_evidence(
            junit_xml=junit,
            source_sha=source_sha,
            provider="gitee_go",
            executor_runtime="gitee-go/python-build:3.12",
        )
