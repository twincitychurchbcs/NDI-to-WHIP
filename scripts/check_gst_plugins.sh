#!/usr/bin/env bash
set -euo pipefail

# Minimal log helpers (keeps output consistent with install.sh)
ok()  { echo "[ OK ]  $*"; }
die() { echo "[FAIL]  $*" >&2; exit 1; }

check_element() {
  local el="$1"
  if gst-inspect-1.0 "${el}" &>/dev/null; then
    ok "gst element '${el}' is available."
  else
    die "gst element '${el}' NOT found. Build may have failed."
  fi
}

check_element ndisrc
check_element whipclientsink
check_element x264enc
check_element opusenc
check_element rtph264pay
check_element rtpopuspay

exit 0
