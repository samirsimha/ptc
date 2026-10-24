const $ = (id) => document.getElementById(id);
const escapeHTML = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let data = { incidents: [], fixes: [], notifications: [], graph: {nodes:[],links:[]} };
let selectedIncident = null;
let selectedFix = null;
let network = null;

async function api(path, options={}) {
  const response = await fetch(path, {headers:{'Content-Type':'application/json'}, ...options});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
}
function notice(text, error=false) { $('message').textContent=text; $('message').className=error?'error':''; }
async function refresh() {
  data = await api('/api/state');
  if (!selectedIncident && data.incidents.length) selectedIncident = data.incidents[0].id;
  if (selectedIncident && !data.incidents.some(i=>i.id===selectedIncident)) selectedIncident=data.incidents[0]?.id || null;
  if (selectedFix && !data.fixes.some(f=>f.id===selectedFix)) selectedFix=null;
  render();
}
function render() {
  $('incident-count').textContent=data.incidents.length;
  $('fix-count').textContent=data.fixes.filter(f=>f.status==='proposed').length;
  $('draft-count').textContent=data.notifications.length;
  $('proposal-pill').textContent=`${data.fixes.filter(f=>f.status==='proposed').length} open`;
  const incidents=data.incidents.filter(i=>i.source==='user');
  $('incidents').classList.remove('empty');
  $('incidents').innerHTML=incidents.length?incidents.map(i=>`<div class="incident-item ${i.id===selectedIncident?'active':''}" data-incident="${escapeHTML(i.id)}"><div class="item-top"><strong>${escapeHTML(i.type.replaceAll('_',' '))} · ${escapeHTML(i.status)}</strong><time>${escapeHTML(new Date(i.ts).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}))}</time></div><div class="item-detail">${escapeHTML(i.desc)}</div></div>`).join(''):'No incidents yet. Try a scenario above.';
  const fixes=data.fixes.filter(f=>f.incident_id===selectedIncident);
  $('fixes').classList.remove('empty');
  $('fixes').innerHTML=fixes.length?fixes.map(f=>`<div class="fix-item ${f.id===selectedFix?'active':''}" data-fix="${escapeHTML(f.id)}"><div class="item-top"><span class="tag ${escapeHTML(f.status)}">${escapeHTML(f.status)}</span><time>${escapeHTML(f.agent)}</time></div><div class="fix-title" style="margin-top:7px">${escapeHTML(f.proposal)}</div>${f.rejected_rule?`<div class="fix-meta" style="color:#bc5151">Rule: ${escapeHTML(f.rejected_rule)}</div>`:''}${f.status==='proposed'?`<div class="fix-actions"><button class="approve" data-action="approve" data-id="${escapeHTML(f.id)}">Approve</button><button class="reject" data-action="reject" data-id="${escapeHTML(f.id)}">Reject</button>${f.requires_confirmation?`<button class="confirm" data-action="confirm" data-id="${escapeHTML(f.id)}">Presenter confirm</button>`:''}</div>`:''}</div>`).join(''):'No fixes for this incident.';
  renderExplain(); renderNotifications(); renderGraph();
}
function renderExplain() {
  const fix=data.fixes.find(f=>f.id===selectedFix);
  if(!fix){$('explain').className='explain empty';$('explain').textContent='Select a fix to inspect the graph path and any rejected rule.';return;}
  $('explain').className='explain';
  const recovery=fix.action.type==='overrun_recovery'?`<p>Approval shifts the talks and changes ${fix.action.rsvp_fallbacks.length} conflicting live RSVPs into replay offers. Draft notices explain the change to those attendees.</p>`:'';
  $('explain').innerHTML=`<p>${escapeHTML(fix.proposal)}</p>${recovery}<div class="path">${fix.path.map((step,i)=>`${i?'<b>→</b>':''}<span>${escapeHTML(step)}</span>`).join('')}</div>${fix.rejected_rule?`<div class="rule"><strong>Rule failed:</strong> ${escapeHTML(fix.rejected_rule)}</div>`:''}`;
}
function renderNotifications() {
  const notices=data.notifications.filter(n=>n.incident_id===selectedIncident && (!selectedFix || n.fix_id===selectedFix));
  $('notice-pill').textContent=`${notices.length} drafts`;
  $('notifications').classList.remove('empty');
  $('notifications').innerHTML=notices.length?notices.slice(0,20).map(n=>`<div class="notification"><strong>${escapeHTML(n.person)}</strong><p>${escapeHTML(n.body)}</p></div>`).join('')+(notices.length>20?`<p class="item-detail">+ ${notices.length-20} more affected people</p>`:''):'No drafts for this selection.';
}
function renderGraph() {
  const container=$('graph');
  if(!window.vis?.Network){container.innerHTML='<div class="empty">Graph library is unavailable. Use the FalkorDB browser link below.</div>';return;}
  const allNodes=data.graph.nodes, allLinks=data.graph.links;
  const incident=allNodes.find(n=>n.key===selectedIncident);
  const keep=new Set(incident?[incident.id]:allNodes.filter(n=>n.labels.includes('Room')||n.labels.includes('Session')).map(n=>n.id));
  if(incident){for(let depth=0;depth<2;depth++) for(const e of allLinks) if(keep.has(e.source)||keep.has(e.target)){keep.add(e.source);keep.add(e.target);}}
  const nodes=allNodes.filter(n=>keep.has(n.id));
  const edges=allLinks.filter(e=>keep.has(e.source)&&keep.has(e.target));
  const direct=new Set(allLinks.filter(e=>e.source===incident?.id&&e.type==='AFFECTS').map(e=>e.target));
  const fix=data.fixes.find(f=>f.id===selectedFix);
  const path=new Set((fix?.path||[]).filter(s=>s.includes(':')).map(s=>s.split(':').slice(1).join(':')));
  const graphNodes=new vis.DataSet(nodes.map(n=>{
    const label=n.labels[0];
    const color=label==='Incident'?(n.status==='resolved'?'#7aaea2':n.status==='projected'?'#df8a36':'#d34545'):label==='Fix'?(n.status==='rejected'?'#a6aba9':'#37a56d'):direct.has(n.id)?'#df8a36':label==='Agent'||label==='AgentRun'?'#7359af':label==='Room'?'#487c9e':'#9ab8ae';
    const highlighted=path.has(String(n.key))||n.key===selectedFix;
    const short=label==='Incident'?n.label.slice(0,26):label==='Fix'?n.label.slice(0,27):n.label.slice(0,24);
    return {id:n.id,label:short,title:`${label}: ${n.label}`,shape:'dot',size:label==='Incident'?21:label==='Fix'?16:highlighted?18:label==='Talk'?11:8,
      color:{background:color,border:highlighted?'#122d2a':color,highlight:{background:color,border:'#102f2b'}},borderWidth:highlighted?3:1,font:{face:'Arial',size:label==='Incident'||label==='Fix'?12:10,color:'#344544',strokeWidth:3,strokeColor:'#fff'}};
  }));
  const graphEdges=new vis.DataSet(edges.map((e,i)=>({id:i,from:e.source,to:e.target,arrows:e.type==='CAUSED'?'to':'',color:{color:e.type==='AFFECTS'?'#e4b884':'#dce6e2',highlight:'#0f6c62'},width:e.type==='AFFECTS'?2:1,smooth:{type:'dynamic'}})));
  if(network) network.destroy();
  network=new vis.Network(container,{nodes:graphNodes,edges:graphEdges},{physics:{solver:'forceAtlas2Based',forceAtlas2Based:{gravitationalConstant:-48,centralGravity:.012,springLength:95,springConstant:.04},stabilization:{iterations:120}},interaction:{hover:true,tooltipDelay:100,navigationButtons:true},nodes:{shadow:false},edges:{selectionWidth:2}});
  network.on('click',e=>{if(!e.nodes.length)return;const node=nodes.find(n=>n.id===e.nodes[0]);if(node?.labels.includes('Fix')){selectedFix=node.key;render();}});
}
async function submit(text) {
  $('submit-btn').disabled=true; notice('Tracing incident through the graph…');
  try {const result=await api('/api/incidents',{method:'POST',body:JSON.stringify({text})});selectedIncident=result.incident_id;selectedFix=result.fixes.find(f=>f.status==='proposed')?.id||null;await refresh();notice(`Incident traced. ${result.fixes.length} fix options and ${result.notifications.length} notification drafts.`);}
  catch(err){notice(err.message,true)}finally{$('submit-btn').disabled=false}
}
$('submit-btn').onclick=()=>{const text=$('incident-input').value.trim();if(text)submit(text)};
$('incident-input').onkeydown=e=>{if(e.key==='Enter')$('submit-btn').click()};
document.querySelectorAll('[data-example]').forEach(button=>button.onclick=()=>{$('incident-input').value=button.dataset.example;submit(button.dataset.example)});
document.addEventListener('click',async e=>{
  const incident=e.target.closest('[data-incident]');if(incident){selectedIncident=incident.dataset.incident;selectedFix=null;render();return;}
  const action=e.target.closest('[data-action]');if(action){e.stopPropagation();let body;
    if(action.dataset.action==='confirm'){const fix=data.fixes.find(f=>f.id===action.dataset.id);const tid=fix.action.changes[0].talk_id;const speaker={'talk-15':'speaker-14'}[tid]||prompt('Presenter ID for this talk (for example speaker-14):');if(!speaker)return;body=JSON.stringify({person_id:speaker});}
    try{await api(`/api/fixes/${action.dataset.id}/${action.dataset.action}`,{method:'POST',body});selectedFix=action.dataset.id;await refresh();notice(action.dataset.action==='approve'?'Fix approved and graph updated.':action.dataset.action==='confirm'?'Presenter confirmation recorded.':'Fix rejected.');}catch(err){notice(err.message,true)}return;
  }
  const fix=e.target.closest('[data-fix]');if(fix){selectedFix=fix.dataset.fix;render();}
});
$('reset-btn').onclick=async()=>{if(!confirm('Reset all demo incidents and restore the deterministic seed?'))return;try{await api('/api/reset',{method:'POST'});selectedIncident=null;selectedFix=null;await refresh();notice('Demo reset to seed data.')}catch(err){notice(err.message,true)}};
$('preflight-btn').onclick=async()=>{try{const result=await api('/api/preflight');$('preflight').hidden=false;const sections=[['Audience overlap',result.audience_cannibalization,r=>`${r.a} overlaps ${r.b} (${r.topic})`],['Chair conflicts',result.chair_conflicts,r=>`${r.person} chairs and presents in ${r.session}`],['Walk time',result.walk_time,r=>`${r.person}: ${r.gap} min gap, ${r.required} min walk (${r.step_free} min step-free)`],['Q&A patterns',result.qa_hogs,r=>`${r.person}: ${r.questions} questions, ${r.avg_minutes} min average`]];$('preflight-content').innerHTML=sections.map(([title,items,format])=>`<div><h3>${escapeHTML(title)} <span class="pill">${items.length}</span></h3>${items.length?items.slice(0,8).map(x=>`<p>${escapeHTML(format(x))}</p>`).join(''):'<p>No issues found.</p>'}</div>`).join('');$('preflight').scrollIntoView({behavior:'smooth'});}catch(err){notice(err.message,true)}};
$('close-preflight').onclick=()=>$('preflight').hidden=true;
api('/api/health').then(()=>{$('db-status').classList.add('ok');$('db-status').innerHTML='<i></i> FalkorDB live';refresh()}).catch(err=>notice(err.message,true));
