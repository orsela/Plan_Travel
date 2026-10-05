#!/usr/bin/env bash
# Plan_Travel F02 QA runner · version 1.0.0
# CHANGE 2026-10-04 F02-QA: first version (no previous version).
# usage: ./run.sh [extra pytest args]     env: APP_DIR (default /home/claude/plan_travel/app), BASELINE_DIR, SCRIPTS_DIR, QA_PSQL
set -u
cd "$(dirname "$0")"
PY=${PYTHON:-python3}

if ! $PY -c "import playwright" 2>/dev/null; then
  $PY -m pip install playwright --break-system-packages -q || true
fi
if ! $PY -c "import pytest" 2>/dev/null; then
  $PY -m pip install pytest --break-system-packages -q 2>/dev/null || true
fi
if ! $PY -c "import pytest" 2>/dev/null; then
  # sandbox fallback: no PyPI access, but a uv-installed pytest may exist
  for d in /root/.local/share/uv/tools/pytest/lib/python3*/site-packages; do
    [ -d "$d" ] && export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$d"
  done
fi
$PY -c "import pytest, playwright" || { echo "pytest/playwright not available"; exit 2; }
$PY - <<'EOF' 2>/dev/null || $PY -m playwright install chromium
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    p.chromium.launch().close()
EOF

export APP_DIR=${APP_DIR:-/home/claude/plan_travel/app}
echo "F02 QA · APP_DIR=$APP_DIR"
$PY -m pytest tests/test_f02.py -q -rA --tb=line -p no:cacheprovider "$@" 2>&1 | tee .last_run.txt | \
  grep -E "^(PASSED|FAILED|ERROR|SKIPPED|XFAIL)|passed|failed|error" | sed -E 's/ - AssertionError: / — /'
exit "${PIPESTATUS[0]}"
