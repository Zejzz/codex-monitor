'use strict';
let data, modelData, tab = 'chats', limit = 100, selectedId = null, selectedModel = null, busy = false, pendingRefresh = false;
let chartPoints=[],chartIndex=-1,chartSelectionKey='',resizeTimer;
const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = v => new Intl.NumberFormat(undefined).format(v || 0);
const compact = v => new Intl.NumberFormat(undefined, {notation:'compact',maximumFractionDigits:2}).format(v || 0);
const money = v => '$' + (v || 0).toLocaleString(undefined, {minimumFractionDigits:2,maximumFractionDigits:2});
const date = t => t ? new Date(t * 1000).toLocaleString(undefined, {dateStyle:'medium',timeStyle:'short'}) : 'Unavailable';
function duration(s) {s=Math.floor(s||0);return s>=3600?`${Math.floor(s/3600)}h ${Math.floor(s%3600/60)}m`:s>=60?`${Math.floor(s/60)}m ${s%60}s`:`${s}s`;}
function status(t) {return t.active?'Running':({completed:'Completed',inProgress:'Running',interrupted:'Interrupted',failed:'Failed',unknown:'No turn record',unconfirmed:'Unconfirmed'}[t.status]||t.status);}
function badge(t) {return `<span class="badge ${t.active?'running':t.status==='unconfirmed'?'unconfirmed':''}">${esc(status(t))}</span>`;}
function cost(t, family=true) {const value=family?t.family_cost:t.cost,missing=family?t.family_unpriced_tokens:t.unpriced_tokens;return `<span class="number money">${money(value)}${missing?' + ?':''}</span>${missing?`<span class="secondary partial">${compact(missing)} unpriced tokens</span>`:''}`;}
function labelTable(table){
 if(!table)return;
 const labels=[...table.querySelectorAll('thead th')].map(th=>th.textContent.replace('ⓘ','').trim());
 table.classList.add('mobile-table');
 for(const row of table.querySelectorAll('tbody tr'))for(const [i,cell] of [...row.cells].entries())if(cell.colSpan===1)cell.dataset.label=labels[i]||'';
}
async function refresh() {
 if(busy){pendingRefresh=true;return;}busy=true;
 try {const query=modelQuery();const response=await fetch('/api/data'+query,{signal:AbortSignal.timeout(20000)});if(!response.ok)throw Error((await response.json()).error);data=await response.json();modelData={models:data.models};render();$('connection').textContent='Live · '+new Date().toLocaleTimeString(undefined);}
 catch(e){$('connection').textContent=e.message==='Invalid date range'?'Check dates':'Disconnected';$('notice').hidden=false;$('notice').textContent=e.message==='Invalid date range'?'Choose a start date on or before the end date.':'Cannot refresh the monitor. Reopen it with Open Codex Monitor.cmd if the server has stopped. '+e.message;}
 finally {busy=false;if(pendingRefresh){pendingRefresh=false;refresh();}}
}
function render() {
 const device=$('device').value;$('device').innerHTML='<option value="">All devices</option>'+(data.devices||[]).map(d=>`<option value="${esc(d.id)}">${esc(d.name)}${d.id!=='local'?(data.now-d.seen>90?' · offline':' · syncing'):''}</option>`).join('');$('device').value=device;
 const ranged=$('period').value!=='all'; const chats=data.threads.filter(t=>!t.agent&&(!ranged||t.family_period_activity)), agents=data.threads.filter(t=>t.agent&&(!ranged||t.family_period_activity)); const periodLabel=$('period').selectedOptions[0].textContent;
 $('period-note').textContent=`${periodLabel} · Tokens, API estimates, graphs and table usage follow this period and device. Running status and weekly meter remain current. ${compact(data.unknown_tier_tokens)} tokens have no selected/requested speed-tier evidence. ${data.pricing==='fast'?'Fast scenario applied to those tokens where rates exist.':'Standard rates assumed for those tokens.'}`;
 $('chat-count').textContent=chats.length;$('agent-count').textContent=agents.length;
 const weekly=data.weekly;
 const old=data.limits.find(w=>w.id==='codex'&&w.window_minutes===10080);
 $('stats').innerHTML=`<article class="stat"><div class="stat-label">RUNNING NOW</div><div class="stat-value">${data.active} <small>agents & chats</small></div><div class="stat-note">${data.threads.filter(t=>!t.agent&&t.family_active).length} chats with active work</div></article>
 <article class="stat"><div class="stat-label">RECORDED TOKENS · ${esc(periodLabel).toUpperCase()}</div><div class="stat-value" title="${num(data.total_tokens)}">${compact(data.total_tokens)}</div><div class="stat-note">${num(chats.length)} chats · ${num(agents.length)} agents<br>To date: ${compact(data.all_time.tokens)} tokens</div></article>
 <article class="stat"><div class="stat-label">API EQUIVALENT · ${esc(periodLabel).toUpperCase()}</div><div class="stat-value">${money(data.total_cost)}${data.unpriced_tokens?' <small>+ unpriced</small>':''}</div><div class="stat-note">${data.unpriced_tokens?compact(data.unpriced_tokens)+' tokens excluded from cost':'Includes cache discounts'}<br>Requested-tier estimate<br>To date: ${money(data.all_time.cost)} known USD</div></article>
 <article class="stat"><div class="stat-label">LATEST WEEKLY SNAPSHOT</div><div class="stat-value">${weekly?weekly.used_percent.toFixed(1)+'%':old?'Stale':'—'} <small>${weekly?'used':''}</small></div>${weekly?`<progress class="meter" max="100" value="${Math.min(100,weekly.used_percent)}" aria-label="Weekly account usage"></progress><div class="stat-note">Resets ${esc(date(weekly.resets_at))}<br>Observed ${esc(date(weekly.time))}</div>`:`<div class="stat-note">${old?'Last snapshot '+esc(date(old.time)):'Waiting for a recorded limit snapshot'}</div>`}</article>`;
 $('notice').hidden=!data.indexing&&!data.errors.length;
 $('notice').textContent=data.indexing?(data.cached_threads?`Saved history loaded · checking for new usage… ${data.progress}%. ${num(data.cached_threads)} local chats and agents restored from disk. Unchanged logs are skipped.`:`Indexing new local history… ${data.progress}%. Usage is saved to disk for later starts.`):data.errors.length?`${data.errors.length} records could not be read. ${data.errors.slice(0,2).join(' · ')}`:'';
 $('source').textContent=`Source: ${data.home} · ${data.updated?'Updated '+date(data.updated):'Initial index in progress'}`;
 $('remote-status').textContent=data.receiver_status||'';
 const counts=data.speed_counts||{};
 $('speed-coverage').innerHTML='<table class="detail-table"><thead><tr><th>TIER EVIDENCE</th><th>USAGE RECORDS</th></tr></thead><tbody>'+[['standard','Standard / default'],['fast','Fast / priority'],['other','Other value'],['absent','Field absent'],['null','Explicit null'],['empty','Empty string']].map(([key,label])=>`<tr><td>${label}</td><td>${num(counts[key])}</td></tr>`).join('')+`</tbody></table><p>${num(data.speed_sources?.diagnostic_selected)} usage records recovered from turn-linked diagnostic settings. Pricing uses the selected/requested tier. No fallback discount is inferred from delivery speed. Direct session settings take precedence. Missing fields remain assumptions.</p><p>${num(data.cache_breaks)} model/effort switch boundaries, including ${num(data.cache_parent_breaks)} inherited subagent boundaries. ${data.cache_policy==='switches'?compact(data.cache_adjusted_tokens)+' reported cached tokens priced as ordinary input on the first request after these switches.':'Pricing uses reported cache hits.'} Later warmed requests retain their cache discounts.</p><p>Model changes break cache. Effort changes break cache through GPT-5.6. GPT-6 onward preserves cache across effort changes under the requested rule; this requires the supported configuration-update mechanism, which these logs do not verify. Copied history is excluded to avoid counting past requests twice. Inherited context in new subagent input is counted.</p>`;
 renderGraphs();renderUnpriced();renderRows();
 if($('detail').open){if(selectedModel)renderModelDetail(selectedModel);else if(selectedId)renderDetail(selectedId);}
}
function filtered() {
 let items=data.threads.filter(t=>tab==='agents'?t.agent:tab==='active'?!t.agent&&t.family_active:tab==='archived'?!t.agent&&t.archived:!t.agent);
 if($('period').value!=='all')items=items.filter(t=>t.family_period_activity);
 const q=$('search').value.trim().toLowerCase();if(q)items=items.filter(t=>[t.title,t.cwd,t.model,t.effort,t.role,t.nickname,t.agent_path,t.device_name,...t.groups.map(g=>g.model)].join(' ').toLowerCase().includes(q));
 const sort=$('sort').value;items.sort((a,b)=>Number(b.family_active)-Number(a.family_active)||(sort==='tokens'?b.family_tokens-a.family_tokens:sort==='cost'?b.family_cost-a.family_cost:($('period').value==='all'?b.updated-a.updated:b.family_usage_last-a.family_usage_last)));return items;
}
function renderRows() {
 const models=tab==='models';$('chat-table').hidden=models;$('model-table').hidden=!models;
 if(models){renderModels();return;}
 const items=filtered();$('rows').innerHTML=items.slice(0,limit).map(t=>`<tr data-id="${esc(t.id)}" tabindex="0" aria-label="Inspect ${esc(t.title)}"><td><span class="chat-title" title="${esc(t.title)}">${esc(t.nickname&&t.agent?t.nickname+' · '+t.title:t.title)}</span><div class="chat-meta">${badge(t)}<span class="badge" title="${t.sync_time?'Last synced '+esc(date(t.sync_time)):'Local records'}">${esc(t.device_name||'Local')}${t.sync_stale?' · offline':''}</span>${t.agent_count?`<span class="badge">${t.agent_count} subagents</span>`:''}${t.archived?'<span class="badge">Archived</span>':''}<span class="path" title="${esc(t.cwd)}">${esc(t.cwd)}</span></div></td><td><span class="model">${esc(t.model||'Unknown')}</span><span class="secondary">${esc(t.effort||'unknown')} effort${t.groups.length>1?' · '+t.groups.length+' usage groups':''}</span></td><td><span class="number" title="${num(t.family_tokens)}">${compact(t.family_tokens)}</span><span class="secondary">${t.agent_count?'Includes subagents':t.turns+' turns'}</span></td><td>${cost(t)}</td><td><span class="number">${t.active?duration(t.runtime):t.family_active?'Agents running':duration(t.duration)}</span><span class="secondary">${t.active?'Current turn':t.family_active?'Open for agent timing':($('period').value==='all'?'Total turn time':'Turn time in period')}</span></td><td><span class="number">${t.weekly_estimate===null?'—':'≈ '+t.weekly_estimate.toFixed(2)+'%'}</span><span class="secondary">${t.weekly_estimate===null?'No current snapshot':'Token share estimate'}</span></td></tr>`).join('')||`<tr><td colspan="6" class="empty">${data.indexing?'Indexing your chat history…':'No chats match this view.'}</td></tr>`;
 labelTable($('chat-table'));
 $('result-count').textContent=`Showing ${Math.min(limit,items.length)} of ${items.length} ${tab==='agents'?'agents':'chats'}`;$('more').hidden=items.length<=limit;
}
function openDetail(id) {selectedModel=null;selectedId=id;renderDetail(id);if(!$('detail').open)$('detail').showModal();}

function modelQuery() {
 const period=$('period').value,now=Date.now()/1000;let start=0,end=now+1;
 if(period==='custom'){start=$('from-date').value?Date.parse($('from-date').value+'T00:00:00Z')/1000:0;end=$('to-date').value?Date.parse($('to-date').value+'T00:00:00Z')/1000+86400:now+1;}
 else if(['today','wtd','mtd'].includes(period)){const d=new Date();start=Date.UTC(d.getUTCFullYear(),d.getUTCMonth(),period==='mtd'?1:d.getUTCDate())/1000;if(period==='wtd')start-=((d.getUTCDay()+6)%7)*86400;}
 else if(period!=='all')start=now-({day:86400,week:604800,month:2592000}[period]);
 if(!(start>=0&&start<end))throw Error('Invalid date range');return '?start='+start+'&end='+end+'&live='+(period==='custom'?'0':'1')+'&pricing='+$('pricing-mode').value+'&cache='+$('cache-policy').value+($('device').value?'&device='+encodeURIComponent($('device').value):'');
}
function renderModels() {
 if(!modelData){$('model-rows').innerHTML='<tr><td colspan="7" class="empty">Loading model usage…</td></tr>';$('more').hidden=true;return;}
 const q=$('search').value.trim().toLowerCase();let models=modelData.models.filter(m=>[m.model,...m.efforts.map(e=>e.effort)].join(' ').toLowerCase().includes(q));const sort=$('sort').value;
 models.sort((a,b)=>sort==='recent'?b.last-a.last:sort==='cost'?b.cost-a.cost:b.tokens-a.tokens);
 $('model-rows').innerHTML=models.map(m=>`<tr data-model="${esc(m.model)}" tabindex="0" aria-label="Inspect model ${esc(m.model)}"><td><span class="model">${esc(m.model)}</span><span class="secondary">${m.efforts.map(e=>esc(e.effort)).join(' · ')}<br>${m.threads.length} chats / agents · ${num(m.requests)} records</span></td><td class="number">${compact(m.input)}${m.write?`<span class="secondary">${compact(m.write)} cache writes</span>`:''}</td><td class="number">${compact(m.cached)}</td><td class="number">${compact(m.output)}<span class="secondary">${compact(m.reasoning)} reasoning included</span></td><td class="number" title="${num(m.tokens)}">${compact(m.tokens)}</td><td>${cost(m,false)}</td><td><span class="secondary">${esc(date(m.last))}</span></td></tr>`).join('')||'<tr><td colspan="7" class="empty">No model usage in this period matches your search.</td></tr>';

 labelTable($('model-table'));
 $('result-count').textContent=`${models.length} models · owned usage across chats and subagents`;$('more').hidden=true;
}
function openModel(model) {selectedId=null;selectedModel=model;renderModelDetail(model);if(!$('detail').open)$('detail').showModal();}
function renderModelDetail(model) {
 const m=modelData?.models.find(m=>m.model===model);$('detail-title').textContent=model+' · model usage';
 if(!m){$('detail-body').innerHTML='<p>No usage in the selected period.</p>';return;}
 $('detail-body').innerHTML=`${model==='gpt-daybreak-blue-latest'?'<p class="muted">Pricing fallback: GPT-5.6 Sol before 22 Sep 2026 UTC; GPT-6 Sol afterward. Actual historical alias resolution is unavailable.</p>':''}${model==='codex-auto-review'?'<p class="muted">Luna pricing proxy selected by you: GPT-5.6 Luna before 22 Sep 2026 UTC; GPT-6 Luna afterward. Actual Auto-review model and billing rate are unverified. Requested speed tier and cache pricing policy apply.</p>':''}${model==='gpt-reserve'?'<p class="muted">User-supplied Luna mapping: GPT-5.6 Luna before 22 Sep 2026 UTC; GPT-6 Luna afterward. Per-request dates, requested tiers and generation-specific cache rules apply. Later Luna releases require a dated pricing-history entry.</p>':''}<p class="muted">Owned usage in the selected period. Each model receives only the tokens recorded while it was used, including changes within a chat.</p><div class="detail-stats"><div class="mini"><span class="muted">Tokens</span><strong>${compact(m.tokens)}</strong></div><div class="mini"><span class="muted">API equivalent</span><strong>${money(m.cost)}${m.unpriced_tokens?' + ?':''}</strong></div><div class="mini"><span class="muted">Chats / agents</span><strong>${m.threads.length}</strong></div><div class="mini"><span class="muted">Usage records</span><strong>${num(m.requests)}</strong></div></div><h3>Reasoning effort breakdown</h3><div class="table-scroll"><table class="detail-table"><thead><tr><th>EFFORT</th><th>TOKENS</th><th>USAGE RECORDS</th><th>API USD</th></tr></thead><tbody>${m.efforts.map(e=>`<tr><td>${esc(e.effort)}</td><td>${num(e.tokens)}</td><td>${num(e.requests)}</td><td>${cost(e,false)}</td></tr>`).join('')}</tbody></table></div><h3>Contributing chats and agents · own usage for this model</h3><div class="table-scroll"><table class="detail-table"><thead><tr><th>CHAT / AGENT</th><th>TOKENS</th><th>API USD</th><th>LAST USED</th></tr></thead><tbody>${m.threads.map(t=>`<tr><td><button class="agent-open" data-agent="${esc(t.id)}">${esc(t.title.slice(0,140))}</button></td><td>${num(t.tokens)}</td><td>${cost(t,false)}</td><td>${esc(date(t.last))}</td></tr>`).join('')}</tbody></table></div>`;
 for(const table of $('detail-body').querySelectorAll('.detail-table'))labelTable(table);
}
function renderDetail(id) {
 const t=data.threads.find(t=>t.id===id);if(!t)return;$('detail-title').textContent=t.title;
 const byId=new Map(data.threads.map(t=>[t.id,t]));const descendants=[];
 function visit(node,depth,seen) {if(seen.has(node.id))return;seen.add(node.id);for(const id of node.children){const child=byId.get(id);if(child){descendants.push([child,depth]);visit(child,depth+1,seen);}}}visit(t,0,new Set());
 $('detail-body').innerHTML=`<div class="inspector-meta">${badge(t)} · ${esc(t.cwd)}<br>${esc(t.id)} · Created ${esc(date(t.created))}${t.parent?` · <button class="agent-open" data-agent="${esc(t.parent)}">Open parent</button>`:''}</div>
 <div class="detail-stats"><div class="mini"><span class="muted">Own tokens</span><strong>${compact(t.tokens)}</strong></div><div class="mini"><span class="muted">With descendants</span><strong>${compact(t.family_tokens)}</strong></div><div class="mini"><span class="muted">Own API estimate</span><strong>${money(t.cost)}${t.unpriced_tokens?' + ?':''}</strong></div><div class="mini"><span class="muted">${t.active?'Current turn':'Accumulated turn time'}</span><strong>${duration(t.active?t.runtime:t.duration)}</strong></div></div>
 <h3>Usage by model and effort · own requests</h3><div class="table-scroll"><table class="detail-table"><thead><tr><th>MODEL / EFFORT</th><th>INPUT</th><th>CACHED</th><th>OUTPUT</th><th>TOTAL</th><th>API USD</th></tr></thead><tbody>${t.groups.map(g=>`<tr><td>${esc(g.model)}<span class="secondary">${esc(g.effort)} · ${esc(g.tier||'unknown')} speed · ${num(g.requests)} usage records${g.long_requests?' · '+g.long_requests+' long-context':''}</span></td><td>${num(g.input)}${g.write?`<span class="secondary">${num(g.write)} cache write</span>`:''}</td><td>${num(g.cached)}</td><td>${num(g.output)}<span class="secondary">${num(g.reasoning)} reasoning included</span></td><td>${num(g.tokens)}</td><td>${money(g.cost)}${g.unpriced_tokens?' + unpriced':''}</td></tr>`).join('')||'<tr><td colspan="6">No owned usage records found.</td></tr>'}</tbody></table></div>
 <h3>Model and effort history</h3><div class="timeline">${t.contexts.map(c=>`<span>${esc(c.model)} · ${esc(c.effort)} · ${esc(c.tier||'unknown')} speed<time>${esc(date(c.time))}</time></span>`).join('')||'<span>No model history recorded.</span>'}</div>
 <h3>Historical subagents · ${descendants.length}</h3>${descendants.length?`<div class="table-scroll"><table class="detail-table"><thead><tr><th>AGENT</th><th>MODEL / EFFORT</th><th>OWN TOKENS</th><th>OWN API USD</th><th>TURN TIME</th></tr></thead><tbody>${descendants.map(([a,depth])=>`<tr><td><button class="agent-open" data-agent="${esc(a.id)}">${'↳ '.repeat(Math.min(depth+1,5))}${esc(a.nickname||a.agent_path||a.title)}</button><span class="secondary">${esc(a.role||'subagent')} · ${esc(status(a))}</span></td><td>${esc(a.model)}<span class="secondary">${esc(a.effort)}</span></td><td>${compact(a.tokens)}</td><td>${cost(a,false)}</td><td>${duration(a.active?a.runtime:a.duration)}<span class="secondary">${a.active?'Current turn':'Accumulated'}</span></td></tr>`).join('')}</tbody></table></div>`:'<p class="muted">No recorded subagents for this chat.</p>'}
 <p class="muted">Database token counter: ${num(t.database_tokens)}. Owned usage: ${num(t.tokens)}. Counters may differ because of inherited history, migrations, missing records, or in-flight usage.${!t.indexed?' Session log unavailable.':''}${t.malformed?' '+t.malformed+' malformed records skipped.':''}</p>`;
 for(const table of $('detail-body').querySelectorAll('.detail-table'))labelTable(table);
}
function openRates() {
 const models=[...new Set([...Object.keys(data.prices),...data.threads.flatMap(t=>t.groups.map(g=>g.model))])].sort();
 $('rate-rows').innerHTML=models.map(model=>{const r=data.prices[model];return `<tr data-model="${esc(model)}"><td>${esc(model)}<span class="secondary">${r?esc(r.note|| (r.source==='User override'?'Custom rate':r.verified)):'Unpriced'}</span></td>${['input','cached','write','output','long_threshold'].map(key=>`<td><input type="number" min="0" step="any" data-key="${key}" aria-label="${esc(model+' '+key)}" value="${r?r[key]??0:''}" placeholder="—"></td>`).join('')}</tr>`;}).join('');$('rate-message').textContent='';$('rates').showModal();
}
$('save-rates').onclick=async()=>{
 const rates={};for(const row of $('rate-rows').children){const inputs=[...row.querySelectorAll('input')];if(inputs.every(i=>i.value===''))continue;if(inputs.some(i=>i.value===''||!Number.isFinite(Number(i.value))||Number(i.value)<0)){$('rate-message').textContent='Fill all five values, or leave the whole model blank.';return;}const value=Object.fromEntries(inputs.map(i=>[i.dataset.key,Number(i.value)]));const old=data.prices[row.dataset.model];if(!old||old.source==='User override'||inputs.some(i=>Number(i.value)!==Number(old[i.dataset.key]||0)))rates[row.dataset.model]=value;}
 try {const response=await fetch('/api/prices',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(rates)});const result=await response.json();if(!response.ok)throw Error(result.error);$('rates').close();await refresh();}catch(e){$('rate-message').textContent=e.message;}
};
$('rows').addEventListener('click',e=>{const row=e.target.closest('tr[data-id]');if(row)openDetail(row.dataset.id);});
$('rows').addEventListener('keydown',e=>{if(e.key==='Enter'){const row=e.target.closest('tr[data-id]');if(row)openDetail(row.dataset.id);}});
$('detail-body').addEventListener('click',e=>{const button=e.target.closest('[data-agent]');if(button)openDetail(button.dataset.agent);});
document.querySelectorAll('[data-tab]').forEach(b=>{b.setAttribute('aria-pressed',b.classList.contains('selected'));b.onclick=()=>{tab=b.dataset.tab;limit=100;document.querySelectorAll('[data-tab]').forEach(x=>{x.classList.toggle('selected',x===b);x.setAttribute('aria-pressed',x===b);});if(data)renderRows();if(tab==='models')refresh();};});
$('model-rows').addEventListener('click',e=>{const row=e.target.closest('tr[data-model]');if(row)openModel(row.dataset.model);});
$('model-rows').addEventListener('keydown',e=>{if(e.key==='Enter'){const row=e.target.closest('tr[data-model]');if(row)openModel(row.dataset.model);}});
$('period').onchange=()=>{$('custom-range').hidden=$('period').value!=='custom';limit=100;modelData=null;refresh();};
$('pricing-mode').onchange=refresh;$('cache-policy').onchange=refresh;
$('chart-model').onchange=renderGraphs;$('chart-view').onchange=renderGraphs;$('chart-interval').onchange=renderGraphs;
$('device').onchange=()=>{modelData=null;refresh();};
for(const id of ['from-date','to-date'])$(id).onchange=()=>{modelData=null;refresh();};
$('search').oninput=()=>{limit=100;if(data)renderRows();};$('sort').onchange=()=>{if(data)renderRows();};$('more').onclick=()=>{limit+=100;renderRows();};
$('close-detail').onclick=()=>$('detail').close();$('close-rates').onclick=()=>$('rates').close();$('pricing').onclick=()=>{if(data)openRates();};$('refresh').onclick=refresh;
function renderUnpriced(){
 const missing=(modelData?.models||[]).filter(m=>m.unpriced_tokens);
 $('unpriced-summary').textContent=missing.length?`${compact(data.unpriced_tokens)} unpriced tokens · show models`:'All usage in this period has a rate';
 $('unpriced-list').innerHTML=missing.map(m=>`<p><b>${esc(m.model)}</b>: ${num(m.unpriced_tokens)} tokens excluded from cost</p>`).join('')+'<p>Open API rates to add a standard price. Missing model or speed-tier prices are excluded, never guessed as zero. Daybreak Blue uses the historical Sol fallback specified in API rates.</p>';
}
function chartSVG(points,key,label,format){
 if(!points.length)return '<p class="muted">No recorded usage in this period.</p>';
 const max=Math.max(...points.map(p=>p[key]),1),W=Math.max(260,Math.min(640,$('token-chart').clientWidth||640)),H=220,left=58,right=15,top=15,bottom=38;
 const x=i=>left+(points.length===1?.5:i/(points.length-1))*(W-left-right),y=v=>H-bottom-v/max*(H-top-bottom);
 const line=points.map((p,i)=>`${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(' ');
 const ticks=[0,.5,1].map(f=>`<line x1="${left}" y1="${y(max*f)}" x2="${W-right}" y2="${y(max*f)}" stroke="#283140"/><text x="${left-6}" y="${y(max*f)+4}" text-anchor="end" fill="#8d9baf" font-size="11">${esc(format(max*f))}</text>`).join('');
 const stamp=t=>new Date(t*1000).toISOString().slice(0,10);
 const color=key==='tokens'?'#8ab5ff':'#8de4b8';
 return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}"><polygon points="${x(0)},${H-bottom} ${line} ${x(points.length-1)},${H-bottom}" fill="${color}" opacity=".08"/>${ticks}<polyline points="${line}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linejoin="round"/>${points.map((p,i)=>`<circle data-point-index="${i}" cx="${x(i)}" cy="${y(p[key])}" r="${points.length>30?2:3.5}" fill="${color}"><title>${p.label||stamp(p.time)}: ${format(p[key])}</title></circle>`).join('')}<text x="${left}" y="${H-10}" fill="#8d9baf" font-size="11">${stamp(points[0].time)}</text><text x="${W-right}" y="${H-10}" text-anchor="end" fill="#8d9baf" font-size="11">${stamp(points.at(-1).time)}</text></svg>`;
}
function selectChartPoint(index){
 chartIndex=Math.max(0,Math.min(chartPoints.length-1,index));const point=chartPoints[chartIndex];
 $('chart-readout').hidden=!point;if(!point)return;
 $('chart-point-date').textContent=(point.label||new Date(point.time*1000).toISOString().slice(0,10))+($('chart-view').value==='cumulative'?' · cumulative':'');
 $('chart-point-value').textContent=num(point.tokens)+' tokens · '+money(point.cost)+' known USD';
 $('chart-prev').disabled=chartIndex===0;$('chart-next').disabled=chartIndex===chartPoints.length-1;
 for(const circle of document.querySelectorAll('.charts circle'))circle.classList.toggle('selected-point',Number(circle.dataset.pointIndex)===chartIndex);
}
$('chart-prev').onclick=()=>selectChartPoint(chartIndex-1);$('chart-next').onclick=()=>selectChartPoint(chartIndex+1);
for(const id of ['token-chart','cost-chart'])$(id).addEventListener('click',event=>{
 const svg=event.target.closest('svg');if(!svg||!chartPoints.length)return;
 const bounds=svg.getBoundingClientRect(),width=svg.viewBox.baseVal.width,x=(event.clientX-bounds.left)*width/bounds.width;
 selectChartPoint(Math.round((x-58)/(width-58-15)*(chartPoints.length-1)));
});
window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(data)renderGraphs();},150);});
function weeklyPoints(points){
 const weeks=new Map();
 for(const p of points){
  const day=Math.floor(p.time/86400)*86400,time=day-((new Date(day*1000).getUTCDay()+6)%7)*86400;
  if(!weeks.has(time))weeks.set(time,{time,tokens:0,cost:0,unpriced_tokens:0,label:'Week of '+new Date(time*1000).toISOString().slice(0,10)});
  const week=weeks.get(time);for(const key of ['tokens','cost','unpriced_tokens'])week[key]+=p[key]||0;
 }
 return [...weeks.values()].sort((a,b)=>a.time-b.time);
}
function monthlyPoints(points){
 const months=new Map();
 for(const p of points){
  const date=new Date(p.time*1000),time=Date.UTC(date.getUTCFullYear(),date.getUTCMonth(),1)/1000;
  if(!months.has(time))months.set(time,{time,tokens:0,cost:0,unpriced_tokens:0,label:new Date(time*1000).toISOString().slice(0,7)});
  const month=months.get(time);for(const key of ['tokens','cost','unpriced_tokens'])month[key]+=p[key]||0;
 }
 return [...months.values()].sort((a,b)=>a.time-b.time);
}
function renderGraphs(){
 const selection=$('chart-model').value,models=modelData?.models||[];
 $('chart-model').innerHTML='<option value="">All models</option>'+models.map(m=>`<option value="${esc(m.model)}">${esc(m.model)}</option>`).join('');$('chart-model').value=models.some(m=>m.model===selection)?selection:'';
 const chosen=$('chart-model').value;
 const series=data.daily.map(p=>({...p,tokens:chosen?(p.models[chosen]||0):p.tokens,cost:chosen?(p.model_costs?.[chosen]||0):p.cost,unpriced_tokens:chosen?(p.model_unpriced?.[chosen]||0):p.unpriced_tokens}));
 const daily=new Map(series.map(p=>[p.time,p]));let points=[];
 if(series.length){const first=data.start?Math.floor(data.start/86400)*86400:series[0].time,last=$('period').value==='all'?series.at(-1).time:Math.floor((data.end-1)/86400)*86400;
 for(let t=first;t<=last&&points.length<3660;t+=86400)points.push(daily.get(t)||{time:t,tokens:0,cost:0,unpriced_tokens:0});}
 const weekly=$('chart-interval').value==='week',monthly=$('chart-interval').value==='month',interval=monthly?'Monthly':weekly?'Weekly':'Daily';
 if(weekly)points=weeklyPoints(points);
 if(monthly)points=monthlyPoints(points);
 $('chart-view').options[0].textContent=monthly?'Per month':weekly?'Per week':'Per day';
 $('values-summary').textContent=interval+' values';$('values-date-label').textContent=monthly?'MONTH (UTC)':weekly?'WEEK START (UTC)':'UTC DATE';
 $('daily-values').innerHTML=points.map(p=>`<tr><td>${p.label||new Date(p.time*1000).toISOString().slice(0,10)}</td><td>${num(p.tokens)}</td><td>${money(p.cost)}</td><td>${num(p.unpriced_tokens)}</td></tr>`).join('')||'<tr><td colspan="4">No recorded usage.</td></tr>';
 labelTable($('daily-values').closest('table'));
 const cumulative=$('chart-view').value==='cumulative';let tokens=0,cost=0;
 if(cumulative)points=points.map(p=>({...p,tokens:tokens+=p.tokens,cost:cost+=p.cost}));
 const selectionKey=[chosen,weekly,monthly,cumulative,$('period').value,$('from-date').value,$('to-date').value,$('device').value].join('|'),previousTime=chartPoints[chartIndex]?.time,followLatest=chartIndex===chartPoints.length-1;
 chartPoints=points;
 chartIndex=selectionKey===chartSelectionKey&&!followLatest?points.findIndex(p=>p.time===previousTime):points.length-1;chartSelectionKey=selectionKey;
 $('token-chart').innerHTML=chartSVG(points,'tokens',(cumulative?'Cumulative '+interval.toLowerCase():interval)+' tokens',compact);
 $('cost-chart').innerHTML=chartSVG(points,'cost',(cumulative?'Cumulative '+interval.toLowerCase():interval)+' known API equivalent',money);
 selectChartPoint(chartIndex<0?points.length-1:chartIndex);
 $('chart-note').textContent=`${cumulative?'Cumulative in the selected period, plotted '+interval.toLowerCase():interval+' totals in UTC'}${monthly?' · Calendar months; first and last months include only usage inside the selected period':weekly?' · Weeks start Monday; first and last weeks include only usage inside the selected period':''} · ${chosen||'All models'} · Cost excludes unpriced tokens. Tap a chart or use the arrows for exact values, or expand ${interval.toLowerCase()} values. Search filters the table; period and device filter graphs and headlines.`;
}
refresh();setInterval(()=>{if(!busy)refresh();},5000);
