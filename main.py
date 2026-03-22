"""
XClaw v3 — entry point.

Usage:
    python main.py                         # CLI
    python main.py --interface web         # Web dashboard + API
    python main.py --interface telegram    # Telegram bot
    python main.py --interface web --port 8080
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _load_env() -> None:
    env_file = Path(".env")
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file)
    except ImportError:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def _setup_logging() -> None:
    Path("memory/logs").mkdir(parents=True, exist_ok=True)
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=getattr(logging, level, logging.INFO),
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("memory/logs/xclaw.log", encoding="utf-8"),
        ],
    )


def _build_xclaw():
    """
    Assemble the full v3 stack.

    Returns:
        (gateway, memory, router, llm, hub, telemetry, kb, tools, scheduler)
    """
    from brain.llm_router import LLMRouter
    from core.agent_loop import AgentLoop
    from core.commander import Commander, ProgressHub
    from core.gateway import Gateway
    from core.knowledge_base import KnowledgeBase
    from core.memory import Memory
    from core.router import Router
    from core.scheduler import Scheduler
    from core.telemetry import Telemetry
    from core.tool_registry import ToolRegistry

    from agents.code import CodeAgent
    from agents.content import ContentAgent
    from agents.leads import LeadsAgent
    from agents.markets import MarketsAgent
    from agents.research import ResearchAgent
    from agents.tasks import TasksAgent
    from agents.toolbox import ToolBox, register_toolbox

    # ── Core services ──────────────────────────────────────────────────
    memory = Memory(db_path="memory/tasks.db", context_path="memory/context.md")
    telemetry = Telemetry()
    llm = LLMRouter(telemetry=telemetry)
    hub = ProgressHub()

    # ── Knowledge Base ─────────────────────────────────────────────────
    kb = KnowledgeBase(memory, kb_dir="memory/kb")

    # ── Tool Registry (v3 agentic tools) ──────────────────────────────
    tools = ToolRegistry()

    # Build the scheduler (needed by toolbox before toolbox is built)
    # We'll wire the run_fn after AgentLoop is created
    scheduler_placeholder = None

    toolbox = ToolBox(memory=memory, kb=kb, scheduler=scheduler_placeholder)
    register_toolbox(tools, toolbox)

    # ── AgentLoop ─────────────────────────────────────────────────────
    agent_loop = AgentLoop(
        llm=llm,
        tools=tools,
        memory=memory,
        telemetry=telemetry,
        progress_hub=hub,
    )

    # ── Scheduler ─────────────────────────────────────────────────────
    async def _scheduled_run(session_id: str, prompt: str) -> str:
        return await agent_loop.run(prompt, session_id=session_id)

    scheduler = Scheduler(memory=memory, run_fn=_scheduled_run)
    toolbox._scheduler = scheduler   # inject now that it exists

    # ── v2 Agent Router (fallback for /plan mode) ─────────────────────
    router = Router()
    router.register(ResearchAgent(llm, memory))
    router.register(ContentAgent(llm))
    router.register(LeadsAgent(llm))
    router.register(TasksAgent(llm, memory))
    router.register(MarketsAgent(llm, memory))
    router.register(CodeAgent(llm))

    # ── Commander ──────────────────────────────────────────────────────
    commander = Commander(
        llm=llm,
        router=router,
        memory=memory,
        agent_loop=agent_loop,
        kb=kb,
        scheduler=scheduler,
        telemetry=telemetry,
        progress_hub=hub,
    )

    gateway = Gateway(handler=commander.handle)

    return gateway, memory, router, llm, hub, telemetry, kb, tools, scheduler


def main() -> None:
    _load_env()
    _setup_logging()
    log = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(description="XClaw v3 — NavOS AI Executive Assistant")
    parser.add_argument("--interface", choices=["cli", "telegram", "web"], default="cli")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    gateway, memory, router, llm, hub, telemetry, kb, tools, scheduler = _build_xclaw()

    providers = llm.available_providers()
    log.info("XClaw v3 starting — interface: %s", args.interface)
    log.info("LLM providers: %s", providers or ["NONE — set at least one API key in .env"])
    log.info("Tools registered: %d", len(tools.tool_names()))

    if args.interface == "cli":
        from interface.cli import run_cli
        # Run scheduler in background alongside CLI
        async def _run_cli():
            asyncio.create_task(scheduler.run_forever())
            from interface.cli import CLIInterface
            cli = CLIInterface(gateway, memory, router)
            await cli.run()
        asyncio.run(_run_cli())

    elif args.interface == "telegram":
        async def _run_telegram():
            asyncio.create_task(scheduler.run_forever())
            from interface.telegram import run_telegram
            run_telegram(gateway)
        asyncio.run(_run_telegram())

    elif args.interface == "web":
        try:
            import uvicorn
        except ImportError:
            print("uvicorn not installed. Run: pip install uvicorn")
            sys.exit(1)
        from interface.web.app import create_app
        app = create_app(
            gateway, memory,
            hub=hub,
            llm_router=llm,
            telemetry=telemetry,
            kb=kb,
            tools=tools,
        )

        # Add scheduler startup to FastAPI lifespan
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def lifespan(application):
            task = asyncio.create_task(scheduler.run_forever())
            yield
            task.cancel()

        app.router.lifespan_context = lifespan

        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
