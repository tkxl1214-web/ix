const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = { token: localStorage.getItem('localmind.token'), user: null, conversationId: null, conversations: [], view: 'chat', authMode: 'login', dashboardTimer: null, trained: false };

function esc(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function toast(message, error = false) { const el = document.createElement('div'); el.className = `toast${error ? ' error' : ''}`; el.textContent = message; $('#toast-root').append(el); setTimeout(() => el.remove(), 3200); }
function fmtDate(value) { if (!value) return '—'; const d = new Date(value.replace(' ', 'T') + (value.includes('Z') ? '' : 'Z')); return new Intl.DateTimeFormat('ko-KR', {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}).format(d); }
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set('Authorization', `Bearer ${state.token}`);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  const response = await fetch(path, {...options, headers});
  if (response.status === 401 && state.token) { logout(false); throw new Error('로그인이 만료되었습니다. 다시 로그인해 주세요.'); }
  const contentType = response.headers.get('content-type') || '';
  const data = contentType.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) throw new Error(data?.detail || data || `요청 실패 (${response.status})`);
  return data;
}
function showAuth() { $('#auth-view').classList.remove('hidden'); $('#app').classList.add('hidden'); }
function showApp() { $('#auth-view').classList.add('hidden'); $('#app').classList.remove('hidden'); }
function setProfile() {
  $('#profile-email').textContent = state.user.email;
  $('#avatar').textContent = (state.user.email[0] || 'U').toUpperCase();
  $('#profile-role').textContent = state.user.role === 'admin' ? '관리자' : '일반 사용자';
  $$('.admin-only').forEach(el => el.classList.toggle('hidden', state.user.role !== 'admin'));
}
function updateRegistrationButton() {
  const allowed = ['#consent-terms', '#consent-privacy', '#consent-youth'].every(selector => $(selector).checked);
  if (state.authMode !== 'login') $('#auth-submit').disabled = !allowed;
}
function setAuthMode(mode) {
  state.authMode = mode;
  const googleRegistering = mode === 'google-register';
  const registering = googleRegistering;
  const fields = $('#auth-fields');
  const emailToggle = $('#email-login-toggle');
  $('#auth-view').dataset.mode = registering ? 'register' : 'login';
  $('#auth-title').textContent = registering ? '계정 만들기' : 'iX에 로그인';
  $('#auth-submit').textContent = registering ? '가입하기' : '로그인';
  $('#auth-switch').textContent = mode==='google-register' ? 'Google 가입을 취소하고 로그인' : 'Google로 가입하기';
  $('#google-login').classList.toggle('hidden', registering);
  $('#google-auth-note').classList.toggle('hidden', registering);
  $('#google-identity').classList.toggle('hidden', !googleRegistering);
  emailToggle.classList.toggle('hidden', registering);
  emailToggle.textContent = '이메일 로그인 (기존 계정)';
  emailToggle.setAttribute('aria-expanded', 'false');
  fields.classList.toggle('hidden', !googleRegistering);
  $('#auth-submit').classList.toggle('hidden', !registering);
  $('#auth-password').autocomplete = registering ? 'new-password' : 'current-password';
  $('#auth-email').required = false;
  $('#auth-password').required = false;
  $('#auth-name').required = registering;
  $('#auth-age').required = registering;
  $$('.email-password-field').forEach(element => element.classList.toggle('hidden', googleRegistering));
  $$('.register-only').forEach(element => element.classList.toggle('hidden', !registering));
  ['#consent-terms', '#consent-privacy', '#consent-youth'].forEach(selector => {
    $(selector).required = registering;
    if (!registering) $(selector).checked = false;
  });
  $('#auth-submit').disabled = registering;
  $('#auth-error').textContent = '';
  updateRegistrationButton();
}
async function enterApp() {
  state.user = await api('/api/auth/me');
  setProfile(); showApp(); await refreshConversations(); await refreshLearningHint(); showView('chat');
}
function logout(show = true) {
  fetch('/api/auth/logout', {method:'POST'}).catch(()=>{});
  localStorage.removeItem('localmind.token'); state.token = null; state.user = null; state.conversationId = null;
  if (state.dashboardTimer) clearInterval(state.dashboardTimer);
  if (show) toast('로그아웃했습니다.');
  showAuth();
}

$('#auth-switch').addEventListener('click', () => {
  const previous=state.authMode;
  if (previous === 'google-register') {
    fetch('/api/auth/google/registration/cancel', {method:'POST'}).catch(()=>{});
    setAuthMode('login');
    return;
  }
  $('#auth-error').textContent='Google 계정으로 인증한 다음 이름과 이용 동의를 완료해 주세요.';
  beginGoogleAuth();
});
$('#email-login-toggle').addEventListener('click', () => {
  const open = $('#auth-fields').classList.contains('hidden');
  $('#auth-fields').classList.toggle('hidden', !open);
  $('#auth-submit').classList.toggle('hidden', !open);
  $('#auth-email').required = open;
  $('#auth-password').required = open;
  $('#email-login-toggle').textContent = open ? '이메일 로그인 닫기' : '이메일 로그인 (기존 계정)';
  $('#email-login-toggle').setAttribute('aria-expanded', String(open));
});
['#consent-terms', '#consent-privacy', '#consent-youth'].forEach(selector => $(selector).addEventListener('change', updateRegistrationButton));
$('#auth-form').addEventListener('submit', async event => {
  event.preventDefault(); $('#auth-submit').disabled = true; $('#auth-error').textContent = '';
  try {
    const googleRegistering = state.authMode === 'google-register';
    if (!googleRegistering && $('#auth-fields').classList.contains('hidden')) return;
    const path = googleRegistering ? '/api/auth/google/register' : '/api/auth/login';
    const payload = googleRegistering ? {
      display_name:$('#auth-name').value,
      age:Number($('#auth-age').value),
      accepted_terms:$('#consent-terms').checked,
      accepted_privacy:$('#consent-privacy').checked,
      accepted_youth:$('#consent-youth').checked,
    } : {email:$('#auth-email').value,password:$('#auth-password').value};
    const result = await api(path, {method:'POST', body:JSON.stringify(payload)});
    state.token = result.token; localStorage.setItem('localmind.token', state.token); await enterApp();
  } catch (error) { $('#auth-error').textContent = error.message; $('#auth-submit').disabled = state.authMode==='google-register' && !['#consent-terms','#consent-privacy','#consent-youth'].every(selector => $(selector).checked); }
});

async function refreshConversations() {
  state.conversations = await api('/api/conversations');
  const list = $('#conversation-list'); list.replaceChildren();
  if (!state.conversations.length) { list.innerHTML = '<div class="empty-state" style="border:0;padding:13px 5px">대화가 아직 없습니다.</div>'; return; }
  for (const item of state.conversations) {
    const button = document.createElement('button'); button.className = `conversation-item${item.id === state.conversationId ? ' active' : ''}`;
    button.innerHTML = `<span>◦</span><span style="overflow:hidden;text-overflow:ellipsis">${esc(item.title || '새 대화')}</span><span class="delete-chat" title="대화 삭제">×</span>`;
    button.addEventListener('click', event => { if (event.target.classList.contains('delete-chat')) { event.stopPropagation(); removeConversation(item.id); } else openConversation(item.id); });
    list.append(button);
  }
}
async function createConversation() {
  const item = await api('/api/conversations', {method:'POST'}); state.conversationId = item.id;
  $('#message-list').replaceChildren(); $('#message-list').classList.add('hidden'); $('#chat-empty').classList.remove('hidden');
  $('#chat-input').focus(); await refreshConversations(); showView('chat'); closeMobileMenu();
}
async function removeConversation(id) {
  if (!confirm('이 대화와 메시지를 삭제할까요?')) return;
  try { await api(`/api/conversations/${id}`, {method:'DELETE'}); if (state.conversationId === id) { state.conversationId = null; $('#message-list').replaceChildren(); $('#message-list').classList.add('hidden'); $('#chat-empty').classList.remove('hidden'); } await refreshConversations(); toast('대화를 삭제했습니다.'); }
  catch (error) { toast(error.message, true); }
}
async function openConversation(id) {
  state.conversationId = id; showView('chat'); $('#chat-empty').classList.add('hidden'); $('#message-list').classList.remove('hidden');
  await loadMessages(id); await refreshConversations(); closeMobileMenu();
}
function addMessage(message, stream = false) {
  const node = document.createElement('article'); node.className = `message ${message.role}`; node.dataset.messageId = message.id || '';
  const avatar = document.createElement('div'); avatar.className = 'message-avatar'; avatar.textContent = message.role === 'assistant' ? '◉' : (state.user?.email[0] || 'U').toUpperCase();
  const body = document.createElement('div'); body.className = 'message-body';
  const label = document.createElement('div'); label.className = 'message-label'; label.textContent = message.role === 'assistant' ? 'ixaistudio' : '나';
  const text = document.createElement('div'); text.className = 'message-text'; text.textContent = message.content || '';
  body.append(label, text);
  if (message.role === 'assistant' && message.id && !stream) {
    const tools = document.createElement('div'); tools.className = 'message-tools';
    const like = document.createElement('button'); like.textContent = '↑'; like.title = '도움이 됐어요'; like.classList.toggle('selected', message.feedback === 1);
    const dislike = document.createElement('button'); dislike.textContent = '↓'; dislike.title = '도움이 되지 않았어요'; dislike.classList.toggle('selected', message.feedback === -1);
    const label = document.createElement('label'); const consent = document.createElement('input'); consent.type = 'checkbox'; label.append(consent, document.createTextNode('개선 학습에 승인'));
    for (const [button, rating] of [[like,1],[dislike,-1]]) button.addEventListener('click', async () => {
      try { const result = await api('/api/feedback', {method:'POST',body:JSON.stringify({message_id:message.id,rating,approve_for_training:consent.checked})});
        toast(result.submitted_for_review ? '승인된 피드백이 관리자 검토에 등록됐습니다.' : '피드백을 저장했습니다.'); button.classList.add('selected'); consent.checked = false; refreshLearningHint();
      } catch (error) { toast(error.message,true); }
    });
    tools.append(like, dislike, label); body.append(tools);
  }
  node.append(avatar, body); $('#message-list').append(node); return {node,text};
}
async function loadMessages(id) {
  const messages = await api(`/api/conversations/${id}/messages`); const list = $('#message-list'); list.replaceChildren();
  for (const message of messages) addMessage(message);
  list.scrollTop = list.scrollHeight;
  $('#chat-empty').classList.toggle('hidden', messages.length > 0); list.classList.toggle('hidden', messages.length === 0);
}
async function sendMessage(value) {
  const content = value.trim(); if (!content) return;
  $('#chat-input').value = ''; $('#char-count').textContent = '0 / 5000'; $('#chat-input').style.height = 'auto';
  if (!state.conversationId) { const item = await api('/api/conversations',{method:'POST'}); state.conversationId = item.id; }
  $('#chat-empty').classList.add('hidden'); $('#message-list').classList.remove('hidden');
  addMessage({role:'user',content}); const waiting = addMessage({role:'assistant',content:''},true);
  waiting.text.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>'; $('#message-list').scrollTop = $('#message-list').scrollHeight;
  $('#send-button').disabled = true;
  try {
    const response = await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','Authorization':`Bearer ${state.token}`},body:JSON.stringify({content,conversation_id:state.conversationId})});
    if (!response.ok) { const error = await response.json().catch(()=>({detail:'응답을 생성하지 못했습니다.'})); throw new Error(error.detail); }
    const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
    waiting.text.textContent = '';
    while (true) {
      const {value,done} = await reader.read(); buffer += decoder.decode(value || new Uint8Array(),{stream:!done});
      const events = buffer.split(/\r?\n\r?\n/); buffer = events.pop() || '';
      for (const raw of events) {
        const lines = raw.split(/\r?\n/); const type = lines.find(line=>line.startsWith('event:'))?.slice(6).trim();
        const dataLine = lines.find(line=>line.startsWith('data:'))?.slice(5).trim(); if (!dataLine) continue;
        const data = JSON.parse(dataLine);
        if (type === 'meta') state.conversationId = data.conversation_id;
        if (type === 'token') { waiting.text.textContent += data.text; $('#message-list').scrollTop = $('#message-list').scrollHeight; }
        if (type === 'error') throw new Error(data.detail);
      }
      if (done) break;
    }
    if (!waiting.text.textContent) waiting.node.remove();
    await loadMessages(state.conversationId); await refreshConversations();
  } catch (error) { waiting.node.remove(); toast(error.message || '응답 중 오류가 발생했습니다.',true); }
  finally { $('#send-button').disabled = false; $('#chat-input').focus(); }
}
$('#chat-form').addEventListener('submit', event => { event.preventDefault(); sendMessage($('#chat-input').value); });
$('#chat-input').addEventListener('input', event => { const field = event.target; field.style.height='auto'; field.style.height=`${Math.min(field.scrollHeight,140)}px`; $('#char-count').textContent=`${field.value.length} / 5000`; });
$('#chat-input').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('#chat-form').requestSubmit(); } });
$$('.suggestion').forEach(button => button.addEventListener('click', () => { $('#chat-input').value=button.dataset.prompt; $('#chat-input').dispatchEvent(new Event('input')); $('#chat-form').requestSubmit(); }));
$('#new-chat').addEventListener('click', createConversation); $('#refresh-chats').addEventListener('click', () => refreshConversations().catch(error=>toast(error.message,true)));

const titles = {chat:['대화','개인 작업 공간'],learning:['학습 센터','모델과 성능'],data:['데이터 관리','동의와 검토'],memory:['장기 기억','사용자 전용 메모']};
function showView(view) {
  state.view=view; $$('.view').forEach(el=>el.classList.add('hidden')); $(`#view-${view}`).classList.remove('hidden');
  $('#app').dataset.view=view;
  $$('.nav-item').forEach(el=>el.classList.toggle('active',el.dataset.view===view)); $('#top-title').textContent=titles[view][0]; $('#top-subtitle').textContent=titles[view][1];
  if (state.dashboardTimer) { clearInterval(state.dashboardTimer); state.dashboardTimer=null; }
  if (view==='learning') { renderLearning().catch(error=>toast(error.message,true)); state.dashboardTimer=setInterval(()=>renderLearning().catch(()=>{}),3000); }
  if (view==='data') renderData().catch(error=>toast(error.message,true));
  if (view==='memory') renderMemory().catch(error=>toast(error.message,true));
  closeMobileMenu();
}
$$('.nav-item').forEach(button=>button.addEventListener('click',()=>showView(button.dataset.view)));
$$('[data-open-view]').forEach(button=>button.addEventListener('click',()=>showView(button.dataset.openView)));

function statusLabel(status) { return ({pending:'검토 대기',approved:'승인됨',rejected:'거부됨',running:'학습 중',queued:'대기 중',paused:'일시 중단',completed:'완료',failed:'오류',cancelled:'중단됨'})[status]||status; }
function metricCard(label,value,foot,icon) { return `<div class="metric-card"><div class="metric-label">${label}<span>${icon}</span></div><div class="metric-value">${value}</div><div class="metric-foot">${foot}</div></div>`; }
function drawChart(metrics) {
  const canvas=$('#loss-chart'); if (!canvas) return; const rect=canvas.getBoundingClientRect(); const scale=window.devicePixelRatio||1; canvas.width=rect.width*scale; canvas.height=rect.height*scale;
  const ctx=canvas.getContext('2d'); ctx.scale(scale,scale); const w=rect.width,h=rect.height,pad={l:32,r:12,t:12,b:22};
  ctx.clearRect(0,0,w,h); ctx.font='9px DM Sans, sans-serif'; ctx.fillStyle=getComputedStyle(document.body).getPropertyValue('--muted'); ctx.strokeStyle=getComputedStyle(document.body).getPropertyValue('--line');
  const vals=metrics.flatMap(m=>[m.training_loss,m.validation_loss]).filter(Number.isFinite); const max=Math.max(1,...vals),min=Math.min(0,...vals),xstep=(w-pad.l-pad.r)/Math.max(1,metrics.length-1);
  for(let i=0;i<4;i++){const y=pad.t+(h-pad.t-pad.b)*i/3;ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(w-pad.r,y);ctx.stroke();ctx.fillText((max-(max-min)*i/3).toFixed(1),2,y+3);}
  for(const [key,color] of [['training_loss','#9cca61'],['validation_loss','#d49a6c']]){ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();metrics.forEach((m,i)=>{const x=pad.l+i*xstep,y=pad.t+(h-pad.t-pad.b)*(1-(m[key]-min)/(max-min||1));i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke();metrics.forEach((m,i)=>{const x=pad.l+i*xstep,y=pad.t+(h-pad.t-pad.b)*(1-(m[key]-min)/(max-min||1));ctx.fillStyle=color;ctx.beginPath();ctx.arc(x,y,2.5,0,Math.PI*2);ctx.fill();});}
}
async function renderLearning() {
  const data=await api('/api/dashboard'); state.trained=data.model.trained; $('#sidebar-version').textContent=data.model.version; $('#sidebar-model-state').textContent=data.model.trained?'학습 완료':'초기 모델';
  const active=(await api('/api/training/jobs').catch(()=>[])).find(job=>['queued','running','paused'].includes(job.status));
  const admin=state.user.role==='admin';
  const autoConfig=admin?await api('/api/training/auto').catch(()=>({enabled:true,min_new_items:1})):null;
  const latest=data.recent_jobs[0];
  const materialHeading=data.learning_materials_status==='running'?'현재 학습 중인 내용':data.learning_materials_status==='completed'?'최근 학습한 내용':'자동 학습 대기 자료';
  $('#view-learning').innerHTML=`<div class="page-head"><div><p class="eyebrow">MODEL OVERVIEW</p><h1>학습 센터</h1><p>직접 수집하고 승인한 데이터로 자체 신경망을 개선합니다.</p></div><div class="page-actions">${admin?`<button class="secondary" id="auto-training">자동 학습 ${autoConfig.enabled?'켜짐':'꺼짐'}</button><button class="secondary" id="go-data">데이터 검토</button><button class="primary" id="start-training">＋ 학습 시작</button>`:''}</div></div>
    <div class="metric-grid">${metricCard('현재 모델',esc(data.model.version),data.model.trained?`${Number(data.model.parameters).toLocaleString()}개 파라미터`:'아직 학습 전','◉')}${metricCard('승인 데이터',data.approved_data_count,'학습에 사용할 수 있는 문서','▤')}${metricCard('완료한 학습',data.completed_training_count,'누적 작업','↗')}${metricCard('검토 대기',data.pending_data_count,'승인 전 데이터','⌑')}</div>
    ${active?`<div class="panel" style="max-width:1050px;margin:0 auto 13px"><div class="panel-head"><h2>현재 학습 작업</h2><span>${statusLabel(active.status)} · ${Math.round(active.progress*100)}%</span></div><div class="progress"><i style="width:${Math.round(active.progress*100)}%"></i></div><div class="job-row" style="border:0;padding:10px 0 0"><div><strong>에포크 ${active.epoch} / ${active.total_epochs}</strong><small>Train loss ${Number(active.training_loss||0).toFixed(3)} · Validation ${Number(active.validation_loss||0).toFixed(3)}</small></div><div class="row-tail">${active.status==='running'?'<button class="secondary job-action" data-action="pause" data-id="'+esc(active.id)+'">일시 중단</button>':''}${active.status==='paused'?'<button class="secondary job-action" data-action="resume" data-id="'+esc(active.id)+'">재개</button>':''}<button class="danger-button job-action" data-action="cancel" data-id="${esc(active.id)}">중단</button></div></div></div>`:''}
    <div class="dashboard-grid"><div class="panel"><div class="panel-head"><h2>학습 손실 변화</h2><span>${data.metrics.length?'에포크별 검증':'학습 기록이 여기에 표시됩니다'}</span></div><div class="chart-wrap"><canvas id="loss-chart"></canvas></div><div class="chart-legend"><span><i class="legend-dot"></i>Training loss</span><span><i class="legend-dot orange"></i>Validation loss</span></div></div>
    <div class="panel"><div class="panel-head"><h2>시스템 자원</h2><span>이 서버</span></div><div class="version-list"><div class="version-row"><span>◉</span><div><strong>실행 장치</strong><small>${esc(data.model.device)}${data.system.gpu_name?' · '+esc(data.system.gpu_name):' · GPU 없음'}</small></div></div><div class="version-row"><span>◫</span><div><strong>CPU</strong><small>${data.system.cpu_percent==null?'측정 불가':data.system.cpu_percent+'%'}</small></div></div><div class="version-row"><span>▥</span><div><strong>메모리</strong><small>${data.system.memory_used_gb==null?'측정 불가':`${data.system.memory_used_gb} / ${data.system.memory_total_gb} GB · ${data.system.memory_percent}%`}</small></div></div><div class="version-row"><span>▣</span><div><strong>GPU 메모리</strong><small>${data.system.gpu_memory_used_gb==null?'해당 없음':data.system.gpu_memory_used_gb+' GB'}</small></div></div></div></div>
    <div class="panel"><div class="panel-head"><h2>모델 버전</h2><span>검증 손실 기준 비교</span></div><div class="version-list">${data.versions.length?data.versions.map(v=>`<div class="version-row"><span>◉</span><div><strong>${esc(v.version)}${v.active?'<span class="version-active">현재 사용</span>':''}</strong><small>검증 손실 ${Number(v.validation_loss).toFixed(4)} · ${fmtDate(v.created_at)}</small></div><div class="row-tail">${admin&&!v.active?`<button class="secondary rollback" data-version="${esc(v.version)}">복구</button>`:''}</div></div>`).join(''):'<div class="empty-state">저장된 버전이 없습니다.</div>'}</div></div>
    <div class="panel"><div class="panel-head"><h2>최근 학습 기록</h2><span>최근 8개</span></div><div class="job-list">${data.recent_jobs.length?data.recent_jobs.map(j=>`<div class="job-row"><span class="status-pill ${esc(j.status)}">${statusLabel(j.status)}</span><div><strong>${esc(j.id.slice(0,8))} · ${j.epoch}/${j.total_epochs} epoch</strong><small>${fmtDate(j.created_at)}${j.validation_loss==null?'':` · 검증 ${Number(j.validation_loss).toFixed(3)}`}</small>${j.error?`<small style="color:#ae5949">${esc(j.error)}</small>`:''}</div><div class="row-tail"><small>${Math.round(j.progress*100)}%</small></div></div>`).join(''):'<div class="empty-state">첫 학습을 시작하면 기록이 나타납니다.</div>'}</div></div>
    <div class="panel learning-materials-panel"><div class="panel-head"><h2>${materialHeading}</h2><span>${data.learning_materials.length}개 자료</span></div><div class="material-list">${data.learning_materials.length?data.learning_materials.map(material=>`<article class="material-card"><div><strong>${esc(material.source)}</strong><small>${esc(material.category)} · ${Number(material.characters).toLocaleString()}자</small></div><p>${esc(material.preview)}${Number(material.characters)>360?'…':''}</p></article>`).join(''):'<div class="empty-state">승인된 학습 자료가 아직 없습니다.</div>'}</div></div></div>`;
  drawChart(data.metrics);
  $('#start-training')?.addEventListener('click',openTrainingDialog); $('#go-data')?.addEventListener('click',()=>showView('data')); $('#auto-training')?.addEventListener('click',()=>openAutoTrainingDialog(autoConfig));
  $$('.rollback').forEach(b=>b.addEventListener('click',()=>rollbackModel(b.dataset.version)));
  $$('.job-action').forEach(b=>b.addEventListener('click',()=>jobAction(b.dataset.id,b.dataset.action)));
}
function openTrainingDialog() {
  $('#modal-content').innerHTML=`<h2>새 학습 작업</h2><p>동의와 관리자 승인이 완료된 ${document.querySelector('.metric-card:nth-child(2) .metric-value')?.textContent||0}개 데이터 항목을 사용합니다. 매 에포크마다 검증 손실을 측정하고, 기존 모델보다 개선된 경우에만 새 버전을 활성화합니다.</p><label class="field-label">학습 에포크<select id="train-epochs"><option value="1">1 에포크 · 빠른 확인</option><option value="3" selected>3 에포크 · 권장</option><option value="5">5 에포크</option><option value="10">10 에포크</option></select></label><p>CPU 학습은 데이터와 장치에 따라 시간이 걸릴 수 있습니다. 웹 서버는 기존 모델로 계속 응답합니다.</p><div class="modal-actions"><button class="secondary" data-close-modal>취소</button><button class="primary" id="confirm-train">학습 시작</button></div>`;
  $('#modal').showModal(); $('[data-close-modal]').onclick=()=>$('#modal').close(); $('#confirm-train').onclick=async()=>{try{await api('/api/training/start',{method:'POST',body:JSON.stringify({epochs:Number($('#train-epochs').value)})});$('#modal').close();toast('별도 학습 프로세스를 시작했습니다.');await renderLearning();}catch(error){toast(error.message,true);}};
}
function openAutoTrainingDialog(config) {
  $('#modal-content').innerHTML=`<h2>자동 학습 설정</h2><p>내장 기초 자료는 자동 학습에 포함됩니다. 이후 사용자가 제공한 대화나 문서는 명시적 동의와 관리자 승인을 거친 뒤, 새 자료가 쌓이면 별도 프로세스에서 학습합니다. 같은 자료만 반복해서 외우는 작업은 자동으로 만들지 않습니다.</p><label class="consent-row"><input id="auto-enabled" type="checkbox" ${config.enabled?'checked':''}><span>자동 학습 활성화</span></label><label class="field-label" style="margin-top:12px">새로 승인된 항목 기준<input id="auto-threshold" type="number" min="1" max="100" value="${Number(config.min_new_items)||1}"></label><div class="modal-actions"><button class="secondary" data-close-modal>취소</button><button class="primary" id="save-auto">저장</button></div>`;
  $('#modal').showModal(); $('[data-close-modal]').onclick=()=>$('#modal').close(); $('#save-auto').onclick=async()=>{try{const threshold=Number($('#auto-threshold').value);if(threshold<1||threshold>100)throw new Error('기준은 1~100 사이여야 합니다.');await api('/api/training/auto',{method:'PUT',body:JSON.stringify({enabled:$('#auto-enabled').checked,min_new_items:threshold})});$('#modal').close();toast('자동 학습 설정을 저장했습니다.');await renderLearning();}catch(error){toast(error.message,true);}};
}
async function jobAction(id,action) { try { await api(`/api/training/jobs/${id}/${action}`,{method:'POST'});toast(({pause:'일시 중단을 요청했습니다.',resume:'학습을 재개했습니다.',cancel:'중단을 요청했습니다.'})[action]); await renderLearning(); } catch(error){toast(error.message,true);} }
async function rollbackModel(version) { if(!confirm(`${version} 모델을 활성 버전으로 복구할까요?`))return;try{await api(`/api/models/rollback/${version}`,{method:'POST'});toast(`${version} 모델로 복구했습니다.`);await renderLearning();}catch(error){toast(error.message,true);} }

async function refreshLearningHint() {
  try { const result=await api('/api/dashboard'); state.trained=result.model.trained; $('#untrained-note').classList.toggle('hidden',result.model.trained||state.view!=='chat'); $('#sidebar-version').textContent=result.model.version; $('#sidebar-model-state').textContent=result.model.trained?'학습 완료':'초기 모델';
    if(state.user?.role==='admin'){const pending=result.pending_data_count;$('#pending-badge').textContent=pending;$('#pending-badge').classList.toggle('hidden',!pending);}
  } catch {}
}

async function renderData() {
  const datasets=await api('/api/datasets'); const admin=state.user.role==='admin';
  $('#view-data').innerHTML=`<div class="page-head"><div><p class="eyebrow">CONSENTED TRAINING DATA</p><h1>${admin?'데이터 관리':'학습 데이터'}</h1><p>${admin?'동의된 항목을 검토하고 승인된 데이터만 학습에 사용합니다.':'직접 입력하거나 문서를 올려 학습 데이터 후보를 만듭니다.'}</p></div></div>
    <div class="data-layout"><div class="panel"><div class="panel-head"><h2>텍스트 제출</h2><span>명시적 동의 · 관리자 검토</span></div><form id="data-form" class="data-form"><label class="field-label">텍스트 내용<small>20자 이상 · 최대 200,000자</small><textarea id="dataset-text" minlength="20" maxlength="200000" required placeholder="학습에 사용할 문장, 설명, 대화 예시를 입력하세요."></textarea></label><label class="field-label">분류<select id="dataset-category"><option value="">자동 분류</option><option value="knowledge">지식</option><option value="conversation">대화</option><option value="feedback">피드백</option><option value="general">일반</option></select></label><label class="consent-row"><input id="dataset-consent" type="checkbox" required><span>이 텍스트를 자체 모델 학습에 사용하도록 승인합니다. 제출 데이터는 관리자 검토 후 여러 사용자의 모델 개선에 쓰일 수 있습니다.</span></label><button class="primary" type="submit">검토 요청 제출</button></form><div style="height:15px"></div><div class="panel-head"><h2>문서 업로드</h2><span>최대 5MB</span></div><form id="upload-form" class="data-form"><input id="dataset-file" class="data-file" type="file" accept=".txt,.md,.csv,.json,.pdf,.docx" required><label class="consent-row"><input id="upload-consent" type="checkbox" required><span>파일에서 추출한 텍스트를 자체 모델 학습에 사용하도록 승인합니다.</span></label><button class="secondary" type="submit">문서에서 텍스트 가져오기</button><small class="muted">지원: TXT, MD, CSV, JSON, PDF, DOCX</small></form></div>
    <div class="panel"><div class="panel-head"><h2>${admin?'전체 학습 데이터':'내 데이터'}</h2><span>${datasets.length}개</span></div><div class="data-list">${datasets.length?datasets.map(d=>`<div class="data-row"><div class="row-main"><strong>${admin?'#'+d.id+' · ':''}${esc(d.category)}</strong><small>${esc(d.source)} · ${Number(d.characters).toLocaleString()}자 · ${fmtDate(d.created_at)}</small></div><div class="row-tail"><span class="status-pill ${esc(d.status)}">${statusLabel(d.status)}</span><button class="secondary inspect-data" data-id="${d.id}">내용</button>${admin&&d.status==='pending'?`<button class="primary review-data" data-id="${d.id}" data-decision="approve">승인</button><button class="danger-button review-data" data-id="${d.id}" data-decision="reject">거부</button>`:''}<button class="icon-button delete-data" data-id="${d.id}" title="삭제">×</button></div></div>`).join(''):'<div class="empty-state">아직 등록된 학습 데이터가 없습니다.</div>'}</div></div></div>`;
  $('#data-form').addEventListener('submit',async e=>{e.preventDefault();try{const result=await api('/api/datasets',{method:'POST',body:JSON.stringify({content:$('#dataset-text').value,consent:$('#dataset-consent').checked,category:$('#dataset-category').value||null})});toast(result.message);$('#data-form').reset();await renderData();refreshLearningHint();}catch(error){toast(error.message,true);}});
  $('#upload-form').addEventListener('submit',async e=>{e.preventDefault();const file=$('#dataset-file').files[0];if(!file)return;const form=new FormData();form.append('file',file);form.append('consent',$('#upload-consent').checked?'true':'false');try{const result=await api('/api/datasets/upload',{method:'POST',body:form});toast(result.message);$('#upload-form').reset();await renderData();refreshLearningHint();}catch(error){toast(error.message,true);}});
  $$('.inspect-data').forEach(b=>b.addEventListener('click',()=>inspectDataset(b.dataset.id)));
  $$('.review-data').forEach(b=>b.addEventListener('click',()=>reviewDataset(b.dataset.id,b.dataset.decision)));
  $$('.delete-data').forEach(b=>b.addEventListener('click',()=>deleteDataset(b.dataset.id)));
}
async function inspectDataset(id) { try{const d=await api(`/api/datasets/${id}`);$('#modal-content').innerHTML=`<h2>학습 데이터 #${d.id}</h2><p>${esc(d.category)} · ${statusLabel(d.status)} · ${d.consent?'학습 동의됨':'동의 없음'}</p><div class="panel" style="white-space:pre-wrap;max-height:55vh;overflow:auto;font-size:10px;line-height:1.65">${esc(d.content)}</div><div class="modal-actions"><button class="secondary" data-close-modal>닫기</button></div>`;$('#modal').showModal();$('[data-close-modal]').onclick=()=>$('#modal').close();}catch(error){toast(error.message,true);} }
async function reviewDataset(id,decision) { try{await api(`/api/datasets/${id}/review?decision=${decision}`,{method:'POST'});toast(decision==='approve'?'학습 데이터가 승인됐습니다.':'학습 데이터를 거부했습니다.');await renderData();refreshLearningHint();}catch(error){toast(error.message,true);} }
async function deleteDataset(id) { if(!confirm('이 데이터 항목을 삭제할까요?'))return;try{await api(`/api/datasets/${id}`,{method:'DELETE'});toast('데이터 항목을 삭제했습니다.');await renderData();refreshLearningHint();}catch(error){toast(error.message,true);} }

async function renderMemory() {
  const memories=await api('/api/memories');$('#view-memory').innerHTML=`<div class="page-head"><div><p class="eyebrow">PRIVATE CONTEXT</p><h1>장기 기억</h1><p>저장한 메모는 본인 계정의 대화에만 관련 검색으로 연결됩니다.</p></div></div><div class="memory-layout"><div class="panel"><div class="panel-head"><h2>기억 추가</h2><span>사용자 전용</span></div><form id="memory-form" class="data-form"><label class="field-label">메모<textarea id="memory-content" required maxlength="2000" placeholder="예: 저는 답변을 간결하고 한국어로 받는 것을 선호합니다."></textarea></label><label class="field-label">분류<input id="memory-category" value="일반" maxlength="40"></label><button class="primary" type="submit">기억 저장</button></form><div style="height:17px"></div><p class="memory-explainer">기억은 대화 전체를 프롬프트에 붙이지 않고, 입력과 단어가 겹치는 메모 최대 3개를 선택해 모델 입력에 더합니다. 현재 관련도 계산은 로컬 키워드 일치 방식이며 임베딩 검색은 사용하지 않습니다.</p></div><div class="panel"><div class="panel-head"><h2>저장된 기억</h2><span>${memories.length}개</span></div><div class="memory-list">${memories.length?memories.map(m=>`<div class="memory-row"><div class="memory-content">${esc(m.content)}<small>${esc(m.category)} · ${fmtDate(m.updated_at)}</small></div><div class="memory-actions"><button class="edit-memory" data-id="${m.id}" title="수정">✎</button><button class="remove-memory" data-id="${m.id}" title="삭제">×</button></div></div>`).join(''):'<div class="empty-state">저장된 기억이 없습니다.</div>'}</div></div></div>`;
  $('#memory-form').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/memories',{method:'POST',body:JSON.stringify({content:$('#memory-content').value,category:$('#memory-category').value||'general'})});toast('기억을 저장했습니다.');await renderMemory();}catch(error){toast(error.message,true);}});
  $$('.remove-memory').forEach(b=>b.addEventListener('click',async()=>{try{await api(`/api/memories/${b.dataset.id}`,{method:'DELETE'});await renderMemory();toast('기억을 삭제했습니다.');}catch(error){toast(error.message,true);}}));
  $$('.edit-memory').forEach(b=>b.addEventListener('click',async()=>{const memory=memories.find(m=>m.id===Number(b.dataset.id));const content=prompt('기억 내용을 수정하세요.',memory.content);if(content===null)return;const category=prompt('분류',memory.category)||'general';try{await api(`/api/memories/${memory.id}`,{method:'PUT',body:JSON.stringify({content,category})});await renderMemory();toast('기억을 수정했습니다.');}catch(error){toast(error.message,true);}}));
}

$('#profile-button').addEventListener('click',()=>{
  $('#modal-content').innerHTML=`<h2>계정 설정</h2><p>${esc(state.user.email)} · ${state.user.role==='admin'?'관리자':'일반 사용자'}</p><div class="version-list"><button class="secondary full" id="logout-button">로그아웃</button><button class="danger-button full" id="delete-data-button">내 대화·기억·학습 데이터 삭제</button><button class="danger-button full" id="delete-account-button">계정 삭제</button></div><p>이미 학습된 모델 가중치에서 특정 데이터의 영향을 정확히 제거하려면 남은 승인 데이터로 재학습해야 합니다.</p><div class="modal-actions"><button class="secondary" data-close-modal>닫기</button></div>`;$('#modal').showModal();$('[data-close-modal]').onclick=()=>$('#modal').close();$('#logout-button').onclick=()=>{$('#modal').close();logout();};$('#delete-data-button').onclick=async()=>{if(!confirm('내 대화, 기억, 학습 데이터 항목을 삭제할까요?'))return;try{await api('/api/account/data',{method:'DELETE'});$('#modal').close();toast('개인 데이터를 삭제했습니다.');if(state.view==='data')renderData();if(state.view==='memory')renderMemory();refreshLearningHint();}catch(error){toast(error.message,true);}};$('#delete-account-button').onclick=async()=>{if(!confirm('계정을 영구 삭제할까요? 이 작업은 되돌릴 수 없습니다.'))return;try{await api('/api/account',{method:'DELETE'});$('#modal').close();logout(false);}catch(error){toast(error.message,true);}};
});
$('#help-button').addEventListener('click',()=>{ $('#modal-content').innerHTML=`<h2>이 서비스 안내</h2><p>채팅은 이 서버의 PyTorch Transformer가 직접 생성합니다. 외부 AI 추론 API 연결은 없습니다. 초기 모델은 무작위 가중치로 시작하므로 학습 전 응답은 의미가 없을 수 있습니다.</p><p>학습은 사용자가 동의하고 관리자가 승인한 텍스트만 사용합니다. 후보 모델이 고정된 검증 데이터에서 기존 모델보다 낮은 손실을 보일 때에만 새 버전을 활성화합니다.</p><p>브라우저 기록, 기억, 데이터는 SQLite에 계정별로 분리 저장합니다. 대화 자체는 별도 승인 없이는 학습에 들어가지 않습니다.</p><div class="modal-actions"><button class="primary" data-close-modal>확인</button></div>`;$('#modal').showModal();$('[data-close-modal]').onclick=()=>$('#modal').close(); });

function closeMobileMenu(){ $('#sidebar').classList.remove('open');$('#mobile-scrim').classList.remove('open'); }
$('#menu-button').addEventListener('click',()=>{$('#sidebar').classList.add('open');$('#mobile-scrim').classList.add('open');});
$('#sidebar-close').addEventListener('click',closeMobileMenu);$('#mobile-scrim').addEventListener('click',closeMobileMenu);
$('#theme-toggle').addEventListener('click',()=>{document.body.classList.toggle('dark');localStorage.setItem('localmind.theme',document.body.classList.contains('dark')?'dark':'light');if(state.view==='learning')renderLearning();});
if(localStorage.getItem('localmind.theme')==='dark')document.body.classList.add('dark');
$('#new-chat').addEventListener('keydown',event=>{if((event.metaKey||event.ctrlKey)&&event.key==='k'){event.preventDefault();createConversation();}});
document.addEventListener('keydown',event=>{if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k'){event.preventDefault();createConversation();}});

$('#google-login').addEventListener('click',beginGoogleAuth);
function beginGoogleAuth(){
  if($('#google-login').disabled){$('#auth-error').textContent='Google 로그인을 사용할 수 없습니다. 배포 환경의 OAuth 설정을 확인해 주세요.';return;}
  window.location.assign('/api/auth/google');
}
async function setupGoogleLogin(){
  try {
    const status=await api('/api/auth/google/status');
    $('#google-login').disabled=!status.enabled;
    $('#google-auth-note').classList.toggle('hidden',status.enabled || state.authMode!=='login');
    if(!status.enabled) $('#google-auth-note').textContent='Google 로그인을 사용할 수 없습니다. 배포 환경의 OAuth 설정을 확인해 주세요.';
  } catch { $('#google-login').disabled=true; }
}
(async function init(){
  const params=new URLSearchParams(location.search);
  setAuthMode('login');
  if(params.has('google_signup')) {
    setAuthMode('google-register');
    try {
      const registration=await api('/api/auth/google/registration/status');
      $('#google-identity-email').textContent=registration.email;
      if(registration.display_name) $('#auth-name').value=registration.display_name;
    } catch {
      setAuthMode('login');
      $('#auth-error').textContent='Google 가입 단계가 만료됐습니다. Google로 가입하기를 다시 눌러 주세요.';
    }
    history.replaceState({},'',location.pathname);
    setupGoogleLogin();
    showAuth();
    return;
  }
  if(params.has('auth_error')){
    $('#auth-error').textContent='Google 로그인에 실패했습니다. Google OAuth 도메인과 리디렉션 주소를 확인해 주세요.';
    history.replaceState({},'',location.pathname);
  }
  setupGoogleLogin();
  try{await enterApp();}catch{if(state.token)logout(false);else showAuth();}
})();
