#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
PYTHON_BIN=${PYTHON_BIN:-python3}
"$PYTHON_BIN" -m compileall -q app.py scripts tests
node --check file-tiles.js
node --test tests/file-tiles.test.cjs
"$PYTHON_BIN" -m unittest discover -s tests -p 'test_*.py' -v
for script in scripts/*.sh; do sh -n "$script"; done
"$PYTHON_BIN" scripts/check-public-files.py
