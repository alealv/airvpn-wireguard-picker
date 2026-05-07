#!/usr/bin/env bash
# Build a self-contained zipapp of airvpn-picker.
#
# Output: ./airvpn-picker-vX.Y.Z.pyz at the repo root.
# Reads the version from src/airvpn_picker/__init__.py so the artifact
# name is always in sync with the source.
#
# Usage:
#   contrib/build-pyz.sh
#
# The zipapp interpreter line is /usr/bin/env python3 so the artifact
# runs on FreeBSD (OPNsense) and Linux without rebuilding.

set -euo pipefail

cd "$(dirname "$0")/.."

VERSION="$(awk -F'"' '/^__version__/ {print $2}' src/airvpn_picker/__init__.py)"
if [ -z "${VERSION}" ]; then
    echo "could not parse version from src/airvpn_picker/__init__.py" >&2
    exit 1
fi

OUT="airvpn-picker-v${VERSION}.pyz"
STAGING="$(mktemp -d)"
trap 'rm -rf "${STAGING}"' EXIT

# zipapp requires __main__.py at the archive root, not inside the package.
cp -r src/airvpn_picker "${STAGING}/airvpn_picker"
find "${STAGING}" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

cat > "${STAGING}/__main__.py" <<'PY'
"""Zipapp entry point."""

from airvpn_picker.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
PY

python3 -m zipapp "${STAGING}" \
    -p '/usr/bin/env python3' \
    -o "${OUT}"

chmod +x "${OUT}"
SHA="$(shasum -a 256 "${OUT}" | awk '{print $1}')"

echo "Built  : ${OUT}"
echo "SHA256 : ${SHA}"
echo "Size   : $(wc -c < "${OUT}") bytes"
