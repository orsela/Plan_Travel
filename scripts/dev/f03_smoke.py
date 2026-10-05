# CHANGE 2026-10-05 F03-DEVSMOKE-01: new file (no previous version). F03 dev smoke (not the QA suite).
"""F03 dev smoke: Playwright Chromium 390x844 against app/index.html, both ADMIN_FEATURE states.

Reuses the F02 QA harness (tests/f02/tests/test_f02.py: static server, FakeBackend + fake supabase-js) and extends it
for F03 (spec §7): from('platform_admins'), rpc('admin_trip_summary'), auth.updateUser, auth.getUser with email, and a
fake `invite-manager` endpoint answered by Playwright route interception. Never touches the network.

usage: python3 scripts/dev/f03_smoke.py            (needs pytest importable for the F02 module; see tests/f02/run.sh)
env:   APP_DIR (default: this repo's app/), F03_SMOKE_OUT (screenshots, default /tmp/f03_smoke)
"""
import copy
import datetime
import hashlib
import json
import os
import re
import secrets
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("APP_DIR", str(ROOT / "app"))
os.environ.setdefault("BASELINE_DIR", str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "tests" / "f02" / "tests"))
import test_f02 as T  # noqa: E402
from playwright.sync_api import Error as PWError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = Path(os.environ.get("F03_SMOKE_OUT", "/tmp/f03_smoke"))
OUT.mkdir(parents=True, exist_ok=True)
FN_URL = T.PT_URL + "/functions/v1/invite-manager"
NOW = datetime.datetime.now(datetime.timezone.utc)
DAY = datetime.timedelta(days=1)

# extra JS appended to the F02 fake: rpc / auth.updateUser go to the Python backend (the F02 fake always errors rpc)
FAKE_F03 = T.FAKE_JS + r"""
;(function(){
  var orig = window.supabase.createClient;
  window.supabase.createClient = function(url, key, options){
    var c = orig(url, key, options);
    var storageKey = (options && options.auth && options.auth.storageKey) || ('sb-' + new URL(url).hostname.split('.')[0] + '-auth-token');
    function store(){ return (options && options.auth && options.auth.storage) || localStorage; }
    async function readSession(){ try { var r = await store().getItem(storageKey); return r ? JSON.parse(r) : null; } catch(e){ return null; } }
    function backend(msg){ msg.online = navigator.onLine !== false; msg.page = String(location.pathname); return window.__fakeBackend(msg); }
    c.rpc = async function(fn, args){
      var s = await readSession();
      var r = await backend({op:'rpc', url:url, key:key, token: s && s.access_token, fn:String(fn), args: args || {}});
      return {data: r && 'data' in r ? r.data : null, error: (r && r.error) || null, status: (r && r.status) || 200};
    };
    c.auth.updateUser = async function(attrs, opts){
      var s = await readSession();
      var r = await backend({op:'auth.updateUser', url:url, key:key, token: s && s.access_token, attrs: attrs, opts: opts || {}});
      if (r && r.error) return {data:{user:null}, error:r.error};
      return {data:{user:r.user}, error:null};
    };
    return c;
  };
})();
"""


class F03Backend(T.FakeBackend):
    def __init__(self):
        super().__init__()
        self.admins = set()
        self.update_mode = "ok"  # ok | rate | exists | invalid
        self.update_calls = []
        self.summary = []

    def handle(self, device, msg):
        with self.lock:
            msg = msg or {}
            op = msg.get("op")
            if op == "query" and (msg.get("req") or {}).get("table") == "platform_admins":
                if msg.get("online") is False or self.mode == "down":
                    return {"error": dict(T.FETCH_ERR), "status": 0, "data": None}
                uid = self.tokens.get(msg.get("token"))
                req = msg.get("req")
                self.calls.append({"device": device, "op": "query", "msg": copy.deepcopy(msg), "url": msg.get("url")})
                if not uid:
                    return T._err("42501", "permission denied", 401)
                rows = [{"user_id": uid}] if uid in self.admins else []
                for f in req.get("filters") or []:
                    if f["col"] == "user_id" and f["op"] == "eq" and f["val"] != uid:
                        rows = []
                if req.get("maybe"):
                    return {"data": rows[0] if rows else None, "status": 200, "error": None}
                return {"data": rows, "status": 200, "error": None}
            if op == "rpc":
                self.calls.append({"device": device, "op": "rpc", "msg": copy.deepcopy(msg), "url": msg.get("url")})
                if msg.get("online") is False or self.mode == "down":
                    return {"error": dict(T.FETCH_ERR), "status": 0, "data": None}
                uid = self.tokens.get(msg.get("token"))
                if msg.get("fn") != "admin_trip_summary":
                    return T._err("PGRST202", "no function", 404)
                if uid not in self.admins:
                    return T._err("42501", "not_platform_admin", 403)
                return {"data": copy.deepcopy(self.summary), "status": 200, "error": None}
            if op == "auth.updateUser":
                self.calls.append({"device": device, "op": op, "msg": copy.deepcopy(msg), "url": msg.get("url")})
                self.update_calls.append(copy.deepcopy(msg))
                if msg.get("online") is False:
                    return {"error": {"name": "AuthRetryableFetchError", "message": "Failed to fetch", "status": 0}}
                uid = self.tokens.get(msg.get("token"))
                if self.update_mode == "rate":
                    return {"error": {"name": "AuthApiError", "message": "Email rate limit exceeded", "status": 429, "code": "over_email_send_rate_limit"}}
                if self.update_mode == "exists":
                    return {"error": {"name": "AuthApiError", "message": "A user with this email address has already been registered", "status": 422, "code": "email_exists"}}
                u = self.users[uid]
                u["new_email"] = msg["attrs"]["email"]
                return {"user": copy.deepcopy(u)}
            return super().handle(device, msg)


class FakeFn:
    """In-memory invite-manager (same contract as supabase/functions/invite-manager)."""

    def __init__(self, be):
        self.be, self.inv, self.reqs, self.mode = be, {}, [], "up"
        self.inviter = "אור"

    def add(self, email, name, status, sent_days_ago=2, count=1, opened=False):
        iid = str(uuid.uuid4())
        tok = secrets.token_urlsafe(32)[:43]
        exp = NOW + DAY * (7 - sent_days_ago) if status != "expired" else NOW - DAY * 5
        self.inv[iid] = {"id": iid, "email": email, "draft_name": name, "token": tok,
                         "created_at": (NOW - DAY * sent_days_ago).isoformat(), "last_sent_at": (NOW - DAY * sent_days_ago).isoformat(),
                         "send_count": count, "opened_at": (NOW - DAY).isoformat() if opened else None,
                         "expires_at": exp.isoformat(), "revoked_at": NOW.isoformat() if status == "revoked" else None,
                         "used_at": NOW.isoformat() if status == "used" else None}
        self.sync()
        return iid, tok

    def status(self, x):
        if x["revoked_at"]:
            return "revoked"
        return "expired" if datetime.datetime.fromisoformat(x["expires_at"]) <= datetime.datetime.now(datetime.timezone.utc) else "pending"

    def sync(self):
        trips = [s for s in self.be.summary if s["kind"] == "trip"]
        invs = [{"kind": "invite", "id": x["id"], "email": x["email"], "draft_name": x["draft_name"], "created_at": x["created_at"],
                 "last_sent_at": x["last_sent_at"], "send_count": x["send_count"], "opened_at": x["opened_at"],
                 "expires_at": x["expires_at"], "status": self.status(x)} for x in self.inv.values() if not x["used_at"]]
        self.be.summary = trips + invs

    def route(self, route, request):
        hdr = request.headers
        body = {}
        try:
            body = json.loads(request.post_data or "{}")
        except Exception:
            pass
        self.reqs.append({"method": request.method, "headers": dict(hdr), "body": body})
        if self.mode == "down":
            return route.abort()

        def reply(status, obj):
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(obj),
                                 headers={"Access-Control-Allow-Origin": "*"})
        a = body.get("action")
        if a == "check":
            x = next((v for v in self.inv.values() if v["token"] == body.get("token")), None)
            if not x or x["revoked_at"] or x["used_at"] or self.status(x) != "pending":
                return reply(200, {"valid": False})
            if not x["opened_at"]:
                x["opened_at"] = NOW.isoformat()
                self.sync()
            return reply(200, {"valid": True, "draft_name": x["draft_name"], "inviter_name": self.inviter, "expires_at": x["expires_at"]})
        tok = (hdr.get("authorization") or "").replace("Bearer ", "")
        uid = self.be.tokens.get(tok)
        if uid not in self.be.admins or hdr.get("apikey") != T.PT_KEY:
            return reply(403, {"error": "not_platform_admin"})
        if a == "create":
            email = body.get("email", "").strip().lower()
            ex = next((v for v in self.inv.values() if v["email"] == email and not v["revoked_at"] and not v["used_at"]), None)
            if ex and self.status(ex) == "pending":
                return reply(409, {"error": "invite_exists", "invite_id": ex["id"], "draft_name": ex["draft_name"], "expires_at": ex["expires_at"]})
            if ex:
                ex["revoked_at"] = NOW.isoformat()
            if self.mode == "mailfail":
                return reply(502, {"error": "email_failed"})
            iid, _ = self.add(email, body.get("draft_name"), "pending", sent_days_ago=0)
            return reply(200, {"ok": True, "invite_id": iid})
        x = self.inv.get(body.get("invite_id"))
        if not x:
            return reply(404, {"error": "invite_not_found"})
        if a == "resend":
            if x["revoked_at"] or x["used_at"]:
                return reply(409, {"error": "invite_closed"})
            x.update(token=secrets.token_urlsafe(32)[:43], send_count=x["send_count"] + 1, last_sent_at=NOW.isoformat(),
                     opened_at=None, expires_at=(NOW + 7 * DAY).isoformat())
            self.sync()
            return reply(200, {"ok": True})
        if a == "revoke":
            if x["used_at"]:
                return reply(409, {"error": "invite_used"})
            x["revoked_at"] = x["revoked_at"] or NOW.isoformat()
            self.sync()
            return reply(200, {"ok": True})
        return reply(400, {"error": "invalid_request"})


results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)[:300]) if (extra and not cond) else ""))


class Env:
    def __init__(self, browser, server):
        self.browser, self.server = browser, server
        self.be = F03Backend()
        self.fn = FakeFn(self.be)
        self.net, self.console = [], []

    def device(self, name="A"):
        ctx = self.browser.new_context(viewport=T.VIEWPORT, locale="he-IL", timezone_id="Asia/Jerusalem", service_workers="block")
        ctx.expose_binding("__fakeBackend", lambda source, msg, _n=name: self.be.handle(_n, msg))
        ctx.add_init_script(T.WATCH_JS)

        def _route(route, request):
            u = request.url
            p = urlparse(u)
            self.net.append(u)
            if p.hostname in ("127.0.0.1", "localhost"):
                return route.continue_()
            if u.startswith(FN_URL):
                return self.fn.route(route, request)
            if p.hostname == "cdn.jsdelivr.net" and "@supabase/supabase-js" in p.path:
                return route.fulfill(status=200, content_type="application/javascript; charset=utf-8", body=FAKE_F03)
            return route.abort()
        ctx.route("**/*", _route)
        page = ctx.new_page()
        page.on("console", lambda m: self.console.append(m.text))
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page._errs = errs
        return ctx, page


def raw_storage(page):
    return page.evaluate("""()=>{const S=Storage.prototype,L=Object.getOwnPropertyDescriptor(S,'length');
      const out={};const n=localStorage.length;for(let i=0;i<n;i++){const k=localStorage.key(i);out[k]=localStorage.getItem(k)}
      const ss={};for(let i=0;i<sessionStorage.length;i++){const k=sessionStorage.key(i);ss[k]=sessionStorage.getItem(k)}return {ls:out,ss}}""")


def raw_ls_all(ctx, server):
    p = ctx.new_page()
    p.goto(server.origin + T.BLANK_PATH)
    o = p.evaluate(T.RAW_LS_DUMP)
    p.close()
    return o


def geometry(page, root_sel):
    return page.evaluate("""(sel)=>{const r=document.querySelector(sel);if(!r)return null;
      const small=[];for(const b of r.querySelectorAll('button,a,input')){const s=getComputedStyle(b);if(s.display==='none'||s.visibility==='hidden'||b.offsetParent===null&&s.position!=='fixed')continue;
        const q=b.getBoundingClientRect();if(q.width===0&&q.height===0)continue;if(q.width<44||q.height<44)small.push([b.id||b.className||b.tagName,(b.innerText||b.getAttribute('aria-label')||'').slice(0,24),Math.round(q.width),Math.round(q.height)])}
      return {scrollW:document.scrollingElement.scrollWidth,innerW:innerWidth,elScroll:r.scrollWidth>r.clientWidth+1,dir:getComputedStyle(r).direction,small}}""", root_sel)


def chrome_visible(page):
    return page.evaluate("""()=>['.tabs','#chatFab','#globalFxBtn'].map(s=>{const e=document.querySelector(s);if(!e)return false;
      const st=getComputedStyle(e);return st.display!=='none'&&st.visibility!=='hidden'&&e.getBoundingClientRect().height>0})""")


def seed_summary(env):
    env.be.summary = [
        {"kind": "trip", "id": str(uuid.uuid4()), "name": "ויאטנם 2026", "start_date": "2026-09-15", "end_date": "2026-11-06",
         "status": "active", "managers": ["אור"], "countries": ["VN"], "member_count": 5},
        {"kind": "trip", "id": str(uuid.uuid4()), "name": "תאילנד בחנוכה", "start_date": "2026-12-14", "end_date": "2026-12-28",
         "status": "active", "managers": ["מיכל", "רון"], "countries": ["TH"], "member_count": 4},
        {"kind": "trip", "id": str(uuid.uuid4()), "name": "פורטוגל", "start_date": "2026-08-01", "end_date": "2026-08-12",
         "status": "ended", "managers": ["שירה"], "countries": ["PT", "ES"], "member_count": 3},
    ]


def linked(env, admin=False, email=None):
    be = env.be
    tid = be.add_trip("QA trip")
    be.set_kv(tid, "tripname", "ויאטנם 2026")
    be.auto_link = {"trip_id": tid, "role": "editor", "status": "active", "display_name": "אור"}
    ctx, page = env.device()
    page.goto(env.server.origin + T.APP_PATH, wait_until="domcontentloaded")
    st = T.wait_ready(page)
    uid = page.evaluate("()=>window.__PT.uid")
    if email:
        be.users[uid]["email"] = email
        be.users[uid]["is_anonymous"] = False
    if admin:
        be.admins.add(uid)
    T.ensure_user(page)
    return ctx, page, uid, st


def open_more(page):
    T.tab(page, "more")
    page.wait_for_timeout(600)


def main():
    server = T.Server()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            run(browser, server)
        finally:
            browser.close()
            server.stop()
    n_ok = sum(1 for _, ok in results if ok)
    print("\n%d/%d pass" % (n_ok, len(results)))
    return 0 if n_ok == len(results) else 1


def run(browser, server):
    src = (Path(os.environ["APP_DIR"]) / "index.html").read_text(encoding="utf-8")
    assert src.count("const ADMIN_FEATURE='on';") == 1
    src_off = src.replace("const ADMIN_FEATURE='on';", "const ADMIN_FEATURE='off';")

    # ---------------------------------------------------------------- 1. non-admin, flag on
    env = Env(browser, server)
    ctx, page, uid, st = linked(env)
    check("on/non-admin: app started", st == "app", st)
    check("on/__PT.version is 3.0.0-alpha.3", page.evaluate("()=>window.__PT.version") == "3.0.0-alpha.3")
    check("on/APP_VERSION is 3.0.0-alpha.3", page.evaluate("()=>APP_VERSION") == "3.0.0-alpha.3")
    open_more(page)
    txt = page.inner_text("#more")
    check("on/non-admin: 'החשבון שלי' section at the top of עוד", txt.strip().split("\n")[1].strip() == "החשבון שלי" if "\n" in txt else False, txt[:120])
    check("on/non-admin: row 'קישור מייל לחשבון'", page.locator("#ptLinkEmailRow").is_visible())
    check("on/non-admin: no 'ניהול מערכת' row", not page.locator("#ptAdminRow").is_visible())
    check("on/non-admin: existing עוד rows still there", all(page.locator(s).is_visible() for s in ("#documentsBtn", "#reservationsBtn", "#moreSyncCard")))
    g = geometry(page, "#ptAccount")
    check("on/more: no h-scroll, RTL, new rows >=44px", g and g["scrollW"] <= g["innerW"] + 1 and g["dir"] == "rtl" and not g["small"], g)
    page.screenshot(path=str(OUT / "f03_more_nonadmin.png"))
    page.evaluate("()=>{location.hash='#admin'}")
    page.wait_for_timeout(1200)
    check("on/non-admin: #admin typed in the address bar shows nothing", page.locator("#admin").count() == 0 and chrome_visible(page) == [True, True, True])
    check("on/non-admin: #admin removed from the address bar", page.evaluate("()=>location.hash") == "")
    rpc_calls = [c for c in env.be.calls if c["op"] == "rpc"]
    check("on/non-admin: admin_trip_summary never called", not rpc_calls)

    # link-email sheet
    page.click("#ptLinkEmailRow")
    page.wait_for_selector("#ptLinkEmail")
    check("link: sheet copy", "קישור מייל לחשבון" in page.inner_text("#ptSheet") and "שליחת מייל אימות" in page.inner_text("#ptSheet")
          and "בטלפון הזה" in page.inner_text("#ptSheet"))
    g = geometry(page, "#ptSheetLayer")
    check("link: sheet 390px, RTL, touch >=44px", g and g["scrollW"] <= g["innerW"] + 1 and not g["elScroll"] and not g["small"], g)
    page.screenshot(path=str(OUT / "f03_link_email.png"))
    page.fill("#ptLinkEmail", "not-an-email")
    page.click("#ptLinkSend")
    check("link: invalid email → Hebrew error, no call", page.inner_text("#ptLinkErr") == "כתובת המייל לא תקינה." and not env.be.update_calls)
    for mode, expect in (("rate", "נשלחו יותר מדי מיילים. נסו שוב בעוד כמה דקות."), ("exists", "המייל הזה כבר מקושר לחשבון אחר.")):
        env.be.update_mode = mode
        page.fill("#ptLinkEmail", "Name@Gmail.com ")
        page.click("#ptLinkSend")
        page.wait_for_function("()=>document.getElementById('ptLinkErr').textContent.length>0", timeout=5000)
        check("link: %s → mapped Hebrew error" % mode, page.inner_text("#ptLinkErr") == expect, page.inner_text("#ptLinkErr"))
    ctx.set_offline(True)
    page.click("#ptLinkSend")
    page.wait_for_timeout(300)
    check("link: offline → Hebrew error", page.inner_text("#ptLinkErr") == "אין חיבור. נסו שוב כשהרשת תחזור.", page.inner_text("#ptLinkErr"))
    ctx.set_offline(False)
    env.be.update_mode = "ok"
    n0 = len(env.be.update_calls)
    page.click("#ptLinkSend")
    page.wait_for_selector("#ptLinkOk", timeout=5000)
    call = env.be.update_calls[-1]
    check("link: updateUser({email}) with normalized email", call["attrs"] == {"email": "name@gmail.com"} and len(env.be.update_calls) == n0 + 1, call)
    check("link: emailRedirectTo = app URL", call["opts"].get("emailRedirectTo") == server.origin + T.APP_PATH, call["opts"])
    check("link: success text", page.inner_text("#ptLinkOk").replace("⁦", "").replace("⁩", "") == "שלחנו מייל ל-name@gmail.com. פתחו את הקישור בטלפון הזה.", page.inner_text("#ptLinkOk"))
    page.click("#ptLinkSend")  # "סגירה"
    page.wait_for_timeout(200)
    check("link: row shows pending address after send", "name@gmail.com" in page.inner_text("#ptAcctCard"))
    check("link: no page errors", not page._errs, page._errs)
    ctx.close()

    # ---------------------------------------------------------------- 2. admin with linked email
    env = Env(browser, server)
    seed_summary(env)
    i_p, tok_p = env.fn.add("dana@example.com", "יפן 2027", "pending", sent_days_ago=2)
    env.fn.add("yoav@example.com", "יוון 2027", "expired", sent_days_ago=12)
    env.fn.add("old@example.com", None, "revoked", sent_days_ago=4)
    ctx, page, uid, st = linked(env, admin=True, email="or@example.com")
    page.reload(wait_until="domcontentloaded")
    T.wait_ready(page)
    T.ensure_user(page)
    open_more(page)
    page.wait_for_selector("#ptAdminRow:not([hidden])", timeout=8000)
    acct = page.inner_text("#ptAcctCard")
    check("admin: 'מייל מקושר' + address + badge 'מקושר'", "מייל מקושר" in acct and "or@example.com" in acct and "מקושר" in acct)
    check("admin: 'ניהול מערכת' + badge 'סופר-אדמין'", "ניהול מערכת" in acct and "סופר-אדמין" in acct)
    page.screenshot(path=str(OUT / "f03_more_admin.png"))
    page.click("#ptAdminRow")
    page.wait_for_selector("#admin .pt-icard", timeout=8000)
    check("admin: trip chrome hidden (tabs, chat FAB, FX pill)", chrome_visible(page) == [False, False, False], chrome_visible(page))
    check("admin: address bar #admin", page.evaluate("()=>location.hash") == "#admin")
    seg = page.locator("#admin .pt-seg button").all_inner_texts()
    check("admin: filter counts הכל 5 / ממתינות 2 / פעילות 2 / הסתיימו 1", seg == ["הכל · 5", "ממתינות · 2", "פעילות · 2", "הסתיימו · 1"], seg)
    names = page.locator("#admin .pt-nm").all_inner_texts()
    check("admin: sort pending/expired (newest sent first) → active by start → ended", names == ["יפן 2027", "יוון 2027", "ויאטנם 2026", "תאילנד בחנוכה", "פורטוגל"], names)
    cards = page.locator("#admin .pt-icard").all_inner_texts()
    check("admin: pending card text", "ממתין למנהל" in cards[0] and "dana@example.com" in cards[0] and "נשלחה לפני יומיים · בתוקף עוד 5 ימים" in cards[0], cards[0])
    check("admin: expired card text + buttons", "פג תוקף" in cards[1] and re.search(r"פג ב-\d\d/\d\d", cards[1]) and "שליחה מחדש" in cards[1] and "הסרה מהרשימה" in cards[1], cards[1])
    check("admin: trip card text", "🇻🇳" in cards[2] and "מנהלים: אור" in cards[2] and "ויאטנם · 15/09–06/11/2026 · 5 חברים" in cards[2] and "פעילה" in cards[2], cards[2])
    check("admin: ended card (2 flags, 2 countries)", "🇵🇹🇪🇸" in cards[4] and "פורטוגל, ספרד" in cards[4] and "הסתיימה" in cards[4], cards[4])
    check("admin: email LTR", page.evaluate("()=>getComputedStyle(document.querySelector('#admin .pt-l1.pt-ltr')).direction") == "ltr")
    check("admin: revoked hidden until toggled", "old@example.com" not in page.inner_text("#admin"))
    page.click("#ptRevokedToggle")
    check("admin: toggle shows revoked", "old@example.com" in page.inner_text("#admin") and "הזמנות שבוטלו · 1" in page.inner_text("#admin"))
    page.click("#ptRevokedToggle")
    page.click("#admin .pt-seg button:nth-child(2)")
    check("admin: filter ממתינות = pending + expired", page.locator("#admin .pt-nm").all_inner_texts() == ["יפן 2027", "יוון 2027"])
    page.click("#admin .pt-seg button:nth-child(4)")
    check("admin: filter הסתיימו", page.locator("#admin .pt-nm").all_inner_texts() == ["פורטוגל"])
    page.click("#admin .pt-seg button:nth-child(1)")
    check("admin: privacy footnote", "מוצגים רק שם, מנהלים, יעד, תאריכים ומספר חברים. התוכן פרטי." in page.inner_text("#admin"))
    g = geometry(page, "#admin")
    check("admin: 390px no h-scroll, RTL, touch >=44px", g and g["scrollW"] <= g["innerW"] + 1 and not g["elScroll"] and g["dir"] == "rtl" and not g["small"], g)
    page.screenshot(path=str(OUT / "f03_admin.png"), full_page=True)

    # invite sheet: success
    page.click("#ptInviteOpen")
    page.wait_for_selector("#ptInvEmail")
    g = geometry(page, "#ptSheetLayer")
    check("invite sheet: 390px, touch >=44px", g and not g["elScroll"] and not g["small"], g)
    page.fill("#ptInvEmail", "bad")
    page.click("#ptInvSend")
    check("invite: invalid email blocked client-side", page.inner_text("#ptInvEmailErr") == "כתובת המייל לא תקינה.")
    n_req = len(env.fn.reqs)
    page.fill("#ptInvEmail", " New@Example.com")
    page.fill("#ptInvName", "איסלנד")
    page.click("#ptInvSend")
    page.wait_for_function("()=>{const t=document.getElementById('ptToast');return t&&t.textContent.includes('ההזמנה נשלחה ל-')}", timeout=5000)
    req = env.fn.reqs[n_req]
    check("invite: create call carries Bearer JWT + apikey, normalized email, draft name",
          req["headers"].get("authorization", "").startswith("Bearer fake-at.") and req["headers"].get("apikey") == T.PT_KEY
          and req["body"] == {"action": "create", "email": "new@example.com", "draft_name": "איסלנד"}, req)
    check("invite: toast text", page.inner_text("#ptToast") == "ההזמנה נשלחה ל-new@example.com")
    page.wait_for_function("()=>document.querySelector('#admin').innerText.includes('new@example.com')", timeout=5000)
    check("invite: list refreshed (count 6)", page.locator("#admin .pt-seg button").first.inner_text() == "הכל · 6")
    # invite_exists
    page.click("#ptInviteOpen")
    page.wait_for_selector("#ptInvEmail")
    page.fill("#ptInvEmail", "DANA@example.com")
    page.click("#ptInvSend")
    page.wait_for_selector("#ptInvExists:not([hidden])", timeout=5000)
    ex = page.inner_text("#ptInvExists")
    check("invite_exists: amber notice with name + days left", "יפן 2027" in ex and "בתוקף עוד 5 ימים" in ex and "לשלוח שוב את ההזמנה הקיימת" in ex, ex)
    check("invite_exists: submit disabled while the email matches", page.locator("#ptInvSend").is_disabled())
    page.screenshot(path=str(OUT / "f03_invite_exists.png"))
    page.fill("#ptInvEmail", "other@example.com")
    check("invite_exists: submit enabled for another email", not page.locator("#ptInvSend").is_disabled())
    page.fill("#ptInvEmail", "dana@example.com")
    page.click("#ptInvExistingBtn")
    page.wait_for_selector("#ptActResend")
    check("invite_exists → actions sheet of that invite", "dana@example.com" in page.inner_text("#ptSheet"))
    # actions sheet
    act = page.inner_text("#ptSheet")
    check("actions: rows", all(s in act for s in ("נשלחה לראשונה", "שליחה אחרונה", "פעם אחת", "בתוקף עד", "הקישור נפתח", "עדיין לא", "שליחה חוזרת", "ביטול ההזמנה")), act)
    g = geometry(page, "#ptSheetLayer")
    check("actions: 390px, touch >=44px", g and not g["elScroll"] and not g["small"], g)
    page.screenshot(path=str(OUT / "f03_actions.png"))
    page.click("#ptActResend")
    page.wait_for_function("()=>document.getElementById('ptToast').textContent.includes('נשלחה שוב')", timeout=5000)
    check("actions: resend → toast + send_count 2", env.fn.inv[i_p]["send_count"] == 2 and env.fn.reqs[-1]["body"] == {"action": "resend", "invite_id": i_p})
    page.wait_for_timeout(400)
    page.click('#admin [data-pt-more="%s"]' % i_p)
    page.wait_for_selector("#ptActRevoke")
    check("actions: send count now '2 פעמים'", "2 פעמים" in page.inner_text("#ptSheet"))
    page.click("#ptActRevoke")
    check("revoke: confirm step shown, nothing sent yet", page.locator("#ptConfirm").is_visible() and env.fn.reqs[-1]["body"]["action"] == "resend")
    page.click("#ptConfirmNo")
    check("revoke: 'חזרה' cancels", page.locator("#ptConfirm").count() == 0 and page.locator("#ptActRevoke").is_visible())
    page.click("#ptActRevoke")
    page.click("#ptConfirmYes")
    page.wait_for_function("()=>document.getElementById('ptToast').textContent==='ההזמנה בוטלה'", timeout=5000)
    check("revoke: invite revoked, list updated", env.fn.inv[i_p]["revoked_at"] and "הזמנות שבוטלו · 2" in page.inner_text("#admin"))
    # expired: remove from list
    exp_id = next(k for k, v in env.fn.inv.items() if v["email"] == "yoav@example.com")
    page.click('#admin [data-pt-remove="%s"]' % exp_id)
    page.wait_for_function("()=>document.getElementById('ptToast').textContent==='ההזמנה הוסרה מהרשימה'", timeout=5000)
    check("expired: 'הסרה מהרשימה' = revoke", env.fn.inv[exp_id]["revoked_at"] is not None)
    # email failure
    env.fn.mode = "mailfail"
    page.click("#ptInviteOpen")
    page.fill("#ptInvEmail", "x@example.com")
    page.click("#ptInvSend")
    page.wait_for_function("()=>document.getElementById('ptInvErr').textContent.length>0", timeout=5000)
    check("invite: email_failed → Hebrew", page.inner_text("#ptInvErr") == "שליחת המייל נכשלה, וההזמנה לא נשמרה. נסו שוב.", page.inner_text("#ptInvErr"))
    env.fn.mode = "up"
    page.click("#ptSheet [data-pt-close]")
    # offline
    ctx.set_offline(True)
    page.evaluate("()=>window.dispatchEvent(new Event('offline'))")
    page.wait_for_timeout(300)
    check("offline: notice shown", page.locator("#ptAdminOffline").is_visible() and page.inner_text("#ptAdminOffline") == "אין חיבור — הרשימה תתעדכן כשהרשת תחזור")
    check("offline: actions disabled", page.locator("#ptInviteOpen").is_disabled() and all(b.is_disabled() for b in page.locator("#admin [data-pt-more],#admin [data-pt-resend],#admin [data-pt-remove]").all()))
    page.screenshot(path=str(OUT / "f03_admin_offline.png"))
    ctx.set_offline(False)
    page.evaluate("()=>window.dispatchEvent(new Event('online'))")
    page.wait_for_timeout(800)
    check("online again: list reloads, notice gone", page.locator("#ptAdminOffline").count() == 0)
    dump = json.dumps(raw_ls_all(ctx, server), ensure_ascii=False)
    check("admin list never cached in storage", "פורטוגל" not in dump and "תאילנד בחנוכה" not in dump and "dana@example.com" not in dump)
    # back
    page.click("#ptAdminBack")
    page.wait_for_timeout(700)
    check("back: returns to עוד with trip chrome", page.locator("#admin").count() == 0 and chrome_visible(page) == [True, True, True]
          and page.evaluate("()=>document.getElementById('more').classList.contains('active')"))
    check("back: hash cleared", page.evaluate("()=>location.hash") == "")
    page.click("#ptAdminRow")
    page.wait_for_selector("#admin .pt-icard")
    page.go_back()
    page.wait_for_timeout(700)
    check("browser back closes admin", page.locator("#admin").count() == 0 and chrome_visible(page) == [True, True, True])
    check("admin: no page errors", not page._errs, page._errs)
    ctx.close()

    # ---------------------------------------------------------------- 3. landing
    env = Env(browser, server)
    i_v, tok_v = env.fn.add("dana@example.com", "יפן 2027", "pending", sent_days_ago=2)
    _, tok_exp = env.fn.add("e@example.com", "x", "expired")
    _, tok_rev = env.fn.add("r@example.com", "x", "revoked")
    _, tok_used = env.fn.add("u@example.com", "x", "used")
    ctx, page = env.device("L")
    page.goto(server.origin + T.APP_PATH + "#invite=" + tok_v, wait_until="domcontentloaded")
    page.wait_for_selector("#ptInvite h1", timeout=8000)
    page.wait_for_function("()=>document.getElementById('ptInviteTitle').textContent!=='בודקים את ההזמנה…'", timeout=8000)
    t = page.inner_text("#ptInvite")
    check("landing valid: title/inviter/name/expiry/steps/disabled button",
          "ההזמנה שלך אומתה" in t and "אור הזמין אותך לנהל את הקבוצה \"יפן 2027\"" in t and re.search(r"בתוקף עד \d\d/\d\d/\d{4}", t)
          and "קישור הצטרפות לשליחה בווטסאפ" in t and page.locator("#ptInvite .pt-soon").is_disabled(), t)
    check("landing: token removed from the address bar", page.evaluate("()=>location.href").find(tok_v) < 0 and page.evaluate("()=>location.hash") == "")
    check("landing: no trip chrome, main script not started",
          chrome_visible(page) == [False, False, False] and page.evaluate("()=>typeof loadState") == "undefined"
          and page.evaluate("()=>window.__PT.phase") == "invite-landing")
    check("landing: no anonymous sign-in / trip lookup", not env.be.ops("auth.signInAnonymously") and not env.be.queries("trip_members"))
    check("landing: opened_at set", env.fn.inv[i_v]["opened_at"] is not None)
    check("landing: check sent without JWT", "authorization" not in env.fn.reqs[-1]["headers"] and env.fn.reqs[-1]["body"]["action"] == "check")
    stor = raw_ls_all(ctx, server)
    s2 = page.evaluate("()=>{const o={};for(let i=0;i<sessionStorage.length;i++){const k=sessionStorage.key(i);o[k]=sessionStorage.getItem(k)}return o}")
    check("landing: token not in localStorage/sessionStorage", tok_v not in json.dumps(stor) and tok_v not in json.dumps(s2))
    check("landing: token not in console", not any(tok_v in m for m in env.console))
    g = geometry(page, "#ptInvite")
    check("landing: 390px no h-scroll, RTL", g and g["scrollW"] <= g["innerW"] + 1 and g["dir"] == "rtl" and not g["small"], g)
    page.screenshot(path=str(OUT / "f03_landing_valid.png"))
    invalid_html = []
    for label, tok in (("expired", tok_exp), ("revoked", tok_rev), ("used", tok_used), ("malformed", "abc"),
                       ("random", secrets.token_urlsafe(32)[:43]), ("replaced", tok_v)):
        if label == "replaced":
            page.goto(server.origin + T.APP_PATH, wait_until="domcontentloaded")
            env.fn.inv[i_v].update(token=secrets.token_urlsafe(32)[:43])
        page.goto(server.origin + T.BLANK_PATH)
        page.goto(server.origin + T.APP_PATH + "#invite=" + tok, wait_until="domcontentloaded")
        page.wait_for_function("()=>{const h=document.getElementById('ptInviteTitle');return h&&h.textContent!=='בודקים את ההזמנה…'}", timeout=8000)
        invalid_html.append(page.inner_html("#ptInvite"))
        check("landing invalid (%s): generic message" % label, "הקישור כבר לא בתוקף" in page.inner_text("#ptInvite"))
    check("landing invalid: byte-identical page for all 6 cases", len(set(invalid_html)) == 1)
    page.screenshot(path=str(OUT / "f03_landing_invalid.png"))
    env.fn.mode = "down"
    page.goto(server.origin + T.BLANK_PATH)
    page.goto(server.origin + T.APP_PATH + "#invite=" + tok_exp, wait_until="domcontentloaded")
    page.wait_for_selector("#ptInviteRetry", timeout=12000)
    check("landing network error: 'אין חיבור. נסו שוב' + retry", page.inner_text("#ptInviteTitle") == "אין חיבור. נסו שוב")
    env.fn.mode = "up"
    page.click("#ptInviteRetry")
    page.wait_for_function("()=>document.getElementById('ptInviteTitle').textContent==='הקישור כבר לא בתוקף'", timeout=8000)
    check("landing retry works", True)
    check("landing: no page errors", not page._errs, page._errs)
    ctx.close()

    # ---------------------------------------------------------------- 4. flag off = alpha.2
    env = Env(browser, server)
    server.overrides[T.APP_PATH] = src_off.encode("utf-8")
    i_v, tok_v = env.fn.add("dana@example.com", "יפן 2027", "pending")
    ctx, page, uid, st = linked(env, admin=True, email="or@example.com")
    open_more(page)
    check("off: no 'החשבון שלי' section", page.locator("#ptAccount").count() == 0 and "החשבון שלי" not in page.inner_text("#more"))
    check("off: no __ptF03, no platform_admins query", page.evaluate("()=>typeof window.__ptF03") == "undefined"
          and not [c for c in env.be.calls if (c["msg"].get("req") or {}).get("table") == "platform_admins"])
    page.evaluate("()=>{location.hash='#admin'}")
    page.wait_for_timeout(600)
    check("off: #admin does nothing", page.locator("#admin").count() == 0 and chrome_visible(page) == [True, True, True])
    ctx.close()
    ctx, page = env.device("OFFL")
    n_fn = len(env.fn.reqs)
    page.goto(server.origin + T.APP_PATH + "#invite=" + tok_v, wait_until="domcontentloaded")
    st = T.wait_ready(page)
    check("off: #invite= falls through to normal boot", st in ("app", "nc") and page.locator("#ptInvite").count() == 0, st)
    check("off: no invite-manager request", len(env.fn.reqs) == n_fn)
    check("off: no page errors", not page._errs, page._errs)
    server.overrides.clear()
    ctx.close()

    # ---------------------------------------------------------------- 5. local-only: nothing F03
    env = Env(browser, server)
    server.overrides[T.APP_PATH] = src.replace("const STORAGE_BACKEND='cloud';", "const STORAGE_BACKEND='local-only';").encode("utf-8")
    ctx, page = env.device("LO")
    page.goto(server.origin + T.APP_PATH, wait_until="domcontentloaded")
    T.wait_ready(page)
    T.ensure_user(page)
    open_more(page)
    check("local-only: no account section", page.locator("#ptAccount").count() == 0)
    server.overrides.clear()
    ctx.close()


if __name__ == "__main__":
    sys.exit(main())
