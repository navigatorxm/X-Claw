"""
XClaw Web Interface — FastAPI + minimal HTML dashboard.

Endpoints:
  GET  /           → serve the dashboard HTML
  POST /chat       → send a message, receive a response
  GET  /tasks      → list tasks for a session
  GET  /history    → list recent executions
  GET  /health     → health check

Run standalone:
    uvicorn interface.web.app:create_app --factory --reload
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.gateway import Gateway
    from core.memory import Memory

_HTML_PATH = Path(__file__).parent / "index.html"


def create_app(gateway: "Gateway", memory: "Memory"):
    """Factory function — returns a configured FastAPI app."""
    try:
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse, JSONResponse
        from pydantic import BaseModel
    except ImportError as exc:
        raise ImportError("FastAPI not installed. Run: pip install fastapi uvicorn") from exc

    app = FastAPI(title="XClaw", version="1.0")

    class ChatRequest(BaseModel):
        text: str
        session_id: str = ""

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        if _HTML_PATH.exists():
            return HTMLResponse(_HTML_PATH.read_text())
        return HTMLResponse(_INLINE_HTML)

    @app.post("/chat")
    async def chat(req: ChatRequest):
        session_id = req.session_id or f"web-{uuid.uuid4().hex[:8]}"
        response = await gateway.handle(req.text, "web", session_id)
        return JSONResponse({
            "text": response.text,
            "requires_approval": response.requires_approval,
            "session_id": session_id,
        })

    @app.get("/tasks")
    async def tasks(session_id: str):
        return JSONResponse(memory.get_tasks(session_id))

    @app.get("/history")
    async def history(session_id: str, limit: int = 10):
        return JSONResponse(memory.get_executions(session_id, limit))

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok"})

    return app


# Inline HTML fallback (used if interface/web/index.html does not exist)
_INLINE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>XClaw — NavOS</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0d0d0d; color: #e0e0e0; height: 100vh; display: flex; flex-direction: column; }
  header { padding: 1rem 1.5rem; border-bottom: 1px solid #1e1e1e; display: flex; align-items: center; gap: .75rem; }
  header h1 { font-size: 1.2rem; font-weight: 600; color: #fff; }
  header span { font-size: .8rem; color: #666; }
  #messages { flex: 1; overflow-y: auto; padding: 1.5rem; display: flex; flex-direction: column; gap: 1rem; }
  .msg { max-width: 75%; padding: .75rem 1rem; border-radius: 12px; line-height: 1.5; font-size: .92rem; white-space: pre-wrap; }
  .msg.user { background: #1a3a5c; align-self: flex-end; }
  .msg.xclaw { background: #1a1a1a; border: 1px solid #2a2a2a; align-self: flex-start; }
  .msg.xclaw.approval { border-color: #f59e0b; }
  .approval-btns { display: flex; gap: .5rem; margin-top: .75rem; }
  .btn { padding: .4rem .9rem; border-radius: 8px; border: none; cursor: pointer; font-size: .85rem; font-weight: 600; }
  .btn.approve { background: #16a34a; color: #fff; }
  .btn.cancel { background: #dc2626; color: #fff; }
  form { display: flex; gap: .5rem; padding: 1rem 1.5rem; border-top: 1px solid #1e1e1e; }
  input { flex: 1; background: #1a1a1a; border: 1px solid #2a2a2a; color: #e0e0e0; padding: .65rem 1rem; border-radius: 10px; font-size: .95rem; outline: none; }
  input:focus { border-color: #3b82f6; }
  button[type=submit] { background: #3b82f6; color: #fff; border: none; padding: .65rem 1.2rem; border-radius: 10px; cursor: pointer; font-weight: 600; }
</style>
</head>
<body>
<header>
  <h1>⚡ XClaw</h1>
  <span id="sid"></span>
</header>
<div id="messages"></div>
<form id="form">
  <input id="input" placeholder="Tell XClaw what you need…" autocomplete="off" autofocus>
  <button type="submit">Send</button>
</form>
<script>
  let sessionId = '';
  const msgs = document.getElementById('messages');
  const form = document.getElementById('form');
  const inp = document.getElementById('input');
  const sidEl = document.getElementById('sid');

  function addMsg(text, cls, withApproval) {
    const div = document.createElement('div');
    div.className = 'msg ' + cls + (withApproval ? ' approval' : '');
    div.textContent = text;
    if (withApproval) {
      const btns = document.createElement('div');
      btns.className = 'approval-btns';
      ['✅ Approve','❌ Cancel'].forEach((label, i) => {
        const b = document.createElement('button');
        b.className = 'btn ' + (i === 0 ? 'approve' : 'cancel');
        b.textContent = label;
        b.onclick = () => send(i === 0 ? 'yes' : 'no');
        btns.appendChild(b);
      });
      div.appendChild(btns);
    }
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
  }

  async function send(text) {
    addMsg(text, 'user', false);
    inp.value = '';
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ text, session_id: sessionId })
    });
    const data = await res.json();
    if (!sessionId) { sessionId = data.session_id; sidEl.textContent = 'Session: ' + sessionId; }
    addMsg(data.text, 'xclaw', data.requires_approval);
  }

  form.onsubmit = e => { e.preventDefault(); const t = inp.value.trim(); if (t) send(t); };
</script>
</body>
</html>
"""
