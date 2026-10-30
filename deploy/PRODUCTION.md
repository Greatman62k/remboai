# Production deployment checklist

Point DNS to server, allow 80/443 for Caddy TLS.

```bash
cp .env.example .env && nano .env
cp deploy/.env.example deploy/.env && nano deploy/.env
openssl rand -hex 32  # TOKEN_ENCRYPTION_KEY, OPS_API_KEY
openssl rand -hex 16  # MPESA_CALLBACK_SECRET
openssl rand -hex 24  # POSTGRES_PASSWORD, REDIS_PASSWORD
```

Set `MPESA_ENV=production`, `MPESA_VERIFY_CALLBACKS=true`, `PUBLIC_BASE_URL=https://$DOMAIN`, strong `OPS_API_KEY`/`TOKEN_ENCRYPTION_KEY`, `DOMAIN`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`.

Migrations apply on start, in filename order; never skip or reorder them. Fresh volumes run automatically.

Encrypt legacy tokens:
```
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/encrypt_salon_tokens.py
.venv/bin/python scripts/encrypt_salon_tokens.py --apply
```

Validate:
```
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env config --quiet
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

Start:
```
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env up -d --build
```

Verify `https://$DOMAIN/health`, set Meta webhook to `https://$DOMAIN/webhook`, Daraja callback `https://$DOMAIN/mpesa/callback/<secret>`, then register a salon from chat with `/start register`.

Ops key must be 64+ hex with distinct chars; callback secret 16+ with distinct chars; token key 64 hex; ingress 120/600/60 until load test.
