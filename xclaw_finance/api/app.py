"""
XClaw Finance API — AI Agent Financial Execution Platform

Start with:
    cd xclaw_finance
    uvicorn api.app:app --reload --port 8001

First-time setup:
    POST /auth/agents  (no key required when zero agents exist) to create the admin.
    All other endpoints require X-API-Key header.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes import agents, approve, auth, execute, history, policies, risk
from .deps import get_agent_store
from auth.dependencies import _get_agent_store

app = FastAPI(
    title="XClaw Finance API",
    description=(
        "AI Agent Financial Execution Platform — "
        "policy + risk-gated trading with agent-level access control and full audit trail."
    ),
    version="1.2.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Wire auth dependency — routes resolve AgentStore through this override
app.dependency_overrides[_get_agent_store] = get_agent_store

app.include_router(auth.router)
app.include_router(agents.router)
app.include_router(execute.router)
app.include_router(approve.router)
app.include_router(history.router)
app.include_router(policies.router)
app.include_router(risk.router)


@app.get("/", tags=["health"])
async def health() -> dict:
    return {
        "service": "xclaw-finance",
        "version": "1.2.0",
        "status": "ok",
        "auth": "X-API-Key header required on all endpoints except POST /auth/agents (bootstrap)",
        "endpoints": {
            "auth": [
                "POST /auth/agents                  (bootstrap: open if 0 agents)",
                "POST /auth/agents/register         (admin)",
                "GET  /auth/agents                  (admin)",
                "GET  /auth/agents/me               (any)",
                "POST /auth/agents/{id}/rotate      (own or admin)",
                "PATCH /auth/agents/{id}/role       (admin)",
                "POST /auth/agents/{id}/revoke      (admin)",
            ],
            "wallets":    ["POST /agent/register (admin)", "GET /agent/{id}/wallets (read)"],
            "execution":  ["POST /execute (execute)", "GET /execute/balance/{id} (read)"],
            "approvals":  ["POST /approve (approve)", "GET /approve/pending (approve)", "GET /approve/{id} (read)"],
            "history":    ["GET /history (read)", "GET /history/{id} (read)"],
            "policies":   ["GET /policies (read)", "POST /policies (admin)", "DELETE /policies/{id} (admin)"],
            "risk":       ["POST /risk/config (admin)", "GET /risk/config/{id} (read)", "GET /risk/status/{id} (read)"],
        },
    }
