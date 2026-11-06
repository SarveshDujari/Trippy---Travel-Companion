"""Operator UI: read-only trip views plus clearly labelled simulated demo controls."""

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trippy · Trip control</title>
<style>
:root{color-scheme:dark}body{font:15px system-ui;background:#101820;color:#e7f0f5;
margin:0;padding:28px}main{max-width:1360px;margin:auto}h1{font-size:36px;margin:0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:18px}
section,.controls{background:#1a2935;border:1px solid #304454;border-radius:16px;
padding:20px;margin:16px 0}.wide{grid-column:1/-1}
table{width:100%;border-collapse:collapse;font-size:12px}td,th{text-align:left;
padding:9px 5px;border-bottom:1px solid #304454;overflow-wrap:anywhere}
button,input{padding:10px 14px;border:1px solid #476176;border-radius:8px;margin:4px}
button{background:#73e0bb;color:#10251e;cursor:pointer}input{background:#101820}
#status{color:#a6c0d1}h2{font-size:19px}p{line-height:1.6}pre{white-space:pre-wrap;
overflow-wrap:anywhere;font-size:12px}.tag{color:#73e0bb}svg{width:100%;min-height:170px}
.timeline{display:flex;gap:14px;overflow:auto}.stop{min-width:180px;padding:14px;
border-left:3px solid #73e0bb;background:#101820}a{color:#73e0bb}
</style><main><h1>Trippy · Trip control</h1>
<p id="mode">Family travel, visible step by step</p>
<div class="controls"><input id="token" type="password" placeholder="Private operator token"
autocomplete="off"><button id="refresh">Refresh status</button>
<span id="status">Enter token to load</span><hr>
<span class="tag">SIMULATED DEMO CONTROLS</span>
<input id="delay-minutes" type="number" min="1" max="360" value="120" aria-label="Delay minutes">
<button id="delay">Simulate flight delay</button><button id="demo">Enable cached demo</button>
<p>No real bookings or payments. Delays create proposals requiring Telegram approval.
Cached demo reads historical tool results only; it does not fabricate missing data.</p></div>
<div class="grid" id="cards"></div></main>
<script>
const $=id=>document.getElementById(id);let demo=false,ledgerOffset=0;
function node(tag,text){const n=document.createElement(tag);
if(text!=null)n.textContent=text;return n;}
async function api(path,body){
const r=await fetch(path,{method:body===undefined?'GET':'POST',cache:'no-store',
headers:{Authorization:'Bearer '+$('token').value,'Content-Type':'application/json'},
body:body===undefined?undefined:JSON.stringify(body)});
if(!r.ok)throw Error('Request failed ('+r.status+'). Check token and server status.');
return r.json();}
function section(title,wide=false){const s=node('section');if(wide)s.className='wide';
s.append(node('h2',title));$('cards').append(s);return s;}
function table(s,rows){if(!rows.length){s.append(node('p','No activity yet'));return;}
const t=node('table'),h=node('tr');for(const k of Object.keys(rows[0]))h.append(node('th',k));
t.append(h);for(const row of rows){const tr=node('tr');for(const v of Object.values(row))
tr.append(node('td',typeof v==='object'?JSON.stringify(v):v??'—'));t.append(tr);}s.append(t);}
function graph(items,edges){const s=section('Dependency graph',true);
const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');
const ids=[...new Set([...items.map(x=>x.id),...edges.flatMap(x=>[x.parent,x.child])])];
const width=Math.max(700,ids.length*210);svg.setAttribute('viewBox','0 0 '+width+' 160');
function shape(tag,attrs,text){const n=document.createElementNS(ns,tag);
for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text)n.textContent=text;
svg.append(n);return n;}
for(const e of edges){const a=ids.indexOf(e.parent)*210+105,b=ids.indexOf(e.child)*210+105;
shape('path',{d:'M '+a+' 65 Q '+((a+b)/2)+' 0 '+b+' 65',stroke:'#73e0bb',fill:'none'});
shape('text',{x:(a+b)/2,y:26,fill:'#a6c0d1','font-size':11},e.buffer_minutes+' min buffer →');}
ids.forEach((id,i)=>{shape('rect',{x:i*210+15,y:65,width:180,height:60,rx:10,fill:'#304454'});
shape('text',{x:i*210+30,y:100,fill:'#fff','font-size':14},id);});s.append(svg);}
function evidence(entries){const s=section('Evidence and citations');for(const e of entries){
const d=node('details');d.append(node('summary',e.tool+' · '+e.result.status+' · '+e.id));
const url=e.result.source_url;if(url){try{const u=new URL(url);if(u.protocol==='https:'){
const a=node('a','Open source');a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';
d.append(a);}}catch{}}
d.append(node('pre',JSON.stringify(e.result,null,2)));s.append(d);}}
async function ledger(s){try{const data=await api('/dashboard/ledger?offset='+ledgerOffset);
for(const entry of data.entries){const d=node('details');d.append(node('summary',entry.id));
d.append(node('pre',entry.card));s.insertBefore(d,s.lastChild);}
ledgerOffset=data.next_offset;if(!data.entries.length)s.lastChild.disabled=true;
}catch(e){$('status').textContent=e.message;}}
async function refresh(){try{const data=await api('/dashboard/state');
$('mode').textContent=data.mode;demo=data.cached_demo;
$('demo').textContent=demo?'Disable cached demo':'Enable cached demo';$('cards').replaceChildren();
const flow=section('Trip timeline',true),line=node('div');line.className='timeline';
for(const item of [...data.itinerary].sort((a,b)=>a.start.localeCompare(b.start))){
const stop=node('div');stop.className='stop';stop.append(node('strong',item.id),
node('p',new Date(item.start).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'})+' IST'),
node('p',item.status));line.append(stop);}flow.append(line);graph(data.itinerary,data.dependencies);
const roles=section('Agent responsibilities');data.agents.forEach(t=>roles.append(node('p',t)));
for(const [key,title]of [['tasks','Workflow state'],['delivery','Message delivery'],
['verification','Verifier check log']])table(section(title),data[key]);
evidence(data.evidence);const l=section('Approval ledger · all history via pagination');
const more=node('button','Load next 20 proposals');l.append(more);ledgerOffset=0;
more.onclick=()=>ledger(l);await ledger(l);
$('status').textContent='Updated '+new Date().toLocaleTimeString();
}catch(e){$('status').textContent=e.message;}}
$('refresh').onclick=refresh;
$('demo').onclick=async()=>{try{await api('/demo-mode',{enabled:!demo});await refresh();}
catch(e){$('status').textContent=e.message;}};
$('delay').onclick=async()=>{try{const minutes=Number($('delay-minutes').value);
if(!Number.isInteger(minutes)||minutes<1||minutes>360)throw Error('Enter 1–360 minutes.');
$('delay').disabled=true;await api('/disruptions/flight-delay',
{event_id:'dashboard-'+crypto.randomUUID(),minutes});await refresh();}
catch(e){$('status').textContent=e.message;}finally{$('delay').disabled=false;}};
</script></html>"""
