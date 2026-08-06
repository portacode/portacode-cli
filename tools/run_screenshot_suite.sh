#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$ROOT_DIR/test_results/screenshot_runs"
STATIC_DIR="$ROOT_DIR/server/portacode_django/static/images/marketing"
mkdir -p "$ROOT_DIR/test_results"
STAGING_DIR="$(mktemp -d "$ROOT_DIR/test_results/marketing-stage.XXXXXX")"

cleanup() {
    rm -rf "$STAGING_DIR"
}
trap cleanup EXIT

ENV_FILE="$ROOT_DIR/.env.play_store"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
else
    echo "Missing $ENV_FILE with SCREENSHOT_USERNAME/SCREENSHOT_PASSWORD"
    exit 1
fi

: "${SCREENSHOT_USERNAME:?SCREENSHOT_USERNAME missing in $ENV_FILE}"
: "${SCREENSHOT_PASSWORD:?SCREENSHOT_PASSWORD missing in $ENV_FILE}"
: "${TEST_BASE_URL:=http://localhost:8001/}"
: "${PLAY_STORE_DEVICE_LABEL:=Atlas Development}"
export TEST_BASE_URL PLAY_STORE_DEVICE_LABEL

mkdir -p "$OUT_DIR"
echo "🧪 Generating screenshots in staging: $STAGING_DIR"

run_profile() {
    local profile_name=$1
    local test_name=$2
    shift
    shift
    echo ""
    echo "=== Running screenshot profile: ${profile_name} ==="

    env \
        TEST_USERNAME="$SCREENSHOT_USERNAME" \
        TEST_PASSWORD="$SCREENSHOT_PASSWORD" \
        SCREENSHOT_DEVICE_NAME="$profile_name" \
        ALLOW_EMPTY_SESSIONS=true \
        "$@" \
        python -m testing_framework.cli --use-existing-portacode run-tests "$test_name"

    local latest_run
    latest_run=$(ls -dt "$ROOT_DIR"/test_results/run_* | head -n 1)
    python - "$latest_run/summary.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as summary_file:
    summary = json.load(summary_file)
if summary["statistics"]["failed"] or not summary["statistics"]["passed"]:
    raise SystemExit("Screenshot test failed; existing marketing assets were not changed")
PY
    local recording_dir
    recording_dir=$(ls -d "$latest_run"/recordings/shared_session_* | head -n 1)
    local profile_dir="$OUT_DIR/${profile_name}"
    rm -rf "$profile_dir"
    mkdir -p "$profile_dir"
    cp "$recording_dir"/screenshots/*.png "$profile_dir"/
    echo "Stored screenshots for ${profile_name} in $profile_dir"

    local staged_target="$STAGING_DIR/${profile_name}"
    mkdir -p "$staged_target"
    cp "$profile_dir"/*.png "$staged_target"/
    echo "Staged ${profile_name} screenshots in $staged_target"
}

GALAXY_UA="Mozilla/5.0 (Linux; Android 13; SAMSUNG SM-S908U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36"

run_profile phone_s22_ultra play_store_phone_screenshot_test \
    TEST_VIEWPORT_WIDTH=384 \
    TEST_VIEWPORT_HEIGHT=844 \
    TEST_DEVICE_SCALE_FACTOR=3.125 \
    TEST_IS_MOBILE=true \
    TEST_HAS_TOUCH=true \
    TEST_USER_AGENT="$GALAXY_UA" \
    TEST_VIDEO_WIDTH=1288 \
    TEST_VIDEO_HEIGHT=2859 \
    SCREENSHOT_ZOOM=0.9

run_profile tablet_7_inch play_store_tablet_screenshot_test \
    TEST_VIEWPORT_WIDTH=960 \
    TEST_VIEWPORT_HEIGHT=600 \
    TEST_DEVICE_SCALE_FACTOR=2.0 \
    TEST_IS_MOBILE=true \
    TEST_HAS_TOUCH=true \
    TEST_VIDEO_WIDTH=1200 \
    TEST_VIDEO_HEIGHT=1920 \
    SCREENSHOT_ZOOM=1.0

run_profile tablet_10_inch play_store_tablet_screenshot_test \
    TEST_VIEWPORT_WIDTH=1280 \
    TEST_VIEWPORT_HEIGHT=800 \
    TEST_DEVICE_SCALE_FACTOR=2.0 \
    TEST_IS_MOBILE=true \
    TEST_HAS_TOUCH=true \
    TEST_VIDEO_WIDTH=1600 \
    TEST_VIDEO_HEIGHT=2560 \
    SCREENSHOT_ZOOM=1.0

[[ $(find "$STAGING_DIR/phone_s22_ultra" -maxdepth 1 -name '*.png' | wc -l) -eq 12 ]]
[[ $(find "$STAGING_DIR/tablet_7_inch" -maxdepth 1 -name '*.png' | wc -l) -eq 10 ]]
[[ $(find "$STAGING_DIR/tablet_10_inch" -maxdepth 1 -name '*.png' | wc -l) -eq 10 ]]

# Linux renameat2(RENAME_EXCHANGE) swaps two populated directories in one
# filesystem operation. If it is unavailable, fail before changing the live
# directory instead of exposing a missing or partial asset tree.
python - "$STATIC_DIR" "$STAGING_DIR" <<'PY'
import ctypes
import os
import sys

live, staged = map(os.fsencode, sys.argv[1:])
libc = ctypes.CDLL(None, use_errno=True)
renameat2 = getattr(libc, "renameat2", None)
if renameat2 is None:
    raise SystemExit("Atomic directory exchange is not supported; live assets were not changed")
AT_FDCWD = -100
RENAME_EXCHANGE = 2
if renameat2(AT_FDCWD, live, AT_FDCWD, staged, RENAME_EXCHANGE) != 0:
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error))
PY

echo "✅ Published all marketing screenshots with one atomic directory exchange"
