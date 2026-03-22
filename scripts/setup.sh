#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  XClaw Interactive Setup — configure, install, and launch in one command
#  Usage: bash scripts/setup.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BOLD="\033[1m"; DIM="\033[2m"; RESET="\033[0m"
GREEN="\033[32m"; YELLOW="\033[33m"; CYAN="\033[36m"; RED="\033[31m"; WHITE="\033[97m"

ok()   { echo -e "${GREEN}  ✓${RESET} $*"; }
info() { echo -e "${CYAN}  →${RESET} $*"; }
warn() { echo -e "${YELLOW}  !${RESET} $*"; }
err()  { echo -e "${RED}  ✗${RESET} $*"; }
ask()  { echo -e "${WHITE}${BOLD}  ?${RESET} $*"; }

hr() { echo -e "${DIM}  ──────────────────────────────────────────────${RESET}"; }

# ── Banner ────────────────────────────────────────────────────────────────────

clear
echo ""
echo -e "${BOLD}${WHITE}"
echo "   ██╗  ██╗ ██████╗██╗      █████╗ ██╗    ██╗"
echo "    ╚██╗██╔╝██╔════╝██║     ██╔══██╗██║    ██║"
echo "     ╚███╔╝ ██║     ██║     ███████║██║ █╗ ██║"
echo "     ██╔██╗ ██║     ██║     ██╔══██║██║███╗██║"
echo "    ██╔╝ ██╗╚██████╗███████╗██║  ██║╚███╔███╔╝"
echo "    ╚═╝  ╚═╝ ╚═════╝╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝ "
echo -e "${RESET}"
echo -e "  ${DIM}AI Executive Assistant — Interactive Setup${RESET}"
echo ""
hr
echo ""

# ── Dependency check ──────────────────────────────────────────────────────────

info "Checking dependencies..."

if ! command -v python3 &>/dev/null; then
    err "Python 3 not found. Install it: sudo apt install python3.11"
    exit 1
fi
PY=$(python3 --version 2>&1 | awk '{print $2}')
ok "Python $PY"

# ── Virtualenv ────────────────────────────────────────────────────────────────

if [[ ! -d ".venv" ]]; then
    info "Creating virtual environment..."
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
ok "Virtual environment active"

info "Installing Python dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
ok "Dependencies installed"

echo ""
hr
echo -e "\n  ${BOLD}Step 1 — Choose your LLM provider${RESET}\n"
echo -e "  ${DIM}XClaw will try providers in order until one responds.${RESET}"
echo -e "  ${DIM}You can add multiple. Pick your primary first.${RESET}\n"
echo "  [1] Groq          — free tier, fastest (recommended to start)"
echo "  [2] Google Gemini — free tier, great quality"
echo "  [3] Ollama        — 100% local, zero cost, full privacy"
echo "  [4] OpenAI        — GPT-4o-mini, pay-as-you-go"
echo "  [5] DigitalOcean  — serverless inference, pay-per-token"
echo "  [6] OVH AI        — EU-hosted, private cloud"
echo "  [7] Skip          — I'll edit .env manually"
echo ""

# Accumulate env vars
declare -A ENV_VARS
ENV_VARS["LOG_LEVEL"]="INFO"
ENV_VARS["PORT"]="8000"
PROVIDERS_CHOSEN=()

add_provider() {
    local choice="$1"
    case "$choice" in
    1)
        echo ""
        ask "Groq API key (https://console.groq.com):"
        read -r -p "    " key
        if [[ -n "$key" ]]; then
            ENV_VARS["GROQ_API_KEY"]="$key"
            PROVIDERS_CHOSEN+=("Groq")
            ok "Groq configured"
        fi
        ;;
    2)
        echo ""
        ask "Gemini API key (https://aistudio.google.com/apikey):"
        read -r -p "    " key
        if [[ -n "$key" ]]; then
            ENV_VARS["GEMINI_API_KEY"]="$key"
            PROVIDERS_CHOSEN+=("Gemini")
            ok "Gemini configured"
        fi
        ;;
    3)
        echo ""
        info "Ollama runs locally. Make sure it's installed first."
        info "Install: curl -fsSL https://ollama.com/install.sh | sh"
        ask "Ollama host [http://localhost:11434]:"
        read -r -p "    " host
        host="${host:-http://localhost:11434}"
        ask "Model name [llama3.2]:"
        read -r -p "    " model
        model="${model:-llama3.2}"
        ENV_VARS["OLLAMA_HOST"]="$host"
        ENV_VARS["OLLAMA_MODEL"]="$model"
        PROVIDERS_CHOSEN+=("Ollama ($model)")
        ok "Ollama configured ($host)"
        if command -v ollama &>/dev/null; then
            info "Pulling model $model (this may take a few minutes)..."
            ollama pull "$model" || warn "Pull failed — start Ollama and run: ollama pull $model"
        else
            warn "Ollama not installed yet. Run: curl -fsSL https://ollama.com/install.sh | sh"
        fi
        ;;
    4)
        echo ""
        ask "OpenAI API key (https://platform.openai.com/api-keys):"
        read -r -p "    " key
        if [[ -n "$key" ]]; then
            ENV_VARS["OPENAI_API_KEY"]="$key"
            PROVIDERS_CHOSEN+=("OpenAI")
            ok "OpenAI configured"
        fi
        ;;
    5)
        echo ""
        info "DigitalOcean GenAI: https://cloud.digitalocean.com/gen-ai"
        ask "DO AI endpoint URL (e.g. https://your-app.ondigitalocean.app):"
        read -r -p "    " endpoint
        ask "DO API key:"
        read -r -p "    " key
        ask "Model name [llama3-8b-instruct]:"
        read -r -p "    " model
        model="${model:-llama3-8b-instruct}"
        if [[ -n "$key" && -n "$endpoint" ]]; then
            ENV_VARS["DO_AI_ENDPOINT"]="$endpoint"
            ENV_VARS["DO_API_KEY"]="$key"
            ENV_VARS["DO_AI_MODEL"]="$model"
            PROVIDERS_CHOSEN+=("DigitalOcean")
            ok "DigitalOcean configured"
        fi
        ;;
    6)
        echo ""
        ask "OVH AI endpoint URL:"
        read -r -p "    " endpoint
        ask "OVH API key:"
        read -r -p "    " key
        if [[ -n "$key" && -n "$endpoint" ]]; then
            ENV_VARS["OVH_AI_ENDPOINT"]="$endpoint"
            ENV_VARS["OVH_API_KEY"]="$key"
            PROVIDERS_CHOSEN+=("OVH")
            ok "OVH configured"
        fi
        ;;
    7)
        warn "Skipping LLM config — edit .env before running XClaw."
        ;;
    esac
}

ask "Choose provider [1-6]:"
read -r -p "    " primary_choice
add_provider "$primary_choice"

# Add more providers
while true; do
    echo ""
    ask "Add another provider as fallback? [y/N]"
    read -r -p "    " more
    [[ "$more" =~ ^[Yy]$ ]] || break
    echo ""
    echo "  [1] Groq  [2] Gemini  [3] Ollama  [4] OpenAI  [5] DigitalOcean  [6] OVH"
    ask "Choose:"
    read -r -p "    " extra_choice
    add_provider "$extra_choice"
done

# ── Telegram ──────────────────────────────────────────────────────────────────

echo ""
hr
echo -e "\n  ${BOLD}Step 2 — Telegram Bot (optional)${RESET}\n"
echo -e "  ${DIM}Chat with XClaw from anywhere via Telegram.${RESET}"
echo -e "  ${DIM}Create a bot: open Telegram → @BotFather → /newbot${RESET}\n"
ask "Telegram Bot Token (press Enter to skip):"
read -r -p "    " tg_token
if [[ -n "$tg_token" ]]; then
    ENV_VARS["TELEGRAM_BOT_TOKEN"]="$tg_token"
    ok "Telegram configured"
else
    info "Skipped — you can add TELEGRAM_BOT_TOKEN to .env later"
fi

# ── GitHub ────────────────────────────────────────────────────────────────────

echo ""
hr
echo -e "\n  ${BOLD}Step 3 — GitHub Token (optional)${RESET}\n"
echo -e "  ${DIM}Without: 60 req/hr on public repos.${RESET}"
echo -e "  ${DIM}With token: 5000 req/hr + private repos.${RESET}"
echo -e "  ${DIM}Create at: https://github.com/settings/tokens${RESET}\n"
ask "GitHub Personal Access Token (press Enter to skip):"
read -r -p "    " gh_token
if [[ -n "$gh_token" ]]; then
    ENV_VARS["GITHUB_TOKEN"]="$gh_token"
    ok "GitHub configured"
else
    info "Skipped — GitHub tools work without a token (rate-limited)"
fi

# ── Email ─────────────────────────────────────────────────────────────────────

echo ""
hr
echo -e "\n  ${BOLD}Step 4 — Email / SMTP (optional)${RESET}\n"
echo -e "  ${DIM}Lets XClaw send emails on your behalf.${RESET}"
echo -e "  ${DIM}Gmail: use an App Password from myaccount.google.com/apppasswords${RESET}\n"
ask "Configure email? [y/N]:"
read -r -p "    " do_email
if [[ "$do_email" =~ ^[Yy]$ ]]; then
    ask "SMTP host [smtp.gmail.com]:"
    read -r -p "    " smtp_host
    smtp_host="${smtp_host:-smtp.gmail.com}"
    ask "SMTP port [587]:"
    read -r -p "    " smtp_port
    smtp_port="${smtp_port:-587}"
    ask "Your email address:"
    read -r -p "    " smtp_user
    ask "App password (not your regular password):"
    read -r -s -p "    " smtp_pass
    echo ""
    if [[ -n "$smtp_user" && -n "$smtp_pass" ]]; then
        ENV_VARS["SMTP_HOST"]="$smtp_host"
        ENV_VARS["SMTP_PORT"]="$smtp_port"
        ENV_VARS["SMTP_USER"]="$smtp_user"
        ENV_VARS["SMTP_PASS"]="$smtp_pass"
        ENV_VARS["SMTP_FROM"]="XClaw <$smtp_user>"
        ok "Email configured ($smtp_user)"
    fi
else
    info "Skipped — add SMTP_* to .env later to enable email sending"
fi

# ── Domain / Port ─────────────────────────────────────────────────────────────

echo ""
hr
echo -e "\n  ${BOLD}Step 5 — Domain / Port${RESET}\n"
ask "Port to run XClaw on [8000]:"
read -r -p "    " port
port="${port:-8000}"
ENV_VARS["PORT"]="$port"

ask "Do you have a domain to point at this server? [y/N]:"
read -r -p "    " has_domain
DOMAIN=""
if [[ "$has_domain" =~ ^[Yy]$ ]]; then
    ask "Your domain (e.g. xclaw.yourdomain.com):"
    read -r -p "    " DOMAIN
    if [[ -n "$DOMAIN" ]]; then
        ok "Will generate nginx config for $DOMAIN"
    fi
fi

# ── Write .env ────────────────────────────────────────────────────────────────

echo ""
hr
echo -e "\n  ${BOLD}Writing configuration…${RESET}\n"

{
    echo "# XClaw Configuration — generated by setup.sh"
    echo "# $(date)"
    echo ""
    echo "# ── LLM Providers ──────────────────────────────────────────────────"
    for key in GROQ_API_KEY GEMINI_API_KEY OPENAI_API_KEY DO_AI_ENDPOINT DO_API_KEY DO_AI_MODEL OVH_AI_ENDPOINT OVH_API_KEY OLLAMA_HOST OLLAMA_MODEL; do
        [[ -n "${ENV_VARS[$key]+x}" ]] && echo "${key}=${ENV_VARS[$key]}"
    done
    echo ""
    echo "# ── Interfaces ─────────────────────────────────────────────────────"
    for key in TELEGRAM_BOT_TOKEN; do
        [[ -n "${ENV_VARS[$key]+x}" ]] && echo "${key}=${ENV_VARS[$key]}"
    done
    echo ""
    echo "# ── Integrations ───────────────────────────────────────────────────"
    for key in GITHUB_TOKEN; do
        [[ -n "${ENV_VARS[$key]+x}" ]] && echo "${key}=${ENV_VARS[$key]}"
    done
    echo ""
    echo "# ── Email ──────────────────────────────────────────────────────────"
    for key in SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASS SMTP_FROM; do
        [[ -n "${ENV_VARS[$key]+x}" ]] && echo "${key}=${ENV_VARS[$key]}"
    done
    echo ""
    echo "# ── App ────────────────────────────────────────────────────────────"
    echo "LOG_LEVEL=${ENV_VARS[LOG_LEVEL]}"
    echo "PORT=${ENV_VARS[PORT]}"
} > .env

ok ".env written"

# ── Storage dirs ──────────────────────────────────────────────────────────────

mkdir -p memory/kb memory/logs
ok "Storage directories ready"

# ── Nginx config ──────────────────────────────────────────────────────────────

if [[ -n "$DOMAIN" ]]; then
    NGINX_CONF="/etc/nginx/sites-available/xclaw"
    cat > /tmp/xclaw_nginx.conf << NGINX
server {
    listen 80;
    server_name ${DOMAIN};

    # Required for WebSocket + SSE streaming
    proxy_buffering         off;
    proxy_read_timeout      300s;
    proxy_connect_timeout   10s;

    location / {
        proxy_pass         http://127.0.0.1:${port};
        proxy_http_version 1.1;
        proxy_set_header   Upgrade \$http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
    }
}
NGINX
    ok "Nginx config created at /tmp/xclaw_nginx.conf"
    echo ""
    echo -e "  ${DIM}To activate nginx + HTTPS:${RESET}"
    echo "  sudo cp /tmp/xclaw_nginx.conf $NGINX_CONF"
    echo "  sudo ln -sf $NGINX_CONF /etc/nginx/sites-enabled/xclaw"
    echo "  sudo nginx -t && sudo systemctl reload nginx"
    echo "  sudo apt install -y certbot python3-certbot-nginx"
    echo "  sudo certbot --nginx -d $DOMAIN"
fi

# ── Systemd service ───────────────────────────────────────────────────────────

echo ""
hr
echo -e "\n  ${BOLD}Step 6 — Run as a service${RESET}\n"
echo -e "  ${DIM}This keeps XClaw running after you close your terminal.${RESET}\n"
ask "Set up systemd service? [y/N]:"
read -r -p "    " do_service

if [[ "$do_service" =~ ^[Yy]$ ]]; then
    XCLAW_DIR="$(pwd)"
    XCLAW_USER="$(whoami)"
    SERVICE_FILE="/tmp/xclaw.service"

    cat > "$SERVICE_FILE" << SERVICE
[Unit]
Description=XClaw AI Executive Assistant
After=network.target

[Service]
Type=simple
User=${XCLAW_USER}
WorkingDirectory=${XCLAW_DIR}
EnvironmentFile=${XCLAW_DIR}/.env
ExecStart=${XCLAW_DIR}/.venv/bin/python main.py --interface web --port ${port}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

    if sudo cp "$SERVICE_FILE" /etc/systemd/system/xclaw.service 2>/dev/null; then
        sudo systemctl daemon-reload
        sudo systemctl enable xclaw
        sudo systemctl start xclaw
        ok "XClaw service installed and started"
        info "Manage with: sudo systemctl {status|stop|restart|logs} xclaw"
        info "View logs:   journalctl -u xclaw -f"
    else
        warn "Could not install systemd service (no sudo). Service file at: $SERVICE_FILE"
        info "To install manually: sudo cp $SERVICE_FILE /etc/systemd/system/xclaw.service"
        info "Then: sudo systemctl daemon-reload && sudo systemctl enable --now xclaw"
    fi
else
    info "Skipped. Start manually with: python main.py --interface web"
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
hr
echo ""
echo -e "  ${BOLD}${GREEN}Setup complete!${RESET}"
echo ""

if [[ "${#PROVIDERS_CHOSEN[@]}" -gt 0 ]]; then
    echo -e "  ${DIM}LLM providers:${RESET} ${PROVIDERS_CHOSEN[*]}"
fi
[[ -n "${ENV_VARS[TELEGRAM_BOT_TOKEN]+x}" ]] && echo -e "  ${DIM}Telegram:${RESET} configured"
[[ -n "${ENV_VARS[GITHUB_TOKEN]+x}" ]]       && echo -e "  ${DIM}GitHub:${RESET} configured"
[[ -n "${ENV_VARS[SMTP_USER]+x}" ]]          && echo -e "  ${DIM}Email:${RESET} configured"

echo ""

if [[ "$do_service" =~ ^[Yy]$ ]]; then
    echo -e "  ${BOLD}XClaw is running as a service.${RESET}"
else
    echo -e "  ${BOLD}Start XClaw:${RESET}"
    echo "  source .venv/bin/activate && python main.py --interface web"
fi

echo ""
if [[ -n "$DOMAIN" ]]; then
    echo -e "  ${BOLD}→ Dashboard:${RESET} https://${DOMAIN}  (after nginx setup)"
else
    echo -e "  ${BOLD}→ Dashboard:${RESET} http://$(hostname -I | awk '{print $1}' 2>/dev/null || echo "YOUR_IP"):${port}"
fi
echo ""
hr
echo ""
