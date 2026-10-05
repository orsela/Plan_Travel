/* Plan_Travel F02 QA · service-worker sandbox · version 1.0.0
   CHANGE 2026-10-04 F02-QA: first version (no previous version).
   usage: node sw_harness.js <sw.js> <scopeUrl> <preexistingCache> [...]
   Runs sw.js in a vm sandbox with a fake CacheStorage that already holds the given caches, dispatches
   install -> activate -> a navigation fetch -> activate again, and prints JSON
   {opened, deleted, remaining, errors}. */
const fs = require('fs');
const vm = require('vm');
const [file, scope, ...pre] = process.argv.slice(2);
const store = new Map(pre.map(n => [n, new Map()]));
const opened = [], deleted = [], errors = [];
function cacheObj(name) {
  const c = store.get(name);
  return {
    add: async u => { c.set(String(u), 'x'); }, addAll: async us => { us.forEach(u => c.set(String(u), 'x')); },
    put: async (k) => { c.set(String(k && k.url || k), 'x'); }, match: async () => undefined,
    keys: async () => [...c.keys()].map(u => ({ url: u })), delete: async k => c.delete(String(k && k.url || k))
  };
}
const caches = {
  open: async n => { n = String(n); opened.push(n); if (!store.has(n)) store.set(n, new Map()); return cacheObj(n); },
  delete: async n => { n = String(n); deleted.push(n); return store.delete(n); },
  keys: async () => [...store.keys()], has: async n => store.has(String(n)),
  match: async () => undefined
};
const listeners = {};
class Resp { constructor(b, i) { this.body = b; this.ok = true; this.status = 200; this.type = 'basic'; this.headers = new Map(Object.entries((i && i.headers) || {})); } clone() { return this; } async text() { return String(this.body || ''); } }
class Req { constructor(u, i) { this.url = String(u && u.url || u); this.method = (i && i.method) || 'GET'; this.mode = (i && i.mode) || 'cors'; this.headers = new Map(); } }
const g = {
  console, setTimeout, clearTimeout, Promise, URL, Request: Req, Response: Resp, caches,
  fetch: async () => new Resp('ok'),
  addEventListener: (t, f) => (listeners[t] = listeners[t] || []).push(f),
  skipWaiting: async () => {}, clients: { claim: async () => {}, matchAll: async () => [] },
  location: new URL('sw.js', scope), registration: { scope, unregister: async () => true }
};
g.self = g; g.globalThis = g;
const ctx = vm.createContext(g);
async function fire(type, extra) {
  const ps = [];
  const ev = Object.assign({ type, waitUntil: p => ps.push(Promise.resolve(p)), respondWith: p => ps.push(Promise.resolve(p)) }, extra || {});
  for (const f of listeners[type] || []) { try { f(ev); } catch (e) { errors.push(type + ': ' + e.message); } }
  for (const p of ps) { try { await p; } catch (e) { errors.push(type + ' (async): ' + e.message); } }
}
(async () => {
  try { vm.runInContext(fs.readFileSync(file, 'utf8'), ctx, { filename: file }); } catch (e) { errors.push('load: ' + e.message); }
  await fire('install');
  await fire('activate');
  await fire('fetch', { request: new Req(scope, { mode: 'navigate' }) });
  await fire('activate');
  process.stdout.write(JSON.stringify({ opened, deleted, remaining: [...store.keys()], errors }));
})();
