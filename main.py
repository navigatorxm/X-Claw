"""
XClaw — entry point.

Usage:
    python main.py                    # defaults to CLI
    python main.py --interface cli
    python main.py --interface telegram
    python main.py --interface web [--host 0.0.0.0] [--port 8000]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
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
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    Path("memory/logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=getattr(logging, level, logging.INFO),
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("memory/logs/xclaw.log", encoding="utf-8"),
        ],
    )


def _build_xclaw():
    """Assemble the full XClaw stack. Returns (gateway, memory, router, llm, hub)."""
    from brain.llm_router import LLMRouter
    from core.commander import Commander, ProgressHub
    from core.gateway import Gateway
    from core.memory import Memory
    from core.router import Router

    from agents.code import CodeAgent
    from agents.content import ContentAgent
    from agents.leads import LeadsAgent
    from agents.markets import MarketsAgent
    from agents.research import ResearchAgent
    from agents.tasks import TasksAgent

    memory = Memory(db_path="memory/tasks.db", context_path="memory/context.md")
    llm = LLMRouter()
    router = Router()
    hub = ProgressHub()

    router.register(ResearchAgent(llm, memory))
    router.register(ContentAgent(llm))
    router.register(LeadsAgent(llm))
    router.register(TasksAgent(llm, memory))
    router.register(MarketsAgent(llm, memory))
    router.register(CodeAgent(llm))

    commander = Commander(llm=llm, router=router, memory=memory, progress_hub=hub)
    gateway = Gateway(handler=commander.handle)

    return gateway, memory, router, llm, hub


def main() -> None:
    _load_env()
    _setup_logging()

    parser = argparse.ArgumentParser(description="XClaw — NavOS AI Executive Assistant")
    parser.add_argument("--interface", choices=["cli", "telegram", "web"], default="cli")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    gateway, memory, router, llm, hub = _build_xclaw()
    log = logging.getLogger(__name__)
    log.info("XClaw v2 starting — interface: %s", args.interface)
    log.info("Available LLM providers: %s", llm.available_providers() or ["none — check .env"])

    if args.interface == "cli":
        from interface.cli import run_cli
        run_cli(gateway, memory, router)

    elif args.interface == "telegram":
        from interface.telegram import run_telegram
        run_telegram(gateway)

    elif args.interface == "web":
        try:
            import uvicorn
        except ImportError:
            print("uvicorn not installed. Run: pip install uvicorn")
            sys.exit(1)
        from interface.web.app import create_app
        app = create_app(gateway, memory, hub=hub, llm_router=llm)
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
