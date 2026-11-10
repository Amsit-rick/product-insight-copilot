import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.benchmark_document_holdout import (
    _matches_expected_hit,
    validate_holdout,
    validate_source_files,
)
from scripts.prepare_document_holdout import prepare_corpus


@dataclass
class FakeBlock:
    text: str
    section_title: str | None = None


@dataclass
class FakeChunk:
    content: str
    section_title: str | None = None


def test_prepare_corpus_detects_duplicates_and_keeps_draft_untrusted(tmp_path: Path):
    first = tmp_path / "a.docx"
    duplicate = tmp_path / "copy.docx"
    other = tmp_path / "b.docx"
    first.write_bytes(b"same")
    duplicate.write_bytes(b"same")
    other.write_bytes(b"other")

    def parse(path: Path):
        return [
            FakeBlock("Heading", "Section"),
            FakeBlock("This is enough source content for a candidate.", "Section"),
        ]

    def chunk(_blocks):
        return [
            FakeChunk(
                "This is enough source content for an annotation candidate and evidence excerpt.",
                "Section",
            ),
            FakeChunk(
                "A second candidate verifies that large early documents cannot dominate sampling.",
                "Section",
            ),
        ]

    manifest, worksheet = prepare_corpus(
        tmp_path,
        max_candidates=2,
        parse=parse,
        chunk=chunk,
    )

    assert manifest["file_count"] == 3
    assert manifest["unique_document_count"] == 2
    assert manifest["exact_duplicate_count"] == 1
    assert manifest["parse_failure_count"] == 0
    assert len(worksheet["cases"]) == 2
    assert {item["expected_source_name"] for item in worksheet["cases"]} == {
        "a.docx",
        "b.docx",
    }
    assert worksheet["readiness"]["holdout_ready"] is False
    assert all(not item["question"] for item in worksheet["cases"])


def test_holdout_validator_rejects_machine_draft_and_accepts_reviewed_dataset():
    digest = "a" * 64
    manifest = {
        "schema_version": "document-corpus-manifest-v1",
        "corpus_fingerprint": "corpus",
        "files": [
            {
                "source_name": "guide.docx",
                "sha256": digest,
                "duplicate_of": None,
                "parse_status": "parsed",
            }
        ],
    }
    dataset = {
        "schema_version": "document-retrieval-holdout-v1",
        "corpus_fingerprint": "corpus",
        "provenance": {
            "annotation_method": "unreviewed_machine_assisted_draft",
            "independent_from_system": False,
            "deidentified": None,
            "reviewer_count": 0,
        },
        "cases": [
            {
                "case_id": "CASE-1",
                "question": "",
                "expected_source_name": "guide.docx",
                "expected_document_sha256": digest,
                "expected_section_title": "安装",
                "annotation_status": "needs_independent_human_review",
            }
        ],
    }

    errors = validate_holdout(manifest, dataset, min_cases=1)
    assert any("independent human review" in error for error in errors)
    assert any("not human reviewed" in error for error in errors)

    dataset["provenance"] = {
        "annotation_method": "double_human_review",
        "independent_from_system": True,
        "deidentified": True,
        "reviewer_count": 2,
    }
    dataset["cases"][0].update(
        {
            "question": "如何配置 Hadoop 集群？",
            "annotation_status": "human_reviewed",
        }
    )
    assert validate_holdout(manifest, dataset, min_cases=1) == []


def test_source_validator_detects_file_drift(tmp_path: Path):
    source = tmp_path / "guide.docx"
    source.write_bytes(b"original")
    manifest = {
        "files": [
            {
                "source_name": source.name,
                "sha256": "0" * 64,
            }
        ]
    }

    assert validate_source_files(manifest, tmp_path) == [
        "source document drifted: guide.docx"
    ]


def test_expected_hit_requires_document_and_annotated_evidence():
    @dataclass
    class Hit:
        source_name: str
        section_title: str | None
        content: str

    case = {
        "expected_source_name": "guide.docx",
        "expected_section_title": "安装",
        "expected_evidence_excerpt": "启动 NameNode",
    }

    assert _matches_expected_hit(Hit("guide.docx", "安装", "先启动 NameNode 服务"), case)
    assert not _matches_expected_hit(Hit("guide.docx", "配置", "启动 NameNode"), case)
    assert not _matches_expected_hit(Hit("other.docx", "安装", "启动 NameNode"), case)


def test_prepare_cli_can_run_from_repository_root(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sample.docx").write_bytes(b"not-a-valid-docx")
    output = tmp_path / "output"
    repository_root = Path(__file__).resolve().parents[3]
    script = repository_root / "backend" / "scripts" / "prepare_document_holdout.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--source-dir",
            str(source),
            "--output-dir",
            str(output),
        ],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )

    # The invalid fixture is expected to fail parsing, but the CLI must load the
    # application package and still emit a manifest instead of crashing at import.
    assert result.returncode == 1
    assert "ModuleNotFoundError" not in result.stderr
    assert (output / "corpus-manifest.json").exists()
