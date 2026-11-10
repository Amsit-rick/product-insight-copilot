from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_github_actions_builds_verifies_and_uploads_release_bound_evidence_bundle():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "release-evidence:" in workflow
    assert "needs: [backend, frontend, e2e, production-shape, supply-chain]" in workflow
    assert "fetch-depth: 0" in workflow
    assert "actions/download-artifact@v4" in workflow
    assert "remote-ci-production-shape-${{ github.sha }}" in workflow
    assert "after_sales_release_report.py" in workflow
    assert '--expected-release-sha "$GITHUB_SHA"' in workflow
    assert "after_sales_delivery_check.py" in workflow
    assert "--fail-if-public-demo-contract-invalid" in workflow
    assert "package_delivery_evidence.py" in workflow
    assert "--attachment remote-ci-object-storage.json" in workflow
    assert "--attachment remote-ci-production-shape.json" in workflow
    assert "--attachment remote-ci-scheduler-failover.json" in workflow
    assert "--attachment remote-ci-image-attestations.json" in workflow
    assert "--attachment remote-ci-image-signature.json" in workflow
    assert "verify_delivery_evidence_bundle.py" in workflow
    assert '--expected-release-sha "$GITHUB_SHA"' in workflow
    assert "delivery-evidence-${{ github.sha }}" in workflow
    assert "after-sales-release-readiness.json" in workflow
    assert "after-sales-delivery-check.json" in workflow
    assert "delivery-evidence-manifest.json" in workflow
    assert "remote-ci-object-storage.json" in workflow
    assert "remote-ci-production-shape.json" in workflow
    assert "remote-ci-scheduler-failover.json" in workflow
    assert "remote-ci-image-attestations.json" in workflow
    assert "remote-ci-image-signature.json" in workflow
    assert "if-no-files-found: error" in workflow


def test_remote_shape_artifact_is_commit_bound_and_fails_when_evidence_is_missing():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = workflow.split("  production-shape:", 1)[1]

    assert "name: remote-ci-production-shape-${{ github.sha }}" in job
    assert "if-no-files-found: error" in job


def test_remote_evidence_job_never_generates_human_release_approval():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = workflow.split("  release-evidence:", 1)[1].split("\n  production-shape:", 1)[0]

    assert "release-decision.json" not in job
    assert "--decision-template-out" not in job
    assert "approve" not in job.lower()
    assert "--fail-if-not-local-demo" not in job


def test_release_report_coverage_gate_runs_locally_and_in_github_ci():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    local_ci = (ROOT / "scripts" / "ci.ps1").read_text(encoding="utf-8")

    for content in (workflow, local_ci):
        assert "coverage run" in content
        assert "--branch" in content
        assert "scripts/after_sales_release_report.py" in content
        assert "tests/unit/test_after_sales_release_report.py" in content
        assert "coverage report" in content
        assert "--fail-under=80" in content
        assert "coverage json" in content
        assert "verify_coverage_report.py" in content
        assert "--min-statements 80" in content
        assert "--min-branches 80" in content
