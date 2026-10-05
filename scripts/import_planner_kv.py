#!/usr/bin/env python3
# Plan_Travel scripts/import_planner_kv.py · version 3.0.0-alpha.2 · F02
# CHANGE 2026-10-04 F02-IMPORT-01: new tool (no previous version). Converts an export of the old app's key-value
# table (columns key,value[,updated_at]; CSV or JSON, as produced by Or from the Supabase dashboard) into
# `INSERT ... ON CONFLICT DO UPDATE` SQL for public.trip_kv of one trip. Spec: docs/F02_spec.md §6.
#
# Rules:
# - `planner:` is stripped from the start of each key (the app maps app key planner:X <-> db key X); other keys
#   are kept as-is.
# - `value` is preserved byte-for-byte (dollar-quoted with a tag that does not occur in the value; NULL stays NULL).
# - Nothing is skipped silently: every row is emitted, or the tool stops with an error (duplicate key after
#   stripping, empty key, key longer than 200 chars, value containing a NUL byte that Postgres text can't hold).
# - updated_at / updated_by are not written: the database sets them (updated_by = the user who runs the SQL is
#   NULL when run as postgres in the SQL editor; that is expected for an import).
# - Budget totals (expenses item count + sum of amount, budget target) are printed too (criterion 4).
# - A per-family count is printed to stderr (trip, expenses, reservations, journal:*, checkins, timeline, budget,
#   other); --verify-sql prints a query returning the same counts from trip_kv for the trip.
#
# Usage:
#   import_planner_kv.py export.csv --trip <uuid> [-o out.sql]
#   import_planner_kv.py export.json --trip <uuid> --verify-sql
#   import_planner_kv.py --self-test
import argparse
import csv
import io
import json
import os
import re
import sys
import uuid

FAMILIES = ["trip", "expenses", "reservations", "journal:*", "checkins", "timeline", "budget", "other"]
PREFIX = "planner:"


def strip_key(k):
    return k[len(PREFIX):] if k.startswith(PREFIX) else k


def family(db_key):
    """Family of a db key (after stripping planner:). Exact names for the single-blob families; journal:* is
    every key starting 'journal:' (per-day blobs and per-entry keys)."""
    if db_key.startswith("journal:"):
        return "journal:*"
    if db_key in ("trip", "expenses", "reservations", "checkins", "timeline", "budget"):
        return db_key
    return "other"


def family_sql_case(col="key"):
    return ("case when {c} like 'journal:%' then 'journal:*' "
            "when {c} in ('trip','expenses','reservations','checkins','timeline','budget') then {c} "
            "else 'other' end").format(c=col)


def read_rows(path, data=None):
    """Returns a list of (key, value) with value str or None. Accepts CSV (header with key,value[,updated_at...])
    or JSON (a list of objects, or {"rows": [...]}, or a dashboard export object with a single list member)."""
    if data is None:
        with open(path, "rb") as f:
            data = f.read()
    text = data.decode("utf-8-sig")
    stripped = text.lstrip()
    is_json = path.lower().endswith(".json") or stripped[:1] in ("[", "{")
    rows = []
    if is_json:
        obj = json.loads(text)
        if isinstance(obj, dict):
            lists = [v for v in obj.values() if isinstance(v, list)]
            if "rows" in obj and isinstance(obj["rows"], list):
                obj = obj["rows"]
            elif len(lists) == 1:
                obj = lists[0]
            else:
                raise SystemExit("JSON: expected a list of rows (or an object with one list member)")
        if not isinstance(obj, list):
            raise SystemExit("JSON: expected a list of rows")
        for i, r in enumerate(obj):
            if not isinstance(r, dict) or "key" not in r:
                raise SystemExit("JSON row %d has no 'key'" % i)
            v = r.get("value")
            if v is not None and not isinstance(v, str):
                # planner_kv.value is text; a JSON export of a text column is a string. If a tool exported it as
                # parsed JSON, re-serialize compactly — flagged loudly because byte-identity can't be guaranteed.
                sys.stderr.write("WARNING: row %d (%s) value is not a string; re-serialized with json.dumps\n" % (i, r["key"]))
                v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            rows.append((str(r["key"]), v))
    else:
        # newline='' semantics: keep embedded \r\n inside quoted fields byte-for-byte
        rdr = csv.DictReader(io.StringIO(text, newline=""))
        if not rdr.fieldnames or "key" not in rdr.fieldnames or "value" not in rdr.fieldnames:
            raise SystemExit("CSV: header must contain key and value (got %r)" % (rdr.fieldnames,))
        for r in rdr:
            v = r["value"]
            # Supabase dashboard CSV writes SQL NULL as an empty field; trip_kv.value is nullable text.
            # An empty string and NULL are indistinguishable in CSV; both become NULL only if --csv-empty-null.
            rows.append((r["key"], v))
    return rows


def dollar_quote(v):
    tag = "$pt$"
    n = 0
    while tag in v:
        n += 1
        tag = "$pt%d$" % n
    return tag + v + tag


def build(rows, trip, csv_empty_null=False, is_csv=False):
    seen = {}
    counts = {f: 0 for f in FAMILIES}
    out = []
    for i, (k, v) in enumerate(rows):
        if k is None or k == "":
            raise SystemExit("row %d: empty key" % i)
        dk = strip_key(k)
        if dk == "":
            raise SystemExit("row %d: key %r is empty after stripping planner:" % (i, k))
        if len(dk) > 200:
            raise SystemExit("row %d: key %r longer than 200 chars (trip_kv_key_len)" % (i, dk))
        if dk in seen:
            raise SystemExit("row %d: key %r collides with row %d (%r) after stripping planner:" % (i, k, seen[dk][0], seen[dk][1]))
        seen[dk] = (i, k)
        if v is not None and "\x00" in v:
            raise SystemExit("row %d (%s): value contains a NUL byte, which Postgres text cannot store" % (i, k))
        if is_csv and csv_empty_null and v == "":
            v = None
        counts[family(dk)] += 1
        out.append((dk, v))
    return out, counts


def emit_sql(pairs, trip, src_name):
    buf = io.StringIO()
    w = buf.write
    w("-- Plan_Travel trip_kv import (scripts/import_planner_kv.py, F02)\n")
    w("-- source: %s · trip: %s · rows: %d\n" % (os.path.basename(src_name), trip, len(pairs)))
    w("-- Values are dollar-quoted and byte-identical to the export. updated_at/updated_by are set by the database.\n")
    w("begin;\n")
    w("do $chk$ begin\n  if not exists (select 1 from public.trips where id = '%s') then\n"
      "    raise exception 'trip %s does not exist';\n  end if;\nend $chk$;\n" % (trip, trip))
    for dk, v in pairs:
        val = "null" if v is None else dollar_quote(v)
        w("insert into public.trip_kv (trip_id, key, value) values ('%s', %s, %s)\n"
          "  on conflict (trip_id, key) do update set value = excluded.value;\n" % (trip, dollar_quote(dk), val))
    w("commit;\n")
    return buf.getvalue()


def verify_sql(trip):
    """Per-family counts straight from trip_kv (count(*) ... where trip_id = <uuid> group by family). Families with no
    rows are still listed with 0, in the same order as the tool's own output, so the two tables compare line by line."""
    fam_order = ", ".join("'%s'" % f for f in FAMILIES)
    return ("-- Verification: per-family counts in trip_kv for trip %s (compare with the import tool's output)\n"
            "with c as (\n"
            "  select %s as family, count(*) as rows\n"
            "  from public.trip_kv\n"
            "  where trip_id = '%s'\n"
            "  group by 1\n"
            ")\n"
            "select f.family, coalesce(c.rows, 0) as rows\n"
            "from unnest(array[%s]) with ordinality as f(family, ord)\n"
            "left join c on c.family = f.family\n"
            "order by f.ord;\n"
            % (trip, family_sql_case("key"), trip, fam_order))


def budget_totals(pairs):
    """Budget figures for acceptance criterion 4: expenses count + sum(amount), and the budget target."""
    d = dict(pairs)
    n, total = 0, 0.0
    try:
        ex = json.loads(d.get("expenses") or "[]")
        if isinstance(ex, list):
            n = len(ex)
            for e in ex:
                try:
                    total += float((e or {}).get("amount") or 0)
                except (TypeError, ValueError, AttributeError):
                    pass
    except ValueError:
        n = -1
    return {"expenses_items": n, "expenses_amount_sum": round(total, 2), "budget": d.get("budget")}


def verify_totals_sql(trip):
    return ("-- Budget totals in trip_kv for trip %s\n"
            "select\n"
            "  (select jsonb_array_length(value::jsonb) from public.trip_kv where trip_id = '%s' and key = 'expenses') as expenses_items,\n"
            "  (select round(coalesce(sum(coalesce(nullif(e->>'amount',''),'0')::numeric),0),2)\n"
            "     from public.trip_kv k, jsonb_array_elements(k.value::jsonb) e\n"
            "    where k.trip_id = '%s' and k.key = 'expenses') as expenses_amount_sum,\n"
            "  (select value from public.trip_kv where trip_id = '%s' and key = 'budget') as budget;\n" % (trip, trip, trip, trip))


def print_counts(counts, total, stream):
    stream.write("family          rows\n")
    for f in FAMILIES:
        stream.write("%-15s %5d\n" % (f, counts[f]))
    stream.write("%-15s %5d\n" % ("TOTAL", total))


def self_test():
    trip = "11111111-2222-3333-4444-555555555555"
    tricky = 'he "שלום"\r\nline2,\t$pt$ inside; \' quote \\ backslash'
    src = [
        {"key": "planner:trip", "value": json.dumps([{"day": 1, "title": "האנוי"}], ensure_ascii=False), "updated_at": "2026-09-01T00:00:00Z"},
        {"key": "planner:expenses", "value": "[]", "updated_at": "x"},
        {"key": "planner:reservations", "value": "[]"},
        {"key": "planner:journal:1", "value": tricky},
        {"key": "planner:journal:1:entry:abc", "value": "{}"},
        {"key": "planner:checkins", "value": "[]"},
        {"key": "planner:timeline", "value": None},
        {"key": "planner:budget", "value": "45000"},
        {"key": "planner:catbudgets", "value": "{\"lodging\":1}"},
        {"key": "planner:photo:1:x", "value": "data:image/jpeg;base64,AAAA"},
        {"key": "no_prefix_key", "value": "v"},
    ]
    expect = {"trip": 1, "expenses": 1, "reservations": 1, "journal:*": 2, "checkins": 1, "timeline": 1, "budget": 1, "other": 3}
    # JSON
    pairs, counts = build(read_rows("x.json", json.dumps(src, ensure_ascii=False).encode()), trip)
    assert counts == expect, counts
    assert dict(pairs)["journal:1"] == tricky and dict(pairs)["timeline"] is None and "no_prefix_key" in dict(pairs)
    sql = emit_sql(pairs, trip, "x.json")
    assert "$pt1$" + tricky + "$pt1$" in sql, "value must be dollar-quoted verbatim with a non-colliding tag"
    assert "planner:" not in sql.split("begin;", 1)[1].replace("$pt$planner:", ""), "keys must be stripped"
    # CSV (with embedded CRLF, comma, quotes)
    b = io.StringIO(newline="")
    cw = csv.writer(b)
    cw.writerow(["key", "value", "updated_at"])
    for r in src:
        cw.writerow([r["key"], "" if r["value"] is None else r["value"], r.get("updated_at", "")])
    pairs2, counts2 = build(read_rows("x.csv", b.getvalue().encode("utf-8")), trip, csv_empty_null=True, is_csv=True)
    assert counts2 == expect, counts2
    assert dict(pairs2)["journal:1"] == tricky, repr(dict(pairs2)["journal:1"])
    assert dict(pairs2)["timeline"] is None
    # collisions are refused, never silently dropped
    try:
        build([("planner:a", "1"), ("a", "2")], trip)
        raise AssertionError("collision not detected")
    except SystemExit:
        pass
    bt = budget_totals([("expenses", '[{"amount":100},{"amount":"50.5"},{"amount":null}]'), ("budget", "45000")])
    assert bt == {"expenses_items": 3, "expenses_amount_sum": 150.5, "budget": "45000"}, bt
    vs = verify_sql(trip)
    assert "journal:%" in vs and trip in vs and "count(*)" in vs and "where trip_id = '%s'" % trip in vs
    # optional: round-trip through a real Postgres if psql + PT_SELFTEST_DSN are available
    dsn = os.environ.get("PT_SELFTEST_DSN")
    if dsn:
        # optional: round-trip through a real Postgres (temp tables) and compare every value byte-for-byte
        import subprocess
        setup = ("create temp table trips(id uuid primary key); insert into pg_temp.trips values ('%s');"
                 "create temp table trip_kv(trip_id uuid, key text, value text, primary key(trip_id, key));\n" % trip)
        script = setup + emit_sql(pairs, trip, "x.json").replace("public.", "pg_temp.")
        script += "\\copy (select key, encode(convert_to(coalesce(value, '<NULL>'), 'UTF8'), 'hex') from pg_temp.trip_kv order by key) to stdout\n"
        r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-At"], input=script, text=True, capture_output=True)
        assert r.returncode == 0, r.stderr
        got = dict(line.split("\t") for line in r.stdout.strip().splitlines())
        want = {k: ("<NULL>" if v is None else v).encode("utf-8").hex() for k, v in pairs}
        assert got == want, "DB round-trip mismatch"
        r2 = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-At", "-F", "\t"],
                            input=setup + emit_sql(pairs, trip, "x.json").replace("public.", "pg_temp.") + verify_sql(trip).replace("public.", "pg_temp."),
                            text=True, capture_output=True)
        assert r2.returncode == 0, r2.stderr
        dbc = {f: int(n) for f, n in (l.split("\t") for l in r2.stdout.strip().splitlines())}
        assert dbc == expect, ("verification query counts differ", dbc)
        print("self-test DB round-trip OK (%d rows byte-identical; verification query counts match)" % len(want))
    print("self-test OK: JSON + CSV parse, family counts %s, byte-identical tricky value, collision refused, budget totals" % expect)


def main():
    ap = argparse.ArgumentParser(description="Convert a planner_kv export (CSV/JSON) to trip_kv INSERT SQL.")
    ap.add_argument("export", nargs="?", help="export file (.csv or .json)")
    ap.add_argument("--trip", help="target trip uuid")
    ap.add_argument("-o", "--output", help="write SQL here instead of stdout")
    ap.add_argument("--verify-sql", action="store_true", help="print the per-family count query for trip_kv and exit")
    ap.add_argument("--csv-empty-null", action="store_true",
                    help="CSV only: treat an empty value field as NULL (the dashboard writes NULL as empty). "
                         "Default: keep as empty string. Counts are unaffected either way.")
    ap.add_argument("--self-test", action="store_true", help="run the built-in test on a synthetic export")
    a = ap.parse_args()
    if a.self_test:
        self_test()
        return
    if not a.trip:
        ap.error("--trip is required")
    try:
        trip = str(uuid.UUID(a.trip))
    except ValueError:
        ap.error("--trip must be a uuid")
    if a.verify_sql:
        sys.stdout.write(verify_sql(trip))
        sys.stdout.write(verify_totals_sql(trip))
        return
    if not a.export:
        ap.error("export file is required")
    is_csv = not (a.export.lower().endswith(".json"))
    rows = read_rows(a.export)
    if is_csv:
        # read_rows sniffs JSON content too; re-derive for the NULL rule
        with open(a.export, "rb") as f:
            is_csv = f.read(64).decode("utf-8-sig", "ignore").lstrip()[:1] not in ("[", "{")
    pairs, counts = build(rows, trip, csv_empty_null=a.csv_empty_null, is_csv=is_csv)
    # CHANGE 2026-10-04 F02-IMPORT-02: a normal run now always ends with the verification queries (spec §6): they are
    # appended after `commit;` in the emitted SQL (so running the file prints the trip_kv counts right after the
    # import), and, when -o is used, also printed to stdout. --verify-sql still prints them alone.
    verify = verify_sql(trip) + verify_totals_sql(trip)
    sql = emit_sql(pairs, trip, a.export) + "\n" + verify
    if a.output:
        with open(a.output, "w", encoding="utf-8", newline="") as f:
            f.write(sql)
        sys.stderr.write("wrote %s\n" % a.output)
    else:
        sys.stdout.write(sql)
    print_counts(counts, len(pairs), sys.stderr)
    t = budget_totals(pairs)
    sys.stderr.write("budget: expenses_items=%s expenses_amount_sum=%s budget=%s\n" % (t["expenses_items"], t["expenses_amount_sum"], t["budget"]))
    if a.output:
        sys.stdout.write(verify)


if __name__ == "__main__":
    main()
