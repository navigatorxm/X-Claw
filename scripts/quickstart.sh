#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  XClaw Quickstart — zero-friction setup in ~60 seconds
#  Usage: bash scripts/quickstart.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
CYAN="\033[36m"
RESET="\033[0m"

info()    { echo -e "${CYAN}[xclaw]${RESET} $*"; }
success() { echo -e "${GREEN}[xclaw]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[xclaw]${RESET} $*"; }

echo -e "\n${BOLD}╔═══════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║   XClaw — AI Executive Assistant v3   ║${RESET}"
echo -e "${BOLD}╚═══════════════════════════════════════╝${RESET}\n"

# ── 1. Python check ──────────────────────────────────────────────────────────
info "Checking Python version..."
python_version=$(python3 --version 2>&1 | awk '{print $2}')
major=$(echo "$python_version" | cut -d. -f1)
minor=$(echo "$python_version" | cut -d. -f2)
if [[ $major -lt 3 || ($major -eq 3 && $minor -lt 11) ]]; then
    warn "Python 3.11+ recommended (you have $python_version)"
else
    success "Python $python_version ✓"
fi

# ── 2. Virtual environment ────────────────────────────────────────────────────
if [[ ! -d ".venv" ]]; then
    info "Creating virtual environment..."
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
success "Virtual environment active ✓"

# ── 3. Dependencies ───────────────────────────────────────────────────────────
info "Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
success "Dependencies installed ✓"

# ── 4. Environment file ───────────────────────────────────────────────────────
if [[ ! -f ".env" ]]; then
    cp .env.example .env
    warn "Created .env from .env.example"
    warn "→ Open .env and set at least ONE LLM provider key before starting."
    echo ""
    echo -e "  ${BOLD}Quick options:${RESET}"
    echo -e "  • Groq (free, fast):  https://console.groq.com"
    echo -e "  • Gemini (free):      https://aistudio.google.com/apikey"
    echo -e "  • Ollama (local):     bash scripts/install_ollama.sh"
    echo ""
else
    success ".env already exists ✓"
fi

# ── 5. Memory directory ───────────────────────────────────────────────────────
mkdir -p memory/kb
success "Storage directories ready ✓"

# ── 6. Done ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}═══════════════════════════════════════${RESET}"
echo -e "${GREEN}${BOLD}  Setup complete! Start XClaw:${RESET}"
echo -e "${GREEN}${BOLD}═══════════════════════════════════════${RESET}"
echo ""
echo -e "  ${BOLD}Web interface:${RESET}"
echo -e "  $ python main.py"
echo -e "  → Open http://localhost:8000\n"
echo -e "  ${BOLD}CLI only:${RESET}"
echo -e "  $ python main.py --cli\n"
echo -e "  ${BOLD}Telegram bot:${RESET}"
echo -e "  $ TELEGRAM_BOT_TOKEN=... python main.py --telegram\n"
