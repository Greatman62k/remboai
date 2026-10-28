#!/usr/bin/env bash
# One-time hardening of a fresh Ubuntu 24.04 VPS. Run as root:  bash deploy/server-setup.sh
set -euo pipefail

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl ufw fail2ban unattended-upgrades

# Docker (official convenience script)
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh

# Firewall: SSH, HTTP (certificate challenge and redirect), HTTPS. Nothing else is reachable.
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

systemctl enable --now fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades

# Swap, so a memory spike does not kill the database on a small VPS
if ! swapon --show | grep -q .; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
echo "Server ready. Next: clone the repository, create .env, then follow docs/DEPLOY.md."
