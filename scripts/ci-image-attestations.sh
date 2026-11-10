#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
REGISTRY_PROJECT="${REGISTRY_PROJECT:-pic-ci-attestation}"
REGISTRY_PORT="${REGISTRY_PORT:-15000}"
REPOSITORY="${REPOSITORY:-pic-api}"
JSON_OUT="${JSON_OUT:-docs/testing/artifacts/remote-ci-image-attestations.json}"
SIGN_IMAGE="${SIGN_IMAGE:-false}"
SIGNATURE_JSON_OUT="${SIGNATURE_JSON_OUT:-docs/testing/artifacts/remote-ci-image-signature.json}"
EVIDENCE_SCOPE="${EVIDENCE_SCOPE:-remote_ci_local_registry}"
COSIGN_IMAGE="${COSIGN_IMAGE:-ghcr.io/sigstore/cosign/cosign@sha256:0f06154836d2de34f887a5c273506d52ca43edaa1d9e53e6510fa78a48f69c2d}"
COSIGN_VERSION="v3.1.4"
KEY_VOLUME_INIT_IMAGE="docker.io/library/alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"
REGISTRY_AUTHORITY="127.0.0.1:${REGISTRY_PORT}"
REGISTRY_COMPOSE="${ROOT}/deploy/docker-compose.local-registry.yml"
VERIFIER="${ROOT}/backend/scripts/verify_image_attestations.py"
SIGNATURE_VERIFIER="${ROOT}/backend/scripts/verify_image_signature.py"
REGISTRY_STARTED=false
SIGNING_VOLUME_CREATED=false
SIGNING_VOLUME=""
SIGNATURE_WORK_DIR=""
KEY_COPY_CONTAINER=""

absolute_path() {
  if [[ "$1" = /* ]]; then
    printf '%s\n' "$1"
  else
    printf '%s/%s\n' "${ROOT}" "$1"
  fi
}

cleanup() {
  if [[ -n "${KEY_COPY_CONTAINER}" ]]; then
    docker rm -f "${KEY_COPY_CONTAINER}" >/dev/null 2>&1 || true
  fi
  if [[ "${SIGNING_VOLUME_CREATED}" == "true" ]]; then
    docker volume rm -f "${SIGNING_VOLUME}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${SIGNATURE_WORK_DIR}" ]]; then
    case "${SIGNATURE_WORK_DIR}" in
      "${ROOT}"/.tmp/cosign-ci.*) rm -rf -- "${SIGNATURE_WORK_DIR}" ;;
    esac
  fi
  if [[ "${REGISTRY_STARTED}" == "true" ]]; then
    LOCAL_REGISTRY_PORT="${REGISTRY_PORT}" docker compose \
      -p "${REGISTRY_PROJECT}" -f "${REGISTRY_COMPOSE}" \
      down -v --remove-orphans >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

for command in git docker curl "${PYTHON_BIN}"; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "[FAIL] required command not found: ${command}" >&2
    exit 1
  fi
done
if ! docker version >/dev/null 2>&1; then
  echo "[FAIL] Docker daemon is unavailable to this runner" >&2
  exit 1
fi
if ! docker buildx version >/dev/null 2>&1; then
  echo "[FAIL] Docker Buildx is unavailable to this runner" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "[FAIL] Docker Compose is unavailable to this runner" >&2
  exit 1
fi

cd "${ROOT}"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "[FAIL] refusing attestation build from a dirty Git worktree" >&2
  exit 1
fi

HEAD_SHA="$(git rev-parse HEAD)"
RELEASE_SHA="${RELEASE_SHA:-${HEAD_SHA}}"
if [[ ! "${RELEASE_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[FAIL] release SHA must be 40 lowercase hexadecimal characters" >&2
  exit 1
fi
if [[ "${RELEASE_SHA}" != "${HEAD_SHA}" ]]; then
  echo "[FAIL] release SHA does not match checked-out Git HEAD" >&2
  exit 1
fi
SIGNING_VOLUME="${REGISTRY_PROJECT}_cosign_keys_${RELEASE_SHA:0:12}_$$"
if [[ "${EVIDENCE_SCOPE}" != "local_loopback_registry" && \
      "${EVIDENCE_SCOPE}" != "remote_ci_local_registry" ]]; then
  echo "[FAIL] unsupported evidence scope: ${EVIDENCE_SCOPE}" >&2
  exit 1
fi
if [[ "${SIGN_IMAGE}" != "true" && "${SIGN_IMAGE}" != "false" ]]; then
  echo "[FAIL] SIGN_IMAGE must be true or false" >&2
  exit 1
fi
if [[ ! "${COSIGN_IMAGE}" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]]; then
  echo "[FAIL] Cosign image must be pinned by sha256 digest" >&2
  exit 1
fi
if ! EXISTING_CONTAINERS="$(
  docker compose -p "${REGISTRY_PROJECT}" -f "${REGISTRY_COMPOSE}" ps --all --quiet
)"; then
  echo "[FAIL] cannot inspect Compose project: ${REGISTRY_PROJECT}" >&2
  exit 1
fi
if [[ -n "${EXISTING_CONTAINERS}" ]]; then
  echo "[FAIL] refusing to replace existing Compose project: ${REGISTRY_PROJECT}" >&2
  exit 1
fi

IMAGE_REFERENCE="${REGISTRY_AUTHORITY}/${REPOSITORY}:supply-chain-${RELEASE_SHA:0:12}"
ATTESTATION_OUTPUT="$(absolute_path "${JSON_OUT}")"
SIGNATURE_OUTPUT="$(absolute_path "${SIGNATURE_JSON_OUT}")"
TOTAL_STEPS=3
if [[ "${SIGN_IMAGE}" == "true" ]]; then
  TOTAL_STEPS=4
fi

echo "[1/${TOTAL_STEPS}] start loopback-only Registry"
LOCAL_REGISTRY_PORT="${REGISTRY_PORT}" docker compose \
  -p "${REGISTRY_PROJECT}" -f "${REGISTRY_COMPOSE}" up -d
REGISTRY_STARTED=true

for _ in $(seq 1 30); do
  if curl -fsS "http://${REGISTRY_AUTHORITY}/v2/" >/dev/null; then
    break
  fi
  sleep 1
done
if ! curl -fsS "http://${REGISTRY_AUTHORITY}/v2/" >/dev/null; then
  echo "[FAIL] local Registry did not become ready within 30 seconds" >&2
  exit 1
fi

echo "[2/${TOTAL_STEPS}] build and push image with SLSA provenance and SPDX SBOM"
docker buildx build \
  --file "${ROOT}/backend/Dockerfile" \
  --tag "${IMAGE_REFERENCE}" \
  --build-arg "APP_RELEASE_SHA=${RELEASE_SHA}" \
  --provenance=mode=max \
  --sbom=true \
  --push \
  "${ROOT}"

echo "[3/${TOTAL_STEPS}] verify remote OCI index, provenance and SBOM"
"${PYTHON_BIN}" "${VERIFIER}" \
  --image-reference "${IMAGE_REFERENCE}" \
  --expected-release-sha "${RELEASE_SHA}" \
  --minimum-sbom-packages 50 \
  --evidence-scope "${EVIDENCE_SCOPE}" \
  --json-out "${ATTESTATION_OUTPUT}"

if [[ "${SIGN_IMAGE}" == "true" ]]; then
  echo "[4/${TOTAL_STEPS}] sign digest and verify with ephemeral local key"
  mkdir -p "${ROOT}/.tmp"
  SIGNATURE_WORK_DIR="$(mktemp -d "${ROOT}/.tmp/cosign-ci.XXXXXX")"
  docker volume create "${SIGNING_VOLUME}" >/dev/null
  SIGNING_VOLUME_CREATED=true
  docker run --rm --platform linux/amd64 --user 0 \
    -v "${SIGNING_VOLUME}:/keys" "${KEY_VOLUME_INIT_IMAGE}" \
    chown 65532:65532 /keys
  COSIGN_VERSION_OUTPUT="$(docker run --rm --platform linux/amd64 \
    "${COSIGN_IMAGE}" version)"
  if ! grep -Eq "GitVersion:[[:space:]]+${COSIGN_VERSION}$" \
    <<< "${COSIGN_VERSION_OUTPUT}"; then
    echo "[FAIL] pinned Cosign image version does not match ${COSIGN_VERSION}" >&2
    exit 1
  fi
  COSIGN_PASSWORD="$("${PYTHON_BIN}" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  export COSIGN_PASSWORD
  docker run --rm --network host --platform linux/amd64 \
    -e COSIGN_PASSWORD -v "${SIGNING_VOLUME}:/keys" "${COSIGN_IMAGE}" \
    generate-key-pair --output-key-prefix /keys/cosign
  docker run --rm --network host --platform linux/amd64 \
    -v "${SIGNING_VOLUME}:/keys" "${COSIGN_IMAGE}" signing-config create \
    --no-default-fulcio --no-default-oidc --no-default-rekor --no-default-tsa \
    --out /keys/offline-signing-config.json
  IMAGE_DIGEST="$("${PYTHON_BIN}" -c \
    'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["index_digest"])' \
    "${ATTESTATION_OUTPUT}")"
  DIGEST_REFERENCE="${REGISTRY_AUTHORITY}/${REPOSITORY}@${IMAGE_DIGEST}"
  docker run --rm --network host --platform linux/amd64 \
    -e COSIGN_PASSWORD -v "${SIGNING_VOLUME}:/keys" "${COSIGN_IMAGE}" sign \
    --yes --allow-http-registry --allow-insecure-registry \
    --signing-config /keys/offline-signing-config.json --key /keys/cosign.key \
    -a "release_sha=${RELEASE_SHA}" "${DIGEST_REFERENCE}"
  unset COSIGN_PASSWORD
  docker run --rm --network host --platform linux/amd64 \
    -v "${SIGNING_VOLUME}:/keys:ro" "${COSIGN_IMAGE}" verify \
    --allow-http-registry --allow-insecure-registry --insecure-ignore-tlog \
    --key /keys/cosign.pub -a "release_sha=${RELEASE_SHA}" -o json \
    "${DIGEST_REFERENCE}" > "${SIGNATURE_WORK_DIR}/cosign-verify.json"
  KEY_COPY_CONTAINER="$(docker create --platform linux/amd64 \
    -v "${SIGNING_VOLUME}:/keys:ro" "${COSIGN_IMAGE}" version)"
  docker cp "${KEY_COPY_CONTAINER}:/keys/cosign.pub" \
    "${SIGNATURE_WORK_DIR}/cosign.pub" >/dev/null
  docker rm "${KEY_COPY_CONTAINER}" >/dev/null
  KEY_COPY_CONTAINER=""
  "${PYTHON_BIN}" "${SIGNATURE_VERIFIER}" \
    --verification-json "${SIGNATURE_WORK_DIR}/cosign-verify.json" \
    --public-key "${SIGNATURE_WORK_DIR}/cosign.pub" \
    --image-reference "${DIGEST_REFERENCE}" \
    --expected-release-sha "${RELEASE_SHA}" \
    --cosign-image "${COSIGN_IMAGE}" \
    --cosign-version "${COSIGN_VERSION}" \
    --evidence-scope "${EVIDENCE_SCOPE}" \
    --json-out "${SIGNATURE_OUTPUT}"
fi

echo "image attestation CI rehearsal passed: ${ATTESTATION_OUTPUT}"
if [[ "${SIGN_IMAGE}" == "true" ]]; then
  echo "image signature CI rehearsal passed: ${SIGNATURE_OUTPUT}"
fi
