'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="local-token"]').content;
const examples = ['will-jd-vance-win-the-2028-us-presidential-election','will-gavin-newsom-win-the-2028-us-presidential-election'];
const labels = {
  starting:'마켓 확인 중', connecting:'실시간 연결 중', waiting_for_snapshot:'초기 호가 수신 대기',
  receiving:'실시간 수집 중', reconnecting:'연결 복구 중', stopping:'받은 데이터를 저장하는 중',
  processing:'CSV·분석 생성 중', complete:'수집·파일 생성 완료', stopped:'중지·파일 저장 완료', failed:'수집을 확인해 주세요'
};
let state = null, selectedJob = null, serverReady = false, actionBusy = false, searching = false;
let pollBusy = false, historyKey = '', fileKey = '', finishScroll = null;
try { $('targets').value = localStorage.getItem('orderbook.targets') || ''; } catch {}
const targets = () => [...new Set($('targets').value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean))];
const number = value => Number(value || 0).toLocaleString('ko-KR');
function elapsed(seconds) {
  const s = Math.max(0,Math.floor(Number(seconds)||0));
  return s >= 3600 ? `${Math.floor(s/3600)}:${String(Math.floor(s%3600/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`
    : `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;
}
function bytes(size) {
  return size >= 1048576 ? `${(size/1048576).toFixed(1)} MB` : size >= 1024 ? `${(size/1024).toFixed(1)} KB` : `${size} B`;
}
function showNotice(message) {
  $('notice').textContent = message;
  $('notice').classList.toggle('hidden',!message);
}
async function api(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(()=>controller.abort(),18000);
  try {
    const response = await fetch(path,{...options, signal:controller.signal,
      headers:{'X-Local-Token':token,...(options.body ? {'Content-Type':'application/json'} : {})}});
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(result.error || '요청을 처리하지 못했습니다.');
      error.status = response.status;
      throw error;
    }
    return result;
  } finally { clearTimeout(timeout); }
}
function updateControls() {
  const active = Boolean(state?.active_job_id), busy = active || actionBusy;
  $('start-button').disabled = !serverReady || busy || targets().length === 0;
  $('start-button').textContent = active ? '수집 진행 중' : actionBusy ? '시작 중…' : '▶ 수집 시작';
  const current = state?.current;
  $('stop-button').disabled = !serverReady || actionBusy || !active || !current ||
    current.id !== state.active_job_id || ['processing','stopping'].includes(current.state);
  for (const id of ['targets','minutes','latency','fee','example-button','search-input']) $(id).disabled = busy;
  $('search-button').disabled = busy || searching;
  document.querySelectorAll('[data-minutes]').forEach(button=>button.disabled=busy);
  document.querySelectorAll('[data-query]').forEach(button=>button.disabled=busy||searching);
  document.querySelectorAll('.search-result button').forEach(button=>{
    const added = targets().includes(button.dataset.slug);
    button.disabled = busy || added;
    button.textContent = added ? '추가됨' : '추가';
  });
  $('history').disabled = active || actionBusy || !state?.jobs?.length;
  $('target-count').textContent = `${targets().length}개`;
}
function saveTargets() {
  try { localStorage.setItem('orderbook.targets',$('targets').value); } catch {}
  updateControls();
}
function setMinutes(minutes) {
  $('minutes').value = minutes;
  updateMinutes();
}
function updateMinutes() {
  const value = Number($('minutes').value);
  document.querySelectorAll('[data-minutes]').forEach(button=>{
    const chosen = Number(button.dataset.minutes)===value;
    button.classList.toggle('selected',chosen);
    button.setAttribute('aria-pressed',String(chosen));
  });
  $('duration-note').textContent = value === .25 ? '15초 실제 수신·CSV 생성 시험입니다. 전략을 검증할 표본은 아닙니다.' : '수신 메시지는 도착할 때마다 저장합니다.';
}
async function search(query) {
  if (searching || state?.active_job_id) return;
  if (!query.trim()) { $('search-status').textContent='영어 검색어를 넣어 주세요. 예: 2028 presidential election'; return; }
  searching = true;
  updateControls();
  $('search-status').textContent = '활성 마켓을 찾고 있습니다…';
  $('search-results').replaceChildren();
  $('search-results').classList.add('hidden');
  try {
    const data = await api('/api/search?q='+encodeURIComponent(query));
    for (const market of data.markets) {
      const row = document.createElement('div'); row.className='search-result';
      const text = document.createElement('div');
      const title = document.createElement('p'); title.textContent=market.question;
      const subtitle = document.createElement('small'); subtitle.textContent=market.event_title;
      text.append(title,subtitle);
      const button = document.createElement('button'); button.type='button'; button.dataset.slug=market.slug;
      button.addEventListener('click',()=>{
        $('targets').value=[...new Set([...targets(),market.slug])].join('\n');
        saveTargets();
      });
      row.append(text,button); $('search-results').append(row);
    }
    $('search-results').classList.toggle('hidden',data.markets.length===0);
    $('search-status').textContent = data.markets.length ? `${data.markets.length}개 질문을 찾았습니다. 추가할 질문을 선택하세요.` : '활성 Yes/No 질문을 찾지 못했습니다. 다른 영어 검색어나 링크를 사용해 주세요.';
  } catch (error) { $('search-status').textContent=error.name==='AbortError' ? '검색 연결이 지연됩니다. 링크를 직접 넣어도 됩니다.' : error.message; }
  finally { searching=false; updateControls(); }
}
function renderHistory() {
  const key=JSON.stringify(state.jobs);
  if(key !== historyKey) {
    historyKey=key;
    $('history').replaceChildren();
    if(!state.jobs.length) $('history').append(new Option('아직 수집 기록이 없습니다',''));
    for(const job of state.jobs) {
      const date = new Date(job.started_at);
      const title = date.toLocaleString('ko-KR',{timeZone:'Asia/Hong_Kong',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});
      $('history').append(new Option(`${title} HKT${job.finished?'':' · 진행 중'}`,job.id));
    }
  }
  if(state.current) $('history').value=state.current.id;
}
function fileLabel(name) {
  if(name.includes('_quotes_')) return '호가 CSV';
  if(name.includes('_trades_')) return '체결 CSV';
  return {'analysis/summary.csv':'분석 요약 CSV','analysis/failed_signals.csv':'보합·반대 사례 CSV',
    'analysis/observations.csv':'전체 분석 관측 CSV','events.jsonl':'원본 메시지',
    'metadata.json':'마켓 정보','status.json':'수집 상태','run_summary.json':'실행 결과',
    'console.log':'진행 로그','analysis/settings.json':'분석 설정','analysis/replay_audit.json':'호가 복원 검사'}[name] || name;
}
function renderFiles(job) {
  const files=job.files.filter(file=>file.size>0);
  const key=JSON.stringify([job.id,files]);
  if(key===fileKey) return;
  fileKey=key;
  $('files').replaceChildren();
  if(!files.length) {
    const empty=document.createElement('div'); empty.className='empty-files';
    empty.textContent='받은 데이터를 저장하면 여기에 파일이 표시됩니다.'; $('files').append(empty); return;
  }
  const rank=name=>name.includes('_quotes_')?0:name.includes('_trades_')?1:name==='analysis/summary.csv'?2:name==='analysis/failed_signals.csv'?3:10;
  files.sort((a,b)=>rank(a.name)-rank(b.name));
  for(const file of files) {
    const link=document.createElement('a'); link.className='file-link';
    link.href=`/api/download?job=${encodeURIComponent(job.id)}&file=${encodeURIComponent(file.name)}`;
    link.setAttribute('download','');
    const label=document.createElement('span'); label.textContent=fileLabel(file.name);
    const size=document.createElement('small'); size.textContent=bytes(file.size);
    const icon=document.createElement('span'); icon.textContent='↓'; icon.setAttribute('aria-hidden','true');
    link.append(label,size,icon); $('files').append(link);
  }
}
function renderAnalysis(job) {
  const rows=job.preview.filter(row=>String(row.horizon_ms)===$('horizon').value);
  const hasAnalysis=job.preview.length>0;
  $('analysis').classList.toggle('hidden',!hasAnalysis);
  $('analysis-empty').classList.toggle('hidden',hasAnalysis);
  $('analysis-empty').textContent=job.summary.analysis_status==='no_usable_windows' ?
    '수집은 됐지만 분석할 우세 신호·비교 구간이 부족합니다. 더 긴 수집이나 다른 활성 질문이 필요합니다.' :
    job.summary.analysis_status==='failed' ? '분석 처리에 실패했습니다. 진행 로그에서 원인을 확인해 주세요.' :
    'CSV와 분석은 수집 종료 후 생성됩니다. 짧은 연결 시험만으로 전략을 판단하지 마세요.';
  $('analysis-body').replaceChildren();
  const questions=new Map((job.metadata.markets||[]).map(m=>[m.slug,m.question]));
  if(hasAnalysis && !rows.length) {
    const row=document.createElement('tr'), cell=document.createElement('td'); cell.colSpan=6;
    cell.textContent='이 비교 시점에는 유효한 관측이 없습니다.'; row.append(cell); $('analysis-body').append(row);
  }
  for(const item of rows) {
    const row=document.createElement('tr'), first=document.createElement('td');
    const title=document.createElement('div'); title.textContent=questions.get(item.market)||item.market;
    const signal=document.createElement('span'); signal.className='signal-tag'+(item.signal==='sell_pressure'?' sell':'');
    signal.textContent=item.signal==='buy_pressure'?'매수 우세':item.signal==='sell_pressure'?'매도 우세':'중립';
    first.append(title,signal); row.append(first);
    for(const key of ['observations','expected','flat','opposite']) {
      const cell=document.createElement('td'); cell.textContent=number(item[key]); row.append(cell);
    }
    const money=document.createElement('td');
    const value=item.mean_net_cents;
    money.textContent=value===''||value===undefined ? '—' : `${Number(value)>0?'+':''}${Number(value).toFixed(3)}¢`;
    money.className=Number(value)<0?'money-negative':Number(value)>0?'money-positive':'';
    row.append(money); $('analysis-body').append(row);
  }
}
function renderJob() {
  const job=state.current;
  $('idle-state').classList.toggle('hidden',Boolean(job));
  $('job-state').classList.toggle('hidden',!job);
  if(!job) return;
  const capture=job.capture, summary=job.summary, final=Boolean(job.finished_at);
  $('phase-badge').textContent=labels[job.state]||job.state;
  $('phase-badge').className='badge '+(job.state==='failed'?'error':final?'success':'active');
  $('phase-title').textContent=labels[job.state]||job.state;
  $('phase-description').textContent=job.state==='starting'?'선택한 마켓의 활성 상태를 확인합니다.':
    job.state==='processing'?'CSV와 분석을 만드는 중입니다. 잠시 기다려 주세요.':
    job.state==='stopping'?'받은 원본을 보관하고 파일 생성을 마칩니다.':
    job.state==='failed'?'마켓·연결 상태를 확인하세요. 아래 진행 로그에 오류가 표시됩니다.':
    final?'결과 파일을 내려받거나 새로운 수집을 시작할 수 있습니다.':'서버가 보내는 메시지를 도착할 때마다 저장합니다.';
  $('live-dot').classList.toggle('running',!final);
  $('message-count').textContent=number(capture.messages);
  $('useful-count').textContent=`호가·체결 ${number(capture.useful_messages)}개`;
  $('snapshot-count').textContent=`${capture.snapshot_tokens||0} / ${capture.expected_tokens||'—'}`;
  $('elapsed').textContent=elapsed(capture.elapsed_seconds);
  $('last-message').textContent=capture.last_message_at ? `최근 수신 ${Math.max(0,Math.floor((Date.now()-Date.parse(capture.last_message_at))/1000))}초 전` : '수신 대기';
  const ratio=Math.min(100,Math.max(0,(Number(capture.elapsed_seconds)||0)/(job.config.minutes*60)*100));
  $('progress-fill').style.width=`${job.state==='complete'?100:ratio}%`;
  $('duration-label').textContent=`목표 ${job.config.minutes===.25?'15초':job.config.minutes+'분'}`;
  $('progress-label').textContent=final?(job.state==='failed'?'완료 조건 미충족':'파일 저장 완료'):labels[job.state]||'진행 중';
  $('observed-markets').replaceChildren();
  const markets=job.metadata.markets||job.config.targets.map(question=>({question}));
  for(const market of markets) { const li=document.createElement('li'); li.textContent=market.question; $('observed-markets').append(li); }
  let warning='';
  if(job.state==='failed') warning='정상 완료로 처리되지 않았습니다. 받은 원본이 있다면 보관되어 있습니다. 진행 로그와 실행 결과를 확인해 주세요.';
  else if(summary.analysis_status==='no_usable_windows') warning='수집은 완료됐지만 분석할 신호·비교 구간이 부족합니다.';
  else if(capture.last_error && !final) warning='연결 문제를 감지했습니다. 자동으로 복구를 시도합니다. '+capture.last_error;
  $('job-warning').textContent=warning; $('job-warning').classList.toggle('hidden',!warning);
  const output=$('log-output'), log=job.logs.join('\n')||'마켓을 확인하는 중입니다…';
  const atBottom=output.scrollHeight-output.scrollTop-output.clientHeight<45;
  if(output.textContent!==log) { output.textContent=log; if(atBottom) output.scrollTop=output.scrollHeight; }
  renderFiles(job); renderAnalysis(job);
  if(final && finishScroll===job.id) {
    finishScroll=null;
    $('results').scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});
  }
}
async function poll() {
  if(pollBusy) return;
  pollBusy=true;
  try {
    state=await api('/api/state'+(selectedJob?'?job='+encodeURIComponent(selectedJob):''));
    if(state.active_job_id && selectedJob!==state.active_job_id) {
      selectedJob=state.active_job_id;
      state=await api('/api/state?job='+encodeURIComponent(selectedJob));
    }
    serverReady=true;
    $('server-status').className='connection online';
    $('server-status').replaceChildren();
    const dot=document.createElement('i'); $('server-status').append(dot,'화면 연결됨');
    renderHistory(); renderJob();
  } catch(error) {
    if(error.status===404 && selectedJob) selectedJob=null;
    else {
      serverReady=false;
      $('server-status').className='connection offline';
      $('server-status').textContent='화면 서버 연결 끊김';
    }
  } finally { pollBusy=false; updateControls(); }
}
$('targets').addEventListener('input',saveTargets);
$('minutes').addEventListener('input',updateMinutes);
document.querySelectorAll('[data-minutes]').forEach(button=>button.addEventListener('click',()=>setMinutes(Number(button.dataset.minutes))));
document.querySelectorAll('[data-query]').forEach(button=>button.addEventListener('click',()=>{
  $('search-input').value=button.dataset.query; search(button.dataset.query);
}));
$('search-form').addEventListener('submit',event=>{event.preventDefault();search($('search-input').value);});
$('example-button').addEventListener('click',()=>{
  $('targets').value=examples.join('\n'); setMinutes(.25); saveTargets();
  showNotice('활성 2028 대선 질문 두 개를 연결 시험용으로 넣었습니다. 실제 연구 대상은 직접 선택해 주세요.');
});
$('start-button').addEventListener('click',async()=>{
  if(actionBusy || state?.active_job_id) return;
  actionBusy=true; showNotice(''); updateControls();
  try {
    const result=await api('/api/start',{method:'POST',body:JSON.stringify({targets:targets(),minutes:Number($('minutes').value),latency_ms:Number($('latency').value),fee_cents:$('fee').value})});
    selectedJob=result.job_id; finishScroll=result.job_id; fileKey=''; await poll();
  } catch(error) { showNotice(error.name==='AbortError'?'시작 요청이 지연됩니다. 수집 현황을 확인한 뒤 다시 시도해 주세요.':error.message); }
  finally { actionBusy=false; updateControls(); }
});
$('stop-button').addEventListener('click',async()=>{
  if(actionBusy || !state?.active_job_id) return;
  actionBusy=true; updateControls();
  try { await api('/api/stop',{method:'POST',body:JSON.stringify({job_id:state.active_job_id})}); await poll(); }
  catch(error) { showNotice(error.message); }
  finally { actionBusy=false; updateControls(); }
});
$('history').addEventListener('change',()=>{selectedJob=$('history').value||null;fileKey='';poll();});
$('horizon').addEventListener('change',()=>{if(state?.current)renderAnalysis(state.current);});
updateMinutes(); updateControls(); poll(); setInterval(poll,1000);
