#!/usr/bin/env bash
set -euo pipefail

ok()  { echo "[ OK ]  $*"; }
warn() { echo "[WARN]  $*"; }
die() { echo "[FAIL]  $*" >&2; exit 1; }

# Where target .so files live (relative to builder context)
TARGET_DIR="/tmp/gst-plugins-rs/target/release"
# Staging typelib output
GI_OUT="/staging/usr/local/lib/girepository-1.0"
mkdir -p "${GI_OUT}"

if ! command -v g-ir-scanner >/dev/null 2>&1 || ! command -v g-ir-compiler >/dev/null 2>&1; then
  die "g-ir-scanner / g-ir-compiler not available; install gobject-introspection"
fi

generate_for() {
  local so="$1"; shift
  local guesses=($@)
  local full="${TARGET_DIR}/${so}"
  if [[ ! -f "${full}" ]]; then
    warn "Skipping missing library: ${full}"
    return 0
  fi

  for ns in "${guesses[@]}"; do
    girfile="/tmp/${ns}.gir"
    typelib="${GI_OUT}/${ns}-1.0.typelib"
    # Try to scan; include common include paths used on Debian/Ubuntu
    if g-ir-scanner --quiet --namespace="${ns}" --nsversion=1.0 \
         -I/usr/include/gstreamer-1.0 -I/usr/local/include \
         "${full}" -o "${girfile}" 2>/dev/null; then
      if g-ir-compiler "${girfile}" -o "${typelib}" 2>/dev/null; then
        ok "Generated typelib: ${typelib}"
        return 0
      else
        warn "g-ir-compiler failed for ${girfile}"
      fi
    fi
  done

  warn "Could not generate typelib for ${so}; tried: ${guesses[*]}"
  return 1
}

generate_for libgstrswebrtc.so GstRsWebRTC GstRsWebrtc GstWebRTC GstWebrtc GstRs_webrtc
generate_for libgstndi.so      GstNdi GstNDI GstRsNdi GstRsNdi0 GstNdi-1.0

exit 0
