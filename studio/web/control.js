// PhysicalAI KAIST Platform 관제 화면 — 촬영 PC 들의 export 서버를 관제 서버(이 PC)를 거쳐 본다.
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names={receiving:'원본 수신',queued:'처리 대기',extracting:'원본 검증',nas_copy:'NAS 저장',verifying:'NAS 검증',done:'NAS 완료',failed:'실패',recording:'촬영 중',qc:'QC 중',calibrating:'캘리브레이션 중'};
const badge=s=>'<span class="badge '+(['done'].includes(s)?'success':['failed'].includes(s)?'fail':'')+'">'+esc(names[s]||s||'-')+'</span>';
let ov=null,sel=null,pending=false,logTimer=null;const updating={};
// 원격 업데이트 상태 (촬영 PC 의 updates\last-update.json)
const UPD={staged:'업데이트 받음',stopping:'서버 종료 중',applying:'코드 교체 중',starting:'서버 켜는 중',rolling_back:'되돌리는 중',ok:'완료',refused:'미룸',rolled_back:'되돌림',failed:'실패'};
const inProgress=u=>u&&['staged','stopping','applying','starting','rolling_back'].includes(u.status);
const outdated=p=>p.online&&p.data?.version!==ov.version&&!inProgress(p.data?.update);
function updBlock(p){
 const id=esc(p.pc.id),w=updating[p.pc.id];
 if(!p.online)return w&&Date.now()-w<120000?'<div class="upd"><span class="badge warn">업데이트 중 · 서버 재시작 중</span></div>':'';
 const d=p.data||{},u=d.update,v=d.version,latest=!!v&&v===ov.version,recent=s=>u&&Date.now()-Date.parse(u.at)<s*1000;
 if(latest&&!inProgress(u))delete updating[p.pc.id];
 let msg='';
 if(inProgress(u))msg='<span class="badge warn">'+esc(UPD[u.status])+'…</span>';
 else if(u&&u.status!=='ok'&&recent(86400))msg='<span class="msg bad">'+esc(UPD[u.status]||u.status)+': '+esc(u.message)+' ('+ago(u.at)+')</span>';
 else if(u&&u.status==='ok'&&recent(600))msg='<span class="msg">'+esc(u.message)+' ('+ago(u.at)+')</span>';
 return '<div class="upd"><span>버전 <code>'+esc(v||'알 수 없음')+'</code></span>'+(latest?'<span class="badge success">최신</span>':'<span class="badge warn">'+(v?'구버전':'업데이트 기능 없음')+'</span>')
  +(!latest&&v&&!inProgress(u)?'<button type="button" data-update="'+id+'">업데이트</button>':'')+msg+'</div>'}
async function pushUpdate(ids){
 if(pending)return;pending=true;const done=[],fail=[];
 try{for(const id of ids){const p=ov.pcs.find(x=>x.pc.id===id);try{const r=await api('/pcs/'+id+'/update','POST');if(!r.already)updating[id]=Date.now();done.push((p?.pc.name||id)+(r.already?' (이미 최신)':''))}catch(e){fail.push(e.message)}}
  toast(fail.length?'업데이트 실패 — '+fail.join(' / '):'업데이트를 보냈습니다: '+done.join(', ')+' · 촬영 PC 서버가 재시작됩니다 (약 10~30초)',!!fail.length);await refresh()}
 finally{pending=false}}
function fmt(s){const d=new Date(s);if(isNaN(d))return '';const p=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds())}
function ago(s){const t=(Date.now()-Date.parse(s))/1000;if(isNaN(t))return '-';return t<60?Math.round(t)+'초 전':t<3600?Math.round(t/60)+'분 전':Math.round(t/3600)+'시간 전'}
const gb=b=>b==null?'-':(b/1024**3).toFixed(1)+'GB';
function toast(s,err=false){$('toast').textContent=s;$('toast').style.background=err?'#9e4440':'#183e32';$('toast').hidden=false;clearTimeout(window.tt);window.tt=setTimeout(()=>$('toast').hidden=true,6500)}
async function api(url,method='GET',body){const r=await fetch('/api'+url,{method,headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});const d=await r.json().catch(()=>({}));if(!r.ok){if(r.status===401&&url!=='/login')showLogin();throw Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail||r.status))}return d}
async function act(fn,msg){if(pending)return;pending=true;try{await fn();if(msg)toast(msg);await refresh()}catch(e){toast(e.message,true)}finally{pending=false}}
function showLogin(){$('login').hidden=false;$('app').hidden=true}
const cur=()=>ov?.pcs.find(p=>p.pc.id===sel);

async function refresh(){try{ov=await api('/overview');$('login').hidden=true;$('app').hidden=false;render()}catch(e){if(!$('app').hidden)$('updated').textContent='갱신 실패: '+e.message}}
function render(){
 $('updated').textContent='갱신 '+fmt(ov.at);
 $('local-version').innerHTML='이 PC 코드 <code>'+esc(ov.version)+'</code>';
 const old=ov.pcs.filter(p=>outdated(p)&&p.data?.version);$('update-all').hidden=!old.length;$('update-all').textContent='⟳ 모두 업데이트 ('+old.length+'대)';
 if(sel&&!cur())sel=null; if(!sel&&ov.pcs.length)sel=ov.pcs[0].pc.id;
 $('pc-cards').innerHTML=ov.pcs.map(p=>{const d=p.data||{},st=(d.stations||[])[0]||{},disk=st.disk||{},jobs=d.jobs||[];
  const online=p.online,stOnline=st.last_seen&&Date.now()-Date.parse(st.last_seen)<20000,cams=(st.cameras||[]).filter(c=>c.connected).length;
  const active=jobs.filter(j=>!['done','failed'].includes(j.status)).length,failed=jobs.filter(j=>j.status==='failed').length;
  return '<div class="pc-card'+(p.pc.id===sel?' sel':'')+(online?'':' off')+'" data-pc="'+esc(p.pc.id)+'"><div class="top"><div><div class="name">'+esc(p.pc.name)+'</div><div class="url">'+esc(p.pc.url)+'</div></div>'+(online?(stOnline?'<span class="badge success">연결됨</span>':'<span class="badge warn">촬영 서버 응답 없음</span>'):'<span class="badge fail">연결 안 됨</span>')+'</div>'
   +(online?'<dl><dt>촬영 PC</dt><dd>'+esc(st.id||'-')+' · '+(st.last_seen?ago(st.last_seen):'-')+'</dd><dt>카메라</dt><dd>'+cams+' / 4'+(st.busy?' · '+esc(names[st.busy]||st.busy):'')+'</dd><dt>저장 공간</dt><dd>'+gb(disk.free)+' 남음 · 약 '+(disk.remaining_episodes??'-')+'개 촬영 가능</dd><dt>전송 대기</dt><dd>원본 '+(disk.pending??'-')+'개 · '+gb(disk.pending_bytes)+'</dd><dt>NAS</dt><dd>'+(d.nas_reachable?'연결됨':'<span class="error">접근 불가</span>')+' · 처리 중 '+active+' · 실패 '+failed+'</dd></dl>'
    :(updating[p.pc.id]&&Date.now()-updating[p.pc.id]<120000?'':'<div class="err">'+esc(p.error||'')+'</div>'))
   +updBlock(p)+'<button type="button" class="text-button remove" data-remove="'+esc(p.pc.id)+'">등록 해제</button></div>'}).join('')||'<div class="empty">등록된 촬영 PC 가 없습니다. 「＋ 촬영 PC 등록」으로 추가하세요.</div>';
 document.querySelectorAll('[data-pc]').forEach(c=>c.onclick=e=>{if(e.target.closest('[data-remove],[data-update]'))return;sel=c.dataset.pc;render();fetchLogs()});
 document.querySelectorAll('[data-update]').forEach(b=>b.onclick=()=>{const p=ov.pcs.find(x=>x.pc.id===b.dataset.update);if(confirm((p?.pc.name||'')+' 를 이 PC 의 코드('+ov.version+')로 업데이트할까요?\n촬영 PC 서버가 10~30초 꺼졌다 켜집니다. 촬영 · NAS 저장 중이면 미뤄집니다.'))pushUpdate([b.dataset.update])});
 document.querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>{const p=ov.pcs.find(x=>x.pc.id===b.dataset.remove);if(confirm((p?.pc.name||'')+' 등록을 해제할까요? (촬영 PC 의 데이터는 그대로입니다)'))act(()=>api('/pcs/'+b.dataset.remove,'DELETE'),'등록을 해제했습니다.')});
 const p=cur();$('detail').hidden=!p||!p.online;if(!p||!p.online)return;const d=p.data;
 $('tasks-title').textContent='작업 · '+p.pc.name;
 $('tasks').innerHTML=(d.tasks||[]).map(t=>'<div class="task-item"><div><b>'+esc(t.name)+'</b><small>'+esc(t.scene)+' · 목표 '+t.target+'건 · '+(t.active?'진행 중':'종료')+'</small><small>'+(t.prompt?'SAM "'+esc(t.prompt)+'" · 라벨 '+esc(t.label)+' · '+esc(t.object_name)+'<br>'+esc(t.interaction):'<span class="error">물체 정보 없음 (예전 작업)</span>')+'</small></div><button data-toggle="'+esc(t.id)+'">'+(t.active?'종료':'다시 열기')+'</button></div>').join('')||'<div class="empty">작업을 만들어 시작하세요.</div>';
 document.querySelectorAll('[data-toggle]').forEach(b=>b.onclick=()=>act(()=>api('/pcs/'+sel+'/tasks/'+b.dataset.toggle+'/toggle','POST')));
 $('nas-state').textContent=d.nas_reachable?'NAS 연결됨':'NAS 접근 불가';$('nas-state').className='badge '+(d.nas_reachable?'success':'fail');
 const jobs=d.jobs||[],tn=id=>(d.tasks||[]).find(t=>t.id===id)?.name||id;
 $('jobs').innerHTML=jobs.length?'<table><thead><tr><th>에피소드</th><th>상태</th><th>NAS 폴더 · 시간</th><th></th></tr></thead><tbody>'+jobs.map(j=>'<tr><td><code>'+esc(j.episode_id)+'</code><br><small class="muted">'+esc(tn(j.task_id))+' · '+esc(j.station)+'</small></td><td>'+badge(j.status)+(j.waiting_recording?'<br><small class="muted">녹화 중이라 대기</small>':'')+(!['done','failed'].includes(j.status)?'<div class="bar"><i style="width:'+Math.min(100,j.progress||0)+'%"></i></div>':'')+'</td><td>'+(j.status==='done'?'<code>'+esc(String(j.output||'').split(/[\\/]/).pop())+'</code><br><small class="muted">NAS '+(j.timings?.nas_s??'-')+'초 · 전체 '+(j.timings?.total_s??'-')+'초</small>':'<small class="muted">'+esc(fmt(j.updated_at||j.created_at))+'</small>')+(j.error?'<div class="err">'+esc(j.error)+'</div>':'')+'</td><td>'+(j.status==='failed'?'<button data-retry="'+esc(j.id)+'">재시도</button>':'')+'</td></tr>').join('')+'</tbody></table>':'<div class="empty">아직 NAS 저장 기록이 없습니다.</div>';
 document.querySelectorAll('[data-retry]').forEach(b=>b.onclick=()=>act(()=>api('/pcs/'+sel+'/jobs/'+b.dataset.retry+'/retry','POST'),'재시도를 등록했습니다.'));
}
async function fetchLogs(){const p=cur();if(!p||!p.online){$('logs').innerHTML='';return}const q=new URLSearchParams();for(const [k,id] of [['episode','log-episode'],['q','log-q'],['level','log-level']]){const v=$(id).value.trim();if(v)q.set(k,v)}q.set('limit','500');
 try{const list=await api('/pcs/'+sel+'/logs?'+q);const f=[...q.keys()].length>1;$('log-count').textContent=(f?'찾은 로그 ':'최근 로그 ')+list.length+'건';
  $('logs').innerHTML=list.map(l=>'<div class="log '+esc(l.level)+'"><time>'+esc(fmt(l.at))+'</time><span>'+esc(String(l.level).toUpperCase())+'</span><span class="log-ep">'+(l.episode?'<button type="button" class="ep-chip" data-ep="'+esc(l.episode)+'">'+esc(l.episode)+'</button>':'')+'</span><span>'+esc(names[l.message]||l.message)+'</span></div>').join('')||'<div class="empty">로그가 없습니다.</div>';
  document.querySelectorAll('[data-ep]').forEach(b=>b.onclick=()=>{$('log-episode').value=b.dataset.ep;onLog()})}catch(e){$('log-count').textContent=e.message}}
function onLog(){clearTimeout(logTimer);logTimer=setTimeout(fetchLogs,250)}

$('login-form').onsubmit=async e=>{e.preventDefault();$('login-error').textContent='';try{await api('/login','POST',{password:$('password').value});$('password').value='';await refresh();fetchLogs()}catch(e){$('login-error').textContent=e.message}};
$('logout').onclick=async()=>{await api('/logout','POST');showLogin()};
$('update-all').onclick=()=>{const old=ov.pcs.filter(p=>outdated(p)&&p.data?.version);if(old.length&&confirm(old.map(p=>p.pc.name).join(', ')+'\n'+old.length+'대를 이 PC 의 코드('+ov.version+')로 업데이트할까요? 각 촬영 PC 서버가 10~30초 꺼졌다 켜집니다.'))pushUpdate(old.map(p=>p.pc.id))};
$('show-add').onclick=()=>{$('add-form').hidden=false;$('pc-name').focus()};$('hide-add').onclick=()=>$('add-form').hidden=true;
$('add-form').onsubmit=e=>{e.preventDefault();act(async()=>{const pc=await api('/pcs','POST',{name:$('pc-name').value,url:$('pc-url').value,token:$('pc-token').value});sel=pc.id;for(const id of ['pc-name','pc-url','pc-token'])$(id).value='';$('add-form').hidden=true},'촬영 PC 를 등록했습니다.')};
$('task-form').onsubmit=e=>{e.preventDefault();if(!sel)return;act(async()=>{await api('/pcs/'+sel+'/tasks','POST',{name:$('task-name').value,scene:$('task-scene').value,target:Number($('task-target').value),prompt:$('task-prompt').value,label:$('task-label').value,object_name:$('task-object-name').value,interaction:$('task-interaction').value});for(const id of ['task-name','task-prompt','task-label','task-object-name','task-interaction'])$(id).value=''},'작업을 만들었습니다.')};
for(const id of ['log-episode','log-q'])$(id).oninput=onLog;$('log-level').onchange=onLog;$('log-clear').onclick=()=>{for(const id of ['log-episode','log-q','log-level'])$(id).value='';fetchLogs()};
refresh().then(()=>{if(!$('app').hidden)fetchLogs()});
setInterval(()=>{if(!$('app').hidden)refresh()},3000);
setInterval(()=>{if(!$('app').hidden&&!['log-episode','log-q','log-level'].some(id=>$(id).value.trim()))fetchLogs()},6000);
