#!/bin/sh
# Runs the test suite in a virtualenv cached by the content of the requirements files,
# so repeated runs (CI, maintainer) do not reinstall everything.
set -eu
cd "$(dirname "$0")/.."
HASH=$(cat requirements.txt requirements-dev.txt | sha256sum | cut -c1-12)
VENV="${XDG_CACHE_HOME:-$HOME/.cache}/projektakte-venv-$HASH"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV.tmp"
  "$VENV.tmp/bin/pip" install -q --disable-pip-version-check -r requirements.txt -r requirements-dev.txt
  mv "$VENV.tmp" "$VENV"
fi
exec "$VENV/bin/python" -m pytest -q -p no:cacheprovider "$@"
