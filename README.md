<div align="center">

# 🦅 XClaw

### Your AI Executive Assistant — Self-Hosted, Open Source, Actually Useful

**Search the web · Browse GitHub · Check the weather · Send email · Run code · Remember everything**
**— all from a single chat window or Telegram.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Free Tier Friendly](https://img.shields.io/badge/cloud-free%20tier%20friendly-brightgreen)](#llm-providers)
[![Ollama](https://img.shields.io/badge/Ollama-100%25%20local-orange)](https://ollama.com)

</div>

---

## What is XClaw?

XClaw is an **agentic AI assistant** that actually *does* things — not just talks about doing them.

It runs a **ReAct loop** (Reason → Act → Observe → repeat) powered by any LLM you choose, using 30+ real tools to search the web, browse GitHub, fetch live weather, read RSS feeds, run Python code, manage tasks, and more.

**Self-hosted. No subscription. Yours.**

```
You: "Search GitHub for the top Rust web frameworks, check their HN discussions,
      and save a comparison report."

XClaw: [searches GitHub] → [fetches HN threads] → [saves report] →
       "Here's your comparison: Axum leads with 17k ⭐, followed by Actix-web..."
```

---

## ✨ What it can do

| Category | Tools |
|---|---|
| 🌐 **Web** | DuckDuckGo search, page fetching, link extraction |
| 📰 **News** | Hacker News, Reddit, any RSS/Atom feed |
| 🐙 **GitHub** | Search repos, trending, README, issues, PRs, create issues |
| 🌍 **Info** | Wikipedia summaries, worldwide weather forecasts |
| 📧 **Email** | Send email via SMTP (Gmail, Outlook, custom) |
| 🐍 **Code** | Execute Python in a sandboxed subprocess |
| 📁 **Files** | Read, write, and list files in memory directory |
| 📊 **Markets** | Live crypto prices, top gainers/losers (Binance API) |
| ⏰ **Schedule** | Recurring tasks ("check HN every 2h", "daily market brief") |
| 🧠 **Memory** | Persistent conversation history, task list, knowledge base |
| 📚 **Knowledge** | Ingest your docs (PDF, MD, TXT) and search them later |

**All free APIs — no paid keys required to get started.**

---

## 🚀 Quickstart (3 commands)

```bash
git clone https://github.com/navigatorxm/OpenClaw.git && cd OpenClaw
bash scripts/quickstart.sh
python main.py
```

Open **http://localhost:8000** — you're in.

> **No LLM key?** Install [Ollama](https://ollama.com) and run `ollama pull llama3.2` for 100% local, free, private inference.

---

## 🔌 LLM Providers

XClaw tries providers in order until one succeeds. Mix and match:

| Provider | Free Tier | Speed | Privacy | Setup |
|---|---|---|---|---|
| **Ollama** (local) | ♾️ unlimited | fast | 🔒 100% local | `ollama pull llama3.2` |
| **Groq** | 6k tok/min | ⚡ fastest | cloud | [console.groq.com](https://console.groq.com) |
| **Google Gemini** | generous | fast | cloud | [aistudio.google.com](https://aistudio.google.com/apikey) |
| **OpenAI** | pay-as-you-go | fast | cloud | [platform.openai.com](https://platform.openai.com/api-keys) |
| **OVH AI** | free trial | medium | 🇪🇺 EU | [endpoints.ai.cloud.ovh.net](https://endpoints.ai.cloud.ovh.net) |

Set your key(s) in `.env` — XClaw auto-detects which ones are available.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Interfaces                            │
│   Web (FastAPI + SSE)  │  Telegram  │  CLI             │
└──────────────┬──────────────────────────────────────────┘
               │ Gateway normalises all channels
               ▼
┌─────────────────────────────────────────────────────────┐
│                  Commander                              │
│   routes intent → AgentLoop  /plan → wave executor     │
└──────────────┬──────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────┐
│                Agent Loop (ReAct)                       │
│                                                         │
│   LLM ──→ tool calls ──→ parallel execution            │
│    ▲              │                                     │
│    └──────────────┘  (up to 20 iterations)             │
└──────────────┬──────────────────────────────────────────┘
               │
       ┌───────┴────────┐
       ▼                ▼
┌──────────────┐  ┌─────────────────────────────────────┐
│  LLM Router  │  │           Tool Registry             │
│              │  │                                     │
│  OVH / Groq  │  │  Web · GitHub · News · Email        │
│  Gemini /    │  │  Code · Files · Markets             │
│  OpenAI /    │  │  Schedule · Knowledge · Memory      │
│  Ollama      │  │  (30+ tools, easily extensible)     │
└──────────────┘  └─────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────┐
│  Memory (SQLite WAL)  │  KnowledgeBase  │  Scheduler   │
└─────────────────────────────────────────────────────────┘
```

**Key design decisions:**
- **ReAct loop** — LLM reasons step by step, calling tools as needed
- **Parallel tool execution** — multiple tool calls from one LLM response run concurrently
- **Circuit breaker** — dead providers are skipped, auto-retry after 60s
- **Context compression** — long conversations summarised automatically
- **No vendor lock-in** — swap any LLM provider in one config line

---

## 📁 Project Structure

```
OpenClaw/
├── main.py                     # Entry point — wires everything together
├── brain/
│   ├── config.yaml             # LLM providers, model settings
│   └── llm_router.py           # Provider routing, tool calling, streaming
├── core/
│   ├── agent_loop.py           # ReAct loop — the brain
│   ├── commander.py            # Intent routing, /plan, slash commands
│   ├── gateway.py              # Channel normalisation (Web/Telegram/CLI)
│   ├── knowledge_base.py       # Document ingestion + TF-IDF search
│   ├── memory.py               # SQLite persistence (history, tasks, cache)
│   ├── router.py               # Specialist agent dispatch
│   ├── scheduler.py            # Recurring task engine
│   ├── telemetry.py            # Latency P95, token budgets, traces
│   └── tool_registry.py        # Tool schema + dispatch
├── agents/
│   ├── toolbox.py              # 30+ LLM-callable tools
│   ├── base.py                 # Retry + timeout base class
│   ├── research.py             # Deep web research agent
│   └── integrations/
│       ├── github_tools.py     # 8 GitHub tools (search, trending, issues…)
│       ├── news_tools.py       # HN, Wikipedia, Weather, RSS, Reddit
│       └── communication_tools.py  # Email send/draft
├── interface/
│   └── web/app.py              # FastAPI dashboard + SSE streaming
├── scripts/
│   └── quickstart.sh           # Zero-friction setup
├── .env.example                # Configuration template
└── requirements.txt
```

---

## 💬 Slash Commands

| Command | Description |
|---|---|
| `/plan <goal>` | Break goal into parallel steps, approve then execute |
| `/tasks` | Show your task list |
| `/history` | Recent execution history |
| `/kb` | Knowledge base stats |
| `/sources` | List ingested documents |
| `/schedule <task> <interval>` | Schedule recurring task |
| `/scheduled` | List scheduled tasks |
| `/metrics` | Token usage, latency P95, provider health |
| `/help` | All commands |

---

## 🔧 Configuration

```bash
cp .env.example .env
# Set at least one LLM key, then:
python main.py
```

**Minimum config for each interface:**

```bash
# Web only (no key needed with Ollama):
OLLAMA_HOST=http://localhost:11434

# + Telegram:
TELEGRAM_BOT_TOKEN=your_bot_token

# + GitHub tools (60 req/hr free, 5000 req/hr with token):
GITHUB_TOKEN=ghp_...

# + Email:
SMTP_USER=you@gmail.com
SMTP_PASS=your_app_password  # from myaccount.google.com/apppasswords
```

---

## 🛠️ Extend XClaw

Adding a new tool takes 5 lines:

```python
# agents/integrations/my_tools.py
async def my_custom_tool(query: str, limit: int = 10) -> str:
    """One-line description the LLM uses to decide when to call this tool."""
    # ... your implementation
    return result
```

Register it in `agents/toolbox.py`:
```python
from agents.integrations import my_tools
_integration_modules = [..., my_tools]
```

The LLM automatically discovers and uses your tool.

---

## 📊 Real Use Cases

**Morning brief:**
> "Schedule a daily 8am brief: check HN top 5, BTC price, and my pending tasks"

**Research:**
> "Find the top 5 Python async frameworks on GitHub, get their READMEs, and create a comparison report"

**GitHub workflow:**
> "List open issues on navigatorxm/OpenClaw and create an issue for the KB schema bug"

**Market watch:**
> "What's the current BTC price and top 5 crypto gainers today?"

**Document Q&A:**
> Upload a PDF → "Summarise the key points from my research paper"

**Automation:**
> "Every 2 hours, check /r/MachineLearning for posts over 100 upvotes and notify me"

---

## 🤝 Contributing

1. Fork the repo
2. Create a branch: `git checkout -b feature/my-tool`
3. Add your tool in `agents/integrations/`
4. Open a PR

All contributions welcome — new tools, provider integrations, UI improvements.

---

## 📄 License

MIT — use it, fork it, build on it.

---

<div align="center">

**Built with ❤️ by [Navigator](https://github.com/navigatorxm)**

*XClaw: because your AI assistant should actually do things.*

</div>
