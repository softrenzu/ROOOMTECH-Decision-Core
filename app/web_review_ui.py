from __future__ import annotations


def render_web_review_html() -> str:
    return r'''<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ROOOMTECH Web Change Review</title>
<style>
:root{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#151515;background:#f6f6f4}
body{margin:0}.wrap{max-width:1180px;margin:0 auto;padding:28px}.top{display:flex;gap:14px;align-items:center;justify-content:space-between;flex-wrap:wrap}
h1{font-size:24px;margin:0}.muted{color:#666;font-size:13px}.panel{background:white;border:1px solid #ddd;border-radius:14px;padding:18px;margin-top:18px}
input,button,select{font:inherit;border-radius:8px;border:1px solid #bbb;padding:9px 11px}input{min-width:220px}button{cursor:pointer;background:#111;color:white;border-color:#111}button.secondary{background:white;color:#111}button.danger{background:#8b1d1d;border-color:#8b1d1d}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.cards{display:grid;gap:12px;margin-top:12px}.card{border:1px solid #ddd;border-radius:12px;padding:14px;background:#fff}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.badge{font-size:12px;padding:3px 7px;border-radius:99px;background:#eee}.score{font-variant-numeric:tabular-nums}.urls{font-size:13px;word-break:break-all}.preview{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:10px 0}.preview pre{white-space:pre-wrap;word-break:break-word;background:#f7f7f7;border-radius:8px;padding:10px;max-height:180px;overflow:auto;font-size:12px}.ok{color:#116329}.warn{color:#8a5a00}.err{color:#9b1c1c}
@media(max-width:700px){.preview{grid-template-columns:1fr}.wrap{padding:16px}}
</style>
</head>
<body><div class="wrap">
<div class="top"><div><h1>ROOOMTECH Web Change Review</h1><div class="muted">監査結果を確認してから明示操作でWordPressへ反映します。自動公開はしません。</div></div><button class="secondary" onclick="loadAll()">再読込</button></div>
<div class="panel"><div class="grid"><label>Project/Admin Key<br><input id="key" type="password" autocomplete="off" placeholder="key"></label><label>Status<br><select id="status"><option value="pending">pending</option><option value="approved">approved</option><option value="stale">stale</option><option value="failed">failed</option><option value="applied">applied</option><option value="">all</option></select></label></div><div id="msg" class="muted" style="margin-top:10px"></div></div>
<div class="panel"><h2 style="font-size:17px;margin-top:0">WordPress Connector</h2><div class="grid"><input id="cname" placeholder="Connector name"><input id="site" placeholder="https://example.com"><input id="uenv" placeholder="WP_USERNAME_ENV"><input id="penv" placeholder="WP_APP_PASSWORD_ENV"></div><div class="row" style="margin-top:10px"><button onclick="createConnector()">Connector作成</button><button class="secondary" onclick="loadConnectors()">一覧更新</button></div><div id="connectors" class="cards"></div></div>
<div class="panel"><h2 style="font-size:17px;margin-top:0">Audit & Schedule</h2><div class="row"><select id="connectorSelect"></select><button onclick="runAudit()">監査して変更候補を作成</button><button class="secondary" onclick="createDailySchedule()">毎日監査を登録</button></div><div class="muted" style="margin-top:8px">定期監査も候補作成まで。反映は必ず下のレビュー操作が必要です。</div></div>
<div class="panel"><h2 style="font-size:17px;margin-top:0">Change Proposals</h2><div id="proposals" class="cards"></div></div>
</div>
<script>
const api='/v1/project/web';
function headers(){const k=document.getElementById('key').value.trim();return {'Content-Type':'application/json','X-RTDC-Project-Key':k,'X-RTDC-Admin-Key':k}}
function say(t,cls=''){const m=document.getElementById('msg');m.textContent=t;m.className='muted '+cls}
async function req(path,opt={}){opt.headers={...headers(),...(opt.headers||{})};const r=await fetch(api+path,opt);let b=null;try{b=await r.json()}catch{}if(!r.ok)throw new Error((b&&b.detail)||('HTTP '+r.status));return b}
async function loadConnectors(){try{const rows=await req('/connectors');const box=document.getElementById('connectors'),sel=document.getElementById('connectorSelect');box.innerHTML='';sel.innerHTML='';for(const c of rows){const d=document.createElement('div');d.className='card';d.innerHTML=`<b>${esc(c.name)}</b><div class="urls">${esc(c.site_url)}</div><div class="muted">${esc(c.username_env)} / ${esc(c.application_password_env)}</div><div class="row" style="margin-top:8px"><button class="secondary" onclick="testConnector('${c.id}')">接続テスト</button></div>`;box.appendChild(d);const o=document.createElement('option');o.value=c.id;o.textContent=c.name;sel.appendChild(o)}say('Connectorを読み込みました','ok')}catch(e){say(e.message,'err')}}
async function createConnector(){try{await req('/connectors/wordpress',{method:'POST',body:JSON.stringify({name:cname.value,site_url:site.value,username_env:uenv.value,application_password_env:penv.value})});await loadConnectors()}catch(e){say(e.message,'err')}}
async function testConnector(id){try{const r=await req(`/connectors/${id}/test`,{method:'POST'});say(r.ok?'WordPress接続成功':r.detail,r.ok?'ok':'err')}catch(e){say(e.message,'err')}}
async function runAudit(){try{const id=connectorSelect.value;if(!id)throw new Error('Connectorを選択してください');say('監査中...');const r=await req('/audits/stage',{method:'POST',body:JSON.stringify({connector_id:id,audit:{max_pages:250,suggestions_per_page:5,min_semantic_score:0.18}})});say(`監査完了: ${r.run.proposals_created}件の新規候補`,'ok');await loadProposals()}catch(e){say(e.message,'err')}}
async function createDailySchedule(){try{const id=connectorSelect.value;if(!id)throw new Error('Connectorを選択してください');await req('/schedules',{method:'POST',body:JSON.stringify({connector_id:id,interval_minutes:1440,enabled:true})});say('毎日監査を登録しました','ok')}catch(e){say(e.message,'err')}}
async function loadProposals(){try{const s=status.value;const rows=await req('/proposals'+(s?`?status=${encodeURIComponent(s)}`:''));const box=document.getElementById('proposals');box.innerHTML='';if(!rows.length){box.innerHTML='<div class="muted">該当する候補はありません。</div>';return}for(const p of rows){const d=document.createElement('div');d.className='card';const auto=p.auto_applicable?'<span class="badge ok">auto-applicable</span>':'<span class="badge warn">manual only</span>';d.innerHTML=`<div class="row"><b>${esc(p.anchor_text)}</b><span class="badge">${esc(p.status)}</span>${auto}<span class="score">score ${Number(p.score).toFixed(3)}</span></div><div class="urls" style="margin-top:8px">FROM ${esc(p.source_url)}<br>TO ${esc(p.target_url)}</div><div class="muted" style="margin-top:6px">${esc(p.reason||'')}</div>${p.preview_before||p.preview_after?`<div class="preview"><pre>${esc(p.preview_before||'')}</pre><pre>${esc(p.preview_after||'')}</pre></div>`:''}<div class="row"><button ${p.auto_applicable&&['pending','approved'].includes(p.status)?'':'disabled'} onclick="approveApply('${p.id}')">確認済みとして反映</button><button class="danger" ${p.status==='applied'?'disabled':''} onclick="rejectProposal('${p.id}')">却下</button></div>`;box.appendChild(d)}}catch(e){say(e.message,'err')}}
async function approveApply(id){if(!confirm('表示内容を確認しました。WordPressへ反映しますか？'))return;try{const r=await req(`/proposals/${id}/approve-apply`,{method:'POST'});say('反映結果: '+r.status,r.status==='applied'?'ok':'warn');await loadProposals()}catch(e){say(e.message,'err')}}
async function rejectProposal(id){try{await req(`/proposals/${id}/reject`,{method:'POST'});await loadProposals()}catch(e){say(e.message,'err')}}
async function loadAll(){await loadConnectors();await loadProposals()}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
status.addEventListener('change',loadProposals);
</script></body></html>'''
