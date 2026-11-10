from __future__ import annotations

import importlib.util
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography import x509


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "backend" / "scripts" / "prepare_secure_registry.py"
SPEC = importlib.util.spec_from_file_location("secure_registry_material_under_test", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_output_directory_must_be_confined_to_repository_tmp(tmp_path: Path):
    with pytest.raises(ValueError, match="repository .tmp"):
        MODULE.confined_output_dir(tmp_path / "secure-registry-outside")

    with pytest.raises(ValueError, match="secure-registry- prefix"):
        MODULE.confined_output_dir(ROOT / ".tmp" / "wrong-prefix")


def test_generated_server_certificate_covers_loopback_names(tmp_path: Path):
    output = ROOT / ".tmp" / f"secure-registry-pytest-{tmp_path.name}"
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    try:
        fingerprints = MODULE.generate_certificates(
            output, now=datetime(2026, 11, 10, tzinfo=UTC)
        )
        certificate = x509.load_pem_x509_certificate(
            (output / "certs" / "registry.crt").read_bytes()
        )
        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value

        assert san.get_values_for_type(x509.DNSName) == ["localhost"]
        assert [str(value) for value in san.get_values_for_type(x509.IPAddress)] == [
            "127.0.0.1"
        ]
        assert len(fingerprints["ca_fingerprint"]) == 16
        assert len(fingerprints["server_certificate_fingerprint"]) == 16
        assert (output / "certs" / "registry.key").is_file()
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_htpasswd_rejects_weak_credentials_before_running_docker(monkeypatch, tmp_path: Path):
    called = False

    def _unexpected_run(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal called
        called = True
        raise AssertionError("docker must not run for invalid credentials")

    monkeypatch.setattr(MODULE.subprocess, "run", _unexpected_run)
    with pytest.raises(ValueError, match="username"):
        MODULE.generate_htpasswd(tmp_path, username="x", password="x" * 24)
    with pytest.raises(ValueError, match="24 characters"):
        MODULE.generate_htpasswd(tmp_path, username="pic-ci", password="short")
    assert called is False
