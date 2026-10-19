const API_BASE = (new URLSearchParams(location.search).get('api') || 'http://localhost:8000').replace(/\/$/, '');
const $ = (selector) => document.querySelector(selector);
const els = {company:$('#company-filter'),period:$('#period-filter'),form:$('#ask-form'),question:$('#question'),send:$('#send-button'),welcome:$('#welcome'),conversation:$('#conversation'),history:$('#history-list'),filings:$('#filing-grid'),filingCount:$('#filing-count'),filingTotal:$('#filing-total'),health:$('.api-status'),healthText:$('#health-text'),toast:$('#toast')};
let sessionId = null, allFilings = [], toastTimer;

// What to show under each answer. Set any of these to false to hide that part.
const SHOW = {
  sourceList: true,      // the SOURCES list: one link per filing table or page the answer used
  figureChips: true,     // the small boxes under it: "Research and development · 2025 -> 15,144 millions"
  sourcesInText: false,  // the "Sources:" list the model itself writes at the end of its answer (duplicates SOURCES)
};

async function api(path, options={}) {
  const response = await fetch(`${API_BASE}${path}`, {headers:{'Content-Type':'application/json',...(options.headers||{})},...options});
  if(!response.ok){let detail=`Request failed (${response.status})`;try{const body=await response.json();detail=body.detail||detail}catch{}throw new Error(detail)}
  return response.json();
}
function esc(value=''){return String(value).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]))}
function toast(message){els.toast.textContent=message;els.toast.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>els.toast.classList.remove('show'),3200)}
function setHealth(ok){els.health.classList.toggle('online',ok);els.health.classList.toggle('offline',!ok);els.healthText.textContent=ok?'Backend connected':'Backend unavailable'}
function humanDate(value){if(!value)return 'Filing date unavailable';const d=new Date(value);return Number.isNaN(+d)?String(value):new Intl.DateTimeFormat(undefined,{year:'numeric',month:'short',day:'numeric'}).format(d)}
function safeMarkdown(text=''){
  let out=esc(text);
  out=out.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/^\s*[-*]\s+(.+)$/gm,'• $1');
  return out;
}

// scroll=false lets a refresh redraw the chat without jumping the page around
function setActiveConversation(active, scroll=true){
  els.welcome.classList.toggle('hidden',active);
  els.conversation.classList.toggle('hidden',!active);
  if(active && scroll) $('#composer-area').scrollIntoView({block:'end',behavior:'smooth'});
}
function resetChat(){sessionId=null;els.conversation.replaceChildren();setActiveConversation(false);els.question.focus();markSelected()}
function fileUrl(filingId){return `${API_BASE}/api/pdf/${encodeURIComponent(filingId)}`}
function renderFilingCards(){const list=allFilings.slice(0,12);els.filingCount.textContent=allFilings.length;els.filingTotal.textContent=`${allFilings.length} reports available`;els.filings.innerHTML=list.length?list.map(f=>`<article class="filing-card"><div class="filing-card-top"><span class="ticker-pill">${esc(f.ticker||'—')}</span><span class="form-pill">${esc(f.form||'10-Q')}</span></div><div class="filing-company">${esc(f.company||f.ticker||'Company')}</div><div class="filing-period">${esc(f.fiscal_period||'Period unavailable')}</div><div class="filing-date">PERIOD END · ${esc(humanDate(f.period_end))}</div><div class="filing-actions"><a href="${esc(fileUrl(f.filing_id))}" target="_blank" rel="noopener">↗ View filing PDF</a></div></article>`).join(''):'<div class="loading-card">No filings found. Check that the backend has been initialized.</div>'}
function fillFilters(){const companies=[...new Map(allFilings.map(f=>[f.ticker,{ticker:f.ticker,company:f.company}])).values()];els.company.innerHTML='<option value="">All companies</option>'+companies.map(c=>`<option value="${esc(c.ticker)}">${esc(c.company)} · ${esc(c.ticker)}</option>`).join('');fillPeriods()}
function fillPeriods(){const ticker=els.company.value;const periods=[...new Set(allFilings.filter(f=>!ticker||f.ticker===ticker).map(f=>f.fiscal_period).filter(Boolean))];els.period.innerHTML='<option value="">Any period</option>'+periods.map(p=>`<option value="${esc(p)}">${esc(p)}</option>`).join('')}
async function loadFilings(){try{allFilings=await api('/api/filings');fillFilters();renderFilingCards()}catch(err){els.filingTotal.textContent='Unable to load';els.filings.innerHTML=`<div class="loading-card">${esc(err.message)}. Start the backend and refresh.</div>`}}
async function checkHealth(){try{await api('/api/health');setHealth(true)}catch{setHealth(false)}}

// ---------------------------------------------------------
// Chat history (sidebar list + the open conversation)
// ---------------------------------------------------------

// Highlight the open chat in the sidebar. Called after every redraw of the list.
function markSelected(){
  document.querySelectorAll('.history-item').forEach(b=>b.classList.toggle('selected', b.dataset.session===sessionId));
}

// Re-reads the list of chats from the backend. Returns true when it worked.
// keepOnError: leave the current list in place if the backend cannot be reached (used by the refresh button).
async function loadHistory({keepOnError=false}={}){
  try{
    const chats=await api('/api/sessions');
    els.history.innerHTML=chats.length
      ? chats.slice(0,30).map(c=>`<div class="history-row"><button class="history-item" data-session="${esc(c.session_id)}" title="${esc(c.title)}">${esc(c.title||'Untitled research')}</button><button class="history-delete" data-delete="${esc(c.session_id)}" title="Delete this chat" aria-label="Delete this chat">✕</button></div>`).join('')
      : '<div class="side-muted">Your conversations will appear here.</div>';
    els.history.querySelectorAll('[data-session]').forEach(b=>b.addEventListener('click',()=>openSession(b.dataset.session)));
    els.history.querySelectorAll('[data-delete]').forEach(b=>b.addEventListener('click',e=>{e.stopPropagation();deleteChat(b.dataset.delete)}));
    markSelected();
    return true;
  }catch{
    if(!keepOnError) els.history.innerHTML='<div class="side-muted">Chat history unavailable.</div>';
    return false;
  }
}

// Draws every turn of a chat. scroll=false keeps the page where it is.
function renderSession(data, scroll=true){
  els.conversation.replaceChildren();
  setActiveConversation(true, scroll);
  (data.turns||[]).forEach(turn=>appendTurn(turn.question,turn,false,scroll));
}

async function openSession(id){
  try{
    const data=await api(`/api/sessions/${encodeURIComponent(id)}`);
    sessionId=id;
    renderSession(data);
    markSelected();
    els.conversation.scrollIntoView({block:'start',behavior:'smooth'});
  }catch(err){toast(err.message)}
}

// The refresh button: reload the list AND the chat that is currently open, straight from the backend.
async function refreshHistory(){
  const button=$('#refresh-history');
  if(button.disabled) return;
  button.disabled=true;
  const spin=button.animate([{transform:'rotate(0deg)'},{transform:'rotate(360deg)'}],{duration:700,iterations:Infinity});
  try{
    const listed=await loadHistory({keepOnError:true});
    if(!listed){setHealth(false);toast('Could not reach the backend. History not refreshed.');return}
    setHealth(true);
    // Reload the open chat too, unless a question is still being answered (that would wipe its loading card).
    if(sessionId && !els.send.disabled){
      try{
        renderSession(await api(`/api/sessions/${encodeURIComponent(sessionId)}`), false);
      }catch(err){
        if(/not found/i.test(err.message)){resetChat();toast('That chat no longer exists.');return}
        throw err;
      }
    }
    toast('History refreshed');
  }catch(err){
    toast(err.message);
  }finally{
    spin.cancel();
    button.disabled=false;
  }
}

// Delete one chat for good (removes it from the database on the backend).
async function deleteChat(id){
  if(!window.confirm('Delete this chat? This cannot be undone.')) return;
  try{
    await api(`/api/sessions/${encodeURIComponent(id)}`,{method:'DELETE'});
    if(id===sessionId) resetChat();
    await loadHistory({keepOnError:true});
    toast('Chat deleted');
  }catch(err){toast(err.message)}
}

// Delete every chat. The backend deletes one chat per call, so we loop over the full list.
async function clearAllHistory(){
  let chats;
  try{chats=await api('/api/sessions')}catch(err){toast(err.message);return}
  if(!chats.length){toast('There are no chats to delete.');return}
  if(!window.confirm(`Delete all ${chats.length} chats? This cannot be undone.`)) return;
  const results=await Promise.allSettled(chats.map(c=>api(`/api/sessions/${encodeURIComponent(c.session_id)}`,{method:'DELETE'})));
  const failed=results.filter(r=>r.status==='rejected').length;
  if(chats.some(c=>c.session_id===sessionId) && failed===0) resetChat();
  await loadHistory({keepOnError:true});
  toast(failed?`Deleted ${chats.length-failed} chats, ${failed} failed.`:'All chats deleted');
}

// Styles and the "delete all" button are added here so no change to styles.css or index.html is needed.
function setupHistoryControls(){
  const style=document.createElement('style');
  style.textContent=`.history-row{display:flex;align-items:center;gap:2px}.history-row .history-item{flex:1;min-width:0}`+
    `.history-delete,.history-clear{border:0;background:none;color:inherit;cursor:pointer;border-radius:6px;padding:4px 8px;font:inherit}`+
    `.history-delete{opacity:0}.history-row:hover .history-delete,.history-delete:focus-visible{opacity:.65}`+
    `.history-delete:hover,.history-clear:hover{opacity:1;color:#b3261e}.history-clear{opacity:.7;font-size:12px}`;
  document.head.append(style);
  if(!$('#clear-history')){
    const button=document.createElement('button');
    button.id='clear-history';button.className='history-clear';button.type='button';
    button.title='Delete all chats';button.setAttribute('aria-label','Delete all chats');button.textContent='Clear all';
    $('#refresh-history').after(button);
  }
  $('#clear-history').addEventListener('click',clearAllHistory);
}

function appendTurn(question,result,loading=false,scroll=true){const turn=document.createElement('article');turn.className='turn';turn.innerHTML=`<div class="user-question"><span class="user-bubble">A</span><span>${esc(question)}</span></div><div class="answer-block">${loading?'<div class="loading-turn">Searching filings and preparing a cited answer…</div>':renderAnswer(result)}</div>`;els.conversation.append(turn);if(scroll)turn.scrollIntoView({block:'nearest',behavior:'smooth'});return turn}
// Removes the "Sources:" list the model writes at the end of its text. The structured SOURCES block replaces it.
function stripModelSources(text){
  const at=text.search(/^\s*(?:#+\s*)?\**sources\**:?\**\s*$/im);
  return at>0 ? text.slice(0,at).trimEnd() : text;
}
function renderAnswer(result={}){
  let answer=result.answer||result.message||'The agent returned no answer.';
  if(!SHOW.sourcesInText) answer=stripModelSources(answer)||answer;
  let html=`<div class="answer-label">LEDGER RESEARCH</div><div class="answer-copy">${safeMarkdown(answer)}</div>`;
  if(result.status==='insufficient_evidence')html+='<div class="answer-state">Not enough supporting evidence was found in the indexed filings.</div>';

  const citations=result.citations||[];
  if(SHOW.sourceList && citations.length){
    html+='<div class="evidence-label">SOURCES</div><div class="citation-list">'+citations.map(c=>{
      const label=c.citation||'Filing source';
      const href=c.table_id?`${API_BASE}/api/table/${encodeURIComponent(c.table_id)}`:c.filing_id?fileUrl(c.filing_id):null;
      return href
        ? `<a class="citation" href="${esc(href)}" target="_blank" rel="noopener"><span class="citation-icon">↗</span>${esc(label)}${c.table_id?' · View table':''}</a>`
        : `<span class="citation"><span class="citation-icon">◈</span>${esc(label)}</span>`;
    }).join('')+'</div>';
  }

  if(SHOW.figureChips && (result.figures||[]).length){
    const unique=[];const seen=new Set();
    for(const f of result.figures){const key=`${f.item}|${f.column}|${f.value}`;if(!seen.has(key)){seen.add(key);unique.push(f)}}
    html+='<div class="figure-list">'+unique.slice(0,6).map(f=>`<div class="figure-chip">${esc(f.item)} · ${esc(f.column)}<b>${esc(f.value)} ${esc(f.units||'')}</b></div>`).join('')+'</div>';
  }
  return html;
}
async function submitQuestion(value){const question=(value??els.question.value).trim();if(!question||els.send.disabled)return;setActiveConversation(true);const turn=appendTurn(question,{},true);els.question.value='';resizeInput();els.send.disabled=true;try{const result=await api('/api/ask',{method:'POST',body:JSON.stringify({question,session_id:sessionId,ticker:els.company.value,fiscal_period:els.period.value})});sessionId=result.session_id;turn.querySelector('.answer-block').innerHTML=renderAnswer(result);await loadHistory()}catch(err){turn.querySelector('.answer-block').innerHTML=`<div class="answer-state">${esc(err.message)}. Check that the API is running at ${esc(API_BASE)}.</div>`;setHealth(false)}finally{els.send.disabled=false;els.question.focus()}}
function resizeInput(){els.question.style.height='auto';els.question.style.height=`${Math.min(els.question.scrollHeight,130)}px`}
els.form.addEventListener('submit',e=>{e.preventDefault();submitQuestion()});els.question.addEventListener('input',resizeInput);els.question.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();submitQuestion()}});$('#new-chat').addEventListener('click',resetChat);$('#refresh-history').addEventListener('click',refreshHistory);els.company.addEventListener('change',fillPeriods);document.querySelectorAll('.suggestion').forEach(b=>b.addEventListener('click',()=>submitQuestion(b.dataset.question)));$('#help-button')?.addEventListener('click',()=>toast('Ask about indexed 10-Q filings. Answers include source citations when available.'));
setupHistoryControls();
Promise.all([checkHealth(),loadFilings(),loadHistory()]);setInterval(checkHealth,30000);