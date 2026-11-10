from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "verify_registry_artifact.py"
spec = importlib.util.spec_from_file_location("verify_registry_artifact_under_test", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_loopback_registry_can_be_explicitly_authorized_for_http():
    location = module.resolve_registry_location(
        "127.0.0.1:15000",
        allow_insecure_localhost=True,
    )

    assert location.authority == "127.0.0.1:15000"
    assert location.api_base_url == "http://127.0.0.1:15000"
    assert location.loopback is True


def test_insecure_remote_registry_is_rejected():
    with pytest.raises(ValueError, match="仅允许 localhost"):
        module.resolve_registry_location(
            "registry.example.test:5000",
            allow_insecure_localhost=True,
        )


@pytest.mark.parametrize(
    "value",
    (
        "http://127.0.0.1:15000",
        "user:secret@127.0.0.1:15000",
        "127.0.0.1:15000/path",
        "127.0.0.1:15000?token=secret",
    ),
)
def test_registry_authority_rejects_protocol_credentials_and_paths(value: str):
    with pytest.raises(ValueError):
        module.resolve_registry_location(value, allow_insecure_localhost=True)


def test_https_remote_registry_is_allowed_without_insecure_override():
    location = module.resolve_registry_location(
        "registry.example.test",
        allow_insecure_localhost=False,
    )

    assert location.api_base_url == "https://registry.example.test"
    assert location.loopback is False
