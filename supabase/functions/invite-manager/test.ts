// Plan_Travel · invite-manager unit tests · version 3.0.0-alpha.3 · F03
// CHANGE 2026-10-05 F03-FN-02: new file (no previous version). Deno tests for spec §5 criteria 1 (function part), 4–11,
//   with a mocked SMTP transport and an in-memory InviteStore that enforces the same rules as the database
//   (lower-case email check, one open invite per email = partial unique index → code 23505, unique token_hash).
//
// Run (network available):      deno test supabase/functions/invite-manager/test.ts
// Run offline (as in the sandbox, where deno.land / npm are blocked): the two remote imports of index.ts are only used
// by the production wiring, so map them to local stubs:
//   deno test --no-check --import-map=supabase/functions/invite-manager/test_import_map.json supabase/functions/invite-manager/test.ts
// No assertion library is imported (to stay runnable offline).

import {
  corsHeaders, type Deps, FALLBACK_INVITER, inviteLink, type InviteRow, type InviteStore, isValidEmail, type MailMessage,
  makeHandler, makeToken, type NewInvite, normalizeEmail, renderInviteEmail, sha256Hex, TOKEN_RE,
} from "./index.ts";

function assert(cond: unknown, msg = "assertion failed"): asserts cond {
  if (!cond) throw new Error(msg);
}
function eq<T>(a: T, b: T, msg = "") {
  const sa = JSON.stringify(a), sb = JSON.stringify(b);
  if (sa !== sb) throw new Error(`${msg} expected ${sb}, got ${sa}`);
}

// ---------------------------------------------------------------------------------------------------
// In-memory store with DB semantics
// ---------------------------------------------------------------------------------------------------
class MemStore implements InviteStore {
  rows = new Map<string, InviteRow>();
  admins = new Set<string>();
  names = new Map<string, string>();
  n = 0;
  private open(r: InviteRow) {
    return !r.used_at && !r.revoked_at;
  }
  private checkConstraints(r: InviteRow, selfId: string | null) {
    if (r.email !== r.email.toLowerCase()) throw Object.assign(new Error("check"), { code: "23514" });
    for (const o of this.rows.values()) {
      if (o.id === selfId) continue;
      if (o.token_hash === r.token_hash) throw Object.assign(new Error("dup hash"), { code: "23505" });
      if (this.open(r) && this.open(o) && o.email === r.email) throw Object.assign(new Error("dup email"), { code: "23505" });
    }
  }
  isPlatformAdmin(uid: string) {
    return Promise.resolve(this.admins.has(uid));
  }
  inviterName(uid: string) {
    return Promise.resolve(this.names.get(uid) ?? null);
  }
  findOpenByEmail(email: string) {
    return Promise.resolve([...this.rows.values()].find((r) => r.email === email && this.open(r)) ?? null);
  }
  getById(id: string) {
    const r = this.rows.get(id);
    return Promise.resolve(r ? { ...r } : null);
  }
  findByHash(hash: string) {
    const r = [...this.rows.values()].find((x) => x.token_hash === hash);
    return Promise.resolve(r ? { ...r } : null);
  }
  insert(row: NewInvite) {
    const id = `00000000-0000-4000-8000-${String(++this.n).padStart(12, "0")}`;
    const r: InviteRow = {
      id, email: row.email, token_hash: row.token_hash, draft_name: row.draft_name, inviter_name: row.inviter_name,
      expires_at: row.expires_at, used_at: null, revoked_at: null, opened_at: null, send_count: row.send_count,
      last_sent_at: row.last_sent_at, created_at: row.last_sent_at,
    };
    this.checkConstraints(r, null);
    this.rows.set(id, r);
    return Promise.resolve(id);
  }
  deleteById(id: string) {
    this.rows.delete(id);
    return Promise.resolve();
  }
  revokeOpen(id: string, at: string) {
    const r = this.rows.get(id);
    if (!r || !this.open(r)) return Promise.resolve(0);
    r.revoked_at = at;
    return Promise.resolve(1);
  }
  updateOpen(id: string, f: Partial<InviteRow>) {
    const r = this.rows.get(id);
    if (!r || !this.open(r)) return Promise.resolve(0);
    const next = { ...r, ...f };
    this.checkConstraints(next, id);
    this.rows.set(id, next);
    return Promise.resolve(1);
  }
  markOpened(id: string, at: string) {
    const r = this.rows.get(id);
    if (r && !r.opened_at) r.opened_at = at;
    return Promise.resolve();
  }
}

const ADMIN = "aaaaaaaa-0000-4000-8000-000000000001";
const MANAGER = "bbbbbbbb-0000-4000-8000-000000000002";
const JWT = { admin: "jwt-admin", manager: "jwt-manager" } as const;
const APP = "https://orsela.github.io/Plan_Travel/app/";
const ORIGIN = "https://orsela.github.io";

function setup(opts: { failMail?: boolean } = {}) {
  const store = new MemStore();
  store.admins.add(ADMIN);
  store.names.set(ADMIN, "אור");
  const sent: MailMessage[] = [];
  const logs: string[] = [];
  let clock = new Date("2026-10-05T09:00:00Z");
  let seed = 1;
  const ctl = { failMail: !!opts.failMail };
  const deps: Deps = {
    store,
    verifyJwt: (j) => Promise.resolve(j === JWT.admin ? ADMIN : j === JWT.manager ? MANAGER : null),
    sendMail: (m) => {
      if (ctl.failMail) return Promise.reject(new Error("smtp down"));
      sent.push(m);
      return Promise.resolve();
    },
    now: () => new Date(clock),
    random: (n) => {
      const a = new Uint8Array(n);
      for (let i = 0; i < n; i++) a[i] = (seed * 31 + i * 7) & 255;
      seed++;
      return a;
    },
    appUrl: APP,
    log: (e, f) => logs.push(JSON.stringify({ e, ...(f || {}) })),
  };
  const h = makeHandler(deps);
  async function call(body: unknown, jwt?: string, origin = ORIGIN) {
    const headers: Record<string, string> = { "content-type": "application/json", origin };
    if (jwt) headers.authorization = `Bearer ${jwt}`;
    const res = await h(new Request("https://x.supabase.co/functions/v1/invite-manager", { method: "POST", headers, body: JSON.stringify(body) }));
    return { status: res.status, body: await res.json(), headers: res.headers };
  }
  const tokenOf = (m: MailMessage) => {
    const mt = /#invite=([A-Za-z0-9_-]+)/.exec(m.text);
    assert(mt, "no token in email");
    return mt[1];
  };
  return { store, sent, logs, call, tokenOf, ctl, advance: (ms: number) => (clock = new Date(clock.getTime() + ms)) };
}
const DAY = 86_400_000;

// ---------------------------------------------------------------------------------------------------
// pure helpers
// ---------------------------------------------------------------------------------------------------
Deno.test("helpers: email normalization/validation, token format, CORS", async () => {
  eq(normalizeEmail("  Dana@Example.COM "), "dana@example.com");
  assert(isValidEmail("dana@example.com"));
  for (const bad of ["", "dana", "dana@", "@x.com", "a b@x.com", "dana@x", "<a@x.com>"]) assert(!isValidEmail(bad), bad);
  const t = makeToken((n) => new Uint8Array(n).fill(255));
  assert(TOKEN_RE.test(t), "token format " + t);
  eq((await sha256Hex("abc")).length, 64);
  eq(await sha256Hex("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  eq(corsHeaders("https://orsela.github.io")["Access-Control-Allow-Origin"], "https://orsela.github.io");
  eq(corsHeaders("http://localhost:8080")["Access-Control-Allow-Origin"], "http://localhost:8080");
  eq(corsHeaders("http://localhost")["Access-Control-Allow-Origin"], "http://localhost");
  for (const o of ["https://evil.example", "https://orsela.github.io.evil.com", "http://localhost.evil.com", "null"]) {
    eq(corsHeaders(o)["Access-Control-Allow-Origin"], undefined, o);
  }
  eq(inviteLink(APP + "#x", "TOK"), APP + "#invite=TOK");
});

Deno.test("email: RTL html + text, subject, link, expiry, escaping, no images/pixels", () => {
  const m = renderInviteEmail({ to: "d@x.com", inviter: "אור", draftName: "יפן <2027>", link: APP + "#invite=T", expiresAt: "2026-10-10T08:00:00Z" });
  eq(m.subject, "הוזמנת לנהל טיול ב-Plan_Travel");
  assert(m.html.includes('dir="rtl"'));
  assert(m.html.includes("יפן &lt;2027&gt;") && !m.html.includes("<2027>"), "escaping");
  assert(m.html.includes(">קבלת ההזמנה והקמת הטיול</a>") && m.html.includes(APP + "#invite=T"));
  assert(m.text.includes(APP + "#invite=T") && m.text.includes("10/10/2026") && m.html.includes("10/10/2026"));
  assert(m.text.includes("אפשר להתעלם מהמייל"));
  assert(!/<img|background-image|url\(/i.test(m.html), "no tracking pixels / remote images");
});

// ---------------------------------------------------------------------------------------------------
// C1 · access
// ---------------------------------------------------------------------------------------------------
Deno.test("C1 non-admin / no JWT → 403 not_platform_admin for create, resend, revoke", async () => {
  const s = setup();
  for (const action of ["create", "resend", "revoke"]) {
    const body = { action, email: "x@example.com", invite_id: "00000000-0000-4000-8000-000000000001" };
    for (const jwt of [JWT.manager, undefined, "garbage"]) {
      const r = await s.call(body, jwt);
      eq(r.status, 403, `${action}/${jwt}`);
      eq(r.body.error, "not_platform_admin");
    }
  }
  eq(s.sent.length, 0);
  eq(s.store.rows.size, 0);
});

Deno.test("CORS: preflight for allowed origin; foreign origin gets no allow header; GET refused", async () => {
  const s = setup();
  const h = makeHandler({ ...({} as Deps), store: s.store, verifyJwt: () => Promise.resolve(null), sendMail: () => Promise.resolve(), now: () => new Date(), random: (n) => new Uint8Array(n), appUrl: APP, log: () => {} });
  const pre = await h(new Request("https://x/f", { method: "OPTIONS", headers: { origin: ORIGIN } }));
  eq(pre.status, 204);
  eq(pre.headers.get("access-control-allow-origin"), ORIGIN);
  const bad = await h(new Request("https://x/f", { method: "OPTIONS", headers: { origin: "https://evil.example" } }));
  eq(bad.headers.get("access-control-allow-origin"), null);
  const get = await h(new Request("https://x/f", { method: "GET", headers: { origin: ORIGIN } }));
  eq(get.status, 405);
});

// ---------------------------------------------------------------------------------------------------
// C4 · create
// ---------------------------------------------------------------------------------------------------
Deno.test("C4 create stores only the SHA-256 hash, expiry 7 days, one email whose token hashes to the stored value", async () => {
  const s = setup();
  const r = await s.call({ action: "create", email: " Dana@Example.com ", draft_name: " יפן 2027 " }, JWT.admin);
  eq(r.status, 200);
  eq(r.body.email, "dana@example.com");
  eq(s.sent.length, 1);
  const row = [...s.store.rows.values()][0];
  const tok = s.tokenOf(s.sent[0]);
  assert(TOKEN_RE.test(tok));
  eq(row.token_hash, await sha256Hex(tok));
  assert(!JSON.stringify(row).includes(tok), "plain token stored");
  eq(new Date(row.expires_at).getTime() - new Date("2026-10-05T09:00:00Z").getTime(), 7 * DAY);
  eq(row.send_count, 1);
  eq(row.draft_name, "יפן 2027");
  eq(row.inviter_name, "אור");
  eq(s.sent[0].to, "dana@example.com");
  assert(!JSON.stringify(r.body).includes(tok) && !JSON.stringify(r.body).includes(row.token_hash), "response leaks token/hash");
  assert(!s.logs.join("\n").includes(tok) && !s.logs.join("\n").includes("dana@"), "logs leak token/email");
});

Deno.test("C4 create validation: bad email / long name refused, no row, no email; inviter fallback", async () => {
  const s = setup();
  eq((await s.call({ action: "create", email: "nope" }, JWT.admin)).body.error, "invalid_email");
  eq((await s.call({ action: "create", email: "a@b.co", draft_name: "x".repeat(81) }, JWT.admin)).body.error, "invalid_name");
  eq(s.store.rows.size, 0);
  eq(s.sent.length, 0);
  s.store.names.delete(ADMIN);
  const r = await s.call({ action: "create", email: "a@b.co" }, JWT.admin);
  eq(r.status, 200);
  eq([...s.store.rows.values()][0].inviter_name, FALLBACK_INVITER);
  eq([...s.store.rows.values()][0].draft_name, null);
});

// ---------------------------------------------------------------------------------------------------
// C5 / C6 · second create
// ---------------------------------------------------------------------------------------------------
Deno.test("C5 second create for the same email (any case/whitespace) while active → invite_exists, no email, no row", async () => {
  const s = setup();
  const first = await s.call({ action: "create", email: "dana@example.com", draft_name: "יפן 2027" }, JWT.admin);
  s.advance(2 * DAY);
  const r = await s.call({ action: "create", email: "  DANA@example.COM", draft_name: "other" }, JWT.admin);
  eq(r.status, 409);
  eq(r.body.error, "invite_exists");
  eq(r.body.invite_id, first.body.invite_id);
  eq(r.body.draft_name, "יפן 2027");
  eq(r.body.days_left, 5);
  eq(s.sent.length, 1);
  eq(s.store.rows.size, 1);
});

Deno.test("C6 create for an email whose previous invite expired → old one revoked, new one created", async () => {
  const s = setup();
  const a = await s.call({ action: "create", email: "yoav@example.com" }, JWT.admin);
  s.advance(8 * DAY);
  const b = await s.call({ action: "create", email: "yoav@example.com", draft_name: "יוון" }, JWT.admin);
  eq(b.status, 200);
  assert(a.body.invite_id !== b.body.invite_id);
  assert(s.store.rows.get(a.body.invite_id)!.revoked_at, "old not revoked");
  eq(s.store.rows.get(b.body.invite_id)!.revoked_at, null);
  eq(s.sent.length, 2);
});

// ---------------------------------------------------------------------------------------------------
// C7 · resend
// ---------------------------------------------------------------------------------------------------
Deno.test("C7 resend → new token, old link invalid, send_count+1, expiry reset, opened_at cleared; works when expired", async () => {
  const s = setup();
  const c = await s.call({ action: "create", email: "d@example.com", draft_name: "יפן" }, JWT.admin);
  const t1 = s.tokenOf(s.sent[0]);
  eq((await s.call({ action: "check", token: t1 })).body.valid, true); // sets opened_at
  assert(s.store.rows.get(c.body.invite_id)!.opened_at);
  s.advance(9 * DAY); // expired
  eq((await s.call({ action: "check", token: t1 })).body.valid, false);
  const r = await s.call({ action: "resend", invite_id: c.body.invite_id }, JWT.admin);
  eq(r.status, 200);
  eq(r.body.send_count, 2);
  const row = s.store.rows.get(c.body.invite_id)!;
  eq(row.send_count, 2);
  eq(row.opened_at, null);
  eq(new Date(row.expires_at).getTime() - new Date(row.last_sent_at!).getTime(), 7 * DAY);
  const t2 = s.tokenOf(s.sent[1]);
  assert(t1 !== t2);
  eq((await s.call({ action: "check", token: t1 })).body, { valid: false });
  eq((await s.call({ action: "check", token: t2 })).body.valid, true);
});

Deno.test("C7 resend refused on revoked and on used invites (invite_closed), no email", async () => {
  const s = setup();
  const c = await s.call({ action: "create", email: "d@example.com" }, JWT.admin);
  await s.call({ action: "revoke", invite_id: c.body.invite_id }, JWT.admin);
  const r = await s.call({ action: "resend", invite_id: c.body.invite_id }, JWT.admin);
  eq([r.status, r.body.error], [409, "invite_closed"]);
  const c2 = await s.call({ action: "create", email: "e@example.com" }, JWT.admin);
  s.store.rows.get(c2.body.invite_id)!.used_at = new Date().toISOString();
  const r2 = await s.call({ action: "resend", invite_id: c2.body.invite_id }, JWT.admin);
  eq([r2.status, r2.body.error], [409, "invite_closed"]);
  eq(s.sent.length, 2);
  eq((await s.call({ action: "resend", invite_id: "00000000-0000-4000-8000-0000000000ff" }, JWT.admin)).body.error, "invite_not_found");
  eq((await s.call({ action: "resend", invite_id: "not-a-uuid" }, JWT.admin)).body.error, "invalid_request");
});

// ---------------------------------------------------------------------------------------------------
// C8 · revoke
// ---------------------------------------------------------------------------------------------------
Deno.test("C8 revoke → link invalid immediately; repeating is harmless; refused on a used invite", async () => {
  const s = setup();
  const c = await s.call({ action: "create", email: "d@example.com" }, JWT.admin);
  const t = s.tokenOf(s.sent[0]);
  eq((await s.call({ action: "revoke", invite_id: c.body.invite_id }, JWT.admin)).status, 200);
  eq((await s.call({ action: "check", token: t })).body, { valid: false });
  const again = await s.call({ action: "revoke", invite_id: c.body.invite_id }, JWT.admin);
  eq([again.status, again.body.ok], [200, true]);
  const c2 = await s.call({ action: "create", email: "d@example.com" }, JWT.admin); // slot freed
  eq(c2.status, 200);
  s.store.rows.get(c2.body.invite_id)!.used_at = new Date().toISOString();
  const u = await s.call({ action: "revoke", invite_id: c2.body.invite_id }, JWT.admin);
  eq([u.status, u.body.error], [409, "invite_used"]);
  eq(s.store.rows.get(c2.body.invite_id)!.revoked_at, null);
});

// ---------------------------------------------------------------------------------------------------
// C9 · email failure
// ---------------------------------------------------------------------------------------------------
Deno.test("C9 email failure on create leaves no row (502 email_failed)", async () => {
  const s = setup({ failMail: true });
  const r = await s.call({ action: "create", email: "d@example.com" }, JWT.admin);
  eq([r.status, r.body.error], [502, "email_failed"]);
  eq(s.store.rows.size, 0);
  s.ctl.failMail = false;
  eq((await s.call({ action: "create", email: "d@example.com" }, JWT.admin)).status, 200); // not blocked by a ghost row
});

Deno.test("C9 email failure on resend keeps the old link valid and the row unchanged", async () => {
  const s = setup();
  const c = await s.call({ action: "create", email: "d@example.com" }, JWT.admin);
  const t1 = s.tokenOf(s.sent[0]);
  const before = JSON.stringify(s.store.rows.get(c.body.invite_id));
  s.ctl.failMail = true;
  const r = await s.call({ action: "resend", invite_id: c.body.invite_id }, JWT.admin);
  eq([r.status, r.body.error], [502, "email_failed"]);
  eq(JSON.stringify(s.store.rows.get(c.body.invite_id)), before);
  eq((await s.call({ action: "check", token: t1 })).body.valid, true);
});

// ---------------------------------------------------------------------------------------------------
// C10 / C11 · check
// ---------------------------------------------------------------------------------------------------
Deno.test("C10 valid token → draft name, inviter, expiry; opened_at set once; invite not consumed", async () => {
  const s = setup();
  const c = await s.call({ action: "create", email: "d@example.com", draft_name: "יפן 2027" }, JWT.admin);
  const t = s.tokenOf(s.sent[0]);
  const r = await s.call({ action: "check", token: t }); // no JWT
  eq(r.status, 200);
  eq(r.body, { valid: true, draft_name: "יפן 2027", inviter_name: "אור", expires_at: s.store.rows.get(c.body.invite_id)!.expires_at });
  const first = s.store.rows.get(c.body.invite_id)!.opened_at;
  assert(first);
  s.advance(3600_000);
  await s.call({ action: "check", token: t });
  eq(s.store.rows.get(c.body.invite_id)!.opened_at, first, "opened_at changed on second open");
  eq(s.store.rows.get(c.body.invite_id)!.used_at, null);
  eq(s.store.rows.get(c.body.invite_id)!.revoked_at, null);
});

Deno.test("C11 expired / revoked / replaced / used / malformed / random → identical {valid:false}, status 200", async () => {
  const s = setup();
  const mk = async (email: string) => {
    const c = await s.call({ action: "create", email }, JWT.admin);
    return { id: c.body.invite_id as string, tok: s.tokenOf(s.sent[s.sent.length - 1]) };
  };
  const exp = await mk("exp@example.com");
  const rev = await mk("rev@example.com");
  const rep = await mk("rep@example.com");
  const used = await mk("used@example.com");
  await s.call({ action: "revoke", invite_id: rev.id }, JWT.admin);
  await s.call({ action: "resend", invite_id: rep.id }, JWT.admin);
  s.store.rows.get(used.id)!.used_at = new Date().toISOString();
  s.store.rows.get(exp.id)!.expires_at = new Date(Date.parse("2026-10-05T08:00:00Z")).toISOString();
  const random = makeToken((n) => crypto.getRandomValues(new Uint8Array(n)));
  const cases: unknown[] = [exp.tok, rev.tok, rep.tok, used.tok, "short", exp.tok + "x", "", 123, null, random, "a".repeat(43)];
  const raw = new Set<string>();
  for (const token of cases) {
    const r = await s.call({ action: "check", token });
    eq(r.status, 200, String(token));
    raw.add(JSON.stringify(r.body));
  }
  eq([...raw], ['{"valid":false}']);
  // check never needs a JWT and never touches opened_at of invalid invites
  for (const id of [exp.id, rev.id, used.id]) eq(s.store.rows.get(id)!.opened_at, null);
});

Deno.test("bad requests: invalid JSON / unknown action → 400 invalid_request; nothing logged with secrets", async () => {
  const s = setup();
  eq((await s.call({ action: "drop_tables" }, JWT.admin)).body.error, "invalid_request");
  eq((await s.call([1, 2], JWT.admin)).body.error, "invalid_request");
  assert(!s.logs.some((l) => l.includes("Bearer") || l.includes("jwt-admin")));
});

// CHANGE 2026-10-09 F03-MAIL-04: the MIME we build has short, correctly folded header lines, a subject that decodes
//   back exactly, and both bodies; the SMTP dialogue is checked against a local fake server.
Deno.test("F03-MAIL-04 buildMime: folded subject decodes, lines ≤ 78, both parts present", async () => {
  const { buildMime, EMAIL_SUBJECT, renderInviteEmail } = await import("./index.ts");
  const m = renderInviteEmail({ to: "a@b.co", inviter: "אור", draftName: "טוקיו 27", link: "https://x/#invite=T", expiresAt: "2026-10-16T00:00:00Z" });
  const raw = buildMime({ from: "s@gmail.com", fromName: "Plan_Travel", to: "a@b.co", subject: m.subject, text: m.text, html: m.html, date: new Date(0), id: "abc" });
  const [head, body] = [raw.slice(0, raw.indexOf("\r\n\r\n")), raw.slice(raw.indexOf("\r\n\r\n") + 4)];
  for (const l of raw.split("\r\n")) if (l.length > 78) throw new Error("long line " + l.length + ": " + l.slice(0, 40));
  if (!/^[\x00-\x7f]*$/.test(raw)) throw new Error("non-ASCII in message");
  const lines = head.split("\r\n");
  for (const l of lines) if (!/^[A-Za-z-]+: /.test(l) && !/^ /.test(l)) throw new Error("header line not a field or fold: " + l);
  const unfolded = head.replace(/\r\n /g, " ");
  const subj = /^Subject: (.*)$/m.exec(unfolded)![1];
  const dec = subj.split(" ").map((w) => new TextDecoder().decode(Uint8Array.from(atob(/^=\?UTF-8\?B\?(.+)\?=$/.exec(w)![1]), (c) => c.charCodeAt(0)))).join("");
  if (dec !== EMAIL_SUBJECT) throw new Error("subject roundtrip: " + dec);
  if (!body.includes("Content-Type: text/plain; charset=UTF-8") || !body.includes("Content-Type: text/html; charset=UTF-8")) throw new Error("parts missing");
  const htmlB64 = body.split("Content-Type: text/html; charset=UTF-8\r\nContent-Transfer-Encoding: base64\r\n\r\n")[1].split("\r\n--pt-abc--")[0].replace(/\r\n/g, "");
  const html = new TextDecoder().decode(Uint8Array.from(atob(htmlB64), (c) => c.charCodeAt(0)));
  if (html !== m.html) throw new Error("html roundtrip");
  if (!raw.endsWith("--pt-abc--\r\n")) throw new Error("no closing boundary");
});

Deno.test("F03-MAIL-04 smtpSend: full dialogue against a fake server; AUTH failure surfaces the code", async () => {
  const { smtpSend } = await import("./index.ts");
  async function run(authCode: number) {
    const l = Deno.listen({ hostname: "127.0.0.1", port: 0 });
    const port = (l.addr as Deno.NetAddr).port;
    const seen: string[] = [];
    const server = (async () => {
      const c = await l.accept();
      const enc = new TextEncoder(), dec = new TextDecoder();
      const w = (s: string) => c.write(enc.encode(s + "\r\n"));
      await w("220 fake ready");
      let buf = "", inData = false;
      const b = new Uint8Array(65536);
      outer: for (;;) {
        const n = await c.read(b); if (n === null) break;
        buf += dec.decode(b.subarray(0, n));
        for (;;) {
          if (inData) {
            const e = buf.indexOf("\r\n.\r\n"); if (e < 0) break;
            seen.push("DATA:" + buf.slice(0, e)); buf = buf.slice(e + 5); inData = false; await w("250 queued"); continue;
          }
          const k = buf.indexOf("\r\n"); if (k < 0) break;
          const line = buf.slice(0, k); buf = buf.slice(k + 2); seen.push(line);
          if (line.startsWith("EHLO")) { await w("250-fake"); await w("250 AUTH PLAIN LOGIN"); }
          else if (line.startsWith("AUTH")) { await w(authCode === 235 ? "235 ok" : "535 5.7.8 bad"); }
          else if (line.startsWith("MAIL") || line.startsWith("RCPT")) await w("250 ok");
          else if (line === "DATA") { await w("354 go"); inData = true; }
          else if (line === "QUIT") { await w("221 bye"); break outer; }
        }
      }
      try { c.close(); } catch { /* */ }
      l.close();
    })();
    let err: unknown = null;
    try {
      const conn = await Deno.connect({ hostname: "127.0.0.1", port });
      await smtpSend(conn, { user: "u@x.co", pass: "pw", from: "u@x.co", to: "t@y.co", data: "Subject: hi\r\n\r\n.dot\r\nbody" });
    } catch (e) { err = e; }
    await server.catch(() => {});
    return { seen, err };
  }
  const ok = await run(235);
  if (ok.err) throw ok.err;
  if (!ok.seen.includes("MAIL FROM:<u@x.co>") || !ok.seen.includes("RCPT TO:<t@y.co>")) throw new Error("envelope: " + ok.seen.join("|"));
  const data = ok.seen.find((s) => s.startsWith("DATA:"))!;
  if (!data.includes("\r\n..dot")) throw new Error("dot-stuffing missing");
  if (ok.seen.some((s) => s.includes("pw") && !s.startsWith("AUTH"))) throw new Error("password leaked");
  const bad = await run(535);
  if (!(bad.err instanceof Error) || !bad.err.message.startsWith("SMTP 535")) throw new Error("auth failure not surfaced: " + bad.err);
});
