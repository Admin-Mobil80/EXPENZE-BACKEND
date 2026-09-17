#!/usr/bin/env bash
# Build the Lambda dependency layer.
#
# There is no Docker on this machine and the only local Python is 3.9, so the
# usual `cdk`/`sam` bundling path (which shells out to a Linux container) is
# closed. Instead we ask pip for prebuilt manylinux wheels matching the Lambda
# runtime directly - no compiler, no container, byte-identical to what a
# container build would have produced for these packages.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$ROOT/build/layer/python"

rm -rf "$ROOT/build/layer"
mkdir -p "$TARGET"

# Use the venv's pip, not the system one. macOS ships pip 21.x on Python 3.9,
# which ignores --python-version when checking a package's requires-python and
# then backtracks for minutes through ancient releases hunting for one that
# supports 3.9. A modern pip honours the flag and resolves in seconds.
PIP="$ROOT/.venv/bin/python"
[ -x "$PIP" ] || PIP="$(command -v python3)"

"$PIP" -m pip install \
  --target "$TARGET" \
  --platform manylinux2014_aarch64 \
  --python-version 3.13 \
  --implementation cp \
  --only-binary=:all: \
  --quiet \
  -r "$ROOT/lambda_src/requirements.txt"

# Strip what Lambda never reads, to keep the layer well under the 250MB limit.
# Note: .dist-info is deliberately kept - several of these packages read their
# own version through importlib.metadata at import time and fail without it.
find "$TARGET" -type d -name "__pycache__" -prune -exec rm -rf {} +

echo "layer built: $(du -sh "$ROOT/build/layer" | cut -f1)"
