"""Read-only operator dashboard, served behind the existing bearer authentication."""

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trippy · Trip control</title>
<style>
body{font:16px system-ui;background:#101820;color:#e7f0f5;margin:0;padding:32px}
main{max-width:1200px;margin:auto}h1{font-size:36px;margin-bottom:4px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:18px}
section{background:#1a2935;border:1px solid #304454;border-radius:16px;padding:20px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;
padding:9px 5px;border-bottom:1px solid #304454;overflow-wrap:anywhere}
button{padding:10px 18px;border:0;border-radius:8px;background:#73e0bb;cursor:pointer}
#status{color:#a6c0d1}h2{font-size:19px}p{line-height:1.6}
</style><main><h1>Trippy · Trip control</h1>
<p id="mode">Family travel, visible step by step</p>
<input id="token" type="password" placeholder="Private operator token" autocomplete="off">
<button id="refresh">Refresh status</button> <span id="status">Enter token to load</span>
<p>Family → Switch → Coordinator → Planner / tools → proposal → human approval
→ Reservation → simulated result</p><div class="grid" id="cards"></div></main>
<script>
const labels={agents:'Agent responsibilities',itinerary:'Saved demo itinerary',
dependencies:'What depends on what',tasks:'Proposals and execution',
tools:'Tool activity',delivery:'Outgoing delivery'};
async function refresh(){
try{
const r=await fetch('/dashboard/state',{cache:'no-store',headers:{
Authorization:'Bearer '+document.getElementById('token').value}});
if(!r.ok)throw Error('Authentication or server unavailable');
const data=await r.json(); document.getElementById('mode').textContent=data.mode;
const cards=document.getElementById('cards');cards.replaceChildren();
for(const [key,title] of Object.entries(labels)){
const section=document.createElement('section'),h=document.createElement('h2');
h.textContent=title;section.append(h);
const rows=data[key];
if(!rows.length){const p=document.createElement('p');
p.textContent='No activity yet';section.append(p);}
else if(typeof rows[0]==='string'){
for(const text of rows){const p=document.createElement('p');p.textContent=text;section.append(p);}
}else{
const table=document.createElement('table'),header=document.createElement('tr');
for(const key of Object.keys(rows[0])){const th=document.createElement('th');
th.textContent=key.replaceAll('_',' ');header.append(th);}table.append(header);
for(const row of rows){const tr=document.createElement('tr');
for(const value of Object.values(row)){const td=document.createElement('td');
td.textContent=value??'—';tr.append(td);}table.append(tr);}section.append(table);
}cards.append(section);}
document.getElementById('status').textContent='Updated '+new Date().toLocaleTimeString();
}catch(e){document.getElementById('status').textContent=e.message;}}
document.getElementById('refresh').onclick=refresh;
</script></html>"""
