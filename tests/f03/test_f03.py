"""Plan_Travel F03 QA · Playwright black-box suite · version 1.0.0
CHANGE 2026-10-05 F03-QA: first version (no previous version).

Black-box acceptance tests for docs/F03_spec.md §5 (super-admin invites a manager), written from the spec and the
approved mockup artboards F03_* only. The F03 implementation (app/index.html changes, supabase/functions/,
migrations 0008/0009, F03 dev notes) was NOT read. The alpha.2 app (git ref ALPHA2_REF) is the "before" reference.

Layout (reuses the F02 harness, imported as a module from ../f02/tests/test_f02.py — nothing is copied from it):
- f02.Server serves APP_DIR under /Plan_Travel/ (http://127.0.0.1:<port>), the alpha.2 index.html is served under
  /Alpha2/ (criterion 16 parity) and flag-flipped copies are served through Server.overrides.
- The supabase-js CDN request is fulfilled with tests/f03/fake_supabase.js (F02 fake + updateUser/rpc/functions).
- F03Backend (below) extends f02.FakeBackend with: platform_admins (own-row RLS), trip_countries, auth.updateUser
  with scripted outcomes, rpc('admin_trip_summary') computed from the fake tables (or not_platform_admin), and a fake
  `invite-manager` Edge Function implementing spec §3 in memory (SHA-256 hash storage, 7-day expiry, resend/revoke/
  check, invite_exists, email_failed). It is reachable both through a route-intercepted
  POST <PT_URL>/functions/v1/invite-manager (spec §4 wiring) and through supabase.functions.invoke('invite-manager').
- Every other external request is logged and aborted. Browser clock and fake clock are pinned to FIXED_NOW.

Selectors: UI is located by visible Hebrew text from the spec/mockups. Everything that may need adjusting after the
dev notes is in the SEL / TXT / SUMMARY_SHAPE blocks right below.

Env: APP_DIR (default /home/claude/plan_travel/app), REPO_DIR (default: two levels up), ALPHA2_REF (default 9b8151b),
QA_HEADFUL=1 to watch.
"""
import base64
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest

HERE = Path(__file__).resolve().parent
REPO_DIR = Path(os.environ.get("REPO_DIR", str(HERE.parent.parent)))
os.environ.setdefault("APP_DIR", str(REPO_DIR / "app"))
os.environ.setdefault("PW_EXPERIMENTAL_SERVICE_WORKER_NETWORK_EVENTS", "1")

_spec = importlib.util.spec_from_file_location("f02_harness", str(REPO_DIR / "tests" / "f02" / "tests" / "test_f02.py"))
f02 = importlib.util.module_from_spec(_spec)
sys.modules["f02_harness"] = f02
_spec.loader.exec_module(f02)

from playwright.sync_api import Error as PWError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

FAKE_JS = (HERE / "fake_supabase.js").read_text(encoding="utf-8")
APP_DIR = Path(os.environ["APP_DIR"])
APP_PATH = f02.APP_PATH
ALPHA2_PATH = "/Alpha2/"
ALPHA2_REF = os.environ.get("ALPHA2_REF", "9b8151b")
PT_URL, PT_KEY = f02.PT_URL, f02.PT_KEY
FN_URL = PT_URL + "/functions/v1/invite-manager"
PROD_APP_URL = "https://orsela.github.io/Plan_Travel/app/"
VERSION = "3.0.0-alpha.3"
PROTECTED = f02.PROTECTED
VIEWPORT = f02.VIEWPORT
HEADFUL = os.environ.get("QA_HEADFUL") == "1"

# Browser clock and fake clock. 09:00 in Jerusalem, so "today" is 05/10/2026 in both UTC and local time.
FIXED_NOW = dt.datetime(2026, 10, 5, 6, 0, 0, tzinfo=dt.timezone.utc)
DAY = dt.timedelta(days=1)

# =====================================================================================================
# Selectors and texts — ADJUST HERE after reading the F03 dev notes (everything else uses these names)
# =====================================================================================================
SEL = {
    "admin_panel": "#admin",                       # spec §1.2 names the panel id
    "email_input": 'input[type="email"]',          # both sheets (mockups: type=email)
    "text_input": 'input:not([type]), input[type="text"]',  # draft-name input in the invite sheet
    "back_aria": "חזרה",                            # admin back button: aria-label contains this (mockup: "חזרה לטיול")
    "card_menu_aria": "פעולות",                     # pending-card ⋯ button: aria-label contains this (mockup)
    "card_menu_texts": ["⋯", "…", "...", "⋮"],
    "trip_chrome": [".tabs", "#chatFab", "#globalFxBtn"],
}
TXT = {
    # "עוד" → "החשבון שלי"
    "account_section": "החשבון שלי",
    "link_row": "קישור מייל לחשבון",
    "linked_row": "מייל מקושר",
    "linked_badge": "מקושר",
    "admin_row": "ניהול מערכת",
    "admin_badge": "סופר-אדמין",
    "link_send": "שליחת מייל אימות",
    "link_success": "שלחנו מייל ל-{email}. פתחו את הקישור בטלפון הזה.",
    # admin screen
    "invite_btn": "הזמנת מנהל לקבוצה חדשה",
    "f_all": "הכל", "f_pending": "ממתינות", "f_active": "פעילות", "f_ended": "הסתיימו",
    "revoked_toggle": "הזמנות שבוטלו",
    "privacy": "מוצגים רק שם, מנהלים, יעד, תאריכים ומספר חברים",
    "no_name": "קבוצה ללא שם",
    "sent_ago": "נשלחה לפני",
    "valid_for": "בתוקף עוד {n} ימים",
    "expired_on": "פג ב-{d}",
    "card_resend": "שליחה מחדש",
    "card_remove": "הסרה מהרשימה",
    "managers": "מנהלים:",
    "active": "פעילה", "ended": "הסתיימה",
    "members": "{n} חברים",
    "offline": "אין חיבור — הרשימה תתעדכן כשהרשת תחזור",
    # invite sheet
    "sheet_submit": "שליחת הזמנה במייל",
    "resend_existing": "לשלוח שוב את ההזמנה הקיימת",
    "toast_sent": "ההזמנה נשלחה ל-{email}",
    # actions sheet
    "a_first": "נשלחה לראשונה", "a_last": "שליחה אחרונה", "a_once": "פעם אחת", "a_times": "{n} פעמים",
    "a_until": "בתוקף עד", "a_opened": "הקישור נפתח", "a_not_yet": "עדיין לא",
    "a_resend": "שליחה חוזרת", "a_revoke": "ביטול ההזמנה",
    "revoke_confirm": ["כן, לבטל", "ביטול ההזמנה", "לבטל", "אישור", "כן"],   # custom confirm dialog candidates
    # landing
    "land_ok": "ההזמנה שלך אומתה",
    "land_btn": "התחלת הקמה · בקרוב",
    "land_steps": ["התחברות עם המייל הזה", "שם הטיול, תאריכים ושפה", "מדינות ומטבעות", "קישור הצטרפות לשליחה בווטסאפ"],
    "land_bad": "הקישור כבר לא בתוקף",
    "land_net": "אין חיבור. נסו שוב",
    "land_retry": "נסו שוב",
}
COUNTRY_HE = {"VN": "ויאטנם", "TH": "תאילנד", "PT": "פורטוגל"}   # mockup spellings; others not asserted
FLAG = {c: "".join(chr(0x1F1E6 + ord(ch) - 65) for ch in c) for c in ("VN", "TH", "PT", "JP", "KR", "XK")}


def SUMMARY_SHAPE(kind, d):
    """Row shape of admin_trip_summary() (setof jsonb) — ASSUMED field names; adjust after reading the dev notes.
    kind 'trip': d has id,name,start_date,end_date,status(active|ended),managers[list],countries[list],member_count.
    kind 'invite': d has id,email,draft_name,created_at,last_sent_at,send_count,opened_at,expires_at,
    status(pending|expired|revoked)."""
    return {"kind": kind, **d}


# Error codes of invite-manager (spec §3: "fixed error codes"); response body shape ASSUMED {error:<code>, ...}.
def FN_ERROR(code, **extra):
    return {"error": code, **extra}


# =====================================================================================================
# Fake backend
# =====================================================================================================
f02.TABLES.setdefault("platform_admins", {"cols": ["user_id", "created_at"], "pk": ("user_id",), "uuid": {"user_id"}})
f02.TABLES.setdefault("trip_countries", {"cols": ["trip_id", "country_code", "sort"], "pk": ("trip_id", "country_code"),
                                         "uuid": {"trip_id"}})
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
AUTH_ERRORS = {
    "invalid": {"name": "AuthApiError", "message": "Unable to validate email address: invalid format", "status": 400,
                "code": "validation_failed"},
    "rate": {"name": "AuthApiError", "message": "email rate limit exceeded", "status": 429, "code": "over_email_send_rate_limit"},
    "exists": {"name": "AuthApiError", "message": "A user with this email address has already been registered", "status": 422,
               "code": "email_exists"},
}


def iso(t):
    return t.astimezone(dt.timezone.utc).isoformat()


def parse(s):
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None


def sha256_hex(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def new_token():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


class F03Backend(f02.FakeBackend):
    def __init__(self):
        super().__init__()
        self.now = FIXED_NOW
        self._t0 = FIXED_NOW - dt.timedelta(days=30)
        self.auto_admin = False
        self.auto_email = None
        self.update_outcome = "ok"           # ok | invalid | rate | exists
        self.update_calls = []
        self.invites = {}                     # id -> row (manager_invites + F03 columns)
        self.emails = []                      # sent invite emails: {to, subject, link, token, invite_id}
        self.email_fail = False
        self.fn_down = False                  # invite-manager unreachable (network error)
        self.fn_calls = []                    # {via, device, action, body, auth, apikey, status, resp}
        self.rpc_calls = []
        self.summary_override = None          # list -> returned as-is by admin_trip_summary

    # ---------- setup helpers ----------
    def make_admin(self, uid):
        self.rows["platform_admins"][(uid,)] = {"user_id": uid, "created_at": self.ts()}

    def is_admin(self, uid):
        return bool(uid) and (uid,) in self.rows["platform_admins"]

    def add_trip_full(self, name, start, end, status="active", countries=(), managers=(), others=(), extra=()):
        """managers/others: display names of active members; extra: (name, role, status) tuples."""
        tid = self.add_trip(name)
        self.rows["trips"][(tid,)].update({"name": name, "start_date": start, "end_date": end, "status": status})
        for i, c in enumerate(countries):
            self.rows["trip_countries"][(tid, c)] = {"trip_id": tid, "country_code": c, "sort": i}
        for n in managers:
            self.add_member(tid, str(uuid.uuid4()), "manager", "active", n)
        for n in others:
            self.add_member(tid, str(uuid.uuid4()), "editor", "active", n)
        for n, role, st in extra:
            self.add_member(tid, str(uuid.uuid4()), role, st, n)
        return tid

    def add_invite(self, email, draft_name=None, sent_days_ago=2, expires_in_days=5, revoked=False, used=False,
                   opened_days_ago=None, send_count=1, first_sent_days_ago=None, inviter="אור"):
        token = new_token()
        iid = str(uuid.uuid4())
        first = self.now - DAY * (first_sent_days_ago if first_sent_days_ago is not None else sent_days_ago)
        self.invites[iid] = {
            "id": iid, "email": email, "token_hash": sha256_hex(token), "draft_name": draft_name, "invited_by": None,
            "expires_at": iso(self.now + DAY * expires_in_days), "used_at": iso(self.now - DAY) if used else None,
            "revoked_at": iso(self.now - DAY) if revoked else None, "trip_id": None, "created_at": iso(first),
            "send_count": send_count, "last_sent_at": iso(self.now - DAY * sent_days_ago),
            "opened_at": iso(self.now - DAY * opened_days_ago) if opened_days_ago is not None else None,
            "inviter_name": inviter}
        return iid, token

    def confirm_email(self, uid, email):
        u = self.users[uid]
        u.update({"email": email, "new_email": None, "email_confirmed_at": iso(self.now), "is_anonymous": False})

    # ---------- binding entry ----------
    def sign_in(self):
        res = super().sign_in()
        s = res.get("session")
        if s:
            uid = s["user"]["id"]
            if self.auto_email:
                self.users[uid].update({"email": self.auto_email, "is_anonymous": False})
            if self.auto_admin:
                self.make_admin(uid)
            s["user"] = copy.deepcopy(self.users[uid])
        return res

    def handle(self, device, msg):
        op = (msg or {}).get("op")
        if op not in ("auth.updateUser", "rpc", "functions.invoke"):
            return super().handle(device, msg)
        with self.lock:
            url = str(msg.get("url") or "")
            entry = {"device": device, "t": time.time(), "op": op, "page": msg.get("page"), "online": msg.get("online"),
                     "url": url, "msg": copy.deepcopy(msg)}
            self.calls.append(entry)
            if f02.LIVE_REF in url:
                self.violations.append({"device": device, "where": op, "url": url})
            offline = msg.get("online") is False or self.mode == "down" or (op == "functions.invoke" and self.fn_down)
            if offline:
                entry["result"] = {"offline": True}
                if op == "auth.updateUser":
                    return {"error": {"name": "AuthRetryableFetchError", "message": "Failed to fetch", "status": 0}}
                if op == "functions.invoke":
                    return {"offline": True}
                return {"error": dict(f02.FETCH_ERR), "status": 0, "data": None}
            if self.mode == "hang":
                return {"hang": True}
            if url.rstrip("/") != PT_URL or msg.get("key") != PT_KEY:
                return {"error": {"code": "FAKE_WRONG_PROJECT", "message": "fake: wrong project/key"}, "status": 0}
            if op == "auth.updateUser":
                res = self.update_user(msg.get("token"), msg.get("attrs") or {}, msg.get("options") or {})
            elif op == "rpc":
                res = self.rpc(msg.get("token"), msg.get("fn"), msg.get("args") or {})
            else:
                bearer = msg.get("token") or msg.get("key")
                status, body = self.invite_manager(device, "invoke", bearer, msg.get("key"), msg.get("body") or {})
                res = {"httpStatus": status, "json": body}
            entry["result"] = copy.deepcopy(res)
            return res

    def query(self, token, req, legacy):
        if str(req.get("table")) != "platform_admins":
            return super().query(token, req, legacy)
        if token and token not in self.tokens:
            return f02._err("PGRST301", "JWT is invalid", 401)
        uid = self.tokens.get(token) if token else None
        if uid is None:
            return f02._err("42501", "permission denied for table platform_admins", 401)
        if req.get("action") != "select":
            return f02._err("42501", "permission denied for table platform_admins", 403)
        return self.exec("platform_admins", req, uid, lambda r: r["user_id"] == uid, None)

    # ---------- auth.updateUser ----------
    def update_user(self, token, attrs, options):
        uid = self.tokens.get(token)
        self.update_calls.append({"uid": uid, "attrs": attrs, "options": options})
        if not uid:
            return {"error": {"name": "AuthApiError", "message": "invalid JWT", "status": 401}}
        if self.update_outcome != "ok":
            return {"error": dict(AUTH_ERRORS[self.update_outcome])}
        email = str(attrs.get("email") or "")
        if not EMAIL_RE.match(email):
            return {"error": dict(AUTH_ERRORS["invalid"])}
        u = self.users[uid]
        u.update({"new_email": email, "email_change_sent_at": iso(self.now)})
        return {"user": copy.deepcopy(u)}

    # ---------- rpc ----------
    def rpc(self, token, fn, args):
        self.rpc_calls.append({"fn": fn, "uid": self.tokens.get(token)})
        if fn != "admin_trip_summary":
            return {"error": {"code": "PGRST202", "message": "Could not find the function public.%s" % fn}, "status": 404}
        uid = self.tokens.get(token) if token else None
        if uid is None:
            return {"error": {"code": "42501", "message": "permission denied for function admin_trip_summary"}, "status": 401}
        if not self.is_admin(uid):
            return {"error": {"code": "42501", "message": "not_platform_admin", "details": None, "hint": None}, "status": 403}
        return {"data": copy.deepcopy(self.summary_override) if self.summary_override is not None else self.summary(),
                "status": 200}

    def invite_status(self, r):
        if r["used_at"]:
            return "used"
        if r["revoked_at"]:
            return "revoked"
        return "expired" if parse(r["expires_at"]) <= self.now else "pending"

    def summary(self):
        out = []
        today = self.now.date().isoformat()
        for t in self.rows["trips"].values():
            mem = [m for m in self.rows["trip_members"].values() if m["trip_id"] == t["id"] and m["status"] == "active"]
            ended = t["status"] == "archived" or (t["end_date"] and t["end_date"] < today)
            cs = sorted([c for c in self.rows["trip_countries"].values() if c["trip_id"] == t["id"]], key=lambda c: c["sort"])
            out.append(SUMMARY_SHAPE("trip", {
                "id": t["id"], "name": t["name"], "start_date": t["start_date"], "end_date": t["end_date"],
                "status": "ended" if ended else "active",
                "managers": [m["display_name"] for m in mem if m["role"] == "manager"],
                "countries": [c["country_code"] for c in cs], "member_count": len(mem)}))
        for r in self.invites.values():
            st = self.invite_status(r)
            if st == "used":
                continue
            out.append(SUMMARY_SHAPE("invite", {k: r[k] for k in ("id", "email", "draft_name", "created_at", "last_sent_at",
                                                                   "send_count", "opened_at", "expires_at")} | {"status": st}))
        return out

    # ---------- fake invite-manager (spec §3) ----------
    def fn_http(self, device, headers, body):
        auth = headers.get("authorization") or ""
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        return self.invite_manager(device, "fetch", bearer, headers.get("apikey"), body, raw_auth=auth)

    def invite_manager(self, device, via, bearer, apikey, body, raw_auth=None):
        with self.lock:
            body = body if isinstance(body, dict) else {}
            action = body.get("action")
            status, resp = self._invite_manager(bearer, body)
            self.fn_calls.append({"via": via, "device": device, "action": action, "body": copy.deepcopy(body),
                                  "auth": raw_auth if raw_auth is not None else ("Bearer " + bearer if bearer else None),
                                  "bearer": bearer, "apikey": apikey, "status": status, "resp": copy.deepcopy(resp)})
            return status, resp

    def _send(self, row, token):
        if self.email_fail:
            return False
        self.emails.append({"to": row["email"], "subject": "הוזמנת לנהל טיול ב-Plan_Travel",
                            "link": PROD_APP_URL + "#invite=" + token, "token": token, "invite_id": row["id"]})
        return True

    def _invite_manager(self, bearer, body):
        action = body.get("action")
        if action == "check":
            tok = body.get("token")
            r = None
            if isinstance(tok, str) and tok:
                h = sha256_hex(tok)
                r = next((x for x in self.invites.values() if x["token_hash"] == h), None)
            if r and self.invite_status(r) == "pending":
                if not r["opened_at"]:
                    r["opened_at"] = iso(self.now)
                return 200, {"valid": True, "draft_name": r["draft_name"], "inviter_name": r["inviter_name"],
                             "expires_at": r["expires_at"]}
            return 200, {"valid": False}
        uid = self.tokens.get(bearer) if bearer else None
        if not self.is_admin(uid):
            return 403, FN_ERROR("not_platform_admin")
        if action == "create":
            email = str(body.get("email") or "").strip().lower()
            if not EMAIL_RE.match(email):
                return 400, FN_ERROR("invalid_email")
            name = body.get("draft_name")
            name = str(name).strip() if name is not None else None
            if name and len(name) > 80:
                return 400, FN_ERROR("invalid_draft_name")
            for r in list(self.invites.values()):
                if r["email"] == email and not r["used_at"] and not r["revoked_at"]:
                    if parse(r["expires_at"]) > self.now:
                        return 409, FN_ERROR("invite_exists", invite_id=r["id"], draft_name=r["draft_name"],
                                             expires_at=r["expires_at"])
                    r["revoked_at"] = iso(self.now)
            token = new_token()
            mine = sorted([m for m in self.rows["trip_members"].values() if m["user_id"] == uid and m["status"] == "active"],
                          key=lambda m: str(m["joined_at"]), reverse=True)
            row = {"id": str(uuid.uuid4()), "email": email, "token_hash": sha256_hex(token), "draft_name": name or None,
                   "invited_by": uid, "expires_at": iso(self.now + 7 * DAY), "used_at": None, "revoked_at": None,
                   "trip_id": None, "created_at": iso(self.now), "send_count": 1, "last_sent_at": iso(self.now),
                   "opened_at": None, "inviter_name": mine[0]["display_name"] if mine else "מנהל המערכת"}
            self.invites[row["id"]] = row
            if not self._send(row, token):
                del self.invites[row["id"]]
                return 502, FN_ERROR("email_failed")
            return 200, {"ok": True, "invite_id": row["id"], "expires_at": row["expires_at"]}
        if action in ("resend", "revoke"):
            r = self.invites.get(str(body.get("invite_id")))
            if not r:
                return 404, FN_ERROR("not_found")
            if action == "revoke":
                if r["used_at"]:
                    return 409, FN_ERROR("invite_used")
                if not r["revoked_at"]:
                    r["revoked_at"] = iso(self.now)
                return 200, {"ok": True}
            if r["used_at"] or r["revoked_at"]:
                return 409, FN_ERROR("invite_closed")
            token = new_token()
            if not self._send(r, token):
                return 502, FN_ERROR("email_failed")
            r.update({"token_hash": sha256_hex(token), "expires_at": iso(self.now + 7 * DAY),
                      "send_count": r["send_count"] + 1, "last_sent_at": iso(self.now), "opened_at": None})
            return 200, {"ok": True, "expires_at": r["expires_at"]}
        return 400, FN_ERROR("bad_action")

    def check(self, token):
        return self._invite_manager(None, {"action": "check", "token": token})[1].get("valid")

    def fn(self, action=None):
        return [c for c in self.fn_calls if action is None or c["action"] == action]


# =====================================================================================================
# Browser environment
# =====================================================================================================
class F03Device(f02.Device):
    def __init__(self, env, name, ctx, page, conf):  # noqa: super().__init__ not called: own dialog policy
        self.env, self.name, self.ctx, self.page, self.conf = env, name, ctx, page, conf
        self.errors, self.console, self.dialogs = [], [], []
        self.dialog_policy = "accept"
        self.is_offline = False
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("console", lambda m: self.console.append(m.type + ": " + m.text))
        page.on("dialog", self._dialog)

    def _dialog(self, d):
        self.dialogs.append({"type": d.type, "message": d.message})
        try:
            d.accept() if self.dialog_policy == "accept" else d.dismiss()
        except PWError:
            pass

    def offline(self, on):
        self.is_offline = bool(on)
        self.ctx.set_offline(on)


class Env(f02.Env):
    def __init__(self, browser, server):
        super().__init__(browser, server)
        self.backend = F03Backend()

    def new_device(self, name="A", service_workers="block", **_):
        conf = {"legacy_fake": False}
        ctx = self.browser.new_context(viewport=VIEWPORT, locale="he-IL", timezone_id="Asia/Jerusalem",
                                       service_workers=service_workers,
                                       permissions=["geolocation", "clipboard-read", "clipboard-write"],
                                       geolocation={"latitude": 21.0285, "longitude": 105.8542, "accuracy": 20})
        ctx.clock.set_fixed_time(FIXED_NOW)
        ctx.expose_binding("__fakeBackend", lambda source, msg, _n=name: self.backend.handle(_n, msg))
        ctx.add_init_script(f02.WATCH_JS)
        page = ctx.new_page()
        d = F03Device(self, name, ctx, page, conf)
        ctx.route("**/*", lambda route, request, _d=d: self._route3(_d, route, request))
        self.devices.append(d)
        return d

    def _route3(self, dev, route, request):
        url = request.url
        p = urlparse(url)
        rec = {"device": dev.name, "url": url, "method": request.method, "type": request.resource_type}
        self.net.append(rec)
        try:
            if p.hostname in ("127.0.0.1", "localhost"):
                rec["action"] = "same-origin"
                return route.continue_()
            if p.hostname == "cdn.jsdelivr.net" and "@supabase/supabase-js" in p.path:
                rec["action"] = "fake-lib"
                return route.fulfill(status=200, content_type="application/javascript; charset=utf-8", body=FAKE_JS,
                                     headers={"Access-Control-Allow-Origin": "*"})
            if url.split("?")[0].rstrip("/") == FN_URL:
                origin = request.headers.get("origin") or "*"
                cors = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "POST, OPTIONS",
                        "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type", "Vary": "Origin"}
                if dev.is_offline or self.backend.fn_down or self.backend.mode == "down":
                    rec["action"] = "fn-offline"
                    return route.abort("internetdisconnected")
                if request.method == "OPTIONS":
                    rec["action"] = "fn-preflight"
                    return route.fulfill(status=204, headers=cors, body="")
                try:
                    body = json.loads(request.post_data or "{}")
                except ValueError:
                    body = {"__unparsable": request.post_data}
                rec["action"] = "fn"
                rec["body"] = body
                status, resp = self.backend.fn_http(dev.name, {k.lower(): v for k, v in request.headers.items()}, body)
                return route.fulfill(status=status, headers=cors, content_type="application/json", body=json.dumps(resp))
            rec["action"] = "aborted"
            return route.abort()
        except PWError:
            pass


@pytest.fixture(scope="session")
def server():
    s = f02.Server()
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
# Source helpers (alpha.2 reference + flag flips)
# =====================================================================================================
def alpha2_html():
    r = subprocess.run(["git", "-C", str(REPO_DIR), "show", "%s:app/index.html" % ALPHA2_REF], capture_output=True)
    assert r.returncode == 0, "harness: cannot read alpha.2 (%s:app/index.html): %s" % (ALPHA2_REF, r.stderr.decode()[:200])
    html = r.stdout.decode("utf-8")
    assert "const APP_VERSION='3.0.0-alpha.2'" in html, "harness: %s is not alpha.2" % ALPHA2_REF
    return html


def app_html():
    return (APP_DIR / "index.html").read_text(encoding="utf-8")


FLAG_RE = re.compile(r"""(const\s+ADMIN_FEATURE\s*=\s*)(['"])on\2""")


def serve_admin_off(env):
    """Serve the app with ADMIN_FEATURE flipped to 'off'. If the flag does not exist (alpha.2) the source is served as-is:
    the behavioral 'off' tests then compare alpha.2 with itself; the flag's existence is checked by test_ac16_flag."""
    src = app_html()
    if FLAG_RE.search(src):
        env.server.overrides[APP_PATH] = FLAG_RE.sub(r"\1'off'", src, count=1).encode("utf-8")
        return True
    return False


def serve_alpha2(env):
    env.server.overrides[ALPHA2_PATH] = alpha2_html().encode("utf-8")


# =====================================================================================================
# UI helpers (text-based)
# =====================================================================================================
NORM_JS = r"""const N=s=>String(s||'').replace(/[‎‏‪-‮⁦-⁩]/g,'').replace(/\s+/g,' ').trim();"""

FIND_JS = NORM_JS + r"""
const VIS=e=>{const r=e.getBoundingClientRect();if(r.width<1||r.height<1)return false;const s=getComputedStyle(e);
 if(s.visibility==='hidden'||s.display==='none'||+s.opacity===0)return false;
 for(let a=e;a;a=a.parentElement){const t=getComputedStyle(a);if(t.display==='none'||+t.opacity===0)return false}return true};
window.__qaFind=(text,scopeSel,exact,clickable)=>{
 const scope=scopeSel?document.querySelector(scopeSel):document.body;if(!scope)return[];
 const t=N(text);
 const own=e=>{const v=N(e.innerText!==undefined?e.innerText:e.textContent);return exact?v===t:v.includes(t)};
 const lbl=e=>N(e.getAttribute&&e.getAttribute('aria-label'));
 let all=[...scope.querySelectorAll('*')].filter(e=>(own(e)||(exact?lbl(e)===t:lbl(e).includes(t)))&&VIS(e));
 if(scope!==document.body&&(own(scope)||lbl(scope).includes(t))&&VIS(scope))all.push(scope);
 let deep=all.filter(e=>!all.some(o=>o!==e&&e.contains(o)));
 if(clickable){const C='button,a[href],[role=button],[role=tab],input,select,textarea,label,summary,[onclick],[data-go],[tabindex]';
   deep=deep.map(e=>e.closest(C)||e).filter((e,i,a)=>a.indexOf(e)===i)}
 window.__qaLast=deep;
 return deep.map((e,i)=>{const r=e.getBoundingClientRect();
   const clk=!!e.closest('button,a[href],[role=button],[role=tab],input,select,summary,[onclick]');
   return{i,w:r.width,h:r.height,tag:e.tagName,clk,text:N(e.innerText).slice(0,80),
     disabled:!!(e.disabled||e.getAttribute('aria-disabled')==='true'||(e.closest('fieldset')&&e.closest('fieldset').disabled))}})};
window.__qaPoint=(i)=>{const e=(window.__qaLast||[])[i];if(!e)return null;e.scrollIntoView({block:'center',inline:'nearest'});
 const r=e.getBoundingClientRect();const x=r.left+r.width/2,y=r.top+r.height/2;
 const h=document.elementFromPoint(Math.max(0,Math.min(innerWidth-1,x)),Math.max(0,Math.min(innerHeight-1,y)));
 return{x,y,top:!!h&&(h===e||e.contains(h)||h.contains(e))}};
"""


def _inject(page):
    page.evaluate("()=>{if(!window.__qaFind){(new Function(%s))()}}" % json.dumps(FIND_JS))


def find(page, text, scope=None, exact=False, clickable=True):
    """Visible elements showing `text` (or with it in aria-label); each with a click point x/y (scrolled into view)."""
    _inject(page)
    out = page.evaluate("([t,s,e,c])=>window.__qaFind(t,s,e,c)", [text, scope, exact, clickable])
    for c in out:
        pt = page.evaluate("(i)=>window.__qaPoint(i)", c["i"]) or {"x": -99, "y": -99, "top": False}
        c.update(pt)
    return out


def wait_find(page, text, scope=None, exact=False, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        c = find(page, text, scope, exact)
        if c:
            return c
        page.wait_for_timeout(150)
    return []


def click_text(page, text, scope=None, exact=False, timeout=8.0, avoid=None):
    """Real mouse click at the centre of the deepest visible element showing `text` (or its clickable ancestor)."""
    cands = wait_find(page, text, scope, exact, timeout)
    if avoid:
        cands = [c for c in cands if abs(c["x"] - avoid["x"]) > 2 or abs(c["y"] - avoid["y"]) > 2]
    assert cands, "UI element with text %r not found%s" % (text, (" in " + scope) if scope else "")
    cands = sorted(cands, key=lambda c: (not c["top"], not c.get("clk")))  # prefer a hit-testable real control
    c = cands[0]
    page.mouse.click(c["x"], c["y"])
    page.wait_for_timeout(350)
    return c


def body_text(page):
    _inject(page)
    return page.evaluate("()=>{%s return N(document.body.innerText)}" % NORM_JS)


def visible(page, text, scope=None):
    return bool(find(page, text, scope, clickable=False))


def wait_text(page, text, timeout=8.0, scope=None):
    end = time.time() + timeout
    while time.time() < end:
        if visible(page, text, scope):
            return True
        page.wait_for_timeout(150)
    return False


def gone(page, text, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if not visible(page, text):
            return True
        page.wait_for_timeout(150)
    return False


def fill(page, selector, value):
    loc = page.locator(selector).filter(visible=True)
    assert loc.count(), "no visible input matching %s" % selector
    loc.first.fill(value)
    page.wait_for_timeout(150)


def mark_container(page, selector, text, mark):
    """Mark the smallest visible element that contains an element matching `selector` (or a text) and `text`."""
    _inject(page)
    return page.evaluate("""([sel,text,mark])=>{""" + NORM_JS + """
      const vis=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden'};
      const has=(e)=>sel.startsWith('text:')?N(e.innerText).includes(sel.slice(5)):[...e.querySelectorAll(sel)].some(vis);
      let best=null;for(const e of document.querySelectorAll('body *')){if(!vis(e)||!N(e.innerText).includes(N(text))||!has(e))continue;
        if(!best||best.contains(e))best=e}
      document.querySelectorAll('[data-qa-mark="'+mark+'"]').forEach(x=>x.removeAttribute('data-qa-mark'));
      if(!best)return null;best.setAttribute('data-qa-mark',mark);return N(best.innerText)}""", [selector, text, mark])


def card(page, name, stops):
    """Mark and return the list card that shows `name`: climb from the name until the parent also shows another card's
    name (or a screen-level text)."""
    _inject(page)
    return page.evaluate("""([name,stops])=>{""" + NORM_JS + """
      const els=window.__qaFind(name,null,false,false);if(!els.length)return null;
      const cand=[...document.querySelectorAll('body *')].filter(e=>N(e.innerText).includes(N(name))&&e.getBoundingClientRect().height>0);
      let el=cand.filter(e=>!cand.some(o=>o!==e&&e.contains(o)))[0];if(!el)return null;
      while(el.parentElement&&el.parentElement!==document.body){const pt=N(el.parentElement.innerText);
        if(stops.some(s=>s!==name&&pt.includes(N(s))))break;el=el.parentElement}
      const key='c'+Math.random().toString(36).slice(2,8);el.setAttribute('data-qa-card',key);
      const r=el.getBoundingClientRect();
      return{key,sel:'[data-qa-card="'+key+'"]',text:N(el.innerText),top:r.top+scrollY,html:el.outerHTML}}""", [name, stops])


def email_is_ltr(page, email, scope=None):
    _inject(page)
    return page.evaluate("""([email,scope])=>{""" + NORM_JS + """
      const root=scope?document.querySelector(scope):document.body;if(!root)return null;
      const els=[...root.querySelectorAll('*')].filter(e=>N(e.innerText||e.textContent).includes(email)&&e.getBoundingClientRect().height>0);
      const deep=els.filter(e=>!els.some(o=>o!==e&&e.contains(o)));
      if(!deep.length)return null;
      return deep.map(e=>{const a=e.closest('[dir]');const d=a?a.getAttribute('dir'):'';
        if(e.tagName==='INPUT')return getComputedStyle(e).direction;
        return getComputedStyle(e).direction==='ltr'||d==='ltr'||d==='auto'||getComputedStyle(e).unicodeBidi==='plaintext'?'ltr':'rtl'})}""", [email, scope])


def is_disabled(page, text, scope=None):
    c = find(page, text, scope)
    return bool(c) and all(x["disabled"] for x in c)


CHROME_JS = """(sels)=>sels.filter(sel=>[...document.querySelectorAll(sel)].some(t=>{
  const r=t.getBoundingClientRect();if(!r.width||!r.height)return false;const s=getComputedStyle(t);
  if(s.visibility==='hidden'||+s.opacity===0||s.display==='none')return false;
  const x=Math.min(Math.max(r.left+r.width/2,0),innerWidth-1),y=Math.min(Math.max(r.top+r.height/2,0),innerHeight-1);
  const h=document.elementFromPoint(x,y);return h&&(t===h||t.contains(h))}))"""


def chrome_visible(page):
    return page.evaluate(CHROME_JS, SEL["trip_chrome"])


def layout_problems(page, scope=None, label=""):
    """390px checks: no horizontal scroll, RTL, every visible interactive element in scope >= 44x44 (hit-tested)."""
    r = page.evaluate("""(scope)=>{const root=scope?document.querySelector(scope):document.body;
      const out={sw:document.scrollingElement.scrollWidth,iw:innerWidth,dir:document.documentElement.getAttribute('dir'),
        rootDir:root?getComputedStyle(root).direction:null,small:[],wide:[]};if(!root)return out;
      const C='button,a[href],[role=button],[role=tab],input:not([type=hidden]),select,textarea,summary,[onclick],[data-go]';
      for(const e of root.querySelectorAll(C)){const r=e.getBoundingClientRect();if(r.width<1||r.height<1)continue;
        const s=getComputedStyle(e);if(s.visibility==='hidden'||+s.opacity===0)continue;
        const x=Math.min(Math.max(r.left+r.width/2,0),innerWidth-1),y=Math.min(Math.max(r.top+r.height/2,0),innerHeight-1);
        if(r.top>=innerHeight||r.bottom<=0){e.scrollIntoView({block:'center'})}
        const q=e.getBoundingClientRect(),h=document.elementFromPoint(Math.min(Math.max(q.left+q.width/2,0),innerWidth-1),Math.min(Math.max(q.top+q.height/2,0),innerHeight-1));
        if(!h||!(h===e||e.contains(h)||h.contains(e)))continue;
        const lab=(e.innerText||e.value||e.getAttribute('aria-label')||e.tagName).trim().slice(0,30);
        if(q.width<44-0.5||q.height<44-0.5)out.small.push(lab+' '+Math.round(q.width)+'x'+Math.round(q.height));
        if(q.left<-0.5||q.right>innerWidth+0.5)out.wide.push(lab)}
      return out}""", scope)
    p = []
    if r["sw"] > r["iw"] + 1:
        p.append("%s: horizontal scroll (%d > %d)" % (label, r["sw"], r["iw"]))
    if r["dir"] != "rtl" or (r["rootDir"] and r["rootDir"] != "rtl"):
        p.append("%s: not RTL (html dir=%r, container direction=%r)" % (label, r["dir"], r["rootDir"]))
    for s in r["small"]:
        p.append("%s: touch target < 44px: %s" % (label, s))
    for s in r["wide"]:
        p.append("%s: element outside the 390px viewport: %s" % (label, s))
    return p


# =====================================================================================================
# Scenario helpers
# =====================================================================================================
def boot(env, name="A", role="manager", admin=False, email=None, display="אור", trip=None, path=APP_PATH):
    be = env.backend
    tid = trip or be.add_trip_full("ויאטנם 2026", "2026-09-15", "2026-10-06", countries=["VN"])
    be.set_kv(tid, "tripname", "טיול QA")
    be.auto_link = {"trip_id": tid, "role": role, "status": "active", "display_name": display}
    be.auto_admin, be.auto_email = admin, email
    dev = env.new_device(name)
    state = dev.goto(path)
    assert state == "app", "app did not start for a linked %s (state=%s)" % (role, state)
    f02.ensure_user(dev.page)
    return tid, dev


def fresh_load(dev, path, ready=True):
    """Full document load of `path` (a goto that only changes the hash would be a same-document navigation)."""
    dev.page.goto(dev.env.server.origin + f02.BLANK_PATH, wait_until="domcontentloaded")
    dev.page.goto(dev.env.server.origin + path, wait_until="domcontentloaded")
    return f02.wait_ready(dev.page) if ready else None


def open_more(page):
    f02.tab(page, "more")
    page.wait_for_timeout(500)


def open_admin(page, wait_list=True):
    open_more(page)
    assert wait_text(page, TXT["admin_row"]), "row %r not in 'עוד' for a super-admin" % TXT["admin_row"]
    click_text(page, TXT["admin_row"])
    assert wait_text(page, TXT["invite_btn"]), "admin screen did not open (no %r)" % TXT["invite_btn"]
    if wait_list:
        page.wait_for_timeout(800)


def admin_dataset(be, admin_trip=True):
    """Spec §1.2/§5.13-14 dataset. Returns dict name -> id/token and the expected order on 'הכל'."""
    d = {}
    d["P1"] = be.add_invite("dana@example.com", "יפן 2027", sent_days_ago=2, expires_in_days=5)
    d["P2"] = be.add_invite("noname@example.com", None, sent_days_ago=1, expires_in_days=6)
    d["E1"] = be.add_invite("yoav@example.com", "יוון 2027", sent_days_ago=10, expires_in_days=-3)
    d["R1"] = be.add_invite("rev@example.com", "הזמנה מבוטלת", sent_days_ago=3, expires_in_days=4, revoked=True)
    d["U1"] = be.add_invite("used@example.com", "הזמנה מנוצלת", sent_days_ago=4, expires_in_days=3, used=True)
    d["T1"] = be.add_trip_full("ויאטנם 2026", "2026-09-15", "2026-10-06", countries=["VN"],
                               others=["עידו", "נועה", "גיל"] if admin_trip else ["עידו", "נועה", "גיל", "רז"],
                               managers=[] if admin_trip else ["אור"],
                               extra=[("פנדינג", "editor", "pending"), ("הוסר", "viewer", "removed")])
    d["T2"] = be.add_trip_full("תאילנד בחנוכה", "2026-12-14", "2026-12-28", countries=["TH"], managers=["מיכל", "רון"],
                               others=["תמר", "יעל"], extra=[("גלעד", "manager", "removed")])
    d["T3"] = be.add_trip_full("פורטוגל", "2026-08-01", "2026-08-12", countries=["PT"], managers=["שירה"], others=["אבי", "בני"])
    d["T4"] = be.add_trip_full("ארכיון אסיה", "2026-11-01", "2026-11-10", status="archived", countries=["JP", "KR", "VN", "TH"],
                               managers=["דוד"], others=["אלה"])
    d["T5"] = be.add_trip_full("קוסובו", "2027-01-05", "2027-01-20", countries=["XK"], managers=["לאה"])
    for k in ("T1", "T2", "T3", "T4", "T5"):
        be.set_kv(d[k], "expenses", json.dumps([{"description": "סוד-%s" % k, "amount": 1}]))
    return d


NAMES = {"P1": "יפן 2027", "P2": "noname@example.com", "E1": "יוון 2027", "R1": "rev@example.com", "U1": "used@example.com",
         "T1": "ויאטנם 2026", "T2": "תאילנד בחנוכה", "T3": "פורטוגל", "T4": "ארכיון אסיה", "T5": "קוסובו"}
ORDER_ALL = ["P2", "P1", "E1", "T1", "T2", "T5", "T4", "T3"]
STOPS = [NAMES[k] for k in NAMES] + [TXT["invite_btn"], TXT["revoked_toggle"], TXT["privacy"]]


def boot_admin_with_data(env, name="A"):
    be = env.backend
    d = admin_dataset(be)
    tid, dev = boot(env, name, role="manager", admin=True, email="or@example.com", trip=d["T1"])
    return d, dev


def filter_counts(page):
    t = body_text(page)
    out = {}
    for k in ("f_all", "f_pending", "f_active", "f_ended"):
        m = re.search(re.escape(TXT[k]) + r"\D{0,4}?(\d+)", t)
        out[k] = int(m.group(1)) if m else None
    m = re.search(re.escape(TXT["revoked_toggle"]) + r"\D{0,4}?(\d+)", t)
    out["revoked"] = int(m.group(1)) if m else None
    return out


def shown(page, keys):
    return [k for k in keys if visible(page, NAMES[k])]


def landing(env, token, name="L", linked=False, wait=True):
    be = env.backend
    if linked:
        tid = be.add_trip_full("טיול קיים", "2026-09-15", "2026-10-06")
        be.auto_link = {"trip_id": tid, "role": "editor", "status": "active", "display_name": "QA"}
    else:
        be.auto_link = None
    dev = env.new_device(name)
    dev.goto(APP_PATH + "#invite=" + token, ready=False)
    if wait:
        wait_landing(dev.page)
    return dev


def wait_landing(page, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        t = body_text(page)
        for k in ("land_ok", "land_bad", "land_net"):
            if TXT[k] in t:
                page.wait_for_timeout(400)
                return k
        if f02.NC_TITLE in t:
            return "nc"
        page.wait_for_timeout(200)
    return None


# =====================================================================================================
# 1 · Access (criteria 1–3, client side; DB side: sql/qa_f03_cases.md, function side: function_test.ts)
# =====================================================================================================
@pytest.mark.parametrize("role", ["manager", "editor", "viewer"])
def test_ac02_no_admin_entry_for_non_admin(env, role):
    """C2: 'ניהול מערכת' row and #admin never appear for a non-admin (any role), also via #admin in the URL."""
    tid, dev = boot(env, role=role, admin=False)
    page = dev.page
    open_more(page)
    problems = []
    if not visible(page, TXT["account_section"]):
        problems.append("section %r missing in 'עוד' for a %s (spec §1.1: visible to every member)" % (TXT["account_section"], role))
    for k in ("admin_row", "admin_badge", "invite_btn"):
        if visible(page, TXT[k]):
            problems.append("%r visible to a non-admin %s" % (TXT[k], role))
    # direct navigation: hash on the running app, then a fresh load with #admin
    page.evaluate("()=>{location.hash='#admin'}")
    page.wait_for_timeout(1200)
    if visible(page, TXT["invite_btn"]) or page.evaluate("(s)=>{const e=document.querySelector(s);return !!e&&e.offsetParent!==null}",
                                                         SEL["admin_panel"]):
        problems.append("admin screen visible after location.hash='#admin'")
    fresh_load(dev, APP_PATH + "#admin")
    page.wait_for_timeout(1000)
    if visible(page, TXT["invite_btn"]) or visible(page, TXT["admin_badge"]):
        problems.append("admin UI visible after loading %s#admin" % APP_PATH)
    if any(c["action"] in ("create", "resend", "revoke") for c in env.backend.fn_calls):
        problems.append("invite-manager admin action called by a non-admin")
    assert not problems, "\n".join(problems)


def test_ac02_admin_entry_for_admin(env):
    """C2 (positive) + §1.1/§1.2: super-admin sees 'ניהול מערכת' + badge 'סופר-אדמין'; opens the admin screen; back returns to
    'עוד'. isPlatformAdmin comes from platform_admins (own row) and is not persisted."""
    d, dev = boot_admin_with_data(env)
    page, be = dev.page, env.backend
    open_more(page)
    assert visible(page, TXT["admin_row"]), "row %r missing for a super-admin" % TXT["admin_row"]
    assert visible(page, TXT["admin_badge"]), "badge %r missing" % TXT["admin_badge"]
    assert be.queries("platform_admins"), "platform_admins was never queried (spec §4: isPlatformAdmin at boot)"
    raw = json.dumps(dev.ls(), ensure_ascii=False)
    assert "platform_admin" not in raw.lower() and "isplatformadmin" not in raw.lower(), "admin flag persisted in localStorage"
    open_admin(page)
    assert any(c["fn"] == "admin_trip_summary" for c in be.rpc_calls), "list not loaded with rpc('admin_trip_summary')"
    back = find(page, SEL["back_aria"], SEL["admin_panel"])
    assert back, "no back button (aria-label containing %r) on the admin screen" % SEL["back_aria"]
    page.mouse.click(back[0]["x"], back[0]["y"])
    page.wait_for_timeout(600)
    assert not visible(page, TXT["invite_btn"]), "back button did not leave the admin screen"
    assert visible(page, TXT["admin_row"]) and visible(page, "כספת מסמכים"), "back did not return to 'עוד'"


def test_ac02_admin_rpc_refused_shows_no_list(env):
    """C1/C2 (client reaction): if admin_trip_summary raises not_platform_admin (row removed after boot), no list renders."""
    d, dev = boot_admin_with_data(env)
    page, be = dev.page, env.backend
    open_more(page)
    be.rows["platform_admins"].clear()
    if not visible(page, TXT["admin_row"]):
        pytest.fail("row %r missing for a super-admin" % TXT["admin_row"])
    click_text(page, TXT["admin_row"])
    page.wait_for_timeout(1500)
    leaked = [k for k in ("T2", "T3", "P1") if visible(page, NAMES[k])]
    assert not leaked, "list rendered although the RPC refused: %r" % leaked


def test_ac03_admin_screen_shows_no_trip_content(env):
    """C3 (client side) + §1.2 privacy/not cached: no trip_kv values, no non-manager names, no tokens; nothing persisted."""
    d, dev = boot_admin_with_data(env)
    page, be = dev.page, env.backend
    ls_before = dev.ls()
    n_kv = len(be.queries("trip_kv"))
    open_admin(page)
    t = body_text(page)
    html = page.content()
    problems = []
    for k in ("T1", "T2", "T3", "T4", "T5"):
        if "סוד-%s" % k in t:
            problems.append("trip_kv content of %s shown" % k)
    for n in ("עידו", "נועה", "גיל", "תמר", "יעל", "אבי", "בני", "אלה", "פנדינג", "הוסר", "גלעד"):
        if n in t:
            problems.append("non-manager / inactive member name shown: %s" % n)
    for r in be.invites.values():
        if r["token_hash"] in html:
            problems.append("token_hash in the DOM")
    new_kv = [c for c in be.queries("trip_kv")[n_kv:] if (c["msg"]["req"].get("filters") or [{}])[0].get("val") not in (d["T1"],)]
    if new_kv:
        problems.append("trip_kv queried for other trips from the admin screen: %d" % len(new_kv))
    if TXT["privacy"] not in t:
        problems.append("privacy footnote %r missing" % TXT["privacy"])
    ls_after = dev.ls()
    added = {k: v for k, v in ls_after.items() if ls_before.get(k) != v}
    leak = [k for k, v in added.items() if any(s in (k + str(v)) for s in ("dana@example.com", "תאילנד בחנוכה", "פורטוגל"))]
    if leak:
        problems.append("admin list cached in localStorage: %r" % leak)
    assert not problems, "\n".join(problems)


# =====================================================================================================
# 4–9 · Invites (client flows over the fake function; function semantics: function_test.ts)
# =====================================================================================================
def open_invite_sheet(page):
    click_text(page, TXT["invite_btn"])
    assert wait_text(page, TXT["sheet_submit"]), "invite sheet did not open (no %r)" % TXT["sheet_submit"]
    page.wait_for_timeout(300)


def invite_name_input(page):
    loc = page.locator(SEL["text_input"]).filter(visible=True)
    return loc.first if loc.count() else None


def test_ac04_invite_sheet_create_sends_and_refreshes(env):
    """C4 (client) + §1.3/§4: create → POST invite-manager {action:create,email,draft_name} with Bearer JWT + apikey; toast
    'ההזמנה נשלחה ל-<email>'; one email; list refreshes with the new pending card."""
    d, dev = boot_admin_with_data(env)
    page, be = dev.page, env.backend
    open_admin(page)
    n_rpc = len(be.rpc_calls)
    open_invite_sheet(page)
    fill(page, SEL["email_input"], "  New.Manager@Example.COM ")
    nm = invite_name_input(page)
    assert nm is not None, "draft-name input not found (%s)" % SEL["text_input"]
    nm.fill("ספרד 2027")
    _inject(page)
    page.evaluate("()=>{window.__qaToasts=[];new MutationObserver(()=>{window.__qaToasts.push(document.body.innerText)}).observe(document.body,{subtree:true,childList:true,characterData:true})}")
    click_text(page, TXT["sheet_submit"])
    page.wait_for_timeout(1200)
    creates = be.fn("create")
    assert len(creates) == 1, "expected 1 create call, got %d" % len(creates)
    c = creates[0]
    assert c["body"].get("email", "").strip().lower() == "new.manager@example.com", "create email: %r" % c["body"]
    assert c["body"].get("draft_name") == "ספרד 2027", "create draft_name: %r" % c["body"]
    assert c["bearer"] and c["bearer"] in be.tokens, "create not sent with the session JWT (Authorization: Bearer)"
    if c["via"] == "fetch":
        assert c["apikey"] == PT_KEY, "fetch to invite-manager without apikey header"
    assert len(be.emails) == 1 and be.emails[0]["to"] == "new.manager@example.com"
    seen = " ".join(page.evaluate("()=>window.__qaToasts||[]")) + body_text(page)
    want = TXT["toast_sent"].format(email="new.manager@example.com")
    assert want.lower() in seen.lower().replace("‎", "").replace("‏", ""), "toast %r not shown" % want
    assert len(be.rpc_calls) > n_rpc, "list not refreshed (no new admin_trip_summary call)"
    assert wait_text(page, "ספרד 2027"), "new invite card not in the list"


def test_ac05_invite_exists_notice_and_resend_existing(env):
    """C5 (client) + §1.3: same email (case/space) while active → amber notice with the existing invite's name and days
    left, button 'לשלוח שוב את ההזמנה הקיימת' (→ its actions sheet); submit disabled while the email matches."""
    d, dev = boot_admin_with_data(env)
    page, be = dev.page, env.backend
    open_admin(page)
    open_invite_sheet(page)
    fill(page, SEL["email_input"], " DANA@Example.com ")
    click_text(page, TXT["sheet_submit"])
    page.wait_for_timeout(1000)
    assert [c for c in be.fn("create")], "no create call sent"
    assert be.fn("create")[-1]["status"] == 409, "harness: fake did not answer invite_exists"
    assert len(be.emails) == 0 and len(be.invites) == 5, "an email was sent or a row added"
    t = body_text(page)
    problems = []
    if not re.search(r"יפן 2027", t):
        problems.append("notice does not name the existing invite ('יפן 2027')")
    if not re.search(r"בתוקף עוד\s*5\s*ימים|\b5 ימים", t):
        problems.append("notice does not show the days left (5)")
    if not visible(page, TXT["resend_existing"]):
        problems.append("button %r missing" % TXT["resend_existing"])
    if not is_disabled(page, TXT["sheet_submit"]):
        problems.append("submit not disabled while the email matches the existing invite")
    fill(page, SEL["email_input"], "someone.else@example.com")
    if is_disabled(page, TXT["sheet_submit"]):
        problems.append("submit still disabled after changing the email")
    fill(page, SEL["email_input"], "dana@example.com")
    page.wait_for_timeout(200)
    if visible(page, TXT["resend_existing"]):
        click_text(page, TXT["resend_existing"])
        if not (wait_text(page, TXT["a_first"]) and visible(page, "dana@example.com")):
            problems.append("'%s' did not open the existing invite's actions sheet" % TXT["resend_existing"])
    assert not problems, "\n".join(problems)


def test_ac05_invite_sheet_validation(env):
    """§1.3: email required + validated (no create for an invalid address); draft name ≤ 80 characters."""
    d, dev = boot_admin_with_data(env)
    page, be = dev.page, env.backend
    open_admin(page)
    open_invite_sheet(page)
    click_text(page, TXT["sheet_submit"])  # empty
    fill(page, SEL["email_input"], "not-an-email")
    click_text(page, TXT["sheet_submit"])
    page.wait_for_timeout(800)
    bad = [c for c in be.fn("create") if c["status"] == 200]
    assert not bad and not be.emails, "an invite was created for an empty/invalid email"
    fill(page, SEL["email_input"], "long@example.com")
    nm = invite_name_input(page)
    assert nm is not None, "draft-name input not found"
    nm.fill("א" * 100)
    click_text(page, TXT["sheet_submit"])
    page.wait_for_timeout(1000)
    for c in be.fn("create"):
        assert len(c["body"].get("draft_name") or "") <= 80, "draft_name longer than 80 sent (%d)" % len(c["body"]["draft_name"])


def open_actions(page, key):
    c = card(page, NAMES[key], STOPS)
    assert c, "card %s (%s) not found" % (key, NAMES[key])
    menu = find(page, SEL["card_menu_aria"], c["sel"])
    if not menu:
        for t in SEL["card_menu_texts"]:
            menu = find(page, t, c["sel"], exact=True)
            if menu:
                break
    assert menu, "⋯ button not found on card %s" % NAMES[key]
    page.mouse.click(menu[0]["x"], menu[0]["y"])
    assert wait_text(page, TXT["a_first"]), "actions sheet did not open for %s" % NAMES[key]
    page.wait_for_timeout(300)
    return mark_container(page, "text:" + TXT["a_revoke"], TXT["a_first"], "actions")


def close_sheet(page):
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    if visible(page, TXT["a_first"]):
        x = find(page, "סגירה", '[data-qa-mark="actions"]') or find(page, "×", '[data-qa-mark="actions"]', exact=True)
        if x:
            page.mouse.click(x[0]["x"], x[0]["y"])
            page.wait_for_timeout(400)


def test_ac07_actions_sheet_rows_and_resend(env):
    """C7 (client) + §1.4: rows נשלחה לראשונה / שליחה אחרונה (+'פעם אחת' / 'N פעמים') / בתוקף עד / הקישור נפתח ('עדיין לא' or date);
    'שליחה חוזרת' → resend: new token (old link invalid), send_count+1, expiry reset."""
    be = env.backend
    d = admin_dataset(be)
    d["P3"] = be.add_invite("opened@example.com", "נפתחה", sent_days_ago=1, expires_in_days=6, opened_days_ago=0.5)
    NAMES["P3"] = "נפתחה"
    STOPS.append("נפתחה")
    try:
        tid, dev = boot(env, role="manager", admin=True, email="or@example.com", trip=d["T1"])
        page = dev.page
        open_admin(page)
        text = open_actions(page, "P1") or ""
        problems = []
        for want in ("dana@example.com", "יפן 2027", TXT["a_first"], "03/10/2026", TXT["a_last"], TXT["a_once"],
                     TXT["a_until"], "10/10/2026", TXT["a_opened"], TXT["a_not_yet"], TXT["a_resend"], TXT["a_revoke"]):
            if want not in text:
                problems.append("actions sheet (P1) lacks %r" % want)
        old_token = d["P1"][1]
        click_text(page, TXT["a_resend"], '[data-qa-mark="actions"]')
        page.wait_for_timeout(1200)
        rs = be.fn("resend")
        if not rs or rs[-1]["body"].get("invite_id") != d["P1"][0]:
            problems.append("resend not called with invite_id of P1: %r" % [c["body"] for c in rs])
        else:
            r = be.invites[d["P1"][0]]
            if be.check(old_token):
                problems.append("old link still valid after resend")
            if r["send_count"] != 2:
                problems.append("send_count=%r" % r["send_count"])
        close_sheet(page)
        page.wait_for_timeout(500)
        text2 = open_actions(page, "P1") or ""
        if not re.search(r"2\s*פעמים", text2):
            problems.append("actions sheet does not show '2 פעמים' after resend: %r" % text2[:200])
        if "12/10/2026" not in text2:
            problems.append("expiry not reset to 12/10/2026 in the sheet")
        close_sheet(page)
        text3 = open_actions(page, "P3") or ""
        if TXT["a_not_yet"] in text3 or not re.search(r"0[45]/10/2026", text3.split(TXT["a_opened"])[-1] if TXT["a_opened"] in text3 else ""):
            problems.append("'%s' does not show the open date for an opened invite: %r" % (TXT["a_opened"], text3[:200]))
        assert not problems, "\n".join(problems)
    finally:
        NAMES.pop("P3", None)
        if "נפתחה" in STOPS:
            STOPS.remove("נפתחה")


def confirm_revoke(page, dev, be, clicked):
    """After clicking a revoke action: native confirm (dismiss first, then accept) or a custom confirm dialog."""
    page.wait_for_timeout(700)
    if dev.dialogs:
        assert not be.fn("revoke"), "revoke sent although the confirm dialog was dismissed"
        dev.dialog_policy = "accept"
        page.mouse.click(clicked["x"], clicked["y"])
        page.wait_for_timeout(1000)
        return "native"
    assert not be.fn("revoke"), "revoke sent without a confirm dialog (spec §1.4)"
    for t in TXT["revoke_confirm"]:
        c = [x for x in find(page, t, exact=True) if abs(x["x"] - clicked["x"]) > 2 or abs(x["y"] - clicked["y"]) > 2]
        c = [x for x in c if x["top"]]
        if c:
            page.mouse.click(c[-1]["x"], c[-1]["y"])
            page.wait_for_timeout(1000)
            return "custom"
    return None


def test_ac08_actions_sheet_revoke_with_confirm(env):
    """C8 (client) + §1.4: 'ביטול ההזמנה' asks for confirmation first; then revoke → card leaves the main list and shows
    under 'הזמנות שבוטלו'; the link checks invalid."""
    d, dev = boot_admin_with_data(env)
    page, be = dev.page, env.backend
    open_admin(page)
    open_actions(page, "P1")
    dev.dialog_policy = "dismiss"
    clicked = click_text(page, TXT["a_revoke"], '[data-qa-mark="actions"]')
    how = confirm_revoke(page, dev, be, clicked)
    assert how, "no confirm dialog appeared after %r" % TXT["a_revoke"]
    rv = be.fn("revoke")
    assert rv and rv[-1]["body"].get("invite_id") == d["P1"][0], "revoke not called for P1"
    assert be.check(d["P1"][1]) is False, "harness: link still valid"
    close_sheet(page)
    page.wait_for_timeout(800)
    assert gone(page, NAMES["P1"]), "revoked invite still in the main list"
    click_text(page, TXT["revoked_toggle"])
    assert wait_text(page, NAMES["P1"]), "revoked invite not shown under %r" % TXT["revoked_toggle"]


def test_ac07_ac08_expired_card_buttons(env):
    """C6/C7/C8 (client) + §1.2: expired card has 'שליחה מחדש' (resend works on expired) and 'הסרה מהרשימה' (= revoke)."""
    be = env.backend
    d = admin_dataset(be)
    d["E2"] = be.add_invite("old@example.com", "ישנה", sent_days_ago=12, expires_in_days=-5)
    NAMES["E2"] = "ישנה"
    STOPS.append("ישנה")
    try:
        tid, dev = boot(env, role="manager", admin=True, email="or@example.com", trip=d["T1"])
        page = dev.page
        open_admin(page)
        c = card(page, NAMES["E1"], STOPS)
        assert c, "expired card not found"
        click_text(page, TXT["card_resend"], c["sel"])
        page.wait_for_timeout(1200)
        rs = be.fn("resend")
        assert rs and rs[-1]["body"].get("invite_id") == d["E1"][0], "'%s' did not resend E1" % TXT["card_resend"]
        assert be.invite_status(be.invites[d["E1"][0]]) == "pending", "harness: E1 not pending after resend"
        page.wait_for_timeout(800)
        c = card(page, NAMES["E1"], STOPS)
        assert c and TXT["valid_for"].format(n=7) in c["text"], "E1 card not refreshed to pending (7 days): %r" % (c and c["text"])
        c2 = card(page, NAMES["E2"], STOPS)
        assert c2, "second expired card not found"
        dev.dialog_policy = "accept"
        clicked = click_text(page, TXT["card_remove"], c2["sel"])
        page.wait_for_timeout(700)
        if not be.fn("revoke"):
            confirm_revoke(page, dev, be, clicked)
        rv = be.fn("revoke")
        assert rv and rv[-1]["body"].get("invite_id") == d["E2"][0], "'%s' did not revoke E2" % TXT["card_remove"]
        assert gone(page, NAMES["E2"]), "removed expired invite still listed"
    finally:
        NAMES.pop("E2", None)
        if "ישנה" in STOPS:
            STOPS.remove("ישנה")


def test_ac09_create_email_failure_shows_error(env):
    """C9 (client): email_failed on create → Hebrew error, no success toast, no new card."""
    d, dev = boot_admin_with_data(env)
    page, be = dev.page, env.backend
    open_admin(page)
    open_invite_sheet(page)
    be.email_fail = True
    before = body_text(page)
    fill(page, SEL["email_input"], "fail@example.com")
    click_text(page, TXT["sheet_submit"])
    page.wait_for_timeout(1200)
    assert be.fn("create") and be.fn("create")[-1]["status"] == 502, "create not sent / harness"
    after = body_text(page)
    assert TXT["toast_sent"].format(email="fail@example.com") not in after, "success toast after email_failed"
    assert "email_failed" not in after, "raw error code shown to the user"
    new = set(re.split(r"(?<=[.!?])\s+|\n", after)) - set(re.split(r"(?<=[.!?])\s+|\n", before))
    assert re.search(r"[֐-׿]", " ".join(new)), "no Hebrew error message shown for email_failed"


# =====================================================================================================
# 10–12 · Landing
# =====================================================================================================
@pytest.mark.parametrize("linked", [False, True], ids=["fresh-device", "linked-device"])
def test_ac10_landing_valid(env, linked):
    """C10 + §1.6: valid token → 'ההזמנה שלך אומתה', inviter + draft name, expiry, 4 steps, disabled 'התחלת הקמה · בקרוב';
    no trip chrome; opened_at set once; invite not consumed."""
    be = env.backend
    iid, token = be.add_invite("dana@example.com", "יפן 2027", sent_days_ago=2, expires_in_days=5, inviter="אור")
    dev = landing(env, token, linked=linked)
    page = dev.page
    state = wait_landing(page)
    assert state == "land_ok", "valid landing not shown (state=%s, text=%r)" % (state, body_text(page)[:200])
    t = body_text(page)
    problems = [("missing %r" % w) for w in ["אור", "יפן 2027", "10/10/2026"] + TXT["land_steps"] if w not in t]
    if not visible(page, TXT["land_btn"]) or not is_disabled(page, TXT["land_btn"]):
        problems.append("button %r missing or not disabled" % TXT["land_btn"])
    if chrome_visible(page):
        problems.append("trip chrome visible on the landing: %r" % chrome_visible(page))
    if page.evaluate("()=>window.__qaWatch.sawNotConnected"):
        problems.append("not-connected card flashed before the landing")
    checks = be.fn("check")
    if not checks or checks[0]["body"].get("token") != token:
        problems.append("invite-manager check not called with the token")
    r = be.invites[iid]
    opened = r["opened_at"]
    if not opened:
        problems.append("opened_at not set")
    if r["used_at"]:
        problems.append("invite consumed by the landing")
    fresh_load(dev, APP_PATH + "#invite=" + token, ready=False)
    wait_landing(page)
    if be.invites[iid]["opened_at"] != opened:
        problems.append("opened_at changed on the second open")
    assert not problems, "\n".join(problems)


INVALID_KINDS = ["expired", "revoked", "replaced", "used", "malformed", "random"]


def test_ac11_landing_invalid_identical(env):
    """C11: expired / revoked / replaced / used / malformed / random tokens → byte-identical invalid page."""
    be = env.backend
    toks = {}
    toks["expired"] = be.add_invite("a@example.com", "א", sent_days_ago=9, expires_in_days=-2)[1]
    toks["revoked"] = be.add_invite("b@example.com", "ב", revoked=True)[1]
    iid, old = be.add_invite("c@example.com", "ג")
    r = be.invites[iid]
    r["token_hash"] = sha256_hex(new_token())  # replaced: a resend issued a new token
    toks["replaced"] = old
    toks["used"] = be.add_invite("d@example.com", "ד", used=True)[1]
    toks["malformed"] = "%%bad token<>"
    toks["random"] = new_token()
    pages = {}
    for k in INVALID_KINDS:
        dev = landing(env, toks[k], name="I" + k, wait=False)
        st = wait_landing(dev.page)
        assert st == "land_bad", "%s token: invalid page not shown (state=%s)" % (k, st)
        if chrome_visible(dev.page):
            pytest.fail("%s: trip chrome visible on the invalid landing" % k)
        pages[k] = {"text": body_text(dev.page),
                    "html": dev.page.evaluate("()=>[...document.body.children].filter(e=>e.tagName!=='SCRIPT'&&e.offsetParent!==null||getComputedStyle(e).position==='fixed').map(e=>e.outerHTML).join('')")}
        assert TXT["land_ok"] not in pages[k]["text"]
    ref = pages["random"]
    diff = [k for k in INVALID_KINDS if pages[k]["text"] != ref["text"]]
    assert not diff, "invalid page text differs for %r vs random:\n%r\n%r" % (diff, pages[diff[0]]["text"][:300], ref["text"][:300])
    diffh = [k for k in INVALID_KINDS if pages[k]["html"] != ref["html"]]
    assert not diffh, "invalid page HTML not byte-identical for %r" % diffh


def test_ac10_landing_network_error_and_retry(env):
    """§1.6: network error → 'אין חיבור. נסו שוב' with retry; retry after the network returns → valid page."""
    be = env.backend
    iid, token = be.add_invite("dana@example.com", "יפן 2027")
    be.fn_down = True
    dev = landing(env, token, wait=False)
    page = dev.page
    st = wait_landing(page)
    assert st == "land_net", "network-error page not shown (state=%s)" % st
    assert TXT["land_bad"] not in body_text(page), "network error shown as an invalid link"
    be.fn_down = False
    click_text(page, TXT["land_retry"])
    st = None
    end = time.time() + 10
    while time.time() < end and st != "land_ok":
        st = wait_landing(page, 2)
    assert st == "land_ok", "retry did not reach the valid page (state=%s)" % st


@pytest.mark.parametrize("valid", [True, False], ids=["valid", "invalid"])
def test_ac12_token_not_kept(env, valid):
    """C12: token removed from the address bar (history.replaceState); never in localStorage / sessionStorage / IndexedDB /
    cookies / Cache Storage / console / DOM / any request URL; only in the check request body."""
    be = env.backend
    token = be.add_invite("dana@example.com", "יפן 2027")[1] if valid else new_token()
    dev = landing(env, token, wait=False)
    page = dev.page
    st = wait_landing(page)
    assert st == ("land_ok" if valid else "land_bad"), "landing state=%s" % st
    page.wait_for_timeout(1000)
    problems = []
    if token in page.url:
        problems.append("token still in the address bar: %s" % page.url)
    hist = page.evaluate("()=>JSON.stringify(history.state)")
    if hist and token in hist:
        problems.append("token in history.state")
    ss = page.evaluate("()=>{const o={};for(let i=0;i<sessionStorage.length;i++){const k=sessionStorage.key(i);o[k]=sessionStorage.getItem(k)}return JSON.stringify(o)}")
    if token in ss:
        problems.append("token in sessionStorage")
    if token in json.dumps(dev.ls()):
        problems.append("token in localStorage (raw)")
    idb = dev.raw_eval("""async()=>{const out=[];for(const d of await indexedDB.databases()){const db=await new Promise((res,rej)=>{const r=indexedDB.open(d.name);r.onsuccess=()=>res(r.result);r.onerror=()=>res(null)});
      if(!db)continue;for(const s of db.objectStoreNames){const v=await new Promise(res=>{try{const q=db.transaction(s).objectStore(s).getAll();q.onsuccess=()=>res(q.result);q.onerror=()=>res([])}catch(e){res([])}});
      try{out.push(JSON.stringify(v))}catch(e){}}db.close()}return out.join('')}""")
    if token in (idb or ""):
        problems.append("token in IndexedDB")
    caches_dump = dev.raw_eval("async()=>{if(!self.caches)return'';const o=[];for(const n of await caches.keys()){const c=await caches.open(n);for(const r of await c.keys())o.push(r.url)}return o.join(' ')}")
    if token in (caches_dump or ""):
        problems.append("token in Cache Storage")
    if any(token in (c.get("value") or "") for c in dev.ctx.cookies()):
        problems.append("token in a cookie")
    logs = [m for m in dev.console + dev.errors if token in m]
    if logs:
        problems.append("token written to the console: %r" % [m[:80] for m in logs])
    if token in page.content() or token in page.title():
        problems.append("token in the DOM / title")
    urls = [r["url"] for r in env.net if token in r["url"]]
    if urls:
        problems.append("token in a request URL: %r" % urls[:3])
    if any(token in json.dumps(e, ensure_ascii=False) for e in dev.fake_log()):
        problems.append("token passed through supabase-js (logged by the fake): use the function endpoint only")
    assert not problems, "\n".join(problems)


# =====================================================================================================
# 13–14 · Admin list
# =====================================================================================================
def test_ac13_statuses_and_filter_counts(env):
    """C13 (client): pending / expired / revoked (hidden until toggled) / active / ended (by end_date or archived); used
    invites never listed; filter counts match the cards; 'ממתינות' = pending + expired."""
    d, dev = boot_admin_with_data(env)
    page = dev.page
    open_admin(page)
    counts = filter_counts(page)
    exp = {"f_all": 8, "f_pending": 3, "f_active": 3, "f_ended": 2, "revoked": 1}
    assert counts == exp, "filter counts %r, expected %r" % (counts, exp)
    allk = ["P1", "P2", "E1", "T1", "T2", "T3", "T4", "T5"]
    assert shown(page, allk) == allk, "'הכל' does not show all cards: shown %r" % shown(page, allk)
    for hidden in ("R1", "U1"):
        assert not visible(page, NAMES[hidden]), "%s visible before toggling revoked" % NAMES[hidden]
    for f, want in (("f_pending", ["P1", "P2", "E1"]), ("f_active", ["T1", "T2", "T5"]), ("f_ended", ["T3", "T4"]),
                    ("f_all", allk)):
        click_text(page, TXT[f])
        page.wait_for_timeout(500)
        got = shown(page, allk)
        assert sorted(got) == sorted(want), "filter %s shows %r, expected %r" % (TXT[f], got, want)
    for k, badge in (("T1", "active"), ("T2", "active"), ("T5", "active"), ("T3", "ended"), ("T4", "ended")):
        c = card(page, NAMES[k], STOPS)
        other = "ended" if badge == "active" else "active"
        assert c and TXT[badge] in c["text"] and TXT[other] not in c["text"], "%s badge: %r" % (NAMES[k], c and c["text"])
    click_text(page, TXT["revoked_toggle"])
    assert wait_text(page, NAMES["R1"]), "revoked invite not shown after the toggle"
    assert not visible(page, NAMES["U1"]), "a used invite is listed"


def test_ac14_sorting(env):
    """C14: pending/expired first (newest sent first), then active trips by start_date, then ended (newest end first)."""
    d, dev = boot_admin_with_data(env)
    page = dev.page
    open_admin(page)
    tops = {}
    for k in ORDER_ALL:
        c = card(page, NAMES[k], STOPS)
        assert c, "card %s not found" % NAMES[k]
        tops[k] = c["top"]
    got = sorted(ORDER_ALL, key=lambda k: tops[k])
    assert got == ORDER_ALL, "order %r, expected %r" % ([NAMES[k] for k in got], [NAMES[k] for k in ORDER_ALL])


def test_ac14_card_contents(env):
    """C14 + §1.2 card kinds: pending (name / 'קבוצה ללא שם', email LTR, 'נשלחה לפני X · בתוקף עוד Y ימים', ⋯), expired
    ('פג ב-dd/mm' + two buttons), trip (flags ≤3, name, 'מנהלים: …', Hebrew countries · dd/mm–dd/mm/yyyy · N חברים, badge)."""
    d, dev = boot_admin_with_data(env)
    page = dev.page
    open_admin(page)
    P = []

    def C(k):
        c = card(page, NAMES[k], STOPS)
        if not c:
            P.append("card %s not found" % NAMES[k])
            return ""
        return c["text"]

    def flags(s):
        return re.findall(r"[\U0001F1E6-\U0001F1FF]{2}", s)

    t = C("P1")
    for w in ("יפן 2027", "dana@example.com", TXT["sent_ago"], TXT["valid_for"].format(n=5)):
        if w not in t:
            P.append("pending card lacks %r: %r" % (w, t))
    c1 = card(page, NAMES["P1"], STOPS)
    if c1 and not (find(page, SEL["card_menu_aria"], c1["sel"]) or any(find(page, x, c1["sel"], exact=True) for x in SEL["card_menu_texts"])):
        P.append("pending card has no ⋯ actions button")
    t = C("P2")
    if TXT["no_name"] not in t or TXT["valid_for"].format(n=6) not in t:
        P.append("unnamed pending card: %r" % t)
    dirs = email_is_ltr(page, "dana@example.com")
    if not dirs or any(x != "ltr" for x in dirs):
        P.append("email not rendered LTR (computed direction %r)" % dirs)
    t = C("E1")
    for w in ("יוון 2027", "yoav@example.com", TXT["expired_on"].format(d="02/10"), TXT["card_resend"], TXT["card_remove"]):
        if w not in t:
            P.append("expired card lacks %r: %r" % (w, t))
    if TXT["valid_for"].format(n=0) in t or "בתוקף עוד" in t:
        P.append("expired card still says 'בתוקף עוד'")
    dash = r"\s*[–\-‒—]\s*"
    for k, mgr, country, dates, n, fl in (
            ("T1", "מנהלים: אור", "ויאטנם", "15/09" + dash + "06/10/2026", 4, ["VN"]),
            ("T2", "מנהלים: מיכל, רון", "תאילנד", "14/12" + dash + "28/12/2026", 4, ["TH"]),
            ("T3", "מנהלים: שירה", "פורטוגל", "01/08" + dash + "12/08/2026", 3, ["PT"]),
            ("T5", "מנהלים: לאה", "XK", "05/01" + dash + "20/01/2027", 1, None)):
        t = C(k)
        if mgr not in t:
            P.append("%s: %r missing: %r" % (NAMES[k], mgr, t))
        if country not in t:
            P.append("%s: country %r missing" % (NAMES[k], country))
        if not re.search(dates, t):
            P.append("%s: dates %r missing: %r" % (NAMES[k], dates, t))
        # CHANGE 2026-10-05 F03-QA-FIX-01: Hebrew singular "חבר אחד" accepted for n=1 (architect review of dev output).
        if TXT["members"].format(n=n) not in t and not (n == 1 and "חבר אחד" in t):
            P.append("%s: %r missing: %r" % (NAMES[k], TXT["members"].format(n=n), t))
        if fl and [FLAG[c] for c in fl] != flags(t):
            P.append("%s: flags %r, expected %r" % (NAMES[k], flags(t), [FLAG[c] for c in fl]))
    t = C("T2")
    if "גלעד" in t:
        P.append("removed manager listed under מנהלים")
    t = C("T4")
    if not (1 <= len(flags(t)) <= 3):
        P.append("archived trip with 4 countries shows %d flags (max 3)" % len(flags(t)))
    assert not P, "\n".join(P)


def test_ac14_offline_admin(env):
    """§1.2: offline → 'אין חיבור — הרשימה תתעדכן כשהרשת תחזור' and actions disabled (no function call)."""
    d, dev = boot_admin_with_data(env)
    page, be = dev.page, env.backend
    open_more(page)
    dev.offline(True)
    page.wait_for_timeout(300)
    assert visible(page, TXT["admin_row"]), "row %r missing offline (isPlatformAdmin is kept in memory)" % TXT["admin_row"]
    click_text(page, TXT["admin_row"])
    assert wait_text(page, TXT["offline"]), "offline message %r not shown" % TXT["offline"]
    n = len(be.fn_calls)
    if visible(page, TXT["invite_btn"]):
        assert is_disabled(page, TXT["invite_btn"]), "primary button enabled offline"
        click_text(page, TXT["invite_btn"])
        if visible(page, TXT["sheet_submit"]):
            fill(page, SEL["email_input"], "x@example.com")
            click_text(page, TXT["sheet_submit"])
    page.wait_for_timeout(500)
    assert len(be.fn_calls) == n, "invite-manager called while offline"


# =====================================================================================================
# 15 · Email linking
# =====================================================================================================
def open_link_sheet(page):
    open_more(page)
    assert visible(page, TXT["link_row"]), "row %r missing in 'עוד' for a user without email" % TXT["link_row"]
    click_text(page, TXT["link_row"])
    assert wait_text(page, TXT["link_send"]), "link-email sheet did not open (no %r)" % TXT["link_send"]
    mark_container(page, SEL["email_input"], TXT["link_send"], "link")


def test_ac15_account_section_position(env):
    """§1.1: section 'החשבון שלי' inserted at the TOP of #more; the rest of #more unchanged."""
    tid, dev = boot(env, role="editor")
    page = dev.page
    open_more(page)
    pos = page.evaluate("""([a,b])=>{const more=document.getElementById('more');if(!more)return null;
      const all=[...more.querySelectorAll('*')].filter(e=>e.offsetParent!==null);
      const f=t=>{const h=all.filter(e=>(e.innerText||e.textContent||'').includes(t));return h.filter(e=>!h.some(o=>o!==e&&e.contains(o)))[0]};
      const x=f(a),y=f(b);return{acc:x?x.getBoundingClientRect().top:null,docs:y?y.getBoundingClientRect().top:null}}""",
                        [TXT["account_section"], "כספת מסמכים"])
    assert pos and pos["acc"] is not None, "section %r not inside #more" % TXT["account_section"]
    assert pos["docs"] is not None and pos["acc"] < pos["docs"], "section is not above the existing #more rows: %r" % pos
    for k in ("כספת מסמכים", "הזמנות וארנק נסיעה", "תמונות הטיול", "ניהול"):
        assert visible(page, k, "#more"), "existing #more row %r missing" % k


def test_ac15_link_email_success_then_linked(env):
    """C15: updateUser({email},{emailRedirectTo:<app URL>}); success text; after confirmation 'מייל מקושר' + address +
    'מקושר'; same auth.uid(), membership kept."""
    tid, dev = boot(env, role="editor")
    page, be = dev.page, env.backend
    uid = dev.uid()
    open_link_sheet(page)
    fill(page, SEL["email_input"], "name@gmail.com")
    click_text(page, TXT["link_send"])
    want = TXT["link_success"].format(email="name@gmail.com")
    assert wait_text(page, want), "success text %r not shown (text: %r)" % (want, body_text(page)[-300:])
    assert len(be.update_calls) == 1, "updateUser calls: %d" % len(be.update_calls)
    call = be.update_calls[0]
    assert call["attrs"] == {"email": "name@gmail.com"}, "updateUser attrs: %r" % call["attrs"]
    redirect = (call["options"] or {}).get("emailRedirectTo")
    ok_urls = {PROD_APP_URL, env.server.origin + APP_PATH, env.server.origin + APP_PATH + "index.html"}
    assert redirect in ok_urls, "emailRedirectTo=%r, expected the app URL (%s)" % (redirect, sorted(ok_urls))
    # the user confirms from the mail on the same phone (simulated: backend + stored session carry the email)
    be.confirm_email(uid, "name@gmail.com")
    k = dev.auth_raw_key()
    s = json.loads(dev.ls()[k])
    s["user"] = copy.deepcopy(be.users[uid])
    dev.seed_ls({k: json.dumps(s)})
    page.reload(wait_until="domcontentloaded")
    assert f02.wait_ready(page) == "app", "app did not start after linking"
    f02.ensure_user(page)
    open_more(page)
    P = []
    for w in (TXT["linked_row"], "name@gmail.com", TXT["linked_badge"]):
        if not visible(page, w):
            P.append("%r not shown after linking" % w)
    if visible(page, TXT["link_row"]):
        P.append("%r still shown after linking" % TXT["link_row"])
    if dev.uid() != uid:
        P.append("auth.uid() changed")
    if len(be.ops("auth.signInAnonymously")) != 1:
        P.append("a new anonymous identity was created")
    if not f02.trip_name_is(page, "טיול QA"):
        P.append("trip membership lost after linking")
    if visible(page, TXT["admin_row"]):
        P.append("'ניהול מערכת' shown to a linked non-admin")
    assert not P, "\n".join(P)


def test_ac15_link_email_errors_mapped(env):
    """C15: invalid email / rate limit / email used by another account / offline → each its own Hebrew text; never the
    success text, never the raw English error."""
    tid, dev = boot(env, role="viewer")
    page, be = dev.page, env.backend
    msgs, P = {}, []
    for kind in ("invalid", "rate", "exists", "offline"):
        open_link_sheet(page)
        before = mark_container(page, SEL["email_input"], TXT["link_send"], "link") or ""
        be.update_outcome = kind if kind != "offline" else "ok"
        fill(page, SEL["email_input"], "bad@@example" if kind == "invalid" else "name%s@gmail.com" % kind)
        if kind == "offline":
            dev.offline(True)
        click_text(page, TXT["link_send"])
        page.wait_for_timeout(1500)
        after = page.evaluate("()=>{const e=document.querySelector('[data-qa-mark=\"link\"]');return e?e.innerText:document.body.innerText}")
        if kind == "offline":
            dev.offline(False)
        new = [ln.strip() for ln in after.splitlines() if ln.strip() and ln.strip() not in before]
        heb = [ln for ln in new if re.search(r"[֐-׿]", ln)]
        if "שלחנו מייל ל-" in after:
            P.append("%s: success text shown" % kind)
        if kind in AUTH_ERRORS and AUTH_ERRORS[kind]["message"] in after:
            P.append("%s: raw English error shown" % kind)
        if not heb:
            P.append("%s: no Hebrew error text appeared" % kind)
        msgs[kind] = " ".join(heb)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        page.reload(wait_until="domcontentloaded")
        f02.wait_ready(page)
        f02.ensure_user(page)
    be.update_outcome = "ok"
    vals = [v for v in msgs.values() if v]
    if len(set(vals)) != len(vals):
        P.append("error texts are not distinct per error: %r" % msgs)
    assert not P, "\n".join(P)


def test_ac15_account_section_absent_in_local_only(env):
    """§1.1: section not shown with STORAGE_BACKEND='local-only'."""
    src = app_html()
    pat = re.compile(r"""(const\s+STORAGE_BACKEND\s*=\s*)(['"])cloud\2""")
    assert pat.search(src), "STORAGE_BACKEND flag not found"
    env.server.overrides[APP_PATH] = pat.sub(r"\1'local-only'", src, count=1).encode("utf-8")
    dev = env.new_device("L")
    assert dev.goto(APP_PATH) == "app"
    f02.ensure_user(dev.page)
    open_more(dev.page)
    for k in ("account_section", "link_row", "admin_row"):
        assert not visible(dev.page, TXT[k]), "%r shown in local-only" % TXT[k]


# =====================================================================================================
# 16 · ADMIN_FEATURE='off' == alpha.2
# =====================================================================================================
def more_text(page):
    open_more(page)
    page.wait_for_timeout(500)
    return f02.norm_text(page.evaluate("()=>{const m=document.getElementById('more');return m?m.innerText:''}"))


def test_ac16_off_more_panel_identical_to_alpha2(env):
    """C16: with ADMIN_FEATURE='off', '#more' shows exactly what alpha.2 shows (even for a super-admin with an email)."""
    be = env.backend
    tid = be.add_trip_full("ויאטנם 2026", "2026-09-15", "2026-10-06", countries=["VN"])
    be.set_kv(tid, "tripname", "טיול QA")
    be.auto_link = {"trip_id": tid, "role": "manager", "status": "active", "display_name": "אור"}
    be.auto_admin, be.auto_email = True, "or@example.com"
    serve_alpha2(env)
    a = env.new_device("ALPHA2")
    assert a.goto(ALPHA2_PATH) == "app"
    f02.ensure_user(a.page)
    ref = more_text(a.page)
    serve_admin_off(env)
    n = env.new_device("OFF")
    assert n.goto(APP_PATH) == "app"
    f02.ensure_user(n.page)
    got = more_text(n.page)
    assert got == ref, "#more differs from alpha.2 with ADMIN_FEATURE='off':\nalpha.2: %r\noff:     %r" % (ref, got)


def test_ac16_off_ignores_invite_hash_and_admin(env):
    """C16: 'off' → '#invite=' ignored (normal boot), '#admin' nothing, no platform_admins / admin rpc / updateUser /
    invite-manager traffic at all."""
    be = env.backend
    iid, token = be.add_invite("dana@example.com", "יפן 2027")
    tid = be.add_trip_full("ויאטנם 2026", "2026-09-15", "2026-10-06")
    be.set_kv(tid, "tripname", "טיול QA")
    be.auto_link = {"trip_id": tid, "role": "manager", "status": "active", "display_name": "אור"}
    be.auto_admin = True
    serve_admin_off(env)
    dev = env.new_device("OFF")
    dev.goto(APP_PATH + "#invite=" + token, ready=False)
    st = f02.wait_ready(dev.page)
    assert st == "app", "'#invite=' not ignored: normal boot did not happen (state=%s)" % st
    t = body_text(dev.page)
    assert TXT["land_ok"] not in t and TXT["land_bad"] not in t, "landing shown with ADMIN_FEATURE='off'"
    f02.ensure_user(dev.page)
    fresh_load(dev, APP_PATH + "#admin")
    dev.page.wait_for_timeout(800)
    assert not visible(dev.page, TXT["invite_btn"]), "admin screen with ADMIN_FEATURE='off'"
    P = []
    if be.fn_calls:
        P.append("invite-manager called: %r" % [c["action"] for c in be.fn_calls])
    if be.queries("platform_admins"):
        P.append("platform_admins queried")
    if be.rpc_calls:
        P.append("rpc called: %r" % [c["fn"] for c in be.rpc_calls])
    if be.update_calls:
        P.append("updateUser called")
    if be.invites[iid]["opened_at"]:
        P.append("invite opened_at set")
    assert not P, "\n".join(P)


def test_ac16_flag_declared(env):
    """C16/§1: top-level const ADMIN_FEATURE='on' next to CHAT_FEATURE (same classic script block)."""
    html = app_html()
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    blk = [b for b in blocks if re.search(r"const\s+CHAT_FEATURE\s*=", b)]
    assert blk, "CHAT_FEATURE flag block not found"
    assert re.search(r"""^\s*const\s+ADMIN_FEATURE\s*=\s*['"]on['"]""", blk[0], re.M), \
        "const ADMIN_FEATURE='on' not declared in the flags script next to CHAT_FEATURE"


# =====================================================================================================
# 17 · 390px / RTL / 44px / no trip chrome
# =====================================================================================================
SCREENS = ["more-account", "link-sheet", "admin", "invite-sheet", "actions-sheet", "landing-valid", "landing-invalid"]


@pytest.mark.parametrize("screen", SCREENS)
def test_ac17_390_rtl_touch(env, screen):
    """C17: 390px — no horizontal scroll, RTL, touch ≥ 44px on all new UI; trip chrome absent on admin and landing."""
    be = env.backend
    P = []
    if screen.startswith("landing"):
        tok = be.add_invite("dana@example.com", "יפן 2027")[1] if screen == "landing-valid" else new_token()
        dev = landing(env, tok)
        st = wait_landing(dev.page)
        assert st == ("land_ok" if screen == "landing-valid" else "land_bad"), "landing state=%s" % st
        P += layout_problems(dev.page, None, screen)
        if chrome_visible(dev.page):
            P.append("%s: trip chrome visible %r" % (screen, chrome_visible(dev.page)))
        assert not P, "\n".join(P)
        return
    if screen in ("more-account", "link-sheet"):
        tid, dev = boot(env, role="editor")
        page = dev.page
        open_more(page)
        if screen == "more-account":
            assert visible(page, TXT["link_row"]), "row %r missing" % TXT["link_row"]
            info = find(page, TXT["link_row"])
            for c in info:
                if c["h"] < 44 or c["w"] < 44:
                    P.append("row %r is %.0fx%.0f" % (TXT["link_row"], c["w"], c["h"]))
            P += layout_problems(page, "#more", screen)
        else:
            open_link_sheet(page)
            P += layout_problems(page, '[data-qa-mark="link"]', screen)
        assert not P, "\n".join(P)
        return
    d, dev = boot_admin_with_data(env)
    page = dev.page
    open_admin(page)
    if screen == "admin":
        P += layout_problems(page, SEL["admin_panel"], screen)
        if chrome_visible(page):
            P.append("admin: trip chrome visible %r" % chrome_visible(page))
    elif screen == "invite-sheet":
        open_invite_sheet(page)
        mark_container(page, SEL["email_input"], TXT["sheet_submit"], "invite")
        P += layout_problems(page, '[data-qa-mark="invite"]', screen)
    else:
        open_actions(page, "P1")
        P += layout_problems(page, '[data-qa-mark="actions"]', screen)
    if chrome_visible(page):
        P.append("%s: trip chrome visible %r" % (screen, chrome_visible(page)))
    assert not P, "\n".join(P)


# =====================================================================================================
# 18 · Static
# =====================================================================================================
def _extract(html, names, tmp):
    p = tmp / ("x%d.html" % (abs(hash(html)) % 10 ** 8))
    p.write_text(html, encoding="utf-8")
    r = f02.node([str(f02.JS_TOOLS / "extract.js"), str(p)] + names)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def sections(html):
    return {m.group(1): m.group(0) for m in re.finditer(r'<section id="([^"]+)" class="panel[^"]*".*?</section>', html, re.S)}


STATIC = ["protected:" + n for n in PROTECTED] + [
    "app_version", "title_version", "change_comments", "node_check_inline_scripts", "admin_endpoint",
    "other_panels_unchanged", "more_rest_unchanged", "no_localstorage_clear", "no_live_project_ref"]


@pytest.mark.parametrize("check", STATIC)
def test_ac18_static(check, tmp_path):
    """C18: protected functions byte-identical to alpha.2; APP_VERSION/<title> 3.0.0-alpha.3; 'CHANGE 2026-10-05 F03-…';
    node --check on every inline script; §4 endpoint; §4 'no change to existing panels except #more'."""
    html = app_html()
    if check.startswith("protected:"):
        name = check.split(":", 1)[1]
        a = _extract(alpha2_html(), [name], tmp_path)["found"][name]
        b = _extract(html, [name], tmp_path)["found"][name]
        assert len(a) == 1, "harness: %s found %d times in alpha.2" % (name, len(a))
        assert b and a[0] in b, "%s is not byte-identical to alpha.2" % name
    elif check == "app_version":
        assert re.search(r"""const\s+APP_VERSION\s*=\s*['"]%s['"]""" % re.escape(VERSION), html), "APP_VERSION != %s" % VERSION
    elif check == "title_version":
        m = re.search(r"<title>(.*?)</title>", html, re.S)
        assert m and VERSION in m.group(1), "<title> does not carry %s: %r" % (VERSION, m and m.group(1))
    elif check == "change_comments":
        assert re.search(r"CHANGE 2026-10-05 F03-", html), "no 'CHANGE 2026-10-05 F03-…' comments in index.html"
    elif check == "node_check_inline_scripts":
        dd = tmp_path / "scripts"
        dd.mkdir()
        p = tmp_path / "index.html"
        p.write_text(html, encoding="utf-8")
        r = f02.node([str(f02.JS_TOOLS / "extract.js"), str(p)], env={**os.environ, "DUMP_DIR": str(dd)})
        assert r.returncode == 0, r.stderr
        files = sorted(list(dd.glob("*.js")) + list(dd.glob("*.mjs")))
        assert files, "no inline scripts found"
        bad = [f.name for f in files if f02.node(["--check", str(f)]).returncode != 0]
        assert not bad, "node --check failed: %r" % bad
    elif check == "admin_endpoint":
        assert re.search(r"""PT_ADMIN_ENDPOINT\s*=\s*PT_SUPABASE_URL\s*\+\s*['"]/functions/v1/invite-manager['"]""", html), \
            "PT_ADMIN_ENDPOINT = PT_SUPABASE_URL + '/functions/v1/invite-manager' not found (spec §4)"
    elif check == "other_panels_unchanged":
        a, b = sections(alpha2_html()), sections(html)
        changed = [k for k in a if k != "more" and a[k] != b.get(k)]
        assert not changed, "existing panels changed vs alpha.2: %r" % changed
    elif check == "more_rest_unchanged":
        a = sections(alpha2_html())["more"]
        rest = a[a.index('<div class="listitem" id="documentsBtn"'):a.rindex("</div></section>")]
        assert rest in html, "the existing #more rows are not byte-identical to alpha.2 (spec §1.1: rest of #more unchanged)"
    elif check == "no_localstorage_clear":
        assert not re.search(r"localStorage\s*\.\s*clear\s*\(", html)
    elif check == "no_live_project_ref":
        assert f02.LIVE_REF not in html


# =====================================================================================================
# DB / function criteria live elsewhere (listed so the report shows them)
# =====================================================================================================
@pytest.mark.skip(reason="C1/C3/C5/C13 DB level: tests/f03/sql/qa_f03_cases.md → qa.f03_run(); C4–C11 function level: "
                         "tests/f03/function_test.ts (Deno); advisors: Supabase Connector")
def test_db_and_function_criteria_elsewhere():
    pass


# =====================================================================================================
# Harness self-check: the fake invite-manager follows spec §3 (so client tests rest on the right semantics)
# =====================================================================================================
def test_harness_fake_invite_manager_semantics():
    be = F03Backend()
    uid = str(uuid.uuid4())
    tok = "fake-at." + uid
    be.tokens[tok] = uid
    be.users[uid] = {"id": uid}
    assert be._invite_manager(tok, {"action": "create", "email": "a@b.co"})[0] == 403
    be.make_admin(uid)
    s, r = be._invite_manager(tok, {"action": "create", "email": " A@B.co ", "draft_name": "x"})
    assert s == 200 and be.invites[r["invite_id"]]["email"] == "a@b.co"
    t1 = be.emails[-1]["token"]
    assert sha256_hex(t1) == be.invites[r["invite_id"]]["token_hash"] and len(t1) == 43
    assert be._invite_manager(tok, {"action": "create", "email": "a@B.co"})[1]["error"] == "invite_exists"
    assert be.check(t1) is True and be.invites[r["invite_id"]]["opened_at"]
    be._invite_manager(tok, {"action": "resend", "invite_id": r["invite_id"]})
    assert be.check(t1) is False and be.invites[r["invite_id"]]["send_count"] == 2
    be.email_fail = True
    t2 = be.emails[-1]["token"]
    assert be._invite_manager(tok, {"action": "resend", "invite_id": r["invite_id"]})[0] == 502 and be.check(t2) is True
    n = len(be.invites)
    assert be._invite_manager(tok, {"action": "create", "email": "z@b.co"})[0] == 502 and len(be.invites) == n
    be.email_fail = False
    assert be._invite_manager(tok, {"action": "revoke", "invite_id": r["invite_id"]})[0] == 200
    assert be._invite_manager(tok, {"action": "revoke", "invite_id": r["invite_id"]})[0] == 200 and be.check(t2) is False
    assert be._invite_manager(tok, {"action": "resend", "invite_id": r["invite_id"]})[1]["error"] == "invite_closed"
    be.invites[r["invite_id"]]["expires_at"] = iso(FIXED_NOW - DAY)
    s, r2 = be._invite_manager(tok, {"action": "create", "email": "a@b.co"})
    assert s == 200
