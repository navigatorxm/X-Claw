"""
XClaw Web Interface — FastAPI + WebSocket streaming dashboard.

v2 upgrades:
  • WebSocket /ws/{session_id} — real-time step-by-step progress streaming
  • /providers               — live LLM provider health status
  • CORS headers             — works behind a reverse proxy
  • Richer dashboard HTML    — shows wave progress, step ticks, provider status

Endpoints:
  GET  /                    → dark-mode dashboard
  POST /chat                → send message, returns plan or final result
  WS   /ws/{session_id}     → stream execution progress events
  GET  /tasks               → list tasks for a session
  GET  /history             → list recent executions
  GET  /providers           → LLM provider status
  GET  /health              → health check
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
    from core.memory import Memory

_HTML_PATH = Path(__file__).parent / "index.html"


def create_app(
    gateway: "Gateway",
    memory: "Memory",
    hub: "ProgressHub | None" = None,
    llm_router: "LLMRouter | None" = None,
):
    """Factory — returns a configured FastAPI application."""
    try:
        from fastapi import FastAPI, WebSocket, WebSocketDisconnect
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import HTMLResponse, JSONResponse
        from pydantic import BaseModel
    except ImportError as exc:
        raise ImportError("FastAPI not installed. Run: pip install fastapi uvicorn websockets") from exc

    app = FastAPI(title="XClaw", version="2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    class ChatRequest(BaseModel):
        text: str
        session_id: str = ""

    # ------------------------------------------------------------------
    # Static dashboard
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        if _HTML_PATH.exists():
            return HTMLResponse(_HTML_PATH.read_text())
        return HTMLResponse(_DASHBOARD_HTML)

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------

    @app.post("/chat")
    async def chat(req: ChatRequest):
        session_id = req.session_id or f"web-{uuid.uuid4().hex[:8]}"
        response = await gateway.handle(req.text, "web", session_id)
        return JSONResponse({
            "text": response.text,
            "requires_approval": response.requires_approval,
            "session_id": session_id,
        })

    # ------------------------------------------------------------------
    # WebSocket streaming
    # ------------------------------------------------------------------

    @app.websocket("/ws/{session_id}")
    async def ws_progress(websocket: WebSocket, session_id: str):
        """
        Stream execution progress events to the browser.
        Events are JSON objects with a "type" field:
          plan_start, wave_start, step_done, done, error
        """
        if hub is None:
            await websocket.accept()
            await websocket.send_text(json.dumps({"type": "error", "message": "Progress hub not configured"}))
            await websocket.close()
            return

        await websocket.accept()
        q = hub.subscribe(session_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=120)
                    await websocket.send_text(json.dumps(event))
                    if event.get("type") == "done":
                        break
                except asyncio.TimeoutError:
                    # Send keep-alive ping
                    await websocket.send_text(json.dumps({"type": "ping"}))
        except WebSocketDisconnect:
            pass
        finally:
            hub.unsubscribe(session_id)

    # ------------------------------------------------------------------
    # Data endpoints
    # ------------------------------------------------------------------

    @app.get("/tasks")
    async def tasks(session_id: str):
        return JSONResponse(memory.get_tasks(session_id))

    @app.get("/history")
    async def history(session_id: str, limit: int = 10):
        return JSONResponse(memory.get_executions(session_id, limit))

    @app.get("/providers")
    async def providers():
        if llm_router:
            return JSONResponse(llm_router.provider_status())
        return JSONResponse([])

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok", "version": "2.0"})

    return app


# ── Embedded dashboard HTML ────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>XClaw — NavOS</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',system-ui,sans-serif;background:#080808;color:#d4d4d4;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  header{padding:.8rem 1.2rem;border-bottom:1px solid #1e1e1e;display:flex;align-items:center;gap:1rem;flex-shrink:0}
  header h1{font-size:1.1rem;font-weight:700;color:#fff;letter-spacing:.5px}
  #status-bar{display:flex;gap:.5rem;align-items:center;margin-left:auto}
  .badge{font-size:.7rem;padding:.2rem .5rem;border-radius:4px;background:#1a1a1a;border:1px solid #333;color:#888}
  .badge.ok{border-color:#166534;color:#4ade80}
  .badge.fail{border-color:#7f1d1d;color:#f87171}
  #body{flex:1;display:flex;overflow:hidden}
  #messages{flex:1;overflow-y:auto;padding:1rem 1.2rem;display:flex;flex-direction:column;gap:.8rem}
  .msg{max-width:78%;padding:.65rem .9rem;border-radius:10px;line-height:1.55;font-size:.88rem;white-space:pre-wrap;word-break:break-word}
  .msg.user{background:#1a3050;align-self:flex-end;border:1px solid #1e3a5f}
  .msg.xclaw{background:#111;border:1px solid #222;align-self:flex-start}
  .msg.xclaw.plan{border-color:#d97706}
  .msg.xclaw.progress{border-color:#1d4ed8;font-size:.8rem;color:#93c5fd;padding:.4rem .7rem}
  .approval-btns{display:flex;gap:.4rem;margin-top:.6rem}
  .btn{padding:.35rem .8rem;border-radius:6px;border:none;cursor:pointer;font-size:.82rem;font-weight:600;transition:opacity .15s}
  .btn:hover{opacity:.85}
  .btn.approve{background:#15803d;color:#fff}
  .btn.cancel{background:#b91c1c;color:#fff}
  .step-tick{display:inline-block;margin-right:.3rem;color:#4ade80}
  #sidebar{width:200px;border-left:1px solid #1e1e1e;padding:.8rem;font-size:.78rem;overflow-y:auto;flex-shrink:0;display:flex;flex-direction:column;gap:.8rem}
  #sidebar h3{color:#666;text-transform:uppercase;letter-spacing:.5px;font-size:.7rem;margin-bottom:.2rem}
  .provider-row{display:flex;justify-content:space-between;align-items:center;padding:.25rem 0;border-bottom:1px solid #181818}
  .dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
  .dot.up{background:#4ade80}.dot.down{background:#ef4444}.dot.unknown{background:#6b7280}
  form{display:flex;gap:.4rem;padding:.8rem 1.2rem;border-top:1px solid #1e1e1e;flex-shrink:0}
  input{flex:1;background:#111;border:1px solid #2a2a2a;color:#e0e0e0;padding:.55rem .9rem;border-radius:8px;font-size:.9rem;outline:none}
  input:focus{border-color:#2563eb}
  button[type=submit]{background:#2563eb;color:#fff;border:none;padding:.55rem 1.1rem;border-radius:8px;cursor:pointer;font-weight:600;font-size:.88rem}
  button[type=submit]:disabled{opacity:.4;cursor:default}
  #typing{font-size:.78rem;color:#555;padding:0 1.2rem .3rem;min-height:1.2rem}
</style>
</head>
<body>
<header>
  <h1>⚡ XClaw</h1>
  <span id="sid-label" style="font-size:.75rem;color:#555"></span>
  <div id="status-bar">
    <span class="badge" id="ws-badge">WS —</span>
  </div>
</header>
<div id="body">
  <div id="messages"></div>
  <div id="sidebar">
    <div>
      <h3>Providers</h3>
      <div id="providers-list"><span style="color:#555">loading…</span></div>
    </div>
    <div>
      <h3>Session</h3>
      <div id="session-info" style="color:#555">—</div>
    </div>
  </div>
</div>
<div id="typing"></div>
<form id="form">
  <input id="input" placeholder="Tell XClaw what you need…" autocomplete="off" autofocus>
  <button type="submit" id="send-btn">Send</button>
</form>

<script>
let sessionId = '';
let ws = null;
let progressMsg = null;

const msgs    = document.getElementById('messages');
const form    = document.getElementById('form');
const inp     = document.getElementById('input');
const sendBtn = document.getElementById('send-btn');
const wsBadge = document.getElementById('ws-badge');
const typing  = document.getElementById('typing');

// ── Helpers ────────────────────────────────────────────────────────────────

function addMsg(text, cls, withApproval) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  div.textContent = text;
  if (withApproval) {
    const btns = document.createElement('div');
    btns.className = 'approval-btns';
    [['✅ Approve','approve','yes'],['❌ Cancel','cancel','no']].forEach(([label, cls2, val]) => {
      const b = document.createElement('button');
      b.className = 'btn ' + cls2; b.textContent = label;
      b.onclick = () => { removeApprovalBtns(); send(val); };
      btns.appendChild(b);
    });
    div.appendChild(btns);
    div.classList.add('plan');
  }
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
  return div;
}

function removeApprovalBtns() {
  document.querySelectorAll('.approval-btns').forEach(el => el.remove());
}

function setProgress(text) {
  if (!progressMsg) {
    progressMsg = addMsg(text, 'xclaw progress', false);
  } else {
    progressMsg.textContent = text;
    msgs.scrollTop = msgs.scrollHeight;
  }
}

function clearProgress() {
  if (progressMsg) { progressMsg.remove(); progressMsg = null; }
}

// ── WebSocket ──────────────────────────────────────────────────────────────

function connectWS(sid) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/${sid}`);
  ws.onopen  = () => { wsBadge.textContent = 'WS live'; wsBadge.className = 'badge ok'; };
  ws.onclose = () => { wsBadge.textContent = 'WS off';  wsBadge.className = 'badge fail'; ws = null; };
  ws.onerror = () => { wsBadge.textContent = 'WS err';  wsBadge.className = 'badge fail'; };
  ws.onmessage = ({data}) => {
    const ev = JSON.parse(data);
    if (ev.type === 'ping') return;
    if (ev.type === 'plan_start') {
      setProgress(`Planning: ${ev.summary} (${ev.waves} wave${ev.waves > 1 ? 's' : ''})`);
    } else if (ev.type === 'wave_start') {
      const par = ev.parallel ? ` (parallel ×${ev.steps.length})` : '';
      setProgress(`Wave ${ev.wave}/${ev.total_waves}${par}: ${ev.steps.map(s=>s.agent).join(', ')}`);
    } else if (ev.type === 'step_done') {
      setProgress(`✓ Step ${ev.step_id} [${ev.agent}] done`);
    } else if (ev.type === 'done') {
      clearProgress();
      sendBtn.disabled = false;
      typing.textContent = '';
    } else if (ev.type === 'error') {
      clearProgress();
      addMsg('Error: ' + ev.message, 'xclaw', false);
      sendBtn.disabled = false;
    }
  };
}

// ── Send ───────────────────────────────────────────────────────────────────

async function send(text) {
  addMsg(text, 'user', false);
  inp.value = '';
  sendBtn.disabled = true;
  typing.textContent = 'XClaw is thinking…';

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
      document.getElementById('session-info').textContent = sessionId;
      connectWS(sessionId);
    }

    clearProgress();
    typing.textContent = '';
    addMsg(data.text, 'xclaw', data.requires_approval);
    if (!data.requires_approval) sendBtn.disabled = false;
  } catch (err) {
    clearProgress();
    typing.textContent = '';
    addMsg('Network error: ' + err.message, 'xclaw', false);
    sendBtn.disabled = false;
  }
}

// ── Provider status ────────────────────────────────────────────────────────

async function refreshProviders() {
  try {
    const res = await fetch('/providers');
    const list = await res.json();
    const el = document.getElementById('providers-list');
    if (!list.length) { el.innerHTML = '<span style="color:#555">none configured</span>'; return; }
    el.innerHTML = list.map(p => `
      <div class="provider-row">
        <span style="color:${p.available ? '#d4d4d4' : '#555'}">${p.provider}</span>
        <span class="dot ${p.available ? 'up' : (p.circuit_open ? 'down' : 'unknown')}"></span>
      </div>`).join('');
  } catch {}
}

// ── Init ───────────────────────────────────────────────────────────────────

form.onsubmit = e => { e.preventDefault(); const t = inp.value.trim(); if (t && !sendBtn.disabled) send(t); };
refreshProviders();
setInterval(refreshProviders, 15000);
</script>
</body>
</html>
"""
