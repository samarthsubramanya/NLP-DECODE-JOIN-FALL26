#!/bin/bash
# Build + verify a STRIPPED DuckDB from a documented one.
# Usage: bash run_stripped.sh <documented.duckdb> <stripped.duckdb>
set -e
SRC="$1"; DST="$2"
[ -z "$SRC" ] || [ -z "$DST" ] && { echo "usage: bash run_stripped.sh <documented.duckdb> <stripped.duckdb>"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
python3 "$HERE/make_stripped.py" --src "$SRC" --dst "$DST" --force
python3 "$HERE/verify_stripped.py" --src "$SRC" --dst "$DST"
echo; shasum -a 256 "$DST"; ls -lh "$DST"
