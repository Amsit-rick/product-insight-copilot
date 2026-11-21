"""Saleor 高保真 Sandbox 的容器与身份隔离契约。"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "deploy" / "docker-compose.commerce-sandbox.yml"
CONTROL = ROOT / "scripts" / "commerce_sandbox.ps1"
BOOTSTRAP = ROOT / "backend" / "scripts" / "bootstrap_saleor_runtime_identity.py"
SCENARIO_BUILDER = ROOT / "backend" / "scripts" / "build_saleor_scenarios.py"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def test_commerce_sandbox_is_optional_pinned_and_isolated():
    compose = COMPOSE.read_text(encoding="utf-8")

    assert "ghcr.io/saleor/saleor:3.23.49@sha256:bb59cbf1" in compose
    assert "postgres:15-alpine@sha256:fc41cfa8" in compose
    assert "valkey/valkey:8.1-alpine" in compose
    assert ":latest" not in compose
    assert 'profiles: ["commerce-sandbox"]' in compose
    assert compose.count("<<: *commerce-profile") >= 7

    assert "commerce-db-data:" in compose
    assert "commerce-cache-data:" in compose
    assert "commerce-seed-state:" in compose
    assert "commerce-runtime-credentials:" in compose
    assert "commerce-backend:" in compose
    assert "commerce-edge:" in compose
    assert "pic-postgres" not in compose
    assert "pic-redis" not in compose
    assert "minio" not in compose.lower()


def test_runtime_identity_is_created_outside_saleor_and_not_exposed_as_env_token():
    compose = COMPOSE.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")

    assert "bootstrap_saleor_runtime_identity.py" in compose
    assert "commerce-credentials-init:" in compose
    assert 'user: "0:0"' in compose
    assert "chown 10001:10001 /run/commerce-credentials" in compose
    assert "commerce-runtime-credentials:/run/commerce-credentials" in compose
    assert "SALEOR_APP_TOKEN_FILE: /run/commerce-credentials/app-token" in compose
    assert "SALEOR_APP_TOKEN:" not in compose
    assert "/run/commerce-seed-state/seeded" in compose
    assert "createsuperuser --noinput" in compose
    assert "--superuser_password" not in compose
    assert "appCreate" in bootstrap
    assert "MANAGE_ORDERS" in bootstrap
    assert "authToken" in bootstrap
    assert "print(token" not in bootstrap
    assert "logger.info(token" not in bootstrap


def test_scenario_manifest_is_built_once_and_mounted_read_only_at_runtime():
    compose = COMPOSE.read_text(encoding="utf-8")
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")

    assert "commerce-scenarios:" in compose
    assert "build_saleor_scenarios.py" in compose
    assert "commerce-scenario-manifest:/run/commerce-scenarios" in compose
    assert "commerce-scenario-manifest:/run/commerce-scenarios:ro" in compose
    assert "SANDBOX_SCENARIO_MANIFEST_FILE: /run/commerce-scenarios/manifest.json" in compose
    assert "commerce-scenarios:\n        condition: service_completed_successfully" in compose
    assert "commerce-scenario-manifest:" in compose
    assert "COPY backend/scripts/build_saleor_scenarios.py" in dockerfile


def test_scenario_builder_uses_a_fixed_read_only_query_and_never_logs_credentials():
    source = SCENARIO_BUILDER.read_text(encoding="utf-8")
    lowered = source.lower()

    assert "query sandboxscenarioorders" in lowered
    assert "mutation" not in lowered
    assert "__schema" not in lowered
    assert "__type" not in lowered
    assert "address" not in lowered
    assert "email" not in lowered
    assert "phone" not in lowered
    assert "print(token" not in lowered
    assert "logger.info(token" not in lowered


def test_control_script_scopes_reset_to_the_dedicated_compose_project():
    script = CONTROL.read_text(encoding="utf-8")

    assert '"product-insight-commerce"' in script
    assert "docker compose" in script
    assert "--profile commerce-sandbox" in script
    assert '@("down", "--volumes", "--remove-orphans")' in script
    assert "deploy\\docker-compose.yml" not in script
    assert "Remove-Item" not in script
    assert "rm -rf" not in script


def test_default_compose_remains_free_of_saleor_services():
    default_compose = (ROOT / "deploy" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )

    assert "ghcr.io/saleor" not in default_compose
    assert "commerce-db" not in default_compose
    assert "commerce-api" not in default_compose


def test_ci_validates_the_optional_commerce_compose_without_starting_it():
    workflow = CI.read_text(encoding="utf-8")

    assert "docker-compose.commerce-sandbox.yml" in workflow
    assert "--profile commerce-sandbox config --quiet" in workflow
    assert "SALEOR_SECRET_KEY:" in workflow
    assert "SALEOR_SEED_ADMIN_PASSWORD:" in workflow
