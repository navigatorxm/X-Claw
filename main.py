"""
XClaw v3 — entry point.

First time setup:
    bash scripts/setup.sh

Usage:
    python main.py                              # Web dashboard (default)
    python main.py --interface web              # Web dashboard + API
    python main.py --interface telegram         # Telegram bot only
    python main.py --interface all              # Web + Telegram simultaneously
    python main.py --interface cli              # CLI (no web server)
    python main.py --port 8080                  # Custom port
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
    from agents.swarm import make_swarm_tool
    from core.plugin_manager import PluginManager

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

    # ── Agent Swarm ───────────────────────────────────────────────────
    swarm_fn = make_swarm_tool(llm=llm, tools=tools, hub=hub)
    tools.register(swarm_fn, description="Dispatch parallel AI agents to complete a complex task", name="swarm_task")

    # ── Plugin Manager ────────────────────────────────────────────────
    plugin_manager = PluginManager(memory=memory)
    plugin_manager.scan()
    plugin_manager.register_all(tools)

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

    return gateway, memory, router, llm, hub, telemetry, kb, tools, scheduler, plugin_manager


def main() -> None:
    _load_env()
    _setup_logging()
    log = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(description="XClaw v3 — NavOS AI Executive Assistant")
    parser.add_argument("--interface", choices=["cli", "telegram", "web", "all"], default="web")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = parser.parse_args()

    gateway, memory, router, llm, hub, telemetry, kb, tools, scheduler, plugin_manager = _build_xclaw()

    providers = llm.available_providers()
    log.info("XClaw v3 starting — interface: %s", args.interface)
    log.info("LLM providers: %s", providers or ["NONE — set at least one API key in .env"])
    log.info("Tools registered: %d (inc. %d plugin tools)", len(tools.tool_names()), sum(len(p.tools) for p in plugin_manager._plugins.values() if p.enabled and p.loaded))

    if not providers:
        print("\n" + "="*60)
        print("  ⚠  No LLM providers configured!")
        print("  Run:  bash scripts/setup.sh")
        print("  Or:   edit .env and set at least one API key")
        print("="*60 + "\n")

    async def _boot_mcp():
        """Load MCP servers if mcp_servers.json exists."""
        try:
            from core.mcp_client import load_mcp_servers
            n = await load_mcp_servers(tools)
            if n > 0:
                log.info("MCP: loaded %d additional tools", n)
        except Exception as exc:
            log.warning("MCP boot failed: %s", exc)

    def _make_web_app():
        from interface.web.app import create_app
        return create_app(
            gateway, memory,
            hub=hub,
            llm_router=llm,
            telemetry=telemetry,
            kb=kb,
            tools=tools,
            plugin_manager=plugin_manager,
        )

    if args.interface == "cli":
        async def _run_cli():
            asyncio.create_task(scheduler.run_forever())
            await _boot_mcp()
            from interface.cli import CLIInterface
            cli = CLIInterface(gateway, memory, router)
            await cli.run()
        asyncio.run(_run_cli())

    elif args.interface == "telegram":
        async def _run_telegram():
            asyncio.create_task(scheduler.run_forever())
            await _boot_mcp()
            from interface.telegram import run_telegram
            run_telegram(gateway)
        asyncio.run(_run_telegram())

    elif args.interface in ("web", "all"):
        try:
            import uvicorn
        except ImportError:
            print("uvicorn not installed. Run: pip install uvicorn")
            sys.exit(1)

        app = _make_web_app()

        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def lifespan(application):
            # Boot MCP servers
            await _boot_mcp()
            sched_task = asyncio.create_task(scheduler.run_forever())
            # Start Telegram alongside web if --interface all
            tg_task = None
            if args.interface == "all":
                tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
                if tg_token:
                    try:
                        from interface.telegram import run_telegram_async
                        tg_task = asyncio.create_task(run_telegram_async(gateway))
                        log.info("Telegram bot started alongside web interface")
                    except Exception as exc:
                        log.warning("Telegram failed to start: %s", exc)
                else:
                    log.warning("--interface all: TELEGRAM_BOT_TOKEN not set, skipping Telegram")
            yield
            sched_task.cancel()
            if tg_task:
                tg_task.cancel()

        app.router.lifespan_context = lifespan

        print(f"\n  ⚡ XClaw v3 — Dashboard: http://{args.host}:{args.port}")
        if args.interface == "all":
            print(f"  🤖 Telegram bot: active")
        print()

        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
