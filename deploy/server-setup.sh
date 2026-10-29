#!/usr/bin/env bash
set -euo pipefail
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl ufw fail2ban unattended-upgrades
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw limit 22/tcp comment 'ssh rate-limited'
ufw allow 80/tcp comment 'http acme'
ufw allow 443/tcp comment 'https app'
ufw --force enable
cat >/etc/sysctl.d/99-remboai.conf <<'SYS'
net.ipv4.tcp_syncookies=1
net.ipv4.conf.all.rp_filter=1
net.ipv4.conf.default.rp_filter=1
net.ipv4.tcp_max_syn_backlog=2048
net.core.somaxconn=1024
net.ipv4.ip_forward=0
net.ipv6.conf.all.disable_ipv6=1
SYS
sysctl -p /etc/sysctl.d/99-remboai.conf --system >/dev/null 2>&1 || sysctl -p /etc/sysctl.d/99-remboai.conf >/dev/null 2>&1 || true
install -m 644 deploy/fail2ban/jail.local /etc/fail2ban/jail.local
install -m 644 deploy/fail2ban/filter.d/rembo-429.conf /etc/fail2ban/filter.d/rembo-429.conf
install -m 644 deploy/fail2ban/filter.d/rembo-5xx.conf /etc/fail2ban/filter.d/rembo-5xx.conf
systemctl enable --now fail2ban
systemctl restart fail2ban || true
mkdir -p /etc/apt/apt.conf.d
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'APT'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
Unattended-Upgrade::Automatic-Reboot "false";
APT
cat >/etc/apt/apt.conf.d/50unattended-upgrades <<'APT2'
Unattended-Upgrade::Mail "";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
APT2
dpkg-reconfigure -f noninteractive unattended-upgrades || true
if ! swapon --show | grep -q .; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
echo "Server ready. Next: clone repo, create .env, follow docs/DEPLOY.md."
echo "Firewall: $(ufw status | head -n 20)"
echo "Fail2ban: $(fail2ban-client status 2>&1 | head -n 20)"
