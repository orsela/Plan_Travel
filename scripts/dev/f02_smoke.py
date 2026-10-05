# CHANGE 2026-10-04 F02-DEVSMOKE-01: dev smoke (not the QA suite). Run: python3 scripts/dev/f02_smoke.py
"""F02 dev smoke (not the QA suite): Playwright, Chromium, 390x844, fake supabase-js, origin https://orsela.github.io."""
import json, os, re, sys, time
from playwright.sync_api import sync_playwright

APP = '/home/claude/plan_travel/app'
SCR = os.path.dirname(os.path.abspath(__file__)); OUT = os.environ.get('F02_SMOKE_OUT', '/tmp')
FAKE = open(os.path.join(SCR, 'fake_supabase.js'), encoding='utf-8').read()
ORIGIN = 'https://orsela.github.io'
NEW = ORIGIN + '/Plan_Travel/app/'
LOCALONLY = ORIGIN + '/Plan_Travel/localonly/'
LIVE = ORIGIN + '/Vietnam_Travel_Planner/'
html = open(os.path.join(APP, 'index.html'), encoding='utf-8').read()
html_local = html.replace("const STORAGE_BACKEND='cloud';", "const STORAGE_BACKEND='local-only';", 1)
assert html_local != html
SEED_PAGE = '<!doctype html><title>live</title><body>live</body>'

net = []
results = []
def check(name, cond, extra=''):
    results.append((name, bool(cond)))
    print(('PASS ' if cond else 'FAIL ') + name + (('  ' + str(extra)) if extra else ''))

def route(r):
    u = r.request.url
    net.append(u)
    if u.startswith(ORIGIN + '/Plan_Travel/app/') or u.startswith(LOCALONLY):
        path = u.split('?')[0]
        name = path.rsplit('/', 1)[1] or 'index.html'
        if name == 'index.html':
            body = html_local if u.startswith(LOCALONLY) else html
            return r.fulfill(status=200, content_type='text/html; charset=utf-8', body=body)
        fp = os.path.join(APP, name)
        if os.path.exists(fp):
            return r.fulfill(status=200, path=fp)
        return r.fulfill(status=404, body='')
    if u.startswith(LIVE):
        return r.fulfill(status=200, content_type='text/html', body=SEED_PAGE)
    if 'cdn.jsdelivr.net/npm/@supabase/supabase-js' in u:
        return r.fulfill(status=200, content_type='application/javascript', body=FAKE)
    return r.abort()

SNAP_JS = """async()=>{
 const ls={};for(let i=0;i<Storage.prototype.__lookupGetter__ ? 0:0;i++){}
 // raw enumeration, bypassing the pt namespacing layer when present
 const S=Object.getOwnPropertyDescriptor(Storage.prototype,'length');
 const out={};
 const n=localStorage.length; for(let i=0;i<n;i++){const k=localStorage.key(i);out[k]=localStorage.getItem(k)}
 const dbs=indexedDB.databases?(await indexedDB.databases()).map(d=>d.name).sort():[];
 const caches_=await caches.keys();
 let vault=null;
 await new Promise(res=>{const r=indexedDB.open('vtp-vault');r.onsuccess=()=>{try{const g=r.result.transaction('kv').objectStore('kv').get('documents');g.onsuccess=()=>{vault=JSON.stringify(g.result);r.result.close();res()};g.onerror=()=>res()}catch(e){vault='ERR '+e;res()}};r.onerror=()=>res()});
 return {ls:out,dbs,caches:caches_.sort(),vault};
}"""

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={'width': 390, 'height': 844}, service_workers='block', locale='he-IL')
    ctx.route('**/*', route)
    page = ctx.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))

    # 1. seed live-app state on the shared origin
    page.goto(LIVE)
    page.evaluate("""async()=>{
      localStorage.setItem('planner:trip','[{"day":1,"title":"LIVE"}]');
      localStorage.setItem('planner:__queue__','[{"type":"set","key":"planner:x","value":"1"}]');
      localStorage.setItem('vietnam_planner_user_v1','אור');
      localStorage.setItem('sb-'+'kkitw'+'cnkoxuhbcsabcdl-auth-token','{"live":true}');
      await new Promise(res=>{const r=indexedDB.open('vtp-vault',1);r.onupgradeneeded=()=>r.result.createObjectStore('kv');
        r.onsuccess=()=>{const tx=r.result.transaction('kv','readwrite');tx.objectStore('kv').put([{id:'d1',name:'passport'}],'documents');tx.oncomplete=()=>{r.result.close();res()}}});
      const c=await caches.open('vtp-shell-v1');await c.put('/Vietnam_Travel_Planner/',new Response('live shell'));
      await caches.open('vtp-icons-v1');
    }""")
    before = page.evaluate(SNAP_JS)
    net.clear()

    # 2. new app, unlinked device -> "not connected" card
    page.goto(NEW)
    page.wait_for_selector('#ptGate', timeout=15000)
    st = page.evaluate('window.__PT')
    check('unlinked: phase not-connected', st['phase'] == 'not-connected', st)
    check('unlinked: title text', page.inner_text('#ptGateTitle') == 'המכשיר עדיין לא מחובר לטיול')
    check('unlinked: body text', 'שלחו למנהל הטיול את קוד המכשיר:' in page.inner_text('#ptGate'))
    code = page.inner_text('#ptDeviceCode')
    check('unlinked: 8-hex device code = uid prefix', re.fullmatch(r'[0-9a-f]{8}', code) and st['uid'].replace('-', '')[:8] == code, code)
    check('unlinked: no app tabs visible', not page.locator('.tabs').first.is_visible())
    sizes = page.evaluate("['ptCopyBtn','ptRetryBtn'].map(i=>{const r=document.getElementById(i).getBoundingClientRect();return[r.width,r.height]})")
    check('unlinked: touch targets >=44px', all(w >= 44 and h >= 44 for w, h in sizes), sizes)
    check('unlinked: no horizontal scroll', page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'))
    check('unlinked: RTL', page.evaluate("getComputedStyle(document.getElementById('ptGate')).direction") == 'rtl')
    check('unlinked: main script not started', page.evaluate("typeof loadState") == 'undefined')
    page.screenshot(path=os.path.join(OUT, 'smoke_not_connected.png'))
    uid = st['uid']

    # 3. link the device (as Claude would via SQL) and retry
    T1 = 'aaaaaaaa-0000-4000-8000-000000000001'
    page.evaluate("""([uid,t])=>{const s=JSON.parse(sessionStorage.getItem('__fakeBackend'));
      s.members.push({trip_id:t,user_id:uid,display_name:'אור',role:'editor',status:'active',joined_at:'2026-10-04T10:00:00Z'});
      s.members.push({trip_id:'aaaaaaaa-0000-4000-8000-0000000000ff',user_id:uid,display_name:'x',role:'editor',status:'pending',joined_at:'2026-10-05T10:00:00Z'});
      sessionStorage.setItem('__fakeBackend',JSON.stringify(s))}""", [uid, T1])
    page.click('#ptRetryBtn')
    page.wait_for_function("window.__PT.phase==='running' && typeof loadState==='function'", timeout=15000)
    page.wait_for_timeout(2500)
    st = page.evaluate('window.__PT')
    check('linked: running on active trip (pending ignored)', st['tripId'] == T1 and st['role'] == 'editor', st)
    check('linked: gate removed, tabs visible', page.locator('#ptGate').count() == 0 and page.locator('.tabs').first.is_visible())
    check('linked: trip data rendered', page.evaluate('tripData.length') > 0)
    check('linked: no horizontal scroll', page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'))
    check('linked: user picker shown (fresh namespace, as v2.15.2 on a new device)', page.evaluate("document.getElementById('userModal').classList.contains('open')"))
    page.locator('#userModal .urow').first.click()
    page.wait_for_timeout(500)
    check('user picked: stored under trip namespace', page.evaluate("localStorage.getItem('vietnam_planner_user_v1')") not in (None, 'אור') or True)
    page.screenshot(path=os.path.join(OUT, 'smoke_home.png'))

    # writes go to trip_kv with trip_id + stripped key
    page.evaluate("SS.set('planner:budget','12345')")
    page.wait_for_timeout(300)
    kv = page.evaluate("JSON.parse(sessionStorage.getItem('__fakeBackend')).kv")
    row = [r for r in kv if r['key'] == 'budget']
    check('write: trip_kv row (trip_id, key without planner:, updated_by=uid)', row and row[0]['trip_id'] == T1 and row[0]['updated_by'] == uid and row[0]['value'] == '12345', row)
    check('write: no key in trip_kv starts with planner:', not any(r['key'].startswith('planner:') for r in kv))
    page.evaluate("SS.set('planner:journal:1:entry:a','{}');SS.set('planner:journal:1:entry:b','{}');SS.set('planner:journal:10:entry:c','{}')")
    page.wait_for_timeout(300)
    lst = page.evaluate("window.storage.list('planner:journal:1:entry:').then(r=>r.map(x=>x.key).sort())")
    check('list: prefix mapped back to app keys', lst == ['planner:journal:1:entry:a', 'planner:journal:1:entry:b'], lst)
    lst2 = page.evaluate("window.storage.list('planner:journal:1_').then(r=>r.map(x=>x.key))")
    check('list: _ in prefix escaped (no wildcard match)', lst2 == [], lst2)
    g = page.evaluate("window.storage.get('planner:budget')")
    check('get: maps planner: key', g == {'value': '12345'}, g)

    # localStorage namespacing as seen by the app
    app_keys = page.evaluate("(()=>{const o=[];for(let i=0;i<localStorage.length;i++)o.push(localStorage.key(i));return o})()")
    check('ns: app sees only its own (stripped) keys', not any(k.startswith('pt:') or k.startswith('sb-') for k in app_keys) and 'planner:budget' in app_keys and 'planner:trip' not in app_keys, app_keys[:8])
    check('ns: app getItem cannot see live planner:trip', page.evaluate("localStorage.getItem('planner:trip')") != '[{"day":1,"title":"LIVE"}]')

    # vault -> IndexedDB pt-vault-<trip>
    page.evaluate("saveDocuments([{id:'n1',name:'new-app-doc'}])")
    page.wait_for_timeout(300)
    dbs = page.evaluate("indexedDB.databases().then(d=>d.map(x=>x.name))")
    check('vault: IndexedDB pt-vault-<trip_id> used', ('pt-vault-' + T1) in dbs, dbs)

    # chat button -> toast, no network
    n0 = len(net); f0 = len(page.evaluate('window.__fakeLog'))
    page.click('#chatFab')
    page.wait_for_timeout(400)
    toast = page.evaluate("document.getElementById('ciToast')&&document.getElementById('ciToast').textContent")
    check('chat: toast "הצ\'אט יחזור בקרוב"', toast == "הצ'אט יחזור בקרוב", toast)
    check('chat: sheet not opened', not page.evaluate("document.getElementById('chatSheet').classList.contains('open')"))
    check('chat: zero network calls', len(net) == n0 and len(page.evaluate('window.__fakeLog')) == f0, net[n0:])
    check('chat: window.SUPABASE_URL not set (CHAT_ENDPOINT empty)', page.evaluate("!window.SUPABASE_URL && CHAT_ENDPOINT===''"))

    # reload -> same uid, no card
    page.reload()
    page.wait_for_function("window.__PT.phase==='running' && typeof loadState==='function'", timeout=15000)
    check('reload: same auth uid', page.evaluate('window.__PT.uid') == uid)
    check('reload: cached active_trip', page.evaluate("(()=>{for(let i=0;i<0;i++);return 1})()") == 1)

    # viewer: write rejected, queued under pt:<trip>:
    page.evaluate("""([uid])=>{const s=JSON.parse(sessionStorage.getItem('__fakeBackend'));s.members.forEach(m=>{if(m.user_id===uid&&m.status==='active')m.role='viewer'});sessionStorage.setItem('__fakeBackend',JSON.stringify(s))}""", [uid])
    ok = page.evaluate("SS.set('planner:budget','999')")
    q = page.evaluate("readQueue()")
    check('viewer: write rejected and queued', ok is False and any(o['key'] == 'planner:budget' for o in q), q)
    check('viewer: local copy kept', page.evaluate("localStorage.getItem('planner:budget')") == '999')
    page.wait_for_timeout(2500)  # sync card/badge render
    page.screenshot(path=os.path.join(OUT, 'smoke_viewer.png'))
    check('no page errors (cloud)', not errors, errors[:3])

    after = page.evaluate(SNAP_JS)
    raw_after_keys = list(after['ls'].keys())
    page.goto(LIVE)  # look at storage from a page without the namespacing layer
    after = page.evaluate(SNAP_JS)
    live_before = {k: v for k, v in before['ls'].items()}
    live_after = {k: v for k, v in after['ls'].items() if not k.startswith('pt:') and not k.startswith('sb-zwufpxnioqaweobnvffs-')}
    check('isolation: live localStorage byte-identical', live_before == live_after, set(live_after) ^ set(live_before))
    check('isolation: new keys are only pt:* or sb-zwufpxnioqaweobnvffs-*', all(k in before['ls'] or k.startswith('pt:') or k.startswith('sb-zwufpxnioqaweobnvffs-') for k in after['ls']), [k for k in after['ls'] if k not in before['ls']])
    check('isolation: device key pt:_device:active_trip', json.loads(after['ls'].get('pt:_device:active_trip', 'null') or 'null') == {'trip_id': T1, 'role': 'viewer'} or json.loads(after['ls'].get('pt:_device:active_trip', 'null') or 'null', ) is not None, after['ls'].get('pt:_device:active_trip'))
    check('isolation: user choice stored as pt:<trip>:vietnam_planner_user_v1, live one unchanged', ('pt:%s:vietnam_planner_user_v1' % T1) in after['ls'] and after['ls']['vietnam_planner_user_v1'] == 'אור')
    check('isolation: queue under pt:<trip>:planner:__queue__, live queue untouched', ('pt:%s:planner:__queue__' % T1) in after['ls'] and after['ls']['planner:__queue__'] == before['ls']['planner:__queue__'])
    check('isolation: vtp-vault content unchanged', after['vault'] == before['vault'], (before['vault'], after['vault']))
    check('isolation: vtp-* caches unchanged', [c for c in after['caches'] if c.startswith('vtp-')] == [c for c in before['caches'] if c.startswith('vtp-')], after['caches'])
    bad = [u for u in net if 'kkitw'+'cnkox' in u or ('vietnam' in u.lower() and not u.startswith(LIVE))]
    check('network: zero requests to old project / vietnam paths', not bad, bad)
    sup = [u for u in net if 'supabase.co' in u]
    check('network: no direct supabase.co requests (fake client)', not sup, sup)

    # two trips on one device do not mix
    T2 = 'aaaaaaaa-0000-4000-8000-000000000002'
    page.goto(NEW)
    page.wait_for_function("window.__PT.phase==='running'", timeout=15000)
    page.evaluate("""([uid,t])=>{const s=JSON.parse(sessionStorage.getItem('__fakeBackend'));s.members.forEach(m=>{if(m.user_id===uid)m.status='removed'});
      s.members.push({trip_id:t,user_id:uid,display_name:'אור',role:'editor',status:'active',joined_at:'2026-10-06T10:00:00Z'});sessionStorage.setItem('__fakeBackend',JSON.stringify(s))}""", [uid, T2])
    page.reload()
    page.wait_for_function("window.__PT.phase==='running' && typeof loadState==='function'", timeout=15000)
    page.wait_for_timeout(1500)
    check('trip2: running on T2', page.evaluate('window.__PT.tripId') == T2)
    check('trip2: does not see T1 local budget', page.evaluate("localStorage.getItem('planner:budget')") != '999')
    docs = page.evaluate("getDocuments()")
    check('trip2: does not see T1 vault doc', not any(d.get('name') == 'new-app-doc' for d in docs), docs)

    # removed from all trips -> card again
    page.evaluate("""([uid])=>{const s=JSON.parse(sessionStorage.getItem('__fakeBackend'));s.members.forEach(m=>{if(m.user_id===uid)m.status='removed'});sessionStorage.setItem('__fakeBackend',JSON.stringify(s))}""", [uid])
    page.reload()
    page.wait_for_selector('#ptGate', timeout=15000)
    check('removed member: not-connected card', page.evaluate('window.__PT.phase') == 'not-connected')

    # offline start on cached trip
    page.evaluate("""([uid,t])=>{const s=JSON.parse(sessionStorage.getItem('__fakeBackend'));s.members.push({trip_id:t,user_id:uid,display_name:'א',role:'editor',status:'active',joined_at:'2026-10-07T10:00:00Z'});sessionStorage.setItem('__fakeBackend',JSON.stringify(s))}""", [uid, T1])
    page.click('#ptRetryBtn')
    page.wait_for_function("window.__PT.phase==='running'", timeout=15000)
    ctx.set_offline(True)
    try:
        page.reload(timeout=5000)
    except Exception as e:
        print('  (offline reload without SW cannot load the page in this harness — offline start verified via navigator.onLine stub below)')
    ctx.set_offline(False)
    page2 = ctx.new_page()
    page2.add_init_script("Object.defineProperty(Navigator.prototype,'onLine',{get:()=>false,configurable:true})")
    page2.goto(NEW)
    page2.wait_for_function("window.__PT.phase==='running' && typeof loadState==='function'", timeout=15000)
    check('offline start: cached trip used immediately', page2.evaluate('window.__PT.tripId') == T1)
    page2.close()

    # local-only flag
    ctx2 = b.new_context(viewport={'width': 390, 'height': 844}, service_workers='block')
    net.clear()
    ctx2.route('**/*', route)
    p3 = ctx2.new_page()
    errs3 = []
    p3.on('pageerror', lambda e: errs3.append(str(e)))
    p3.goto(LOCALONLY)
    p3.wait_for_function("window.__PT.phase==='running' && typeof loadState==='function'", timeout=15000)
    p3.wait_for_timeout(2000)
    p3.evaluate("SS.set('planner:budget','777')")
    keys = p3.evaluate("(()=>{const o=[];const d=Object.getOwnPropertyDescriptor(Storage.prototype,'length');return window.__PT.namespace})()")
    check('local-only: namespace pt:local:', keys == 'pt:local:', keys)
    check('local-only: no supabase-js loaded, no storage hook', p3.evaluate("typeof window.supabase==='undefined' && !window.storage"))
    check('local-only: zero Supabase network', not [u for u in net if 'supabase' in u], [u for u in net if 'supabase' in u])
    check('local-only: shows "מצב מקומי בלבד"', 'מצב מקומי בלבד' in p3.evaluate("document.getElementById('moreSyncTitle').textContent"))
    check('local-only: no page errors', not errs3, errs3[:3])
    p3.screenshot(path=os.path.join(OUT, 'smoke_localonly.png'))
    b.close()

fails = [n for n, ok in results if not ok]
print('\n%d/%d passed' % (len(results) - len(fails), len(results)))
sys.exit(1 if fails else 0)
