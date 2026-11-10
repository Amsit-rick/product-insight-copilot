"""Verify a loopback Registry with TLS, Basic Auth, push, and digest pull."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def _basic_auth(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def _request(
    *,
    endpoint: str,
    method: str,
    path: str,
    ca_cert: Path | None,
    username: str | None = None,
    password: str | None = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("secure Registry endpoint must be loopback HTTPS")
    context = (
        ssl.create_default_context(cafile=str(ca_cert))
        if ca_cert is not None
        else ssl.create_default_context()
    )
    connection = http.client.HTTPSConnection(
        parsed.hostname, parsed.port or 443, timeout=5, context=context
    )
    request_headers = dict(headers or {})
    if username is not None and password is not None:
        request_headers["Authorization"] = _basic_auth(username, password)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        response_body = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, response_headers, response_body
    finally:
        connection.close()


def build_probe_artifact(source_sha: str) -> dict[str, Any]:
    if not SHA_RE.fullmatch(source_sha):
        raise ValueError("source SHA must be 40 lowercase hexadecimal characters")
    config = json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": []},
            "config": {"Labels": {"org.opencontainers.image.revision": source_sha}},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    layer = b"pic-secure-registry-probe\n"
    config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    layer_digest = "sha256:" + hashlib.sha256(layer).hexdigest()
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "size": len(config),
                "digest": config_digest,
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "size": len(layer),
                    "digest": layer_digest,
                }
            ],
            "annotations": {"org.opencontainers.image.revision": source_sha},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "config": config,
        "config_digest": config_digest,
        "layer": layer,
        "layer_digest": layer_digest,
        "manifest": manifest,
        "manifest_digest": "sha256:" + hashlib.sha256(manifest).hexdigest(),
    }


def _location_path(location: str) -> str:
    parsed = urlsplit(location)
    if parsed.scheme and parsed.scheme != "https":
        raise ValueError("Registry upload location must use HTTPS")
    path = parsed.path
    if parsed.query:
        path += "?" + parsed.query
    if not path.startswith("/v2/"):
        raise ValueError("Registry upload location is invalid")
    return path


def _upload_blob(
    *, endpoint: str, repository: str, digest: str, content: bytes,
    ca_cert: Path, username: str, password: str,
) -> None:
    status, headers, _ = _request(
        endpoint=endpoint,
        method="POST",
        path=f"/v2/{repository}/blobs/uploads/",
        ca_cert=ca_cert,
        username=username,
        password=password,
        headers={"Content-Length": "0"},
    )
    if status != 202 or "location" not in headers:
        raise RuntimeError("Registry did not start a blob upload")
    location = _location_path(headers["location"])
    separator = "&" if "?" in location else "?"
    target = location + separator + urlencode({"digest": digest})
    status, _, _ = _request(
        endpoint=endpoint,
        method="PUT",
        path=target,
        ca_cert=ca_cert,
        username=username,
        password=password,
        body=content,
        headers={"Content-Type": "application/octet-stream"},
    )
    if status != 201:
        raise RuntimeError("Registry blob upload did not complete")


def verify_secure_registry(
    *, endpoint: str, ca_cert: Path, source_sha: str, username: str, password: str
) -> dict[str, Any]:
    if not ca_cert.is_file():
        raise ValueError("Registry CA certificate is missing")
    if len(username) < 3 or len(password) < 24:
        raise ValueError("Registry credentials are missing or too short")
    deadline = time.monotonic() + 30
    while True:
        try:
            status, _, _ = _request(
                endpoint=endpoint, method="GET", path="/v2/", ca_cert=ca_cert,
                username=username, password=password,
            )
            if status == 200:
                break
        except (OSError, ssl.SSLError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("secure Registry did not become ready")
        time.sleep(0.5)

    untrusted_tls_rejected = False
    try:
        _request(endpoint=endpoint, method="GET", path="/v2/", ca_cert=None)
    except ssl.SSLCertVerificationError:
        untrusted_tls_rejected = True
    if not untrusted_tls_rejected:
        raise RuntimeError("Registry certificate was unexpectedly trusted without the rehearsal CA")

    unauthorized_status, unauthorized_headers, _ = _request(
        endpoint=endpoint, method="GET", path="/v2/", ca_cert=ca_cert
    )
    wrong_status, _, _ = _request(
        endpoint=endpoint, method="GET", path="/v2/", ca_cert=ca_cert,
        username=username, password=password + "-wrong",
    )
    if unauthorized_status != 401 or "basic" not in unauthorized_headers.get("www-authenticate", "").lower():
        raise RuntimeError("Registry did not require Basic authentication")
    if wrong_status != 401:
        raise RuntimeError("Registry accepted incorrect credentials")

    artifact = build_probe_artifact(source_sha)
    repository = "pic-secure-probe"
    _upload_blob(
        endpoint=endpoint, repository=repository, digest=artifact["config_digest"],
        content=artifact["config"], ca_cert=ca_cert, username=username, password=password,
    )
    _upload_blob(
        endpoint=endpoint, repository=repository, digest=artifact["layer_digest"],
        content=artifact["layer"], ca_cert=ca_cert, username=username, password=password,
    )
    tag = "probe-" + source_sha[:12]
    status, headers, _ = _request(
        endpoint=endpoint,
        method="PUT",
        path=f"/v2/{repository}/manifests/{tag}",
        ca_cert=ca_cert,
        username=username,
        password=password,
        body=artifact["manifest"],
        headers={"Content-Type": OCI_MANIFEST},
    )
    if status != 201 or headers.get("docker-content-digest") != artifact["manifest_digest"]:
        raise RuntimeError("Registry manifest push did not preserve digest")
    status, headers, pulled = _request(
        endpoint=endpoint,
        method="GET",
        path=f"/v2/{repository}/manifests/{artifact['manifest_digest']}",
        ca_cert=ca_cert,
        username=username,
        password=password,
        headers={"Accept": OCI_MANIFEST},
    )
    pulled_digest = "sha256:" + hashlib.sha256(pulled).hexdigest()
    if status != 200 or pulled_digest != artifact["manifest_digest"]:
        raise RuntimeError("Registry digest pull verification failed")

    ca_der = x509_der_from_pem(ca_cert.read_bytes())
    authority = urlsplit(endpoint).netloc
    return {
        "schema_version": "secure-registry-rehearsal-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "executed": True,
        "passed": True,
        "scope": "local_tls_authenticated_registry",
        "source_git_sha": source_sha,
        "registry_endpoint_fingerprint": hashlib.sha256(authority.encode("utf-8")).hexdigest()[:16],
        "ca_certificate_fingerprint": hashlib.sha256(ca_der).hexdigest()[:16],
        "username_fingerprint": hashlib.sha256(username.encode("utf-8")).hexdigest()[:16],
        "tls_verified": True,
        "untrusted_tls_rejected": True,
        "basic_auth_required": True,
        "wrong_credentials_rejected": True,
        "push_verified": True,
        "digest_pull_verified": True,
        "manifest_digest": artifact["manifest_digest"],
        "enterprise_registry_verified": False,
        "cross_host_verified": False,
        "production_ready": False,
    }


def x509_der_from_pem(pem: bytes) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    return x509.load_pem_x509_certificate(pem).public_bytes(serialization.Encoding.DER)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--ca-cert", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args(argv)
    username = os.environ.get("SECURE_REGISTRY_USERNAME", "")
    password = os.environ.get("SECURE_REGISTRY_PASSWORD", "")
    try:
        payload = verify_secure_registry(
            endpoint=args.endpoint,
            ca_cert=args.ca_cert,
            source_sha=args.source_sha,
            username=username,
            password=password,
        )
    except (ValueError, RuntimeError, OSError, ssl.SSLError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
