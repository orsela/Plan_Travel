/* CHANGE 2026-10-04 F02-DEVSMOKE-01: minimal fake supabase-js for the dev smoke only (in-memory RLS model in sessionStorage). */
/* Minimal fake supabase-js for the F02 dev smoke test (NOT the QA suite). Backend state lives in sessionStorage
   (survives reloads in one tab; the app's namespacing layer only wraps localStorage). */
(function(){
 const BK='__fakeBackend';
 const load=()=>{try{return JSON.parse(sessionStorage.getItem(BK))||{users:[],members:[],kv:[]}}catch{return{users:[],members:[],kv:[]}}};
 const save=s=>sessionStorage.setItem(BK,JSON.stringify(s));
 window.__fakeLog=window.__fakeLog||[];
 const log=e=>{window.__fakeLog.push(e);try{const a=JSON.parse(sessionStorage.getItem('__fakeLogAll')||'[]');a.push(e);sessionStorage.setItem('__fakeLogAll',JSON.stringify(a))}catch{}};
 function uuid(){return'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g,c=>{const r=Math.random()*16|0;return(c==='x'?r:(r&3|8)).toString(16)})}
 function createClient(url,key,opts){
  log({op:'createClient',url,key});
  const st=(opts&&opts.auth&&opts.auth.storage)||null;
  const SK='sb-'+new URL(url).hostname.split('.')[0]+'-auth-token';
  let uid=null;
  try{const s=st&&st.getItem(SK);if(s)uid=JSON.parse(s).user.id}catch{}
  const auth={
   async getSession(){log({op:'getSession'});return{data:{session:uid?{user:{id:uid}}:null},error:null}},
   async signInAnonymously(){log({op:'signInAnonymously'});uid=uuid();const s=load();s.users.push(uid);save(s);st&&st.setItem(SK,JSON.stringify({user:{id:uid}}));return{data:{user:{id:uid},session:{}},error:null}}
  };
  function myTrips(roles){return load().members.filter(m=>m.user_id===uid&&m.status==='active'&&(!roles||roles.includes(m.role))).map(m=>m.trip_id)}
  function from(table){
   const q={table,filters:[],op:'select',order:null,single:false,payload:null,ret:false};
   const b={
    select(){if(q.op==='select'||q.op==='delete'){if(q.op==='delete')q.ret=true}return b},
    eq(c,v){q.filters.push(r=>String(r[c])===String(v));return b},
    like(c,p){const re=new RegExp('^'+p.replace(/\\([\\%_])|([%_])|([.*+?^${}()|[\]])/g,(m,esc,wild,meta)=>esc?esc.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'):wild?(wild==='%'?'.*':'.'):'\\'+meta)+'$','s');q.filters.push(r=>re.test(r[c]));return b},
    order(c,o){q.order=[c,o&&o.ascending===false?-1:1];return b},
    maybeSingle(){q.single=true;return b},
    upsert(p){q.op='upsert';q.payload=p;return b},
    delete(){q.op='delete';return b},
    then(res,rej){return Promise.resolve().then(run).then(res,rej)}
   };
   function run(){
    log({op:q.op,table,payload:q.payload});
    if(!uid)return{data:null,error:{message:'not authenticated'}};
    const s=load();
    if(table==='trip_members'){
     let rows=s.members.filter(m=>m.user_id===uid||myTrips().includes(m.trip_id)).filter(r=>q.filters.every(f=>f(r)));
     if(q.order)rows.sort((a,b)=>(a[q.order[0]]>b[q.order[0]]?1:-1)*q.order[1]);
     return{data:rows,error:null};
    }
    if(table==='trip_kv'){
     if(q.op==='select'){const rows=s.kv.filter(r=>myTrips().includes(r.trip_id)).filter(r=>q.filters.every(f=>f(r)));return{data:q.single?(rows[0]||null):rows,error:null}}
     if(q.op==='upsert'){const p=q.payload;if(!myTrips(['manager','editor']).includes(p.trip_id))return{data:null,error:{message:'new row violates row-level security policy',code:'42501'}};
      const i=s.kv.findIndex(r=>r.trip_id===p.trip_id&&r.key===p.key);const row={trip_id:p.trip_id,key:p.key,value:p.value,updated_by:uid,updated_at:new Date().toISOString()};if(i>-1)s.kv[i]=row;else s.kv.push(row);save(s);return{data:null,error:null}}
     if(q.op==='delete'){const w=myTrips(['manager','editor']);const del=s.kv.filter(r=>w.includes(r.trip_id)&&q.filters.every(f=>f(r)));s.kv=s.kv.filter(r=>!del.includes(r));save(s);return{data:del.map(r=>({key:r.key})),error:null}}
    }
    return{data:null,error:{message:'unknown table'}};
   }
   return b;
  }
  return{auth,from};
 }
 window.supabase={createClient};
})();
