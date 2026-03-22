"""
XClaw Markets Agent — price monitoring and market analysis.

Supported actions:
  - "price"     → get current price for params["symbol"]
  - "alert"     → register a price alert (stored in memory as a task)
  - "analyse"   → LLM market commentary for params["symbol"] or params["topic"]
  - "summary"   → summarise market conditions for params.get("market", "crypto")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agents.base import BaseAgent

if TYPE_CHECKING:
    from brain.llm_router import LLMRouter
    from core.memory import Memory

logger = logging.getLogger(__name__)

_ANALYSE_PROMPT = """\
Provide a concise market analysis for {symbol}.
Cover: current sentiment, recent price action, key support/resistance levels,
and a short-term outlook. Use bullet points.
"""

_SUMMARY_PROMPT = """\
Give a brief overview of current {market} market conditions.
Highlight the top movers, major news, and overall sentiment today.
"""


class MarketsAgent(BaseAgent):
    name = "markets"
    timeout_seconds = 30.0

    def __init__(self, llm: "LLMRouter", memory: "Memory") -> None:
        self._llm = llm
        self._memory = memory

    async def _run(self, action: str, params: dict, session_id: str) -> str:
        a = action.lower()

        if "price" in a or "quote" in a:
            return await self._price(params.get("symbol", "BTC"), session_id)

        if "alert" in a:
            return self._register_alert(params, session_id)

        if "analys" in a or "analysis" in a:
            return await self._analyse(params.get("symbol", params.get("topic", "BTC")), session_id)

        if "summary" in a or "overview" in a:
            return await self._summary(params.get("market", "crypto"), session_id)

        # Default: analysis
        return await self._analyse(params.get("symbol", action), session_id)

    async def _price(self, symbol: str, session_id: str) -> str:
        """
        Attempt a live price fetch via a public API; fall back to LLM knowledge.
        A real deployment would use a dedicated exchange/data API key.
        """
        symbol = symbol.upper().replace("/", "_")
        try:
            import urllib.request, json  # noqa: E401
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}USDT"
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
                data = json.loads(resp.read())
            return f"{symbol}/USDT: ${float(data['price']):,.2f}"
        except Exception:  # noqa: BLE001
            return await self._analyse(symbol, session_id)

    def _register_alert(self, params: dict, session_id: str) -> str:
        symbol = params.get("symbol", "?")
        price = params.get("price", "?")
        direction = params.get("direction", "reaches")
        title = f"ALERT: {symbol} {direction} {price}"
        task_id = self._memory.add_task(session_id, title)
        return f"Price alert registered (id={task_id}): {title}"

    async def _analyse(self, symbol: str, session_id: str) -> str:
        logger.info("[markets] analyse: %s", symbol)
        prompt = _ANALYSE_PROMPT.format(symbol=symbol)
        return await self._llm.complete(prompt, session_id=session_id)

    async def _summary(self, market: str, session_id: str) -> str:
        logger.info("[markets] summary: %s", market)
        prompt = _SUMMARY_PROMPT.format(market=market)
        return await self._llm.complete(prompt, session_id=session_id)
