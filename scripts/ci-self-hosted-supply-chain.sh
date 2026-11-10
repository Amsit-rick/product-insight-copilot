#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUNNER_PROVIDER="${RUNNER_PROVIDER:-gitee_go}"
RUNNER_SCOPE="${RUNNER_SCOPE:-self_hosted_supply_chain_runner}"
DOCKER_HOST_GROUP_ID="${DOCKER_HOST_GROUP_ID:-}"
RELEASE_SHA="${RELEASE_SHA:-$(git -C "${ROOT}" rev-parse HEAD)}"
WORK_DIR="${WORK_DIR:-${ROOT}/.tmp/self-hosted-supply-chain}"
LOCK_DIR="${RUNNER_LOCK_DIR:-${ROOT}/.tmp/self-hosted-supply-chain.lock}"
RUNNER_TIMEOUT_SECONDS="${RUNNER_TIMEOUT_SECONDS:-1800}"
RUNNER_JSON="${RUNNER_JSON:-${WORK_DIR}/runner-readiness.json}"
ATTESTATION_JSON="${ATTESTATION_JSON:-${WORK_DIR}/image-attestations.json}"
SIGNATURE_JSON="${SIGNATURE_JSON:-${WORK_DIR}/image-signature.json}"
BUNDLE_JSON="${BUNDLE_JSON:-${WORK_DIR}/supply-chain-bundle.json}"
MINIMUM_MEMORY_MB="${MINIMUM_MEMORY_MB:-4096}"
MINIMUM_DISK_MB="${MINIMUM_DISK_MB:-10240}"
LOCK_ACQUIRED=false

cleanup_lock() {
  if [[ "${LOCK_ACQUIRED}" == "true" ]]; then
    rm -f -- "${LOCK_DIR}/owner.json"
    rmdir -- "${LOCK_DIR}" 2>/dev/null || true
  fi
}
trap cleanup_lock EXIT INT TERM

case "${RUNNER_TIMEOUT_SECONDS}" in
  ''|*[!0-9]*) echo "[FAIL] RUNNER_TIMEOUT_SECONDS must be an integer" >&2; exit 2 ;;
esac
if (( RUNNER_TIMEOUT_SECONDS < 300 || RUNNER_TIMEOUT_SECONDS > 7200 )); then
  echo "[FAIL] RUNNER_TIMEOUT_SECONDS must be between 300 and 7200" >&2
  exit 2
fi
if ! command -v timeout >/dev/null 2>&1; then
  echo "[FAIL] timeout command is required by the runner" >&2
  exit 2
fi
if [[ ! "${RELEASE_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[FAIL] RELEASE_SHA must be a full lowercase Git SHA" >&2
  exit 2
fi
LOCK_DIR="$("${PYTHON_BIN}" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "${LOCK_DIR}")"
case "${LOCK_DIR}" in
  "${ROOT}/.tmp/"*.lock) ;;
  *) echo "[FAIL] RUNNER_LOCK_DIR must be a .lock directory under repository .tmp" >&2; exit 2 ;;
esac
mkdir -p "$(dirname "${LOCK_DIR}")"
if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  echo "[FAIL] another supply-chain run owns the runner lock" >&2
  exit 3
fi
LOCK_ACQUIRED=true
umask 077
printf '{"pid":%d,"source_git_sha":"%s","started_at_epoch":%d}\n' \
  "$$" "${RELEASE_SHA}" "$(date +%s)" > "${LOCK_DIR}/owner.json"
RUNNER_DEADLINE_EPOCH=$(( $(date +%s) + RUNNER_TIMEOUT_SECONDS ))

run_with_deadline() {
  local now remaining
  now="$(date +%s)"
  remaining=$(( RUNNER_DEADLINE_EPOCH - now ))
  if (( remaining <= 0 )); then
    echo "[FAIL] self-hosted supply-chain deadline was exhausted" >&2
    return 124
  fi
  timeout -s TERM -k 30 "${remaining}" "$@"
}

case "${RUNNER_SCOPE}" in
  self_hosted_supply_chain_runner) IMAGE_EVIDENCE_SCOPE=remote_ci_local_registry ;;
  local_host_rehearsal) IMAGE_EVIDENCE_SCOPE=local_loopback_registry ;;
  *) echo "[FAIL] unsupported runner scope: ${RUNNER_SCOPE}" >&2; exit 2 ;;
esac

mkdir -p "${WORK_DIR}"

run_with_deadline "${PYTHON_BIN}" "${ROOT}/backend/scripts/verify_self_hosted_runner.py" \
  --provider "${RUNNER_PROVIDER}" \
  --scope "${RUNNER_SCOPE}" \
  --source-sha "${RELEASE_SHA}" \
  --host-group-id "${DOCKER_HOST_GROUP_ID}" \
  --minimum-memory-mb "${MINIMUM_MEMORY_MB}" \
  --minimum-disk-mb "${MINIMUM_DISK_MB}" \
  --json-out "${RUNNER_JSON}"

run_with_deadline env \
  RELEASE_SHA="${RELEASE_SHA}" \
  EVIDENCE_SCOPE="${IMAGE_EVIDENCE_SCOPE}" \
  JSON_OUT="${ATTESTATION_JSON}" \
  SIGN_IMAGE=true \
  SIGNATURE_JSON_OUT="${SIGNATURE_JSON}" \
  bash "${ROOT}/scripts/ci-image-attestations.sh"

run_with_deadline "${PYTHON_BIN}" "${ROOT}/backend/scripts/package_self_hosted_supply_chain_evidence.py" \
  --runner "${RUNNER_JSON}" \
  --attestation "${ATTESTATION_JSON}" \
  --signature "${SIGNATURE_JSON}" \
  --expected-release-sha "${RELEASE_SHA}" \
  --json-out "${BUNDLE_JSON}"

printf 'self-hosted supply-chain evidence ready: %s\n' "${BUNDLE_JSON}"
