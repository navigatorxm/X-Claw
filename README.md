# XClaw — NavOS AI Executive Assistant

> Navigator gives intent → XClaw plans → Navigator approves → XClaw executes → Reports back.
> Never acts without confirmation. Always explains what it's about to do.

---

## Architecture

```
Navigator (Telegram / Web / CLI)
         │
         ▼
    XClaw Gateway          ← normalises all input channels
         │
         ▼
    Commander              ← Intent → Plan → Approve → Execute loop
         │
    LLM Brain              ← OVH Qwen 14B (primary) + Groq/Gemini/OpenAI fallbacks
         │
         ▼
    Router → Agent Swarm
              ├── research   Web search, summarise, monitor
              ├── content    Write, format, draft emails
              ├── leads      Find, qualify, outreach
              ├── tasks      Plan, track, remind
              ├── markets    Price alerts, analysis
              ├── code       Generate, review, execute
              └── [+yours]   Add any agent via SKILL.md
```

---

## Quickstart

### 1. Clone & configure

```bash
git clone https://github.com/navigatorxm/OpenClaw
cd OpenClaw
cp .env.example .env
# Edit .env — add at least one LLM API key
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run

**CLI (no API keys needed for dev):**
```bash
python main.py --interface cli
```

**Web dashboard:**
```bash
python main.py --interface web
# Open http://localhost:8000
```

**Telegram bot:**
```bash
python main.py --interface telegram
```

**Docker (all-in-one):**
```bash
docker compose up -d
```

---

## Example Flow

```
Navigator › Research competitors of Harver Space

XClaw › Here's my plan:

  1. [research] Search aerospace startups in India
  2. [research] Pull company profiles and summaries
  3. [content]  Format results as a comparison table

  Estimated time: ~3m 0s
  Agents involved: research, content

  ✅ Reply 'yes' to approve   ❌ Reply 'no' to cancel

Navigator › yes

XClaw › Done. Here's what I found:

  **Step 1 (research):**
  ...
```

---

## LLM Priority

| Priority | Provider | Model | Cost |
|----------|----------|-------|------|
| 1 (primary) | OVH AI Endpoints | Qwen2.5-14B-Instruct | Free tier |
| 2 | Groq | llama-3.3-70b | Free tier |
| 3 | Gemini | gemini-2.0-flash | Free tier |
| 4 | OpenAI | gpt-4o-mini | Paid |

Configure in `brain/config.yaml`. Keys go in `.env`.

---

## Adding a New Agent

See [`agents/SKILL.md`](agents/SKILL.md) — takes ~15 minutes.

---

## Repo Structure

```
OpenClaw/
├── main.py                 ← Entry point
├── core/
│   ├── gateway.py          ← Unified interface normaliser
│   ├── commander.py        ← Intent → Plan → Approve → Execute
│   ├── router.py           ← Agent dispatcher
│   └── memory.py           ← SQLite + Markdown persistence
├── agents/
│   ├── research.py
│   ├── content.py
│   ├── leads.py
│   ├── tasks.py
│   ├── markets.py
│   ├── code.py
│   └── SKILL.md            ← How to add agents
├── brain/
│   ├── llm_router.py       ← Multi-provider LLM with fallback
│   └── config.yaml         ← Model configuration
├── interface/
│   ├── telegram.py
│   ├── web/app.py
│   └── cli.py
├── memory/
│   ├── context.md          ← Navigator's rolling context
│   └── logs/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## Philosophy

- **Private first** — OVH runs locally, nothing leaves your infra by default.
- **Approval gate** — XClaw never takes action without Navigator's confirmation.
- **Extensible** — add an agent in one file, register in one line.
- **Simple** — SQLite + Markdown files, no external services required to start.
