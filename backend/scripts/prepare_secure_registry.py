"""Create ephemeral TLS and bcrypt htpasswd material for a local Registry."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


ROOT = Path(__file__).resolve().parents[2]
TMP_ROOT = (ROOT / ".tmp").resolve()
HTTPD_IMAGE = "httpd@sha256:698a0f59374eeb59c2c3ae6385c5f346edd2c70c2b63f7b552fb3d64dbc3cf66"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


def confined_output_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved == TMP_ROOT or TMP_ROOT not in resolved.parents:
        raise ValueError("secure Registry material must be under repository .tmp")
    if not resolved.name.startswith("secure-registry-"):
        raise ValueError("secure Registry directory must use secure-registry- prefix")
    return resolved


def _write_private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def generate_certificates(output_dir: Path, *, now: datetime | None = None) -> dict[str, str]:
    current = now or datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PIC Local Registry CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(current - timedelta(minutes=5))
        .not_valid_after(current + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(current - timedelta(minutes=5))
        .not_valid_after(current + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    certs = output_dir / "certs"
    certs.mkdir(parents=True, exist_ok=False)
    ca_bytes = ca_cert.public_bytes(serialization.Encoding.PEM)
    server_bytes = server_cert.public_bytes(serialization.Encoding.PEM)
    key_bytes = server_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    (certs / "ca.crt").write_bytes(ca_bytes)
    (certs / "registry.crt").write_bytes(server_bytes)
    _write_private(certs / "registry.key", key_bytes)
    return {
        "ca_fingerprint": hashlib.sha256(ca_cert.public_bytes(serialization.Encoding.DER)).hexdigest()[:16],
        "server_certificate_fingerprint": hashlib.sha256(
            server_cert.public_bytes(serialization.Encoding.DER)
        ).hexdigest()[:16],
    }


def generate_htpasswd(output_dir: Path, *, username: str, password: str) -> str:
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Registry username is invalid")
    if len(password) < 24:
        raise ValueError("Registry password must contain at least 24 characters")
    completed = subprocess.run(
        ["docker", "run", "--rm", "-i", "--entrypoint", "htpasswd", HTTPD_IMAGE, "-Bni", username],
        input=password + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("failed to generate bcrypt htpasswd material")
    line = completed.stdout.strip()
    if not line.startswith(username + ":$2") or password in line:
        raise RuntimeError("htpasswd output is invalid")
    auth = output_dir / "auth"
    auth.mkdir(parents=True, exist_ok=False)
    _write_private(auth / "htpasswd", (line + "\n").encode("utf-8"))
    return hashlib.sha256(username.encode("utf-8")).hexdigest()[:16]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args(argv)
    username = os.environ.get("SECURE_REGISTRY_USERNAME", "")
    password = os.environ.get("SECURE_REGISTRY_PASSWORD", "")
    try:
        output = confined_output_dir(args.output_dir)
        if output.exists():
            raise ValueError("secure Registry material directory already exists")
        output.mkdir(parents=True)
        fingerprints = generate_certificates(output)
        username_fingerprint = generate_htpasswd(
            output, username=username, password=password
        )
        payload = {
            "schema_version": "secure-registry-material-v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "executed": True,
            "passed": True,
            **fingerprints,
            "username_fingerprint": username_fingerprint,
            "httpd_tool_image": HTTPD_IMAGE,
            "tls_enabled": True,
            "basic_auth_enabled": True,
            "ephemeral_material": True,
            "production_ready": False,
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
