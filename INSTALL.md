# XClaw — Installation Guide

> Install on a VPS, local computer, Raspberry Pi, Jetson Nano, or Docker.

---

## Requirements

| | Minimum | Recommended |
|---|---|---|
| **Python** | 3.11 | 3.12 |
| **RAM** | 512 MB (cloud LLM) | 4 GB+ (local Ollama) |
| **Disk** | 500 MB | 2 GB+ (for Ollama models) |
| **OS** | Linux / macOS / Windows | Ubuntu 22.04 LTS |
| **LLM** | One free API key | Groq (fastest free) or Ollama (local) |

**One LLM key is the only hard requirement.** Everything else is optional.

---

## 1. VPS (Ubuntu 22.04 / Debian 12)

### Install & start

```bash
# System dependencies
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git

# Clone XClaw
git clone https://github.com/navigatorxm/X-Claw.git && cd X-Claw

# Interactive setup — sets LLM keys, Telegram, domain, nginx, systemd
bash scripts/setup.sh

# Launch
source .venv/bin/activate
python main.py
# → http://YOUR_SERVER_IP:8000
```

### Add a domain + HTTPS

```bash
sudo apt install -y nginx certbot python3-certbot-nginx

# Then open dashboard → Settings → "Connect a Domain"
# Paste the 4 generated commands — nginx config + certbot HTTPS
```

### Always-on service (auto-restarts on crash / reboot)

```bash
sudo cp /tmp/xclaw.service /etc/systemd/system/xclaw.service
sudo systemctl enable --now xclaw

# Monitor
sudo journalctl -u xclaw -f
```

### Telegram bot (reach XClaw from your phone)

```bash
# Add to .env:
TELEGRAM_BOT_TOKEN=your_bot_token   # create at t.me/BotFather

# Start both web dashboard + Telegram simultaneously
python main.py --interface all
```

### Recommended free LLM for VPS

**Groq** — fastest free cloud inference, no credit card needed.
```
GROQ_API_KEY=your_key    # console.groq.com → free in 2 minutes
```

---

## 2. Local Computer

### macOS / Linux

```bash
git clone https://github.com/navigatorxm/X-Claw.git && cd X-Claw
bash scripts/quickstart.sh        # creates .venv, installs packages
cp .env.example .env
nano .env                          # add at least one LLM key
source .venv/bin/activate
python main.py
# → http://localhost:8000
```

### Windows

```powershell
git clone https://github.com/navigatorxm/X-Claw.git
cd X-Claw
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
notepad .env                       # add at least one LLM key
python main.py
```

### Zero-cost local inference with Ollama

```bash
# 1. Install Ollama:  https://ollama.com
# 2. Pull a model
ollama pull llama3.2               # 2 GB download, fast responses

# 3. In .env:
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2

# 4. Start XClaw — no API key needed, fully private
python main.py
```

### Docker (any OS with Docker Desktop)

```bash
cp .env.example .env               # fill in at least one LLM key
docker compose up -d
# → http://localhost:8000
```

---

## 3. Raspberry Pi

**Recommended**: Pi 4 (4 GB) or Pi 5. Pi 3 works fine with cloud providers (Groq/Gemini).

### Install

```bash
# Python 3.11 is included in Raspberry Pi OS Bookworm
sudo apt update && sudo apt install -y python3.11 python3.11-venv git

git clone https://github.com/navigatorxm/X-Claw.git && cd X-Claw
bash scripts/quickstart.sh

# Best free LLM for Pi: Groq (no GPU needed)
nano .env
# → GROQ_API_KEY=your_key

source .venv/bin/activate
python main.py --port 8000
```

### Access from your phone or any device on the same Wi-Fi

```bash
hostname -I          # shows Pi's local IP, e.g. 192.168.1.42
# Open: http://192.168.1.42:8000 on any device on the network
```

### Ollama on Raspberry Pi (Pi 4 / Pi 5)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.2              # ~2 GB, runs on 4 GB RAM Pi

# In .env:
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2
```

### Always-on at boot

```bash
bash scripts/setup.sh              # generates /tmp/xclaw.service
sudo cp /tmp/xclaw.service /etc/systemd/system/xclaw.service
sudo systemctl enable --now xclaw
```

### Pi performance tips

- **Use Groq or Gemini** for most tasks — cloud inference is faster than CPU Ollama on Pi
- **Ollama model**: use `llama3.2` (3B) not `llama3.1:70b` — Pi has no discrete GPU
- **Reduce context** in `brain/config.yaml`: set `max_context_tokens: 3000`
- **Pi 5 + NVMe SSD**: significantly faster SQLite I/O for memory/tasks.db

---

## 4. NVIDIA Jetson Nano (4 GB)

The Jetson Nano has a 128-core Maxwell GPU — run Ollama with full GPU acceleration.

### Install Python 3.11 (Nano ships with Python 3.6)

```bash
sudo apt update
sudo apt install -y software-properties-common
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt install -y python3.11 python3.11-venv python3.11-distutils git
```

### Install XClaw

```bash
git clone https://github.com/navigatorxm/X-Claw.git && cd X-Claw

python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cp .env.example .env
nano .env
```

### GPU-accelerated Ollama on Jetson Nano

```bash
# Install Ollama (ARM64 build — works on Jetson)
curl -fsSL https://ollama.com/install.sh | sh

# Models that fit in 4 GB VRAM:
ollama pull llama3.2              # best balance of speed + quality
ollama pull phi3:mini             # fastest, very low memory

# In .env:
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2

# Start
python main.py
```

### Reduce resource usage for Nano (edit `brain/config.yaml`)

```yaml
agent_loop:
  max_iterations: 10          # default 20 — reduce for Nano
  max_context_tokens: 3000    # default 6000 — reduce for Nano
```

### Always-on service

```bash
bash scripts/setup.sh
sudo cp /tmp/xclaw.service /etc/systemd/system/xclaw.service
sudo systemctl enable --now xclaw
```

---

## 5. Docker (Any Platform)

Works on x86-64, ARM64 (Pi, Jetson), macOS (Apple Silicon), and Windows.

### Start

```bash
git clone https://github.com/navigatorxm/X-Claw.git && cd X-Claw
cp .env.example .env
nano .env                          # add at least one LLM key

docker compose up -d xclaw-web    # web only
# OR
docker compose up -d               # web + telegram bot

# Logs
docker compose logs -f

# Update XClaw
git pull && docker compose build && docker compose up -d
```

### Ollama + Docker (Ollama runs on host, XClaw in container)

```bash
# Install Ollama on your host machine first, then:

# In .env:
OLLAMA_HOST=http://host.docker.internal:11434   # macOS / Windows Docker Desktop
OLLAMA_HOST=http://172.17.0.1:11434             # Linux
```

### ARM (Raspberry Pi / Jetson)

```bash
# The Dockerfile uses python:3.12-slim which auto-pulls the ARM64 image
docker compose up -d               # no changes needed for ARM
```

---

## LLM Provider Quick Reference

| Provider | Cost | Best For | Get Key |
|----------|------|----------|---------|
| **Groq** | Free (6k tok/min) | VPS, Pi without GPU | [console.groq.com](https://console.groq.com) |
| **Gemini Flash** | Free tier | General backup | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| **Ollama** | Free (local) | Pi 4, Jetson, full privacy | [ollama.com](https://ollama.com) |
| **DigitalOcean** | Pay-per-token | DO VPS, serverless | [cloud.digitalocean.com/gen-ai](https://cloud.digitalocean.com/gen-ai) |
| **OpenAI** | Pay-per-token | Highest accuracy | [platform.openai.com](https://platform.openai.com/api-keys) |
| **OVH AI** | Free trial | EU data residency | [endpoints.ai.cloud.ovh.net](https://endpoints.ai.cloud.ovh.net) |

**XClaw auto-routes requests** to the cheapest capable model:
- Simple questions → cheap tier (Groq 8B, Gemini Flash)
- Research/analysis → standard tier (Groq 70B, Gemini Flash)
- Complex builds → premium tier (GPT-4o, Gemini Pro)

---

## All Startup Modes

```bash
python main.py                          # web dashboard (default, port 8000)
python main.py --interface web          # same as above
python main.py --interface telegram     # Telegram bot only
python main.py --interface all          # web + Telegram simultaneously
python main.py --interface cli          # terminal, no web server
python main.py --port 8080              # custom port
python main.py --host 127.0.0.1        # bind to localhost only
PORT=9000 python main.py               # port via env var
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `No LLM providers configured` | Add at least one key to `.env` |
| Port 8000 already in use | `python main.py --port 8080` |
| Telegram bot not responding | Check `TELEGRAM_BOT_TOKEN` in `.env` |
| Slow responses on Pi / Jetson | Use Groq (cloud) instead of large local model |
| `python3.11` not found | `sudo apt install python3.11 python3.11-venv` |
| `Permission denied: memory/` | `mkdir -p memory/logs && chmod 755 memory` |
| Systemd service won't start | `journalctl -u xclaw -n 50` — check logs |
| Ollama not connecting | Verify `ollama serve` is running: `curl http://localhost:11434` |
| Docker build fails on ARM | Use `docker buildx build --platform linux/arm64 .` |
| `.env` changes not picked up | Restart XClaw: `sudo systemctl restart xclaw` |
