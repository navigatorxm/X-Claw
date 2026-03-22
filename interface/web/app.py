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

    @app.post("/tasks")
    async def create_task(req: ChatRequest):
        task_id = memory.add_task(req.session_id, req.text)
        return JSONResponse({"id": task_id, "title": req.text})

    @app.post("/tasks/{task_id}/done")
    async def complete_task(task_id: int):
        memory.update_task_status(task_id, "done")
        return JSONResponse({"ok": True})

    @app.delete("/tasks/{task_id}")
    async def delete_task(task_id: int):
        memory.update_task_status(task_id, "cancelled")
        return JSONResponse({"ok": True})

    @app.get("/scheduled")
    async def scheduled_tasks(session_id: str):
        from core.commander import Commander
        if hasattr(gateway.handler, "_scheduler") and gateway.handler._scheduler:
            return JSONResponse(gateway.handler._scheduler.list_tasks(session_id))
        return JSONResponse([])

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

    @app.get("/settings")
    async def settings_endpoint():
        """Return current runtime config (no secrets)."""
        import os, socket
        hostname = socket.gethostname()
        try: host_ip = socket.gethostbyname(hostname)
        except Exception: host_ip = "unknown"
        domain = os.getenv("XCLAW_DOMAIN", "")
        port = os.getenv("PORT", "8000")
        providers_configured = [p for p in ["GROQ_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY",
                                             "OVH_API_KEY", "DO_API_KEY", "OLLAMA_HOST"]
                                 if os.getenv(p)]
        return JSONResponse({
            "version": "3.0",
            "host_ip": host_ip,
            "hostname": hostname,
            "port": port,
            "domain": domain,
            "providers_configured": providers_configured,
            "telegram_configured": bool(os.getenv("TELEGRAM_BOT_TOKEN")),
            "github_configured": bool(os.getenv("GITHUB_TOKEN")),
            "email_configured": bool(os.getenv("SMTP_USER")),
            "mcp_configured": Path("mcp_servers.json").exists(),
            "tools_count": len(tools.tool_names()) if tools else 0,
        })

    @app.get("/nginx-config")
    async def nginx_config(domain: str, port: int = 8000):
        """Generate an nginx reverse-proxy config for the given domain."""
        cfg = f"""server {{
    listen 80;
    server_name {domain};

    # Required for WebSocket + SSE streaming
    proxy_buffering         off;
    proxy_read_timeout      300s;
    proxy_connect_timeout   10s;

    location / {{
        proxy_pass         http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }}
}}"""
        return JSONResponse({"config": cfg, "domain": domain, "commands": [
            f"sudo tee /etc/nginx/sites-available/xclaw > /dev/null << 'EOF'\\n{cfg}\\nEOF",
            "sudo ln -sf /etc/nginx/sites-available/xclaw /etc/nginx/sites-enabled/xclaw",
            "sudo nginx -t && sudo systemctl reload nginx",
            f"sudo certbot --nginx -d {domain}  # free HTTPS",
        ]})

    return app


# ── Embedded dashboard ─────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>XClaw — AI Executive Assistant</title>
<style>
:root{--bg:#070707;--bg2:#0a0a0a;--bg3:#0f0f0f;--border:#1a1a1a;--border2:#222;--text:#d0d0d0;--text-dim:#555;--text-mid:#888;--blue:#2563eb;--blue-dim:#1e3a5f;--green:#4ade80;--red:#ef4444;--yellow:#fbbf24;--orange:#f97316}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;overflow:hidden;font-size:13px}
/* ── Layout ── */
header{padding:.55rem 1rem;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:.6rem;flex-shrink:0;background:var(--bg2)}
#logo{font-size:.95rem;font-weight:800;color:#fff;letter-spacing:.3px;white-space:nowrap}
#logo span{color:var(--blue)}
nav{display:flex;gap:2px;margin-left:.4rem}
.tab{padding:.3rem .65rem;border-radius:5px;border:none;background:transparent;color:var(--text-mid);cursor:pointer;font-size:.8rem;font-weight:500;transition:all .15s}
.tab:hover{background:#111;color:var(--text)}
.tab.active{background:#111;color:#fff;border:1px solid var(--border2)}
.tab.active{border:1px solid var(--border2)}
#hdr-right{margin-left:auto;display:flex;gap:.35rem;align-items:center;flex-shrink:0}
.badge{font-size:.67rem;padding:.12rem .4rem;border-radius:3px;background:#111;border:1px solid var(--border2);color:var(--text-dim);white-space:nowrap}
.badge.ok{border-color:#14532d;color:var(--green)}.badge.err{border-color:#7f1d1d;color:var(--red)}.badge.warn{border-color:#78350f;color:var(--yellow)}
.pdot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:2px}
.pdot.up{background:var(--green)}.pdot.down{background:var(--red)}.pdot.unk{background:#444}
/* ── Body ── */
#body{flex:1;display:flex;overflow:hidden}
#main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
/* ── Tab panels ── */
.panel-tab{flex:1;display:flex;flex-direction:column;overflow:hidden}
.panel-tab.hidden{display:none!important}
/* ── Chat ── */
#messages{flex:1;overflow-y:auto;padding:.8rem 1rem;display:flex;flex-direction:column;gap:.5rem}
.msg{max-width:82%;padding:.55rem .85rem;border-radius:8px;line-height:1.6;word-break:break-word}
.msg.user{background:#152238;align-self:flex-end;border:1px solid var(--blue-dim);white-space:pre-wrap}
.msg.xclaw{background:var(--bg3);border:1px solid var(--border2);align-self:flex-start}
.msg.xclaw.plan{border-color:#b45309}
.msg.xclaw p{margin:.3rem 0}.msg.xclaw p:first-child{margin-top:0}.msg.xclaw p:last-child{margin-bottom:0}
.msg.xclaw pre{background:#111;border:1px solid var(--border2);border-radius:5px;padding:.5rem .7rem;margin:.4rem 0;overflow-x:auto;font-size:.8rem}
.msg.xclaw code{background:#111;border-radius:3px;padding:.1rem .3rem;font-size:.82rem;font-family:'Fira Code','Consolas',monospace}
.msg.xclaw pre code{background:transparent;padding:0;font-size:.8rem}
.msg.xclaw h1,.msg.xclaw h2,.msg.xclaw h3{color:#ddd;margin:.5rem 0 .2rem;font-size:.95rem}
.msg.xclaw ul,.msg.xclaw ol{padding-left:1.3rem;margin:.3rem 0}
.msg.xclaw li{margin:.15rem 0}
.msg.xclaw a{color:#60a5fa;text-decoration:none}.msg.xclaw a:hover{text-decoration:underline}
.msg.xclaw strong{color:#eee}.msg.xclaw em{color:#bbb}
.msg.xclaw blockquote{border-left:3px solid var(--border2);padding-left:.6rem;color:var(--text-mid);margin:.3rem 0}
.msg.xclaw table{border-collapse:collapse;width:100%;margin:.4rem 0;font-size:.82rem}
.msg.xclaw th,.msg.xclaw td{border:1px solid var(--border2);padding:.25rem .5rem}
.msg.xclaw th{background:#111;color:#ccc}
.event-line{font-size:.73rem;color:#334;padding:.05rem .4rem;align-self:flex-start;font-family:monospace}
.event-line.tool{color:#1e3a6e}.event-line.done{color:#14532d}
.approval-btns{display:flex;gap:.35rem;margin-top:.5rem}
.abtn{padding:.28rem .65rem;border-radius:5px;border:none;cursor:pointer;font-size:.78rem;font-weight:600}
.abtn.yes{background:#15803d;color:#fff}.abtn.no{background:#991b1b;color:#fff}
#status-line{font-size:.72rem;color:var(--text-dim);padding:.18rem .9rem;min-height:1rem;flex-shrink:0;font-style:italic}
#chat-input-bar{display:flex;gap:.4rem;padding:.55rem .8rem;border-top:1px solid var(--border);flex-shrink:0;background:var(--bg2)}
#chat-input-bar input{flex:1;background:var(--bg3);border:1px solid var(--border2);color:#e8e8e8;padding:.45rem .75rem;border-radius:6px;font-size:.88rem;outline:none}
#chat-input-bar input:focus{border-color:var(--blue)}
#chat-input-bar button{background:var(--blue);color:#fff;border:none;padding:.45rem .95rem;border-radius:6px;cursor:pointer;font-weight:600;font-size:.82rem;white-space:nowrap}
#chat-input-bar button:disabled{opacity:.35;cursor:default}
/* ── Tasks ── */
#tasks-panel{padding:.8rem 1rem;overflow-y:auto}
.task-add{display:flex;gap:.4rem;margin-bottom:.8rem}
.task-add input{flex:1;background:var(--bg3);border:1px solid var(--border2);color:#e0e0e0;padding:.38rem .65rem;border-radius:6px;font-size:.85rem;outline:none}
.task-add input:focus{border-color:var(--blue)}
.task-add button{background:var(--blue);color:#fff;border:none;padding:.38rem .8rem;border-radius:6px;cursor:pointer;font-size:.82rem;font-weight:600}
.task-item{display:flex;align-items:center;gap:.5rem;padding:.4rem .2rem;border-bottom:1px solid #111}
.task-item.done .task-title{text-decoration:line-through;color:var(--text-dim)}
.task-cb{width:14px;height:14px;cursor:pointer;flex-shrink:0;accent-color:var(--blue)}
.task-title{flex:1;font-size:.85rem}
.task-del{background:none;border:none;color:#444;cursor:pointer;font-size:.9rem;padding:0 .2rem}
.task-del:hover{color:var(--red)}
.section-empty{color:var(--text-dim);font-size:.82rem;padding:.5rem 0;font-style:italic}
/* ── Schedule ── */
#schedule-panel{padding:.8rem 1rem;overflow-y:auto}
.sched-item{background:var(--bg3);border:1px solid var(--border2);border-radius:6px;padding:.5rem .75rem;margin-bottom:.5rem}
.sched-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:.2rem}
.sched-name{font-size:.82rem;color:#ccc;font-weight:500}
.sched-interval{font-size:.72rem;color:var(--blue);background:#0f1729;border:1px solid #1e3a5f;padding:.1rem .35rem;border-radius:3px}
.sched-prompt{font-size:.78rem;color:var(--text-mid);font-style:italic}
/* ── Metrics ── */
#metrics-panel{padding:.8rem 1rem;overflow-y:auto;display:grid;grid-template-columns:1fr 1fr;gap:.7rem}
.metric-card{background:var(--bg3);border:1px solid var(--border2);border-radius:6px;padding:.6rem .8rem}
.metric-card h4{font-size:.67rem;text-transform:uppercase;letter-spacing:.5px;color:var(--text-dim);margin-bottom:.35rem}
.metric-val{font-size:1.35rem;font-weight:700;color:#fff}
.metric-sub{font-size:.72rem;color:var(--text-mid);margin-top:.1rem}
.provider-table{width:100%;border-collapse:collapse;font-size:.8rem}
.provider-table th{text-align:left;color:var(--text-dim);font-weight:500;padding:.2rem .4rem;border-bottom:1px solid var(--border)}
.provider-table td{padding:.3rem .4rem;border-bottom:1px solid #111}
.tool-grid{display:flex;flex-wrap:wrap;gap:.25rem;margin-top:.3rem}
.tool-chip{font-size:.68rem;background:#111;border:1px solid var(--border2);border-radius:3px;padding:.1rem .35rem;color:var(--text-mid)}
.tool-chip.firing{border-color:var(--blue);color:#93c5fd;background:#0f1729}
/* ── KB ── */
#kb-panel{padding:.8rem 1rem;overflow-y:auto}
.drop-zone{border:1px dashed #2a2a2a;border-radius:7px;padding:1rem;text-align:center;color:var(--text-dim);font-size:.8rem;cursor:pointer;transition:border-color .2s;margin-bottom:.7rem}
.drop-zone:hover,.drop-zone.over{border-color:var(--blue);color:#93c5fd}
.kb-search{display:flex;gap:.4rem;margin-bottom:.7rem}
.kb-search input{flex:1;background:var(--bg3);border:1px solid var(--border2);color:#e0e0e0;padding:.38rem .65rem;border-radius:6px;font-size:.85rem;outline:none}
.kb-search input:focus{border-color:var(--blue)}
.kb-search button{background:#111;color:var(--text);border:1px solid var(--border2);padding:.38rem .7rem;border-radius:6px;cursor:pointer;font-size:.8rem}
.kb-source-item{background:var(--bg3);border:1px solid var(--border2);border-radius:5px;padding:.4rem .65rem;margin-bottom:.35rem;font-size:.8rem;display:flex;justify-content:space-between;align-items:center}
.kb-source-name{color:#bbb;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.kb-chunks{font-size:.72rem;color:var(--text-dim);white-space:nowrap;margin-left:.5rem}
.kb-results{background:var(--bg3);border:1px solid var(--border2);border-radius:6px;padding:.6rem .8rem;margin-bottom:.5rem;font-size:.8rem;line-height:1.55;color:#bbb;white-space:pre-wrap;word-break:break-word}
/* ── Right sidebar ── */
#sidebar{width:195px;border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0;background:var(--bg2)}
.side-section{padding:.55rem .65rem;border-bottom:1px solid var(--border)}
.side-title{font-size:.62rem;text-transform:uppercase;letter-spacing:.6px;color:#333;margin-bottom:.3rem}
.prov-row{display:flex;align-items:center;gap:.35rem;padding:.12rem 0}
.prov-name{font-size:.78rem;color:var(--text-mid)}
.prov-name.up{color:#aaa}
.live-feed{flex:1;overflow-y:auto;padding:.4rem .65rem}
.feed-item{font-size:.72rem;color:#1e3a6e;padding:.1rem 0;font-family:monospace;border-bottom:1px solid #0a0a0a;word-break:break-all}
.feed-item.done-ev{color:#14532d}
/* ── Settings ── */
#settings-panel .scard{background:var(--bg3);border:1px solid var(--border2);border-radius:6px;padding:.6rem .8rem}
#settings-panel .scard h4{font-size:.67rem;text-transform:uppercase;letter-spacing:.5px;color:var(--text-dim);margin-bottom:.35rem}
#settings-panel .scard .sval{font-size:.85rem;color:#ccc;word-break:break-all}
#settings-panel .pill{display:inline-block;padding:.1rem .4rem;border-radius:3px;font-size:.72rem;font-weight:500;margin:.1rem}
#settings-panel .pill.on{background:#14532d;color:var(--green)}.pill.off{background:#1f1f1f;color:#555}
.nginx-cmd{background:#0a0a0a;border:1px solid var(--border);border-radius:4px;padding:.35rem .6rem;font-size:.73rem;font-family:monospace;color:#93c5fd;margin:.2rem 0;word-break:break-all;cursor:pointer}
.nginx-cmd:hover{border-color:var(--blue)}

</style>
</head>
<body>

<!-- ── Header ─────────────────────────────────────────────────────────── -->
<header>
  <span id="logo">⚡ X<span>Claw</span></span>
  <nav>
    <button class="tab active" data-tab="chat">Chat</button>
    <button class="tab" data-tab="tasks">Tasks</button>
    <button class="tab" data-tab="schedule">Schedule</button>
    <button class="tab" data-tab="metrics">Metrics</button>
    <button class="tab" data-tab="kb">Knowledge</button>
    <button class="tab" data-tab="settings">Settings</button>
  </nav>
  <div id="hdr-right">
    <span class="badge" id="ws-badge">WS —</span>
    <span class="badge" id="iter-badge">0 itr</span>
    <span class="badge" id="tool-count-badge">— tools</span>
    <span style="color:#333;font-size:.68rem" id="sid-label"></span>
  </div>
</header>

<!-- ── Body ──────────────────────────────────────────────────────────── -->
<div id="body">
  <div id="main">

    <!-- Chat tab -->
    <div class="panel-tab" id="tab-chat">
      <div id="messages"></div>
      <div id="status-line"></div>
      <div id="chat-input-bar">
        <input type="text" id="chat-inp" placeholder="Ask XClaw anything — or try /help" autocomplete="off" autofocus>
        <button id="send-btn">Send</button>
      </div>
    </div>

    <!-- Tasks tab -->
    <div class="panel-tab hidden" id="tab-tasks">
      <div id="tasks-panel">
        <div class="task-add">
          <input type="text" id="task-inp" placeholder="New task…">
          <button id="task-add-btn">Add</button>
        </div>
        <div id="task-list"><div class="section-empty">No tasks yet.</div></div>
      </div>
    </div>

    <!-- Schedule tab -->
    <div class="panel-tab hidden" id="tab-schedule">
      <div id="schedule-panel">
        <div id="schedule-list"><div class="section-empty">No scheduled tasks. Use /schedule in chat to create one.</div></div>
      </div>
    </div>

    <!-- Metrics tab -->
    <div class="panel-tab hidden" id="tab-metrics">
      <div id="metrics-panel">
        <div class="metric-card">
          <h4>Total Tokens</h4>
          <div class="metric-val" id="m-tokens">—</div>
          <div class="metric-sub">prompt + completion</div>
        </div>
        <div class="metric-card">
          <h4>P95 Latency</h4>
          <div class="metric-val" id="m-p95">—</div>
          <div class="metric-sub">ms (last 1000 reqs)</div>
        </div>
        <div class="metric-card">
          <h4>Total Traces</h4>
          <div class="metric-val" id="m-traces">—</div>
          <div class="metric-sub">completed executions</div>
        </div>
        <div class="metric-card">
          <h4>Tools Registered</h4>
          <div class="metric-val" id="m-tools">—</div>
          <div class="metric-sub">LLM-callable</div>
        </div>
        <div class="metric-card" style="grid-column:span 2">
          <h4>Provider Health</h4>
          <table class="provider-table">
            <thead><tr><th>Provider</th><th>Status</th><th>Calls</th></tr></thead>
            <tbody id="m-providers"></tbody>
          </table>
        </div>
        <div class="metric-card" style="grid-column:span 2">
          <h4>Registered Tools</h4>
          <div class="tool-grid" id="m-tool-list"></div>
        </div>
      </div>
    </div>

    <!-- Knowledge Base tab -->
    <div class="panel-tab hidden" id="tab-kb">
      <div id="kb-panel">
        <div class="drop-zone" id="kb-drop">
          📎 Drop files here or click to upload (PDF, TXT, MD, CSV, code)
          <input type="file" id="kb-file-in" style="display:none" multiple>
        </div>
        <div class="kb-search">
          <input type="text" id="kb-query" placeholder="Search knowledge base…">
          <button id="kb-search-btn">Search</button>
        </div>
        <div id="kb-results"></div>
        <div style="font-size:.67rem;text-transform:uppercase;letter-spacing:.5px;color:#333;margin:.6rem 0 .3rem">Ingested Documents</div>
        <div id="kb-sources"><div class="section-empty">Knowledge base is empty.</div></div>
      </div>
    </div>

    <!-- Settings tab -->
    <div class="panel-tab hidden" id="tab-settings">
      <div id="settings-panel" style="padding:.8rem 1rem;overflow-y:auto">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.7rem" id="settings-cards"></div>
        <div style="margin-top:1rem">
          <div style="font-size:.67rem;text-transform:uppercase;letter-spacing:.5px;color:#333;margin-bottom:.5rem">Connect a Domain</div>
          <div style="display:flex;gap:.4rem;margin-bottom:.6rem">
            <input type="text" id="domain-inp" placeholder="xclaw.yourdomain.com" style="flex:1;background:var(--bg3);border:1px solid var(--border2);color:#e0e0e0;padding:.38rem .65rem;border-radius:6px;font-size:.85rem;outline:none">
            <input type="number" id="domain-port" value="8000" placeholder="8000" style="width:70px;background:var(--bg3);border:1px solid var(--border2);color:#e0e0e0;padding:.38rem .5rem;border-radius:6px;font-size:.85rem;outline:none">
            <button id="nginx-gen-btn" style="background:var(--blue);color:#fff;border:none;padding:.38rem .8rem;border-radius:6px;cursor:pointer;font-size:.82rem;font-weight:600">Generate Config</button>
          </div>
          <pre id="nginx-output" style="display:none;background:#0a0a0a;border:1px solid var(--border2);border-radius:6px;padding:.7rem;font-size:.75rem;color:#93c5fd;overflow-x:auto;white-space:pre-wrap;word-break:break-all"></pre>
          <div id="nginx-cmds" style="display:none;margin-top:.5rem"></div>
        </div>
      </div>
    </div>

  </div><!-- /main -->

  <!-- ── Right sidebar ──────────────────────────────────────────────── -->
  <div id="sidebar">
    <div class="side-section">
      <div class="side-title">Providers</div>
      <div id="sb-providers"><span style="color:#333;font-size:.75rem">loading…</span></div>
    </div>
    <div class="side-section">
      <div class="side-title">Active Tools</div>
      <div id="sb-tools" class="tool-grid" style="min-height:1.2rem"><span style="color:#333;font-size:.75rem">—</span></div>
    </div>
    <div class="side-section" style="border-bottom:none;flex:1;overflow:hidden;display:flex;flex-direction:column">
      <div class="side-title">Live Feed</div>
      <div class="live-feed" id="sb-feed"></div>
    </div>
  </div>

</div><!-- /body -->

<script>
'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let sessionId = localStorage.getItem('xclaw_sid') || '';
let ws = null;
let iterations = 0;
let activeTools = new Set();
let allToolNames = [];
let currentTab = 'chat';

// ── Minimal Markdown renderer ──────────────────────────────────────────────

function renderMd(src) {
  if (!src) return '';
  let html = src
    // Code blocks first
    .replace(/```(\\w*)\\n([\\s\\S]*?)```/g, (_, lang, code) =>
      `<pre><code class="lang-${lang}">${esc(code.trim())}</code></pre>`)
    // Inline code
    .replace(/`([^`]+)`/g, (_, c) => `<code>${esc(c)}</code>`)
    // Headers
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    // Bold / italic
    .replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>')
    .replace(/\\*([^*]+)\\*/g, '<em>$1</em>')
    // Links
    .replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g, '<a href="$2" target="_blank">$1</a>')
    // Blockquotes
    .replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>')
    // Tables (simple: |a|b|)
    .replace(/^\\|(.+)\\|$/gm, row => {
      const cells = row.slice(1,-1).split('|').map(c=>c.trim());
      return '<tr>' + cells.map(c=>`<td>${c}</td>`).join('') + '</tr>';
    })
    // Lists
    .replace(/^[\\-\\*] (.+)$/gm, '<li>$1</li>')
    .replace(/^\\d+\\. (.+)$/gm, '<li>$1</li>');

  // Wrap consecutive <li> in <ul>
  html = html.replace(/(<li>.*<\\/li>\\n?)+/gs, m => `<ul>${m}</ul>`);
  // Wrap consecutive <tr> in <table>
  html = html.replace(/(<tr>.*<\\/tr>\\n?)+/gs, m => `<table>${m}</table>`);

  // Paragraphs: double newline → <p>
  html = html.split(/\\n\\n+/).map(block => {
    if (/^<(pre|ul|ol|blockquote|h[1-6]|table)/.test(block.trim())) return block;
    const inner = block.replace(/\\n/g, '<br>');
    return `<p>${inner}</p>`;
  }).join('\\n');

  return html;
}

function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Tab switching ──────────────────────────────────────────────────────────

document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    currentTab = btn.dataset.tab;
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.panel-tab').forEach(p => p.classList.add('hidden'));
    document.getElementById('tab-' + currentTab).classList.remove('hidden');
    if (currentTab === 'tasks') refreshTasks();
    if (currentTab === 'schedule') refreshSchedule();
    if (currentTab === 'metrics') refreshMetrics();
    if (currentTab === 'kb') refreshKBSources();
    if (currentTab === 'settings') refreshSettings();
  });
});

// ── Chat ───────────────────────────────────────────────────────────────────

const msgs = document.getElementById('messages');
const chatInp = document.getElementById('chat-inp');
const sendBtn = document.getElementById('send-btn');
const statusLine = document.getElementById('status-line');

function addMsg(text, cls, withApproval) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  if (cls === 'xclaw') {
    div.innerHTML = renderMd(text);
  } else {
    div.textContent = text;
  }
  if (withApproval) {
    const btns = document.createElement('div');
    btns.className = 'approval-btns';
    [['✅ Approve','yes','yes'],['❌ Cancel','no','no']].forEach(([label, c, val]) => {
      const b = document.createElement('button');
      b.className = 'abtn ' + c; b.textContent = label;
      b.onclick = () => { document.querySelectorAll('.approval-btns').forEach(e=>e.remove()); sendChat(val); };
      btns.appendChild(b);
    });
    div.appendChild(btns);
    div.classList.add('plan');
  }
  msgs.appendChild(div); msgs.scrollTop = msgs.scrollHeight;
  return div;
}

function addEventLine(text, cls='') {
  const div = document.createElement('div');
  div.className = 'event-line ' + cls;
  div.textContent = text;
  msgs.appendChild(div); msgs.scrollTop = msgs.scrollHeight;
}

function setStatus(text) { statusLine.textContent = text; }

async function sendChat(text) {
  if (!text.trim()) return;
  addMsg(text, 'user', false);
  chatInp.value = '';
  sendBtn.disabled = true;
  setStatus('Thinking…');
  activeTools.clear(); renderSideTools();

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({text, session_id: sessionId}),
    });
    const data = await res.json();
    if (!sessionId && data.session_id) {
      sessionId = data.session_id;
      localStorage.setItem('xclaw_sid', sessionId);
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

chatInp.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !sendBtn.disabled) {
    e.preventDefault(); sendChat(chatInp.value);
  }
});
sendBtn.addEventListener('click', () => sendChat(chatInp.value));

// ── WebSocket ──────────────────────────────────────────────────────────────

const wsBadge = document.getElementById('ws-badge');
const iterBadge = document.getElementById('iter-badge');

function connectWS(sid) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/${sid}`);
  ws.onopen = () => { wsBadge.textContent = 'WS ✓'; wsBadge.className = 'badge ok'; };
  ws.onclose = () => { wsBadge.textContent = 'WS ✗'; wsBadge.className = 'badge err'; ws = null; };
  ws.onmessage = ({data}) => {
    const ev = JSON.parse(data);
    if (ev.type === 'ping') return;
    addFeedItem(ev);
    if (ev.type === 'agent_start') {
      setStatus('Running…'); iterations = 0;
    } else if (ev.type === 'tool_calls') {
      iterations = ev.iteration || iterations;
      iterBadge.textContent = iterations + ' itr';
      ev.tools?.forEach(t => activeTools.add(t));
      renderSideTools();
      addEventLine('→ ' + (ev.tools||[]).join(', '), 'tool');
      setStatus('Iteration ' + ev.iteration + ': ' + (ev.tools||[]).join(', '));
    } else if (ev.type === 'tool_done') {
      const preview = (ev.preview||'').slice(0,90);
      addEventLine('✓ ' + ev.tool + (preview ? ': ' + preview : ''));
      setTimeout(() => { activeTools.delete(ev.tool); renderSideTools(); }, 2000);
    } else if (ev.type === 'agent_done') {
      addEventLine('Done in ' + ev.iterations + ' iteration(s)', 'done-ev');
      setStatus(''); sendBtn.disabled = false;
      activeTools.clear(); renderSideTools();
    } else if (ev.type === 'done') {
      setStatus(''); sendBtn.disabled = false;
    }
  };
}

function addFeedItem(ev) {
  const feed = document.getElementById('sb-feed');
  const d = document.createElement('div');
  d.className = 'feed-item' + (ev.type === 'agent_done' ? ' done-ev' : '');
  const ts = new Date().toLocaleTimeString('en-GB', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
  d.textContent = `[${ts}] ${ev.type}` + (ev.tools ? ': ' + ev.tools.join(',') : '') + (ev.tool ? ': ' + ev.tool : '');
  feed.appendChild(d);
  if (feed.children.length > 50) feed.removeChild(feed.firstChild);
  feed.scrollTop = feed.scrollHeight;
}

function renderSideTools() {
  const el = document.getElementById('sb-tools');
  if (activeTools.size === 0) { el.innerHTML = '<span style="color:#333;font-size:.75rem">—</span>'; return; }
  el.innerHTML = [...activeTools].map(t =>
    `<span class="tool-chip firing">${t}</span>`
  ).join('');
}

// ── Tasks ──────────────────────────────────────────────────────────────────

async function refreshTasks() {
  if (!sessionId) return;
  try {
    const tasks = await (await fetch('/tasks?session_id=' + encodeURIComponent(sessionId))).json();
    const el = document.getElementById('task-list');
    if (!tasks.length) { el.innerHTML = '<div class="section-empty">No tasks yet.</div>'; return; }
    el.innerHTML = tasks.map(t => `
      <div class="task-item${t.status==='done'?' done':''}" data-id="${t.id}">
        <input type="checkbox" class="task-cb" ${t.status==='done'?'checked':''} onchange="toggleTask(${t.id},this.checked)">
        <span class="task-title">${esc(t.title)}</span>
        <button class="task-del" onclick="deleteTask(${t.id})">✕</button>
      </div>`).join('');
  } catch {}
}

async function toggleTask(id, done) {
  if (done) await fetch('/tasks/' + id + '/done', {method:'POST'});
  await refreshTasks();
}

async function deleteTask(id) {
  await fetch('/tasks/' + id, {method:'DELETE'});
  await refreshTasks();
}

document.getElementById('task-add-btn').addEventListener('click', async () => {
  const inp = document.getElementById('task-inp');
  const title = inp.value.trim(); if (!title || !sessionId) return;
  await fetch('/tasks', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({text: title, session_id: sessionId})});
  inp.value = ''; await refreshTasks();
});
document.getElementById('task-inp').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('task-add-btn').click();
});

// ── Schedule ───────────────────────────────────────────────────────────────

async function refreshSchedule() {
  if (!sessionId) return;
  try {
    const tasks = await (await fetch('/scheduled?session_id=' + encodeURIComponent(sessionId))).json();
    const el = document.getElementById('schedule-list');
    if (!tasks.length) { el.innerHTML = '<div class="section-empty">No scheduled tasks. Use /schedule in chat.</div>'; return; }
    el.innerHTML = tasks.map(t => `
      <div class="sched-item">
        <div class="sched-head">
          <span class="sched-name">${esc(t.prompt.slice(0,50))}</span>
          <span class="sched-interval">${esc(t.interval_str)}</span>
        </div>
        <div class="sched-prompt">${esc(t.prompt)}</div>
      </div>`).join('');
  } catch {}
}

// ── Metrics ────────────────────────────────────────────────────────────────

async function refreshMetrics() {
  try {
    const [metrics, providers, toolsList] = await Promise.all([
      fetch('/metrics').then(r=>r.json()),
      fetch('/providers').then(r=>r.json()),
      fetch('/tools').then(r=>r.json()),
    ]);
    // Stats
    const totalTok = metrics.total_prompt_tokens + metrics.total_completion_tokens;
    document.getElementById('m-tokens').textContent =
      totalTok > 1000 ? (totalTok/1000).toFixed(1)+'k' : totalTok;
    document.getElementById('m-p95').textContent =
      metrics.p95_latency_ms ? Math.round(metrics.p95_latency_ms)+'ms' : '—';
    document.getElementById('m-traces').textContent = metrics.total_traces ?? '—';
    document.getElementById('m-tools').textContent = toolsList.length;
    // Provider table
    document.getElementById('m-providers').innerHTML = providers.map(p => `
      <tr>
        <td><span class="pdot ${p.available?'up':p.circuit_open?'down':'unk'}"></span>${esc(p.provider)}</td>
        <td style="color:${p.available?'var(--green)':p.circuit_open?'var(--red)':'var(--text-dim)'}">${p.available?'Online':p.circuit_open?'Circuit open':'Unknown'}</td>
        <td style="color:var(--text-mid)">${p.total_calls ?? 0}</td>
      </tr>`).join('');
    // Tool chips
    allToolNames = toolsList;
    document.getElementById('m-tool-list').innerHTML = toolsList.map(t =>
      `<span class="tool-chip">${esc(t)}</span>`).join('');
    document.getElementById('tool-count-badge').textContent = toolsList.length + ' tools';
  } catch(e) { console.error(e); }
}

// ── Provider sidebar ───────────────────────────────────────────────────────

async function refreshSideProviders() {
  try {
    const data = await (await fetch('/providers')).json();
    const el = document.getElementById('sb-providers');
    if (!data.length) { el.innerHTML = '<span style="color:#333;font-size:.75rem">none configured</span>'; return; }
    el.innerHTML = data.map(p =>
      `<div class="prov-row"><span class="pdot ${p.available?'up':p.circuit_open?'down':'unk'}"></span><span class="prov-name ${p.available?'up':''}">${esc(p.provider)}</span></div>`
    ).join('');
  } catch {}
}

// ── Knowledge Base ─────────────────────────────────────────────────────────

const kbDrop = document.getElementById('kb-drop');
const kbFileIn = document.getElementById('kb-file-in');

kbDrop.addEventListener('click', () => kbFileIn.click());
kbDrop.addEventListener('dragover', e => { e.preventDefault(); kbDrop.classList.add('over'); });
kbDrop.addEventListener('dragleave', () => kbDrop.classList.remove('over'));
kbDrop.addEventListener('drop', e => { e.preventDefault(); kbDrop.classList.remove('over'); uploadKB(e.dataTransfer.files); });
kbFileIn.addEventListener('change', () => uploadKB(kbFileIn.files));

async function uploadKB(files) {
  for (const file of files) {
    const fd = new FormData(); fd.append('file', file);
    try {
      const res = await fetch('/upload', {method:'POST', body:fd});
      const data = await res.json();
      addMsg(data.result || data.error, 'xclaw', false);
      await refreshKBSources();
    } catch (err) { addMsg('Upload failed: '+err.message, 'xclaw', false); }
  }
}

async function refreshKBSources() {
  try {
    const sources = await (await fetch('/kb/sources')).json();
    const el = document.getElementById('kb-sources');
    if (!sources.length) { el.innerHTML = '<div class="section-empty">Knowledge base is empty.</div>'; return; }
    el.innerHTML = sources.map(s =>
      `<div class="kb-source-item"><span class="kb-source-name">${esc(s.source)}</span><span class="kb-chunks">${s.chunks} chunks</span></div>`
    ).join('');
  } catch {}
}

document.getElementById('kb-search-btn').addEventListener('click', async () => {
  const q = document.getElementById('kb-query').value.trim();
  if (!q) return;
  try {
    const res = await (await fetch('/kb/search?query=' + encodeURIComponent(q) + '&limit=5')).json();
    const el = document.getElementById('kb-results');
    if (!res.results?.length) { el.innerHTML = '<div class="section-empty">No results.</div>'; return; }
    el.innerHTML = res.results.map(r => `<div class="kb-results">${esc(r)}</div>`).join('');
  } catch {}
});
document.getElementById('kb-query').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('kb-search-btn').click();
});

// ── Settings ───────────────────────────────────────────────────────────────

async function refreshSettings() {
  try {
    const s = await (await fetch('/settings')).json();
    const cards = [
      {title:'Server', val: s.hostname + ' (' + s.host_ip + ':' + s.port + ')'},
      {title:'Domain', val: s.domain || '<span style="color:#555">not set</span>'},
      {title:'XClaw Version', val: 'v' + s.version},
      {title:'Tools', val: s.tools_count + ' registered'},
    ];
    const pills = [
      ['Telegram', s.telegram_configured],
      ['GitHub', s.github_configured],
      ['Email', s.email_configured],
      ['MCP', s.mcp_configured],
      ...s.providers_configured.map(p => [p.replace('_API_KEY','').replace('_HOST',''), true]),
    ];
    document.getElementById('settings-cards').innerHTML = cards.map(c =>
      `<div class="scard"><h4>${esc(c.title)}</h4><div class="sval">${c.val}</div></div>`
    ).join('') + `<div class="scard" style="grid-column:span 2"><h4>Integrations</h4>${
      pills.map(([n,v]) => `<span class="pill ${v?'on':'off'}">${n}</span>`).join('')
    }</div>`;
  } catch {}
}

document.getElementById('nginx-gen-btn').addEventListener('click', async () => {
  const domain = document.getElementById('domain-inp').value.trim();
  const port = parseInt(document.getElementById('domain-port').value) || 8000;
  if (!domain) return;
  try {
    const res = await (await fetch(`/nginx-config?domain=${encodeURIComponent(domain)}&port=${port}`)).json();
    const pre = document.getElementById('nginx-output');
    pre.textContent = res.config;
    pre.style.display = 'block';
    const cmdsEl = document.getElementById('nginx-cmds');
    cmdsEl.style.display = 'block';
    cmdsEl.innerHTML = '<div style="font-size:.7rem;color:#555;margin-bottom:.3rem">Run these commands on your server:</div>' +
      res.commands.map(cmd =>
        `<div class="nginx-cmd" onclick="navigator.clipboard.writeText(this.textContent)" title="Click to copy">${esc(cmd)}</div>`
      ).join('');
  } catch {}
});

// ── Init ───────────────────────────────────────────────────────────────────

// Restore session
if (sessionId) {
  document.getElementById('sid-label').textContent = sessionId;
  connectWS(sessionId);
  addMsg('Welcome back! Session restored: ' + sessionId, 'xclaw', false);
} else {
  addMsg('👋 Welcome to XClaw. Type a message to get started, or try /help', 'xclaw', false);
}

refreshSideProviders();
refreshMetrics();
setInterval(refreshSideProviders, 15000);
setInterval(refreshMetrics, 30000);

</script>
</body>
</html>
"""
