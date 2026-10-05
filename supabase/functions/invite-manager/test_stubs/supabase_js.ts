// CHANGE 2026-10-05 F03-FN-02: offline test stub (no previous version). Stands in for npm:@supabase/supabase-js in
// test.ts runs without network; the tests never call it. Typed loosely (only the call shapes index.ts uses).
// deno-lint-ignore-file no-explicit-any
export type SupabaseClient = {
  from(table: string): any;
  auth: { getUser(jwt: string): Promise<{ data: { user: { id: string } | null } | null; error: unknown }> };
};
export function createClient(..._a: unknown[]): SupabaseClient {
  throw new Error("supabase-js stub: not available in unit tests");
}
