"""Write a sanitized evidence record from a remote CI JUnit result."""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path


SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _integer_attr(element: ET.Element, name: str) -> int:
    raw = element.attrib.get(name, "0")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"JUnit attribute {name!r} must be an integer") from exc
    if value < 0:
        raise ValueError(f"JUnit attribute {name!r} cannot be negative")
    return value


def build_evidence(
    *,
    junit_xml: Path,
    source_sha: str,
    provider: str,
    executor_runtime: str,
) -> dict[str, object]:
    if not SHA_PATTERN.fullmatch(source_sha):
        raise ValueError("source SHA must be 40 lowercase hexadecimal characters")
    if provider != "gitee_go":
        raise ValueError("unsupported remote CI provider")
    if not executor_runtime or any(character.isspace() for character in executor_runtime):
        raise ValueError("executor runtime must be a non-empty reference without whitespace")

    root = ET.parse(junit_xml).getroot()
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = list(root.findall("testsuite"))
    else:
        raise ValueError(f"unsupported JUnit root element: {root.tag}")
    if not suites:
        raise ValueError("JUnit report contains no test suites")

    test_count = sum(_integer_attr(suite, "tests") for suite in suites)
    failure_count = sum(_integer_attr(suite, "failures") for suite in suites)
    error_count = sum(_integer_attr(suite, "errors") for suite in suites)
    skipped_count = sum(_integer_attr(suite, "skipped") for suite in suites)
    if test_count <= 0:
        raise ValueError("JUnit report contains no executed tests")
    if failure_count + error_count + skipped_count > test_count:
        raise ValueError("JUnit counters are inconsistent")

    passed = failure_count == 0 and error_count == 0
    return {
        "schema_version": "remote-ci-contract-evidence-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "executed": True,
        "passed": passed,
        "provider": provider,
        "scope": "remote_ci_contract",
        "source_git_sha": source_sha,
        "executor_runtime_reference": executor_runtime,
        "executor_runtime_immutable": bool(
            re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", executor_runtime)
        ),
        "test_count": test_count,
        "failure_count": failure_count,
        "error_count": error_count,
        "skipped_count": skipped_count,
        "image_supply_chain_executed": False,
        "docker_daemon_verified": False,
        "enterprise_registry_verified": False,
        "cross_host_verified": False,
        "production_ready": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit-xml", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--provider", default="gitee_go")
    parser.add_argument("--executor-runtime", required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args()

    evidence = build_evidence(
        junit_xml=args.junit_xml,
        source_sha=args.source_sha,
        provider=args.provider,
        executor_runtime=args.executor_runtime,
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
