#!/usr/bin/env bash
# Provision a fresh Ubuntu 22.04+ droplet to run the CV Agent webhook.
#
# Run as root on the droplet AFTER you have:
#   1. cloned the repo to /opt/cv_agent
#   2. copied deploy/.env.template to /etc/cv_agent.env and filled in values
#   3. copied your Google service-account JSON to /opt/cv_agent/credentials.json
#
# Usage: sudo bash /opt/cv_agent/deploy/install.sh

set -euo pipefail

REPO_DIR=/opt/cv_agent
SERVICE_USER=cvagent
ENV_FILE=/etc/cv_agent.env
LOG_DIR=/var/log/cv_agent

if [[ $EUID -ne 0 ]]; then
    echo "Run as root (sudo)." >&2
    exit 1
fi

if [[ ! -d "$REPO_DIR" ]]; then
    echo "Repo not found at $REPO_DIR. Clone it there first." >&2
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Env file $ENV_FILE missing. Copy deploy/.env.template and fill it in." >&2
    exit 1
fi

echo "=== Installing system packages ==="
apt-get update
apt-get install -y \
    python3 python3-venv python3-pip \
    git curl ca-certificates \
    debian-keyring debian-archive-keyring apt-transport-https \
    libnss3 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2t64 libpangocairo-1.0-0 libpango-1.0-0

echo "=== Installing Caddy ==="
if ! command -v caddy >/dev/null; then
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt | tee /etc/apt/sources.list.d/caddy-stable.list
    apt-get update
    apt-get install -y caddy
fi

echo "=== Creating service user $SERVICE_USER ==="
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$REPO_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_DIR"

echo "=== Setting up Python venv + dependencies ==="
sudo -u "$SERVICE_USER" python3 -m venv "$REPO_DIR/.venv"
sudo -u "$SERVICE_USER" "$REPO_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt"

echo "=== Installing Playwright Chromium ==="
sudo -u "$SERVICE_USER" "$REPO_DIR/.venv/bin/playwright" install chromium

echo "=== Securing env file ==="
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

echo "=== Creating log dir ==="
mkdir -p "$LOG_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$LOG_DIR"
chmod 755 "$LOG_DIR"
mkdir -p /var/log/caddy

echo "=== Installing systemd unit ==="
cp "$REPO_DIR/deploy/cv_agent.service" /etc/systemd/system/cv_agent.service
systemctl daemon-reload
systemctl enable cv_agent.service
systemctl restart cv_agent.service

echo "=== Installing Caddyfile ==="
if [[ ! -f /etc/caddy/Caddyfile.bak ]]; then
    cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak 2>/dev/null || true
fi
cp "$REPO_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
echo
echo "EDIT /etc/caddy/Caddyfile and replace <SUBDOMAIN> with your duckdns hostname,"
echo "then run:   systemctl reload caddy"
echo
echo "=== Priming FlowCV cache ==="
echo "Run this manually now (interactive output may help debug FlowCV login):"
echo "  sudo -u $SERVICE_USER $REPO_DIR/.venv/bin/python $REPO_DIR/tools/read_cv_flowcv.py --refresh"
echo
echo "=== Status ==="
systemctl --no-pager status cv_agent.service || true

echo
echo "Done. Verify with: curl https://<your-subdomain>.duckdns.org/health"
