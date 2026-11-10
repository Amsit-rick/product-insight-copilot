"""容器交付文件的静态契约；真正镜像构建仍由 Docker 门禁负责。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_api_image_builds_and_copies_locked_frontend():
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "COPY frontend/package.json frontend/package-lock.json" in dockerfile
    assert "RUN npm ci --no-audit --no-fund" in dockerfile
    assert "COPY --from=frontend-build /build/frontend/dist ./frontend/dist/" in dockerfile
    assert "node:20-alpine@sha256:" in dockerfile
    assert "python:3.12-slim@sha256:" in dockerfile
    assert "COPY backend/requirements-runtime.lock ./" in dockerfile
    assert "--require-hashes -r requirements-runtime.lock" in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
    assert dockerfile.index("--require-hashes -r requirements-runtime.lock") < dockerfile.index(
        "ARG APP_RELEASE_SHA=unknown"
    )
    assert "FRONTEND_DIST_DIR=/app/frontend/dist" in dockerfile
    assert "http://127.0.0.1:8000/health/ready" in dockerfile
    assert "COPY backend/scripts/preflight_check.py ./scripts/preflight_check.py" in dockerfile
    assert "COPY backend/scripts/verify_ai_integrations.py ./scripts/verify_ai_integrations.py" in dockerfile
    assert "COPY backend/scripts/verify_fact_integrations.py ./scripts/verify_fact_integrations.py" in dockerfile
    assert (
        "COPY backend/scripts/verify_webhook_integration.py "
        "./scripts/verify_webhook_integration.py"
    ) in dockerfile
    assert (
        "COPY backend/scripts/verify_object_storage.py "
        "./scripts/verify_object_storage.py"
    ) in dockerfile
    assert (
        "COPY backend/scripts/verify_target_environment.py "
        "./scripts/verify_target_environment.py"
    ) in dockerfile
    assert (
        "COPY backend/scripts/verify_scheduler_failover.py "
        "./scripts/verify_scheduler_failover.py"
    ) in dockerfile
    assert (
        "COPY backend/scripts/verify_metric_source.py "
        "./scripts/verify_metric_source.py"
    ) in dockerfile
    assert (
        "COPY docs/integrations/fact-probe-plan.example.json "
        "./docs/integrations/fact-probe-plan.example.json"
    ) in dockerfile
    assert "!docs/integrations/" in dockerignore
    assert "!docs/integrations/fact-probe-plan.example.json" in dockerignore


def test_compose_uses_repository_root_as_build_context():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    assert compose.count("context: ..") == 5
    assert compose.count("dockerfile: backend/Dockerfile") == 5
    assert compose.count("APP_RELEASE_SHA: ${APP_RELEASE_SHA:-dev}") == 5


def test_release_compose_removes_source_builds_and_requires_digest_image():
    release = (ROOT / "deploy" / "docker-compose.release.yml").read_text(encoding="utf-8")

    for service in ("migrate", "api", "worker", "proactive-scheduler", "integration-sandbox"):
        assert f"\n  {service}:\n" in release
    assert release.count("build: !reset null") == 5
    assert release.count("PIC_API_IMAGE must be name@sha256:digest") == 5
    assert release.count("pull_policy: ${PIC_RELEASE_PULL_POLICY:-always}") == 5


def test_local_registry_is_loopback_only_and_resource_bounded():
    registry = (ROOT / "deploy" / "docker-compose.local-registry.yml").read_text(
        encoding="utf-8"
    )

    assert "registry:2@sha256:" in registry
    assert '127.0.0.1:${LOCAL_REGISTRY_PORT:-15000}:5000' in registry
    assert "REGISTRY_STORAGE_DELETE_ENABLED" in registry
    assert "cpus:" in registry
    assert "mem_limit:" in registry
    assert "pids_limit:" in registry
    assert "http://127.0.0.1:5000/v2/" in registry


def test_registry_rollback_rehearsal_waits_for_api_and_workers():
    script = (ROOT / "scripts" / "rehearse-registry-rollback.ps1").read_text(
        encoding="utf-8"
    )

    assert "function Wait-Application" in script
    assert "function Wait-ApiHealthy" in script
    assert "function Wait-WorkersHealthy" in script
    assert 'label=com.docker.compose.service=api' in script
    assert 'label=com.docker.compose.service=worker' in script
    assert "Wait-Application\n    Wait-ApiHealthy\n    Wait-WorkersHealthy" in script
    assert "RestoreCurrentBeforeExit" in script
    assert 'schema_version = "registry-rollout-rollback-evidence-v1"' in script
    assert "current_restored_for_target_probe" in script
    assert "docker image inspect $Image | Out-String" in script
    assert "ConvertFrom-Json" in script
    assert "--format '{{ index .Config.Labels" not in script
    assert "function Get-ImageMigrationInventory" in script
    assert "-exec basename {} \\; | sort" in script
    assert "database_migration_change_detected" in script
    assert "database_migration_rollback_required" in script
    assert "database_migration_rollback_exercised" in script
    assert "database_release_safety_verified" in script
    assert "migration_inventory_identical" in script
    assert "DatabaseRollbackEvidence" in script
    assert "verify_database_rollback_evidence.py" in script
    assert "snapshot_restore_rehearsed" in script


def test_database_migration_rollback_rehearsal_is_bound_and_restores_snapshot():
    script = (
        ROOT / "scripts" / "rehearse-database-migration-rollback.ps1"
    ).read_text(encoding="utf-8")
    dockerfile = (
        ROOT / "backend" / "tests" / "fixtures" / "database-rollback" / "Dockerfile"
    ).read_text(encoding="utf-8")
    migration = (
        ROOT
        / "backend"
        / "tests"
        / "fixtures"
        / "database-rollback"
        / "016_p3_32_release_marker.sql"
    ).read_text(encoding="utf-8")

    assert "database-migration-rollback-rehearsal-v1" in script
    assert "pg_dump" in script
    assert "pg_restore --exit-on-error" in script
    assert "candidate_migration_applied" in script
    assert "candidate_schema_absent_after_restore" in script
    assert "business_data_restored" in script
    assert "snapshot_sha256" in script
    assert "snapshot_size_bytes" in script
    assert "observed_rpo_seconds" in script
    assert "observed_rto_seconds" in script
    assert "backup_encrypted = $false" in script
    assert "offsite_copy_verified = $false" in script
    assert "retention_policy_enforced = $false" in script
    assert "snapshot_restore_rehearsed" in script
    assert "production_ready = $false" in script
    assert "$ContainerCreated = $false" in script
    assert "$ContainerCreated = $created -eq $ContainerName" in script
    assert "if ($ContainerCreated -and -not $KeepResources)" in script
    assert "finally {" in script
    assert "docker rm -f" in script
    assert "ARG BASE_IMAGE=pic-api:local" in dockerfile
    assert "FROM ${BASE_IMAGE}" in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
    assert "release_migration_probe" in migration


def test_database_recovery_objective_verifier_is_available_to_release_admission():
    verifier = ROOT / "backend" / "scripts" / "verify_database_recovery_objectives.py"
    admission = ROOT / "backend" / "scripts" / "verify_target_release_admission.py"

    assert verifier.is_file()
    verifier_text = verifier.read_text(encoding="utf-8")
    admission_text = admission.read_text(encoding="utf-8")

    assert "database-recovery-objectives-verification-v1" in verifier_text
    assert "snapshot_integrity_verified" in verifier_text
    assert "recovery_timeline_verified" in verifier_text
    assert "rpo_objective_met" in verifier_text
    assert "rto_objective_met" in verifier_text
    assert "enterprise_backup_governance_verified" in verifier_text
    assert '"database_recovery"' in admission_text
    assert "database_recovery_objectives_verified" in admission_text
    assert "enterprise_backup_governance_verified" in admission_text


def test_image_attestation_rehearsal_requires_clean_git_and_buildkit_evidence():
    script = (ROOT / "scripts" / "rehearse-image-attestations.ps1").read_text(
        encoding="utf-8"
    )

    assert "git status --porcelain" in script
    assert "dirty Git worktree" in script
    assert "--provenance=mode=max" in script
    assert "--sbom=true" in script
    assert "--minimum-sbom-packages 50" in script
    assert "verify_image_attestations.py" in script


def test_platform_neutral_ci_attestation_script_fails_closed_and_cleans_up():
    script = (ROOT / "scripts" / "ci-image-attestations.sh").read_text(
        encoding="utf-8"
    )

    assert "set -euo pipefail" in script
    assert "release SHA does not match checked-out Git HEAD" in script
    assert "dirty Git worktree" in script
    assert "Docker daemon is unavailable to this runner" in script
    assert "Docker Buildx is unavailable to this runner" in script
    assert "Docker Compose is unavailable to this runner" in script
    assert "cannot inspect Compose project" in script
    assert "--provenance=mode=max" in script
    assert "--sbom=true" in script
    assert "--minimum-sbom-packages 50" in script
    assert "verify_image_signature.py" in script
    assert "SIGN_IMAGE" in script
    assert "COSIGN_IMAGE" in script
    assert "signing-config create" in script
    assert "--insecure-ignore-tlog" in script
    assert "COSIGN_PASSWORD" in script
    assert "docker volume rm -f" in script
    assert "pinned Cosign image version does not match" in script
    assert "_${RELEASE_SHA:0:12}_$$" in script
    assert "KEY_VOLUME_INIT_IMAGE" in script
    assert "chown 65532:65532 /keys" in script
    assert "trap cleanup EXIT" in script
    assert "down -v --remove-orphans" in script
    assert "production_ready" not in script


def test_self_hosted_supply_chain_wrapper_binds_preflight_and_image_evidence():
    script = (ROOT / "scripts" / "ci-self-hosted-supply-chain.sh").read_text(
        encoding="utf-8"
    )

    assert "set -euo pipefail" in script
    assert "verify_self_hosted_runner.py" in script
    assert "MINIMUM_MEMORY_MB" in script
    assert "MINIMUM_DISK_MB" in script
    assert "RUNNER_LOCK_DIR" in script
    assert "another supply-chain run owns the runner lock" in script
    assert "RUNNER_TIMEOUT_SECONDS" in script
    assert "RUNNER_DEADLINE_EPOCH" in script
    assert "run_with_deadline" in script
    assert 'timeout -s TERM -k 30 "${remaining}"' in script
    assert script.count("run_with_deadline") == 4
    assert "trap cleanup_lock EXIT INT TERM" in script
    assert "RELEASE_SHA must be a full lowercase Git SHA" in script
    assert "RUNNER_LOCK_DIR must be a .lock directory under repository .tmp" in script
    assert "self_hosted_supply_chain_runner" in script
    assert "local_host_rehearsal" in script
    assert "ci-image-attestations.sh" in script
    assert "SIGN_IMAGE=true" in script
    assert "package_self_hosted_supply_chain_evidence.py" in script
    assert "supply-chain-bundle.json" in script
    assert "production_ready" not in script


def test_remote_contract_lock_contains_only_pytest_dependency_chain():
    lock = (ROOT / "backend" / "requirements-ci-contract.lock").read_text(
        encoding="utf-8"
    )
    package_rows = {
        line.split("==", 1)[0]
        for line in lock.splitlines()
        if line and not line.startswith(("#", " ")) and "==" in line
    }

    assert package_rows == {
        "colorama",
        "iniconfig",
        "packaging",
        "pluggy",
        "pygments",
        "pytest",
    }
    assert lock.count("--hash=sha256:") == 12


def test_remote_ci_generates_ephemeral_signature_without_claiming_production():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "ci-image-attestations.sh").read_text(
        encoding="utf-8"
    )

    assert "supply-chain:" in workflow
    assert "bash scripts/ci-image-attestations.sh" in workflow
    assert "RELEASE_SHA: ${{ github.sha }}" in workflow
    assert "--provenance=mode=max" in script
    assert "--sbom=true" in script
    assert "verify_image_attestations.py" in script
    assert "remote-ci-image-attestations.json" in workflow
    assert "remote-ci-image-signature.json" in workflow
    assert "SIGN_IMAGE: 'true'" in workflow
    assert "supply-chain-evidence-${{ github.sha }}" in workflow


def test_minio_initializer_retries_until_service_accepts_connections():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "until /opt/bitnami/minio-client/bin/mc alias set" in compose
    assert 'if [ "$$attempt" -ge 20 ]' in compose
    assert "sleep 2" in compose


def test_compose_runs_versioned_migrations_before_api_and_worker():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    migrate_section = compose.split("\n  migrate:\n", 1)[1].split("\n  api:\n", 1)[0]
    assert 'command: ["python", "-m", "app.db.migrate", "apply"]' in migrate_section
    assert 'restart: "no"' in migrate_section

    api_section = compose.split("\n  api:\n", 1)[1].split("\n  worker:\n", 1)[0]
    worker_section = compose.split("\n  worker:\n", 1)[1]
    dependency = "migrate:\n        condition: service_completed_successfully"
    assert dependency in api_section
    assert dependency in worker_section


def test_application_containers_have_least_privilege_and_resource_boundaries():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    assert 'read_only: true' in compose
    assert 'cap_drop: ["ALL"]' in compose
    assert 'security_opt: ["no-new-privileges:true"]' in compose
    assert '/tmp:rw,noexec,nosuid,size=64m' in compose

    api_section = compose.split("\n  api:\n", 1)[1].split("\n  worker:\n", 1)[0]
    worker_section = compose.split("\n  worker:\n", 1)[1].split(
        "\n  proactive-scheduler:\n", 1
    )[0]
    for section in (api_section, worker_section):
        assert "<<: *pic-app-hardening" in section
        assert "cpus:" in section
        assert "mem_limit:" in section
        assert "pids_limit:" in section

    assert '["CMD", "python", "-m", "app.worker_health"]' in worker_section
    assert "WORKER_HEARTBEAT_PATH: /tmp/pic-worker-heartbeat" in worker_section


def test_infrastructure_images_are_immutable_by_default():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    for image in (
        "pgvector/pgvector:pg16@sha256:",
        "redis:7-alpine@sha256:",
        "qdrant/qdrant:v1.13.2@sha256:",
        "bitnamilegacy/minio:2025.7.23-debian-12-r5@sha256:",
    ):
        assert image in compose
    assert "image: ${PIC_API_IMAGE:-pic-api:local}" in compose


def test_single_host_infrastructure_services_have_resource_limits():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    service_boundaries = {
        "postgres": "redis",
        "redis": "qdrant",
        "qdrant": "minio",
        "minio": "minio-init",
    }
    for service, next_service in service_boundaries.items():
        section = compose.split(f"\n  {service}:\n", 1)[1].split(
            f"\n  {next_service}:\n", 1
        )[0]
        assert "cpus:" in section
        assert "mem_limit:" in section
        assert "pids_limit:" in section


def test_compose_proactive_scheduler_is_single_explicit_profile_service():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "\n  proactive-scheduler:\n" in compose
    section = compose.split("\n  proactive-scheduler:\n", 1)[1].split(
        "\n  integration-sandbox:\n", 1
    )[0]
    assert 'profiles: ["proactive"]' in section
    assert 'command: ["python", "-m", "app.proactive_scheduler"]' in section
    assert 'PROACTIVE_ANOMALY_ENABLED: ${PROACTIVE_ANOMALY_ENABLED:-false}' in compose
    assert (
        'PROACTIVE_ANOMALY_MAX_DATA_AGE_DAYS: '
        '${PROACTIVE_ANOMALY_MAX_DATA_AGE_DAYS:-2}'
    ) in compose
    assert (
        'PROACTIVE_ANOMALY_MAX_DATA_AGE_BY_METRIC: '
        '${PROACTIVE_ANOMALY_MAX_DATA_AGE_BY_METRIC:-{}}'
    ) in compose
    assert 'METRIC_SOURCE_HTTP_ENABLED: ${METRIC_SOURCE_HTTP_ENABLED:-false}' in compose
    assert 'METRIC_SOURCE_HTTP_URL: ${METRIC_SOURCE_HTTP_URL:-}' in compose
    assert 'METRIC_SOURCE_HTTP_AUTH_HEADERS: ${METRIC_SOURCE_HTTP_AUTH_HEADERS:-{}}' in compose
    assert "ports:" not in section
    assert "condition: service_completed_successfully" in section


def test_optional_storage_and_vector_services_do_not_block_default_startup():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    qdrant_section = compose.split("\n  qdrant:\n", 1)[1].split("\n  minio:\n", 1)[0]
    minio_section = compose.split("\n  minio:\n", 1)[1].split("\n  minio-init:\n", 1)[0]
    assert 'profiles: ["qdrant"]' in qdrant_section
    assert 'profiles: ["minio"]' in minio_section
    assert "STORAGE_BACKEND: ${STORAGE_BACKEND:-local}" in compose
    assert "QDRANT_URL: ${QDRANT_URL:-}" in compose
    assert "bitnamilegacy/minio:2025.7.23-debian-12-r5" in compose
    assert "/opt/bitnami/minio-client/bin/mc" in compose
    assert "MINIO_ENDPOINT: ${MINIO_ENDPOINT:-minio:9000}" in compose
    assert "MINIO_SECURE: ${MINIO_SECURE:-false}" in compose
    assert "MINIO_ACCESS_KEY: ${MINIO_ACCESS_KEY:-${MINIO_ROOT_USER:-picminio}}" in compose
    assert "MINIO_SECRET_KEY: ${MINIO_SECRET_KEY:-${MINIO_ROOT_PASSWORD:-picminio123}}" in compose


def test_api_mounts_release_evidence_read_only_and_binds_build_sha():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "APP_RELEASE_SHA: ${APP_RELEASE_SHA:-}" in compose
    assert "DELIVERY_EVIDENCE_DIR: /app/delivery-evidence" in compose
    assert (
        "DELIVERY_EVIDENCE_MAX_AGE_HOURS: "
        "${DELIVERY_EVIDENCE_MAX_AGE_HOURS:-24}"
    ) in compose
    api_section = compose.split("\n  api:\n", 1)[1].split("\n  worker:\n", 1)[0]
    assert (
        "${DELIVERY_EVIDENCE_HOST_DIR:-../docs/testing/artifacts}:"
        "/app/delivery-evidence:ro"
    ) in api_section


def test_remote_ci_exercises_single_runner_production_shape_without_claiming_cross_host():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "production-shape:" in workflow
    assert "--profile minio" in workflow
    assert "--scale worker=2" in workflow
    assert "verify_object_storage.py" in workflow
    assert "--allow-real-storage-calls --action full" in workflow
    assert "verify_target_environment.py" in workflow
    assert "verify_scheduler_failover.py" in workflow
    assert "remote-ci-scheduler-failover.json" in workflow
    assert "--expected-env test" in workflow
    assert "--allow-insecure-http" in workflow
    assert "remote-ci-production-shape" in workflow
    assert "if: always()" in workflow
    assert "down -v --remove-orphans" in workflow

    api_section = compose.split("\n  api:\n", 1)[1].split("\n  worker:\n", 1)[0]
    worker_section = compose.split("\n  worker:\n", 1)[1]
    assert "      minio:\n" not in api_section
    assert "      minio:\n" not in worker_section


def test_restore_rehearsal_counts_current_migrations_instead_of_hardcoding():
    script = (ROOT / "scripts" / "rehearse-postgres-restore.ps1").read_text(encoding="utf-8")

    assert 'Get-ChildItem -LiteralPath $MigrationDirectory -Filter "*.sql" -File' in script
    assert "[int]$migrationCount -ne $ExpectedMigrationCount" in script
    assert 'migrationCount -ne "6"' not in script
