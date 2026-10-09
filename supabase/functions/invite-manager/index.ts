// Plan_Travel Edge Function `invite-manager` · version 3.0.0-alpha.3.6 · F03
// CHANGE 2026-10-05 F03-FN-01: new file (no previous version). Super-admin manager invites: actions create / resend /
//   revoke / check (docs/F03_spec.md §3). Runs with the service role; verify_jwt is OFF for this function because
//   `check` is called by an invitee who has no session — every other action verifies the caller's JWT itself and
//   requires a row in public.platform_admins.
//
// Structure (one file, so it can be pasted into the dashboard editor as-is):
//   1. pure helpers (validation, tokens, hashing, CORS, email rendering) — exported for test.ts
//   2. makeHandler(deps) — all request logic, with the database, the mailer, the clock and the random source injected
//   3. production wiring (supabase-js service client + a small built-in SMTP client over smtp.gmail.com:465 TLS),
//      only when this file is the entry point.
//
// Secrets: GMAIL_USER, GMAIL_APP_PASSWORD, APP_URL (default https://orsela.github.io/Plan_Travel/app/).
// Provided by Supabase: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (or set PT_SERVICE_KEY to an sb_secret_… key).
// Never logs tokens, request bodies or email addresses; never returns token_hash.

import { createClient, type SupabaseClient } from "npm:@supabase/supabase-js@2.45.4";
// CHANGE 2026-10-09 F03-MAIL-04: denomailer removed (it broke Hebrew subjects into raw MIME in Gmail, twice).

// =====================================================================================================
// 1. Pure helpers
// =====================================================================================================
export const FN_VERSION = "3.0.0-alpha.3.6"; // CHANGE 2026-10-09 F03-LAND-03: ?invite=1 in the link; F03-MAIL-04: own MIME+SMTP (denomailer removed); F03-MAIL-03: subject pre-encoded; F03-MAIL-02: redesigned invite email; F03-FN-03: mailer error logging; // CHANGE 2026-10-09 F03-FN-02: bumped for the startServer()/main.ts entry split
export const INVITE_TTL_DAYS = 7;
export const DEFAULT_APP_URL = "https://orsela.github.io/Plan_Travel/app/";
export const FALLBACK_INVITER = "מנהל המערכת";
export const EMAIL_SUBJECT = "הוזמנת לנהל טיול ב-Plan_Travel";
export const FROM_NAME = "Plan_Travel";
const DAY_MS = 86_400_000;

/** Fixed error codes (translated by the client dictionary). */
export type ErrorCode =
  | "not_platform_admin" | "invalid_request" | "invalid_email" | "invalid_name" | "invite_exists"
  | "invite_not_found" | "invite_closed" | "invite_used" | "email_failed" | "server_error" | "method_not_allowed";

export function normalizeEmail(v: unknown): string {
  return typeof v === "string" ? v.trim().toLowerCase() : "";
}

const EMAIL_RE = /^[^\s@<>()",;:\\[\]]+@[^\s@<>()",;:\\[\]]+\.[^\s@<>()",;:\\[\]]{2,}$/;
export function isValidEmail(e: string): boolean {
  return e.length >= 6 && e.length <= 254 && EMAIL_RE.test(e);
}

/** Draft name: optional, trimmed, ≤80 chars, no control characters. Returns null for empty, undefined for invalid. */
export function cleanDraftName(v: unknown): string | null | undefined {
  if (v === undefined || v === null) return null;
  if (typeof v !== "string") return undefined;
  const s = v.trim();
  if (!s) return null;
  // deno-lint-ignore no-control-regex
  if (s.length > 80 || /[\u0000-\u001f\u007f]/.test(s)) return undefined;
  return s;
}

export function base64url(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** 32 random bytes → 43-char base64url token. */
export function makeToken(random: (n: number) => Uint8Array): string {
  return base64url(random(32));
}

export const TOKEN_RE = /^[A-Za-z0-9_-]{43}$/;

export async function sha256Hex(s: string): Promise<string> {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)));
  return Array.from(d, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function isAllowedOrigin(origin: string | null): boolean {
  if (!origin) return false;
  return origin === "https://orsela.github.io" || /^http:\/\/localhost(:\d{1,5})?$/.test(origin);
}

export function corsHeaders(origin: string | null): Record<string, string> {
  const h: Record<string, string> = { "Vary": "Origin" };
  if (isAllowedOrigin(origin)) {
    h["Access-Control-Allow-Origin"] = origin as string;
    h["Access-Control-Allow-Methods"] = "POST, OPTIONS";
    h["Access-Control-Allow-Headers"] = "authorization, x-client-info, apikey, content-type";
    h["Access-Control-Max-Age"] = "600";
  }
  return h;
}

export function htmlEscape(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

/** dd/mm/yyyy in Israel time (the family's time zone). */
export function fmtDate(iso: string | Date): string {
  const d = typeof iso === "string" ? new Date(iso) : iso;
  const p = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Jerusalem", day: "2-digit", month: "2-digit", year: "numeric" })
    .formatToParts(d);
  const g = (t: string) => p.find((x) => x.type === t)?.value ?? "";
  return `${g("day")}/${g("month")}/${g("year")}`;
}

/** CHANGE 2026-10-09 F03-LAND-03: the link now carries ?invite=1 before the hash. What changed from 3.0.0-alpha.3.5: with
 *  only a #hash, tapping the button while the app was already open did not reload the page on Or's phone; a different
 *  query forces a full page load. The token stays in the hash (never sent to the web server); the app removes both. */
export function inviteLink(appUrl: string, token: string): string {
  const base = appUrl.split("#")[0].split("?")[0];
  return `${base}?invite=1#invite=${token}`;
}

export interface MailMessage {
  to: string;
  subject: string;
  text: string;
  html: string;
}

/** Invite email (artboard F03_Email).
 *  CHANGE 2026-10-09 F03-MAIL-02: redesigned per Or's approval (2026-10-09): clear headline with inviter + group name,
 *  one full-width button "קבלת ההזמנה והקמת הטיול", expiry under the button, a 3-step "what happens next", and a
 *  copy-paste fallback link. What changed from 3.0.0-alpha.3.2: copy and layout only (RTL HTML + plain text, no tracking
 *  pixels, no remote images); the link, expiry and data are unchanged. */
export const BUTTON_TEXT = "קבלת ההזמנה והקמת הטיול";
export function renderInviteEmail(p: { to: string; inviter: string; draftName: string | null; link: string; expiresAt: string }): MailMessage {
  const exp = fmtDate(p.expiresAt);
  const name = p.draftName ? `"${p.draftName}"` : "";
  const headline = p.draftName ? `${p.inviter} מזמין אותך לנהל את הטיול ${name}` : `${p.inviter} מזמין אותך לנהל טיול חדש`;
  const headlineHtml = p.draftName
    ? `${htmlEscape(p.inviter)} מזמין אותך לנהל את הטיול <span style="white-space:nowrap">"${htmlEscape(p.draftName)}"</span>`
    : `${htmlEscape(p.inviter)} מזמין אותך לנהל טיול חדש`;
  const steps = ["מתחברים עם כתובת המייל הזו", "ממלאים שם, תאריכים ומדינות", "שולחים לחברים קישור הצטרפות בווטסאפ"];
  const text = [
    "הזמנה אישית",
    headline,
    "",
    "לחיצה אחת על הקישור, והקמת הטיול מתחילה:",
    p.link,
    `בתוקף עד ${exp}`,
    "",
    "מה יקרה אחרי הלחיצה:",
    ...steps.map((t, i) => `${i + 1}. ${t}`),
    "",
    "הקישור אישי ומיועד לכתובת הזו בלבד. אם לא ציפית להזמנה, אפשר להתעלם מהמייל.",
    "",
    "נשלח ממערכת Plan_Travel. אין להשיב למייל זה.",
  ].join("\n");
  const L = htmlEscape(p.link);
  const stepRows = steps.map((t, i) =>
    `<tr><td style="padding:3px 0 3px 8px;vertical-align:top;color:#0f6b4f;font-weight:800">${i + 1}</td><td style="padding:3px 0">${htmlEscape(t)}</td></tr>`
  ).join("");
  const html = `<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${htmlEscape(EMAIL_SUBJECT)}</title></head>` +
    `<body dir="rtl" style="margin:0;padding:16px 12px;background:#eceae3;color:#17211b;font-family:Arial,'Segoe UI',sans-serif">` +
    `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;margin:0 auto;background:#ffffff;border-radius:16px;overflow:hidden" dir="rtl">` +
    `<tr><td style="background:#0f6b4f;color:#ffffff;padding:20px;text-align:right"><div style="font-size:20px;font-weight:900">Plan_Travel</div><div style="font-size:13px;opacity:.9">מתכננים יחד, מטיילים יחד</div></td></tr>` +
    `<tr><td style="padding:24px 20px 8px;text-align:right">` +
    `<div style="font-size:13px;color:#5f6861;margin:0 0 6px">הזמנה אישית</div>` +
    `<h1 style="margin:0 0 10px;font-size:22px;line-height:1.35">${headlineHtml}</h1>` +
    `<p style="margin:0 0 20px;font-size:15px;line-height:1.6;color:#3a443d">לחיצה אחת על הכפתור, והקמת הטיול מתחילה.</p>` +
    `<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="border-radius:14px;background:#0f6b4f">` +
    `<a href="${L}" style="display:block;padding:16px 12px;font-size:17px;font-weight:800;color:#ffffff;text-decoration:none;text-align:center">${BUTTON_TEXT}</a>` +
    `</td></tr></table>` +
    `<p style="margin:10px 0 0;font-size:12.5px;color:#5f6861;text-align:center">בתוקף עד ${exp}</p>` +
    `</td></tr>` +
    `<tr><td style="padding:18px 20px 6px;text-align:right"><div style="font-size:13.5px;font-weight:800;margin:0 0 8px">מה יקרה אחרי הלחיצה</div>` +
    `<table role="presentation" cellpadding="0" cellspacing="0" style="font-size:13.5px;line-height:1.5;color:#3a443d">${stepRows}</table></td></tr>` +
    `<tr><td style="padding:16px 20px 20px;text-align:right;font-size:12px;line-height:1.6;color:#5f6861">הכפתור לא עובד? העתיקו את הקישור לדפדפן:<br>` +
    `<a href="${L}" dir="ltr" style="word-break:break-all;color:#0f6b4f">${L}</a><br><br>` +
    `הקישור אישי ומיועד לכתובת הזו בלבד. אם לא ציפית להזמנה, אפשר להתעלם מהמייל.</td></tr>` +
    `<tr><td style="border-top:1px solid #efeee8;padding:12px 20px;font-size:11.5px;color:#5f6861;text-align:right">נשלח ממערכת Plan_Travel. אין להשיב למייל זה.</td></tr>` +
    `</table></body></html>`;
  return { to: p.to, subject: EMAIL_SUBJECT, text, html };
}

// =====================================================================================================
// 2. Request logic (dependencies injected)
// =====================================================================================================
export interface InviteRow {
  id: string;
  email: string;
  token_hash: string;
  draft_name: string | null;
  inviter_name: string | null;
  expires_at: string;
  used_at: string | null;
  revoked_at: string | null;
  opened_at: string | null;
  send_count: number;
  last_sent_at: string | null;
  created_at: string;
}

export interface NewInvite {
  email: string;
  token_hash: string;
  draft_name: string | null;
  invited_by: string;
  inviter_name: string;
  expires_at: string;
  last_sent_at: string;
  send_count: number;
}

/** Database operations (service role). Each "open" filter = used_at is null and revoked_at is null. */
export interface InviteStore {
  isPlatformAdmin(uid: string): Promise<boolean>;
  inviterName(uid: string): Promise<string | null>;
  findOpenByEmail(email: string): Promise<InviteRow | null>;
  getById(id: string): Promise<InviteRow | null>;
  findByHash(hash: string): Promise<InviteRow | null>;
  /** returns the new id; throws {code:'23505'} when the one-active-invite-per-email index refuses it */
  insert(row: NewInvite): Promise<string>;
  deleteById(id: string): Promise<void>;
  /** set revoked_at on an open invite; returns rows affected */
  revokeOpen(id: string, at: string): Promise<number>;
  /** replace token / expiry etc. on an open invite; returns rows affected */
  updateOpen(id: string, fields: Partial<InviteRow>): Promise<number>;
  /** set opened_at only if still null */
  markOpened(id: string, at: string): Promise<void>;
}

export interface Deps {
  store: InviteStore;
  /** verify a JWT and return the user id, or null */
  verifyJwt(jwt: string): Promise<string | null>;
  sendMail(msg: MailMessage): Promise<void>;
  now(): Date;
  random(n: number): Uint8Array;
  appUrl: string;
  log(event: string, fields?: Record<string, unknown>): void;
}

class HttpError extends Error {
  constructor(public status: number, public code: ErrorCode, public extra?: Record<string, unknown>) {
    super(code);
  }
}

/** CHANGE 2026-10-09 F03-FN-03: safe summary of a mailer error for the logs (name + first 300 chars of the message,
 *  with anything that looks like an email address masked). */
export function mailErr(e: unknown): Record<string, unknown> {
  const name = e instanceof Error ? e.name : typeof e;
  const msg = String(e instanceof Error ? e.message : e).replace(/[^\s@<>]+@[^\s@<>]+/g, "<addr>").slice(0, 300);
  return { name, message: msg };
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function makeHandler(deps: Deps): (req: Request) => Promise<Response> {
  const json = (origin: string | null, status: number, body: unknown) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { ...corsHeaders(origin), "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
    });

  async function requireAdmin(req: Request): Promise<string> {
    const h = req.headers.get("authorization") || "";
    const m = /^Bearer\s+(.+)$/i.exec(h);
    if (!m) throw new HttpError(403, "not_platform_admin");
    const uid = await deps.verifyJwt(m[1].trim());
    if (!uid) throw new HttpError(403, "not_platform_admin");
    if (!(await deps.store.isPlatformAdmin(uid))) throw new HttpError(403, "not_platform_admin");
    return uid;
  }

  const plusDays = (d: Date, n: number) => new Date(d.getTime() + n * DAY_MS).toISOString();
  const isOpen = (r: InviteRow) => !r.used_at && !r.revoked_at;
  const isExpired = (r: InviteRow, now: Date) => new Date(r.expires_at).getTime() <= now.getTime();
  const daysLeft = (r: InviteRow, now: Date) => Math.max(0, Math.ceil((new Date(r.expires_at).getTime() - now.getTime()) / DAY_MS));

  async function actCreate(uid: string, body: Record<string, unknown>) {
    const email = normalizeEmail(body.email);
    if (!isValidEmail(email)) throw new HttpError(400, "invalid_email");
    const draft = cleanDraftName(body.draft_name);
    if (draft === undefined) throw new HttpError(400, "invalid_name");
    const now = deps.now();

    const existing = await deps.store.findOpenByEmail(email);
    if (existing) {
      if (!isExpired(existing, now)) {
        throw new HttpError(409, "invite_exists", {
          invite_id: existing.id, draft_name: existing.draft_name, expires_at: existing.expires_at, days_left: daysLeft(existing, now),
        });
      }
      await deps.store.revokeOpen(existing.id, now.toISOString()); // D5: expired-but-not-revoked → revoked
    }

    let inviter = (await deps.store.inviterName(uid)) || FALLBACK_INVITER;
    inviter = inviter.trim().slice(0, 40) || FALLBACK_INVITER;
    const token = makeToken(deps.random);
    const row: NewInvite = {
      email, token_hash: await sha256Hex(token), draft_name: draft, invited_by: uid, inviter_name: inviter,
      expires_at: plusDays(now, INVITE_TTL_DAYS), last_sent_at: now.toISOString(), send_count: 1,
    };
    let id: string;
    try {
      id = await deps.store.insert(row);
    } catch (e) {
      if ((e as { code?: string })?.code === "23505") { // lost a race with a concurrent create for the same email
        const ex = await deps.store.findOpenByEmail(email);
        throw new HttpError(409, "invite_exists", ex
          ? { invite_id: ex.id, draft_name: ex.draft_name, expires_at: ex.expires_at, days_left: daysLeft(ex, now) }
          : {});
      }
      throw e;
    }
    try {
      await deps.sendMail(renderInviteEmail({ to: email, inviter, draftName: draft, link: inviteLink(deps.appUrl, token), expiresAt: row.expires_at }));
    } catch (e) {
      // CHANGE 2026-10-09 F03-FN-03: log the mailer's error name/message (never the password, token or address) —
      //   the first live send failed with no detail. What changed from 3.0.0-alpha.3.1: one log line, same response.
      deps.log("email_error", mailErr(e));
      await deps.store.deleteById(id); // criterion 9: no row left behind
      throw new HttpError(502, "email_failed");
    }
    deps.log("create", { invite: id });
    return { ok: true, invite_id: id, email, draft_name: draft, expires_at: row.expires_at };
  }

  async function loadForAdmin(body: Record<string, unknown>): Promise<InviteRow> {
    const id = typeof body.invite_id === "string" ? body.invite_id : "";
    if (!UUID_RE.test(id)) throw new HttpError(400, "invalid_request");
    const row = await deps.store.getById(id);
    if (!row) throw new HttpError(404, "invite_not_found");
    return row;
  }

  async function actResend(body: Record<string, unknown>) {
    const inv = await loadForAdmin(body);
    if (!isOpen(inv)) throw new HttpError(409, "invite_closed");
    const now = deps.now();
    const token = makeToken(deps.random);
    const expires = plusDays(now, INVITE_TTL_DAYS);
    // Send first with the NEW token; the row is changed only after the email went out, so a failed send leaves the
    // previous token (and link) valid — the "transaction" of spec §3 without a multi-statement DB transaction.
    try {
      await deps.sendMail(renderInviteEmail({
        to: inv.email, inviter: inv.inviter_name || FALLBACK_INVITER, draftName: inv.draft_name,
        link: inviteLink(deps.appUrl, token), expiresAt: expires,
      }));
    } catch (e) {
      deps.log("email_error", mailErr(e)); // CHANGE 2026-10-09 F03-FN-03 (see actCreate)
      throw new HttpError(502, "email_failed");
    }
    const n = await deps.store.updateOpen(inv.id, {
      token_hash: await sha256Hex(token), expires_at: expires, send_count: (inv.send_count || 1) + 1,
      last_sent_at: now.toISOString(), opened_at: null,
    });
    if (!n) throw new HttpError(409, "invite_closed"); // revoked/used between read and write
    deps.log("resend", { invite: inv.id });
    return { ok: true, invite_id: inv.id, expires_at: expires, send_count: (inv.send_count || 1) + 1 };
  }

  async function actRevoke(body: Record<string, unknown>) {
    const inv = await loadForAdmin(body);
    if (inv.used_at) throw new HttpError(409, "invite_used");
    if (!inv.revoked_at) {
      const n = await deps.store.revokeOpen(inv.id, deps.now().toISOString());
      if (!n) {
        const again = await deps.store.getById(inv.id);
        if (again?.used_at) throw new HttpError(409, "invite_used");
      }
    }
    deps.log("revoke", { invite: inv.id });
    return { ok: true, invite_id: inv.id };
  }

  async function actCheck(body: Record<string, unknown>) {
    const INVALID = { valid: false };
    const token = typeof body.token === "string" ? body.token : "";
    if (!TOKEN_RE.test(token)) return INVALID;
    const inv = await deps.store.findByHash(await sha256Hex(token));
    const now = deps.now();
    if (!inv || !isOpen(inv) || isExpired(inv, now)) return INVALID;
    if (!inv.opened_at) await deps.store.markOpened(inv.id, now.toISOString());
    return { valid: true, draft_name: inv.draft_name, inviter_name: inv.inviter_name || FALLBACK_INVITER, expires_at: inv.expires_at };
  }

  return async function handle(req: Request): Promise<Response> {
    const origin = req.headers.get("origin");
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(origin) });
    if (req.method !== "POST") return json(origin, 405, { error: "method_not_allowed" });
    let action = "";
    try {
      let body: Record<string, unknown>;
      try {
        const b = await req.json();
        body = b && typeof b === "object" && !Array.isArray(b) ? b as Record<string, unknown> : {};
      } catch {
        throw new HttpError(400, "invalid_request");
      }
      action = typeof body.action === "string" ? body.action : "";
      if (action === "check") return json(origin, 200, await actCheck(body));
      if (!["create", "resend", "revoke"].includes(action)) throw new HttpError(400, "invalid_request");
      const uid = await requireAdmin(req);
      const out = action === "create" ? await actCreate(uid, body) : action === "resend" ? await actResend(body) : await actRevoke(body);
      return json(origin, 200, out);
    } catch (e) {
      if (e instanceof HttpError) {
        deps.log("refused", { action, code: e.code });
        return json(origin, e.status, { error: e.code, ...(e.extra || {}) });
      }
      deps.log("error", { action, message: e instanceof Error ? e.name : "unknown" });
      if (action === "check") return json(origin, 500, { error: "server_error" });
      return json(origin, 500, { error: "server_error" });
    }
  };
}

// =====================================================================================================
// 3. Production wiring
// =====================================================================================================
const COLS = "id,email,token_hash,draft_name,inviter_name,expires_at,used_at,revoked_at,opened_at,send_count,last_sent_at,created_at";

export function supabaseStore(db: SupabaseClient): InviteStore {
  const t = () => db.from("manager_invites");
  const one = async (q: PromiseLike<{ data: unknown; error: unknown }>): Promise<InviteRow | null> => {
    const { data, error } = await q;
    if (error) throw error;
    return (data as InviteRow | null) ?? null;
  };
  return {
    async isPlatformAdmin(uid) {
      const { data, error } = await db.from("platform_admins").select("user_id").eq("user_id", uid).maybeSingle();
      if (error) throw error;
      return !!data;
    },
    async inviterName(uid) {
      const { data, error } = await db.from("trip_members").select("display_name").eq("user_id", uid).eq("status", "active")
        .order("joined_at", { ascending: false }).limit(1);
      if (error) throw error;
      return (data && data[0]?.display_name) || null;
    },
    findOpenByEmail: (email) => one(t().select(COLS).eq("email", email).is("used_at", null).is("revoked_at", null).maybeSingle()),
    getById: (id) => one(t().select(COLS).eq("id", id).maybeSingle()),
    findByHash: (hash) => one(t().select(COLS).eq("token_hash", hash).maybeSingle()),
    async insert(row) {
      const { data, error } = await t().insert(row).select("id").single();
      if (error) throw error;
      return (data as { id: string }).id;
    },
    async deleteById(id) {
      const { error } = await t().delete().eq("id", id);
      if (error) throw error;
    },
    async revokeOpen(id, at) {
      const { data, error } = await t().update({ revoked_at: at }).eq("id", id).is("used_at", null).is("revoked_at", null).select("id");
      if (error) throw error;
      return (data || []).length;
    },
    async updateOpen(id, fields) {
      const { data, error } = await t().update(fields).eq("id", id).is("used_at", null).is("revoked_at", null).select("id");
      if (error) throw error;
      return (data || []).length;
    },
    async markOpened(id, at) {
      const { error } = await t().update({ opened_at: at }).eq("id", id).is("opened_at", null);
      if (error) throw error;
    },
  };
}

/** CHANGE 2026-10-09 F03-MAIL-04: our own MIME + SMTP, replacing denomailer. denomailer re-wrapped long header lines
 *  without folding whitespace, so Gmail showed the whole message as raw text (F03-MAIL-03's pre-encoding did not help).
 *  What changed from 3.0.0-alpha.3.4: the message is built here with correctly folded RFC 2047 subject words and base64
 *  bodies (76-char lines), and sent with a minimal SMTP dialogue (EHLO, AUTH PLAIN, MAIL, RCPT, DATA, QUIT). */
const CRLF = "\r\n";
function b64Utf8(s: string): string {
  let bin = "";
  for (const byte of new TextEncoder().encode(s)) bin += String.fromCharCode(byte);
  return btoa(bin);
}
function wrap76(s: string): string {
  return s.replace(/.{1,76}/g, (m) => m + CRLF).replace(/\r\n$/, "");
}

/** Subject header value: ASCII as is; otherwise RFC 2047 base64 words (≤39 UTF-8 bytes each, split on character
 *  boundaries), folded with CRLF + space so every header line stays short. */
export function encodeSubjectHeader(subject: string): string {
  if (/^[\x20-\x7e]*$/.test(subject)) return subject;
  const enc = new TextEncoder();
  const words: string[] = [];
  let chunk = "";
  for (const ch of subject) {
    if (enc.encode(chunk + ch).length > 39) { words.push(chunk); chunk = ""; }
    chunk += ch;
  }
  if (chunk) words.push(chunk);
  return words.map((w) => `=?UTF-8?B?${b64Utf8(w)}?=`).join(CRLF + " ");
}

export function buildMime(p: { from: string; fromName: string; to: string; subject: string; text: string; html: string; date: Date; id: string }): string {
  const boundary = `pt-${p.id.replace(/-/g, "").slice(0, 24)}`; // short: keeps the Content-Type line under 78 chars
  const domain = p.from.split("@")[1] || "plan-travel";
  const headers = [
    `From: "${p.fromName}" <${p.from}>`,
    `To: <${p.to}>`,
    `Subject: ${encodeSubjectHeader(p.subject)}`,
    `Date: ${p.date.toUTCString()}`,
    `Message-ID: <${p.id}@${domain}>`,
    "MIME-Version: 1.0",
    `Content-Type: multipart/alternative; boundary="${boundary}"`,
  ];
  const part = (type: string, body: string) =>
    [`--${boundary}`, `Content-Type: ${type}; charset=UTF-8`, "Content-Transfer-Encoding: base64", "", wrap76(b64Utf8(body))].join(CRLF);
  return headers.join(CRLF) + CRLF + CRLF + part("text/plain", p.text) + CRLF + part("text/html", p.html) + CRLF + `--${boundary}--` + CRLF;
}

export type SmtpConn = { read(b: Uint8Array): Promise<number | null>; write(b: Uint8Array): Promise<number>; close(): void };

/** Minimal SMTP client (implicit TLS on 465 in production; any connection in tests). Throws Error("SMTP <code> …")
 *  on an unexpected reply; the reply text never contains the password. */
export async function smtpSend(conn: SmtpConn, o: { user: string; pass: string; from: string; to: string; data: string }): Promise<void> {
  const enc = new TextEncoder(), dec = new TextDecoder();
  let buf = "";
  async function reply(): Promise<{ code: number; text: string }> {
    for (;;) {
      const lines = buf.split(CRLF);
      for (let i = 0; i < lines.length - 1; i++) {
        if (/^\d{3} /.test(lines[i]) || /^\d{3}$/.test(lines[i])) {
          const text = lines.slice(0, i + 1).join("\n");
          buf = lines.slice(i + 1).join(CRLF);
          return { code: Number(lines[i].slice(0, 3)), text };
        }
      }
      const chunk = new Uint8Array(4096);
      const n = await conn.read(chunk);
      if (n === null) throw new Error("SMTP connection closed");
      buf += dec.decode(chunk.subarray(0, n), { stream: true });
    }
  }
  async function writeAll(bytes: Uint8Array) { // a TLS write may be partial: loop until every byte is sent
    let off = 0;
    while (off < bytes.length) off += await conn.write(bytes.subarray(off));
  }
  async function expect(ok: number[], cmd?: string) {
    if (cmd !== undefined) await writeAll(enc.encode(cmd + CRLF));
    const r = await reply();
    if (!ok.includes(r.code)) throw new Error(`SMTP ${r.code} ${r.text.slice(0, 200)}`);
  }
  try {
    await expect([220]);
    await expect([250], "EHLO plan-travel");
    await expect([235], "AUTH PLAIN " + b64Utf8(`\u0000${o.user}\u0000${o.pass}`));
    await expect([250], `MAIL FROM:<${o.from}>`);
    await expect([250, 251], `RCPT TO:<${o.to}>`);
    await expect([354], "DATA");
    const body = o.data.replace(/\r\n\./g, "\r\n..").replace(/^\./, "..");
    await expect([250], body + CRLF + ".");
    try { await conn.write(enc.encode("QUIT" + CRLF)); } catch { /* ignore */ }
  } finally {
    try { conn.close(); } catch { /* already closed */ }
  }
}

export function gmailSender(user: string, appPassword: string): (m: MailMessage) => Promise<void> {
  return async (m) => {
    const data = buildMime({ from: user, fromName: FROM_NAME, to: m.to, subject: m.subject, text: m.text, html: m.html, date: new Date(), id: crypto.randomUUID() });
    const conn = await Deno.connectTls({ hostname: "smtp.gmail.com", port: 465 });
    await smtpSend(conn, { user, pass: appPassword, from: user, to: m.to, data });
  };
}

// CHANGE 2026-10-09 F03-FN-02: the production wiring moved into an exported startServer() so the deployed entry point
//   (main.ts) can start it explicitly; import.meta.main is not guaranteed true inside the Supabase Edge Runtime.
//   What changed from the 2026-10-05 version: the same code, now wrapped in a function; behavior unchanged.
export function startServer(): void {
  const env = (k: string) => Deno.env.get(k) || "";
  const url = env("SUPABASE_URL");
  const key = env("PT_SERVICE_KEY") || env("SUPABASE_SERVICE_ROLE_KEY");
  const db = createClient(url, key, { auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false } });
  const handler = makeHandler({
    store: supabaseStore(db),
    async verifyJwt(jwt) {
      const { data, error } = await db.auth.getUser(jwt);
      return error || !data?.user ? null : data.user.id;
    },
    sendMail: gmailSender(env("GMAIL_USER"), env("GMAIL_APP_PASSWORD")),
    now: () => new Date(),
    random: (n) => crypto.getRandomValues(new Uint8Array(n)),
    appUrl: env("APP_URL") || DEFAULT_APP_URL,
    // only event names, invite ids and error codes — never tokens, hashes, emails or bodies
    log: (event, fields) => console.log(JSON.stringify({ fn: "invite-manager", v: FN_VERSION, event, ...(fields || {}) })),
  });
  Deno.serve(handler);
}

if (import.meta.main) startServer();
