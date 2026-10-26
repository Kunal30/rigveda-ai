"""Local HTTP server and browser UI for Rigveda AI."""

from __future__ import annotations

import hmac
import json
import logging
import re
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import conversation, facts, status, suggest, write
from .config import Config
from .llm import OllamaError, ask
from .search import cite, search
from .store import connect

LOOPBACK = {"127.0.0.1", "localhost", "::1"}

INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rigveda AI</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#121a30;--line:#253250;--text:#edf2ff;--muted:#a7b5d6;--accent:#7c9cff;--user:#263b79}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#182651,var(--bg) 45%);color:var(--text);font:16px system-ui,sans-serif}
main{max-width:860px;margin:auto;min-height:100vh;padding:28px 18px;display:flex;flex-direction:column}header{padding:10px 6px 24px}h1{margin:0;font-size:1.7rem;letter-spacing:-.04em}header p{color:var(--muted);margin:.35rem 0 0}.status{font-size:.8rem;color:var(--muted)}
#messages{flex:1;display:flex;flex-direction:column;gap:16px}.message{max-width:86%;padding:14px 16px;border:1px solid var(--line);border-radius:16px;background:var(--panel);white-space:pre-wrap;line-height:1.5}.message.user{align-self:flex-end;background:var(--user)}.sources{font-size:.78rem;color:var(--muted);margin-top:12px;word-break:break-all}.sources b{color:var(--text)}
form{display:flex;gap:10px;margin-top:22px;padding:10px;border:1px solid var(--line);border-radius:18px;background:rgba(18,26,48,.9);position:sticky;bottom:12px}textarea{font:inherit;color:inherit;background:transparent;border:0;resize:none;outline:0;min-height:44px;max-height:150px;flex:1;padding:10px}button{align-self:end;background:var(--accent);color:#07102d;border:0;border-radius:12px;font-weight:700;padding:12px 16px;cursor:pointer}button:disabled{opacity:.5;cursor:wait}.empty{color:var(--muted);text-align:center;margin-top:14vh}
</style></head><body><main><header><h1>Rigveda AI</h1><p>Private answers from your selected local files. <a href="/facts" style="color:var(--accent)">Your facts profile →</a> · <a href="/status" style="color:var(--accent)">Status</a></p><span class="status" id="status">Checking local index…</span></header><section id="messages"><p class="empty">Ask a question about your files: documents, slides, images, audio and video.</p></section><form id="chat"><textarea id="question" placeholder="Ask about your files…" aria-label="Question" autofocus></textarea><button id="send">Send</button></form></main>
<script>
const messages=document.querySelector('#messages'), form=document.querySelector('#chat'), question=document.querySelector('#question'), send=document.querySelector('#send'), status=document.querySelector('#status');
function sourceList(sources){const box=document.createElement('div');box.className='sources';const title=document.createElement('b');title.textContent='Sources';box.append(title);for(const s of sources){const line=document.createElement('div');line.textContent=s.cite||(s.path+' (chunk '+s.chunk+')');box.append(line)}return box}
function message(text, user, sources=[]){const box=document.createElement('article');box.className='message '+(user?'user':'assistant');box.textContent=text;if(sources.length)box.append(sourceList(sources));messages.querySelector('.empty')?.remove();messages.append(box);box.scrollIntoView({behavior:'smooth',block:'end'});return box}
async function health(){try{const r=await fetch('/api/health');const d=await r.json();status.textContent=d.documents+' indexed file'+(d.documents===1?'':'s')+' · '+d.model}catch{status.textContent='Local server is unavailable'}}
form.addEventListener('submit',async e=>{e.preventDefault();const text=question.value.trim();if(!text||send.disabled)return;message(text,true);question.value='';send.disabled=true;send.textContent='Thinking…';const pending=message('Searching your local index…',false);try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:text})});const d=await r.json();pending.textContent=d.answer||d.error||'No answer returned.';if(d.sources?.length)pending.append(sourceList(d.sources))}catch{pending.textContent='The local server could not complete that request.'}finally{send.disabled=false;send.textContent='Send';question.focus()}});
question.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();form.requestSubmit()}});health();
</script></body></html>"""


FACTS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rigveda AI · Facts</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#121a30;--line:#253250;--text:#edf2ff;--muted:#a7b5d6;--accent:#7c9cff;--ok:#5fd39a;--no:#ff7d7d;--wait:#f6c45a}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#182651,var(--bg) 45%);color:var(--text);font:15px system-ui,sans-serif}
main{max-width:920px;margin:auto;padding:28px 18px 60px}h1{margin:0;font-size:1.7rem;letter-spacing:-.04em}header p{color:var(--muted);margin:.35rem 0 1rem}a{color:var(--accent)}
nav{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 20px}nav button{background:var(--panel);color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:7px 14px;cursor:pointer;font:inherit}nav button.on{color:var(--text);border-color:var(--accent)}
h2{font-size:.85rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:26px 0 8px}
.fact{border:1px solid var(--line);background:var(--panel);border-radius:14px;padding:12px 14px;margin:8px 0;display:grid;grid-template-columns:1fr auto;gap:8px 14px}
.value{font-size:1.05rem;font-weight:600;word-break:break-word}.value input{font:inherit;color:inherit;background:#0b1226;border:1px solid var(--accent);border-radius:8px;padding:4px 8px;width:100%}
.badge{font-size:.72rem;border-radius:999px;padding:2px 8px;margin-left:8px;vertical-align:middle;border:1px solid}.pending{color:var(--wait)}.approved{color:var(--ok)}.rejected{color:var(--no)}
.actions{display:flex;gap:6px;align-items:start;flex-wrap:wrap;justify-content:end}.actions button{font:inherit;font-size:.85rem;border-radius:9px;border:1px solid var(--line);background:#18223f;color:var(--text);padding:6px 10px;cursor:pointer}
.actions .yes{background:var(--ok);color:#06210f;border:0;font-weight:700}.actions .no{color:var(--no)}
.sources{grid-column:1/-1;font-size:.8rem;color:var(--muted)}.sources div{margin-top:4px;word-break:break-all}.sources q{color:var(--text)}
.reveal{font-size:.75rem;margin-left:8px;cursor:pointer;color:var(--accent);background:none;border:0;padding:0}
form{display:flex;gap:8px;flex-wrap:wrap;margin-top:28px;padding:12px;border:1px solid var(--line);border-radius:14px;background:var(--panel)}form select,form input{font:inherit;color:inherit;background:#0b1226;border:1px solid var(--line);border-radius:8px;padding:8px}form input{flex:1;min-width:200px}form button{background:var(--accent);color:#07102d;border:0;border-radius:9px;font-weight:700;padding:8px 14px;cursor:pointer}
.empty{color:var(--muted);margin-top:30px}
</style></head><body><main><header><h1>Your facts profile</h1>
<p>Details found in your files. All of them are suggested in forms; <b>approve</b> one to rank it first, <b>reject</b> one to stop suggesting it. <a href="/">← Ask your files</a> · <a href="/status">Status</a></p></header>
<nav id="tabs"></nav><section id="list"></section>
<form id="add"><select id="key" aria-label="Fact"></select><input id="value" placeholder="Add a fact yourself, e.g. your passport number" aria-label="Value"><button>Add</button></form>
</main><script>
let facts=[],keys={},home='',tab='pending';
const list=document.querySelector('#list'),tabs=document.querySelector('#tabs');
const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e};
const mask=v=>'•'.repeat(Math.max(v.length-4,0))+v.slice(-4);
async function call(url,body){const r=await fetch(url,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const d=await r.json();if(!r.ok)throw new Error(d.error||'Request failed');return d}
async function load(){const d=await call('/api/facts');facts=d.facts;keys=d.keys;home=d.home;render()}
async function decide(f,status,value){try{await call('/api/facts/'+f.id,{status,value});await load()}catch(e){alert(e.message)}}
function render(){
  tabs.replaceChildren();for(const t of ['pending','approved','rejected','all']){const n=t==='all'?facts.length:facts.filter(f=>f.status===t).length;const b=el('button',t===tab?'on':'',t[0].toUpperCase()+t.slice(1)+' ('+n+')');b.onclick=()=>{tab=t;render()};tabs.append(b)}
  list.replaceChildren();const shown=facts.filter(f=>tab==='all'||f.status===tab);
  if(!shown.length){list.append(el('p','empty',tab==='pending'?'Nothing waiting for review. New documents are scanned automatically, or run `rigveda facts scan`.':'No '+tab+' facts.'));return}
  let group='';for(const f of shown){if(f.key!==group){group=f.key;list.append(el('h2','',f.label))}list.append(card(f))}}
function card(f){
  const box=el('article','fact'),left=el('div'),value=el('div','value');let hidden=f.sensitive;
  const text=el('span','',hidden?mask(f.value):f.value);value.append(text);
  if(f.sensitive){const r=el('button','reveal','show');r.onclick=()=>{hidden=!hidden;text.textContent=hidden?mask(f.value):f.value;r.textContent=hidden?'show':'hide'};value.append(r)}
  value.append(el('span','badge '+f.status,f.status));left.append(value);
  const actions=el('div','actions');
  const button=(label,cls,fn)=>{const b=el('button',cls,label);b.onclick=fn;actions.append(b)};
  if(f.status!=='approved')button('Approve','yes',()=>decide(f,'approved'));
  button('Edit','',()=>{const input=el('input');input.value=f.value;value.replaceChildren(input);input.focus();
    actions.replaceChildren();button('Save & approve','yes',()=>decide(f,'approved',input.value));button('Cancel','',render);
    input.onkeydown=e=>{if(e.key==='Enter')decide(f,'approved',input.value);if(e.key==='Escape')render()}});
  if(f.status!=='rejected')button('Reject','no',()=>decide(f,'rejected'));
  if(f.status!=='pending')button('Undo','',()=>decide(f,'pending'));
  const sources=el('div','sources');
  if(!f.sources.length)sources.append(el('div','','Added by you'));else if(f.files>1)sources.append(el('b','','Found in '+f.files+' files'));
  const short=p=>home&&p.startsWith(home+'/')?'~'+p.slice(home.length):p;
  const line=s=>{const d=el('div','',short(s.path)+(s.location?' ('+s.location+')':'')+': ');d.title=s.path;d.append(el('q','',s.evidence));return d};
  f.sources.slice(0,3).forEach(s=>sources.append(line(s)));
  if(f.sources.length>3){const more=el('button','reveal','show '+(f.sources.length-3)+' more');more.onclick=()=>{more.replaceWith(...f.sources.slice(3).map(line))};sources.append(more)}
  box.append(left,actions,sources);return box}
const select=document.querySelector('#key');
document.querySelector('#add').addEventListener('submit',async e=>{e.preventDefault();const v=document.querySelector('#value');if(!v.value.trim())return;
  try{await call('/api/facts',{key:select.value,value:v.value});v.value='';tab='approved';await load()}catch(err){alert(err.message)}});
load().then(()=>{for(const[k,label]of Object.entries(keys))select.append(new Option(label,k))});
</script></body></html>"""


TEST_FORM_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test form</title><style>
body{margin:0;background:#f4f1ec;color:#1d1b2e;font:15px system-ui,sans-serif}main{max-width:720px;margin:auto;padding:28px 18px 60px}
h1{font-size:1.5rem}p{color:#5b5870}fieldset{border:1px solid #d8d2c8;border-radius:12px;margin:18px 0;padding:14px 16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
legend{font-weight:700;padding:0 6px}label{display:flex;flex-direction:column;gap:4px;font-size:.85rem;color:#3a3750}
input,select,textarea,.editor{font:inherit;padding:8px 10px;border:1px solid #c9c2b6;border-radius:8px;background:#fff}textarea,.editor{min-height:110px;grid-column:1/-1}.editor{line-height:1.45}fieldset label.wide{grid-column:1/-1}button{font:inherit;padding:10px 18px;border-radius:10px;border:0;background:#2a2160;color:#fff;font-weight:700}
#result{margin-top:14px;color:#a33}
</style></head><body><main>
<h1>Test form</h1><p>Click into a field: Rigveda suggests a value from the facts in your files. <b>Tab</b> accepts, <b>Esc</b> dismisses. Or use <i>Fill this page</i> in the extension's menu. Nothing here is sent anywhere, and the extension never submits forms.</p>
<form id="form" autocomplete="off">
<fieldset><legend>About you</legend>
<label>First name<input name="fname"></label><label>Last name<input name="lname"></label>
<label>Full legal name<input id="legalName"></label><label>Date of birth<input name="dob" placeholder="MM/DD/YYYY"></label>
<label>Birth date (calendar)<input type="date" name="birth_date"></label><label>Gender<select name="gender"><option value="">Select…</option><option>Female</option><option>Male</option><option>Other</option></select></label>
<label>Nationality<select name="nationality"><option value="">Select…</option><option value="US">United States</option><option value="IN">India</option><option value="CA">Canada</option></select></label>
<label>Place of birth<input name="pob"></label></fieldset>
<fieldset><legend>Contact</legend>
<label>Email<input type="email" name="email"></label><label>Mobile number<input name="mobile"></label></fieldset>
<fieldset><legend>Address</legend>
<label>Street address<input name="address1"></label><label>Apt / Suite<input name="address2"></label>
<label>City<input name="city"></label><label>State<select name="state"><option value="">Select…</option><option value="CA">California</option><option value="OR">Oregon</option><option value="WA">Washington</option></select></label>
<label>ZIP code<input name="zip"></label><label>Country<input name="country"></label></fieldset>
<fieldset><legend>Documents</legend>
<label>Passport number<input name="passportNo"></label><label>Passport expiry date<input name="passport_expiry" placeholder="DD/MM/YYYY"></label>
<label>Driver's license number<input name="dl_number"></label></fieldset>
<fieldset><legend>Writing help</legend>
<p style="grid-column:1/-1;margin:0">Click in a box and press <b>Alt+Shift+R</b> (or right-click → <i>Write with Rigveda</i>). Empty: a draft from your files. With text: the next sentences. <b>Tab</b> inserts.</p>
<label class="wide">Tell us about your most recent role and what you worked on.<textarea name="experience" maxlength="1500"></textarea></label>
<label class="wide"><span id="coverLabel">Cover letter for a software engineering role</span><div class="editor" contenteditable="true" role="textbox" aria-multiline="true" aria-labelledby="coverLabel"></div></label></fieldset>
<fieldset><legend>Never filled</legend>
<label>Password<input type="password" name="password"></label><label>Card number<input name="cardnumber" autocomplete="cc-number"></label>
<label>Father's name<input name="father"></label></fieldset>
<button>Submit</button><div id="result"></div>
</form>
<script>document.querySelector('#form').addEventListener('submit',e=>{e.preventDefault();document.querySelector('#result').textContent='Submitted by you (the extension never submits). Nothing was sent.'})</script>
</main></body></html>"""


WRITE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Write with Rigveda</title><style>
:root{color-scheme:dark;--bg:#14112e;--panel:#1d1940;--line:#3a3266;--text:#f4f1ea;--muted:#b8b0d8;--accent:#f39a33}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,sans-serif}
main{padding:14px 16px;display:flex;flex-direction:column;gap:10px;min-height:100vh}h1{margin:0;font:500 1.25rem "EB Garamond",Georgia,serif}
textarea{font:inherit;color:inherit;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px;resize:vertical;width:100%}
#ask{min-height:70px}#out{min-height:160px;flex:1}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
button{font:inherit;font-weight:700;border:0;border-radius:9px;padding:8px 14px;cursor:pointer;background:var(--accent);color:#1d1300}
button.plain{background:var(--panel);color:var(--text);border:1px solid var(--line)}button:disabled{opacity:.5;cursor:wait}
.note{color:var(--muted);font-size:12px}#sources{color:var(--muted);font-size:12px;word-break:break-all}
</style></head><body><main>
<h1>Write with Rigveda</h1>
<textarea id="ask" placeholder="What should I write? e.g. a three-sentence bio for a conference, or a reply saying I can start in January" autofocus></textarea>
<div class="row"><button id="go">Write</button><span class="note">Ctrl+Enter · from your files, on this computer</span></div>
<textarea id="out" placeholder="Your draft appears here. Edit it, then copy." hidden></textarea>
<div class="row" id="actions" hidden><button id="copy">Copy</button><button class="plain" id="again">Try again</button><span class="note" id="done"></span></div>
<div id="sources"></div>
</main><script>
const $=id=>document.getElementById(id);
async function run(){const instruction=$('ask').value.trim();if(!instruction||$('go').disabled)return;
  $('go').disabled=$('again').disabled=true;$('go').textContent='Writing…';$('done').textContent='';
  try{const r=await fetch('/api/write',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({instruction})});
    const d=await r.json();if(!r.ok)throw new Error(d.error||'Request failed');
    $('out').hidden=$('actions').hidden=false;$('out').value=d.text;
    $('sources').textContent=d.sources.length?'From: '+d.sources.map(s=>s.name).join(', '):'Nothing relevant in your files; this draft is general.';
  }catch(e){$('out').hidden=false;$('out').value='';$('sources').textContent=e.message}
  finally{$('go').disabled=$('again').disabled=false;$('go').textContent='Write'}}
$('go').onclick=run;$('again').onclick=run;
$('ask').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();run()}});
$('copy').onclick=async()=>{try{await navigator.clipboard.writeText($('out').value);$('done').textContent='Copied. Paste it with Ctrl+V.'}
  catch{$('out').select();document.execCommand('copy');$('done').textContent='Copied.'}};
</script></body></html>"""

STATUS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rigveda AI · Status</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#121a30;--line:#253250;--grid:#1f2a45;--text:#edf2ff;--muted:#a7b5d6;--accent:#7c9cff;--bar:#3987e5;--ok:#5fd39a;--no:#ff7d7d;--wait:#f6c45a}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#182651,var(--bg) 45%);color:var(--text);font:15px system-ui,sans-serif}
main{max-width:980px;margin:auto;padding:28px 16px 60px}h1{margin:0;font-size:1.7rem;letter-spacing:-.04em}header p{color:var(--muted);margin:.35rem 0 1rem}a{color:var(--accent)}
h2{font-size:.85rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:28px 0 10px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.tile{border:1px solid var(--line);background:var(--panel);border-radius:14px;padding:14px 16px}.tile .label{color:var(--muted);font-size:.85rem}
.tile .big{font-size:2.6rem;font-weight:700;letter-spacing:-.03em;margin:4px 0 2px;font-variant-numeric:tabular-nums}.tile .detail{color:var(--muted);font-size:.8rem;margin-top:4px}
.state{font-size:1.15rem;font-weight:700;margin-top:6px}.state .icon{margin-right:6px}.ok .icon{color:var(--ok)}.no .icon{color:var(--no)}.wait .icon{color:var(--wait)}.muted .icon{color:var(--muted)}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:10px}
.chart{border:1px solid var(--line);background:var(--panel);border-radius:14px;padding:14px 16px;position:relative;min-width:0}.chart svg{display:block;width:100%;height:auto}.chart h3{margin:0;font-size:1rem}.chart .sub{color:var(--muted);font-size:.8rem;margin:2px 0 8px}
svg text{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}.tip{position:absolute;pointer-events:none;background:#0b1226;border:1px solid var(--line);border-radius:8px;padding:6px 9px;font-size:.8rem;white-space:nowrap;display:none}
details{margin-top:8px;font-size:.85rem;color:var(--muted)}summary{cursor:pointer}table{border-collapse:collapse;margin-top:6px}td,th{padding:2px 14px 2px 0;text-align:left;font-variant-numeric:tabular-nums}th{font-weight:600}
.list{border:1px solid var(--line);background:var(--panel);border-radius:14px;padding:4px 16px}.list div{padding:9px 0;border-bottom:1px solid var(--grid);word-break:break-all;font-size:.88rem}.list div:last-child{border:0}.list .why{color:var(--muted);font-size:.8rem}
pre{border:1px solid var(--line);background:#0b1226;border-radius:14px;padding:12px 14px;font-size:.78rem;line-height:1.5;white-space:pre-wrap;word-break:break-all;color:var(--muted);margin:0}
.empty{color:var(--muted)}
</style></head><body><main><header><h1>Status</h1>
<p><span id="checked">Checking…</span> Refreshes every 30 seconds. <a href="/">Ask your files</a> · <a href="/facts">Your facts</a></p></header>
<h2>Services</h2><section class="tiles" id="services"></section>
<h2>Index</h2><section class="tiles" id="numbers"></section>
<h2>Last 30 days</h2><section class="charts">
<div class="chart" id="indexed"><h3>Files indexed</h3><p class="sub">Files read or re-read each day</p></div>
<div class="chart" id="found"><h3>New facts found</h3><p class="sub">Personal details found in files each day</p></div></section>
<h2>Skipped files</h2><section class="list" id="skipped"></section>
<h2>Recent activity</h2><pre id="log"></pre>
<script>
const $=id=>document.getElementById(id);
const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e};
const SVG='http://www.w3.org/2000/svg',svg=(tag,attrs)=>{const e=document.createElementNS(SVG,tag);for(const k in attrs)e.setAttribute(k,attrs[k]);return e};
const STATES={active:['●','Running','ok'],failed:['✕','Not responding','no'],inactive:['■','Stopped','wait'],activating:['◐','Starting','wait'],deactivating:['◐','Stopping','wait'],unknown:['?','Unknown','muted']};
const num=n=>n.toLocaleString(),day=d=>new Date(d+'T00:00').toLocaleDateString(undefined,{month:'short',day:'numeric'});
const ago=t=>{const s=Math.max(0,Date.now()/1000-t);return s<90?'just now':s<5400?Math.round(s/60)+' min ago':s<129600?Math.round(s/3600)+' h ago':Math.round(s/86400)+' days ago'};
let data=null;
function tile(parent,label,state,detail){const t=el('div','tile');t.append(el('div','label',label));const [icon,word,cls]=STATES[state]||STATES.unknown;
  const s=el('div','state '+cls);s.append(el('span','icon',icon),word);t.append(s);if(detail)t.append(el('div','detail',detail));parent.append(t)}
function stat(parent,label,value,detail){const t=el('div','tile');t.append(el('div','label',label),el('div','big',num(value)));if(detail)t.append(el('div','detail',detail));parent.append(t)}
function step(max){if(max<=3)return 1;const raw=max/3,p=10**Math.floor(Math.log10(raw));return [1,2,5,10].map(m=>m*p).find(s=>s>=raw)}
function chart(box,rows,noun){
  box.querySelectorAll('svg,.tip,details').forEach(e=>e.remove());
  const W=Math.max(box.clientWidth-32,240),H=170,L=34,T=10,B=24,pw=W-L-4,ph=H-T-B,band=pw/rows.length,bw=Math.min(24,band-2);
  const max=Math.max(1,...rows.map(r=>r.count)),s=step(max),top=s*Math.ceil(max/s),y=v=>T+ph-ph*v/top;
  const g=svg('svg',{width:W,height:H,viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':`${noun} per day, last 30 days`});
  for(let v=0;v<=top;v+=s){g.append(svg('line',{x1:L,x2:W-4,y1:y(v),y2:y(v),stroke:'var(--grid)','stroke-width':1}));
    const t=svg('text',{x:L-6,y:y(v)+4,'text-anchor':'end'});t.textContent=num(v);g.append(t)}
  const tip=el('div','tip');box.append(tip);
  rows.forEach((r,i)=>{const cx=L+band*i+band/2,x=cx-bw/2,h=ph*r.count/top,b=T+ph;
    if(r.count){const rr=Math.min(4,h,bw/2);g.append(svg('path',{fill:'var(--bar)',d:`M${x},${b}V${b-h+rr}Q${x},${b-h} ${x+rr},${b-h}H${x+bw-rr}Q${x+bw},${b-h} ${x+bw},${b-h+rr}V${b}Z`}))}
    if((rows.length-1-i)%7===0){const last=i===rows.length-1,t=svg('text',{x:last?W-4:cx,y:H-6,'text-anchor':last?'end':'middle'});t.textContent=day(r.day);g.append(t)}
    const hit=svg('rect',{x:L+band*i,y:T,width:band,height:ph,fill:'transparent'});
    hit.onmouseenter=()=>{tip.textContent=`${day(r.day)}: ${num(r.count)} ${noun}`;tip.style.display='block';
      const left=Math.min(Math.max(cx+16-tip.offsetWidth/2,4),box.clientWidth-tip.offsetWidth-4);tip.style.left=left+'px';tip.style.top=(y(r.count)+box.querySelector('svg').getBoundingClientRect().top-box.getBoundingClientRect().top-34)+'px'};
    hit.onmouseleave=()=>{tip.style.display='none'};g.append(hit)});
  box.insertBefore(g,tip);
  const d=el('details');d.append(el('summary',null,'Show as a table'));const active=rows.filter(r=>r.count);
  if(active.length){const tb=el('table');const hr=el('tr');hr.append(el('th',null,'Day'),el('th',null,noun[0].toUpperCase()+noun.slice(1)));tb.append(hr);
    for(const r of active){const tr=el('tr');tr.append(el('td',null,day(r.day)),el('td',null,num(r.count)));tb.append(tr)}d.append(tb)}
  else d.append(el('p',null,'None in the last 30 days.'));box.append(d)}
function render(){if(!data)return;
  $('checked').textContent='Checked '+new Date(data.checked_at*1000).toLocaleTimeString()+'.';
  const sv=$('services');sv.replaceChildren();
  for(const s of data.services)tile(sv,s.label,s.state,s.state==='active'?`Since ${s.since.replace(/^\\w+ /,'').replace(/ [A-Z]+$/,'')}`+(s.restarts?` · restarted ${s.restarts}×`:''):s.unit);
  tile(sv,'Ollama (local models)',data.ollama.state,data.ollama.state==='active'?`${data.ollama.models.length} model(s) available`:'Answers and fact scanning wait until it is back');
  const d=data.documents,f=data.facts,nb=$('numbers');nb.replaceChildren();
  stat(nb,'Files indexed',d.total,Object.entries(d.kinds).map(([k,n])=>`${num(n)} ${k}`).join(' · ')+(d.last_indexed?` · last ${ago(d.last_indexed)}`:''));
  stat(nb,'Facts suggested in forms',f.pending+f.approved,`${num(f.approved)} approved · ${num(f.pending)} unreviewed · ${num(f.rejected)} rejected`);
  stat(nb,'Files scanned for facts',d.scanned,`${num(d.chunks)} passages searchable`);
  stat(nb,'Files skipped',data.skipped.length,data.skipped.length?'Listed below':'Every file could be read');
  if(data.drive){const g=data.drive,done=g.stages.reduce((a,s)=>a+s.done,0);stat(nb,'Google Drive files read',done,
    `of ${num(g.files)} · ${g.running?'indexing now':'not running'} · `+g.stages.filter(s=>s.files).map(s=>`${s.name} ${Math.floor(100*s.done/s.files)}%`).join(' · '))}
  chart($('indexed'),data.indexed_per_day,'files');chart($('found'),data.facts_per_day,'facts');
  const sk=$('skipped');sk.replaceChildren();if(!data.skipped.length)sk.append(el('div','empty','None. Every file in your folders could be read.'));
  for(const s of data.skipped){const row=el('div',null,s.path);row.append(el('div','why',s.reason));sk.append(row)}
  $('log').textContent=data.log.length?data.log.join('\\n'):'No log available (the services are not running under systemd).'}
async function load(){try{const r=await fetch('/api/status');data=await r.json();render()}catch{$('checked').textContent='The local server is not responding.'}}
load();setInterval(load,30000);let resize;addEventListener('resize',()=>{clearTimeout(resize);resize=setTimeout(render,150)});
</script></body></html>"""

# The floating assistant (`rigveda orb`). In its own window (?native=1) it starts as a small orb and opens into the
# panel; in a browser tab it shows the panel straight away.
ORB_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rigveda AI · Orb</title><style>
:root{color-scheme:dark;--cyan:#5ee7ff;--cyan-dim:rgba(94,231,255,.32);--cyan-faint:rgba(94,231,255,.1);--glass:rgba(5,20,34,.74);--glass-deep:rgba(2,9,18,.86);
--text:#e8fbff;--muted:#8fb6c4;--ok:#5fffb0;--warn:#ffc35c;--bad:#ff5c7a;--sans:'Ubuntu Sans','Ubuntu',system-ui,sans-serif;--mono:'Ubuntu Sans Mono','Ubuntu Mono',monospace}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:transparent;color:var(--text);font:14px var(--sans);overflow:hidden;user-select:none}
body.web{background:radial-gradient(circle at 30% 20%,#0d2a44,#020810 70%)}
/* ── the orb ── */
#orb{position:fixed;left:50%;top:50%;width:86vmin;height:86vmin;margin:-43vmin 0 0 -43vmin;cursor:pointer;transition:filter .25s ease;filter:drop-shadow(0 0 10px rgba(94,231,255,.4))}
#orb:hover{filter:drop-shadow(0 0 18px rgba(94,231,255,.7))}
/* voice: set on <html> by voice(), so opening and closing the panel doesn't clear it */
html.v-thinking .art .spin{animation-duration:1.2s}html.v-thinking .art .rev{animation-duration:4s}
html.v-listening header .art{animation:hear 1s ease-in-out infinite alternate}html.v-speaking header .art{filter:drop-shadow(0 0 14px rgba(94,231,255,.9))}
@keyframes hear{from{filter:drop-shadow(0 0 6px rgba(94,231,255,.5))}to{filter:drop-shadow(0 0 18px rgba(94,231,255,1)) brightness(1.25)}}
body.panel #orb,body.web #orb{display:none}
/* edges and corners that resize the panel window */
.grip{position:fixed;z-index:9;display:none}body.panel .grip{display:block}
.grip.n,.grip.s{left:18px;right:18px;height:8px;cursor:ns-resize}.grip.n{top:0}.grip.s{bottom:0}.grip.e,.grip.w{top:18px;bottom:18px;width:8px;cursor:ew-resize}.grip.e{right:0}.grip.w{left:0}
.grip.nw,.grip.ne,.grip.se,.grip.sw{width:18px;height:18px}.grip.nw{left:0;top:0;cursor:nwse-resize}.grip.se{right:0;bottom:0;cursor:nwse-resize}.grip.ne{right:0;top:0;cursor:nesw-resize}.grip.sw{left:0;bottom:0;cursor:nesw-resize}
.art{overflow:visible}.art .spin{transform-origin:60px 60px;animation:spin 18s linear infinite}.art .rev{transform-origin:60px 60px;animation:spin 11s linear infinite reverse}
.art .spin{animation-duration:24s}.art .rev{animation-duration:60s}.thinking .art .spin{animation-duration:1.2s}.thinking .art .rev{animation-duration:4s}
.meter{fill:none;stroke-width:3;stroke-linecap:round}.meter.track{stroke:rgba(94,231,255,.3)}.meter.fill{stroke:var(--cyan);transition:stroke-dasharray .8s ease,stroke .4s}.meter.fill.warn{stroke:var(--warn)}.meter.fill.bad{stroke:var(--bad)}
.arc{fill:none;stroke-width:3.6;stroke-linecap:round;transition:stroke .4s}.arc.ok{stroke:var(--ok)}.arc.warn{stroke:var(--warn)}.arc.bad{stroke:var(--bad)}.arc.unknown{stroke:var(--cyan-dim)}
@keyframes spin{to{transform:rotate(360deg)}}
/* ── the panel ── */
#panel{position:fixed;inset:6px;display:none;flex-direction:column;border-radius:22px;padding:16px 18px 16px;overflow:hidden;
  background:linear-gradient(160deg,var(--glass),var(--glass-deep)),repeating-linear-gradient(0deg,transparent 0 23px,var(--cyan-faint) 23px 24px),repeating-linear-gradient(90deg,transparent 0 23px,var(--cyan-faint) 23px 24px);
  border:1px solid var(--cyan-dim);box-shadow:0 0 28px rgba(94,231,255,.18),inset 0 0 40px rgba(94,231,255,.07);animation:open .32s cubic-bezier(.2,.9,.3,1.2)}
body.panel #panel,body.web #panel{display:flex}body.web #panel{inset:auto;left:50%;top:50%;width:428px;height:768px;margin:-384px 0 0 -214px}
#panel::before{content:"";position:absolute;inset:0;pointer-events:none;background:linear-gradient(180deg,transparent,rgba(94,231,255,.07) 50%,transparent);height:30%;animation:sweep 6s linear infinite}
.corner{position:absolute;width:18px;height:18px;border:2px solid var(--cyan);opacity:.8;pointer-events:none}.tl{left:8px;top:8px;border-right:0;border-bottom:0;border-radius:8px 0 0 0}.tr{right:8px;top:8px;border-left:0;border-bottom:0;border-radius:0 8px 0 0}
.bl{left:8px;bottom:8px;border-right:0;border-top:0;border-radius:0 0 0 8px}.br{right:8px;bottom:8px;border-left:0;border-top:0;border-radius:0 0 8px 0}
@keyframes open{from{opacity:0;transform:scale(.92)}}@keyframes sweep{from{transform:translateY(-100%)}to{transform:translateY(400%)}}
header{display:flex;align-items:center;gap:12px;padding:2px 4px 12px;border-bottom:1px solid var(--cyan-faint)}header .art{width:46px;height:46px;flex:none;filter:drop-shadow(0 0 8px rgba(94,231,255,.5))}
.title{flex:1;min-width:0}.title b{display:block;font-size:17px;letter-spacing:.32em;font-weight:700}.title span{font-size:10px;letter-spacing:.22em;color:var(--muted);text-transform:uppercase}
.clock{font:600 13px var(--mono);color:var(--cyan);letter-spacing:.08em;text-align:right}.clock small{display:block;font-size:9px;color:var(--muted);letter-spacing:.2em}
.icon-btn{background:none;border:1px solid var(--cyan-dim);color:var(--cyan);border-radius:9px;width:28px;height:28px;cursor:pointer;font:16px var(--sans);line-height:1}.icon-btn:hover{background:var(--cyan-faint)}body.web .icon-btn{display:none}
.label{font-size:9.5px;letter-spacing:.24em;color:var(--muted);text-transform:uppercase}
.systems{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0 4px}
.sys{display:flex;align-items:center;gap:8px;padding:7px 8px;border:1px solid var(--cyan-faint);border-radius:12px;background:rgba(94,231,255,.03)}
.sys svg{width:30px;height:30px;flex:none}.sys .arc{stroke-width:3}.sys .track{fill:none;stroke:var(--cyan-faint);stroke-width:3}.sys .state{font:600 11px var(--mono);letter-spacing:.06em}
.state.ok{color:var(--ok)}.state.warn{color:var(--warn)}.state.bad{color:var(--bad)}.state.unknown{color:var(--muted)}
.live{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}.gauge{padding:8px 10px 7px;border:1px solid var(--cyan-faint);border-radius:12px;background:rgba(94,231,255,.03)}
.gauge .top{display:flex;justify-content:space-between;align-items:baseline}.gauge .val{font:600 18px var(--mono);text-shadow:0 0 12px rgba(94,231,255,.5)}.gauge .val.warn{color:var(--warn)}.gauge .val.bad{color:var(--bad)}
.gauge svg{display:block;width:100%;height:22px;margin:4px 0 2px}.gauge .sub{font-size:10.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gauge{cursor:pointer;transition:border-color .2s,background .2s}.gauge:hover,.gauge:focus-visible{border-color:var(--cyan-dim);background:rgba(94,231,255,.07);outline:0}
#drawer,#activity{position:absolute;inset:70px 10px 10px;display:none;flex-direction:column;border:1px solid var(--cyan-dim);border-radius:16px;padding:10px 10px 6px;
  background:linear-gradient(160deg,#04101c,#020810);box-shadow:0 0 30px rgba(94,231,255,.15);animation:open .25s cubic-bezier(.2,.9,.3,1.2);z-index:2}
#voicelog{position:absolute;inset:70px 10px 10px;display:none;flex-direction:column;border:1px solid var(--cyan-dim);border-radius:16px;padding:10px 10px 6px;
  background:linear-gradient(160deg,#04101c,#020810);box-shadow:0 0 30px rgba(94,231,255,.15);animation:open .25s cubic-bezier(.2,.9,.3,1.2);z-index:2}
#voicelog.on{display:flex}#vlist{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:8px;padding:6px 2px;scrollbar-width:thin;scrollbar-color:var(--cyan-dim) transparent;user-select:text}
#vask{margin-top:6px}  /* otherwise styled like the main chat box */
#vlist time{align-self:center;font:10px var(--mono);color:var(--muted);margin-top:4px}#vlist .did{align-self:flex-start;font:10px var(--mono);color:var(--muted);margin:-4px 0 0 4px}
#drawer.on,#activity.on{display:flex}.dhead{display:flex;align-items:center;gap:8px}.dhead button,.tabs button{font:600 11px var(--sans);letter-spacing:.12em;text-transform:uppercase;cursor:pointer;border-radius:9px;padding:6px 10px}
.dhead .back{background:none;border:1px solid var(--cyan-dim);color:var(--cyan)}.tabs{display:flex;gap:4px;margin-left:auto}.tabs button{background:none;border:1px solid transparent;color:var(--muted)}.tabs button.on{color:var(--text);border-color:var(--cyan);background:var(--cyan-faint)}
.dhead h3{margin:0 0 0 4px;font:600 11px var(--sans);letter-spacing:.18em;text-transform:uppercase;color:var(--cyan)}
#activity .scroll{flex:1;min-height:0;overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--cyan-dim) transparent;user-select:text}
.stage{padding:6px 2px}.stage .row{display:flex;justify-content:space-between;font-size:12px}.stage .row span:last-child{font:11px var(--mono);color:var(--muted)}
.stage .bar{height:4px;margin-top:4px;border-radius:2px;background:var(--cyan-faint);overflow:hidden}.stage .bar i{display:block;height:100%;background:var(--cyan);box-shadow:0 0 6px var(--cyan);transition:width .6s}
.stage.now .row span:first-child::after{content:" ◂ now";color:var(--cyan);font:10px var(--mono)}
.feed{margin:2px 0 8px}.feed div{display:grid;grid-template-columns:14px 1fr auto;gap:6px;padding:3px 2px;font:10.5px var(--mono);color:var(--muted);border-bottom:1px dashed rgba(94,231,255,.08)}
.feed b{color:var(--text);font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.feed .indexed{color:var(--ok)}.feed .failed{color:var(--bad)}.feed .waiting,.feed .skipped,.feed .empty{color:var(--warn)}
.logs div{padding:3px 2px;font:10.5px/1.4 var(--mono);color:#b9d7e0;word-break:break-word}.logs time{color:var(--muted);margin-right:6px}.logs span{color:var(--cyan);margin-right:6px}
.alert.link{cursor:pointer}.alert.link:hover b,.alert.link:hover span:last-child{color:var(--text)}
.dsum{font:11px var(--mono);color:var(--muted);margin:9px 2px 6px}.dcols{display:grid;grid-template-columns:1fr 70px;gap:8px;padding:0 8px 4px;border-bottom:1px solid var(--cyan-faint)}
#dlist{flex:1;min-height:0;overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--cyan-dim) transparent;user-select:text}
.app{border-bottom:1px solid rgba(94,231,255,.07)}.app>.row{display:grid;grid-template-columns:1fr 70px;gap:8px;align-items:center;padding:7px 8px;cursor:pointer;border-radius:8px}.app>.row:hover{background:rgba(94,231,255,.06)}
.app .nm{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.app .nm small{font:10px var(--mono);color:var(--muted);margin-left:6px;font-weight:400}.app .nm::before{content:"▸";color:var(--cyan);margin-right:6px;display:inline-block;transition:transform .15s}.app.open .nm::before{transform:rotate(90deg)}
.app .more{grid-column:1/-1;font:10.5px var(--mono);color:var(--muted)}.app .bar{grid-column:1/-1;height:3px;border-radius:2px;background:var(--cyan-faint);overflow:hidden}.app .bar i{display:block;height:100%;background:var(--cyan);box-shadow:0 0 6px var(--cyan)}
.app .v{font:600 14px var(--mono);text-align:right}.app .v.warn{color:var(--warn)}.app .v.bad{color:var(--bad)}
.procs{display:none;margin:0 4px 8px 18px;border-left:1px solid var(--cyan-dim);padding-left:8px}.app.open .procs{display:block}
.proc{padding:6px 0;border-bottom:1px dashed rgba(94,231,255,.1);font:10.5px var(--mono);color:var(--muted)}.proc:last-child{border:0}.proc b{color:var(--text);font-weight:600}.proc .cmd{color:#b9d7e0;word-break:break-all;margin-top:2px;white-space:pre-wrap}
.readouts{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:8px}.readout{padding:9px 10px 8px;border-left:2px solid var(--cyan);background:linear-gradient(90deg,rgba(94,231,255,.08),transparent)}
.readout .num{font:600 27px var(--mono);letter-spacing:-.02em;text-shadow:0 0 14px rgba(94,231,255,.55)}.readout .sub{font-size:10.5px;color:var(--muted);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.activity{margin-top:12px}.activity .row{display:flex;justify-content:space-between;align-items:baseline}.activity .row span:last-child{font:11px var(--mono);color:var(--muted)}
.bars{display:flex;align-items:flex-end;gap:2px;height:44px;margin-top:6px;border-bottom:1px solid var(--cyan-dim)}.bars i{flex:1;min-height:2px;border-radius:3px 3px 0 0;background:var(--cyan-faint);transform-origin:bottom}.bars.grow i{animation:grow .7s cubic-bezier(.2,.9,.3,1) backwards}@keyframes grow{from{transform:scaleY(0)}}
.bars i.on{background:linear-gradient(180deg,var(--cyan),rgba(94,231,255,.35));box-shadow:0 0 8px rgba(94,231,255,.6)}.bars i:hover{background:var(--text)}
.alerts{margin-top:8px;font-size:11.5px}.alert{display:flex;gap:7px;align-items:baseline;padding:5px 0;color:var(--muted)}.alert b{font-weight:600}.alert .i{font-weight:700}.alert.warn .i{color:var(--warn)}.alert.bad .i{color:var(--bad)}.alert.ok .i{color:var(--ok)}
#log{flex:1;min-height:0;overflow-y:auto;margin-top:8px;padding:10px 2px 6px;border-top:1px solid var(--cyan-faint);display:flex;flex-direction:column;gap:10px;user-select:text;scrollbar-width:thin;scrollbar-color:var(--cyan-dim) transparent}
.msg{max-width:92%;padding:9px 12px;border-radius:14px;line-height:1.45;white-space:pre-wrap;word-wrap:break-word;font-size:13.5px}
.msg.you{align-self:flex-end;background:rgba(94,231,255,.16);border:1px solid var(--cyan-dim);border-bottom-right-radius:4px}
.msg.ai{align-self:flex-start;background:rgba(255,255,255,.04);border:1px solid var(--cyan-faint);border-bottom-left-radius:4px}.msg.ai.wait{color:var(--muted);font-style:italic}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}.chips span{font:10.5px var(--mono);padding:2px 7px;border:1px solid var(--cyan-dim);border-radius:999px;color:var(--cyan);max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
form{display:flex;gap:8px;margin-top:8px;padding:6px 6px 6px 14px;border:1px solid var(--cyan-dim);border-radius:16px;background:rgba(2,9,18,.6);transition:border-color .2s,box-shadow .2s}
form:focus-within{border-color:var(--cyan);box-shadow:0 0 18px rgba(94,231,255,.25)}form input{flex:1;min-width:0;background:none;border:0;outline:0;color:var(--text);font:14px var(--sans)}form input::placeholder{color:var(--muted)}
form button{width:36px;height:36px;border-radius:12px;border:0;background:var(--cyan);color:#022;font:700 17px var(--sans);cursor:pointer;box-shadow:0 0 14px rgba(94,231,255,.5)}form button:disabled{opacity:.4;cursor:wait}
footer{display:flex;justify-content:space-between;margin-top:9px;padding:0 22px;font-size:10px;letter-spacing:.18em;text-transform:uppercase}footer a{color:var(--muted);text-decoration:none;cursor:pointer}footer a:hover{color:var(--cyan)}body.web #quit{display:none}
</style></head><body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs><radialGradient id="core" cx="40%" cy="35%" r="70%"><stop offset="0" stop-color="#d9fdff"/><stop offset=".35" stop-color="#4fd8f5"/><stop offset=".75" stop-color="#0b5f86"/><stop offset="1" stop-color="#03243a"/></radialGradient>
<radialGradient id="halo" r="50%"><stop offset=".55" stop-color="#5ee7ff" stop-opacity=".3"/><stop offset="1" stop-color="#5ee7ff" stop-opacity="0"/></radialGradient>
<linearGradient id="casing" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6c8499"/><stop offset=".5" stop-color="#34475a"/><stop offset="1" stop-color="#18232f"/></linearGradient>
<linearGradient id="coil" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#9fe9ff"/></linearGradient>
<filter id="glow" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="2.2"/></filter></defs></svg>
<svg id="orb" class="art" viewBox="0 0 120 120" role="button" tabindex="0" aria-label="Open Rigveda AI"><title id="orb-title">Rigveda AI</title>
<circle cx="60" cy="60" r="60" fill="url(#halo)"/>
<g class="body">
<g fill="none" stroke-linecap="round"><g stroke="#04131f" stroke-width="7"><path d="M60 3.5 A56.5 56.5 0 0 1 109 88.2"/><path d="M104.6 95.9 A56.5 56.5 0 0 1 15.4 95.9"/><path d="M11 88.2 A56.5 56.5 0 0 1 55 3.7"/></g>
<path class="arc unknown" data-arc="0" d="M60 3.5 A56.5 56.5 0 0 1 109 88.2"/><path class="arc unknown" data-arc="1" d="M104.6 95.9 A56.5 56.5 0 0 1 15.4 95.9"/><path class="arc unknown" data-arc="2" d="M11 88.2 A56.5 56.5 0 0 1 55 3.7"/></g>
<circle cx="60" cy="60" r="50" fill="url(#casing)" stroke="#04131f" stroke-width="2.6"/>
<path d="M17 52 A44 44 0 0 1 103 52" fill="none" stroke="#fff" stroke-opacity=".18" stroke-width="3" stroke-linecap="round"/>
<g class="rev bolts"></g>
<circle cx="60" cy="60" r="38.5" fill="none" stroke="#5ee7ff" stroke-width="13" filter="url(#glow)"/>
<g class="spin coils"></g>
<circle cx="60" cy="60" r="30" fill="#0a2233" stroke="#04131f" stroke-width="2.6"/><g class="meters"></g>
<circle cx="60" cy="60" r="25" fill="url(#core)" stroke="#04131f" stroke-width="2.2"/>
<path d="M44 52 A17 17 0 0 1 58 41.5 A20 20 0 0 0 47 55 Z" fill="#fff" fill-opacity=".75"/>
<circle cx="60" cy="60" r="9" fill="#fff" fill-opacity=".7" filter="url(#glow)"/><circle cx="60" cy="60" r="4.5" fill="#fff"/>
</g></svg>
<section id="panel" aria-label="Rigveda AI"><i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
<header><span id="mini"></span><div class="title"><b>RIGVEDA</b><span id="tagline">Local intelligence</span></div><div class="clock" id="clock"></div>
<button class="icon-btn" id="close" title="Back to the orb (Esc)">–</button></header>
<div class="systems" id="systems"></div>
<div class="live" id="live"></div>
<div id="drawer" role="dialog" aria-label="What's using your computer"><div class="dhead"><button id="back" class="back">← Back</button>
<div class="tabs" id="tabs"><button data-tab="cpu">CPU</button><button data-tab="ram">Memory</button><button data-tab="gpu">GPU</button></div></div>
<div class="dsum" id="dsum">Reading processes…</div><div class="dcols"><span class="label">App · processes</span><span class="label" id="dcol" style="text-align:right">CPU</span></div><div id="dlist"></div></div>
<div id="voicelog" role="dialog" aria-label="What you said to Rigved"><div class="dhead"><button id="vback" class="back">← Back</button><h3>Voice</h3></div>
<div class="dsum" id="vsum">Reading your conversations…</div><div id="vlist"></div>
<form id="vask"><input id="vq" placeholder="Type to Rigved: open Downloads, what time is it…" autocomplete="off" aria-label="Type a command for Rigved"><button id="vsend" aria-label="Send to Rigved">➜</button></form></div>
<div id="activity" role="dialog" aria-label="What Rigveda is doing"><div class="dhead"><button id="aback" class="back">← Back</button><h3>Activity</h3></div>
<div class="dsum" id="asum">Reading activity…</div><div class="scroll"><div id="stages"></div>
<div class="label" id="feed-label" style="margin-top:10px">Just read from Google Drive</div><div class="feed" id="feed"></div>
<div class="label" style="margin-top:6px">Log · every Rigveda service · newest first</div><div class="logs" id="logs"></div></div></div>
<div class="readouts" id="readouts"></div>
<div class="activity"><div class="row"><span class="label">Index activity · 30 days</span><span id="act-total"></span></div><div class="bars" id="bars"></div></div>
<div class="alerts" id="alerts"></div>
<div id="log"><div class="msg ai">Hi. Ask me anything about your files.</div></div>
<form id="ask"><input id="q" placeholder="Ask anything…" autocomplete="off" aria-label="Your question"><button id="send" aria-label="Ask">➜</button></form>
<footer><a id="status-link" href="/status" target="_blank">Status page ↗</a><a id="activity-link">Activity</a><a id="voice-link">Voice</a><a id="quit">Quit</a></footer></section>
<i class="grip n" data-edge="n"></i><i class="grip ne" data-edge="ne"></i><i class="grip e" data-edge="e"></i><i class="grip se" data-edge="se"></i>
<i class="grip s" data-edge="s"></i><i class="grip sw" data-edge="sw"></i><i class="grip w" data-edge="w"></i><i class="grip nw" data-edge="nw"></i>
<script>
const native=new URLSearchParams(location.search).has('native')&&window.webkit?.messageHandlers?.orb;
const post=m=>native&&window.webkit.messageHandlers.orb.postMessage(JSON.stringify(m));
const $=id=>document.getElementById(id),el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
const num=n=>Number(n).toLocaleString(),orb=$('orb');
const STATES={active:['ok','●','Online'],failed:['bad','✕','Offline'],inactive:['warn','■','Stopped'],activating:['warn','◐','Starting'],deactivating:['warn','◐','Stopping'],unknown:['unknown','?','Unknown']};
let data=null,busy=false;
document.body.className=native?'orb':'web';
const NS='http://www.w3.org/2000/svg',pt=(r,a)=>[(60+r*Math.sin(a*Math.PI/180)).toFixed(2),(60-r*Math.cos(a*Math.PI/180)).toFixed(2)].join(' ');
for(let i=0;i<10;i++){const a0=i*36+3,a1=a0+30,c=document.createElementNS(NS,'path');  // the reactor's ten coils
  c.setAttribute('d',`M${pt(44,a0)} A44 44 0 0 1 ${pt(44,a1)} L${pt(33,a1)} A33 33 0 0 0 ${pt(33,a0)} Z`);
  c.setAttribute('fill','url(#coil)');c.setAttribute('stroke','#04131f');c.setAttribute('stroke-width','1.8');c.setAttribute('stroke-linejoin','round');orb.querySelector('.coils').append(c)}
for(let i=0;i<8;i++){const [x,y]=pt(47.2,i*45+22.5).split(' '),b=document.createElementNS(NS,'circle');
  Object.entries({cx:x,cy:y,r:1.9,fill:'#a9bccc',stroke:'#04131f','stroke-width':1}).forEach(([k,v])=>b.setAttribute(k,v));orb.querySelector('.bolts').append(b)}
const METERS=['cpu','gpu','vram','ram'];  // clockwise from the top: one quarter of the inner ring each
for(const [i,name] of METERS.entries()){const a0=i*90+6,a1=a0+78;for(const cls of ['track','fill']){const m=document.createElementNS(NS,'path');
  m.setAttribute('d',`M${pt(27.6,a0)} A27.6 27.6 0 0 1 ${pt(27.6,a1)}`);m.setAttribute('pathLength','100');m.setAttribute('class','meter '+cls);
  if(cls==='fill'){m.dataset.meter=name;m.setAttribute('stroke-dasharray','0 100')}orb.querySelector('.meters').append(m)}}
const mini=orb.cloneNode(true);mini.removeAttribute('id');mini.removeAttribute('role');mini.removeAttribute('tabindex');mini.setAttribute('aria-hidden','true');$('mini').append(mini);
function expand(){if(document.body.className==='panel')return;post({cmd:'expand'});document.body.className='panel';load(true);setTimeout(()=>$('q').focus(),250)}
function collapse(){if(!native)return;closeDetails();closeActivity();document.body.className='orb';post({cmd:'collapse'});animate()}
let press=null;
// Springs: the orb bobs, leans toward the pointer anywhere on screen (the window sends look(dx, dy), pixels from
// the orb's centre), squashes when pressed and bounces back.
const body=orb.querySelector('.body');
const S={s:1,vs:0,q:0,vq:0,t:0,vt:0};let hover=false,held=false,ptr=null,idle=null,lastT=0,running=false;
window.look=(dx,dy)=>{ptr={x:dx,y:dy};clearTimeout(idle);idle=setTimeout(()=>{ptr=null},4000)};
const spring=(k,x,v,to,stiff,damp,dt)=>{v+=(-stiff*(x-to)-damp*v)*dt;S[k]=x+v*dt;return v};
function frame(t){if(document.body.className!=='orb'){running=false;return}
  const dt=Math.min(.033,(t-(lastT||t))/1000);lastT=t;
  S.vs=spring('s',S.s,S.vs,held?.9:Math.max(hover?1.08:1,voiceFrame(dt)),260,11,dt);S.vq=spring('q',S.q,S.vq,held?.07:0,300,10,dt);
  S.vt=spring('t',S.t,S.vt,ptr?Math.max(-1,Math.min(1,ptr.x/160))*9:0,90,9,dt);
  const bob=Math.sin(t/1000*1.7)*2.4;
  body.setAttribute('transform',`translate(60 ${(60+bob).toFixed(2)}) rotate(${S.t.toFixed(2)}) scale(${(S.s*(1+S.q)).toFixed(3)} ${(S.s*(1-S.q)).toFixed(3)}) translate(-60 -60)`);
  requestAnimationFrame(frame)}
function animate(){if(!running&&document.body.className==='orb'){running=true;lastT=0;requestAnimationFrame(frame)}}
orb.addEventListener('mouseenter',()=>{hover=true});orb.addEventListener('mouseleave',()=>{hover=false;held=false});
orb.addEventListener('mousemove',e=>{const r=orb.getBoundingClientRect();window.look(e.clientX-r.left-r.width/2,e.clientY-r.top-r.height/2)});
orb.addEventListener('mousedown',e=>{if(e.button===0)held=true});addEventListener('mouseup',()=>{if(held){held=false;S.vs+=3}});
(function hop(){setTimeout(()=>{if(!hover)S.vs+=1.4+Math.random();hop()},7000+Math.random()*7000)})();
animate();
orb.addEventListener('mousedown',e=>{if(e.button===0)press={x:e.screenX,y:e.screenY,moved:false}});
addEventListener('mousemove',e=>{if(press&&!press.moved&&Math.hypot(e.screenX-press.x,e.screenY-press.y)>4){press.moved=true;held=false;post({cmd:'drag'});press=null}});
addEventListener('mouseup',()=>{if(press&&!press.moved)setTimeout(expand,140);press=null});
orb.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' ')expand()});
$('close').onclick=collapse;addEventListener('keydown',e=>{if(e.key==='Escape'){if($('activity').classList.contains('on'))closeActivity();else if($('drawer').classList.contains('on'))closeDetails();else collapse()}});
$('quit').onclick=()=>post({cmd:'quit'});
// Voice: what you said to Rigved by voice and what it said back, newest at the bottom.
let voiceTimer=null,voiceSeen=-1;
function showVoice(){$('voicelog').classList.add('on');voiceSeen=-1;voices();clearInterval(voiceTimer);voiceTimer=setInterval(voices,3000);setTimeout(()=>$('vq').focus(),50)}
function closeVoice(){$('voicelog').classList.remove('on');clearInterval(voiceTimer);voiceTimer=null}
async function voices(){let d;try{d=await (await fetch('/api/voice')).json()}catch{$('vsum').textContent='The local server is not responding.';return}
  const list=$('vlist'),n=d.entries.length;if(n===voiceSeen||vbusy)return;voiceSeen=n;
  $('vsum').textContent=n?`${n} conversation${n===1?'':'s'} · hold the talk key, or type below`:'Nothing yet. Hold the talk key and speak, or type below.';
  list.replaceChildren();let day='';
  for(const e of d.entries){const t=new Date(e.at*1000),label=t.toLocaleDateString([], {weekday:'short',month:'short',day:'numeric'});
    if(label!==day){day=label;list.append(el('time',null,label))}
    const you=el('div','msg you',(e.typed?'⌨ ':'')+(e.heard||'…'));you.title=t.toLocaleTimeString()+(e.typed?' · typed':' · spoken');list.append(you);
    if(e.did?.length)list.append(el('div','did',e.did.join(' · ')));list.append(el('div','msg ai',e.said))}
  list.scrollTop=1e9}
// Typing to Rigved: the listener carries it out like something said, and answers here in text.
let vbusy=false;
$('vask').addEventListener('submit',async e=>{e.preventDefault();const text=$('vq').value.trim();if(!text||vbusy)return;vbusy=true;$('vsend').disabled=true;$('vq').value='';
  const list=$('vlist');list.append(el('div','msg you','⌨ '+text));const reply=el('div','msg ai wait','On it…');list.append(reply);list.scrollTop=1e9;
  try{const r=await fetch('/api/voice',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});const d=await r.json();
    reply.classList.remove('wait');reply.textContent=d.said||d.error||'No answer came back.'}
  catch{reply.classList.remove('wait');reply.textContent='The local server could not pass that on.'}
  finally{vbusy=false;$('vsend').disabled=false;$('vq').focus();voiceSeen=-1;voices()}});
$('voice-link').onclick=showVoice;$('vback').onclick=closeVoice;$('close').addEventListener('click',closeVoice);
addEventListener('keydown',e=>{if(e.key==='Escape'&&$('voicelog').classList.contains('on')){e.stopImmediatePropagation();closeVoice()}},true);
// Voice: the listener's state and loudness, passed in by the window (orb.py) as voice(state, level). The orb pops
// when the microphone opens, swells and glows with your voice, spins while thinking and pulses as it answers.
let vstate='idle',vlevel=0,glow=0;
window.voice=(state,level)=>{if(state!==vstate){const root=document.documentElement;
  for(const s of ['listening','thinking','speaking'])root.classList.toggle('v-'+s,s===state);
  if(state==='listening')S.vs+=5;vstate=state;animate()}vlevel=level};
function voiceFrame(dt){const want=vstate==='listening'?.45+vlevel*.55:vstate==='speaking'?.15+vlevel*.7:vstate==='thinking'?.25:0;
  glow+=(want-glow)*Math.min(1,dt*14);
  orb.style.filter=glow>.02?`drop-shadow(0 0 ${(8+glow*14).toFixed(1)}px rgba(94,231,255,${(.4+glow*.55).toFixed(2)}))`:'';
  return vstate==='listening'?1.05+vlevel*.06:vstate==='speaking'?1+vlevel*.05:1}
// Resizing: the mouse wheel over the orb grows or shrinks it; the panel's edges and corners drag.
let wheelAt=0;orb.addEventListener('wheel',e=>{e.preventDefault();const t=performance.now();if(t-wheelAt<60)return;wheelAt=t;post({cmd:'zoom',step:e.deltaY<0?1:-1})},{passive:false});
document.querySelectorAll('.grip').forEach(g=>g.addEventListener('mousedown',e=>{if(e.button===0){e.preventDefault();post({cmd:'resize',edge:g.dataset.edge})}}));
$('status-link').onclick=e=>{if(native){e.preventDefault();post({cmd:'open',url:location.origin+'/status'})}};
function tick(){const d=new Date();$('clock').replaceChildren(d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}),
  el('small',null,d.toLocaleDateString([], {weekday:'short',month:'short',day:'numeric'}).toUpperCase()))}
tick();setInterval(tick,1000);
function countUp(node,to){const from=0,start=performance.now();const step=t=>{const k=Math.min(1,(t-start)/700),e=1-Math.pow(1-k,3);node.textContent=num(Math.round(from+(to-from)*e));if(k<1)requestAnimationFrame(step)};requestAnimationFrame(step)}
function gauge(state){const s=document.createElementNS('http://www.w3.org/2000/svg','svg');s.setAttribute('viewBox','0 0 30 30');
  s.innerHTML=`<circle class="track" cx="15" cy="15" r="11"/><circle class="arc ${STATES[state]?STATES[state][0]:'unknown'}" cx="15" cy="15" r="11" stroke-dasharray="${state==='active'?69:state==='failed'?8:30} 69" transform="rotate(-90 15 15)"/>`;return s}
function render(animate){if(!data)return;
  const svc=[...data.services.map(s=>({name:s.label==='File watcher'?'Watcher':'Server',state:s.state})),{name:'Models',state:data.ollama.state}];
  document.querySelectorAll('[data-arc]').forEach(a=>{const s=svc[a.dataset.arc];a.setAttribute('class','arc '+(STATES[s.state]||STATES.unknown)[0])});
  const down=svc.filter(s=>s.state!=='active');$('tagline').textContent=down.length?`${down.length} system${down.length>1?'s':''} need attention`:'All systems online';
  const sys=$('systems');sys.replaceChildren();for(const s of svc){const [cls,icon,word]=STATES[s.state]||STATES.unknown;const box=el('div','sys');const t=el('div');
    t.append(el('div','label',s.name),el('div','state '+cls,icon+' '+word));box.append(gauge(s.state),t);sys.append(box)}
  const d=data.documents,f=data.facts,ro=$('readouts');ro.replaceChildren();
  for(const [label,value,sub] of [['Files',d.total,Object.entries(d.kinds).slice(0,2).map(([k,n])=>`${num(n)} ${k}`).join(' · ')],['Facts',f.pending+f.approved,`${num(f.approved)} approved`],['Scanned',d.scanned,`${num(d.chunks)} passages`]]){
    const r=el('div','readout'),n=el('div','num',animate?'0':num(value));r.append(el('div','label',label),n,el('div','sub',sub));ro.append(r);if(animate)countUp(n,value)}
  const days=data.indexed_per_day,max=Math.max(1,...days.map(x=>x.count)),bars=$('bars');bars.replaceChildren();
  bars.classList.toggle('grow',!!animate);
  days.forEach((x,i)=>{const b=el('i',x.count?'on':'');b.style.height=Math.max(2,44*x.count/max)+'px';b.style.animationDelay=i*12+'ms';
    b.title=new Date(x.day+'T00:00').toLocaleDateString([], {month:'short',day:'numeric'})+': '+num(x.count)+' files';bars.append(b)});
  $('act-total').textContent=num(days.reduce((a,x)=>a+x.count,0))+' indexed';
  const al=$('alerts');al.replaceChildren();const add=(cls,icon,text)=>{const a=el('div','alert '+cls);a.append(el('span','i',icon),el('span',null,text));al.append(a)};
  for(const s of down)add(s.state==='failed'?'bad':'warn','!',`${s.name} is ${(STATES[s.state]||STATES.unknown)[2].toLowerCase()}.`);
  if(data.skipped.length)add('warn','!',`${data.skipped.length} file${data.skipped.length>1?'s':''} skipped · ${data.skipped[0].reason.replace(/^\\w+Error: /,'')}`);
  if(data.drive){const g=data.drive,done=g.stages.reduce((a,s)=>a+s.done,0),now=g.stages.find(s=>s.done<s.files);
    if(done<g.files){add(g.running?'ok':'warn',g.running?'↻':'!',`Google Drive: ${num(done)} of ${num(g.files)} read`+(now?` · now ${now.name}`:'')+(g.running?'':' · paused')+' ›');
      const a=al.lastChild;a.classList.add('link');a.title='Show progress and logs';a.onclick=showActivity}}
  if(!al.children.length)add('ok','✓','Every file in your folders is indexed.')}
async function load(animate){try{const r=await fetch('/api/status');data=await r.json();render(animate)}
  catch{data=null;document.querySelectorAll('[data-arc]').forEach(a=>a.setAttribute('class','arc bad'));$('tagline').textContent='Server not responding'}}
load(!native);setInterval(()=>load(false),native?60000:15000);
const GB=b=>(b/2**30).toFixed(1),level=p=>p>=90?'bad':p>=75?'warn':'',history={cpu:[],gpu:[],vram:[],ram:[]};
function spark(values,cls){const s=document.createElementNS(NS,'svg');s.setAttribute('viewBox','0 0 100 22');s.setAttribute('preserveAspectRatio','none');
  const pts=values.map((v,i)=>`${(i*100/29).toFixed(1)},${(21-20*v/100).toFixed(1)}`).join(' '),color=cls==='bad'?'var(--bad)':cls==='warn'?'var(--warn)':'var(--cyan)';
  s.innerHTML=values.length>1?`<polygon points="0,22 ${pts} ${((values.length-1)*100/29).toFixed(1)},22" fill="${color}" fill-opacity=".12"/><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>`:'';return s}
async function system(){let d;try{d=await (await fetch('/api/system')).json()}catch{return}
  const g=d.gpu,m=d.memory,now={cpu:d.cpu.percent,gpu:g?g.percent:null,vram:g?100*g.memory_used/g.memory_total:null,ram:m.percent};
  for(const k of METERS){if(now[k]===null)continue;history[k].push(now[k]);if(history[k].length>30)history[k].shift()}
  document.querySelectorAll('[data-meter]').forEach(a=>{const v=now[a.dataset.meter]??0;a.setAttribute('stroke-dasharray',`${v.toFixed(1)} 100`);a.setAttribute('class','meter fill '+level(v))});
  const rows=[['cpu','CPU',`${now.cpu.toFixed(0)}%`,`${d.cpu.threads} threads`],
    ['gpu','GPU',g?`${now.gpu.toFixed(0)}%`:'—',g?`${g.name} · ${g.temperature.toFixed(0)}°C`:'No NVIDIA GPU found'],
    ['vram','GPU memory',g?`${GB(g.memory_total-g.memory_used)} GB`:'—',g?`free of ${GB(g.memory_total)} GB`:''],
    ['ram','RAM',`${now.ram.toFixed(0)}%`,`${GB(m.used)} of ${GB(m.total)} GB · ${GB(m.total-m.used)} GB free`]];
  $('orb-title').textContent=`CPU ${rows[0][2]} · GPU ${rows[1][2]} · GPU memory ${rows[2][2]} free · RAM ${rows[3][2]}`;
  if(document.body.className==='orb')return;
  const live=$('live');live.replaceChildren();
  for(const [k,label,value,sub] of rows){const box=el('div','gauge'),top=el('div','top');box.tabIndex=0;box.title='Show what is using it';
    box.onclick=()=>details(k==='vram'?'gpu':k);box.onkeydown=e=>{if(e.key==='Enter')details(k==='vram'?'gpu':k)};const lv=now[k]===null?'':level(now[k]);
    top.append(el('span','label',label),el('span','val '+lv,value));box.append(top,spark(history[k],lv),el('div','sub',sub));live.append(box)}}
system();setInterval(system,2000);
// Details: every app's share of CPU, memory and GPU, refreshed every 2 s while open; click an app for its processes.
let tab='cpu',procTimer=null;const opened=new Set();
const MB=b=>b>=2**30?(b/2**30).toFixed(1)+' GB':(b/2**20).toFixed(0)+' MB',STATE={R:'running',S:'sleeping',D:'waiting on disk',Z:'zombie',T:'stopped',I:'idle',t:'traced'};
const since=t=>{const s=Date.now()/1000-t;return s<3600?Math.round(s/60)+' min':s<172800?Math.round(s/3600)+' h':Math.round(s/86400)+' d'};
const METRIC={cpu:{col:'CPU',key:'cpu',fmt:a=>a.cpu.toFixed(1)+'%',pct:a=>a.cpu},ram:{col:'Memory',key:'rss',fmt:a=>MB(a.rss),pct:a=>a.memory},
  gpu:{col:'GPU',key:'gpu_memory',fmt:a=>a.gpu?a.gpu.toFixed(0)+'%':MB(a.gpu_memory),pct:a=>a.gpu}};
function details(t){tab=t;$('drawer').classList.add('on');$('tabs').querySelectorAll('button').forEach(b=>b.classList.toggle('on',b.dataset.tab===tab));
  $('dcol').textContent=METRIC[tab].col;procs();clearInterval(procTimer);procTimer=setInterval(procs,2000)}
let actTimer=null;
const ago=s=>{const d=Math.max(0,Date.now()/1000-s);return d<60?Math.round(d)+'s':d<3600?Math.round(d/60)+'m':Math.round(d/3600)+'h'};
const span=s=>s<3600?Math.max(1,Math.round(s/60))+' min':s<86400?(s/3600).toFixed(1)+' h':(s/86400).toFixed(1)+' days';
const size=b=>!b?'':b<2**20?Math.max(1,Math.round(b/1024))+' KB':b<2**30?(b/2**20).toFixed(1)+' MB':GB(b)+' GB';
const MARK={indexed:'✓',failed:'✕',waiting:'…',skipped:'–',empty:'○'};
function showActivity(){$('activity').classList.add('on');activity();clearInterval(actTimer);actTimer=setInterval(activity,3000)}
function closeActivity(){$('activity').classList.remove('on');clearInterval(actTimer);actTimer=null}
$('aback').onclick=closeActivity;$('activity-link').onclick=showActivity;if(location.hash==='#activity')setTimeout(showActivity,300);
async function activity(){let d;try{d=await (await fetch('/api/activity')).json()}catch{$('asum').textContent='The local server is not responding.';return}
  const g=d.drive,st=$('stages'),feed=$('feed');st.replaceChildren();feed.replaceChildren();
  $('feed-label').style.display=feed.style.display=g?'':'none';
  if(g){const done=g.stages.reduce((a,s)=>a+s.done,0),now=g.stages.find(s=>s.done<s.files);
    $('asum').textContent=`Google Drive · ${num(done)} of ${num(g.files)} files · `+(!g.running?(done<g.files?'paused':'all read'):
      g.rate?`${g.rate.toFixed(1)} files/s`+(g.eta?` · about ${span(g.eta)} left at this pace`:''):
      ([...d.log].reverse().find(e=>e.source==='Google Drive')?.message||'checking Drive for changes')+'…');
    for(const s of g.stages){const r=el('div','stage'+(g.running&&s===now?' now':'')),row=el('div','row'),bar=el('div','bar'),i=el('i');
      row.append(el('span',null,s.name[0].toUpperCase()+s.name.slice(1)),el('span',null,`${num(s.done)} / ${num(s.files)} · ${s.files?Math.floor(100*s.done/s.files):100}%`));
      i.style.width=(s.files?100*s.done/s.files:100)+'%';bar.append(i);r.append(row,bar);st.append(r)}
    for(const f of g.recent){const r=el('div');r.title=f.note||f.state;r.append(el('span',f.state,MARK[f.state]||'·'),el('b',null,f.name||'(no name)'),el('span',null,[size(f.size),ago(f.done_at)].filter(Boolean).join(' · ')));feed.append(r)}}
  else $('asum').textContent='Google Drive is not connected.';
  const logs=$('logs'),today=new Date().toDateString();logs.replaceChildren();
  for(const e of [...d.log].reverse()){const t=new Date(e.time*1000),r=el('div');  // newest first
    r.append(el('time',null,(t.toDateString()===today?'':t.toLocaleDateString([], {month:'short',day:'numeric'})+' ')+t.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})),el('span',null,e.source),e.message);logs.append(r)}
  if(!d.log.length)logs.append(el('div',null,'No log lines yet.'))}
function closeDetails(){$('drawer').classList.remove('on');clearInterval(procTimer);procTimer=null}
$('back').onclick=closeDetails;if(location.hash.startsWith('#apps'))setTimeout(()=>details(location.hash.split(':')[1]||'cpu'),300);$('tabs').querySelectorAll('button').forEach(b=>b.onclick=()=>details(b.dataset.tab));
async function procs(){let d;try{d=await (await fetch('/api/processes')).json()}catch{$('dsum').textContent='Could not read processes.';return}
  const m=METRIC[tab],apps=d.apps.filter(a=>tab!=='gpu'||a.gpu||a.gpu_memory).sort((a,b)=>tab==='gpu'?(b.gpu-a.gpu)||(b.gpu_memory-a.gpu_memory):b[m.key]-a[m.key]);
  const cpuTotal=d.apps.reduce((s,a)=>s+a.cpu,0),vram=d.apps.reduce((s,a)=>s+a.gpu_memory,0);
  $('dsum').textContent=tab==='cpu'?`${num(d.count)} processes · ${num(d.apps_total)} apps · ${d.cpus} CPUs · % of the whole machine`
    :tab==='ram'?`${MB(d.apps.reduce((s,a)=>s+a.rss,0))} in these apps · ${MB(d.memory_total)} installed`:`${apps.length} apps on the GPU · ${MB(vram)} of GPU memory in use`;
  const top=Math.max(...apps.map(a=>tab==='gpu'?a.gpu_memory:a[m.key]),1e-9),list=$('dlist'),scroll=list.scrollTop;list.replaceChildren();
  if(!apps.length)list.append(el('div','dsum',tab==='gpu'?'Nothing is using the GPU right now.':'Nothing to show.'));
  for(const a of apps){const box=el('div','app'+(opened.has(a.name)?' open':'')),row=el('div','row'),name=el('div','nm',a.name);name.append(el('small',null,'×'+a.count));
    const p=m.pct(a),bar=el('div','bar'),fill=el('i');fill.style.width=Math.max(1,100*(tab==='gpu'?a.gpu_memory:a[m.key])/top)+'%';bar.append(fill);
    row.append(name,el('div','v '+(tab==='ram'?'':level(p)),m.fmt(a)),bar,el('div','more',`CPU ${a.cpu.toFixed(1)}% · RAM ${MB(a.rss)} (${a.memory.toFixed(1)}%)`+(a.gpu||a.gpu_memory?` · GPU ${a.gpu.toFixed(0)}% · ${MB(a.gpu_memory)} VRAM`:'')));
    row.onclick=()=>{box.classList.toggle('open');box.classList.contains('open')?opened.add(a.name):opened.delete(a.name)};
    const ps=el('div','procs');for(const p of a.processes){const r=el('div','proc'),head=el('div');
      head.append(el('b',null,'PID '+p.pid),` · ${p.user} · ${STATE[p.state]||p.state} · up ${since(p.started)} · ${p.threads} thread${p.threads>1?'s':''}`);
      r.append(head,el('div',null,`CPU ${p.cpu.toFixed(1)}% · RAM ${MB(p.rss)} (${p.memory.toFixed(2)}%)`+(p.gpu||p.gpu_memory?` · GPU ${p.gpu.toFixed(0)}% · ${MB(p.gpu_memory)} VRAM`:'')),el('div','cmd',p.command));ps.append(r)}
    if(a.count>a.processes.length)ps.append(el('div','proc',`…and ${a.count-a.processes.length} more`));box.append(row,ps);list.append(box)}
  list.scrollTop=scroll}
function type(node,text){return new Promise(done=>{let i=0;const t=setInterval(()=>{i=Math.min(text.length,i+4);node.textContent=text.slice(0,i);$('log').scrollTop=1e9;if(i>=text.length){clearInterval(t);done()}},14)})}
$('ask').addEventListener('submit',async e=>{e.preventDefault();const question=$('q').value.trim();if(!question||busy)return;busy=true;$('send').disabled=true;$('q').value='';
  const log=$('log');log.append(el('div','msg you',question));const reply=el('div','msg ai wait','Searching your files…');log.append(reply);log.scrollTop=1e9;
  document.body.classList.add('thinking');
  try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question})});const d=await r.json();
    reply.classList.remove('wait');await type(reply,d.answer||d.error||'No answer came back.');
    if(d.sources?.length){const chips=el('div','chips');for(const s of d.sources.slice(0,4)){const c=el('span',null,s.path.split('/').pop());c.title=s.path;chips.append(c)}reply.append(chips)}}
  catch{reply.classList.remove('wait');reply.textContent='The local server could not answer that.'}
  finally{document.body.classList.remove('thinking');busy=false;$('send').disabled=false;$('q').focus();log.scrollTop=1e9}});
</script></body></html>"""


@dataclass(frozen=True)
class ServerConfig:
    database: Path
    model: str
    ollama: str
    limit: int
    settings: Config | None = None  # enables meaning search
    token: str | None = None  # the browser extension's secret; form suggestions are off without one


def make_handler(config: ServerConfig):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RigvedaAI/0.1"

        def log_message(self, format: str, *args: object) -> None:
            logging.info("%s - %s", self.address_string(), format % args)

        def respond(self, status: HTTPStatus, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def trusted(self) -> bool:
            """Refuse requests addressed to another host name. A web page can point its own domain at 127.0.0.1
            (DNS rebinding) and would then be allowed to read the replies; its Host header gives it away."""
            if self.server.server_address[0] not in LOOPBACK:  # deliberately exposed with --host
                return True
            host = urlsplit("//" + self.headers.get("Host", "")).hostname or ""
            if host in LOOPBACK:
                return True
            self.respond(HTTPStatus.FORBIDDEN, {"error": "Open Rigveda AI at http://127.0.0.1 or http://localhost."})
            return False

        def page(self, html: str) -> None:
            data = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def body(self) -> dict:
            """The JSON request body. Requiring the JSON content type means other web sites cannot post here:
            browsers must ask permission first for that, and this server never grants it."""
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                raise ValueError("Send JSON with Content-Type: application/json.")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 20_000:
                raise ValueError("Request must be between 1 and 20,000 bytes.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Send a JSON object.")
            return payload

        def authorized(self) -> bool:
            """Form suggestions reveal your personal details, so they need the extension's secret token."""
            sent = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if config.token and sent and hmac.compare_digest(sent.encode(), config.token.encode()):
                return True
            self.respond(HTTPStatus.UNAUTHORIZED, {"error": "Missing or wrong token. Run `rigveda token` and paste it into the extension's options."})
            return False

        def voice_post(self) -> None:
            """A command typed in the orb's Voice tab, passed to the voice listener, which carries it out."""
            from . import voice

            if self.server.server_address[0] not in LOOPBACK and not self.authorized():
                return  # it opens apps and plays media, so from another device it needs the token
            try:
                text = self.body().get("text", "")
                if not isinstance(text, str) or not text.strip() or len(text) > voice.TYPED_LONGEST:
                    raise ValueError
            except (ValueError, json.JSONDecodeError):
                self.respond(HTTPStatus.BAD_REQUEST, {"error": f"Type 1–{voice.TYPED_LONGEST:,} characters."})
                return
            try:
                self.respond(HTTPStatus.OK, voice.send(text.strip()))
            except (OSError, ValueError):
                self.respond(HTTPStatus.SERVICE_UNAVAILABLE,
                             {"error": "Rigved isn't listening. Start it with `systemctl --user start rigveda-listen`."})

        def nearest_post(self) -> None:
            """Meaning search for the voice listener on this computer, so the vectors are in memory once, here.
            Only chunk ids go back, and only to this computer."""
            from . import embeddings
            from .store import connect

            if self.client_address[0] not in LOOPBACK or config.settings is None:
                self.respond(HTTPStatus.FORBIDDEN, {"error": "Only for Rigveda on this computer."})
                return
            try:
                payload = self.body()
                question, limit = payload.get("question"), payload.get("limit", 50)
                if not isinstance(question, str) or not question.strip() or not isinstance(limit, int) or not 0 < limit <= 200:
                    raise ValueError
            except (ValueError, json.JSONDecodeError):
                self.respond(HTTPStatus.BAD_REQUEST, {"error": "Send a question and a limit of 1–200."})
                return
            con = connect(config.database)
            try:
                ids = embeddings.nearest(con, config.settings, question, limit)
                database = con.execute("PRAGMA database_list").fetchone()[2]
            except OllamaError as error:
                self.respond(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
                return
            finally:
                con.close()
            self.respond(HTTPStatus.OK, {"ids": ids, "database": database})

        def suggest_post(self) -> None:
            try:
                fields = self.body().get("fields")
                if not isinstance(fields, list) or not 0 < len(fields) <= 300 or not all(isinstance(f, dict) for f in fields):
                    raise ValueError("Send 1 to 300 fields.")
            except (ValueError, json.JSONDecodeError) as error:
                self.respond(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            con = connect(config.database)
            try:
                self.respond(HTTPStatus.OK, {"results": [suggest.suggestions(con, field) for field in fields]})
            finally:
                con.close()

        def write_post(self) -> None:
            try:
                payload = self.body()
                for name in ("before", "after", "instruction", "title", "site"):
                    if not isinstance(payload.get(name, ""), str):
                        raise ValueError(f"{name} must be text.")
                if not isinstance(payload.get("field") or {}, dict):
                    raise ValueError("field must be an object.")
                if not (payload.get("before", "").strip() or payload.get("instruction", "").strip()
                        or write.field_question(payload.get("field") or {})):
                    raise ValueError("Say what to write, or send the text written so far.")
            except (ValueError, json.JSONDecodeError) as error:
                self.respond(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            settings = config.settings or Config(database=str(config.database), model=config.model, ollama=config.ollama)
            try:
                self.respond(HTTPStatus.OK, write.write(config.database, settings, payload))
            except OllamaError as error:
                self.respond(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})

        def do_GET(self) -> None:
            if not self.trusted():
                return
            if self.path == "/api/suggest/status":
                if self.authorized():
                    con = connect(config.database)
                    try:
                        usable = len(facts.usable(con))
                    finally:
                        con.close()
                    self.respond(HTTPStatus.OK, {"ok": True, "facts": usable})
                return
            if self.path == "/write":
                self.page(WRITE_HTML)
                return
            if self.path == "/test-form":
                self.page(TEST_FORM_HTML)
                return
            if self.path == "/facts":
                self.page(FACTS_HTML)
                return
            if self.path == "/status":
                self.page(STATUS_HTML)
                return
            if urlsplit(self.path).path == "/orb":
                self.page(ORB_HTML)
                return
            if self.path == "/api/processes":
                self.respond(HTTPStatus.OK, status.processes())
                return
            if self.path == "/api/system":
                self.respond(HTTPStatus.OK, status.system())
                return
            if self.path == "/api/voice":
                from .voice import history
                self.respond(HTTPStatus.OK, {"entries": history()})
                return
            if self.path == "/api/activity":
                con = connect(config.database)
                try:
                    self.respond(HTTPStatus.OK, status.activity(con))
                finally:
                    con.close()
                return
            if self.path == "/api/status":
                con = connect(config.database)
                try:
                    self.respond(HTTPStatus.OK, status.collect(con, config.ollama))
                finally:
                    con.close()
                return
            if self.path == "/api/facts":
                con = connect(config.database)
                try:
                    self.respond(HTTPStatus.OK, {"facts": facts.listing(con), "keys": facts.KEYS, "home": str(Path.home())})
                finally:
                    con.close()
                return
            if self.path == "/":
                data = INDEX_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path == "/api/health":
                try:
                    con = connect(config.database)
                    count = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
                    con.close()
                    self.respond(HTTPStatus.OK, {"documents": count, "model": config.model})
                except Exception as error:  # pragma: no cover - unexpected filesystem failures
                    self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
                return
            self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def facts_post(self) -> None:
            try:
                payload = self.body()
            except (ValueError, json.JSONDecodeError) as error:
                self.respond(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            con = connect(config.database)
            try:
                if self.path == "/api/facts":
                    fact_id = facts.add_manual(con, str(payload.get("key", "")), str(payload.get("value", "")))
                else:
                    value = payload.get("value")
                    fact_id = facts.decide(con, int(self.path.rsplit("/", 1)[1]), str(payload.get("status", "")),
                                           None if value is None else str(value))
                self.respond(HTTPStatus.OK, {"id": fact_id})
            except KeyError:
                self.respond(HTTPStatus.NOT_FOUND, {"error": "No such fact."})
            except ValueError as error:
                self.respond(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            finally:
                con.close()

        def do_POST(self) -> None:
            if not self.trusted():
                return
            if self.path == "/api/suggest":
                if self.authorized():
                    self.suggest_post()
                return
            if self.path == "/api/write":
                self.write_post()
                return
            if self.path == "/api/facts" or re.fullmatch(r"/api/facts/\d+", self.path):
                self.facts_post()
                return
            if self.path == "/api/voice":
                self.voice_post()
                return
            if self.path == "/api/nearest":
                self.nearest_post()
                return
            if self.path != "/api/chat":
                self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            try:
                payload = self.body()
                question = payload.get("question", "")
                if not isinstance(question, str):
                    raise ValueError("Question must be text.")
                question = question.strip()
                if not question or len(question) > 6_000:
                    raise ValueError("Question must contain 1–6,000 characters.")
            except (ValueError, json.JSONDecodeError):
                self.respond(HTTPStatus.BAD_REQUEST, {"error": "Send a valid question under 6,000 characters."})
                return
            talked = conversation.messages()  # so a follow-up ("and the deposit?") finds the right files
            sources = search(config.database, conversation.standalone(question, config.ollama, config.model),
                             config.limit, config.settings)
            if not sources:
                self.respond(HTTPStatus.OK, {"answer": "I could not find matching indexed text. Index a folder first, or try different terms.", "sources": []})
                return
            try:
                answer = ask(question, sources, config.model, config.ollama, talked)
            except OllamaError as error:
                self.respond(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error), "sources": []})
                return
            conversation.add(question, answer, "chat")
            self.respond(HTTPStatus.OK, {"answer": answer, "sources": [{"path": row["path"], "chunk": row["ordinal"] + 1, "cite": cite(row)} for row in sources]})

    return Handler


def run_server(config: ServerConfig, host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(config))
    print(f"Rigveda AI is listening at http://{host}:{port}")
    if config.settings is not None:  # the first question shouldn't be the one that waits for every vector to load
        import threading

        from . import embeddings

        def warm() -> None:
            started = time.monotonic()
            try:
                embeddings.warm(config.database, config.settings)
                print(f"Meaning search ready ({time.monotonic() - started:.0f} s to load).", flush=True)
            except Exception as error:
                print(f"Couldn't load meaning search ahead of time ({type(error).__name__}: {error}).", flush=True)

        threading.Thread(target=warm, daemon=True).start()
    print("Press Ctrl+C to stop the server.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        httpd.server_close()
