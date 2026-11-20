#!/bin/bash
# Install frps on the Aliyun ECS (Ubuntu). Usage: sudo bash install-frps.sh <token>
# The token must match the laptop's frpc token (stored in C:\dafeiyu-open\api_keys.json).
set -euo pipefail
TOKEN="${1:?usage: sudo bash install-frps.sh <token>}"
VER="0.72.0"
cd /tmp
if [ ! -f "frp_${VER}_linux_amd64.tar.gz" ]; then
  wget "https://github.com/fatedier/frp/releases/download/v${VER}/frp_${VER}_linux_amd64.tar.gz"
fi
tar -xzf "frp_${VER}_linux_amd64.tar.gz"
cp "frp_${VER}_linux_amd64/frps" /usr/local/bin/frps
chmod +x /usr/local/bin/frps
mkdir -p /etc/frp
printf 'bindPort = 7000\nauth.token = "%s"\n' "$TOKEN" > /etc/frp/frps.toml
cat > /etc/systemd/system/frps.service <<'UNIT'
[Unit]
Description=frp server
After=network.target
[Service]
Type=simple
ExecStart=/usr/local/bin/frps -c /etc/frp/frps.toml
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now frps
systemctl is-active frps
ss -tlnp | grep 7000 || true
