#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

API_URL="${API_URL:-http://localhost:8000}"
WEB_URL="${WEB_URL:-http://localhost:3000}"
WAIT_TIMEOUT_SECONDS="${WAIT_TIMEOUT_SECONDS:-120}"
TEARDOWN="${TEARDOWN:-0}"
NO_UP=0

usage() {
  cat <<'EOF'
Usage: scripts/e2e_smoke.sh [OPTIONS]

Run the end-to-end smoke test suite against the CachAI API and Web services.

Options:
  --no-up        Skip starting Docker Compose; test an already running stack.
  --down         Tear down the Docker Compose stack on exit (same as TEARDOWN=1).
  -h, --help     Show this help message and exit.

Environment variables:
  API_URL               Base URL for the API service (default: http://localhost:8000)
  WEB_URL               Base URL for the Web service (default: http://localhost:3000)
  WAIT_TIMEOUT_SECONDS  Max seconds to wait for services to become ready (default: 120)
  TEARDOWN              Set to 1 to tear down the Compose stack on exit (default: 0)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-up)
      NO_UP=1
      shift
      ;;
    --down)
      TEARDOWN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      echo "Run '$0 --help' for usage." >&2
      exit 1
      ;;
  esac
done

API_URL="${API_URL%/}"
WEB_URL="${WEB_URL%/}"

if ! command -v curl >/dev/null 2>&1; then
  echo "ERROR: 'curl' is required but was not found in PATH." >&2
  exit 1
fi

COMPOSE_CMD=()
STACK_STARTED=0

detect_compose() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD=(docker-compose)
  else
    echo "ERROR: Neither 'docker compose' nor 'docker-compose' was found in PATH." >&2
    echo "Install Docker Compose or pass --no-up to test an already running stack." >&2
    exit 1
  fi
}

cleanup() {
  local exit_code=$?
  if [[ "${STACK_STARTED}" -eq 1 && ${#COMPOSE_CMD[@]} -gt 0 ]]; then
    if [[ "${exit_code}" -ne 0 ]]; then
      echo ""
      echo "=== Docker Compose logs (last 50 lines) ===" >&2
      (cd "${REPO_ROOT}" && "${COMPOSE_CMD[@]}" logs --tail=50) >&2 || true
    fi
    if [[ "${TEARDOWN}" == "1" ]]; then
      echo ""
      echo "Tearing down Docker Compose stack..."
      (cd "${REPO_ROOT}" && "${COMPOSE_CMD[@]}" down --remove-orphans) || true
    fi
  fi
  exit "${exit_code}"
}
trap cleanup EXIT

PASS_COUNT=0
FAIL_COUNT=0
RESULTS=()

record_pass() {
  local label="$1"
  PASS_COUNT=$(( PASS_COUNT + 1 ))
  RESULTS+=("PASS  ${label}")
  echo "  [PASS] ${label}"
}

record_fail() {
  local label="$1"
  FAIL_COUNT=$(( FAIL_COUNT + 1 ))
  RESULTS+=("FAIL  ${label}")
  echo "  [FAIL] ${label}" >&2
}

wait_for_url() {
  local name="$1"
  local url="$2"
  local timeout="$3"
  local elapsed=0
  local interval=2
  local status="000"

  echo "Waiting for ${name} (${url}) to return HTTP 200 (timeout: ${timeout}s)..."
  while (( elapsed < timeout )); do
    status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${url}" 2>/dev/null || echo "000")"
    if [[ "${status}" == "200" ]]; then
      echo "  [READY] ${name} returned HTTP 200 after ${elapsed}s"
      return 0
    fi
    sleep "${interval}"
    elapsed=$(( elapsed + interval ))
  done

  echo "  [TIMEOUT] ${name} (${url}) did not return HTTP 200 within ${timeout}s (last status: ${status})" >&2
  return 1
}

check_get_200() {
  local url="$1"
  local status
  status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "${url}" 2>/dev/null || echo "000")"
  if [[ "${status}" == "200" ]]; then
    record_pass "GET ${url} (HTTP 200)"
  else
    record_fail "GET ${url} (expected HTTP 200, got ${status})"
  fi
}

check_rss_feed() {
  local url="${WEB_URL}/rss.xml"
  local body_file
  body_file="$(mktemp)"
  local status
  status="$(curl -sS -o "${body_file}" -w '%{http_code}' --max-time 15 "${url}" 2>/dev/null || echo "000")"
  local body
  body="$(cat "${body_file}")"
  rm -f "${body_file}"

  if [[ "${status}" != "200" ]]; then
    record_fail "GET ${url} (expected HTTP 200, got ${status})"
    return
  fi

  if printf '%s' "${body}" | grep -Fq "<rss"; then
    record_pass "GET ${url} (HTTP 200, contains <rss)"
  else
    record_fail "GET ${url} (HTTP 200, but response body missing <rss)"
  fi
}

check_trigger_demo() {
  local url="${API_URL}/api/trigger-demo"
  local body_file
  body_file="$(mktemp)"
  local status
  status="$(curl -sS -X POST -o "${body_file}" -w '%{http_code}' --max-time 60 "${url}" 2>/dev/null || echo "000")"
  local body
  body="$(cat "${body_file}")"
  rm -f "${body_file}"

  if [[ "${status}" != "200" ]]; then
    record_fail "POST ${url} (expected HTTP 200, got ${status})"
    if [[ -n "${body}" ]]; then
      echo "         Response body: ${body}" >&2
    fi
    return
  fi

  local status_ok=0
  if command -v python3 >/dev/null 2>&1; then
    if printf '%s' "${body}" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(data, dict) and data.get("status") == "completed" else 1)
'; then
      status_ok=1
    fi
  else
    if printf '%s' "${body}" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"completed"'; then
      status_ok=1
    fi
  fi

  if [[ "${status_ok}" -eq 1 ]]; then
    record_pass "POST ${url} (HTTP 200, status=completed)"
  else
    record_fail "POST ${url} (HTTP 200, but JSON missing \"status\":\"completed\")"
    if [[ -n "${body}" ]]; then
      echo "         Response body: ${body}" >&2
    fi
  fi
}

if [[ "${NO_UP}" -eq 0 ]]; then
  detect_compose
  echo "Starting stack with: ${COMPOSE_CMD[*]} up -d --build"
  STACK_STARTED=1
  (cd "${REPO_ROOT}" && "${COMPOSE_CMD[@]}" up -d --build)
else
  echo "Skipping Docker Compose startup (--no-up)."
fi

echo ""
echo "=== Readiness Checks ==="
wait_for_url "API health" "${API_URL}/health" "${WAIT_TIMEOUT_SECONDS}"
wait_for_url "Web root" "${WEB_URL}/" "${WAIT_TIMEOUT_SECONDS}"

echo ""
echo "=== Smoke Checks ==="
check_trigger_demo

check_get_200 "${API_URL}/api/posts"
check_get_200 "${API_URL}/api/models"
check_get_200 "${API_URL}/api/runs"
check_get_200 "${API_URL}/api/budget"

check_get_200 "${WEB_URL}/"
check_rss_feed

echo ""
echo "=== E2E Smoke Summary ==="
for line in "${RESULTS[@]}"; do
  echo "  ${line}"
done
echo "Total: $(( PASS_COUNT + FAIL_COUNT )) | Passed: ${PASS_COUNT} | Failed: ${FAIL_COUNT}"

if [[ "${FAIL_COUNT}" -gt 0 ]]; then
  exit 1
fi
