"""
XClaw Web Interface — v3.

New endpoints:
  POST /upload             → ingest a file into the knowledge base
  GET  /stream/{sid}       → SSE token streaming (LLM response as it types)
  GET  /metrics            → telemetry JSON snapshot
  GET  /traces             → recent execution traces
  GET  /kb/sources         → knowledge base document list
  GET  /kb/search          → knowledge base query
  GET  /tools              → list registered tools
  WS   /ws/{sid}           → real-time progress events (agent steps, tool calls)

Dashboard features:
  • Live tool-call feed     → see which tools are firing in real-time
  • Provider health sidebar → circuit breaker status per LLM
  • Knowledge base panel    → upload + search documents
  • Token/latency metrics   → in dashboard header
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain.llm_router import LLMRouter
    from core.commander import ProgressHub
    from core.gateway import Gateway
    from core.knowledge_base import KnowledgeBase
    from core.memory import Memory
    from core.telemetry import Telemetry
    from core.tool_registry import ToolRegistry

_HTML_PATH = Path(__file__).parent / "index.html"


def create_app(
    gateway: "Gateway",
    memory: "Memory",
    hub: "ProgressHub | None" = None,
    llm_router: "LLMRouter | None" = None,
    telemetry: "Telemetry | None" = None,
    kb: "KnowledgeBase | None" = None,
    tools: "ToolRegistry | None" = None,
):
    try:
        from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
        from pydantic import BaseModel
    except ImportError as exc:
        raise ImportError("FastAPI not installed. Run: pip install fastapi uvicorn websockets") from exc

    app = FastAPI(title="XClaw", version="3.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    class ChatRequest(BaseModel):
        text: str
        session_id: str = ""

    # ── Dashboard ──────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        if _HTML_PATH.exists():
            return HTMLResponse(_HTML_PATH.read_text())
        return HTMLResponse(_DASHBOARD_HTML)

    # ── Chat ───────────────────────────────────────────────────────────

    @app.post("/chat")
    async def chat(req: ChatRequest):
        session_id = req.session_id or f"web-{uuid.uuid4().hex[:8]}"
        response = await gateway.handle(req.text, "web", session_id)
        return JSONResponse({
            "text": response.text,
            "requires_approval": response.requires_approval,
            "session_id": session_id,
        })

    # ── SSE streaming ──────────────────────────────────────────────────

    @app.get("/stream/{session_id}")
    async def stream_response(session_id: str, prompt: str):
        """Stream an LLM response token-by-token via Server-Sent Events."""
        if not llm_router:
            return JSONResponse({"error": "LLM not configured"}, status_code=503)

        async def event_generator():
            try:
                async for token in llm_router.stream(prompt, session_id=session_id):
                    yield f"data: {json.dumps({'token': token})}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── WebSocket progress streaming ───────────────────────────────────

    @app.websocket("/ws/{session_id}")
    async def ws_progress(websocket: WebSocket, session_id: str):
        if hub is None:
            await websocket.accept()
            await websocket.send_text(json.dumps({"type": "error", "message": "Hub not configured"}))
            await websocket.close()
            return

        await websocket.accept()
        q = hub.subscribe(session_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=120)
                    await websocket.send_text(json.dumps(event))
                    if event.get("type") in {"done", "agent_done"}:
                        break
                except asyncio.TimeoutError:
                    await websocket.send_text(json.dumps({"type": "ping"}))
        except WebSocketDisconnect:
            pass
        finally:
            hub.unsubscribe(session_id)

    # ── File upload → Knowledge Base ───────────────────────────────────

    @app.post("/upload")
    async def upload_file(file: UploadFile = File(...)):
        if not kb:
            return JSONResponse({"error": "Knowledge base not configured"}, status_code=503)
        try:
            import tempfile
            content = await file.read()
            with tempfile.NamedTemporaryFile(suffix=Path(file.filename or "upload.txt").suffix, delete=False) as tmp:
                tmp.write(content)
                tmp_path = Path(tmp.name)
            result = kb.ingest_file(tmp_path, tags=["upload"])
            tmp_path.unlink(missing_ok=True)
            return JSONResponse({"result": result, "filename": file.filename})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    # ── Knowledge Base ─────────────────────────────────────────────────

    @app.get("/kb/sources")
    async def kb_sources():
        if not kb:
            return JSONResponse([])
        return JSONResponse(kb.list_sources())

    @app.get("/kb/search")
    async def kb_search(query: str, limit: int = 5):
        if not kb:
            return JSONResponse({"error": "KB not configured"}, status_code=503)
        results = kb.search(query, limit)
        return JSONResponse({"query": query, "results": results})

    # ── Data endpoints ─────────────────────────────────────────────────

    @app.get("/tasks")
    async def tasks_endpoint(session_id: str):
        return JSONResponse(memory.get_tasks(session_id))

    @app.get("/history")
    async def history_endpoint(session_id: str, limit: int = 10):
        return JSONResponse(memory.get_executions(session_id, limit))

    @app.get("/tools")
    async def tools_endpoint():
        if not tools:
            return JSONResponse([])
        return JSONResponse(tools.tool_names())

    @app.get("/providers")
    async def providers_endpoint():
        if llm_router:
            return JSONResponse(llm_router.provider_status())
        return JSONResponse([])

    @app.get("/metrics")
    async def metrics_endpoint():
        if telemetry:
            return JSONResponse(telemetry.snapshot())
        return JSONResponse({"error": "Telemetry not configured"})

    @app.get("/traces")
    async def traces_endpoint(limit: int = 20):
        if telemetry:
            return JSONResponse(telemetry.recent_traces(limit))
        return JSONResponse([])

    @app.get("/health")
    async def health():
        providers = llm_router.available_providers() if llm_router else []
        return JSONResponse({"status": "ok", "version": "3.0", "providers": providers})

    return app


# ── Embedded dashboard ─────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>XClaw v3 — NavOS</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#070707;color:#d0d0d0;height:100vh;display:flex;flex-direction:column;overflow:hidden;font-size:13px}
header{padding:.6rem 1rem;border-bottom:1px solid #1a1a1a;display:flex;align-items:center;gap:.8rem;flex-shrink:0;background:#0a0a0a}
header h1{font-size:1rem;font-weight:700;color:#fff;letter-spacing:.4px}
.badge{font-size:.68rem;padding:.15rem .45rem;border-radius:3px;background:#111;border:1px solid #2a2a2a;color:#666}
.badge.ok{border-color:#14532d;color:#4ade80}.badge.warn{border-color:#78350f;color:#fbbf24}.badge.err{border-color:#7f1d1d;color:#f87171}
#hdr-right{margin-left:auto;display:flex;gap:.4rem;align-items:center}
#body{flex:1;display:flex;overflow:hidden}
#chat-area{flex:1;display:flex;flex-direction:column;overflow:hidden}
#messages{flex:1;overflow-y:auto;padding:.8rem 1rem;display:flex;flex-direction:column;gap:.6rem}
.msg{max-width:80%;padding:.55rem .8rem;border-radius:8px;line-height:1.55;white-space:pre-wrap;word-break:break-word}
.msg.user{background:#1a2f4a;align-self:flex-end;border:1px solid #1e3a5f}
.msg.xclaw{background:#0f0f0f;border:1px solid #1e1e1e;align-self:flex-start}
.msg.xclaw.plan{border-color:#b45309}
.msg.event{background:transparent;border:none;color:#3b5998;font-size:.75rem;align-self:flex-start;padding:.1rem .5rem}
.msg.event.tool{color:#1e40af}.msg.event.done{color:#166534}
.approval-btns{display:flex;gap:.35rem;margin-top:.5rem}
.btn{padding:.3rem .7rem;border-radius:5px;border:none;cursor:pointer;font-size:.78rem;font-weight:600;transition:opacity .15s}
.btn:hover{opacity:.85}.btn.approve{background:#15803d;color:#fff}.btn.cancel{background:#b91c1c;color:#fff}
#sidebar{width:210px;border-left:1px solid #141414;display:flex;flex-direction:column;overflow:hidden;flex-shrink:0}
.panel{padding:.6rem .7rem;border-bottom:1px solid #141414}
.panel h3{font-size:.65rem;color:#444;text-transform:uppercase;letter-spacing:.5px;margin-bottom:.35rem}
.provider-row{display:flex;align-items:center;gap:.3rem;padding:.15rem 0}
.dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.dot.up{background:#4ade80}.dot.down{background:#ef4444}.dot.unknown{background:#555}
.tool-tag{display:inline-block;background:#111;border:1px solid #222;border-radius:3px;padding:.1rem .3rem;font-size:.68rem;color:#6b7280;margin:.1rem}
.tool-tag.firing{border-color:#2563eb;color:#93c5fd;background:#0f1729}
#kb-section{flex:1;overflow-y:auto;padding:.5rem .7rem}
#drop-zone{border:1px dashed #2a2a2a;border-radius:6px;padding:.6rem;text-align:center;color:#555;font-size:.75rem;cursor:pointer;transition:border-color .2s}
#drop-zone:hover,#drop-zone.over{border-color:#2563eb;color:#93c5fd}
#kb-list{margin-top:.4rem;font-size:.72rem}
.kb-source{padding:.15rem 0;color:#666;border-bottom:1px solid #0f0f0f}
form{display:flex;gap:.35rem;padding:.6rem .8rem;border-top:1px solid #141414;flex-shrink:0;background:#0a0a0a}
input[type=text]{flex:1;background:#0f0f0f;border:1px solid #222;color:#e0e0e0;padding:.45rem .7rem;border-radius:6px;font-size:.88rem;outline:none}
input[type=text]:focus{border-color:#2563eb}
button[type=submit]{background:#2563eb;color:#fff;border:none;padding:.45rem .9rem;border-radius:6px;cursor:pointer;font-weight:600;font-size:.82rem}
button[type=submit]:disabled{opacity:.35;cursor:default}
#status-line{font-size:.72rem;color:#444;padding:.2rem .8rem;min-height:1.1rem;flex-shrink:0}
</style>
</head>
<body>
<header>
  <h1>⚡ XClaw <span style="color:#444;font-weight:400;font-size:.75rem">v3</span></h1>
  <span id="sid-label" style="color:#333;font-size:.72rem"></span>
  <div id="hdr-right">
    <span class="badge" id="ws-badge">WS —</span>
    <span class="badge" id="iter-badge">0 iterations</span>
    <span class="badge" id="tool-count-badge">0 tools</span>
  </div>
</header>
<div id="body">
  <div id="chat-area">
    <div id="messages"></div>
    <div id="status-line"></div>
    <form id="form">
      <input type="text" id="input" placeholder="Tell XClaw what you need — or /help for commands" autocomplete="off" autofocus>
      <button type="submit" id="send-btn">Send</button>
    </form>
  </div>
  <div id="sidebar">
    <div class="panel">
      <h3>Providers</h3>
      <div id="providers-list"><span style="color:#333">loading…</span></div>
    </div>
    <div class="panel">
      <h3>Active Tools</h3>
      <div id="tools-list"><span style="color:#333">—</span></div>
    </div>
    <div id="kb-section">
      <div class="panel" style="padding:0;border:none">
        <h3 style="margin-bottom:.35rem">Knowledge Base</h3>
        <div id="drop-zone" onclick="document.getElementById('file-in').click()">
          Drop file or click to upload
          <input type="file" id="file-in" style="display:none" multiple>
        </div>
        <div id="kb-list"></div>
      </div>
    </div>
  </div>
</div>

<script>
let sessionId = '';
let ws = null;
let iterations = 0;
let activeTools = new Set();

const msgs      = document.getElementById('messages');
const form      = document.getElementById('form');
const inp       = document.getElementById('input');
const sendBtn   = document.getElementById('send-btn');
const wsBadge   = document.getElementById('ws-badge');
const iterBadge = document.getElementById('iter-badge');
const tcBadge   = document.getElementById('tool-count-badge');
const statusLine= document.getElementById('status-line');

// ── Render ─────────────────────────────────────────────────────────────────

function addMsg(text, cls, withApproval) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  div.textContent = text;
  if (withApproval) {
    const btns = document.createElement('div');
    btns.className = 'approval-btns';
    [['✅ Approve','approve','yes'],['❌ Cancel','cancel','no']].forEach(([label, c, val]) => {
      const b = document.createElement('button');
      b.className = 'btn ' + c; b.textContent = label;
      b.onclick = () => { document.querySelectorAll('.approval-btns').forEach(e=>e.remove()); send(val); };
      btns.appendChild(b);
    });
    div.appendChild(btns);
    div.classList.add('plan');
  }
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
  return div;
}

function addEvent(text, cls = '') {
  const div = document.createElement('div');
  div.className = 'msg event ' + cls;
  div.textContent = text;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

function setStatus(text) { statusLine.textContent = text; }

// ── WebSocket ──────────────────────────────────────────────────────────────

function connectWS(sid) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/${sid}`);
  ws.onopen  = () => { wsBadge.textContent = 'WS ✓'; wsBadge.className = 'badge ok'; };
  ws.onclose = () => { wsBadge.textContent = 'WS ✗'; wsBadge.className = 'badge err'; ws = null; };
  ws.onmessage = ({data}) => {
    const ev = JSON.parse(data);
    if (ev.type === 'ping') return;

    if (ev.type === 'agent_start') {
      setStatus(`Running agentic loop… tools: ${ev.tools?.join(', ')}`);
      iterations = 0;
    } else if (ev.type === 'tool_calls') {
      iterations = ev.iteration || iterations;
      iterBadge.textContent = `${iterations} iterations`;
      ev.tools?.forEach(t => { activeTools.add(t); renderTools(); });
      addEvent(`→ ${ev.tools?.join(', ')}`, 'tool');
      setStatus(`Iteration ${ev.iteration}: calling ${ev.tools?.join(', ')}`);
    } else if (ev.type === 'tool_done') {
      addEvent(`✓ ${ev.tool}: ${ev.preview?.slice(0,80)}…`);
      setTimeout(() => { activeTools.delete(ev.tool); renderTools(); }, 1500);
    } else if (ev.type === 'agent_done') {
      addEvent(`Done in ${ev.iterations} iteration(s)`, 'done');
      setStatus('');
      sendBtn.disabled = false;
    } else if (ev.type === 'wave_start') {
      setStatus(`Wave ${ev.wave}/${ev.total_waves}: ${ev.steps?.map(s=>s.agent).join(', ')}`);
    } else if (ev.type === 'step_done') {
      addEvent(`✓ Step ${ev.step_id} [${ev.agent}]`);
    } else if (ev.type === 'done') {
      setStatus('');
      sendBtn.disabled = false;
    }
  };
}

function renderTools() {
  const el = document.getElementById('tools-list');
  if (activeTools.size === 0) { el.innerHTML = '<span style="color:#333">—</span>'; return; }
  el.innerHTML = [...activeTools].map(t =>
    `<span class="tool-tag firing">${t}</span>`
  ).join('');
}

// ── Send ───────────────────────────────────────────────────────────────────

async function send(text) {
  addMsg(text, 'user', false);
  inp.value = '';
  sendBtn.disabled = true;
  setStatus('Thinking…');
  activeTools.clear(); renderTools();

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({text, session_id: sessionId}),
    });
    const data = await res.json();

    if (!sessionId) {
      sessionId = data.session_id;
      document.getElementById('sid-label').textContent = sessionId;
      connectWS(sessionId);
    }

    setStatus('');
    addMsg(data.text, 'xclaw', data.requires_approval);
    if (!data.requires_approval) sendBtn.disabled = false;
  } catch (err) {
    setStatus('');
    addMsg('Network error: ' + err.message, 'xclaw', false);
    sendBtn.disabled = false;
  }
}

// ── File upload ────────────────────────────────────────────────────────────

const dropZone = document.getElementById('drop-zone');
const fileIn = document.getElementById('file-in');

dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('over'));
dropZone.addEventListener('drop', e => { e.preventDefault(); dropZone.classList.remove('over'); uploadFiles(e.dataTransfer.files); });
fileIn.addEventListener('change', () => uploadFiles(fileIn.files));

async function uploadFiles(files) {
  for (const file of files) {
    const fd = new FormData(); fd.append('file', file);
    try {
      const res = await fetch('/upload', {method:'POST', body:fd});
      const data = await res.json();
      addMsg(data.result || data.error, 'xclaw', false);
      refreshKB();
    } catch (err) { addMsg('Upload failed: '+err.message, 'xclaw', false); }
  }
}

// ── Sidebar refresh ────────────────────────────────────────────────────────

async function refreshProviders() {
  try {
    const data = await (await fetch('/providers')).json();
    const el = document.getElementById('providers-list');
    if (!data.length) { el.innerHTML = '<span style="color:#333">none</span>'; return; }
    el.innerHTML = data.map(p =>
      `<div class="provider-row"><span class="dot ${p.available?'up':p.circuit_open?'down':'unknown'}"></span><span style="color:${p.available?'#aaa':'#444'}">${p.provider}</span></div>`
    ).join('');
  } catch {}
}

async function refreshKB() {
  try {
    const sources = await (await fetch('/kb/sources')).json();
    const el = document.getElementById('kb-list');
    if (!sources.length) { el.innerHTML = '<div style="color:#333;font-size:.7rem;margin-top:.3rem">empty</div>'; return; }
    el.innerHTML = sources.map(s =>
      `<div class="kb-source">${s.source} <span style="color:#333">(${s.chunks})</span></div>`
    ).join('');
  } catch {}
}

async function refreshToolCount() {
  try {
    const tools = await (await fetch('/tools')).json();
    tcBadge.textContent = `${tools.length} tools`;
  } catch {}
}

// ── Init ───────────────────────────────────────────────────────────────────

form.onsubmit = e => { e.preventDefault(); const t=inp.value.trim(); if(t && !sendBtn.disabled) send(t); };
refreshProviders(); refreshKB(); refreshToolCount();
setInterval(refreshProviders, 15000);
</script>
</body>
</html>
"""
