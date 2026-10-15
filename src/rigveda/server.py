"""Local HTTP server and browser UI for Rigveda AI."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import facts
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
</style></head><body><main><header><h1>Rigveda AI</h1><p>Private answers from your selected local files. <a href="/facts" style="color:var(--accent)">Your facts profile →</a></p><span class="status" id="status">Checking local index…</span></header><section id="messages"><p class="empty">Ask a question about your files: documents, slides, images, audio and video.</p></section><form id="chat"><textarea id="question" placeholder="Ask about your files…" aria-label="Question" autofocus></textarea><button id="send">Send</button></form></main>
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
<p>Details found in your files. Only <b>approved</b> facts are used to fill forms. Check each against the words it came from. <a href="/">← Ask your files</a></p></header>
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


@dataclass(frozen=True)
class ServerConfig:
    database: Path
    model: str
    ollama: str
    limit: int
    settings: Config | None = None  # enables meaning search


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

        def do_GET(self) -> None:
            if not self.trusted():
                return
            if self.path == "/facts":
                self.page(FACTS_HTML)
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
            if self.path == "/api/facts" or re.fullmatch(r"/api/facts/\d+", self.path):
                self.facts_post()
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
            sources = search(config.database, question, config.limit, config.settings)
            if not sources:
                self.respond(HTTPStatus.OK, {"answer": "I could not find matching indexed text. Index a folder first, or try different terms.", "sources": []})
                return
            try:
                answer = ask(question, sources, config.model, config.ollama)
            except OllamaError as error:
                self.respond(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error), "sources": []})
                return
            self.respond(HTTPStatus.OK, {"answer": answer, "sources": [{"path": row["path"], "chunk": row["ordinal"] + 1, "cite": cite(row)} for row in sources]})

    return Handler


def run_server(config: ServerConfig, host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(config))
    print(f"Rigveda AI is listening at http://{host}:{port}")
    print("Press Ctrl+C to stop the server.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        httpd.server_close()
