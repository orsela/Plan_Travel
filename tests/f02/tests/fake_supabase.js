/* Plan_Travel F02 QA · fake supabase-js · version 1.0.0
   CHANGE 2026-10-04 F02-QA: first version (no previous version).

   Served by the Playwright route handler IN PLACE OF https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.
   Exposes the same UMD global as the real library: window.supabase.createClient(url, key, options).

   All data + the RLS model live in a Python-side store (FakeBackend in test_f02.py), reached through a
   Playwright binding `window.__fakeBackend(msg)`. That makes the state shared between browser contexts
   (two "devices"), atomic per call, and it survives page reloads. Without the binding every call returns
   an error object (the fake never touches the network).

   What it implements (the parts the F02 spec needs, tolerant of the common supabase-js chain shapes):
   - auth: getSession / signInAnonymously / onAuthStateChange / getUser / signOut / refreshSession /
     setSession. Session persisted like supabase-js v2: JSON under options.auth.storageKey or the default
     `sb-<project ref>-auth-token`, in options.auth.storage or globalThis.localStorage (read at call time).
   - from(table): select / insert / upsert(onConflict, ignoreDuplicates) / update / delete; filters eq neq
     gt gte lt lte like ilike is in match filter not; order / limit / range / single / maybeSingle /
     abortSignal / returns / throwOnError / csv(no). Resolves {data, error, count, status, statusText}.
   - channel / removeChannel / removeAllChannels / getChannels (stubs, logged), rpc and functions.invoke
     (logged, always error), storage.from (logged, always error).
   - Offline: when navigator.onLine is false, or the backend is in 'down' mode, calls resolve with the
     same error shape supabase-js gives for a failed fetch. 'hang' mode never resolves (timeout tests).
   Every call is recorded in window.__fakeLog (this page) and in the backend log (whole test).
   Test control from the page: window.__fake.{state(), log(), setMode(m), link(opts), control(msg)}. */
(function () {
  'use strict';
  var LOG = (window.__fakeLog = window.__fakeLog || []);
  var FAKE_VERSION = '1.0.0';

  function now() { return Date.now(); }
  function clone(x) { return x === undefined ? undefined : JSON.parse(JSON.stringify(x)); }
  function log(kind, extra) {
    var e = Object.assign({ t: now(), kind: kind, page: String(location.pathname) }, extra || {});
    LOG.push(e);
    return e;
  }
  function backend(msg) {
    if (typeof window.__fakeBackend !== 'function') {
      return Promise.resolve({ error: { message: 'fake backend not attached', code: 'FAKE', details: '', hint: '' }, status: 500 });
    }
    msg.online = navigator.onLine !== false;
    msg.page = String(location.pathname);
    return window.__fakeBackend(msg);
  }
  var NEVER = function () { return new Promise(function () {}); };
  function fetchError() {
    return { message: 'TypeError: Failed to fetch', details: 'TypeError: Failed to fetch', hint: '', code: '' };
  }

  function projectRef(url) {
    try { return new URL(url).hostname.split('.')[0]; } catch (e) { return 'unknown'; }
  }

  function createClient(url, key, options) {
    options = options || {};
    var authOpts = options.auth || {};
    var ref = projectRef(url);
    var storageKey = authOpts.storageKey || ('sb-' + ref + '-auth-token');
    var persist = authOpts.persistSession !== false;
    var memSession = null;
    var listeners = [];
    var clientId = Math.random().toString(36).slice(2, 10);

    var created = log('createClient', {
      url: String(url), key: String(key), clientId: clientId, storageKey: storageKey,
      persistSession: persist, customStorage: !!authOpts.storage,
      autoRefreshToken: authOpts.autoRefreshToken !== false,
      optionKeys: Object.keys(options)
    });
    backend({ op: 'log', entry: created });

    function store() { return authOpts.storage || globalThis.localStorage; }
    async function readSession() {
      if (!persist) return memSession;
      try {
        var raw = await store().getItem(storageKey);
        return raw ? JSON.parse(raw) : null;
      } catch (e) { return null; }
    }
    async function writeSession(s) {
      if (!persist) { memSession = s; return; }
      try {
        if (s) await store().setItem(storageKey, JSON.stringify(s));
        else await store().removeItem(storageKey);
      } catch (e) {}
    }
    function emit(event, session) {
      log('auth.event', { event: event, uid: session && session.user ? session.user.id : null });
      listeners.slice().forEach(function (l) {
        setTimeout(function () { try { l.cb(event, session); } catch (e) { console.error(e); } }, 0);
      });
    }
    async function token() {
      var s = await readSession();
      return s && s.access_token ? s.access_token : null;
    }

    var auth = {
      getSession: async function () {
        var s = await readSession();
        log('auth.getSession', { clientId: clientId, uid: s && s.user ? s.user.id : null });
        return { data: { session: s }, error: null };
      },
      getUser: async function (jwt) {
        var t = jwt || (await token());
        log('auth.getUser', { clientId: clientId });
        if (!t) return { data: { user: null }, error: { name: 'AuthSessionMissingError', message: 'Auth session missing!', status: 400 } };
        var r = await backend({ op: 'auth.getUser', url: url, key: key, token: t });
        if (r && r.hang) return NEVER();
        if (r && r.error) return { data: { user: null }, error: r.error };
        return { data: { user: r.user }, error: null };
      },
      signInAnonymously: async function (creds) {
        log('auth.signInAnonymously', { clientId: clientId, url: String(url) });
        var r = await backend({ op: 'auth.signInAnonymously', url: url, key: key, options: creds && creds.options ? clone(creds.options) : null });
        if (r && r.hang) return NEVER();
        if (!r || r.error) {
          return { data: { user: null, session: null }, error: (r && r.error) || { name: 'AuthRetryableFetchError', message: 'Failed to fetch', status: 0 } };
        }
        await writeSession(r.session);
        emit('SIGNED_IN', r.session);
        return { data: { user: r.session.user, session: r.session }, error: null };
      },
      onAuthStateChange: function (cb) {
        var id = Math.random().toString(36).slice(2);
        var entry = { id: id, cb: cb };
        listeners.push(entry);
        log('auth.onAuthStateChange', { clientId: clientId });
        readSession().then(function (s) {
          setTimeout(function () { try { cb('INITIAL_SESSION', s); } catch (e) { console.error(e); } }, 0);
        });
        return { data: { subscription: { id: id, callback: cb, unsubscribe: function () {
          listeners = listeners.filter(function (l) { return l !== entry; });
        } } } };
      },
      signOut: async function () {
        log('auth.signOut', { clientId: clientId });
        await writeSession(null);
        emit('SIGNED_OUT', null);
        return { error: null };
      },
      refreshSession: async function () {
        var s = await readSession();
        log('auth.refreshSession', { clientId: clientId });
        if (!s) return { data: { user: null, session: null }, error: { name: 'AuthSessionMissingError', message: 'Auth session missing!', status: 400 } };
        if (navigator.onLine === false) return { data: { user: null, session: null }, error: { name: 'AuthRetryableFetchError', message: 'Failed to fetch', status: 0 } };
        s.expires_at = Math.floor(now() / 1000) + 3600;
        await writeSession(s);
        emit('TOKEN_REFRESHED', s);
        return { data: { user: s.user, session: s }, error: null };
      },
      setSession: async function (tokens) {
        log('auth.setSession', { clientId: clientId });
        var s = await readSession();
        if (s && tokens && tokens.access_token === s.access_token) return { data: { user: s.user, session: s }, error: null };
        return { data: { user: null, session: null }, error: { message: 'fake: setSession only accepts the stored session', status: 400 } };
      },
      startAutoRefresh: async function () {}, stopAutoRefresh: async function () {},
      admin: {}
    };

    /* ---------- query builder ---------- */
    function Builder(table) {
      this.req = { table: String(table), action: 'select', columns: '*', values: null, options: {},
                   filters: [], order: [], limit: null, range: null, single: false, maybe: false,
                   returning: false, count: null, head: false };
      this._throw = false;
      this._promise = null;
    }
    var B = Builder.prototype;
    B.select = function (columns, opts) {
      opts = opts || {};
      if (this.req.action === 'select') {
        this.req.columns = columns == null ? '*' : String(columns);
        this.req.count = opts.count || null;
        this.req.head = !!opts.head;
      } else {
        this.req.returning = true;
        this.req.columns = columns == null ? '*' : String(columns);
      }
      return this;
    };
    B.insert = function (values, opts) { this.req.action = 'insert'; this.req.values = clone(values); this.req.options = clone(opts || {}); return this; };
    B.upsert = function (values, opts) { this.req.action = 'upsert'; this.req.values = clone(values); this.req.options = clone(opts || {}); return this; };
    B.update = function (values, opts) { this.req.action = 'update'; this.req.values = clone(values); this.req.options = clone(opts || {}); return this; };
    B['delete'] = function (opts) { this.req.action = 'delete'; this.req.options = clone(opts || {}); return this; };
    function f(op) { return function (col, val) { this.req.filters.push({ op: op, col: String(col), val: clone(val) }); return this; }; }
    ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'like', 'ilike', 'is', 'in', 'contains', 'containedBy'].forEach(function (op) { B[op] = f(op); });
    B.likeAllOf = function (col, pats) { var s = this; (pats || []).forEach(function (p) { s.like(col, p); }); return this; };
    B.match = function (obj) { var s = this; Object.keys(obj || {}).forEach(function (k) { s.eq(k, obj[k]); }); return this; };
    B.filter = function (col, op, val) {
      op = String(op);
      if (op === 'in' && typeof val === 'string') val = val.replace(/^\(|\)$/g, '').split(',').map(function (x) { return x.trim().replace(/^"|"$/g, ''); });
      this.req.filters.push({ op: op, col: String(col), val: clone(val) });
      return this;
    };
    B.not = function (col, op, val) { this.req.filters.push({ op: 'not.' + String(op), col: String(col), val: clone(val) }); return this; };
    B.or = function (expr) { this.req.filters.push({ op: 'or', col: '', val: String(expr) }); return this; };
    B.order = function (col, opts) { opts = opts || {}; this.req.order.push({ col: String(col), asc: opts.ascending !== false, nullsFirst: !!opts.nullsFirst }); return this; };
    B.limit = function (n) { this.req.limit = Number(n); return this; };
    B.range = function (a, b) { this.req.range = [Number(a), Number(b)]; return this; };
    B.single = function () { this.req.single = true; return this; };
    B.maybeSingle = function () { this.req.maybe = true; return this; };
    B.abortSignal = function () { return this; };
    B.returns = function () { return this; };
    B.overrideTypes = function () { return this; };
    B.csv = function () { this.req.csv = true; return this; };
    B.explain = function () { return this; };
    B.throwOnError = function () { this._throw = true; return this; };
    B.setHeader = function () { return this; };
    B._run = function () {
      if (this._promise) return this._promise;
      var req = this.req, self = this;
      this._promise = (async function () {
        var t = await token();
        var entry = log('query', { clientId: clientId, url: String(url), table: req.table, action: req.action, req: clone(req), hasToken: !!t });
        var r = await backend({ op: 'query', url: url, key: key, token: t, req: req, logId: entry.t });
        if (r && r.hang) return NEVER();
        var res = { data: r && 'data' in r ? r.data : null, error: (r && r.error) || null,
                    count: r && 'count' in r ? r.count : null, status: (r && r.status) || 0,
                    statusText: (r && r.statusText) || '' };
        entry.result = { status: res.status, error: res.error, rows: Array.isArray(res.data) ? res.data.length : (res.data ? 1 : 0) };
        if (self._throw && res.error) { var err = new Error(res.error.message); Object.assign(err, res.error); throw err; }
        return res;
      })();
      return this._promise;
    };
    B.then = function (onOk, onErr) { return this._run().then(onOk, onErr); };
    B['catch'] = function (onErr) { return this._run()['catch'](onErr); };
    B['finally'] = function (fn) { return this._run()['finally'](fn); };

    function from(table) { return new Builder(table); }

    /* ---------- realtime / functions / rpc / storage stubs ---------- */
    var channels = [];
    function channel(name, opts) {
      log('realtime.channel', { clientId: clientId, name: String(name) });
      var ch = {
        topic: 'realtime:' + name, bindings: [],
        on: function (type, filter, cb) { ch.bindings.push({ type: type, filter: clone(filter) }); log('realtime.on', { name: String(name), type: type, filter: clone(filter) }); return ch; },
        subscribe: function (cb) { log('realtime.subscribe', { name: String(name) }); if (cb) setTimeout(function () { cb(navigator.onLine === false ? 'CHANNEL_ERROR' : 'SUBSCRIBED'); }, 0); return ch; },
        unsubscribe: function () { return Promise.resolve('ok'); },
        send: function () { return Promise.resolve('ok'); },
        track: function () { return Promise.resolve('ok'); }, untrack: function () { return Promise.resolve('ok'); },
        presenceState: function () { return {}; }
      };
      channels.push(ch);
      return ch;
    }
    async function rpc(fn, args) {
      log('rpc', { clientId: clientId, fn: String(fn) });
      var r = await backend({ op: 'rpc', url: url, key: key, token: await token(), fn: String(fn), args: clone(args || {}) });
      return { data: null, error: (r && r.error) || { message: 'fake: rpc not available', code: 'PGRST202' }, status: 404, statusText: 'Not Found' };
    }
    var functions = {
      invoke: async function (name, opts) {
        log('functions.invoke', { clientId: clientId, name: String(name) });
        var r = await backend({ op: 'functions.invoke', url: url, key: key, name: String(name) });
        return { data: null, error: (r && r.error) || { name: 'FunctionsFetchError', message: 'fake: functions not available' } };
      },
      setAuth: function () {}
    };
    var storage = {
      from: function (bucket) {
        log('storage.from', { clientId: clientId, bucket: String(bucket) });
        var err = async function () { return { data: null, error: { message: 'fake: storage not available' } }; };
        return { upload: err, download: err, remove: err, list: err, createSignedUrl: err, getPublicUrl: function () { return { data: { publicUrl: '' } }; } };
      }
    };

    return {
      supabaseUrl: url, supabaseKey: key, auth: auth, from: from, schema: function () { return { from: from, rpc: rpc }; },
      rpc: rpc, functions: functions, storage: storage, channel: channel,
      getChannels: function () { return channels.slice(); },
      removeChannel: function (ch) { channels = channels.filter(function (c) { return c !== ch; }); log('realtime.removeChannel', {}); return Promise.resolve('ok'); },
      removeAllChannels: function () { channels = []; log('realtime.removeAllChannels', {}); return Promise.resolve(['ok']); },
      realtime: { setAuth: function () {}, connect: function () {}, disconnect: function () {} },
      __fake: true
    };
  }

  window.supabase = { createClient: createClient, __fake: true, __fakeVersion: FAKE_VERSION };
  window.__fake = {
    version: FAKE_VERSION,
    log: function () { return LOG.slice(); },
    control: function (msg) { return backend(Object.assign({ op: 'control' }, msg)); },
    state: function () { return backend({ op: 'control', action: 'state' }); },
    setMode: function (mode) { return backend({ op: 'control', action: 'setMode', mode: mode }); },
    link: function (opts) { return backend(Object.assign({ op: 'control', action: 'link' }, opts || {})); }
  };
  log('fake.loaded', { version: FAKE_VERSION });
})();
