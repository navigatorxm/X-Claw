"""
XClaw Finance API — AI Agent Financial Execution Platform

Start with:
    cd xclaw_finance
    uvicorn api.app:app --reload --port 8001
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes import agents, approve, execute, history, policies

app = FastAPI(
    title="XClaw Finance API",
    description="AI Agent Financial Execution Platform — policy-gated trading with full audit trail.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agents.router)
app.include_router(execute.router)
app.include_router(approve.router)
app.include_router(history.router)
app.include_router(policies.router)


@app.get("/", tags=["health"])
async def health() -> dict:
    return {
        "service": "xclaw-finance",
        "version": "1.0.0",
        "status": "ok",
        "endpoints": [
            "POST /agent/register",
            "POST /execute",
            "GET  /execute/balance/{wallet_id}",
            "POST /approve",
            "GET  /approve/pending",
            "GET  /approve/{request_id}",
            "GET  /history",
            "GET  /history/{entry_id}",
            "GET  /policies",
            "POST /policies",
            "GET  /policies/{policy_id}",
            "DELETE /policies/{policy_id}",
        ],
    }
