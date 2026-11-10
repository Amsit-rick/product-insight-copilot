"""Prepare a real-document retrieval corpus and a human annotation worksheet.

This script never copies source documents into the repository and never claims
quality from machine-generated questions. It fingerprints, parses, deduplicates,
and produces draft annotation candidates that must be reviewed independently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = BACKEND / ".tmp" / "document-holdout"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _candidate_id(file_sha: str, index: int) -> str:
    return f"DRAFT-{file_sha[:10]}-{index:04d}"


def prepare_corpus(
    source_dir: Path,
    *,
    max_candidates: int = 200,
    parse: Callable[[Path], list[Any]] | None = None,
    chunk: Callable[[list[Any]], list[Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not source_dir.is_dir():
        raise ValueError("source directory does not exist")
    if max_candidates < 1:
        raise ValueError("max_candidates must be positive")

    if parse is None or chunk is None:
        from app.rag.chunking import build_chunks
        from app.rag.parsers import parse_file

        parse = parse or (lambda path: parse_file(path, path.suffix))
        chunk = chunk or build_chunks

    files = sorted(source_dir.rglob("*.docx"), key=lambda item: item.as_posix().lower())
    if not files:
        raise ValueError("no DOCX files found")

    seen_hashes: dict[str, str] = {}
    manifest_files: list[dict[str, Any]] = []
    candidate_pools: list[list[dict[str, Any]]] = []
    failures: list[dict[str, str]] = []
    total_blocks = 0
    total_chunks = 0

    for path in files:
        relative_name = path.relative_to(source_dir).as_posix()
        data = path.read_bytes()
        digest = _sha256(data)
        duplicate_of = seen_hashes.get(digest)
        if duplicate_of is None:
            seen_hashes[digest] = relative_name

        row: dict[str, Any] = {
            "source_name": relative_name,
            "sha256": digest,
            "size_bytes": len(data),
            "modified_at": datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
            "duplicate_of": duplicate_of,
            "parse_status": "duplicate_skipped" if duplicate_of else "pending",
            "block_count": 0,
            "chunk_count": 0,
            "heading_count": 0,
        }
        if duplicate_of:
            manifest_files.append(row)
            continue

        try:
            blocks = parse(path)
            chunks = chunk(blocks)
        except Exception as exc:
            row["parse_status"] = "failed"
            row["error_type"] = type(exc).__name__
            failures.append({"source_name": relative_name, "error_type": type(exc).__name__})
            manifest_files.append(row)
            continue

        headings = []
        seen_headings: set[str] = set()
        for block in blocks:
            title = str(getattr(block, "section_title", "") or "").strip()
            if title and title not in seen_headings:
                seen_headings.add(title)
                headings.append(title)
        row.update(
            {
                "parse_status": "parsed",
                "block_count": len(blocks),
                "chunk_count": len(chunks),
                "heading_count": len(headings),
            }
        )
        manifest_files.append(row)
        total_blocks += len(blocks)
        total_chunks += len(chunks)

        document_candidates: list[dict[str, Any]] = []
        for item in chunks:
            section = str(getattr(item, "section_title", "") or "").strip()
            content = str(getattr(item, "content", "") or "").strip()
            if len(content) < 40:
                continue
            document_candidates.append(
                {
                    "case_id": _candidate_id(digest, len(document_candidates) + 1),
                    "question": "",
                    "expected_source_name": relative_name,
                    "expected_document_sha256": digest,
                    "expected_section_title": section or None,
                    "expected_evidence_excerpt": content[:240],
                    "annotation_status": "needs_independent_human_review",
                    "reviewer_a": None,
                    "reviewer_b": None,
                    "adjudication": None,
                }
            )
        if document_candidates:
            candidate_pools.append(document_candidates)

    # 按文档轮询抽取，避免大文档或文件名排序靠前的文档占满整个标注表。
    candidates: list[dict[str, Any]] = []
    candidate_index = 0
    while len(candidates) < max_candidates:
        added = False
        for pool in candidate_pools:
            if candidate_index < len(pool):
                candidates.append(pool[candidate_index])
                added = True
                if len(candidates) >= max_candidates:
                    break
        if not added:
            break
        candidate_index += 1

    unique_hashes = sorted(seen_hashes)
    corpus_fingerprint = _sha256("\n".join(unique_hashes).encode("utf-8"))
    duplicate_count = sum(1 for item in manifest_files if item["duplicate_of"])
    manifest = {
        "schema_version": "document-corpus-manifest-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root_name": source_dir.name,
        "corpus_fingerprint": corpus_fingerprint,
        "file_count": len(manifest_files),
        "unique_document_count": len(unique_hashes),
        "exact_duplicate_count": duplicate_count,
        "parse_failure_count": len(failures),
        "total_blocks": total_blocks,
        "total_chunks": total_chunks,
        "files": manifest_files,
        "failures": failures,
        "production_quality_claim_allowed": False,
    }
    worksheet = {
        "schema_version": "document-retrieval-holdout-v1",
        "dataset_version": f"{source_dir.name}-{corpus_fingerprint[:12]}-draft",
        "corpus_fingerprint": corpus_fingerprint,
        "provenance": {
            "source_name": source_dir.name,
            "annotation_method": "unreviewed_machine_assisted_draft",
            "independent_from_system": False,
            "deidentified": None,
            "reviewer_count": 0,
            "split": "holdout",
        },
        "cases": candidates,
        "readiness": {
            "holdout_ready": False,
            "blockers": [
                "questions_not_written_by_independent_humans",
                "annotations_not_double_reviewed_or_adjudicated",
                "deidentification_not_confirmed",
                "acceptance_thresholds_not_owned_by_business",
            ],
        },
    }
    return manifest, worksheet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-candidates", type=int, default=200)
    args = parser.parse_args()

    manifest, worksheet = prepare_corpus(
        args.source_dir.resolve(),
        max_candidates=args.max_candidates,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "corpus-manifest.json"
    worksheet_path = args.output_dir / "annotation-worksheet.draft.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    worksheet_path.write_text(
        json.dumps(worksheet, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("=== 真实文档语料准备 ===")
    print(
        f"files={manifest['file_count']} unique={manifest['unique_document_count']} "
        f"duplicates={manifest['exact_duplicate_count']} failures={manifest['parse_failure_count']}"
    )
    print(
        f"blocks={manifest['total_blocks']} chunks={manifest['total_chunks']} "
        f"draft_candidates={len(worksheet['cases'])}"
    )
    print(f"manifest={manifest_path.resolve()}")
    print(f"annotation_draft={worksheet_path.resolve()}")
    print("holdout_ready=false；必须完成人工独立提问、双人标注/裁决和脱敏确认。")
    return 1 if manifest["parse_failure_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
