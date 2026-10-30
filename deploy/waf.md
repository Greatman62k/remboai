# WAF and edge protection

RemboAI has three layers: host firewall, Caddy and provider WAF. None replaces the others.

## 1. Host (ufw + fail2ban + sysctl)

`deploy/server-setup.sh` does:
- `ufw --force reset` → `deny incoming / allow outgoing`, `limit 22/tcp` (6 attempts / 30s, then 10 min block), `allow 80/443`.
- `sysctl 99-remboai.conf`: syncookies, rp_filter, `tcp_max_syn_backlog 2048`, `somaxconn 1024`, `disable_ipv6`.
- `fail2ban jail.local`: `sshd` 5/10m→24h, `rembo-429` 20/5m→1h, `rembo-5xx` 10/5m→2h, `banaction=ufw[reject]`.
- `unattended-upgrades` daily, `Remove-Unused-Kernel-Packages`, `Automatic-Reboot false` (enable if you want nightly reboot).

Verify:
```
ufw status verbose
fail2ban-client status
fail2ban-client status sshd
fail2ban-client status rembo-429
sysctl net.ipv4.tcp_syncookies
```

## 2. Caddy (edge)

`deploy/Caddyfile`:
- `max_header_size 8KiB`, `read_body 15s/5s`, `request_body 256KiB`, `max_conns_per_host 48`.
- `@app` allowlist: only `/health /ready /webhook /mpesa/callback/* /ops/*` proxied, else `404`.
- `app` is `read_only + tmpfs /tmp + cap_drop ALL + no-new-privileges + no-access-log`.

`app/hardening.py`:
- `IngressRateLimit` before `BodyLimit`, Redis Lua + local overflow, `503` in production if Redis down.
- `BodyLimit` counts streamed `body` bytes, not just `Content-Length`.
- `TRUSTED_PROXY_CIDRS` (see `app/config.py`): only those CIDRs may set `X-Forwarded-For`; otherwise client IP is `scope[client]`.

## 3. Provider WAF / DDoS

Host + Caddy preserve local capacity, not network link. Put `$DOMAIN` behind Cloudflare/AWS Shield/GCP Armor:
- DNS → orange cloud / proxy, `Cache: bypass` for `/webhook` `/mpesa/*` `/ops/*`.
- Rate limit at provider: 100 rps/IP, challenge if `429` burst.
- Allow-list `ops` to office IPs via `Caddyfile` `remote_ip` or provider rule:
```
@ops path /ops/*
handle @ops {
  @office remote_ip 203.0.113.0/24 198.51.100.5
  handle @office { reverse_proxy app:8080 }
  respond 403
}
```
- Keep `DOMAIN` → origin IP secret; rotate origin IP if leaked.

## 4. Ops hardening

- `OPS_API_KEY` 64+ chars/8 distinct, `TOKEN_ENCRYPTION_KEY` 64 hex; `scripts/doctor.py` checks `ufw`/`fail2ban`/`Caddy validate`.
- Rotate `OPS_API_KEY` via `deploy/.env` + `docker compose up -d caddy` if exposed.
- Monitor `app:8080/metrics` (see `docs/METRICS.md`) and provider WAF logs.
