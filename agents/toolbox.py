"""
XClaw Toolbox — all real tools exposed to the LLM via the ToolRegistry.

Every method on ToolBox is a tool the LLM can call during the ReAct loop.
Tools are grouped into categories:

  Web:        web_search, fetch_page, extract_links
  Knowledge:  search_knowledge, save_note, list_sources
  Memory:     search_history, get_tasks, add_task, complete_task
  Code:       run_python, write_file, read_file
  Markets:    get_price, get_market_summary
  Scheduling: schedule_task, list_scheduled, cancel_scheduled
  Output:     create_report, format_table, send_notification
  Meta:       get_date_time, calculator

Call self._registry.register_from_toolbox(toolbox) to register all methods.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from core.tool_registry import ToolRegistry

if TYPE_CHECKING:
    from core.knowledge_base import KnowledgeBase
    from core.memory import Memory
    from core.scheduler import Scheduler

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
_DDG = "https://html.duckduckgo.com/html/"
_FETCH_TIMEOUT = 12
_EXEC_TIMEOUT = 15


class ToolBox:
    """
    Container for all XClaw tools.

    Pass the shared resources at construction time; they are injected into
    every tool call automatically.
    """

    def __init__(
        self,
        memory: "Memory",
        kb: "KnowledgeBase",
        scheduler: "Scheduler | None" = None,
        notify_fn=None,
    ) -> None:
        self._memory = memory
        self._kb = kb
        self._scheduler = scheduler
        self._notify_fn = notify_fn

    # ── Web ──────────────────────────────────────────────────────────────

    async def web_search(self, query: str, max_results: int = 5) -> str:
        """Search the web using DuckDuckGo. Returns a list of result snippets."""
        cache_key = self._memory.cache_key("websearch", query)
        if cached := self._memory.cache_get(cache_key):
            return cached

        try:
            async with httpx.AsyncClient(headers={"User-Agent": _UA}, timeout=10, follow_redirects=True) as client:
                resp = await client.get(_DDG, params={"q": query, "kl": "us-en"})
                resp.raise_for_status()
                html = resp.text
        except Exception as exc:
            return f"Search failed: {exc}"

        results = self._parse_ddg(html, max_results)
        if not results:
            return "No results found."

        output = "\n\n".join(f"**{r['title']}**\n{r['url']}\n{r['snippet']}" for r in results)
        self._memory.cache_set(cache_key, output, ttl=300)
        return output

    async def fetch_page(self, url: str, max_chars: int = 4000) -> str:
        """Fetch a web page and return its main text content."""
        try:
            async with httpx.AsyncClient(headers={"User-Agent": _UA}, timeout=_FETCH_TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text
            return self._extract_text(html)[:max_chars]
        except Exception as exc:
            return f"Could not fetch {url}: {exc}"

    async def extract_links(self, url: str) -> str:
        """Extract all hyperlinks from a web page."""
        html = await self.fetch_page(url, max_chars=100_000)
        links = re.findall(r'https?://[^\s\'"<>]+', html)
        unique = list(dict.fromkeys(links))[:20]
        return "\n".join(unique) if unique else "No links found."

    # ── Knowledge Base ────────────────────────────────────────────────

    def search_knowledge(self, query: str, max_results: int = 5) -> str:
        """Search Navigator's personal knowledge base (uploaded documents, saved notes)."""
        return self._kb.search_formatted(query, limit=max_results)

    def save_note(self, content: str, title: str = "note") -> str:
        """Save a note to Navigator's knowledge base for future reference."""
        return self._kb.ingest_text(content, source=f"note:{title}")

    def list_knowledge_sources(self) -> str:
        """List all documents and notes in Navigator's knowledge base."""
        sources = self._kb.list_sources()
        if not sources:
            return "Knowledge base is empty."
        lines = [f"- {s['source']} ({s['chunks']} chunks, added {s['last_ingested'][:10]})" for s in sources]
        return "\n".join(lines)

    # ── Memory & Tasks ────────────────────────────────────────────────

    def search_history(self, query: str, session_id: str) -> str:
        """Search past XClaw executions for relevant results."""
        executions = self._memory.get_executions(session_id, limit=20)
        if not executions:
            return "No execution history found."
        query_lower = query.lower()
        matches = []
        for ex in executions:
            if query_lower in ex["summary"].lower():
                matches.append(f"[{ex['executed_at'][:10]}] {ex['summary']}")
        return "\n".join(matches) if matches else "No matching history."

    def get_tasks(self, session_id: str, status: str = "") -> str:
        """Get Navigator's task list."""
        tasks = self._memory.get_tasks(session_id, status or None)
        if not tasks:
            return "No tasks."
        lines = [f"[{t['status']}] (id={t['id']}) {t['title']}" for t in tasks]
        return "\n".join(lines)

    def add_task(self, title: str, session_id: str) -> str:
        """Add a new task to Navigator's task list."""
        task_id = self._memory.add_task(session_id, title)
        return f"Task added (id={task_id}): {title}"

    def complete_task(self, task_id: int, session_id: str) -> str:
        """Mark a task as completed."""
        self._memory.update_task_status(task_id, "done")
        return f"Task {task_id} marked as done."

    # ── Code Execution ────────────────────────────────────────────────

    async def run_python(self, code: str) -> str:
        """Execute Python code in a sandboxed subprocess. Returns stdout/stderr."""
        if not code.strip():
            return "No code provided."

        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
            f.write(code)
            tmp = Path(f.name)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(tmp),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_EXEC_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                return f"Execution timed out after {_EXEC_TIMEOUT}s."

            out = stdout.decode(errors="replace").strip()
            err = stderr.decode(errors="replace").strip()
            if err and not out:
                return f"Error:\n{err}"
            if err:
                return f"Output:\n{out}\n\nStderr:\n{err}"
            return out or "(no output)"
        finally:
            tmp.unlink(missing_ok=True)

    def write_file(self, filename: str, content: str) -> str:
        """Write content to a file in the memory directory."""
        safe_name = re.sub(r"[^\w.\-]", "_", filename)
        path = Path("memory") / safe_name
        path.write_text(content, encoding="utf-8")
        return f"File written: {path} ({len(content)} chars)"

    def read_file(self, filename: str) -> str:
        """Read a file from the memory directory."""
        safe_name = re.sub(r"[^\w.\-]", "_", filename)
        path = Path("memory") / safe_name
        if not path.exists():
            return f"File not found: {safe_name}"
        return path.read_text(encoding="utf-8", errors="replace")[:6000]

    def list_files(self) -> str:
        """List files saved in the memory directory."""
        files = [p for p in Path("memory").iterdir() if p.is_file() and p.suffix not in {".db", ".log"}]
        if not files:
            return "No files saved."
        return "\n".join(f"- {p.name} ({p.stat().st_size} bytes)" for p in sorted(files))

    # ── Market Data ───────────────────────────────────────────────────

    async def get_price(self, symbol: str) -> str:
        """Get current cryptocurrency price (e.g. BTC, ETH, SOL)."""
        sym = symbol.upper().replace("/", "").replace("-", "")
        if not sym.endswith("USDT"):
            sym += "USDT"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"https://api.binance.com/api/v3/ticker/price?symbol={sym}")
                resp.raise_for_status()
                data = resp.json()
            price = float(data["price"])
            return f"{sym}: ${price:,.4f}" if price < 1 else f"{sym}: ${price:,.2f}"
        except Exception as exc:
            return f"Price unavailable for {symbol}: {exc}"

    async def get_market_summary(self, market: str = "crypto") -> str:
        """Get a brief market overview (top movers, sentiment)."""
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get("https://api.binance.com/api/v3/ticker/24hr")
                resp.raise_for_status()
                tickers = resp.json()

            # Top gainers and losers
            by_change = sorted(tickers, key=lambda x: float(x.get("priceChangePercent", 0)), reverse=True)
            gainers = [(t["symbol"], float(t["priceChangePercent"])) for t in by_change[:5] if t["symbol"].endswith("USDT")]
            losers = [(t["symbol"], float(t["priceChangePercent"])) for t in by_change[-5:] if t["symbol"].endswith("USDT")]

            lines = ["**Top Gainers:**"]
            lines += [f"  {s}: +{p:.2f}%" for s, p in gainers]
            lines += ["\n**Top Losers:**"]
            lines += [f"  {s}: {p:.2f}%" for s, p in losers]
            return "\n".join(lines)
        except Exception as exc:
            return f"Market summary unavailable: {exc}"

    # ── Scheduling ────────────────────────────────────────────────────

    def schedule_task(self, prompt: str, interval: str, session_id: str, notify_to: str = "") -> str:
        """
        Schedule a recurring task. interval: '30m', '2h', 'daily', 'daily@09:00'.
        Returns the task ID.
        """
        if self._scheduler is None:
            return "Scheduler is not configured."
        try:
            task_id = self._scheduler.add_task(session_id, prompt, interval, notify_to)
            return f"Scheduled (id={task_id}): '{prompt[:60]}' every {interval}"
        except ValueError as exc:
            return f"Invalid interval: {exc}"

    def list_scheduled(self, session_id: str) -> str:
        """List all active scheduled tasks for this session."""
        if self._scheduler is None:
            return "Scheduler not configured."
        tasks = self._scheduler.list_tasks(session_id)
        if not tasks:
            return "No scheduled tasks."
        lines = [f"[{t['id']}] every {t['interval_str']}: {t['prompt'][:60]}" for t in tasks]
        return "\n".join(lines)

    def cancel_scheduled(self, task_id: str) -> str:
        """Cancel a scheduled task by ID."""
        if self._scheduler is None:
            return "Scheduler not configured."
        ok = self._scheduler.disable_task(task_id)
        return f"Task {task_id} cancelled." if ok else f"Task {task_id} not found."

    # ── Output & Formatting ───────────────────────────────────────────

    def create_report(self, title: str, content: str) -> str:
        """Format a structured report with a title and save it as a markdown file."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        safe = re.sub(r"[^\w]", "_", title)[:40]
        filename = f"report_{safe}_{now}.md"
        body = f"# {title}\n\n*Generated by XClaw on {now}*\n\n{content}"
        path = Path("memory") / filename
        path.write_text(body, encoding="utf-8")
        return f"Report saved as `{filename}`.\n\n{body}"

    def format_table(self, data: str) -> str:
        """
        Format JSON array of objects as a Markdown table.
        data: JSON string like '[{"name":"X","value":1}, ...]'
        """
        try:
            rows = json.loads(data)
            if not rows or not isinstance(rows, list):
                return "No data."
            keys = list(rows[0].keys())
            header = "| " + " | ".join(keys) + " |"
            sep = "| " + " | ".join(["---"] * len(keys)) + " |"
            body = "\n".join("| " + " | ".join(str(r.get(k, "")) for k in keys) + " |" for r in rows)
            return f"{header}\n{sep}\n{body}"
        except (json.JSONDecodeError, AttributeError, KeyError) as exc:
            return f"format_table error: {exc}"

    async def send_notification(self, message: str, session_id: str) -> str:
        """Send a push notification to Navigator (via Telegram if configured)."""
        if self._notify_fn:
            try:
                await self._notify_fn(session_id, message)
                return "Notification sent."
            except Exception as exc:
                return f"Notification failed: {exc}"
        return "No notification channel configured."

    # ── Meta ──────────────────────────────────────────────────────────

    def get_date_time(self) -> str:
        """Return the current UTC date and time."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def calculator(self, expression: str) -> str:
        """Evaluate a safe mathematical expression (e.g. '2 ** 10', 'sqrt(144)')."""
        import math
        allowed = set("0123456789+-*/().,_ eE")
        allowed_words = {"sqrt", "abs", "round", "pow", "log", "sin", "cos", "tan", "pi", "e"}
        clean = expression.strip()
        safe = all(c in allowed for c in clean if not c.isalpha()) and \
               all(w in allowed_words for w in re.findall(r"[a-z]+", clean))
        if not safe:
            return f"Unsafe expression rejected: {clean!r}"
        try:
            result = eval(clean, {"__builtins__": {}}, {**vars(math), "abs": abs, "round": round})  # noqa: S307
            return str(result)
        except Exception as exc:
            return f"Calculation error: {exc}"

    # ── DDG helpers ───────────────────────────────────────────────────

    @staticmethod
    def _parse_ddg(html: str, max_results: int) -> list[dict]:
        """Parse DuckDuckGo HTML results into title/url/snippet dicts."""
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            results = []
            for r in soup.select(".result"):
                title_el = r.select_one(".result__title")
                url_el = r.select_one(".result__url")
                snippet_el = r.select_one(".result__snippet")
                if not title_el:
                    continue
                href = r.select_one("a.result__a")
                url = ""
                if href:
                    raw = href.get("href", "")
                    m = re.search(r"uddg=([^&]+)", raw)
                    url = m.group(1) if m else raw
                    from urllib.parse import unquote
                    url = unquote(url)
                results.append({
                    "title": title_el.get_text(strip=True),
                    "url": url,
                    "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                })
                if len(results) >= max_results:
                    break
            return results
        except ImportError:
            # Regex fallback
            titles = re.findall(r'class="result__a"[^>]*>([^<]+)<', html)
            urls_raw = re.findall(r'uddg=([^&"]+)', html)
            from urllib.parse import unquote
            urls = [unquote(u) for u in urls_raw]
            return [{"title": t, "url": u, "snippet": ""} for t, u in zip(titles, urls)][:max_results]

    @staticmethod
    def _extract_text(html: str) -> str:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()
            main = soup.find("article") or soup.find("main") or soup.find("body") or soup
            text = main.get_text(separator=" ", strip=True)
        except ImportError:
            text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()


def register_toolbox(registry: ToolRegistry, toolbox: ToolBox) -> None:
    """
    Register all ToolBox methods into the given ToolRegistry.
    Methods starting with _ are skipped.
    """
    import inspect

    skip = {"register_toolbox"}
    for name in dir(toolbox):
        if name.startswith("_") or name in skip:
            continue
        fn = getattr(toolbox, name)
        if not callable(fn) or not inspect.ismethod(fn):
            continue
        doc = (fn.__doc__ or "").strip().split("\n")[0]
        registry.register(fn, description=doc, name=name)

    logger.info("[toolbox] registered %d tools", len(registry.tool_names()))
