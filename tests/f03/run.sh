#!/usr/bin/env bash
# Plan_Travel F03 QA runner · version 1.0.0
# CHANGE 2026-10-05 F03-QA: first version (no previous version).
#
# Runs everything available for F03 and prints a pass/fail summary:
#   1. F03 browser suite (tests/f03/test_f03.py) against APP_DIR (ADMIN_FEATURE='on' + the 'off' cases inside it)
#   2. F02 regression suite (tests/f02) against the NEW app, flag 'on'  (C16)
#   3. F02 regression suite against the NEW app with ADMIN_FEATURE flipped to 'off' (copy of APP_DIR)  (C16)
#   4. Function tests (tests/f03/function_test.ts) with Deno, if deno is installed and the function exists
#   5. DB harness: prints how to run qa.f03_run() (needs the real project; run by the architect via the Connector)
#
# usage: ./run.sh [--only f03|f02on|f02off|fn] [extra pytest args]
# env:   APP_DIR (default <repo>/app), BASELINE_DIR (v2.15.2 checkout for the F02 suite; cloned to a cache dir
#        if missing and the network allows), ALPHA2_REF (default 9b8151b), PYTHON, DENO
#
# F02 cases deselected when run against alpha.3 (they assert the alpha.2 version string / F02 CHANGE tag, which F03
# legitimately bumps — spec §4 "Version APP_VERSION='3.0.0-alpha.3'"): test_ac15_static[app_version],
# test_ac15_static[title_version]. Everything else in F02 must pass unchanged in both flag states.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PY=${PYTHON:-python3}
export APP_DIR=${APP_DIR:-$REPO/app}
export REPO_DIR=$REPO
ONLY=""
if [ "${1:-}" = "--only" ]; then ONLY=$2; shift 2; fi
WORK=$(mktemp -d "${TMPDIR:-/tmp}/f03qa.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

# ---- python deps (same fallbacks as tests/f02/run.sh) ----
$PY -c "import playwright" 2>/dev/null || $PY -m pip install playwright --break-system-packages -q || true
$PY -c "import pytest" 2>/dev/null || $PY -m pip install pytest --break-system-packages -q 2>/dev/null || true
if ! $PY -c "import pytest" 2>/dev/null; then
  for d in /root/.local/share/uv/tools/pytest/lib/python3*/site-packages; do
    [ -d "$d" ] && export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$d"
  done
fi
$PY -c "import pytest, playwright" || { echo "pytest/playwright not available"; exit 2; }

# ---- v2.15.2 baseline for the F02 suite ----
if [ -z "${BASELINE_DIR:-}" ]; then
  for c in /home/claude/orsela/vietnam_travel_planner "$HOME/.cache/plan_travel_qa/vtp_baseline"; do
    [ -f "$c/index.html" ] && export BASELINE_DIR=$c && break
  done
fi
if [ -z "${BASELINE_DIR:-}" ]; then
  mkdir -p "$HOME/.cache/plan_travel_qa"
  timeout 90 git clone -q --depth 1 https://github.com/orsela/Vietnam_Travel_Planner "$HOME/.cache/plan_travel_qa/vtp_baseline" 2>/dev/null \
    && export BASELINE_DIR="$HOME/.cache/plan_travel_qa/vtp_baseline"
fi
if ! grep -q "const APP_VERSION='2.15.2'" "${BASELINE_DIR:-/nonexistent}/index.html" 2>/dev/null; then
  echo "WARN: BASELINE_DIR is not a v2.15.2 checkout (${BASELINE_DIR:-unset}); F02 baseline-dependent cases will fail"
fi

declare -A RES
summ() { # name logfile rc
  local p f s e
  p=$(grep -Eo "[0-9]+ passed" "$2" | tail -1 | grep -Eo "[0-9]+"); f=$(grep -Eo "[0-9]+ failed" "$2" | tail -1 | grep -Eo "[0-9]+")
  s=$(grep -Eo "[0-9]+ skipped" "$2" | tail -1 | grep -Eo "[0-9]+"); e=$(grep -Eo "[0-9]+ errors?" "$2" | tail -1 | grep -Eo "[0-9]+")
  RES[$1]="passed=${p:-0} failed=${f:-0} skipped=${s:-0} errors=${e:-0} (rc=$3)"
}
pyt() { # name dir testfile logfile [pytest args]
  local name=$1 dir=$2 file=$3 log=$4; shift 4
  echo; echo "=== $name · APP_DIR=$APP_DIR"
  (cd "$dir" && $PY -m pytest "$file" -q -rA --tb=line -p no:cacheprovider "$@") >"$log" 2>&1
  local rc=$?
  grep -E "^(PASSED|FAILED|ERROR|SKIPPED|XFAIL)|[0-9]+ (passed|failed)" "$log" | sed -E 's/ - AssertionError: / — /'
  summ "$name" "$log" $rc
  return $rc
}

RC=0
if [ -z "$ONLY" ] || [ "$ONLY" = f03 ]; then
  pyt "F03 browser suite" "$HERE" test_f03.py "$HERE/.last_run_f03.txt" "$@" || RC=1
fi
F02_DESEL='not (test_ac15_static and (app_version or title_version))'
if [ -z "$ONLY" ] || [ "$ONLY" = f02on ]; then
  pyt "F02 regression · ADMIN_FEATURE=on" "$REPO/tests/f02" tests/test_f02.py "$HERE/.last_run_f02_on.txt" -k "$F02_DESEL" "$@" || RC=1
fi
if [ -z "$ONLY" ] || [ "$ONLY" = f02off ]; then
  OFF="$WORK/app_off"; cp -r "$APP_DIR" "$OFF"
  if grep -Eq "const[[:space:]]+ADMIN_FEATURE[[:space:]]*=[[:space:]]*['\"]on['\"]" "$OFF/index.html"; then
    sed -i -E "0,/const[[:space:]]+ADMIN_FEATURE[[:space:]]*=[[:space:]]*['\"]on['\"]/s//const ADMIN_FEATURE='off'/" "$OFF/index.html"
    echo; echo "(ADMIN_FEATURE flipped to 'off' in $OFF/index.html)"
  else
    echo; echo "WARN: const ADMIN_FEATURE='on' not found in $APP_DIR/index.html — 'off' run uses the app as-is"
  fi
  APP_DIR="$OFF" pyt "F02 regression · ADMIN_FEATURE=off" "$REPO/tests/f02" tests/test_f02.py "$HERE/.last_run_f02_off.txt" -k "$F02_DESEL" "$@" || RC=1
fi
if [ -z "$ONLY" ] || [ "$ONLY" = fn ]; then
  echo; echo "=== invite-manager function tests (Deno)"
  DENO=${DENO:-$(command -v deno || true)}
  FN="$REPO/supabase/functions/invite-manager/index.ts"
  if [ -z "$DENO" ]; then
    RES["Function tests (Deno)"]="NOT RUN: deno not installed"; echo "deno not installed — skipped"
  elif [ ! -f "$FN" ]; then
    RES["Function tests (Deno)"]="NOT RUN: $FN missing"; echo "$FN missing — skipped"
  else
    (cd "$HERE" && "$DENO" test --allow-env --allow-net=127.0.0.1 --allow-read function_test.ts) >"$HERE/.last_run_fn.txt" 2>&1
    rc=$?; tail -30 "$HERE/.last_run_fn.txt"
    RES["Function tests (Deno)"]="$(grep -Eo 'ok \| [0-9]+ passed \| [0-9]+ failed' "$HERE/.last_run_fn.txt" | tail -1) (rc=$rc)"
    [ $rc -eq 0 ] || RC=1
  fi
  if command -v npx >/dev/null 2>&1 || [ -x /opt/node22/bin/node ]; then
    TSC=""
    for c in "$(npm root -g 2>/dev/null)/typescript/bin/tsc" /opt/node22/lib/node_modules/typescript/bin/tsc /usr/lib/node_modules/typescript/bin/tsc; do
      [ -f "$c" ] && TSC=$c && break
    done
    if [ -n "$TSC" ]; then
      node "$TSC" --noEmit --allowImportingTsExtensions --target es2022 --module esnext --moduleResolution bundler \
        --skipLibCheck --noResolve --types "" --lib es2022,dom "$HERE/function_test.ts" >"$HERE/.last_tsc.txt" 2>&1
      n=$(grep -c "error TS" "$HERE/.last_tsc.txt"); syn=$(grep -E "error TS1[0-9]{3}" "$HERE/.last_tsc.txt" | wc -l)
      RES["function_test.ts syntax (tsc)"]="syntax errors=$syn (type errors from unresolved Deno imports ignored: $n total)"
    fi
  fi
fi

echo; echo "=== DB harness"
echo "qa.f03_run() needs the real database. Compare the dev harness 0009_qa_f03_harness.sql against"
echo "tests/f03/sql/qa_f03_cases.md, then (architect, via the Connector): select * from qa.f03_run() order by id;"

echo; echo "================ F03 QA summary ================"
for k in "${!RES[@]}"; do printf '%-42s %s\n' "$k" "${RES[$k]}"; done | sort
echo "overall rc=$RC"
exit $RC
