# CHANGE 2026-10-04 F02-DEVCHK-01: dev self-check (not the QA suite): node --check per inline script, protected-function byte compare vs v2.15.2, main-script line diff, duplicate ids, forbidden strings.
import re,subprocess,sys,os,tempfile,collections
OLD='/home/claude/orsela/vietnam_travel_planner/index.html'; NEW='/home/claude/plan_travel/app/index.html'
o=open(OLD,encoding='utf-8').read(); n=open(NEW,encoding='utf-8').read()
from html.parser import HTMLParser
class P(HTMLParser):
  def __init__(s):super().__init__(convert_charrefs=False);s.out=[];s.cur=None
  def handle_starttag(s,t,a):
    if t=='script' and not dict(a).get('src'):s.cur=[]
  def handle_endtag(s,t):
    if t=='script' and s.cur is not None:s.out.append(''.join(s.cur));s.cur=None
  def handle_data(s,d):
    if s.cur is not None:s.cur.append(d)
def blocks(s):
  p=P();p.feed(s);return p.out
ok=True
for i,b in enumerate(blocks(n)):
  f=tempfile.NamedTemporaryFile('w',suffix='.js',delete=False,encoding='utf-8');f.write(b);f.close()
  r=subprocess.run(['node','--check',f.name],capture_output=True,text=True)
  print(f'script block {i}: {len(b)} chars node --check', 'OK' if r.returncode==0 else 'FAIL '+r.stderr[:300]); ok&=r.returncode==0
# protected functions: extract by brace matching from declaration start
def extract(s,start_pat):
  m=re.search(start_pat,s)
  if not m: return None
  i=s.index('{' if 'function' in start_pat else '[',m.end()-1 if False else m.start())
  open_c=s[i]; close_c='}' if open_c=='{' else ']'
  d=0;j=i;q=None
  while True:
    c=s[j]
    if c==open_c:d+=1
    elif c==close_c:
      d-=1
      if d==0:break
    j+=1
  return s[m.start():j+1]
pats={'drawMap':r'function drawMap\(','drawGoogleMap':r'function drawGoogleMap\(','drawSchematicMap':r'function drawSchematicMap\(','fallbackToSchematic':r'function fallbackToSchematic\(','SEED_DATA':r'const SEED_DATA=','stayBlock':r'function stayBlock\(','bookingUrl':r'function bookingUrl\('}
for k,p in pats.items():
  a=extract(o,p);b=extract(n,p)
  same=a is not None and a==b
  print(f'protected {k}: {len(a or "")} bytes -> {"IDENTICAL" if same else "DIFFERENT"}'); ok&=same
# main script body identical except APP_VERSION line
def main(s,tag):
  i=s.index(tag)+len(tag);return s[i:s.index('</script>',i)]
mo=main(o,"<script>\n'use strict';"); mn=main(n,'<script type="text/plain" id="ptMainScript">\n\'use strict\';')
lo=mo.split('\n');ln=mn.split('\n');diff=[(i,a,b) for i,(a,b) in enumerate(zip(lo,ln)) if a!=b]
print('main script lines old/new',len(lo),len(ln),'differing lines:',len(diff),[d[1][:40] for d in diff]); ok&=len(lo)==len(ln) and len(diff)==1 and 'APP_VERSION' in diff[0][1]
ids=lambda s:collections.Counter(re.findall(r'\bid="([^"]+)"',s))
do={k for k,v in ids(o).items() if v>1}; dn={k for k,v in ids(n).items() if v>1}
print('duplicate ids v2.15.2:',sorted(do),' new:',sorted(dn),' new duplicates:',sorted(dn-do)); ok&=not(dn-do)
for f in ['index.html','sw.js','manifest.json']:
  t=open('/home/claude/plan_travel/app/'+f,encoding='utf-8').read()
  bad=[x for x in ['kkitw'+'cnkox','e7CzGjayv7'+'yHKDbxypzTjA','vietnam-trip-2026','planner_kv','localStorage.clear('] if x in t]
  print(f,'forbidden strings:',bad or 'none'); ok&=not bad
print('ALL OK' if ok else 'PROBLEMS'); sys.exit(0 if ok else 1)
