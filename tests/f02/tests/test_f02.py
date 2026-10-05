"""Plan_Travel F02 QA · Playwright + static suite · version 1.0.0
CHANGE 2026-10-04 F02-QA: first version (no previous version).

Black-box acceptance tests for docs/F02_spec.md §8, written from the spec only (the implementation under app/,
scripts/, F02_dev_notes.md and migration 0005 were NOT read). Reference for "same as v2.15.2": the baseline at
BASELINE_DIR.

Layout
- A local static server serves APP_DIR under /Plan_Travel/ and the v2.15.2 baseline under /Vietnam_Travel_Planner/
  on the SAME origin (http://127.0.0.1:<port>), like orsela.github.io in production.
- The supabase-js CDN request is fulfilled with tests/fake_supabase.js for pages under /Plan_Travel/; for pages
  under /Vietnam_Travel_Planner/ it is aborted (baseline runs local-only), except in the concurrency test where the
  baseline gets the fake too (legacy planner_kv emulation) so both apps can be compared on SYNC-MERGE.
- The fake keeps all data in FakeBackend (Python) through a Playwright binding: shared between browser contexts
  ("devices"), atomic, surviving reloads. It models F01 RLS (active manager/editor write, viewer read-only ->
  42501, pending/removed/outsider -> 0 rows, anon -> permission denied).
- Every other external request is logged and aborted.

Env: APP_DIR (default /home/claude/plan_travel/app), BASELINE_DIR, SCRIPTS_DIR (import tool, criterion 4),
QA_PSQL (optional psql args of a scratch DB with the F01 schema, used by criterion 4 to check values byte-for-byte),
QA_HEADFUL=1 to watch.
"""
import copy
import datetime
import difflib
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

# Route service-worker network traffic through context.route as well (so the SW cannot bypass the fake/logging).
os.environ.setdefault("PW_EXPERIMENTAL_SERVICE_WORKER_NETWORK_EVENTS", "1")
from playwright.sync_api import Error as PWError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

HERE = Path(__file__).resolve().parent
JS_TOOLS = HERE / "js_tools"
FAKE_JS = (HERE / "fake_supabase.js").read_text(encoding="utf-8")

APP_DIR = Path(os.environ.get("APP_DIR", "/home/claude/plan_travel/app"))
BASELINE_DIR = Path(os.environ.get("BASELINE_DIR", "/home/claude/orsela/vietnam_travel_planner"))
SCRIPTS_DIR = Path(os.environ.get("SCRIPTS_DIR", "/home/claude/plan_travel/scripts"))
APP_PATH = "/Plan_Travel/"
BASE_PATH = "/Vietnam_Travel_Planner/"
BLANK_PATH = "/__qa__/blank.html"

PT_REF = "zwufpxnioqaweobnvffs"
PT_URL = "https://zwufpxnioqaweobnvffs.supabase.co"
PT_KEY = "sb_publishable_Atuc7ybYTF2o7ujCJBjKjA_So_TbaV8"
AUTH_KEY = "sb-zwufpxnioqaweobnvffs-auth-token"
LIVE_REF = "kkitwcnkoxuhbcsabcdl"
LIVE_URL = "https://kkitwcnkoxuhbcsabcdl.supabase.co"

VERSION = "3.0.0-alpha.2"
NC_TITLE = "המכשיר עדיין לא מחובר לטיול"
NC_BODY = "שלחו למנהל הטיול את קוד המכשיר:"
NC_RETRY = "נסו שוב"
CHAT_TOAST = "הצ'אט יחזור בקרוב"
PROTECTED = ["drawMap", "drawGoogleMap", "drawSchematicMap", "fallbackToSchematic", "SEED_DATA", "stayBlock", "bookingUrl"]

VIEWPORT = {"width": 390, "height": 844}
HEADFUL = os.environ.get("QA_HEADFUL") == "1"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# =====================================================================================================
# Fake Supabase backend (Python side of fake_supabase.js)
# =====================================================================================================
def _err(code, message, status, details=None, hint=None):
    return {"error": {"code": code, "message": message, "details": details, "hint": hint}, "status": status, "data": None}


FETCH_ERR = {"message": "TypeError: Failed to fetch", "details": "TypeError: Failed to fetch", "hint": "", "code": ""}

TABLES = {
    "trip_kv": {"cols": ["trip_id", "key", "value", "updated_at", "updated_by"], "pk": ("trip_id", "key"),
                "uuid": {"trip_id", "updated_by"}},
    "trip_members": {"cols": ["id", "trip_id", "user_id", "display_name", "role", "status", "joined_at", "approved_at",
                              "last_seen_at"], "pk": ("id",), "uuid": {"id", "trip_id", "user_id"}},
    "trips": {"cols": ["id", "name", "start_date", "end_date", "home_currency", "default_lang", "default_role", "status",
                       "created_by", "created_at", "updated_at"], "pk": ("id",), "uuid": {"id", "created_by"}},
    "planner_kv": {"cols": ["key", "value", "updated_at"], "pk": ("key",), "uuid": set()},
}


class Unsupported(Exception):
    pass


def like_to_regex(pattern, ci=False):
    out, i = [], 0
    pattern = pattern.replace("*", "%")  # PostgREST treats * as % in like patterns
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if c == "%" else "." if c == "_" else re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$", re.S | (re.I if ci else 0))


def split_top(s):
    parts, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


class FakeBackend:
    def __init__(self):
        self.lock = threading.RLock()
        self.users, self.tokens = {}, {}
        self.rows = {t: {} for t in TABLES}  # table -> {pk tuple: row}
        self.mode = "up"  # up | down | hang
        self.anon_enabled = True
        self.auto_link = None  # dict(trip_id, role, status, display_name)
        self.legacy_ok = False
        self.calls, self.page_logs, self.violations = [], [], []
        self._t0 = datetime.datetime(2026, 10, 4, 6, 0, 0, tzinfo=datetime.timezone.utc)
        self._tick = 0

    # ---------- helpers used by tests ----------
    def ts(self):
        self._tick += 1
        return (self._t0 + datetime.timedelta(milliseconds=self._tick)).isoformat()

    def add_trip(self, name="QA trip", tid=None):
        tid = tid or str(uuid.uuid4())
        self.rows["trips"][(tid,)] = {"id": tid, "name": name, "start_date": "2026-09-15", "end_date": "2026-10-06",
                                      "home_currency": "ILS", "default_lang": "he", "default_role": "editor",
                                      "status": "active", "created_by": None, "created_at": self.ts(), "updated_at": self.ts()}
        return tid

    def add_member(self, tid, uid, role="editor", status="active", display_name="QA", joined_at=None):
        mid = str(uuid.uuid4())
        self.rows["trip_members"][(mid,)] = {"id": mid, "trip_id": tid, "user_id": uid, "display_name": display_name,
                                             "role": role, "status": status, "joined_at": joined_at or self.ts(),
                                             "approved_at": self.ts() if status == "active" else None, "last_seen_at": None}
        return mid

    def set_member(self, tid, uid, **fields):
        for r in self.rows["trip_members"].values():
            if r["trip_id"] == tid and r["user_id"] == uid:
                r.update(fields)
                return r
        return None

    def members_of(self, uid):
        return [r for r in self.rows["trip_members"].values() if r["user_id"] == uid]

    def set_kv(self, tid, key, value, updated_by=None):
        self.rows["trip_kv"][(tid, key)] = {"trip_id": tid, "key": key, "value": value, "updated_at": self.ts(),
                                            "updated_by": updated_by}

    def kv(self, tid, key):
        r = self.rows["trip_kv"].get((tid, key))
        return r["value"] if r else None

    def kv_row(self, tid, key):
        return self.rows["trip_kv"].get((tid, key))

    def set_planner(self, key, value):
        self.rows["planner_kv"][(key,)] = {"key": key, "value": value, "updated_at": self.ts()}

    def planner(self, key):
        r = self.rows["planner_kv"].get((key,))
        return r["value"] if r else None

    def queries(self, table=None, action=None, device=None):
        out = []
        for c in self.calls:
            if c.get("op") != "query":
                continue
            req = c["msg"].get("req") or {}
            if table and req.get("table") != table:
                continue
            if action and req.get("action") not in (action if isinstance(action, (list, tuple, set)) else [action]):
                continue
            if device and c["device"] != device:
                continue
            out.append(c)
        return out

    def ops(self, op, device=None):
        return [c for c in self.calls if c.get("op") == op and (device is None or c["device"] == device)]

    # ---------- binding entry point ----------
    def handle(self, device, msg):
        with self.lock:
            msg = msg or {}
            op = msg.get("op")
            if op == "log":
                self.page_logs.append({"device": device, **(msg.get("entry") or {})})
                url = str((msg.get("entry") or {}).get("url") or "")
                if LIVE_REF in url:
                    self.violations.append({"device": device, "where": "createClient", "url": url, "page": msg.get("page")})
                return {"ok": True}
            if op == "control":
                return self.control(msg)
            url = str(msg.get("url") or "")
            entry = {"device": device, "t": time.time(), "op": op, "page": msg.get("page"), "online": msg.get("online"),
                     "url": url, "msg": copy.deepcopy(msg)}
            self.calls.append(entry)
            if LIVE_REF in url:
                self.violations.append({"device": device, "where": op, "url": url, "page": msg.get("page")})
            if msg.get("online") is False or self.mode == "down":
                entry["result"] = {"offline": True}
                return {"error": dict(FETCH_ERR), "status": 0, "data": None}
            if self.mode == "hang":
                entry["result"] = {"hang": True}
                return {"hang": True}
            legacy = url.startswith(LIVE_URL) and self.legacy_ok and str(msg.get("page") or "").startswith(BASE_PATH)
            if not legacy and (url.rstrip("/") != PT_URL or msg.get("key") != PT_KEY):
                res = {"error": {"code": "FAKE_WRONG_PROJECT", "message": "fake: unreachable project or wrong key (%s)" % url,
                                 "details": None, "hint": None}, "status": 0, "data": None}
                entry["result"] = res
                return res
            try:
                if op == "auth.signInAnonymously":
                    res = self.sign_in()
                elif op == "auth.getUser":
                    uid = self.tokens.get(msg.get("token"))
                    res = {"user": self.users[uid]} if uid else {"error": {"message": "invalid JWT", "status": 401}}
                elif op == "query":
                    res = self.query(msg.get("token"), msg.get("req") or {}, legacy)
                else:
                    res = {"error": {"code": "FAKE_NA", "message": "fake: %s not available" % op}, "status": 404}
            except Unsupported as e:
                res = _err("FAKE_UNSUPPORTED", "fake: unsupported query shape: %s" % e, 400)
            except Exception as e:  # pragma: no cover - harness bug guard
                res = _err("FAKE_BUG", "fake internal error: %r" % (e,), 500)
            entry["result"] = copy.deepcopy(res)
            return res

    def control(self, msg):
        a = msg.get("action")
        if a == "state":
            return {"mode": self.mode, "rows": {t: list(v.values()) for t, v in self.rows.items()}}
        if a == "setMode":
            self.mode = msg.get("mode") or "up"
            return {"ok": True}
        if a == "link":
            self.add_member(msg["trip_id"], msg["user_id"], msg.get("role", "editor"), msg.get("status", "active"))
            return {"ok": True}
        return {"error": "unknown control"}

    def sign_in(self):
        if not self.anon_enabled:
            return {"error": {"name": "AuthApiError", "message": "Anonymous sign-ins are disabled", "status": 422,
                              "code": "anonymous_provider_disabled"}}
        uid = str(uuid.uuid4())
        token = "fake-at." + uid + "." + uuid.uuid4().hex[:8]
        now = self.ts()
        user = {"id": uid, "aud": "authenticated", "role": "authenticated", "email": "", "phone": "", "is_anonymous": True,
                "app_metadata": {"provider": "anonymous", "providers": ["anonymous"]}, "user_metadata": {},
                "identities": [], "created_at": now, "updated_at": now}
        self.users[uid] = user
        self.tokens[token] = uid
        if self.auto_link:
            al = self.auto_link
            self.add_member(al["trip_id"], uid, al.get("role", "editor"), al.get("status", "active"), al.get("display_name", "QA"))
        session = {"access_token": token, "refresh_token": "fake-rt-" + uuid.uuid4().hex, "token_type": "bearer",
                   "expires_in": 3600, "expires_at": int(time.time()) + 3600, "user": user}
        return {"session": session}

    # ---------- query engine ----------
    def active_trips(self, uid, roles=("manager", "editor", "viewer")):
        return {m["trip_id"] for m in self.rows["trip_members"].values()
                if m["user_id"] == uid and m["status"] == "active" and m["role"] in roles}

    def query(self, token, req, legacy):
        table, action = str(req.get("table")), str(req.get("action"))
        if legacy:
            if table != "planner_kv":
                return _err("PGRST205", "Could not find the table 'public.%s' in the schema cache" % table, 404)
            return self.exec(table, req, None, lambda r: True, lambda r: True)
        if table not in ("trip_kv", "trip_members", "trips"):
            return _err("PGRST205", "Could not find the table 'public.%s' in the schema cache" % table, 404)
        if token and token not in self.tokens:
            return _err("PGRST301", "JWT is invalid", 401)
        uid = self.tokens.get(token) if token else None
        if uid is None:  # anon key without a session: F01 grants nothing to anon
            return _err("42501", "permission denied for table %s" % table, 401)
        readable = self.active_trips(uid)
        writable = self.active_trips(uid, ("manager", "editor"))
        if table == "trip_kv":
            vis, can_write = (lambda r: r["trip_id"] in readable), (lambda r: r["trip_id"] in writable)
        elif table == "trip_members":
            vis, can_write = (lambda r: r["user_id"] == uid or r["trip_id"] in readable), None
        else:
            vis, can_write = (lambda r: r["id"] in readable), None
        if action != "select" and can_write is None:
            return _err("42501", "permission denied for table %s" % table, 403)
        return self.exec(table, req, uid, vis, can_write)

    def _check_filter_cols(self, table, filters):
        cols = TABLES[table]["cols"]
        for f in filters:
            if f["op"] == "or":
                raise Unsupported("or(): " + str(f["val"]))
            if f["col"] not in cols:
                return _err("42703", "column %s.%s does not exist" % (table, f["col"]), 400)
            if f["col"] in TABLES[table]["uuid"] and f["op"] in ("eq", "neq", "not.eq"):
                v = f.get("val")
                if v is None or not UUID_RE.match(str(v)):
                    return _err("22P02", 'invalid input syntax for type uuid: "%s"' % ("undefined" if v is None else v), 400)
        return None

    @staticmethod
    def _match(row, f):
        op, col, val = f["op"], f["col"], f.get("val")
        neg = op.startswith("not.")
        if neg:
            op = op[4:]
        rv = row.get(col)
        sv = None if rv is None else str(rv)
        if op == "eq":
            res = sv is not None and sv == str(val)
        elif op == "neq":
            res = sv is not None and sv != str(val)
        elif op in ("gt", "gte", "lt", "lte"):
            if sv is None:
                res = False
            else:
                try:
                    a, b = float(sv), float(val)
                except (TypeError, ValueError):
                    a, b = sv, str(val)
                res = {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
        elif op in ("like", "ilike"):
            res = sv is not None and bool(like_to_regex(str(val), op == "ilike").match(sv))
        elif op == "is":
            res = (rv is None) if val in (None, "null") else (str(rv).lower() == str(val).lower())
        elif op == "in":
            vals = val if isinstance(val, list) else [val]
            res = sv is not None and sv in [str(x) for x in vals]
        else:
            raise Unsupported("filter op " + op)
        return (not res) if neg else res

    def _project(self, table, rows, columns):
        columns = (columns or "*").strip()
        cols = TABLES[table]["cols"]
        if columns == "*":
            return [dict(r) for r in rows], None
        spec = split_top(columns.replace("\n", " "))
        out = []
        for r in rows:
            o = {}
            for item in spec:
                m = re.match(r"^(?:(\w+):)?\s*(\w+)(?:!\w+)?\s*\((.*)\)$", item, re.S)
                if m:  # embedded resource, e.g. trips(name)
                    alias, rel, inner = m.group(1), m.group(2), m.group(3)
                    if rel != "trips" or "trip_id" not in r:
                        return None, _err("PGRST200", "Could not find a relationship between '%s' and '%s'" % (table, rel), 400)
                    t = self.rows["trips"].get((r["trip_id"],))
                    sub, e = self._project("trips", [t] if t else [], inner)
                    if e:
                        return None, e
                    o[alias or rel] = sub[0] if sub else None
                    continue
                m = re.match(r"^(?:(\w+):)?\s*(\w+)(?:::\w+)?$", item)
                if not m:
                    raise Unsupported("select item " + item)
                alias, col = m.group(1), m.group(2)
                if col not in cols:
                    return None, _err("42703", "column %s.%s does not exist" % (table, col), 400)
                o[alias or col] = r.get(col)
            out.append(o)
        return out, None

    def _shape(self, table, rows, req):
        data, e = self._project(table, rows, req.get("columns"))
        if e:
            return e
        count = len(data) if req.get("count") else None
        if req.get("head"):
            return {"data": None, "count": count, "status": 200, "error": None}
        if req.get("single"):
            if len(data) != 1:
                return _err("PGRST116", "JSON object requested, multiple (or no) rows returned", 406,
                            "The result contains %d rows" % len(data))
            return {"data": data[0], "count": count, "status": 200, "error": None}
        if req.get("maybe"):
            if len(data) > 1:
                return _err("PGRST116", "JSON object requested, multiple (or no) rows returned", 406,
                            "The result contains %d rows" % len(data))
            return {"data": data[0] if data else None, "count": count, "status": 200, "error": None}
        return {"data": data, "count": count, "status": 200, "error": None}

    def exec(self, table, req, uid, vis, can_write):
        action = req.get("action")
        filters = req.get("filters") or []
        e = self._check_filter_cols(table, filters)
        if e:
            return e
        store = self.rows[table]
        cols, pk = TABLES[table]["cols"], TABLES[table]["pk"]

        def sel(rows):
            return [r for r in rows if vis(r) and all(self._match(r, f) for f in filters)]

        if action == "select":
            rows = sel(list(store.values()))
            for o in reversed(req.get("order") or []):
                if o["col"] not in cols:
                    return _err("42703", "column %s.%s does not exist" % (table, o["col"]), 400)
                rows.sort(key=lambda r: (r.get(o["col"]) is None, str(r.get(o["col"]) or "")), reverse=not o.get("asc", True))
            if req.get("range"):
                a, b = req["range"]
                rows = rows[a:b + 1]
            if req.get("limit") is not None:
                rows = rows[: int(req["limit"])]
            return self._shape(table, rows, req)

        if action in ("insert", "upsert"):
            values = req.get("values")
            vals = values if isinstance(values, list) else [values]
            opts = req.get("options") or {}
            oc = opts.get("onConflict")
            if action == "upsert" and oc and sorted(c.strip() for c in str(oc).split(",")) != sorted(pk):
                return _err("42P10", "there is no unique or exclusion constraint matching the ON CONFLICT specification", 400)
            staged, out = dict(store), []
            for v in vals:
                if not isinstance(v, dict):
                    return _err("PGRST102", "Empty or invalid json", 400)
                for c in v:
                    if c not in cols:
                        return _err("PGRST204", "Could not find the '%s' column of '%s' in the schema cache" % (c, table), 400)
                for c in pk:
                    if v.get(c) is None:
                        return _err("23502", 'null value in column "%s" of relation "%s" violates not-null constraint' % (c, table), 400)
                for c in TABLES[table]["uuid"]:
                    if v.get(c) is not None and not UUID_RE.match(str(v[c])):
                        return _err("22P02", 'invalid input syntax for type uuid: "%s"' % v[c], 400)
                if table == "trip_kv" and not (1 <= len(str(v["key"])) <= 200):
                    return _err("23514", 'new row for relation "trip_kv" violates check constraint "trip_kv_key_len"', 400)
                v = dict(v)
                if "value" in v and v["value"] is not None and not isinstance(v["value"], str):
                    v["value"] = json.dumps(v["value"], ensure_ascii=False, separators=(",", ":"))
                k = tuple(str(v[c]) for c in pk)
                exists = k in staged
                if exists and action == "insert":
                    return _err("23505", 'duplicate key value violates unique constraint "%s_pkey"' % table, 409)
                if exists:
                    if opts.get("ignoreDuplicates"):
                        continue
                    if not can_write(staged[k]):
                        return _err("42501", 'new row violates row-level security policy (USING expression) for table "%s"' % table,
                                    403 if uid else 401)
                    row = dict(staged[k])
                    row.update(v)
                    if "updated_at" in cols:
                        row["updated_at"] = self.ts()  # BEFORE UPDATE trigger (F01)
                    if table == "trip_kv" and "updated_by" not in v:
                        row["updated_by"] = uid  # §4 contract: "DB sets them"
                else:
                    row = {c: None for c in cols}
                    if "updated_at" in cols:
                        row["updated_at"] = self.ts()
                    if table == "trip_kv":
                        row["updated_by"] = uid  # column default auth.uid()
                    row.update(v)
                    if not can_write(row):
                        return _err("42501", 'new row violates row-level security policy for table "%s"' % table, 403 if uid else 401)
                staged[k] = row
                out.append(row)
            self.rows[table] = staged
            if req.get("returning"):
                return self._shape(table, out, {**req, "single": req.get("single"), "maybe": req.get("maybe")})
            return {"data": None, "status": 201, "error": None}

        if action == "update":
            v = req.get("values") or {}
            for c in v:
                if c not in cols:
                    return _err("PGRST204", "Could not find the '%s' column of '%s' in the schema cache" % (c, table), 400)
            hit = [r for r in sel(list(store.values())) if can_write(r)]
            if table == "trip_kv" and "trip_id" in v and any(str(r["trip_id"]) != str(v["trip_id"]) for r in hit):
                return _err("23514", "trip_kv_trip_locked: a key cannot be moved to another trip", 400)
            out = []
            for r in hit:
                r.update(v)
                if "updated_at" in cols:
                    r["updated_at"] = self.ts()
                if table == "trip_kv" and "updated_by" not in v:
                    r["updated_by"] = uid
                out.append(r)
            if req.get("returning"):
                return self._shape(table, out, req)
            return {"data": None, "status": 204, "error": None}

        if action == "delete":
            hit = [r for r in sel(list(store.values())) if can_write(r)]
            for r in hit:
                store.pop(tuple(str(r[c]) for c in pk), None)
            if req.get("returning"):
                return self._shape(table, hit, req)
            return {"data": None, "status": 204, "error": None}
        raise Unsupported("action " + str(action))


# =====================================================================================================
# Static server (one origin, two apps)
# =====================================================================================================
class Server:
    def __init__(self):
        self.overrides = {}  # url path -> bytes
        self.log = []
        srv_self = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = unquote(urlparse(self.path).path)
                srv_self.log.append(path)
                if path in srv_self.overrides:
                    return self._send(200, srv_self.overrides[path], mimetypes.guess_type(path)[0] or "text/html")
                if path == BLANK_PATH:
                    return self._send(200, b"<!doctype html><meta charset=utf-8><title>qa</title>", "text/html")
                for prefix, root in ((APP_PATH, APP_DIR), (BASE_PATH, BASELINE_DIR)):
                    if path == prefix.rstrip("/"):
                        self.send_response(301)
                        self.send_header("Location", prefix)
                        self.end_headers()
                        return
                    if path.startswith(prefix):
                        rel = path[len(prefix):]
                        f = (root / rel).resolve()
                        if not str(f).startswith(str(root.resolve())):
                            return self._send(403, b"forbidden", "text/plain")
                        if f.is_dir():
                            f = f / "index.html"
                        if f.is_file():
                            ctype = "application/manifest+json" if f.suffix == ".webmanifest" else (
                                mimetypes.guess_type(str(f))[0] or "application/octet-stream")
                            if f.name == "index.html" and (prefix + "index.html") in srv_self.overrides:
                                return self._send(200, srv_self.overrides[prefix + "index.html"], "text/html")
                            if f.name == "index.html" and prefix in srv_self.overrides:
                                return self._send(200, srv_self.overrides[prefix], "text/html")
                            return self._send(200, f.read_bytes(), ctype)
                        return self._send(404, b"not found", "text/plain")
                return self._send(404, b"not found", "text/plain")

            def _send(self, code, body, ctype):
                self.send_response(code)
                if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                    ctype += "; charset=utf-8"
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.origin = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()


# =====================================================================================================
# Browser environment
# =====================================================================================================
WATCH_JS = r"""
(() => {
  if (window.__qaWatch) return;
  const W = window.__qaWatch = { sawNotConnected: false, copied: [], errors: [] };
  const T = %s;
  const chk = () => { try { if (document.body && document.body.innerText.includes(T)) W.sawNotConnected = true; } catch (e) {} };
  setInterval(chk, 250);
  document.addEventListener('DOMContentLoaded', chk);
  window.addEventListener('error', e => W.errors.push(String(e.message || e)));
  try {
    const cb = navigator.clipboard;
    if (cb && cb.writeText) {
      const orig = cb.writeText.bind(cb);
      cb.writeText = (t) => { W.copied.push(String(t)); return orig(t).catch(() => {}); };
    }
  } catch (e) {}
  try {
    const ex = document.execCommand.bind(document);
    document.execCommand = function (cmd) {
      if (String(cmd).toLowerCase() === 'copy') {
        try {
          const a = document.activeElement;
          const sel = (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA')) ? a.value.slice(a.selectionStart, a.selectionEnd) : String(getSelection());
          W.copied.push(sel);
        } catch (e) {}
      }
      return ex.apply(document, arguments);
    };
  } catch (e) {}
})();
""" % json.dumps(NC_TITLE)


# Raw storage is read and written from a SECOND page of the same context, on a harness-served blank page
# (BLANK_PATH, outside both app directories, so no app script and no app service worker ever runs there).
# Storage is per origin, so this page sees exactly what is stored, not the app's namespaced view (§2 wraps
# Storage.prototype / indexedDB.open for the whole app page).
RAW_LS_DUMP = """()=>{const o={};for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);o[k]=localStorage.getItem(k)}return o}"""
RAW_LS_SET = """(o)=>{for(const[k,v] of Object.entries(o))localStorage.setItem(k,v)}"""


class Device:
    def __init__(self, env, name, ctx, page, conf):
        self.env, self.name, self.ctx, self.page, self.conf = env, name, ctx, page, conf
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())

    def goto(self, path, ready=True, timeout=30000):
        self.page.goto(self.env.server.origin + path, wait_until="domcontentloaded", timeout=timeout)
        if ready:
            return wait_ready(self.page)
        return None

    def raw(self):
        """The raw-storage page (created on first use, re-created if closed)."""
        if getattr(self, "_raw", None) is None or self._raw.is_closed():
            self._raw = self.ctx.new_page()
            self._raw.on("dialog", lambda d: d.accept())
        if not self._raw.url.endswith(BLANK_PATH):
            self._raw.goto(self.env.server.origin + BLANK_PATH, wait_until="domcontentloaded")
        return self._raw

    def raw_eval(self, js, arg=None):
        return self.raw().evaluate(js, arg) if arg is not None else self.raw().evaluate(js)

    def ls(self):
        return self.raw_eval(RAW_LS_DUMP)

    def seed_ls(self, obj):
        self.raw_eval(RAW_LS_SET, obj)

    def idb_names(self):
        return self.raw_eval("async()=>(await indexedDB.databases()).map(d=>d.name).sort()")

    def auth_raw_key(self):
        """Where the session really is in raw localStorage. §3 names the default key; §2 allows device-level data
        under pt:_device:<name>, so an app storage adapter that namespaces it there is accepted too."""
        ls = self.ls()
        for k in (AUTH_KEY, "pt:_device:" + AUTH_KEY):
            if k in ls:
                return k
        return None

    def uid(self):
        k = self.auth_raw_key()
        raw = self.ls().get(k) if k else None
        if not raw:
            return None
        try:
            s = json.loads(raw)
            return (s.get("user") or {}).get("id") or ((s.get("currentSession") or {}).get("user") or {}).get("id")
        except Exception:
            return None

    def fake_log(self):
        try:
            return self.page.evaluate("()=>window.__fakeLog||[]")
        except PWError:
            return []

    def offline(self, on):
        self.ctx.set_offline(on)


class Env:
    def __init__(self, browser, server):
        self.browser, self.server = browser, server
        self.backend = FakeBackend()
        self.net = []
        self.devices = []

    def new_device(self, name="A", service_workers="block", legacy_fake=False, fixed_time=None):
        conf = {"legacy_fake": legacy_fake}
        ctx = self.browser.new_context(
            viewport=VIEWPORT, locale="he-IL", timezone_id="Asia/Jerusalem", service_workers=service_workers,
            permissions=["geolocation", "clipboard-read", "clipboard-write"],
            geolocation={"latitude": 21.0285, "longitude": 105.8542, "accuracy": 20})
        if fixed_time:
            ctx.clock.set_fixed_time(fixed_time)
        ctx.expose_binding("__fakeBackend", lambda source, msg, _n=name: self.backend.handle(_n, msg))
        ctx.add_init_script(WATCH_JS)
        ctx.route("**/*", lambda route, request, _n=name, _c=conf: self._route(_n, _c, route, request))
        page = ctx.new_page()
        d = Device(self, name, ctx, page, conf)
        self.devices.append(d)
        return d

    def _route(self, name, conf, route, request):
        url = request.url
        p = urlparse(url)
        try:
            frame_url = request.frame.url
        except Exception:
            frame_url = ""
        if not frame_url:
            try:
                frame_url = request.service_worker.url if request.service_worker else ""
            except Exception:
                frame_url = ""
        rec = {"device": name, "url": url, "method": request.method, "type": request.resource_type, "frame": frame_url}
        self.net.append(rec)
        try:
            if p.hostname in ("127.0.0.1", "localhost"):
                rec["action"] = "same-origin"
                return route.continue_()
            if p.hostname == "cdn.jsdelivr.net" and "@supabase/supabase-js" in p.path:
                from_base = urlparse(frame_url).path.startswith(BASE_PATH)
                if from_base and not conf["legacy_fake"]:
                    rec["action"] = "blocked-lib(baseline)"
                    return route.abort()
                rec["action"] = "fake-lib"
                return route.fulfill(status=200, content_type="application/javascript; charset=utf-8", body=FAKE_JS,
                                     headers={"Access-Control-Allow-Origin": "*"})
            rec["action"] = "aborted"
            return route.abort()
        except PWError:
            pass

    def external(self):
        return [r for r in self.net if r.get("action") not in ("same-origin",)]

    def close(self):
        for d in self.devices:
            try:
                d.ctx.close()
            except Exception:
                pass


@pytest.fixture(scope="session")
def server():
    s = Server()
    yield s
    s.stop()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=not HEADFUL)
        yield b
        b.close()


@pytest.fixture
def env(browser, server):
    server.overrides.clear()
    e = Env(browser, server)
    yield e
    e.close()
    server.overrides.clear()


# =====================================================================================================
# UI helpers (they work on v2.15.2 and on the new app: same DOM)
# =====================================================================================================
READY_JS = """(t)=>{const h=document.getElementById('homeV2');
 if(h&&h.classList.contains('active')&&getComputedStyle(h).display!=='none')return 'app';
 if(document.body&&document.body.innerText.includes(t))return 'nc';return false}"""


def wait_ready(page, timeout=30000):
    h = page.wait_for_function(READY_JS, arg=NC_TITLE, timeout=timeout)
    state = h.json_value()
    page.wait_for_timeout(700 if state == "app" else 300)
    return state


def wait_until(fn, timeout=15.0, interval=0.2, page=None, msg="condition"):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:  # noqa: BLE001
            last = e
        if page is not None:
            page.wait_for_timeout(int(interval * 1000))
        else:
            time.sleep(interval)
    raise AssertionError("timed out waiting for %s (last=%r)" % (msg, last))


def click(page, sel, timeout=3000):
    try:
        page.locator(sel).first.click(timeout=timeout)
    except PWError:
        page.eval_on_selector(sel, "e=>e.click()")


def ensure_user(page, name=None):
    try:
        page.wait_for_timeout(300)
        if page.locator("#userModal.open").count():
            page.wait_for_selector("#userChoices .urow", timeout=6000)
            loc = page.locator("#userChoices .urow", has_text=name) if name else page.locator("#userChoices .urow")
            loc.first.click()
            page.wait_for_timeout(300)
    except PWError:
        pass


def tab(page, panel):
    click(page, '.tab[data-panel="%s"]' % panel)
    page.wait_for_timeout(350)


def add_expense(page, desc, amount):
    tab(page, "budget")
    page.evaluate("()=>{const d=document.getElementById('addExpenseDetails');if(d)d.open=true}")
    click(page, '#expenseCurRow [data-cur="ILS"]')
    page.fill("#expenseDescription", desc)
    page.fill("#expenseAmount", str(amount))
    click(page, "#saveExpense")
    page.wait_for_function("()=>!document.getElementById('expenseDescription').value", timeout=20000)
    page.wait_for_timeout(300)


def delete_expense(page, desc):
    tab(page, "budget")
    ok = page.evaluate("""(d)=>{const rows=[...document.querySelectorAll('#expenseList tr')].filter(r=>r.innerText.includes(d));
      const b=rows.length&&rows[0].querySelector('[data-delete-expense]');if(!b)return false;b.click();return true}""", desc)
    assert ok, "expense %r is not in the budget list (shared data not loaded on this device)" % desc
    page.wait_for_timeout(1500)


def add_journal(page, text):
    tab(page, "journal")
    page.fill("#journalText", text)
    click(page, "#saveJournal")
    page.wait_for_function("()=>!document.getElementById('journalText').value", timeout=20000)
    page.wait_for_timeout(300)


def close_modal(page, mid):
    try:
        page.locator("#%s [data-modal-x]" % mid).first.click(timeout=2000)
    except PWError:
        page.evaluate("(id)=>{const m=document.getElementById(id);if(m)m.classList.remove('open')}", mid)
    page.wait_for_timeout(200)


def queue_of(ls, prefix):
    raw = ls.get(prefix + "planner:__queue__")
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return None


def full_session(dev, tag="S"):
    """Navigate every tab, open reservations / vault / converter, add an expense and a journal entry,
    then go offline, add an expense, come back online and let the queue flush."""
    page = dev.page
    ensure_user(page)
    for p in ("route", "journal", "budget", "more", "home"):
        tab(page, p)
    tab(page, "more")
    click(page, "#reservationsBtn")
    page.wait_for_timeout(500)
    close_modal(page, "reservationsModal")
    click(page, "#documentsBtn")
    page.wait_for_timeout(500)
    close_modal(page, "documentsModal")
    click(page, "#globalFxBtn")
    page.wait_for_timeout(300)
    close_modal(page, "fxQuickModal")
    add_expense(page, "QA %s expense 1" % tag, 11)
    add_journal(page, "QA %s journal" % tag)
    dev.offline(True)
    page.wait_for_timeout(300)
    add_expense(page, "QA %s offline" % tag, 22)
    dev.offline(False)
    page.wait_for_timeout(3000)
    tab(page, "home")


def trip_name_is(page, name):
    """#tripName is filled from planner:tripname by the v2.15.2 boot (present in the DOM in both home layouts)."""
    return page.evaluate("(n)=>{const e=document.getElementById('tripName');return !!e&&e.textContent.trim()===n}", name)


def text_has(page, text):
    return page.evaluate("(t)=>document.body.innerText.includes(t)", text)


def linked_setup(env, role="editor", status="active", tripname="טיול QA"):
    be = env.backend
    tid = be.add_trip("QA trip")
    if tripname:
        be.set_kv(tid, "tripname", tripname)
    be.auto_link = {"trip_id": tid, "role": role, "status": status, "display_name": "QA"}
    return tid


def boot_linked(env, name="A", role="editor", **kw):
    tid = linked_setup(env, role=role)
    dev = env.new_device(name, **kw)
    state = dev.goto(APP_PATH)
    return tid, dev, state


def node(args, **kw):
    return subprocess.run(["node"] + args, capture_output=True, text=True, timeout=120, **kw)


def read_app(name):
    return (APP_DIR / name).read_text(encoding="utf-8")


# =====================================================================================================
# 1 · Identity
# =====================================================================================================
def test_ac01_anonymous_identity_silent_and_survives_reload(env):
    """C1: new linked device gets an anonymous identity on first open, no new screen, same auth.uid() after reload.
    (Real-phone part: MANUAL.md.)"""
    tid, dev, state = boot_linked(env)
    be = env.backend
    assert state == "app", "linked device did not reach the app (state=%s)" % state
    assert not dev.page.evaluate("()=>window.__qaWatch.sawNotConnected"), "the not-connected card flashed for a linked device"
    created = [e for e in dev.fake_log() if e.get("kind") == "createClient"]
    assert created, "supabase-js createClient was never called"
    for c in created:
        assert c["url"].rstrip("/") == PT_URL and c["key"] == PT_KEY, "client not created with the plan-travel URL/key: %r" % c
        assert c["persistSession"] is not False, "persistSession is false"
        assert c["storageKey"] == AUTH_KEY, "session not stored under the default key %s: %r" % (AUTH_KEY, c["storageKey"])
    signins = be.ops("auth.signInAnonymously", "A")
    assert len(signins) == 1, "expected exactly 1 anonymous sign-in on first open, got %d" % len(signins)
    uid1 = dev.uid()
    assert uid1 and UUID_RE.match(uid1), "no session persisted under %s" % AUTH_KEY
    assert be.members_of(uid1), "auto-linked member row missing (harness)"
    cached = dev.ls().get("pt:_device:active_trip")
    assert cached and tid in cached, "pt:_device:active_trip not cached with the trip id: %r" % cached
    # reload: same identity, no second sign-in
    dev.page.reload(wait_until="domcontentloaded")
    assert wait_ready(dev.page) == "app"
    assert len(be.ops("auth.signInAnonymously", "A")) == 1, "a second anonymous identity was created on reload"
    assert dev.uid() == uid1, "auth.uid() changed after reload"
    assert not dev.page.evaluate("()=>window.__qaWatch.sawNotConnected")


# =====================================================================================================
# 2 · Not connected
# =====================================================================================================
def nc_card_info(page):
    return page.evaluate("""([title, retry])=>{
      const all=[...document.querySelectorAll('body *')].filter(e=>e.childElementCount===0||[...e.childNodes].some(n=>n.nodeType===3&&n.textContent.includes(title)));
      const t=all.find(e=>e.textContent.includes(title)&&e.offsetParent!==null);
      if(!t)return null;
      let card=t;while(card&&card!==document.body&&![...card.querySelectorAll('button,[role=button]')].some(b=>b.textContent.includes(retry)))card=card.parentElement;
      if(!card||card===document.body)return {noRetry:true};
      const r=card.getBoundingClientRect(),cs=getComputedStyle(card);
      const buttons=[...card.querySelectorAll('button,[role=button]')].filter(b=>b.offsetParent!==null).map(b=>{const q=b.getBoundingClientRect();return{text:b.textContent.trim(),aria:b.getAttribute('aria-label')||'',w:q.width,h:q.height}});
      return {text:card.innerText,rect:{l:r.left,r:r.right,t:r.top,b:r.bottom,w:r.width},dir:cs.direction,buttons,
              htmlDir:document.documentElement.getAttribute('dir'),scrollW:document.scrollingElement.scrollWidth,innerW:innerWidth};
    }""", [NC_TITLE, NC_RETRY])


def code_style(page, code):
    return page.evaluate("""(code)=>{const c=code.toLowerCase();
      const el=[...document.querySelectorAll('body *')].find(e=>e.offsetParent!==null&&e.childElementCount===0&&e.textContent.trim().toLowerCase()===c);
      if(!el)return null;const s=getComputedStyle(el);return{font:s.fontFamily,size:parseFloat(s.fontSize)}}""", code)


def tabs_visible(page):
    return page.evaluate("""()=>[...document.querySelectorAll('.tab, .panel.active')].filter(t=>{
      const r=t.getBoundingClientRect();if(!r.width||!r.height||t.offsetParent===null)return false;
      const s=getComputedStyle(t);if(s.visibility==='hidden'||+s.opacity===0)return false;
      const x=Math.min(Math.max(r.left+r.width/2,0),innerWidth-1),y=Math.min(Math.max(r.top+Math.min(r.height/2,20),0),innerHeight-1);
      const hit=document.elementFromPoint(x,y);return hit&&(t===hit||t.contains(hit));}).map(t=>t.className+'#'+t.id)""")


@pytest.mark.parametrize("case", ["unlinked", "pending", "removed"])
def test_ac02_not_connected_card(env, case):
    """C2: unlinked device / pending / removed member -> 0 trip rows, the §3 card (not an empty app); 'נסו שוב' re-runs step 3."""
    be = env.backend
    tid = be.add_trip("QA trip")
    be.set_kv(tid, "tripname", "טיול QA")
    be.set_kv(tid, "expenses", json.dumps([{"description": "secret", "amount": 1, "category": "food"}]))
    if case != "unlinked":
        be.auto_link = {"trip_id": tid, "role": "editor", "status": case}
    dev = env.new_device("A")
    state = dev.goto(APP_PATH)
    page = dev.page
    assert state == "nc", "not-connected card not shown for %s device (state=%s)" % (case, state)
    uid = dev.uid()
    assert uid, "no anonymous identity created for the unlinked device"
    info = nc_card_info(page)
    assert info and not info.get("noRetry"), "card with title and a '%s' button not found: %r" % (NC_RETRY, info)
    assert NC_BODY in info["text"], "card body text missing"
    code = uid.replace("-", "")[:8]
    assert code.lower() in info["text"].lower(), "device code (first 8 hex of auth.uid() = %s) not on the card" % code
    cs = code_style(page, code)
    assert cs, "device code is not its own element"
    assert re.search(r"mono|courier|consolas|menlo", cs["font"], re.I), "code font is not monospace: %s" % cs["font"]
    assert cs["size"] >= 20, "code font is not large (%.1fpx < 20px)" % cs["size"]
    others = [b for b in info["buttons"] if NC_RETRY not in b["text"]]
    assert others, "no copy button on the card"
    assert not tabs_visible(page), "app tabs/panels visible behind the card: %r" % tabs_visible(page)
    assert not text_has(page, "secret")
    rows = [c for c in be.queries("trip_kv") if (c.get("result") or {}).get("data")]
    assert not rows, "trip_kv returned rows to a %s device" % case
    assert not be.queries("trip_kv"), "main app read trip_kv before a trip was known (§3 step 5)"
    trip_keys = [k for k in dev.ls() if k.startswith("pt:") and not k.startswith("pt:_device:")]
    assert not trip_keys, "trip-namespaced keys written before a trip is known: %r" % trip_keys
    # copy button puts the code on the clipboard
    page.locator("button, [role=button]", has_text=others[0]["text"]).first.click() if others[0]["text"] else \
        page.locator('[aria-label="%s"]' % others[0]["aria"]).first.click()
    page.wait_for_timeout(300)
    copied = page.evaluate("()=>window.__qaWatch.copied")
    assert any(code.lower() in c.lower() for c in copied), "copy button did not copy the code (copied=%r)" % copied
    # Claude links the device via SQL, then 'נסו שוב'
    if case == "unlinked":
        be.add_member(tid, uid, "editor", "active")
    else:
        be.set_member(tid, uid, status="active")
    page.locator("button, [role=button]", has_text=NC_RETRY).first.click()
    assert wait_ready(page) == "app", "'%s' did not re-run step 3 into the app after linking" % NC_RETRY
    wait_until(lambda: trip_name_is(page, "טיול QA"), page=page, msg="trip data after linking")


# =====================================================================================================
# 3 · Viewer write
# =====================================================================================================
def test_ac03_viewer_write_rejected_kept_locally_and_pending(env):
    """C3: viewer write rejected by RLS, local change kept, sync card shows the pending change."""
    tid, dev, state = boot_linked(env, role="viewer")
    page, be = dev.page, env.backend
    assert state == "app"
    ensure_user(page)
    before = be.kv(tid, "expenses")
    add_expense(page, "QA viewer expense", 33)
    page.wait_for_timeout(1500)
    ups = [c for c in be.queries("trip_kv", ("upsert", "insert", "update")) if c["device"] == "A"]
    assert ups, "the viewer write was never sent (spec: rejected by RLS, not blocked client-side)"
    assert any(((c.get("result") or {}).get("error") or {}).get("code") == "42501" for c in ups), "no RLS 42501 rejection seen"
    assert be.kv(tid, "expenses") == before, "server data changed by a viewer"
    ls = dev.ls()
    pre = "pt:%s:" % tid
    assert "QA viewer expense" in (ls.get(pre + "planner:expenses") or ""), "local copy discarded / not under %splanner:expenses" % pre
    q = queue_of(ls, pre)
    assert q and any(o.get("key") == "planner:expenses" for o in q), "rejected write not in the offline queue: %r" % q
    tab(page, "more")
    title = page.inner_text("#moreSyncTitle")
    assert re.search(r"\d+\s*שינויים ממתינים לסנכרון", title), "sync card does not show the pending change: %r" % title
    page.wait_for_timeout(1000)
    assert "QA viewer expense" in (dev.ls().get(pre + "planner:expenses") or ""), "local change lost after retry"


# =====================================================================================================
# 4 · Import tool
# =====================================================================================================
def _export_rows():
    v = {
        "planner:trip": json.dumps([{"day": 1, "title": "יום 'א' \"ציטוט\"", "notes": "שורה\nשנייה \\ backslash $$ dollar"}], ensure_ascii=False),
        "planner:expenses": json.dumps([{"description": "קפה", "amount": 12.5}], ensure_ascii=False),
        "planner:reservations": "[]",
        "planner:journal:1:entry:e1": json.dumps({"id": "e1", "text": "😀 emoji; drop table x;--"}, ensure_ascii=False),
        "planner:journal:1:entry:e2": json.dumps({"id": "e2", "text": "two"}),
        "planner:journal:2": json.dumps({"text": "", "photoIds": []}),
        "planner:checkins": "[]",
        "planner:timeline": "{}",
        "planner:budget": "23000",
        "planner:tripname": "מסע של חלומות",
        "planner:fxrates": '{"USD":3.7,"EUR":4,"VND":0.145}',
    }
    fam = {"trip": 1, "expenses": 1, "reservations": 1, "journal": 3, "checkins": 1, "timeline": 1, "budget": 1, "other": 2}
    return v, fam


def test_ac04_import_tool_counts_and_bytes(env, tmp_path):
    """C4 (tool part): scripts/import_planner_kv.py strips planner:, preserves values byte-for-byte, upserts,
    prints a count per key family. Or's real export + budget totals: manual / architect (DB)."""
    tool = SCRIPTS_DIR / "import_planner_kv.py"
    if not tool.is_file():
        pytest.skip("import tool not found at %s (SCRIPTS_DIR)" % tool)
    values, fam = _export_rows()
    js = tmp_path / "export.json"
    js.write_text(json.dumps([{"key": k, "value": val, "updated_at": "2026-10-01T10:00:00+00:00"} for k, val in values.items()],
                             ensure_ascii=False), encoding="utf-8")
    import csv
    cs = tmp_path / "export.csv"
    with cs.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["key", "value", "updated_at"])
        for k, val in values.items():
            w.writerow([k, val, "2026-10-01 10:00:00+00"])
    for src in (js, cs):
        trip = str(uuid.uuid4())  # one trip per run: the tool's own SQL may commit (begin; … commit;)
        work = tmp_path / ("run_" + src.suffix[1:])
        work.mkdir()
        r = subprocess.run(["python3", str(tool), str(src), "--trip", trip], cwd=work, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, "tool failed on %s: %s" % (src.name, r.stderr[-800:])
        # everything the tool produced: stdout, stderr, and any file it wrote (in its cwd or next to the input)
        produced = [p for p in work.rglob("*") if p.is_file() and not p.name.startswith("check") and p.name != "out.json"]
        files_txt = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in produced)
        sql = r.stdout + "\n" + "\n".join(p.read_text(encoding="utf-8") for p in work.rglob("*.sql"))
        allout = r.stdout + "\n" + r.stderr + "\n" + files_txt
        low = sql.lower()
        assert "trip_kv" in low and "insert" in low and "on conflict" in low and "do update" in low, "no INSERT … ON CONFLICT DO UPDATE into trip_kv"
        assert trip in sql, "trip uuid not in the SQL"
        assert not re.search(r"'planner:", sql), "keys still carry the planner: prefix"
        # spec §6: "Plus a verification query that prints the same counts from trip_kv" — not necessarily in the same
        # output: accepted from the import run itself, or from a verify option the tool documents in --help.
        def verif_stmts(text):
            stmts = re.split(r";", re.sub(r"(?m)^\s*--\s?", "", text))
            return [st for st in stmts if re.search(r"\b(select|with)\b", st, re.I) and re.search(r"\bcount\s*\(", st, re.I)
                    and re.search(r"\btrip_kv\b", st, re.I)]
        verif = verif_stmts(allout)
        how = "import run"
        if not verif:
            hlp = subprocess.run(["python3", str(tool), "--help"], cwd=work, capture_output=True, text=True, timeout=60).stdout
            opts = re.findall(r"(--[\w-]*verif[\w-]*)", hlp)
            for opt in dict.fromkeys(opts):
                vr = subprocess.run(["python3", str(tool), opt, "--trip", trip], cwd=work, capture_output=True, text=True, timeout=60)
                verif = verif_stmts(vr.stdout + "\n" + vr.stderr)
                if verif:
                    how = "option " + opt
                    break
        assert verif, "no verification query counting rows from trip_kv (import output, output files, or a --*verif* option)"
        assert trip in "\n".join(verif), "verification query (%s) is not scoped to the trip uuid" % how
        for family, n in fam.items():
            label = "journal" if family == "journal" else family
            lines = [ln for ln in allout.splitlines() if label in ln.lower() and re.search(r"(?<![\d.])%d(?![\d.])" % n, ln)]
            assert lines, "no count line for family %s (= %d) in the tool output (%s)" % (family, n, src.name)
        psql = os.environ.get("QA_PSQL")
        if psql:
            # the emitted SQL may contain its own transaction, so no wrapper: create the trip, run, read back, then
            # always delete the trip (cascades to trip_kv) — scratch DB only, never the project
            base = ["psql"] + shlex.split(psql) + ["-v", "ON_ERROR_STOP=1", "-q"]
            (work / "check_sql.sql").write_text(sql, encoding="utf-8")
            (work / "check_read.sql").write_text(
                "\\copy (select key, value from public.trip_kv where trip_id = '%s' order by key) to '%s/out.json' with (format csv)\n"
                % (trip, work), encoding="utf-8")
            try:
                subprocess.run(base + ["-c", "insert into public.trips (id, name) values ('%s', 'qa import')" % trip],
                               check=True, capture_output=True, text=True, timeout=60)
                pr = subprocess.run(base + ["-f", str(work / "check_sql.sql")], capture_output=True, text=True, timeout=120)
                assert pr.returncode == 0, "emitted SQL failed on the scratch DB: %s" % pr.stderr[-800:]
                subprocess.run(base + ["-f", str(work / "check_read.sql")], check=True, capture_output=True, text=True, timeout=60)
            finally:
                subprocess.run(base + ["-c", "delete from public.trips where id = '%s'" % trip], capture_output=True, text=True, timeout=60)
            with (work / "out.json").open(encoding="utf-8", newline="") as f:
                got = {row[0]: row[1] for row in csv.reader(f)}
            want = {k[len("planner:"):]: val for k, val in values.items()}
            assert got == want, "rows in trip_kv differ from the export (keys stripped, values byte-for-byte)"


# =====================================================================================================
# 5 · Parity with v2.15.2
# =====================================================================================================
_SEED_CACHE = {}


def baseline_seed_data():
    if "seed" not in _SEED_CACHE:
        r = node([str(JS_TOOLS / "extract.js"), str(BASELINE_DIR / "index.html"), "SEED_DATA"])
        src = json.loads(r.stdout)["found"]["SEED_DATA"][0]
        r2 = node(["-e", src + "\nprocess.stdout.write(JSON.stringify(SEED_DATA))"])
        _SEED_CACHE["seed"] = json.loads(r2.stdout)
    return copy.deepcopy(_SEED_CACHE["seed"])


def parity_dataset():
    trip = baseline_seed_data()
    trip[0]["title"] = "QA יום ראשון"
    trip[5]["activities"] = "QA פעילות מיוחדת"
    ts = 1789900000000
    expenses = [
        {"description": "QA ארוחה", "amount": 120, "category": "food", "dayIndex": 5, "by": "אור", "createdAt": ts},
        {"description": "QA מונית", "amount": 45.5, "category": "transport", "dayIndex": 5, "by": "שירי", "createdAt": ts + 1},
        {"description": "QA כרטיסים", "amount": 300, "category": "attractions", "dayIndex": None, "by": "אור", "createdAt": ts + 2,
         "originalAmount": 81, "originalCurrency": "USD"},
    ]
    res = [
        {"id": "rqa1", "type": "flight", "title": "טיסה QA", "provider": "אל על", "confirmationRef": "ABC123", "phone": "",
         "link": "", "address": "", "datetime": "2026-09-15 10:00", "linkedDayStart": 0, "notes": "", "cost": 0,
         "budgetCategory": "other", "statusOverride": "", "createdBy": "אור", "createdAt": ts, "updatedBy": "אור", "updatedAt": ts},
        {"id": "rqa2", "type": "activity", "title": "שייט QA", "provider": "", "confirmationRef": "", "phone": "", "link": "",
         "address": "", "datetime": "", "linkedDayStart": 5, "notes": "הערת QA", "cost": 300, "budgetCategory": "attractions",
         "statusOverride": "pending", "createdBy": "אור", "createdAt": ts, "updatedBy": "אור", "updatedAt": ts},
    ]
    shared = {
        "planner:trip": json.dumps(trip, ensure_ascii=False),
        "planner:expenses": json.dumps(expenses, ensure_ascii=False),
        "planner:budget": "23000",
        "planner:catbudgets": json.dumps({"lodging": 12000, "food": 4000}),
        "planner:reservations": json.dumps(res, ensure_ascii=False),
        "planner:journal:6:entry:eqa1": json.dumps({"id": "eqa1", "text": "רשומת יומן QA ראשונה", "by": "אור", "createdAt": ts}, ensure_ascii=False),
        "planner:journal:6:entry:eqa2": json.dumps({"id": "eqa2", "text": "רשומת יומן QA שנייה", "by": "שחר", "createdAt": ts + 5}, ensure_ascii=False),
        "planner:checkins": json.dumps([{"id": "ci_qa1", "d": "2026-09-20", "t": "2026-09-20T04:00:00.000Z", "lat": 21.0285,
                                         "lng": 105.8542, "acc": 15, "pid": "", "name": "QA קפה", "type": "קפה",
                                         "note": "הערה QA", "by": "אור"}], ensure_ascii=False),
        "planner:tripname": "טיול QA",
        "planner:fxrates": json.dumps({"USD": 3.6, "EUR": 3.9, "VND": 0.14}),
    }
    device = {"planner:documents": json.dumps([{"id": "dqa1", "name": "דרכון QA", "category": "passport",
                                                "categoryLabel": "דרכונים / ויזות", "src": "",
                                                "driveUrl": "https://drive.google.com/file/d/qa", "createdAt": ts, "by": "אור"}],
                                              ensure_ascii=False)}
    return shared, device


# Sync-status widgets legitimately differ: the baseline reference runs local-only ("מצב מקומי בלבד") while the new
# app is cloud-connected ("סנכרון בענן פעיל"). Everything else on the screens must match.
PARITY_EXCLUDE = ["#moreSyncCard", "#backupStatusCard", "#syncBadge", "#hminSync"]


def norm_text(t):
    t = re.sub(r"v?\d+\.\d+\.\d+(?:-[a-z]+\.\d+)?", "vX", t)
    t = re.sub(r"\b\d{1,2}:\d{2}\b", "HH:MM", t)
    t = re.sub(r"לפני \d+ (?:דקות|שעות|ימים)", "לפני N", t)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in t.splitlines()]
    return [ln for ln in lines if ln]


def screen_text(page, sel):
    return page.evaluate("""([sel, ex])=>{const el=document.querySelector(sel);if(!el)return '<missing '+sel+'>';
      let t=el.innerText;for(const x of ex){for(const e of el.querySelectorAll(x)){const s=e.innerText;if(s)t=t.replace(s,'')}}return t}""",
                         [sel, PARITY_EXCLUDE])


def capture_screens(dev):
    page = dev.page
    ensure_user(page, "אור")
    out = {}
    tab(page, "home")
    page.wait_for_timeout(2500)
    out["home"] = screen_text(page, "#homeV2")
    tab(page, "route")
    page.wait_for_timeout(2000)
    out["route"] = screen_text(page, "#route")
    tab(page, "budget")
    page.wait_for_timeout(800)
    out["budget"] = screen_text(page, "#budget")
    tab(page, "journal")
    page.wait_for_timeout(800)
    out["journal"] = screen_text(page, "#journal")
    tab(page, "more")
    click(page, "#reservationsBtn")
    page.wait_for_timeout(1200)
    out["reservations"] = screen_text(page, "#reservationsModal")
    close_modal(page, "reservationsModal")
    tab(page, "more")
    click(page, "#documentsBtn")
    page.wait_for_timeout(1200)
    out["vault"] = screen_text(page, "#documentsModal")
    close_modal(page, "documentsModal")
    click(page, "#globalFxBtn")
    page.wait_for_timeout(600)
    out["converter"] = screen_text(page, "#fxQuickModal")
    close_modal(page, "fxQuickModal")
    tab(page, "home")
    click(page, "#hv2CheckinBtn")
    try:
        page.wait_for_function("()=>typeof ciSheetState!=='undefined'&&ciSheetState&&!ciSheetState.loading", timeout=20000)
    except PWError:
        pass
    page.wait_for_timeout(500)
    out["check-in"] = screen_text(page, "#ciSheet")
    return out


PARITY_TIME = datetime.datetime(2026, 9, 20, 5, 0, 0, tzinfo=datetime.timezone.utc)


def test_ac05_parity_with_v2152_rendering(env):
    """C5: same visible text as v2.15.2 on the same data: home, route, budget, reservations, journal, check-in, vault,
    converter. Baseline runs local-only (its live Supabase block is cut off); the new app reads the same data from trip_kv."""
    shared, device = parity_dataset()
    # baseline, local-only
    b = env.new_device("BASE", fixed_time=PARITY_TIME)
    b.seed_ls({**shared, **device})
    b.goto(BASE_PATH)
    base = capture_screens(b)
    # new app, cloud, same data in trip_kv
    be = env.backend
    tid = be.add_trip("QA trip")
    for k, v in shared.items():
        be.set_kv(tid, k[len("planner:"):], v)
    be.auto_link = {"trip_id": tid, "role": "editor", "status": "active"}
    n = env.new_device("NEW", fixed_time=PARITY_TIME)
    n.seed_ls({"pt:%s:%s" % (tid, k): v for k, v in device.items()})
    assert n.goto(APP_PATH) == "app"
    new = capture_screens(n)
    diffs = []
    for screen in base:
        a, c = norm_text(base[screen]), norm_text(new.get(screen, ""))
        if a != c:
            d = list(difflib.unified_diff(a, c, "v2.15.2/" + screen, "new/" + screen, lineterm="", n=1))
            diffs.append("\n".join(d[:30]))
    assert not diffs, "rendering differs from v2.15.2 on %d screen(s):\n%s" % (len(diffs), "\n\n".join(diffs))


# =====================================================================================================
# 6 · Writes land with trip_id + updated_by
# =====================================================================================================
def test_ac06_writes_land_with_trip_id_and_updated_by(env):
    """C6 (client side): every write goes to trip_kv with the active trip_id, key without 'planner:', no
    updated_at/updated_by sent, upsert on (trip_id,key); rows end with updated_by = auth.uid(). DB side: qa_f02.sql #1–7."""
    tid, dev, state = boot_linked(env)
    page, be = dev.page, env.backend
    assert state == "app"
    other = be.add_trip("other trip")
    be.set_kv(other, "expenses", "[]")
    ensure_user(page)
    add_expense(page, "QA C6 expense", 42)
    add_journal(page, "QA C6 journal")
    page.wait_for_timeout(1500)
    uid = dev.uid()
    writes = [c for c in be.queries("trip_kv", ("upsert", "insert", "update", "delete")) if c["device"] == "A"]
    assert writes, "no writes reached trip_kv"
    problems = []
    for c in writes:
        req = c["msg"]["req"]
        vals = req.get("values")
        vals = vals if isinstance(vals, list) else ([vals] if vals else [])
        for v in vals:
            if str(v.get("trip_id")) != tid and req["action"] != "update":
                problems.append("write with trip_id=%r" % v.get("trip_id"))
            if "updated_at" in v or "updated_by" in v:
                problems.append("client sent updated_at/updated_by: %r" % sorted(v))
            if str(v.get("key", "")).startswith("planner:"):
                problems.append("db key keeps the planner: prefix: %s" % v.get("key"))
        if req["action"] in ("update", "delete") and not any(f["op"] == "eq" and f["col"] == "trip_id" and str(f["val"]) == tid
                                                              for f in req["filters"]):
            problems.append("%s without eq(trip_id, active trip)" % req["action"])
        if req["action"] == "upsert":
            oc = (req.get("options") or {}).get("onConflict")
            if oc and sorted(x.strip() for x in str(oc).split(",")) != ["key", "trip_id"]:
                problems.append("upsert onConflict=%r" % oc)
        err = (c.get("result") or {}).get("error")
        if err:
            problems.append("write failed: %r" % err)
    for c in [c for c in be.queries("trip_kv", "select") if c["device"] == "A"]:
        if not any(f["op"] == "eq" and f["col"] == "trip_id" and str(f["val"]) == tid for f in c["msg"]["req"]["filters"]):
            problems.append("select without eq(trip_id, active trip): %r" % c["msg"]["req"]["filters"])
    assert not problems, "\n".join(sorted(set(problems))[:20])
    exp = be.kv_row(tid, "expenses")
    assert exp and "QA C6 expense" in exp["value"] and exp["updated_by"] == uid, "expense row missing or updated_by != auth.uid()"
    jr = [r for (t, k), r in be.rows["trip_kv"].items() if t == tid and k.startswith("journal:") and "QA C6 journal" in (r["value"] or "")]
    assert jr and all(r["updated_by"] == uid for r in jr), "journal entry not in trip_kv under journal:* with updated_by = auth.uid()"
    assert be.kv(other, "expenses") == "[]", "another trip's row was touched"


# =====================================================================================================
# 7 · No calls to the live project
# =====================================================================================================
def test_ac07_zero_requests_to_live_project(env):
    """C7: zero network requests to kkitwcnkoxuhbcsabcdl (or any vietnam path) during a full session."""
    tid, dev, state = boot_linked(env, service_workers="allow")
    assert state == "app"
    full_session(dev, "C7")
    dev.page.reload(wait_until="domcontentloaded")
    wait_ready(dev.page)
    bad = [r for r in env.net if LIVE_REF in r["url"]]
    viet = [r for r in env.net if (urlparse(r["url"]).hostname in ("127.0.0.1",) or r["url"].endswith(".supabase.co") or
                                   ".supabase.co/" in r["url"]) and "vietnam" in urlparse(r["url"]).path.lower()]
    assert not bad, "requests to the live project: %r" % bad[:5]
    assert not viet, "requests to a vietnam path: %r" % [r["url"] for r in viet[:5]]
    assert not env.backend.violations, "supabase client calls aimed at the live project: %r" % env.backend.violations[:5]
    for d in env.devices:
        for e in d.fake_log():
            if e.get("kind") == "createClient":
                assert e["url"].rstrip("/") == PT_URL, "createClient with %s" % e["url"]


# =====================================================================================================
# 8 · Offline queue
# =====================================================================================================
def test_ac08_offline_changes_queued_under_trip_namespace_and_flushed(env):
    """C8: offline changes are queued under pt:<trip_id>: and flushed on reconnect."""
    tid, dev, state = boot_linked(env)
    page, be = dev.page, env.backend
    assert state == "app"
    ensure_user(page)
    pre = "pt:%s:" % tid
    before_keys = set(dev.ls())
    dev.offline(True)
    page.wait_for_timeout(300)
    add_expense(page, "QA offline 1", 15)
    ls = dev.ls()
    q = queue_of(ls, pre)
    assert q and any(o.get("key") == "planner:expenses" for o in q), "offline change not queued under %splanner:__queue__: %r" % (pre, q)
    assert "planner:__queue__" not in ls or "planner:__queue__" in before_keys, "queue written to the live app's key planner:__queue__"
    assert "QA offline 1" in (ls.get(pre + "planner:expenses") or "")
    assert "QA offline 1" not in (be.kv(tid, "expenses") or ""), "reached the server while offline (harness)"
    dev.offline(False)
    wait_until(lambda: "QA offline 1" in (be.kv(tid, "expenses") or ""), timeout=30, page=page, msg="flush to trip_kv")
    wait_until(lambda: not queue_of(dev.ls(), pre), timeout=10, page=page, msg="empty queue after flush")
    assert be.kv_row(tid, "expenses")["updated_by"] == dev.uid()


# =====================================================================================================
# 9 · Concurrency: same SYNC-MERGE outcome as v2.15.2
# =====================================================================================================
def smmerge3(base, local, remote):
    if remote is None:
        return local
    sid = lambda i: json.dumps(i, sort_keys=False, ensure_ascii=False)  # noqa: E731
    if base is None:
        ids = {sid(i) for i in remote}
        return remote + [i for i in local if sid(i) not in ids]
    L, B = {sid(i) for i in local}, {sid(i) for i in base}
    removed = {i for i in B if i not in L}
    out = [i for i in remote if sid(i) not in removed]
    have = {sid(i) for i in out}
    for i in local:
        if sid(i) not in B and sid(i) not in have:
            out.append(i)
            have.add(sid(i))
    return out


def run_concurrency(env, app):
    """A goes offline and adds an expense; B (online) deletes the shared one and adds its own; A reconnects."""
    be = env.backend
    base_item = {"description": "QA בסיס", "amount": 10, "category": "food", "dayIndex": None, "by": "אור", "createdAt": 1789900000000}
    if app == "base":
        be.legacy_ok = True
        be.set_planner("planner:expenses", json.dumps([base_item], ensure_ascii=False))
        path, get = BASE_PATH, (lambda: be.planner("planner:expenses"))
        a, b = env.new_device("A-" + app, legacy_fake=True), env.new_device("B-" + app, legacy_fake=True)
        pre = ""
    else:
        tid = be.add_trip("QA trip")
        be.set_kv(tid, "expenses", json.dumps([base_item], ensure_ascii=False))
        be.auto_link = {"trip_id": tid, "role": "editor", "status": "active"}
        path, get = APP_PATH, (lambda: be.kv(tid, "expenses"))
        a, b = env.new_device("A-" + app), env.new_device("B-" + app)
        pre = "pt:%s:" % tid
    for d in (a, b):
        assert d.goto(path) == "app"
        ensure_user(d.page)
        tab(d.page, "budget")
    a.offline(True)
    a.page.wait_for_timeout(300)
    add_expense(a.page, "QA A-offline", 21)
    add_expense(b.page, "QA B-online", 32)
    delete_expense(b.page, "QA בסיס")
    wait_until(lambda: "QA בסיס" not in (get() or "") and "QA B-online" in (get() or ""), timeout=20, page=b.page,
               msg="B's changes on the server (%s)" % app)
    a.offline(False)
    wait_until(lambda: "QA A-offline" in (get() or ""), timeout=40, page=a.page, msg="A's flush (%s)" % app)
    wait_until(lambda: not queue_of(a.ls(), pre), timeout=15, page=a.page, msg="A queue empty (%s)" % app)
    a.page.wait_for_timeout(1000)
    final = json.loads(get())
    return [i.get("description") for i in final]


def test_ac09_concurrent_edits_same_sync_merge_outcome(env):
    """C9: two devices editing concurrently -> the same SYNC-MERGE outcome as v2.15.2 (baseline run on an emulated
    planner_kv, new app on trip_kv; identical scripted scenario)."""
    base_result = run_concurrency(env, "base")
    new_result = run_concurrency(env, "new")
    expected = [i["description"] for i in smmerge3(
        [{"description": "QA בסיס"}], [{"description": "QA בסיס"}, {"description": "QA A-offline"}], [{"description": "QA B-online"}])]
    assert base_result == expected, "harness: baseline outcome %r != SYNC-MERGE expectation %r" % (base_result, expected)
    assert new_result == base_result, "SYNC-MERGE outcome differs: new %r vs v2.15.2 %r" % (new_result, base_result)


# =====================================================================================================
# 10 · Same-origin isolation
# =====================================================================================================
SNAP_JS = r"""
async () => {
  const ls = {}; for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); ls[k] = localStorage.getItem(k); }
  const dbs = (await indexedDB.databases()).map(d => d.name).sort();
  const idb = {};
  for (const name of dbs.filter(n => n === 'vtp-vault')) {
    idb[name] = await new Promise(res => {
      const rq = indexedDB.open(name);
      rq.onsuccess = async () => {
        const db = rq.result, out = {};
        for (const st of [...db.objectStoreNames]) {
          out[st] = await new Promise(r2 => {
            const tx = db.transaction(st, 'readonly'), s = tx.objectStore(st), acc = [];
            const c = s.openCursor();
            c.onsuccess = () => { const cur = c.result; if (cur) { acc.push([String(cur.key), JSON.stringify(cur.value)]); cur.continue(); } else r2(acc); };
            c.onerror = () => r2(['<error>']);
          });
        }
        db.close(); res({ version: db.version, stores: out });
      };
      rq.onerror = () => res('<open error>');
    });
  }
  const cacheNames = (await caches.keys()).sort();
  const cache = {};
  for (const n of cacheNames.filter(n => n.startsWith('vtp-'))) {
    const c = await caches.open(n), ent = {};
    for (const rq of await c.keys()) { const r = await c.match(rq); ent[rq.url] = r ? await r.clone().text() : null; }
    cache[n] = ent;
  }
  const regs = navigator.serviceWorker ? (await navigator.serviceWorker.getRegistrations()).map(r => ({scope: r.scope,
     script: (r.active || r.waiting || r.installing || {}).scriptURL || ''})) : [];
  return { ls, dbs, idb, cacheNames, cache, regs };
}
"""

SEED_LIVE_JS = r"""
async () => {
  localStorage.setItem('planner:qa_live', '{"live":true,"n":1}');
  localStorage.setItem('planner:expenses', '[{"description":"LIVE expense","amount":5}]');
  localStorage.setItem('planner:__queue__', '[{"type":"set","key":"planner:qa_live","value":"x"}]');
  localStorage.setItem('vietnam_planner_user_v1', 'אור');
  localStorage.setItem('vietnam_planner_qa_v1', 'live-bytes ✓');
  await new Promise((res, rej) => {
    const rq = indexedDB.open('vtp-vault', 1);
    rq.onupgradeneeded = () => { try { rq.result.createObjectStore('kv'); } catch (e) {} };
    rq.onsuccess = () => { const db = rq.result; const tx = db.transaction('kv', 'readwrite');
      tx.objectStore('kv').put([{ id: 'dlive', name: 'LIVE passport', src: 'data:application/pdf;base64,JVBERi0=' }], 'documents');
      tx.oncomplete = () => { db.close(); res(); }; tx.onerror = () => rej(tx.error); };
    rq.onerror = () => rej(rq.error);
  });
  const sh = await caches.open('vtp-shell-v1');
  await sh.put(location.origin + '/Vietnam_Travel_Planner/__qa_live_entry', new Response('live-shell-bytes'));
  const ic = await caches.open('vtp-icons-v1');
  await ic.put(location.origin + '/Vietnam_Travel_Planner/__qa_icon', new Response('live-icon-bytes'));
}
"""


def test_ac10_live_app_state_byte_identical_after_session(env):
    """C10: seeded live-app state (planner:* / vietnam_planner_* localStorage, vtp-vault IndexedDB, vtp-* caches, the
    live SW registration) is byte-identical after a full session in the new app; the new app only adds pt:* keys,
    pt-vault-* databases and pt-* caches, and its SW scope is its own directory."""
    tid = linked_setup(env)
    dev = env.new_device("A", service_workers="allow")
    page = dev.page
    assert dev.goto(BASE_PATH) == "app"  # the live app is installed on this phone (registers its SW, fills vtp caches)
    ensure_user(page)
    page.wait_for_timeout(2500)
    dev.raw_eval(SEED_LIVE_JS)
    snap1 = dev.raw_eval(SNAP_JS)
    assert "vtp-vault" in snap1["dbs"] and "vtp-shell-v1" in snap1["cacheNames"], "harness: live state not seeded"
    assert dev.goto(APP_PATH) == "app"
    full_session(dev, "C10")
    page.wait_for_timeout(1500)
    snap2 = dev.raw_eval(SNAP_JS)
    problems = []
    ls1 = snap1["ls"]
    ls2_live = {k: v for k, v in snap2["ls"].items() if not k.startswith("pt:") and k != AUTH_KEY}
    if ls2_live != ls1:
        for k in sorted(set(ls1) | set(ls2_live)):
            if ls1.get(k) != ls2_live.get(k):
                problems.append("localStorage %r: %r -> %r" % (k, (ls1.get(k) or "")[:60], (ls2_live.get(k) or "")[:60]))
    new_dbs = [d for d in snap2["dbs"] if d not in snap1["dbs"]]
    problems += ["new IndexedDB not pt-vault-*: %s" % d for d in new_dbs if not d.startswith("pt-vault-")]
    if snap2["idb"] != snap1["idb"]:
        problems.append("vtp-vault content changed")
    if [d for d in snap1["dbs"] if d not in snap2["dbs"]]:
        problems.append("a live IndexedDB was deleted")
    problems += ["new cache not pt-*: %s" % c for c in snap2["cacheNames"] if c not in snap1["cacheNames"] and not c.startswith("pt-")]
    if snap2["cache"] != snap1["cache"]:
        problems.append("vtp-* cache content changed: %r -> %r" % (sorted(snap1["cache"]), sorted(snap2["cache"])))
    base_regs = [r for r in snap1["regs"] if BASE_PATH in r["scope"]]
    if base_regs and not all(r in snap2["regs"] for r in base_regs):
        problems.append("the live app's service worker registration changed: %r -> %r" % (base_regs, snap2["regs"]))
    for r in snap2["regs"]:
        if r not in snap1["regs"] and not r["scope"].startswith(env.server.origin + APP_PATH):
            problems.append("new SW scope outside the app directory: %r" % r)
    assert not problems, "\n".join(problems)


# =====================================================================================================
# 11 · Two trips on one device
# =====================================================================================================
def test_ac11_two_trips_do_not_mix(env):
    """C11: two trip_ids on one device do not mix (localStorage and IndexedDB). Also §3: >=2 active rows -> most recent joined_at."""
    be = env.backend
    ta, tb = be.add_trip("A"), be.add_trip("B")
    be.set_kv(ta, "tripname", "טיול A")
    be.set_kv(tb, "tripname", "טיול B")
    dev = env.new_device("D")
    # first open: sign in (unlinked), then Claude links the device to both trips; B joined later
    assert dev.goto(APP_PATH) == "nc"
    uid = dev.uid()
    be.add_member(ta, uid, "editor", "active", joined_at="2026-10-01T10:00:00+00:00")
    be.add_member(tb, uid, "editor", "active", joined_at="2026-10-03T10:00:00+00:00")
    page = dev.page
    page.reload(wait_until="domcontentloaded")
    assert wait_ready(page) == "app"
    wait_until(lambda: trip_name_is(page, "טיול B"), page=page, msg="trip B (most recent joined_at) active")
    assert tb in (dev.ls().get("pt:_device:active_trip") or "")
    ensure_user(page)
    add_expense(page, "QA only-B", 7)
    page.evaluate("""async()=>{const d=await getDocuments();d.push({id:'dqaB',name:'B-doc',category:'other',categoryLabel:'אחר',src:'',
       driveUrl:'https://drive.google.com/file/d/b',createdAt:1,by:'אור'});await saveDocuments(d)}""")
    page.wait_for_timeout(800)
    # switch to trip A (B removed from the device)
    be.set_member(tb, uid, status="removed")
    page.reload(wait_until="domcontentloaded")
    assert wait_ready(page) == "app"
    wait_until(lambda: trip_name_is(page, "טיול A"), page=page, msg="trip A active after B removed")
    ensure_user(page)
    tab(page, "budget")
    assert not text_has(page, "QA only-B"), "trip B expense visible in trip A"
    docs = page.evaluate("async()=>(await getDocuments()).map(d=>d.id)")
    assert "dqaB" not in docs, "trip B vault document visible in trip A"
    ls = dev.ls()
    pa, pb = "pt:%s:" % ta, "pt:%s:" % tb
    leaks = [k for k, v in ls.items() if k.startswith(pa) and "only-B" in (v or "")]
    assert not leaks, "trip B data stored under trip A keys: %r" % leaks
    assert "QA only-B" in (ls.get(pb + "planner:expenses") or ""), "trip B local copy not under pt:<B>:"
    stray = [k for k in ls if not k.startswith("pt:") and k != AUTH_KEY]  # pt:_device:<auth key> is under pt: anyway
    assert not stray, "keys outside pt:*: %r" % stray
    dbs = dev.idb_names()
    assert "pt-vault-" + tb in dbs, "trip B vault DB pt-vault-<B> missing: %r" % dbs
    assert all(d.startswith("pt-vault-") for d in dbs), "unexpected IndexedDB names: %r" % dbs
    assert "QA only-B" not in (be.kv(ta, "expenses") or "") and "QA only-B" in (be.kv(tb, "expenses") or "")


# =====================================================================================================
# 12 · Chat
# =====================================================================================================
def test_ac12_chat_soon_zero_network(env):
    """C12: chat button shows 'בקרוב' (toast `הצ'אט יחזור בקרוב`), zero network calls; CHAT_FEATURE='soon', CHAT_ENDPOINT=''."""
    tid, dev, state = boot_linked(env)
    page, be = dev.page, env.backend
    assert state == "app"
    ensure_user(page)
    page.wait_for_timeout(1000)
    n_net, n_calls = len(env.net), len(be.calls)
    click(page, "#chatFab")
    try:
        page.wait_for_function("(t)=>document.body.innerText.includes(t)", arg=CHAT_TOAST, timeout=2500)
        toast = True
    except PWError:
        toast = False
    if page.locator("#chatInput").is_visible():
        page.fill("#chatInput", "שלום")
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)
    page.wait_for_timeout(800)
    new_net = [r for r in env.net[n_net:] if r.get("action") != "same-origin" or "functions" in r["url"]]
    new_calls = [c for c in be.calls[n_calls:] if c["op"] in ("functions.invoke", "rpc")]
    chatty = [r for r in env.net if re.search(r"functions/v1|chat-assistant|anthropic", r["url"])]
    consts = page.evaluate("()=>({f:typeof CHAT_FEATURE!=='undefined'?CHAT_FEATURE:null,e:typeof CHAT_ENDPOINT!=='undefined'?CHAT_ENDPOINT:null})")
    assert toast, "chat button did not show the toast %r" % CHAT_TOAST
    assert not new_net and not new_calls and not chatty, "chat made network calls: %r" % (new_net + new_calls + chatty)[:5]
    assert consts["f"] == "soon", "CHAT_FEATURE=%r" % consts["f"]
    assert consts["e"] == "", "CHAT_ENDPOINT must be '' (got %r)" % consts["e"]


# =====================================================================================================
# 13 · local-only flag
# =====================================================================================================
def test_ac13_local_only_flag_no_supabase(env):
    """C13: STORAGE_BACKEND='local-only' works fully on the device with zero calls to Supabase (served copy of index.html
    with the flag flipped). Loading the supabase-js file itself is not counted as a call to Supabase."""
    src = read_app("index.html")
    pat = re.compile(r"""(const\s+STORAGE_BACKEND\s*=\s*)(['"])cloud\2""")
    assert pat.search(src), "top-level const STORAGE_BACKEND='cloud' not found in index.html"
    env.server.overrides[APP_PATH] = pat.sub(r"\1'local-only'", src, count=1).encode("utf-8")
    dev = env.new_device("L")
    state = dev.goto(APP_PATH)
    page, be = dev.page, env.backend
    assert state == "app", "local-only did not start the app (state=%s)" % state
    ensure_user(page)
    add_expense(page, "QA local-only", 9)
    add_journal(page, "QA local journal")
    tab(page, "more")
    assert "מצב מקומי בלבד" in page.inner_text("#moreSyncTitle")
    page.reload(wait_until="domcontentloaded")
    assert wait_ready(page) == "app"
    ensure_user(page)
    tab(page, "budget")
    assert text_has(page, "QA local-only"), "local-only data not kept across reload"
    ls = dev.ls()
    assert "QA local-only" in (ls.get("pt:local:planner:expenses") or ""), "local-only namespace is not pt:local:"
    stray = [k for k in ls if not k.startswith("pt:")]
    assert not stray, "keys outside pt:* in local-only: %r" % stray
    supa = [r for r in env.net if ".supabase.co" in r["url"]]
    created = [e for e in dev.fake_log() if e.get("kind") not in ("fake.loaded",)]
    assert not supa and not be.calls and not created, "Supabase activity in local-only: net=%r calls=%d client=%r" % (
        supa[:3], len(be.calls), created[:3])


# =====================================================================================================
# 14 · 390px
# =====================================================================================================
def test_ac14_390px_rtl_touch_targets(env):
    """C14 (automatable part): no horizontal scroll at 390px, RTL intact, touch >=44px for the new card.
    Map on the new path (Google key referrer) and the real phone: MANUAL.md."""
    be = env.backend
    tid = be.add_trip("QA trip")
    dev = env.new_device("A")
    assert dev.goto(APP_PATH) == "nc"
    page = dev.page
    info = nc_card_info(page)
    assert info and not info.get("noRetry"), "card not found"
    problems = []
    if info["scrollW"] > info["innerW"] + 1:
        problems.append("card screen scrolls horizontally (%d > %d)" % (info["scrollW"], info["innerW"]))
    if info["htmlDir"] != "rtl" or info["dir"] != "rtl":
        problems.append("not RTL (html dir=%r, card direction=%r)" % (info["htmlDir"], info["dir"]))
    if info["rect"]["l"] < -0.5 or info["rect"]["r"] > VIEWPORT["width"] + 0.5:
        problems.append("card wider than the viewport: %r" % info["rect"])
    for b in info["buttons"]:
        if b["w"] < 44 or b["h"] < 44:
            problems.append("card button %r is %.0fx%.0f (< 44px)" % (b["text"] or b["aria"], b["w"], b["h"]))
    be.add_member(tid, dev.uid(), "editor", "active")
    page.locator("button, [role=button]", has_text=NC_RETRY).first.click()
    assert wait_ready(page) == "app"
    ensure_user(page)
    for p in ("home", "route", "journal", "budget", "more"):
        tab(page, p)
        sw = page.evaluate("()=>[document.scrollingElement.scrollWidth,innerWidth,document.documentElement.getAttribute('dir')]")
        if sw[0] > sw[1] + 1:
            problems.append("%s scrolls horizontally (%d > %d)" % (p, sw[0], sw[1]))
        if sw[2] != "rtl":
            problems.append("%s: html dir=%r" % (p, sw[2]))
    assert not problems, "\n".join(problems)


# =====================================================================================================
# 15 · Static checks
# =====================================================================================================
def _extract(path, names):
    r = node([str(JS_TOOLS / "extract.js"), str(path)] + names)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


STATIC_CHECKS = ["protected:" + n for n in PROTECTED] + [
    "app_version", "title_version", "change_comments_index", "change_comments_sw", "node_check_inline_scripts",
    "node_check_sw", "no_localstorage_clear", "no_live_project_ref", "sw_cache_names_pt", "sw_never_deletes_foreign_caches",
    "manifest_id", "manifest_visible_fields_unchanged", "storage_backend_default_cloud"]


@pytest.mark.parametrize("check", STATIC_CHECKS)
def test_ac15_static(check, tmp_path):
    """C15: protected functions byte-identical to v2.15.2; version + CHANGE comments; node --check on every script block;
    no localStorage.clear( / live ref; sw caches pt-* and never deletes foreign caches; manifest id plan-travel."""
    idx = APP_DIR / "index.html"
    html = idx.read_text(encoding="utf-8")
    if check.startswith("protected:"):
        name = check.split(":", 1)[1]
        a = _extract(BASELINE_DIR / "index.html", [name])["found"][name]
        b = _extract(idx, [name])["found"][name]
        assert len(a) == 1, "harness: %s found %d times in baseline" % (name, len(a))
        assert b, "%s not found as a top-level declaration in the new index.html" % name
        assert a[0] in b, "%s is not byte-identical to v2.15.2" % name
    elif check == "app_version":
        assert re.search(r"""const\s+APP_VERSION\s*=\s*['"]%s['"]""" % re.escape(VERSION), html), "APP_VERSION != %s" % VERSION
    elif check == "title_version":
        m = re.search(r"<title>(.*?)</title>", html, re.S)
        assert m and VERSION in m.group(1), "<title> does not carry %s: %r" % (VERSION, m and m.group(1))
    elif check == "change_comments_index":
        assert re.search(r"CHANGE 2026-10-04 F02", html), "no 'CHANGE 2026-10-04 F02…' comments in index.html"
    elif check == "change_comments_sw":
        assert re.search(r"CHANGE 2026-10-04 F02", read_app("sw.js")), "no 'CHANGE 2026-10-04 F02…' comment in sw.js"
    elif check == "node_check_inline_scripts":
        d = tmp_path / "scripts"
        d.mkdir()
        r = node([str(JS_TOOLS / "extract.js"), str(idx)], env={**os.environ, "DUMP_DIR": str(d)})
        assert r.returncode == 0, r.stderr
        files = sorted(list(d.glob("*.js")) + list(d.glob("*.mjs")))
        assert files, "no inline scripts found"
        bad = []
        for f in files:
            c = node(["--check", str(f)])
            if c.returncode != 0:
                bad.append("%s: %s" % (f.name, c.stderr.strip().splitlines()[-1] if c.stderr.strip() else "?"))
        assert not bad, "node --check failed:\n" + "\n".join(bad)
    elif check == "node_check_sw":
        c = node(["--check", str(APP_DIR / "sw.js")])
        assert c.returncode == 0, c.stderr
    elif check == "no_localstorage_clear":
        hits = [str(p) for p in APP_DIR.rglob("*") if p.is_file() and p.suffix in (".html", ".js", ".json", ".webmanifest")
                and re.search(r"localStorage\s*\.\s*clear\s*\(", p.read_text(encoding="utf-8", errors="ignore"))]
        assert not hits, "localStorage.clear( in %r" % hits
    elif check == "no_live_project_ref":
        hits = [str(p) for p in APP_DIR.rglob("*") if p.is_file() and LIVE_REF.encode() in p.read_bytes()]
        assert not hits, "%s appears in %r" % (LIVE_REF, hits)
    elif check in ("sw_cache_names_pt", "sw_never_deletes_foreign_caches"):
        r = node([str(JS_TOOLS / "sw_harness.js"), str(APP_DIR / "sw.js"), "http://127.0.0.1:1" + APP_PATH,
                  "vtp-shell-v1", "vtp-icons-v1", "other-app-cache", "pt-shell-v0"])
        res = json.loads(r.stdout)
        assert not res["errors"], "sw.js errors in the sandbox: %r" % res["errors"]
        if check == "sw_cache_names_pt":
            assert res["opened"], "sw.js opened no cache"
            bad = [c for c in res["opened"] if not c.startswith("pt-")]
            assert not bad, "sw.js uses caches not starting with pt-: %r" % bad
            names = re.findall(r"""['"]((?:vtp|pt)-[a-z]+-v\d+)['"]""", read_app("sw.js"))
            assert names and all(n.startswith("pt-") for n in names), "cache name literals: %r" % names
        else:
            bad = [c for c in res["deleted"] if not c.startswith("pt-")]
            assert not bad, "sw.js deleted foreign caches: %r" % bad
            for c in ("vtp-shell-v1", "vtp-icons-v1", "other-app-cache"):
                assert c in res["remaining"], "%s gone after install/activate" % c
    elif check == "manifest_id":
        m = json.loads(read_app("manifest.json"))
        assert m.get("id") == "plan-travel", "manifest id=%r" % m.get("id")
    elif check == "manifest_visible_fields_unchanged":
        m = json.loads(read_app("manifest.json"))
        b = json.loads((BASELINE_DIR / "manifest.json").read_text(encoding="utf-8"))
        for k in ("name", "short_name", "icons"):
            assert m.get(k) == b.get(k), "manifest %s changed (visible change)" % k
    elif check == "storage_backend_default_cloud":
        assert re.search(r"""const\s+STORAGE_BACKEND\s*=\s*['"]cloud['"]""", html), "STORAGE_BACKEND default 'cloud' not found"


# =====================================================================================================
# 16 · advisors / anon
# =====================================================================================================
@pytest.mark.skip(reason="C16 needs the real database: covered by sql/qa_f02.sql (#40-44, anon reads 0 rows) and the "
                         "Supabase advisors run by the architect via the Connector")
def test_ac16_advisors_clean_and_anon_reads_zero():
    pass


# =====================================================================================================
# Extra spec checks (§2/§3/§4) that are not a numbered criterion
# =====================================================================================================
def test_spec_s4_storage_interface_mapping_escaping_timeout(env):
    """§4: key mapping planner:X <-> X, other keys as-is, list() escapes %/_ and maps keys back, throw-on-error, 8 s timeout."""
    tid, dev, state = boot_linked(env)
    page, be = dev.page, env.backend
    assert state == "app"
    be.set_kv(tid, "qa50%off", "pct")
    be.set_kv(tid, "qa50xoff", "nopct")
    be.set_kv(tid, "qa_a", "under")
    be.set_kv(tid, "qaZa", "nounder")
    r = page.evaluate("""async()=>{const s=window.storage,o={};try{
      await s.set('planner:qa_x','v1');await s.set('other_key','w1');
      o.get=await s.get('planner:qa_x');o.missing=await s.get('planner:qa_nothing');
      o.pct=(await s.list('planner:qa50%')).map(r=>r.key).sort();
      o.und=(await s.list('planner:qa_')).map(r=>r.key).sort();
      await s.delete('planner:qa_x');o.afterDel=await s.get('planner:qa_x');return o}
      catch(e){return {thrown:String(e&&(e.message||e.code)||e),partial:o}}}""")
    assert "thrown" not in r, "window.storage call failed against an editor's own trip: %r" % r
    assert be.kv(tid, "qa_x") is None and be.kv(tid, "other_key") == "w1", "other key not stored as-is / delete failed"
    assert any(c["msg"]["req"]["action"] == "upsert" and (c["msg"]["req"]["values"] or {}).get("key") == "qa_x"
               for c in be.queries("trip_kv")), "planner:qa_x not stored as qa_x"
    assert r["get"] == {"value": "v1"}, "get() shape: %r" % r["get"]
    assert r["missing"] is None
    assert r["pct"] == ["planner:qa50%off"], "list('planner:qa50%%') not escaped / not mapped back: %r" % r["pct"]
    assert r["und"] == ["planner:qa_a", "planner:qa_x"] or r["und"] == ["planner:qa_a"], "list('planner:qa_') not escaped: %r" % r["und"]
    assert r["afterDel"] is None
    # throw-on-error
    be.mode = "down"
    thrown = page.evaluate("async()=>{try{await window.storage.set('planner:qa_y','z');return false}catch(e){return true}}")
    assert thrown, "set() did not throw on a backend error"
    be.mode = "hang"
    t = page.evaluate("async()=>{const t0=performance.now();try{await window.storage.get('planner:qa_x')}catch(e){};return performance.now()-t0}")
    be.mode = "up"
    assert 7000 <= t <= 10500, "get() did not time out after ~8 s (took %.0f ms)" % t


def test_spec_s3_offline_start_and_no_cache(env):
    """§3 steps 2 and 4: backend unreachable + cached trip -> start on the cached trip; unreachable + no cache -> not connected."""
    tid, dev, state = boot_linked(env)
    assert state == "app"
    page = dev.page
    env.backend.mode = "down"
    page.reload(wait_until="domcontentloaded")
    assert wait_ready(page) == "app", "did not start on the cached trip with the backend unreachable"
    assert not page.evaluate("()=>window.__qaWatch.sawNotConnected")
    fresh = env.new_device("FRESH")
    assert fresh.goto(APP_PATH) == "nc", "fresh device with the backend unreachable must show the not-connected state"


def test_spec_s2_list_fallback_sees_only_trip_keys(env):
    """§2: the SS.list localStorage fallback only sees pt:<trip_id>: keys (prefix stripped), never the live app's planner:* keys."""
    tid = linked_setup(env)
    dev = env.new_device("A")
    dev.seed_ls({"planner:journal:6:entry:LIVE": '{"id":"LIVE","text":"live"}',
                                    "pt:%s:planner:journal:6:entry:MINE" % tid: '{"id":"MINE","text":"mine"}'})
    assert dev.goto(APP_PATH) == "app"
    env.backend.mode = "down"
    keys = dev.page.evaluate("async()=>(await SS.list('planner:journal:6:entry:')).map(r=>r.key)")
    env.backend.mode = "up"
    assert "planner:journal:6:entry:LIVE" not in keys, "SS.list fallback sees the live app's keys: %r" % keys
    assert keys == ["planner:journal:6:entry:MINE"], "SS.list fallback keys: %r" % keys
