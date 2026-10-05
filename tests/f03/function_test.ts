// Plan_Travel F03 QA · invite-manager black-box tests (Deno) · version 1.0.0
// CHANGE 2026-10-05 F03-QA: first version (no previous version).
//
// Written from docs/F03_spec.md §3/§5 only (the function source was NOT read). Case ids match tests/f03/function_cases.md.
//
// How it runs
// - Target: the function's exported handler, imported from supabase/functions/invite-manager/index.ts. Accepted export
//   shapes (first found wins): `handler(req)`, `default(req)` (function), `default.fetch(req)`. If the module only calls
//   Deno.serve and exports nothing, set INVITE_MANAGER_URL to a served copy (`supabase functions serve`) instead —
//   then the mail-dependent cases are ignored (no way to capture mail over HTTP).
// - Mail capture: the module must expose a test seam to replace the SMTP transport. Accepted names (first found):
//   setMailer(fn) · __setMailer(fn) · setTransport(fn) · __setTransport(fn). fn receives one message object
//   {to, subject, html, text, from?} and may throw to simulate an SMTP failure. ASSUMPTION for the dev notes to confirm.
// - Database: a real Postgres behind PostgREST + GoTrue with migrations 0001–0008 (local `supabase start`, or a branch).
//   Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_ANON_KEY (the function itself reads the same env).
//   Test data is created with the service role and removed at the end (emails under @f03qa.test).
// - Every case is `ignore`d (reported, not failed) when its prerequisites are missing.
//
// Run: deno test --allow-env --allow-net --allow-read tests/f03/function_test.ts

const URL_ = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const ANON = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const REMOTE = Deno.env.get("INVITE_MANAGER_URL") ?? "";
const APP_URL = Deno.env.get("APP_URL") ?? "https://orsela.github.io/Plan_Travel/app/";
const FN_PATH = new URL("../../supabase/functions/invite-manager/index.ts", import.meta.url).href;
const DOMAIN = "f03qa.test";

type Msg = { to?: string | string[]; subject?: string; html?: string; text?: string; from?: string };
type Handler = (req: Request) => Promise<Response> | Response;

// ---------------------------------------------------------------------------------------------------------------
// target discovery
// ---------------------------------------------------------------------------------------------------------------
let handler: Handler | null = null;
let setMailer: ((fn: (m: Msg) => Promise<void> | void) => void) | null = null;
let loadError = "";
const sent: Msg[] = [];
let failMail = false;

if (URL_ && SERVICE) {
  try {
    // deno-lint-ignore no-explicit-any
    const mod: any = await import(FN_PATH);
    const h = mod.handler ?? (typeof mod.default === "function" ? mod.default : mod.default?.fetch?.bind(mod.default));
    if (typeof h === "function") handler = h;
    const sm = mod.setMailer ?? mod.__setMailer ?? mod.setTransport ?? mod.__setTransport;
    if (typeof sm === "function") setMailer = sm;
  } catch (e) {
    loadError = String(e);
  }
}
if (setMailer) {
  setMailer(async (m: Msg) => {
    if (failMail) throw new Error("qa: simulated SMTP failure");
    sent.push(m);
  });
}
const HAVE_DB = !!(URL_ && SERVICE && ANON);
const HAVE_TARGET = !!(handler || REMOTE);
const HAVE_MAIL = !!(handler && setMailer);
const CAN = HAVE_DB && HAVE_TARGET;
const CAN_MAIL = CAN && HAVE_MAIL;
if (!CAN || !CAN_MAIL) {
  console.warn(`[f03 fn] db=${HAVE_DB} target=${HAVE_TARGET} mailSeam=${HAVE_MAIL} ${loadError ? "load error: " + loadError : ""}`);
}

// ---------------------------------------------------------------------------------------------------------------
// tiny assert helpers (no imports, so the file also type-checks offline)
// ---------------------------------------------------------------------------------------------------------------
function ok(c: unknown, msg: string): asserts c {
  if (!c) throw new Error("FAIL: " + msg);
}
function eq<T>(a: T, b: T, msg: string) {
  if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`FAIL: ${msg}\n  actual:   ${JSON.stringify(a)}\n  expected: ${JSON.stringify(b)}`);
}
async function sha256hex(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------------------------------------------------------------------------------------------------------------
// calling the function
// ---------------------------------------------------------------------------------------------------------------
type Res = { status: number; body: Record<string, unknown>; raw: string; headers: Headers };
async function call(body: unknown, jwt: string | null, extra: Record<string, string> = {}): Promise<Res> {
  const headers: Record<string, string> = { "Content-Type": "application/json", apikey: ANON, Origin: "https://orsela.github.io", ...extra };
  if (jwt) headers.Authorization = "Bearer " + jwt;
  const req = new Request(REMOTE || `${URL_}/functions/v1/invite-manager`, { method: "POST", headers, body: JSON.stringify(body) });
  const r = handler ? await handler(req) : await fetch(req);
  const raw = await r.text();
  let parsed: Record<string, unknown> = {};
  try { parsed = JSON.parse(raw); } catch { /* not json */ }
  return { status: r.status, body: parsed, raw, headers: r.headers };
}
async function preflight(origin: string): Promise<Response> {
  const req = new Request(REMOTE || `${URL_}/functions/v1/invite-manager`, {
    method: "OPTIONS",
    headers: { Origin: origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization, apikey, content-type" },
  });
  const r = handler ? await handler(req) : await fetch(req);
  await r.body?.cancel();
  return r;
}
const errCode = (r: Res) => String((r.body.error as string) ?? (r.body.code as string) ?? "");

// ---------------------------------------------------------------------------------------------------------------
// database access (service role, PostgREST / GoTrue admin)
// ---------------------------------------------------------------------------------------------------------------
const svc = { apikey: SERVICE, Authorization: "Bearer " + SERVICE, "Content-Type": "application/json" };
async function rest(method: string, path: string, body?: unknown, prefer = "return=representation") {
  const r = await fetch(`${URL_}/rest/v1/${path}`, { method, headers: { ...svc, Prefer: prefer }, body: body === undefined ? undefined : JSON.stringify(body) });
  const t = await r.text();
  if (!r.ok) throw new Error(`rest ${method} ${path}: ${r.status} ${t}`);
  return t ? JSON.parse(t) : null;
}
// deno-lint-ignore no-explicit-any
async function invites(email: string): Promise<any[]> {
  return await rest("GET", `manager_invites?email=eq.${encodeURIComponent(email)}&order=created_at.asc`);
}
async function patchInvite(id: string, fields: Record<string, unknown>) {
  await rest("PATCH", `manager_invites?id=eq.${id}`, fields);
}
const users: string[] = [];
async function user(tag: string): Promise<{ id: string; jwt: string }> {
  const email = `${tag}-${crypto.randomUUID().slice(0, 8)}@${DOMAIN}`;
  const password = crypto.randomUUID();
  const c = await fetch(`${URL_}/auth/v1/admin/users`, { method: "POST", headers: svc, body: JSON.stringify({ email, password, email_confirm: true }) });
  const u = await c.json();
  ok(c.ok, "create user: " + JSON.stringify(u));
  users.push(u.id);
  const t = await fetch(`${URL_}/auth/v1/token?grant_type=password`, { method: "POST", headers: { apikey: ANON, "Content-Type": "application/json" }, body: JSON.stringify({ email, password }) });
  const s = await t.json();
  ok(t.ok, "sign in: " + JSON.stringify(s));
  return { id: u.id, jwt: s.access_token };
}
const trips: string[] = [];
async function memberOf(uid: string, role: string, display: string) {
  const [t] = await rest("POST", "trips", { name: "F03 QA trip", start_date: "2026-09-15", end_date: "2026-10-06" });
  trips.push(t.id);
  await rest("POST", "trip_members", { trip_id: t.id, user_id: uid, display_name: display, role, status: "active", approved_at: new Date().toISOString() });
}
let ADMIN: { id: string; jwt: string } | null = null;
async function admin() {
  if (ADMIN) return ADMIN;
  ADMIN = await user("admin");
  await rest("POST", "platform_admins", { user_id: ADMIN.id });
  await memberOf(ADMIN.id, "manager", "אור QA");
  return ADMIN;
}
const addr = (tag: string) => `${tag}-${crypto.randomUUID().slice(0, 8)}@${DOMAIN}`;
function tokenFrom(m: Msg): string {
  const s = `${m.html ?? ""}\n${m.text ?? ""}`;
  const x = s.match(/#invite=([A-Za-z0-9_-]+)/);
  ok(x, "no #invite=<token> link in the email");
  return x![1];
}
async function create(email: string, draft_name?: string | null) {
  const a = await admin();
  const before = sent.length;
  const r = await call({ action: "create", email, ...(draft_name !== undefined ? { draft_name } : {}) }, a.jwt);
  const mails = sent.slice(before);
  return { r, mails, token: mails.length ? tokenFrom(mails[mails.length - 1]) : "" };
}
async function check(token: unknown) {
  return await call({ action: "check", token }, null);
}

function t(name: string, fn: () => Promise<void>, needsMail = true) {
  Deno.test({ name, ignore: needsMail ? !CAN_MAIL : !CAN, sanitizeOps: false, sanitizeResources: false, fn });
}

// ---------------------------------------------------------------------------------------------------------------
// C1 · access
// ---------------------------------------------------------------------------------------------------------------
for (const role of ["manager", "editor", "viewer", "none"]) {
  t(`FN-01 non-admin (${role}) create/resend/revoke → 403 not_platform_admin`, async () => {
    const u = await user("nonadmin-" + role);
    if (role !== "none") await memberOf(u.id, role, "QA " + role);
    const target = addr("target");
    const { r: made } = await create(target, "x");
    ok(made.status === 200, "admin create failed: " + made.raw);
    const [row] = await invites(target);
    for (const body of [{ action: "create", email: addr("x") }, { action: "resend", invite_id: row.id }, { action: "revoke", invite_id: row.id }]) {
      const r = await call(body, u.jwt);
      eq(r.status, 403, `${body.action} status`);
      eq(errCode(r), "not_platform_admin", `${body.action} error code`);
    }
    const [after] = await invites(row.email);
    eq([after.revoked_at, after.send_count, after.token_hash], [row.revoked_at, row.send_count, row.token_hash], "row changed by a non-admin");
  });
}
t("FN-02 no JWT / anon key as bearer / garbage JWT → refused, nothing returned", async () => {
  for (const jwt of [null, ANON, "not.a.jwt"]) {
    const r = await call({ action: "create", email: addr("anon") }, jwt);
    ok(r.status === 401 || r.status === 403, `status ${r.status} for jwt=${jwt ? jwt.slice(0, 6) : "none"}`);
    ok(!("invite_id" in r.body) && !("token" in r.body), "data returned to an unauthenticated caller: " + r.raw);
  }
}, false);

// ---------------------------------------------------------------------------------------------------------------
// C4 · create
// ---------------------------------------------------------------------------------------------------------------
t("FN-10 create: hash only, 7-day expiry, one email whose token hashes to token_hash, inviter_name", async () => {
  const email = addr("create");
  const t0 = Date.now();
  const { r, mails, token } = await create(email, "יפן 2027");
  eq(r.status, 200, "create status " + r.raw);
  eq(mails.length, 1, "emails sent");
  const rows = await invites(email);
  eq(rows.length, 1, "rows");
  const row = rows[0];
  ok(/^[0-9a-f]{64}$/.test(row.token_hash), "token_hash is not SHA-256 hex: " + row.token_hash);
  eq(row.token_hash, await sha256hex(token), "token in the email does not hash to token_hash");
  ok(!JSON.stringify(row).includes(token), "raw token stored in the row");
  ok(/^[A-Za-z0-9_-]{43}$/.test(token), "token is not 32 random bytes base64url (43 chars): " + token);
  const exp = Date.parse(row.expires_at) - t0;
  ok(Math.abs(exp - 7 * 864e5) < 5 * 60e3, "expires_at is not now()+7d: " + row.expires_at);
  eq([row.send_count, row.opened_at, row.used_at, row.revoked_at], [1, null, null, null], "counters");
  ok(row.last_sent_at, "last_sent_at not set");
  eq(row.inviter_name, "אור QA", "inviter_name = caller's most recent active display_name");
  eq(row.draft_name, "יפן 2027", "draft_name");
  ok(!r.raw.includes(row.token_hash) && !r.raw.includes(token), "response leaks token/hash: " + r.raw);
});
t("FN-11 create: email content (subject, from name, RTL HTML + text, link, expiry, no tracking)", async () => {
  const email = addr("mail");
  const { mails, token } = await create(email, "יפן 2027");
  const m = mails[0];
  eq(m.subject, "הוזמנת לנהל טיול ב-Plan_Travel", "subject");
  ok(!m.from || /Plan_Travel/.test(m.from), "from name is not Plan_Travel: " + m.from);
  ok([m.to].flat().join(",").includes(email), "to");
  ok(m.html && /dir=["']?rtl/i.test(m.html), "HTML part missing or not RTL");
  ok(m.text && m.text.length > 20, "plain-text part missing");
  ok(`${m.html}${m.text}`.includes(`${APP_URL}#invite=${token}`), "link is not <APP_URL>#invite=<token>");
  ok(m.html!.includes("פתיחת ההזמנה"), "button 'פתיחת ההזמנה' missing");
  ok(m.html!.includes("יפן 2027") && m.html!.includes("אור QA"), "draft name / inviter missing");
  ok(/\d{2}\/\d{2}\/\d{4}/.test(m.html!), "expiry date missing");
  ok(!/<img\b[^>]*(width=["']?1\b|height=["']?1\b)/i.test(m.html!) && !/<img\b/i.test(m.html!), "image / tracking pixel in the email");
});
t("FN-12 create: email normalized (trim, lower); invalid email → 400, no row, no email", async () => {
  const email = addr("norm");
  const { r } = await create(`  ${email.toUpperCase()}  `, null);
  eq(r.status, 200, "create with spaces/upper");
  eq((await invites(email)).length, 1, "stored lower-case");
  for (const bad of ["", "no-at-sign", "a@", "@b.c", "a b@c.d"]) {
    const before = sent.length;
    const x = await create(bad, null);
    ok(x.r.status >= 400 && x.r.status < 500, `invalid email ${JSON.stringify(bad)} accepted: ${x.r.status}`);
    eq(sent.length, before, "email sent for an invalid address");
  }
});
t("FN-13 create: draft_name > 80 chars refused (or never stored longer)", async () => {
  const email = addr("long");
  const { r } = await create(email, "א".repeat(81));
  const rows = await invites(email);
  ok(r.status >= 400 || (rows[0]?.draft_name ?? "").length <= 80, "draft_name > 80 stored");
});

// ---------------------------------------------------------------------------------------------------------------
// C5 / C6 · duplicates
// ---------------------------------------------------------------------------------------------------------------
t("FN-20 second create for the same email (case/space) while active → 409 invite_exists, no email, no row", async () => {
  const email = addr("dup");
  const first = await create(email, "יוון");
  const row = (await invites(email))[0];
  for (const v of [email, email.toUpperCase(), `  ${email} `]) {
    const x = await create(v, "אחר");
    eq(x.r.status, 409, "status for " + v);
    eq(errCode(x.r), "invite_exists", "code");
    eq(x.r.body.invite_id, row.id, "invite_id of the existing invite");
    eq(x.r.body.draft_name, "יוון", "draft_name of the existing invite");
    ok(x.r.body.expires_at, "expires_at of the existing invite");
    eq(x.mails.length, 0, "email sent on invite_exists");
  }
  eq((await invites(email)).length, 1, "row added");
  eq((await check(first.token)).body.valid, true, "existing link broken by the refused create");
});
t("FN-21 create when the previous invite is expired → old revoked, new created", async () => {
  const email = addr("exp");
  const first = await create(email, "ישנה");
  const old = (await invites(email))[0];
  await patchInvite(old.id, { expires_at: new Date(Date.now() - 864e5).toISOString() });
  const second = await create(email, "חדשה");
  eq(second.r.status, 200, "create after expiry " + second.r.raw);
  const rows = await invites(email);
  eq(rows.length, 2, "rows");
  ok(rows.find((x) => x.id === old.id).revoked_at, "expired invite not revoked");
  eq((await check(first.token)).body, { valid: false }, "old link");
  eq((await check(second.token)).body.valid, true, "new link");
});
t("FN-22 create when the previous invite is revoked or used → new invite allowed", async () => {
  for (const f of ["revoked_at", "used_at"]) {
    const email = addr("closed");
    await create(email, "a");
    await patchInvite((await invites(email))[0].id, { [f]: new Date().toISOString() });
    eq((await create(email, "b")).r.status, 200, "create after " + f);
  }
});

// ---------------------------------------------------------------------------------------------------------------
// C7 · resend
// ---------------------------------------------------------------------------------------------------------------
t("FN-30 resend: new token, old link invalid, send_count+1, expiry reset, opened_at cleared", async () => {
  const email = addr("resend");
  const first = await create(email, "r");
  await check(first.token); // sets opened_at
  const row = (await invites(email))[0];
  ok(row.opened_at, "check did not set opened_at");
  await patchInvite(row.id, { expires_at: new Date(Date.now() + 2 * 864e5).toISOString() });
  const a = await admin();
  const before = sent.length;
  const r = await call({ action: "resend", invite_id: row.id }, a.jwt);
  eq(r.status, 200, "resend " + r.raw);
  eq(sent.length - before, 1, "one email");
  const tok2 = tokenFrom(sent[sent.length - 1]);
  ok(tok2 !== first.token, "same token re-sent");
  const after = (await invites(email))[0];
  eq(after.token_hash, await sha256hex(tok2), "new hash");
  eq(after.send_count, 2, "send_count");
  eq(after.opened_at, null, "opened_at not cleared");
  ok(Math.abs(Date.parse(after.expires_at) - Date.now() - 7 * 864e5) < 5 * 60e3, "expiry not reset to 7 days");
  ok(Date.parse(after.last_sent_at) > Date.parse(row.last_sent_at), "last_sent_at not updated");
  eq((await check(first.token)).body, { valid: false }, "old link still valid");
  eq((await check(tok2)).body.valid, true, "new link");
});
t("FN-31 resend works on an expired invite", async () => {
  const email = addr("resexp");
  await create(email, "e");
  const row = (await invites(email))[0];
  await patchInvite(row.id, { expires_at: new Date(Date.now() - 864e5).toISOString() });
  const r = await call({ action: "resend", invite_id: row.id }, (await admin()).jwt);
  eq(r.status, 200, "resend expired " + r.raw);
  eq((await check(tokenFrom(sent[sent.length - 1]))).body.valid, true, "resent link");
});
t("FN-32 resend refused on used / revoked → 409 invite_closed, no email", async () => {
  for (const f of ["used_at", "revoked_at"]) {
    const email = addr("resclosed");
    await create(email, "c");
    const row = (await invites(email))[0];
    await patchInvite(row.id, { [f]: new Date().toISOString() });
    const before = sent.length;
    const r = await call({ action: "resend", invite_id: row.id }, (await admin()).jwt);
    eq([r.status, errCode(r)], [409, "invite_closed"], "resend on " + f);
    eq(sent.length, before, "email sent");
    eq((await invites(email))[0].send_count, 1, "send_count changed");
  }
});

// ---------------------------------------------------------------------------------------------------------------
// C8 · revoke
// ---------------------------------------------------------------------------------------------------------------
t("FN-40 revoke: link invalid immediately; repeat is harmless", async () => {
  const email = addr("revoke");
  const x = await create(email, "v");
  const row = (await invites(email))[0];
  const a = await admin();
  const r1 = await call({ action: "revoke", invite_id: row.id }, a.jwt);
  eq(r1.status, 200, "revoke " + r1.raw);
  eq((await check(x.token)).body, { valid: false }, "revoked link");
  const t1 = (await invites(email))[0].revoked_at;
  const r2 = await call({ action: "revoke", invite_id: row.id }, a.jwt);
  ok(r2.status === 200, "second revoke not harmless: " + r2.raw);
  eq((await invites(email))[0].revoked_at, t1, "revoked_at moved on repeat");
});
t("FN-41 revoke refused on a used invite → 409 invite_used", async () => {
  const email = addr("revused");
  await create(email, "u");
  const row = (await invites(email))[0];
  await patchInvite(row.id, { used_at: new Date().toISOString() });
  const r = await call({ action: "revoke", invite_id: row.id }, (await admin()).jwt);
  eq([r.status, errCode(r)], [409, "invite_used"], "revoke used");
  eq((await invites(email))[0].revoked_at, null, "used invite revoked");
});

// ---------------------------------------------------------------------------------------------------------------
// C9 · email failure
// ---------------------------------------------------------------------------------------------------------------
t("FN-50 email failure on create → 502 email_failed, no row", async () => {
  const email = addr("mailfail");
  failMail = true;
  try {
    const { r } = await create(email, "f");
    eq([r.status, errCode(r)], [502, "email_failed"], "create with SMTP failure");
  } finally { failMail = false; }
  eq((await invites(email)).length, 0, "row left behind");
  eq((await create(email, "f")).r.status, 200, "cannot create after a failed send");
});
t("FN-51 email failure on resend → email_failed, old link still valid, row unchanged", async () => {
  const email = addr("resfail");
  const x = await create(email, "f");
  const row = (await invites(email))[0];
  failMail = true;
  let r: Res;
  try { r = await call({ action: "resend", invite_id: row.id }, (await admin()).jwt); } finally { failMail = false; }
  eq(errCode(r!), "email_failed", "code");
  const after = (await invites(email))[0];
  eq([after.token_hash, after.send_count, after.expires_at], [row.token_hash, row.send_count, row.expires_at], "row changed");
  eq((await check(x.token)).body.valid, true, "old link broken by a failed resend");
});

// ---------------------------------------------------------------------------------------------------------------
// C10 / C11 · check
// ---------------------------------------------------------------------------------------------------------------
t("FN-60 check valid (no JWT): {valid,draft_name,inviter_name,expires_at}; opened_at set once; not consumed", async () => {
  const email = addr("check");
  const x = await create(email, "יפן 2027");
  const r = await check(x.token);
  eq(r.status, 200, "status");
  eq(Object.keys(r.body).sort(), ["draft_name", "expires_at", "inviter_name", "valid"], "keys");
  eq([r.body.valid, r.body.draft_name, r.body.inviter_name], [true, "יפן 2027", "אור QA"], "values");
  const o1 = (await invites(email))[0].opened_at;
  ok(o1, "opened_at not set");
  await new Promise((res) => setTimeout(res, 1100));
  await check(x.token);
  const row = (await invites(email))[0];
  eq(row.opened_at, o1, "opened_at changed on the second check");
  eq(row.used_at, null, "invite consumed by check");
  ok(!r.raw.includes(row.token_hash) && !r.raw.includes(email), "check leaks hash/email: " + r.raw);
});
t("FN-61 invalid tokens → identical {valid:false}, status 200, byte-identical body", async () => {
  const mk = async (tag: string) => { const e = addr(tag); const x = await create(e, tag); return { e, tok: x.token, id: (await invites(e))[0].id }; };
  const exp = await mk("i-exp");
  await patchInvite(exp.id, { expires_at: new Date(Date.now() - 1000).toISOString() });
  const rev = await mk("i-rev");
  await call({ action: "revoke", invite_id: rev.id }, (await admin()).jwt);
  const rep = await mk("i-rep");
  await call({ action: "resend", invite_id: rep.id }, (await admin()).jwt);
  const used = await mk("i-used");
  await patchInvite(used.id, { used_at: new Date().toISOString() });
  const rnd = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(32)))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const cases: [string, unknown][] = [["expired", exp.tok], ["revoked", rev.tok], ["replaced", rep.tok], ["used", used.tok],
    ["malformed", "%%bad<>"], ["empty", ""], ["missing", undefined], ["number", 12345], ["random", rnd], ["huge", "A".repeat(10000)]];
  const outs: Record<string, Res> = {};
  for (const [k, tok] of cases) outs[k] = await call(tok === undefined ? { action: "check" } : { action: "check", token: tok }, null);
  for (const [k] of cases) {
    eq(outs[k].status, 200, `${k} status`);
    eq(outs[k].raw, outs.random.raw, `${k} body differs from random`);
  }
  eq(outs.random.body, { valid: false }, "invalid shape");
  for (const x of [exp, rev, used]) eq((await invites(x.e))[0].opened_at, null, "opened_at set by an invalid check");
});

// ---------------------------------------------------------------------------------------------------------------
// misc spec §3: unknown action, CORS, no token/hash leakage in any response
// ---------------------------------------------------------------------------------------------------------------
t("FN-70 unknown action / bad body → 4xx, no 500", async () => {
  const a = await admin();
  for (const b of [{ action: "nope" }, {}, { action: "resend" }, { action: "revoke", invite_id: "not-a-uuid" }, { action: "resend", invite_id: crypto.randomUUID() }]) {
    const r = await call(b, a.jwt);
    ok(r.status >= 400 && r.status < 500, `${JSON.stringify(b)} → ${r.status} ${r.raw}`);
  }
}, false);
t("FN-71 CORS: orsela.github.io and http://localhost:* allowed; other origins not", async () => {
  for (const o of ["https://orsela.github.io", "http://localhost:5173", "http://localhost:8080"]) {
    const r = await preflight(o);
    ok(r.status < 300, `preflight ${o} status ${r.status}`);
    eq(r.headers.get("access-control-allow-origin"), o, `allow-origin for ${o}`);
  }
  for (const o of ["https://evil.example", "https://orsela.github.io.evil.example", "http://localhost.evil.example"]) {
    const r = await preflight(o);
    const ao = r.headers.get("access-control-allow-origin");
    ok(ao !== o && ao !== "*", `origin ${o} allowed (${ao})`);
  }
}, false);

// ---------------------------------------------------------------------------------------------------------------
// cleanup
// ---------------------------------------------------------------------------------------------------------------
Deno.test({
  name: "zz cleanup (QA data under @" + DOMAIN + ")", ignore: !HAVE_DB, sanitizeOps: false, sanitizeResources: false,
  fn: async () => {
    await rest("DELETE", `manager_invites?email=like.*%40${DOMAIN}`, undefined, "return=minimal");
    for (const id of trips) await rest("DELETE", `trips?id=eq.${id}`, undefined, "return=minimal");
    for (const id of users) await fetch(`${URL_}/auth/v1/admin/users/${id}`, { method: "DELETE", headers: svc }).then((r) => r.body?.cancel());
  },
});
