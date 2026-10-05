#!/bin/bash
# pwa-test.sh — cek sintaks file JS PWA Agentarium (opsional, nilai plus).
# Cara pakai: bash backend/static/pwa-test.sh
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAIL=0

check_js() {
  local f="$1"
  if [ ! -f "$f" ]; then echo "SKIP: $f tidak ada"; return 0; fi
  if ! command -v node >/dev/null 2>&1; then
    echo "SKIP: node tidak tersedia, lewati cek sintaks $f"
    return 0
  fi
  if node --check "$f" 2>&1; then
    echo "OK: sintaks $f valid"
  else
    echo "FAIL: sintaks $f bermasalah"
    FAIL=1
  fi
}

check_js "$HERE/sw.js"

# Validasi manifest JSON bila python3 ada
if [ -f "$HERE/manifest.webmanifest" ]; then
  if command -v python3 >/dev/null 2>&1; then
    if python3 -m json.tool "$HERE/manifest.webmanifest" >/dev/null 2>&1; then
      echo "OK: manifest.webmanifest JSON valid"
    else
      echo "FAIL: manifest.webmanifest JSON tidak valid"
      FAIL=1
    fi
  else
    echo "SKIP: python3 tidak tersedia, lewati validasi manifest"
  fi
fi

# Pastikan tidak ada pemakaian innerHTML (sebagai properti DOM) di file PWA baru
if grep -rE '\.innerHTML|innerHTML\s*=' "$HERE/sw.js" "$HERE/pwa-snippet.html" "$HERE/manifest.webmanifest" 2>/dev/null; then
  echo "FAIL: ditemukan pemakaian innerHTML di file PWA baru"
  FAIL=1
else
  echo "OK: tidak ada pemakaian innerHTML di file PWA baru"
fi

exit "$FAIL"
