<div align="center">

# 🦅 XClaw

### Your AI Executive Assistant — Self-Hosted, Open Source, Actually Executes

**Research · Browse · Code · Email · Schedule · Remember · Run Agent Swarms**
**— from a chat dashboard, Telegram, or CLI. On your VPS, Pi, Jetson Nano, or laptop.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Free Tier Friendly](https://img.shields.io/badge/cloud-free%20tier%20friendly-brightgreen)](#-llm-providers)
[![Ollama](https://img.shields.io/badge/Ollama-100%25%20local-orange)](https://ollama.com)

</div>

---

## What is XClaw?

XClaw is an **agentic AI assistant** that actually *does* things — not just talks about them.

It runs a **ReAct loop** (Reason → Act → Observe → repeat) powered by any LLM you choose, with 30+ real tools, a plugin skill library, parallel agent swarms, smart cost routing, and a full multi-tab dashboard.

**Self-hosted. No subscription. Yours.**

```
You: "Research the top 5 Rust web frameworks, compare their GitHub stats,
      and write a report — in parallel."

XClaw: [swarm: researcher + analyst + writer run simultaneously]
       → "Here's your comparison report: Axum leads with 17k ⭐..."
```

---

## ✨ What it can do

| Category | Capability |
|---|---|
| 🌐 **Web** | DuckDuckGo search, full page fetch, link extraction |
| 📰 **News** | Hacker News, Reddit, any RSS/Atom feed, Wikipedia |
| 🐙 **GitHub** | Search repos, trending, README, issues, PRs, create issues |
| 🌍 **Info** | Worldwide weather, Wikipedia summaries |
| 📧 **Email** | Send + read email (Gmail IMAP/SMTP), XClaw's own email identity |
| 🐍 **Code** | Run Python/Bash/Node, install packages, lint, format, git diff |
| 🌐 **Browser** | Headless browser automation, screenshots, form filling (Playwright optional) |
| 🎭 **Persona** | Each XClaw instance has its own identity, social handles, branded posts |
| 📁 **Files** | Read, write, list files in memory directory |
| 📊 **Markets** | Live crypto prices, top gainers/losers (Binance API) |
| ⏰ **Schedule** | Recurring tasks ("check HN every 2h", "daily market brief") |
| 🧠 **Memory** | Persistent conversation history, task list, knowledge base |
| 📚 **Knowledge** | Ingest PDF, MD, TXT, CSV → search with TF-IDF |
| 🐝 **Agent Swarm** | Decompose tasks → run parallel specialist agents → synthesise |
| 🔌 **MCP** | Connect any Model Context Protocol server (filesystem, DB, APIs) |
| 🧩 **Plugins** | Enable/disable skill modules from the dashboard |

**All cloud APIs have a free tier. Zero cost with Ollama local inference.**

---

## 🚀 Quickstart (3 commands)

```bash
git clone https://github.com/navigatorxm/X-Claw.git && cd X-Claw
bash scripts/setup.sh        # interactive: LLM key, Telegram, domain, nginx, systemd
python main.py
```

Open **http://localhost:8000** — full dashboard, ready to use.

> **No LLM key?** Install [Ollama](https://ollama.com) and run `ollama pull llama3.2` for 100% local, free, private inference.

> **VPS / Raspberry Pi / Jetson Nano?** See **[INSTALL.md](INSTALL.md)** for platform-specific guides.

---

## 🔌 LLM Providers

XClaw **auto-routes requests** to the cheapest model that can handle the job:

| Tier | When | Models |
|------|------|--------|
| **cheap** | ≤30 words, simple Q&A | Groq 8B · Gemini Flash · DO llama3-8b · Ollama |
| **standard** | Research, tool use, analysis | Groq 70B · Gemini Flash · OVH · OpenAI mini · DO llama3-70b |
| **premium** | Build / implement / architect | Gemini 1.5 Pro · GPT-4o · DO mistral |

Expensive models (Opus, GPT-4o) are **never called automatically** — only on premium-tier keywords.

**Supported providers:**

| Provider | Free Tier | Speed | Privacy | Get Key |
|---|---|---|---|---|
| **Ollama** (local) | ♾️ unlimited | fast | 🔒 100% local | [ollama.com](https://ollama.com) |
| **Groq** | 6k tok/min | ⚡ fastest | cloud | [console.groq.com](https://console.groq.com) |
| **Google Gemini** | generous | fast | cloud | [aistudio.google.com](https://aistudio.google.com/apikey) |
| **DigitalOcean GenAI** | pay-per-token | fast | cloud | [cloud.digitalocean.com/gen-ai](https://cloud.digitalocean.com/gen-ai) |
| **OpenAI** | pay-per-token | fast | cloud | [platform.openai.com](https://platform.openai.com/api-keys) |
| **OVH AI** | free trial | medium | 🇪🇺 EU | [endpoints.ai.cloud.ovh.net](https://endpoints.ai.cloud.ovh.net) |

Set keys in `.env` — XClaw auto-detects available ones and routes intelligently.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Interfaces                           │
│   Web Dashboard (7 tabs)  │  Telegram Bot  │  CLI          │
└──────────────┬──────────────────────────────────────────────┘
               │ Gateway — normalises all channels
               ▼
┌─────────────────────────────────────────────────────────────┐
│                       Commander                             │
│   intent routing · /plan · slash commands · approval gate   │
└──────────────┬──────────────────────────────────────────────┘
               │
       ┌───────┴────────────────┐
       ▼                        ▼
┌──────────────────┐    ┌──────────────────────────────────┐
│  Agent Loop      │    │        Agent Swarm               │
│  (ReAct, 20 itr) │    │  decompose → parallel workers    │
│  LLM ↔ tools     │    │  → synthesise result             │
└──────────────────┘    └──────────────────────────────────┘
               │
    ┌──────────┴────────────┐
    ▼                       ▼
┌──────────────────┐  ┌────────────────────────────────────┐
│  Smart LLM Router│  │         Tool Registry              │
│                  │  │                                    │
│  cheap tier      │  │  30+ built-in tools                │
│  standard tier   │  │  + Plugin Skills (6 built-in)      │
│  premium tier    │  │  + MCP server tools (dynamic)      │
│  circuit breaker │  │  Web · GitHub · News · Email       │
│  cost tracking   │  │  Code · Browser · Markets          │
└──────────────────┘  │  Persona · Productivity · Writing  │
                      └────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────┐
│  Memory (SQLite WAL)  │  KnowledgeBase  │  Scheduler        │
│  Token Optimizer      │  Plugin Manager │  Telemetry        │
└─────────────────────────────────────────────────────────────┘
```

**Key design decisions:**
- **Smart tier routing** — cheap/standard/premium auto-selected per request; no expensive model called unnecessarily
- **Agent Swarm** — complex tasks decomposed into parallel specialist workers
- **Plugin system** — enable/disable skills from the dashboard, hot-reload without restart
- **MCP support** — plug in any Model Context Protocol server via `mcp_servers.json`
- **ReAct loop** — LLM reasons step by step, calling tools as needed, up to 20 iterations
- **Parallel tool execution** — multiple tool calls from one LLM response run concurrently
- **Circuit breaker** — dead providers skipped, auto-retry after 60s
- **Token optimizer** — semantic cache, TTL by tier, truncation before context overflow

---

## 📁 Project Structure

```
X-Claw/
├── main.py                         # Entry point — wires everything together
├── brain/
│   ├── config.yaml                 # LLM tiers, provider routing, model settings
│   └── llm_router.py               # Smart routing, tool calling, streaming, circuit breaker
├── core/
│   ├── agent_loop.py               # ReAct loop — the execution brain
│   ├── commander.py                # Intent routing, /plan, slash commands
│   ├── gateway.py                  # Channel normalisation (Web/Telegram/CLI)
│   ├── knowledge_base.py           # Document ingestion + TF-IDF search
│   ├── mcp_client.py               # Model Context Protocol client (stdio + HTTP)
│   ├── memory.py                   # SQLite persistence (history, tasks, cache)
│   ├── plugin_manager.py           # Plugin discovery, enable/disable, hot-reload
│   ├── router.py                   # Specialist agent dispatch
│   ├── scheduler.py                # Recurring task engine
│   ├── telemetry.py                # Latency P95, token budgets, traces
│   ├── token_optimizer.py          # Semantic cache, complexity routing, budget guard
│   └── tool_registry.py            # Tool schema + dispatch
├── agents/
│   ├── toolbox.py                  # 30+ LLM-callable built-in tools
│   ├── swarm.py                    # Agent Swarm orchestrator
│   ├── base.py                     # Retry + timeout base class
│   ├── research.py                 # Deep web research agent
│   └── integrations/
│       ├── github_tools.py         # GitHub: search, trending, issues, PRs
│       ├── news_tools.py           # HN, Wikipedia, Weather, RSS, Reddit
│       ├── communication_tools.py  # Email send/draft
│       └── gmail_tools.py          # Gmail identity: read inbox, search, send
├── plugins/                        # Skill plugins — auto-discovered at startup
│   ├── research_skill.py           # Web research, source finding, URL summarise
│   ├── coding_skill.py             # Run code, install packages, lint, git tools
│   ├── writing_skill.py            # Templates, word count, key points, clean text
│   ├── productivity_skill.py       # Task breakdown, time blocking, morning brief
│   ├── computer_skill.py           # Browser automation, screenshots, form filling
│   └── persona_skill.py            # Identity, social handles, branded post drafting
├── interface/
│   └── web/app.py                  # FastAPI: 7-tab dashboard, SSE streaming, APIs
├── scripts/
│   ├── setup.sh                    # Interactive wizard: LLM, Telegram, nginx, systemd
│   └── quickstart.sh               # Fast 60-second setup
├── Dockerfile                      # Container build (python:3.12-slim, ARM64 compatible)
├── docker-compose.yml              # Web + Telegram services with health checks
├── mcp_servers.json                # MCP server config (filesystem, browser, search…)
├── INSTALL.md                      # Platform install guide: VPS, Pi, Jetson, Docker
├── .env.example                    # All configuration keys with docs
└── requirements.txt                # 12 pure-Python packages
```

---

## 🖥️ Dashboard

The web dashboard at **http://localhost:8000** has 7 tabs:

| Tab | What you get |
|-----|-------------|
| **Chat** | Full agent chat with markdown rendering, approval gate, live tool feed |
| **Tasks** | Persistent task list — add, complete, delete |
| **Schedule** | View recurring scheduled tasks |
| **Metrics** | Token usage, P95 latency, provider health table with tier + cost/1M |
| **Knowledge** | Upload documents (PDF, MD, TXT), search the knowledge base |
| **Plugins** | Enable/disable skills, category filter, integration marketplace |
| **Settings** | Server info, domain setup, nginx config generator, integrations status |

The right sidebar shows live provider status and a real-time tool-call feed.

---

## 🧩 Plugin Skill Library

XClaw ships with 6 built-in skills, managed from the Plugins tab:

| Skill | Tools |
|-------|-------|
| **Research** | `research_topic`, `find_sources`, `summarize_url` |
| **Coding** | `run_code`, `install_package`, `lint_python`, `format_code`, `git_status`, `git_diff` |
| **Writing** | `word_count`, `extract_key_points`, `clean_text`, `generate_template` |
| **Productivity** | `break_down_task`, `time_block_day`, `estimate_effort`, `morning_brief_template` |
| **Computer** | `browser_open`, `browser_screenshot`, `browser_fill_and_submit`, `browser_extract_links` |
| **Persona** | `get_persona`, `set_persona`, `set_social_handle`, `set_hashtags`, `draft_social_post` |

Enable/disable any skill without restarting. Add your own by dropping a `.py` file in `plugins/`.

---

## 🐝 Agent Swarm

For complex multi-part tasks, XClaw can deploy a swarm of parallel agents:

```
You: "swarm: research AI coding tools, analyse the top 5, and write an exec summary"

XClaw Swarm:
  [researcher] → fetches web data on AI coding tools
  [analyst]    → compares features, pricing, GitHub stars      (all running
  [writer]     → drafts executive summary                       in parallel)
  [synthesiser] → merges all outputs into one coherent report
```

Use it via chat: `swarm: <your complex task>` or the `swarm_task` tool is called automatically when the agent loop detects a parallelisable task.

---

## 💬 Slash Commands

| Command | Description |
|---|---|
| `/plan <goal>` | Break goal into parallel steps, approve then execute |
| `/tasks` | Show your task list |
| `/history` | Recent execution history |
| `/kb` | Knowledge base stats |
| `/sources` | List ingested documents |
| `/schedule <task> <interval>` | Schedule a recurring task |
| `/scheduled` | List all scheduled tasks |
| `/metrics` | Token usage, latency P95, provider health |
| `/providers` | LLM provider status and active routing tier |
| `/help` | All commands |

---

## 🔧 Configuration

```bash
cp .env.example .env
# Add at least one LLM key, then:
python main.py
```

**Startup modes:**

```bash
python main.py                          # web dashboard (default, port 8000)
python main.py --interface all          # web + Telegram simultaneously
python main.py --interface telegram     # Telegram bot only
python main.py --interface cli          # terminal only
python main.py --port 8080              # custom port
```

**Docker:**

```bash
docker compose up -d                    # web + Telegram, health-checked
docker compose up -d xclaw-web         # web only
```

**Minimum config per feature:**

```bash
# Local inference (no API key):
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2

# + Telegram bot:
TELEGRAM_BOT_TOKEN=your_token           # create at t.me/BotFather

# + GitHub tools (5000 req/hr vs 60 without):
GITHUB_TOKEN=ghp_...

# + Email identity (read + send):
SMTP_USER=xclaw@gmail.com
SMTP_PASS=your_app_password             # myaccount.google.com/apppasswords
IMAP_HOST=imap.gmail.com

# + DigitalOcean GenAI:
DO_AI_ENDPOINT=https://your-endpoint.ondigitalocean.app/v1
DO_API_KEY=your_do_token
DO_AI_MODEL=llama3-70b-instruct

# + MCP servers (edit mcp_servers.json):
# filesystem, brave-search, puppeteer, github, postgres — all pre-templated
```

---

## 🛠️ Add a Tool (5 lines)

```python
# plugins/my_skill.py
PLUGIN_META = {
    "name": "my_skill", "display_name": "My Skill",
    "description": "What this skill does", "version": "1.0.0",
    "category": "automation", "tags": ["example"], "enabled_by_default": True,
    "requires": [],
}

async def my_custom_tool(query: str, limit: int = 10) -> str:
    """One-line description the LLM uses to decide when to call this."""
    # ... your implementation
    return result
```

Drop it in `plugins/` — XClaw auto-discovers it on next startup. Enable/disable from the dashboard.

---

## 📊 Real Use Cases

**Morning brief (scheduled):**
> "Schedule a daily 8am brief: check HN top 5, BTC price, weather, and my pending tasks"

**Parallel research:**
> "Swarm: research quantum computing breakthroughs in 2025, analyse key papers, write a 500-word summary"

**GitHub workflow:**
> "List open issues on navigatorxm/XClaw and create an issue for the KB schema bug"

**Browser automation:**
> "Take a screenshot of my dashboard and extract all links from the page"

**Persona + social:**
> "Draft a LinkedIn post about our new XClaw release in my persona's voice"

**Market watch:**
> "What's the current BTC price and top 5 crypto gainers today?"

**Document Q&A:**
> Upload a PDF → "Summarise the key points from my research paper"

---

## 🖥️ Platform Support

| Platform | Status | Notes |
|----------|--------|-------|
| Ubuntu / Debian VPS | ✅ Full | nginx + systemd setup included |
| macOS | ✅ Full | Ollama native |
| Windows | ✅ Full | PowerShell + Docker |
| Raspberry Pi 4/5 | ✅ Full | Groq (cloud) or Ollama 3B local |
| Jetson Nano (4GB) | ✅ Full | GPU Ollama, reduced context config |
| Docker (x86/ARM64) | ✅ Full | `docker compose up -d` |

**→ See [INSTALL.md](INSTALL.md) for step-by-step guides for each platform.**

---

## 🤝 Contributing

1. Fork the repo
2. Create a branch: `git checkout -b feature/my-tool`
3. Add your plugin in `plugins/` or tool in `agents/integrations/`
4. Open a PR

All contributions welcome — new skills, provider integrations, dashboard improvements.

---

## 📄 License

MIT — use it, fork it, build on it.

---

<div align="center">

**Built with ❤️ by [Navigator](https://github.com/navigatorxm)**

*XClaw: because your AI assistant should actually do things.*

</div>
