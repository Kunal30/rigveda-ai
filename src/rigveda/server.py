"""Local HTTP server and browser UI for Rigveda AI."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .cli import ask, search


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rigveda AI</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#121a30;--line:#253250;--text:#edf2ff;--muted:#a7b5d6;--accent:#7c9cff;--user:#263b79}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#182651,var(--bg) 45%);color:var(--text);font:16px system-ui,sans-serif}
main{max-width:860px;margin:auto;min-height:100vh;padding:28px 18px;display:flex;flex-direction:column}header{padding:10px 6px 24px}h1{margin:0;font-size:1.7rem;letter-spacing:-.04em}header p{color:var(--muted);margin:.35rem 0 0}.status{font-size:.8rem;color:var(--muted)}
#messages{flex:1;display:flex;flex-direction:column;gap:16px}.message{max-width:86%;padding:14px 16px;border:1px solid var(--line);border-radius:16px;background:var(--panel);white-space:pre-wrap;line-height:1.5}.message.user{align-self:flex-end;background:var(--user)}.sources{font-size:.78rem;color:var(--muted);margin-top:12px;word-break:break-all}.sources b{color:var(--text)}
form{display:flex;gap:10px;margin-top:22px;padding:10px;border:1px solid var(--line);border-radius:18px;background:rgba(18,26,48,.9);position:sticky;bottom:12px}textarea{font:inherit;color:inherit;background:transparent;border:0;resize:none;outline:0;min-height:44px;max-height:150px;flex:1;padding:10px}button{align-self:end;background:var(--accent);color:#07102d;border:0;border-radius:12px;font-weight:700;padding:12px 16px;cursor:pointer}button:disabled{opacity:.5;cursor:wait}.empty{color:var(--muted);text-align:center;margin-top:14vh}
</style></head><body><main><header><h1>Rigveda AI</h1><p>Private answers from your selected local files.</p><span class="status" id="status">Checking local index…</span></header><section id="messages"><p class="empty">Ask a question about the folders you indexed.</p></section><form id="chat"><textarea id="question" placeholder="Ask about your files…" aria-label="Question" autofocus></textarea><button id="send">Send</button></form></main>
<script>
const messages=document.querySelector('#messages'), form=document.querySelector('#chat'), question=document.querySelector('#question'), send=document.querySelector('#send'), status=document.querySelector('#status');
function message(text, user, sources=[]){const box=document.createElement('article');box.className='message '+(user?'user':'assistant');box.textContent=text;if(sources.length){const source=document.createElement('div');source.className='sources';source.innerHTML='<b>Sources</b><br>'+sources.map(s=>s.path+' (chunk '+s.chunk+')').join('<br>');box.append(source)}messages.querySelector('.empty')?.remove();messages.append(box);box.scrollIntoView({behavior:'smooth',block:'end'});return box}
async function health(){try{const r=await fetch('/api/health');const d=await r.json();status.textContent=d.documents+' indexed file'+(d.documents===1?'':'s')+' · '+d.model}catch{status.textContent='Local server is unavailable'}}
form.addEventListener('submit',async e=>{e.preventDefault();const text=question.value.trim();if(!text||send.disabled)return;message(text,true);question.value='';send.disabled=true;send.textContent='Thinking…';const pending=message('Searching your local index…',false);try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:text})});const d=await r.json();pending.textContent=d.answer||d.error||'No answer returned.';if(d.sources?.length){const s=document.createElement('div');s.className='sources';s.innerHTML='<b>Sources</b><br>'+d.sources.map(x=>x.path+' (chunk '+x.chunk+')').join('<br>');pending.append(s)}}catch{pending.textContent='The local server could not complete that request.'}finally{send.disabled=false;send.textContent='Send';question.focus()}});
question.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();form.requestSubmit()}});health();
</script></body></html>"""


@dataclass(frozen=True)
class ServerConfig:
    database: Path
    model: str
    ollama: str
    limit: int


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

        def do_GET(self) -> None:
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
                    from .cli import connect
                    con = connect(config.database)
                    count = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
                    con.close()
                    self.respond(HTTPStatus.OK, {"documents": count, "model": config.model})
                except Exception as error:  # pragma: no cover - unexpected filesystem failures
                    self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
                return
            self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self) -> None:
            if self.path != "/api/chat":
                self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 20_000:
                    raise ValueError("Request must be between 1 and 20,000 bytes.")
                payload = json.loads(self.rfile.read(length))
                question = payload.get("question", "")
                if not isinstance(question, str):
                    raise ValueError("Question must be text.")
                question = question.strip()
                if not question or len(question) > 6_000:
                    raise ValueError("Question must contain 1–6,000 characters.")
            except (ValueError, json.JSONDecodeError):
                self.respond(HTTPStatus.BAD_REQUEST, {"error": "Send a valid question under 6,000 characters."})
                return
            sources = search(config.database, question, config.limit)
            if not sources:
                self.respond(HTTPStatus.OK, {"answer": "I could not find matching indexed text. Index a folder first, or try different terms.", "sources": []})
                return
            try:
                answer = ask(question, sources, config.model, config.ollama)
            except RuntimeError as error:
                self.respond(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error), "sources": []})
                return
            self.respond(HTTPStatus.OK, {"answer": answer, "sources": [{"path": row["path"], "chunk": row["ordinal"] + 1} for row in sources]})

    return Handler


def run_server(config: ServerConfig, host: str = "127.0.0.1", port: int = 8000) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(config))
    print(f"Rigveda AI is listening at http://{host}:{port}")
    print("Press Ctrl+C to stop the server.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        httpd.server_close()
