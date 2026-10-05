# CHANGE 2026-10-05 F03-DEVDB-01: new file (no previous version). Dev self-check, not the QA suite, never touches Supabase.
"""Apply the Plan_Travel migrations to a scratch LOCAL PostgreSQL database and run the QA harnesses.

usage:  python3 scripts/dev/f03_db_check.py "<psql connection args>"
        e.g. python3 scripts/dev/f03_db_check.py "-h /tmp/pg -p 55432 -U postgres"

Steps: drop/create database pt_f03_dev; scripts/dev/supabase_local_shim.sql (roles, auth.users, auth.uid());
0001, 0002, 0003, 0004, 0006, 0007, 0008, 0009 (0005 is the F02 test-trip data migration, not in the repo);
0008 a second time (idempotency); qa.f01_run(), qa.f02_run(), qa.f03_run() — prints failures and totals.
"""
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIG = ROOT / "supabase" / "migrations"
SHIM = ROOT / "scripts" / "dev" / "supabase_local_shim.sql"
DB = "pt_f03_dev"
ORDER = ["0001_core.sql", "0002_qa_f01_harness.sql", "0003_qa_f01_fix_case12.sql", "0004_qa_search_path.sql",
         "0006_f02.sql", "0007_qa_f02_harness.sql", "0008_f03.sql", "0009_qa_f03_harness.sql", "0008_f03.sql"]


def psql(conn, db, *args, input_sql=None):
    cmd = ["psql"] + conn + ["-d", db, "-v", "ON_ERROR_STOP=1", "-X", "-q"] + list(args)
    return subprocess.run(cmd, input=input_sql, capture_output=True, text=True)


def main():
    conn = shlex.split(sys.argv[1]) if len(sys.argv) > 1 else []
    r = psql(conn, "postgres", "-c", "drop database if exists %s" % DB)
    r = psql(conn, "postgres", "-c", "create database %s" % DB)
    if r.returncode:
        print(r.stderr)
        return 2
    ok = True
    for f in [SHIM] + [MIG / n for n in ORDER]:
        r = psql(conn, DB, "-1", "-f", str(f))
        print("apply %-28s %s" % (f.name, "OK" if r.returncode == 0 else "FAIL\n" + r.stderr[-1500:]))
        ok &= r.returncode == 0
    if not ok:
        return 1
    for run in ("f01", "f02", "f03"):
        r = psql(conn, DB, "-A", "-t", "-F", "\t", "-c", (("select id, pass, scenario, 'expected: ' || expected || ' | actual: ' || actual from qa.%s_run() order by id") if run == "f01"
                    else "select id, pass, name, detail from qa.%s_run() order by id") % run)
        if r.returncode:
            print(run, "ERROR", r.stderr[-800:])
            ok = False
            continue
        rows = [ln.split("\t") for ln in r.stdout.strip().splitlines() if ln.strip()]
        fails = [x for x in rows if len(x) > 1 and x[1] != "t"]
        print("qa.%s_run(): %d/%d pass" % (run, len(rows) - len(fails), len(rows)))
        for x in fails:
            print("   FAIL #%s %s :: %s" % (x[0], x[2] if len(x) > 2 else "", x[3] if len(x) > 3 else ""))
        ok &= not fails
    print("ALL OK" if ok else "PROBLEMS")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
