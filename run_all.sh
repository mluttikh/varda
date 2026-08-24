#!/usr/bin/env bash
# The gate. Every step must pass; there is no "warnings are fine" mode.
set -euo pipefail
cd "$(dirname "$0")"

PY=${PY:-.venv/bin}
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "ruff check"
"$PY/ruff" check .

step "ruff format --check"
"$PY/ruff" format --check .

step "actionlint"
"$PY/actionlint" .github/workflows/*.yml
echo "workflows ok"

step "mypy --strict"
"$PY/mypy"

step "pytest"
"$PY/python" -m pytest -q

step "generate the docs reference pages"
"$PY/python" scripts/gen_reference.py

step "the docs agree with the tool"
"$PY/python" scripts/check_docs.py

step "varda check the examples --strict"
for model in examples/*.yaml; do
  "$PY/varda" check "$model" --strict
done

step "varda generate (into a temp tree, then discarded)"
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
"$PY/varda" generate examples/retail.yaml --out "$OUT"

step "generation is deterministic"
OUT2=$(mktemp -d)
trap 'rm -rf "$OUT" "$OUT2"' EXIT
"$PY/varda" generate examples/retail.yaml --out "$OUT2" >/dev/null
diff -r "$OUT" "$OUT2"
echo "same bytes on a second run"

printf '\n\033[1;32mPASS\033[0m\n'
