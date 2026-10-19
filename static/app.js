// All API data is rendered via textContent only (no HTML string injection) to prevent XSS.
const P=[{music:"Radiohead, Bon Iver",films:"Amelie, Lost in Translation",food:"Blue Bottle Coffee",lifestyle:"art, cafes, walkable"},
{music:"Daft Punk, Bad Bunny",tv:"The Bear",food:"Nobu",lifestyle:"nightlife, beaches"},
{music:"Coldplay, Taylor Swift",films:"Up, Paddington",lifestyle:"hiking, kids, nature",country:"US"}];
const f=document.getElementById('f'),out=document.getElementById('out');
function el(tag,text,cls){const e=document.createElement(tag);if(text!=null)e.textContent=String(text);if(cls)e.className=cls;return e}
function chips(list,prefix=""){const p=el('p');for(const t of list||[])p.append(el('span',prefix+t,'chip'));return p}
function msg(t){out.replaceChildren(el('p',t))}
document.querySelectorAll('[data-preset]').forEach(b=>b.addEventListener('click',()=>{const v=P[+b.dataset.preset];for(const x of f.elements)if(x.name)x.value=v[x.name]||""}));
let busy=false;
f.addEventListener('submit',async e=>{e.preventDefault();if(busy)return;busy=true;
 msg("🔎 Agent is resolving your tastes and querying Qloo… (~15s)");
 try{const res=await fetch('/api/scout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(f)))});
  const r=await res.json();
  if(!res.ok||r.error){msg(r.error||"Something went wrong.");return}
  const frag=document.createDocumentFragment();
  const m=chips((r.matched||[]).map(x=>x.name));m.prepend("Matched in Qloo: ");
  for(const t of r.lifestyle_tags||[])m.append(el('span','#'+t.name,'chip'));frag.append(m);
  if(r.narrative)frag.append(el('div',r.narrative,'card'));
  for(const c of r.cities||[]){const d=el('div',null,'card');
   d.append(el('span',Math.round((+c.affinity||0)*100),'score'),el('h2',c.location),el('p',c.why));
   for(const n of c.neighborhoods||[]){const p=el('p');p.append(el('b',n.name),': '+(n.spots||[]).join(' · '));d.append(p)}
   d.append(chips(c.vibe));frag.append(d)}
  const det=el('details');det.append(el('summary','Agent trace (Qloo calls)'),el('pre',(r.trace||[]).join('\n')));frag.append(det);
  out.replaceChildren(frag);
 }catch(err){msg("Something went wrong. Try again.")}finally{busy=false}});
